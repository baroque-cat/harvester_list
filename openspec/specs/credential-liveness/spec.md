# credential-liveness Specification

## Purpose

Guarantees that the credential layer stays live under rate-limit pressure: every wait for a pooled credential is bounded and accounted, exhaustion becomes an honest task deferral instead of an infinite sleep or a dishonest empty result, proven-working credentials are released from cooldown immediately, and primary quota exhaustion is reacted to differently from subject-scoped secondary limits.

## Requirements

### Requirement: Bounded credential waits
Every sleep performed while waiting for a usable pooled credential SHALL have a deadline computed before the sleep, from the configured budget `credential_liveness.max_wait_s` (default 60 s). The selector SHALL NOT contain any unbounded wait path: neither a blocking loop over cooldown timers nor a hot spin when the earliest release time is unknown or non-positive. When the budget is spent, or when all credentials are cooling and no positive release time can be determined, the selector SHALL raise a typed exhaustion signal carrying the service, a named reason, and a best-known wait estimate; it SHALL NOT return a falsy credential for a pool that IS configured (falsy remains reserved for "no credentials configured"). Under `queue.backend: sqlite`, configuration SHALL loudly reject `credential_liveness.max_wait_s >= queue.visibility_timeout_s` at load time (no in-task sleep may outlive the claim window while no claim-renewal API exists). Each wait episode SHALL produce exactly one WARNING (masked credentials), increment an episode counter, and never log per polling iteration.

#### Scenario: Available credential returns immediately
- **WHEN** a credential is requested and at least one pooled credential is not cooling
- **THEN** it is returned without any sleep and without touching cooldown state

#### Scenario: Short cooldown is waited out within budget
- **WHEN** every pooled credential is cooling and the earliest release is sooner than `max_wait_s`
- **THEN** the selector sleeps once (at most until the earliest release), returns the released credential, and the episode is counted and logged exactly once

#### Scenario: Long cooldown exhausts the budget
- **WHEN** every pooled credential is cooling and the earliest release is later than `max_wait_s`
- **THEN** the selector raises the typed exhaustion signal after sleeping no longer than the budget in total, with reason indicating the budget was spent

#### Scenario: Non-positive wait anomaly fails fast instead of spinning
- **WHEN** all pooled credentials are cooling and the earliest-release probe returns a non-positive or undeterminable wait
- **THEN** the typed exhaustion signal is raised immediately with an anomaly reason, and no hot spin loop is entered

#### Scenario: Cross-section invariant enforced at load time
- **WHEN** a configuration sets `queue.backend: sqlite` and `credential_liveness.max_wait_s` greater than or equal to `queue.visibility_timeout_s`
- **THEN** configuration loading fails loudly with a validation error naming both keys

#### Scenario: Blocking rollback mode restores legacy behavior with a loud caveat
- **WHEN** `credential_liveness.wait_mode` is set to `blocking`
- **THEN** the selector behaves as the pre-change unbounded waiter (config-flip rollback, no code removal), and under `queue.backend: sqlite` a loud WARNING is emitted at load time that duplicate execution hazard is deliberately re-accepted

### Requirement: Exhaustion defers tasks, never drops or empties them
A stage task that cannot obtain a credential because of pool exhaustion SHALL be deferred through the existing durable deferral seam: returned to the pending queue with `attempts`, `created_at`, and dedup identity unchanged, counted in the stage's `tasks_deferred` metric, writing nothing to the link registry, and pausing the stage only within the existing bounded defer-pause cap. The task SHALL NOT be dropped, SHALL NOT be completed with an empty result, and SHALL NOT sleep inside its queue claim beyond the credential budget. Repeated exhaustion cycles SHALL remain bounded by the queue's existing `max_age_hours` age-gate with its loud purge accounting. When exhaustion episodes on one service reach `credential_liveness.emergency_threshold` consecutive trips, the system SHALL emit one loud ERROR per trip, increment an emergency counter, and continue operating (fail-open: deferral within the age-gate, never pipeline death).

#### Scenario: Search task defers on credential exhaustion
- **WHEN** a search task's credential rotation ends in the typed exhaustion signal
- **THEN** the task is re-queued as deferred with attempts/created_at/dedup identity preserved, `tasks_deferred` increments, no registry row is written for it, and the worker proceeds to the next task after at most the bounded defer pause

