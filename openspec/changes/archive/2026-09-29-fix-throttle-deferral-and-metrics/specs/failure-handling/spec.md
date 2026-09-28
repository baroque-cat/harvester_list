## MODIFIED Requirements

### Requirement: Empty-result taxonomy at the fetch boundary
The system SHALL classify every empty search/gather fetch outcome as either a **legitimate zero** (the remote answered successfully with zero matching items) or a **failure-empty** (no usable answer was obtained: transport exception after retries exhausted, blank or undecodable payload, or a **search** fetch suppressed by the local rate limiter). Failure-empties MUST NOT be returned to callers as zero-result answers. A fetch that was **refused before completion** — on a published rate limit, or **withheld by the system's own budget before any request was sent** — is a third category, a *deferral*, and SHALL NOT be collapsed into either of the other two: it is not an answer, and it is not a task-level fault. Parsing/extraction of a successfully fetched payload remains fail-open: malformed or absent fields degrade to empty/NULL with a counted warning, never an exception.

The two suppression cases are deliberately distinct, because the signals are distinct: a search-boundary suppression surfaces as a blank payload from a call that returns content, and is classified failure-empty; a gather fetch withheld by its own budget never produces a payload at all and is classified as a deferral.

#### Scenario: Legitimate zero passes through cleanly
- **WHEN** a search fetch receives a successful response (HTTP 200) whose parsed item set is empty
- **THEN** the stage receives an empty result with total count from the response, the task completes normally, and no failure counter is incremented

#### Scenario: Network failure after retries is a failure-empty
- **WHEN** a search fetch exhausts its retry budget on network errors (timeout/connection/5xx)
- **THEN** the outcome is classified as failure-empty and surfaced as a typed transient failure (strict mode) or detected+counted+logged (shadow mode) — never returned as an empty result set

#### Scenario: Limiter suppression is a failure-empty
- **WHEN** a request is suppressed by the local rate limiter and would previously yield an empty payload without any network call
- **THEN** the outcome is classified as failure-empty (retryable), not as a zero-result answer

#### Scenario: Rate-limit refusal is a deferral, not an answer and not a task fault
- **WHEN** a gather fetch is refused on a published rate limit before any payload was obtained
- **THEN** the outcome is classified as a deferral, it is not reported to callers as a zero-result answer, it is not recorded as a completed gather, and it increments a deferral counter distinct from the failure counters

#### Scenario: Gather withholding by the system's own budget is a deferral
- **WHEN** a gather fetch is withheld by the local rate-limit budget and no request reaches the network
- **THEN** the outcome is classified as a deferral — never returned as a zero-result answer, never counted as a failure-empty, never recorded as a completed or failed gather — and it increments the deferral counter family rather than any failure counter

#### Scenario: The search boundary keeps its failure-empty classification
- **WHEN** a search fetch is suppressed by the local rate limiter and surfaces as a blank payload
- **THEN** it remains classified as a failure-empty and follows the bounded-retry path exactly as before this amendment, so no search-side pin or behavior changes

### Requirement: Bounded retry and honest accounting for failed tasks
In strict mode, a task whose execution raised a typed transient failure SHALL be re-enqueued by the worker-loop retry policy with its attempt counter incremented, SHALL be counted as an error (not a success), and SHALL be dropped loudly (warning log + dedicated counter) once the configured maximum requeue count is exceeded. A failed task MUST NOT produce a handler-visible success outcome and MUST increment the error counter; the pre-existing `total_processed` figure remains a throughput counter over every executed task (successes and errors alike, unchanged by this capability). The deduplication gate MUST admit re-enqueued tasks whose attempt count is within bounds. A task **deferred** on a published rate limit, **or withheld by the system's own budget before any request was sent**, SHALL instead return to the pending state with its attempt counter **unchanged**, because neither is a property of the task: the first is a property of the remote budget, the second of our own throttling decision. Deferral SHALL be bounded by the queue's own age gate so a deferred task cannot circulate forever, and SHALL NOT consume the requeue budget that exists to terminate genuinely faulty work.

#### Scenario: Transient failure triggers bounded requeue
- **WHEN** a stage task fails with a typed transient failure under `failure_handling: strict`
- **THEN** the task is re-enqueued with attempts incremented by one, `total_errors` (or equivalent) increments, and the task is NOT counted as successfully processed

#### Scenario: Re-enqueue passes the deduplication gate
- **WHEN** a previously enqueued (already dedup-marked) task is re-enqueued with an attempt count greater than zero and not exceeding the configured maximum
- **THEN** the deduplication gate accepts it and the task reaches a worker again

#### Scenario: Exhausted retries drop loudly
- **WHEN** a task's attempt count exceeds the configured maximum requeue bound
- **THEN** the task is discarded with a warning log identifying stage, provider and task, and the drop counter increments — the task never completes silently

#### Scenario: Deferral does not consume the retry budget
- **WHEN** a task is deferred on a published rate limit and later succeeds within the same run
- **THEN** its attempt counter is unchanged by the deferral, it is not counted in the max-retries drop counter, and the successful completion is accounted normally

#### Scenario: Deferral cannot circulate forever
- **WHEN** a task is deferred repeatedly and its original creation time passes the queue age gate
- **THEN** the task is purged by the existing age-gate maintenance with a loud warning stating the count, rather than remaining claimable indefinitely

#### Scenario: Own-budget withholding preserves the task's identity and age
- **WHEN** a gather task is withheld by the system's own budget and returned to the pending state, repeatedly if necessary
- **THEN** its attempt counter, creation timestamp and deduplication identity are all preserved across every withholding, no error, requeue or processed counter advances for it, and the queue's age gate remains the single ceiling on how long it may circulate
