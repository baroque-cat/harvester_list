# Delta Spec: gather-skip

## Purpose

Suppresses creation of acquisition tasks for GitHub files that the harvester has already successfully gathered under the current provider and pattern set and that have not changed since, turning repeated runs from full re-fetches into delta collection — without ever skipping on incomplete or erroneous evidence. Amended (September 2026) after live probes established that `repo_pushed_at` is NULL in the prevailing production state (date-extraction design D7): absence of push evidence degrades precision to TTL-bounded staleness instead of vetoing skips outright; evidence quality is made visible via `push_signal_coverage`.

## ADDED Requirements

### Requirement: Conjunctive skip rule

The system SHALL skip creating an acquisition task for a discovered link only when ALL of the following hold: (1) registry `visit_status = 'gathered_ok'`; (2) `gathered_ts` is within the configured gather TTL; (3) no positive evidence of change — IF `repo_pushed_at` is non-NULL THEN it is NOT the case that `repo_pushed_at > gathered_ts` (plus grace); a NULL `repo_pushed_at` satisfies this condition vacuously (freshness unknown; staleness is bounded by condition 2 — the documented TTL-only degradation mode, which is the prevailing production state until `add-repo-meta-enrichment` supplies dates); (4) a `link_coverage` row exists for the current `(provider, patterns_hash)`. If condition (1), (2) or (4) fails, if the registry row or `gathered_ts` is missing, or if the lookup errored, the task SHALL be created.

#### Scenario: Fully known fresh covered link is skipped

- **WHEN** a link was gathered successfully 1 day ago for the same provider and unchanged patterns, and its repo has no newer push
- **THEN** no acquisition task is created and `skipped_known` increments

#### Scenario: Never-gathered link is not skipped

- **WHEN** a link exists in the registry with `visit_status='discovered'` (seen but never gathered)
- **THEN** the acquisition task IS created

#### Scenario: TTL expiry forces re-gather

- **WHEN** a link's `gathered_ts` is older than the configured gather TTL
- **THEN** the task IS created and `regathered_ttl_expired` increments

#### Scenario: Fresh push invalidates prior gather

- **WHEN** `repo_pushed_at` is non-NULL and later than the link's `gathered_ts` (beyond grace)
- **THEN** the task IS created and `regathered_changed` increments

#### Scenario: Provider or pattern switch forces re-gather

- **WHEN** the link was gathered under provider A or an older patterns hash, and the current run uses provider B or changed patterns
- **THEN** the task IS created and `regathered_coverage_gap` increments

#### Scenario: NULL push date does not veto skip

- **WHEN** a link is `gathered_ok` within TTL with matching coverage but `repo_pushed_at` is NULL
- **THEN** no acquisition task is created, `skipped_known` increments, and the decision-log conditions snapshot marks push evidence as absent

### Requirement: Three-mode rollout flag

Skip behavior SHALL be controlled by `skip_known` with values `off` (default), `shadow`, and `on`. In `off` mode the registry SHALL NOT be read at all during search. In `shadow` mode every decision SHALL be computed and logged to a structured decision log while all tasks are still created. In `on` mode skips are enforced.

#### Scenario: Off mode is indistinguishable from pre-change behavior

- **WHEN** the pipeline runs with `skip_known=off`
- **THEN** no registry reads occur in the search stage and task creation matches the pre-change implementation exactly

#### Scenario: Shadow mode logs without acting

- **WHEN** the pipeline runs with `skip_known=shadow` over links that satisfy the skip rule
- **THEN** every would-skip candidate appears in the decision log with its url_hash and satisfied-condition snapshot, AND the acquisition tasks are still created and executed

### Requirement: Failed gathers are never skipped

Links whose last gather attempt failed SHALL always produce a new acquisition task regardless of recency, and SHALL be attributed to `regathered_failed_retry`.

#### Scenario: Failed link re-gathered despite fresh timestamp

- **WHEN** a link has `visit_status='failed'` with a recent `gathered_ts`
- **THEN** the task IS created and `regathered_failed_retry` increments

### Requirement: Fail-open reads

Registry read errors (locked DB, corruption, query failure) SHALL be caught in the search worker context: the affected links SHALL be treated as unknown (tasks created), a warning SHALL be logged once per kind, and the run SHALL continue. No skip decision SHALL ever be derived from an errored lookup.

#### Scenario: Read error produces tasks, not skips

- **WHEN** the batched registry lookup raises during a search worker iteration
- **THEN** acquisition tasks are created for all links of that page, a warning is logged, and the run proceeds to completion

### Requirement: Reason-attributed decision metrics

Per-run counters SHALL report `skipped_known`, the four `regathered_*` reasons (`changed`, `coverage_gap`, `ttl_expired`, `failed_retry`), each attributed by the first failing condition of the conjunctive rule, and `push_signal_coverage` — the share of evaluated already-known links whose skip decision carried non-NULL `repo_pushed_at` evidence — all exposed in run statistics.

#### Scenario: Counters reflect a mixed page of links

- **WHEN** one result page contains 1 skippable, 1 TTL-expired, 1 changed, 1 coverage-gap, 1 failed, and 1 unseen link
- **THEN** counters increment respectively `skipped_known=1`, `regathered_ttl_expired=1`, `regathered_changed=1`, `regathered_coverage_gap=1`, `regathered_failed_retry=1`, and the unseen link yields a task with no regathered attribution

#### Scenario: Push-signal coverage reported

- **WHEN** a run evaluates 100 already-known links, 30 of which carry non-NULL `repo_pushed_at`
- **THEN** run stats report `push_signal_coverage = 0.30` alongside the skip/regather counters
