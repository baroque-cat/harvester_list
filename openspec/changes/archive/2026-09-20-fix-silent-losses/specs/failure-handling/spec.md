# Delta Spec: failure-handling

## Purpose

Guarantees that transient fetch failures are never accounted as successful completions: empties are classified (legitimate zero vs failure-empty), failures propagate into the existing bounded-retry machinery, gather outcomes stay truthful in the persistent registry, and the whole contract rolls out behind a tri-mode flag with shadow measurement before enforcement.

## ADDED Requirements

### Requirement: Empty-result taxonomy at the fetch boundary
The system SHALL classify every empty search/gather fetch outcome as either a **legitimate zero** (the remote answered successfully with zero matching items) or a **failure-empty** (no usable answer was obtained: transport exception after retries exhausted, local limiter suppression, blank or undecodable payload). Failure-empties MUST NOT be returned to callers as zero-result answers. Parsing/extraction of a successfully fetched payload remains fail-open: malformed or absent fields degrade to empty/NULL with a counted warning, never an exception.

#### Scenario: Legitimate zero passes through cleanly
- **WHEN** a search fetch receives a successful response (HTTP 200) whose parsed item set is empty
- **THEN** the stage receives an empty result with total count from the response, the task completes normally, and no failure counter is incremented

#### Scenario: Network failure after retries is a failure-empty
- **WHEN** a search fetch exhausts its retry budget on network errors (timeout/connection/5xx)
- **THEN** the outcome is classified as failure-empty and surfaced as a typed transient failure (strict mode) or detected+counted+logged (shadow mode) — never returned as an empty result set

#### Scenario: Limiter suppression is a failure-empty
- **WHEN** a request is suppressed by the local rate limiter and would previously yield an empty payload without any network call
- **THEN** the outcome is classified as failure-empty (retryable), not as a zero-result answer

### Requirement: Bounded retry and honest accounting for failed tasks
In strict mode, a task whose execution raised a typed transient failure SHALL be re-enqueued by the worker-loop retry policy with its attempt counter incremented, SHALL be counted as an error (not a success), and SHALL be dropped loudly (warning log + dedicated counter) once the configured maximum requeue count is exceeded. A failed task MUST NOT produce a handler-visible success outcome and MUST increment the error counter; the pre-existing `total_processed` figure remains a throughput counter over every executed task (successes and errors alike, unchanged by this capability). The deduplication gate MUST admit re-enqueued tasks whose attempt count is within bounds.

#### Scenario: Transient failure triggers bounded requeue
- **WHEN** a stage task fails with a typed transient failure under `failure_handling: strict`
- **THEN** the task is re-enqueued with attempts incremented by one, `total_errors` (or equivalent) increments, and the task is NOT counted as successfully processed

#### Scenario: Re-enqueue passes the deduplication gate
- **WHEN** a previously enqueued (already dedup-marked) task is re-enqueued with an attempt count greater than zero and not exceeding the configured maximum
- **THEN** the deduplication gate accepts it and the task reaches a worker again

#### Scenario: Exhausted retries drop loudly
- **WHEN** a task's attempt count exceeds the configured maximum requeue bound
- **THEN** the task is discarded with a warning log identifying stage, provider and task, and the drop counter increments — the task never completes silently

### Requirement: Gather-outcome fidelity
A gather outcome SHALL be recorded as successful in the persistent registry only when the blob HTTP fetch actually completed successfully. A failed fetch (transient network error, timeout, or HTTP 404) SHALL be recorded as a failed gather (no coverage row), making the existing failed-gather re-attempt path reachable. A successful fetch that yields zero extracted keys SHALL still be recorded as successful with its coverage row ("researched, nothing found" remains distinct from "never researched").

#### Scenario: Fetch failure never produces gathered_ok
- **WHEN** an acquisition task's blob fetch fails (injected network error, timeout, or 404 response)
- **THEN** the registry records the gather as failed (`visit_status='failed'`), no `link_coverage` row is written for this attempt, and the task follows the bounded-retry path