#### Scenario: Exhaustion never produces an empty result
- **WHEN** credentials are exhausted during a search task
- **THEN** the task produces no StageOutput with zero-result semantics and is not counted as an executed search; the honest-empty outcome remains reserved for genuinely empty fetch results

#### Scenario: Consecutive full-bench trips raise a bounded emergency
- **WHEN** exhaustion episodes on one service reach the configured emergency threshold consecutively
- **THEN** exactly one ERROR is logged per trip, the emergency counter increments, and the pipeline keeps running

#### Scenario: Deferred backlog remains bounded by the age gate
- **WHEN** a task keeps deferring on exhaustion and its original creation time exceeds `max_age_hours`
- **THEN** the existing age-gate purge removes it with the existing loud accounting; deferral itself never extends task lifetime

### Requirement: Early cooldown release on success
When a request made with a cooling-down or previously limited credential succeeds, the credential's cooldown entry SHALL be cleared immediately (not only after expiry), making it selectable at once; the release SHALL be counted and logged at most once per release. A subsequent limit on a released credential SHALL restart the backoff ladder from its minimum (the escalation schedule for REPEATED limits without intervening success is unchanged: 60→120→240→480→900 s clamp). Setting `credential_liveness.early_release: false` SHALL restore the expired-only release behavior exactly (config-flip rollback).

#### Scenario: Successful request frees a benched credential
- **WHEN** a credential under an unexpired cooldown completes a request successfully and `early_release` is true
- **THEN** it is immediately eligible for selection again and the early-release counter increments

#### Scenario: Rollback flag restores expired-only release
- **WHEN** `credential_liveness.early_release` is false
- **THEN** a success does not clear an unexpired cooldown, matching pre-change behavior byte-for-byte

#### Scenario: Repeated limits still escalate
- **WHEN** a credential is limited again without an intervening success
- **THEN** its cooldown escalates by the existing doubling schedule up to the existing maximum

### Requirement: Primary and secondary limits react differently
Rate-limit reactions SHALL be derived from the signals already read off the wire, as opportunistic contracts (signal present → use it; absent → defined safe degradation, never a guess that benches credentials):
- Header-anchored primary exhaustion (`X-RateLimit-Remaining: 0` with a usable `X-RateLimit-Reset`, or a `Retry-After` value) SHALL cool ONLY the credential that hit it, until the indicated reset, clamped by the existing cooldown bounds; actual in-task sleeping remains capped by the credential wait budget (long resets lead to deferral, not to holding the claim).
- Secondary-limit markers (body or reason matching `secondary rate limit` or `abuse detection`) SHALL cool the ENTIRE credential pool of the affected service (subject-scoped limit: continued pressure from any pooled identity worsens it), emit one loud ERROR per episode, increment a secondary-incident counter, and pause the stage within the existing bounded defer-pause.
- A 403/429 carrying NEITHER usable limit headers NOR secondary markers SHALL be treated as a transient task failure (existing bounded requeue with attempt accounting), and SHALL NOT cool any credential.
Pools of different services SHALL NOT be cooled jointly (separate budgets: a single global brake converts partial degradation into a full stop).

#### Scenario: Primary quota exhaustion cools one credential until reset
- **WHEN** a response carries `X-RateLimit-Remaining: 0` and a near-future `X-RateLimit-Reset`
- **THEN** only the credential used is cooled until the reset time (clamped), other pooled credentials remain selectable, and the wait is honored through the bounded selector

#### Scenario: Reset far in the future defers instead of sleeping
- **WHEN** the header-indicated reset lies beyond the credential wait budget
- **THEN** the credential is cooled for the indicated duration but the task in hand defers rather than sleeping out the reset inside its claim

#### Scenario: Secondary marker cools the whole service pool and escalates loudly
- **WHEN** a response body or reason matches a secondary-limit marker
- **THEN** every credential of that service enters cooldown, one ERROR is logged for the episode, the secondary-incident counter increments, and the stage pauses within the bounded defer-pause; other services' pools are untouched

#### Scenario: Ambiguous refusal never benches a credential
- **WHEN** a 403 or 429 arrives without usable limit headers and without secondary markers
- **THEN** it is handled as a transient task failure with existing attempt accounting, and no cooldown entry is created

### Requirement: Narrowed soft-block content detectors
Content-based rate-limit detection SHALL match only verifiable GitHub markers: `rate limit`, `secondary rate limit`, `abuse detection`, and (for the web search surface only) the exact full sentence `Search failed. Please try again later.`. The broad fragments `please wait` and `try again later` SHALL NOT by themselves classify a response as rate-limited on any surface. A response whose text does not match these markers but whose status indicates refusal SHALL follow the ambiguous-refusal path (transient failure, counted), never silent success.

#### Scenario: Polite fragment alone is not a limit
- **WHEN** a 403 body contains only a polite phrase such as "please wait" without any rate-limit or abuse marker
- **THEN** no credential is cooled and the response is classified as a transient failure

#### Scenario: Verifiable markers still detected
- **WHEN** a body contains `secondary rate limit` or `abuse detection`
- **THEN** the response is classified as a secondary limit with the whole-pool reaction

#### Scenario: Web search soft-block string preserved
- **WHEN** the web search surface returns its exact `Search failed. Please try again later.` sentence
- **THEN** it is still classified as rate-limited exactly as before this change

### Requirement: Honest adaptive-rate configuration
Rate-limit configuration SHALL expose only keys that influence behavior. The dead coefficients (`backoff_factor`, `recovery_factor`, `max_rate_multiplier`, `min_rate_multiplier`) SHALL be removed from the schema, validator, and documentation; when present in an operator's config file they SHALL be ignored with exactly one loud WARNING per key per load (graceful acceptance period: live configs must not crash), and SHALL have no effect. Documentation and example configs SHALL describe the real adaptive model (per-bucket rate halved after consecutive failures with a floor, recovered stepwise after consecutive successes with a cap).

#### Scenario: Dead keys warn once and are ignored
- **WHEN** a config file contains any of the removed coefficient keys
- **THEN** loading succeeds, each present key produces exactly one WARNING naming it as ignored, and runtime rate behavior is identical to a config without those keys

#### Scenario: Clean config loads silently
- **WHEN** a config file contains no removed keys
- **THEN** no acceptance-period warning is emitted

### Requirement: Credential liveness observability
Pipeline status SHALL surface a credential-metrics block with monotonically increasing counters: exhaustion episodes, tasks deferred due to exhaustion, secondary-limit incidents, early releases, and emergency trips, plus a flag indicating `blocking` rollback mode is active. Counters SHALL increment at the episode sites without adding per-request overhead beyond a counter bump.

#### Scenario: Counters readable from status
- **WHEN** exhaustion, secondary-limit, and early-release events occur during a run
- **THEN** the corresponding counters are readable from the pipeline status surface alongside existing metric blocks

### Requirement: Peripheral credential consumers stay live
Non-search consumers SHALL degrade safely on the typed exhaustion signal instead of sleeping or crashing: the startup capability probe SHALL report "no usable credential" with at most one WARNING and SHALL neither block startup nor raise; repository-metadata enrichment SHALL treat exhaustion exactly as its existing all-cooling silent yield (zero requests, NULL enrichment, pipeline proceeds); the rest-transport gather path SHALL translate exhaustion into the existing gather deferral instead of issuing an unauthenticated request that is guaranteed to fail.

#### Scenario: Startup with an all-cooling pool boots
- **WHEN** the process starts while every token is cooling
- **THEN** startup completes, the capability probe reports no usable credential with one WARNING, and no startup sleep occurs

#### Scenario: Enrichment yields silently on exhaustion
- **WHEN** enrichment needs a token and the pool is exhausted
- **THEN** enrichment skips with its existing silent-yield semantics and no exception reaches a worker loop

#### Scenario: Rest-transport gather defers instead of failing unauthenticated
- **WHEN** a gather fetch configured for the rest transport cannot obtain a token due to exhaustion
- **THEN** the fetch is deferred through the gather refusal taxonomy, and no credential-less REST request is sent