#### Scenario: Zero-key success keeps coverage semantics
- **WHEN** an acquisition task's blob fetch succeeds but pattern extraction yields zero services
- **THEN** the registry records `gathered_ok` and writes the coverage row for `(provider, patterns_hash)` exactly as before this change

### Requirement: Check tasks survive limiter starvation
When the provider-side rate-limit bucket cannot be acquired even after the scheduled wait, the check task SHALL be re-enqueued through the bounded-retry path instead of being dropped, so no harvested key silently loses its validation.

#### Scenario: Starved check requeues
- **WHEN** a check task finds its provider limiter bucket unavailable after waiting
- **THEN** the task is re-enqueued with attempts incremented (bounded), and no silent completion is recorded

### Requirement: Tri-mode rollout flag
The failure-handling contract SHALL be governed by a configuration flag with three modes: `legacy` (behavior byte-equivalent to the pre-change system, with the new detection counters inert — present but zero, never incremented — the pure rollback state), `shadow` (failure-empties detected, counted and logged with stage/provider/task context, while task outcomes remain exactly as in legacy), and `strict` (full enforcement of propagation, requeue and fidelity requirements above). Default at release: `shadow`. Switching modes SHALL require only a configuration change, never code removal.

#### Scenario: Legacy mode is indistinguishable from pre-change behavior
- **WHEN** the pipeline runs under `failure_handling: legacy` against injected transient failures
- **THEN** task outcomes, statistics and logs match the pre-change behavior, and the new detection counters do not operate

#### Scenario: Shadow mode measures without enforcing
- **WHEN** the pipeline runs under `failure_handling: shadow` against injected transient failures
- **THEN** every failure-empty increments `failure_empties_detected` for its stage and emits a contextual log line, while the affected tasks complete exactly as they would in legacy mode

#### Scenario: Strict mode enforces
- **WHEN** the pipeline runs under `failure_handling: strict` against injected transient failures
- **THEN** the propagation, bounded-requeue and gather-fidelity requirements take effect

#### Scenario: Rollback is a config flip
- **WHEN** an operator switches the flag from `strict` to `legacy` and restarts
- **THEN** the system exhibits legacy behavior with no code change and no data migration

### Requirement: Failure observability counters
The system SHALL expose per-stage counters: `failure_empties_detected` (shadow and strict modes), `tasks_requeued`, and `tasks_dropped_max_retries`, surfaced through the existing status/metrics structures without registry-schema or shard-format changes.

#### Scenario: Counters visible in status
- **WHEN** transient failures occur under shadow or strict mode
- **THEN** the corresponding counters are readable from the pipeline status/metrics surface alongside existing stage metrics

### Requirement: Existing protections are preserved
Credential-cooldown mechanics SHALL remain untouched: rate-limit signals (HTTP 429/403 and soft-block content) keep rotating credentials through the existing bounded-backoff channel (60→900s escalation) and MUST NOT be double-handled as generic transient failures; an all-credentials-cooling pool keeps blocking (waiting) rather than dropping tasks; NDJSON shard formats and `recover_tasks()` replay semantics remain unchanged.

#### Scenario: Cooldown rotation unchanged under the new contract
- **WHEN** a credential hits a rate limit during search in strict mode
- **THEN** the rotation loop exchanges the credential and proceeds exactly as before this change, with backoff escalating per the existing schedule, and the event is not counted as a failure-empty requeue

#### Scenario: Full-pool cooldown waits, never drops
- **WHEN** every pooled credential is cooling down while a search task needs one
- **THEN** the task blocks until the earliest cooldown release and then proceeds; it is never dropped or completed-empty by this mechanism

#### Scenario: Restart recovery unaffected
- **WHEN** the process restarts after runs that included transient failures
- **THEN** queue-file and shard-based recovery replays the same task sets as before this change (formats untouched)
