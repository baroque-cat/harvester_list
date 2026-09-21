# Delta Spec: durable-task-queue

## Purpose

Guarantees that queued pipeline work survives process death and load spikes: a config-selected durable SQLite backend makes task loss structurally impossible (no capacity drops, no snapshot drain races, no staleness window), recovers crashed runs at-least-once, imports legacy JSON snapshots one-shot, and preserves the pinned dedup/retry/FIFO semantics identically across both backends so rollback is a single config flip.

## ADDED Requirements

### Requirement: Backend selection and loud configuration validation

The system SHALL support a `queue` configuration section with `backend` (`memory` | `sqlite`, default `memory`), `visibility_timeout_s` (> 0, default 300), and `max_age_hours` (> 0, default 24). With `backend: memory` (or no section at all) every queue-related behavior SHALL remain byte-identical to the pre-change system. With `backend: sqlite` each created stage SHALL be backed by a durable store at `<workspace>/queue_state/{stage}_queue.sqlite` opened in WAL journal mode. Invalid backend names or non-positive numeric fields SHALL fail configuration validation loudly at load time.

#### Scenario: S1 absent config selects memory backend with unchanged behavior
- **WHEN** a config without any `queue` section loads and stages are created
- **THEN** each stage queue is a plain in-memory FIFO, defaults resolve to `backend="memory"`, `visibility_timeout_s=300`, `max_age_hours=24`, and enqueue/dequeue behave exactly as before the change

#### Scenario: S2 sqlite backend creates per-stage WAL store
- **WHEN** a config with `queue.backend: sqlite` loads and a stage named `search` is created against a workspace
- **THEN** the file `<workspace>/queue_state/search_queue.sqlite` exists after construction and reports `journal_mode` equal to `wal`

#### Scenario: S3 invalid backend name fails validation loudly
- **WHEN** a config sets `queue.backend` to any value other than `memory` or `sqlite`
- **THEN** configuration loading/validation raises an error naming the offending value, and the pipeline is not constructed

#### Scenario: S4 non-positive numeric queue fields fail validation loudly
- **WHEN** a config sets `queue.visibility_timeout_s` or `queue.max_age_hours` to zero or a negative number
- **THEN** configuration loading/validation raises an error identifying the field

### Requirement: Loss-free durable enqueue

Under the `sqlite` backend, accepting a task SHALL durably commit it before reporting success; enqueue SHALL NOT be bounded by any configured queue size and SHALL NEVER fail because of capacity. If a storage write error nevertheless occurs at runtime (e.g., disk full), the enqueue SHALL report failure, log a warning, and increment a dedicated loud counter (`tasks_dropped_backend_errors`) exposed in stage metrics — a dropped task is never silent.

#### Scenario: S5 burst far beyond configured queue size is fully retained
- **WHEN** a stage built with a small configured queue size (e.g. 10) under `backend: sqlite` accepts a burst of thousands of tasks via the normal enqueue entry point
- **THEN** every enqueue returns success, the pending count equals the burst size, and no capacity-full condition is reported

#### Scenario: S6 runtime write error is loud and counted
- **WHEN** the durable store's write path raises a storage error during an enqueue (injected fault)
- **THEN** the enqueue returns failure, a warning is logged, and the stage's `tasks_dropped_backend_errors` counter increments by one while all other counters stay unchanged

### Requirement: FIFO order with claim/ack lifecycle

The durable store SHALL dequeue in strict insertion order (monotonic sequence). Dequeue SHALL atomically claim exactly one pending row (making it invisible to other consumers) and start a visibility timeout; acknowledgement SHALL delete the claimed row. Dequeue on an empty store SHALL block up to the caller's timeout and then raise the standard empty-queue exception, preserving the existing worker-loop contract. Claims whose visibility timeout expired WITHOUT acknowledgement SHALL return to the pending state on the periodic sweep.

#### Scenario: S7 dequeue order equals insertion order
- **WHEN** N distinguishable tasks are enqueued and then dequeued one by one under `backend: sqlite`
- **THEN** the observed task order is exactly the insertion order

#### Scenario: S8 claim is exclusive until acknowledged
- **WHEN** one row is claimed by a dequeue and not yet acknowledged
- **THEN** the pending count excludes the claimed row, a further dequeue with a short timeout raises the empty-queue exception, and acknowledging the claim makes the store empty

#### Scenario: S9 empty-store dequeue honors timeout and raises empty
- **WHEN** a dequeue with `timeout=0.2` runs against an empty durable store
- **THEN** it raises the standard empty-queue exception after approximately the timeout, without busy-spinning the CPU hot

#### Scenario: S10 expired claims return to pending on sweep
- **WHEN** a row was claimed with a visibility timeout that has since elapsed (short injected timeout) and the periodic sweep runs
- **THEN** the row is pending again and can be claimed by a subsequent dequeue exactly once

### Requirement: Crash recovery is at-least-once with an age gate

On store open, rows left in the claimed state (crash orphans) SHALL return to pending with payloads intact; task serialization SHALL round-trip every queued task type losslessly, including additive metadata fields and nested service structures. Rows older than `max_age_hours` SHALL be deleted during startup maintenance with a loud log stating the deleted count (parity with the legacy snapshot age gate).

#### Scenario: S11 unacknowledged claims survive reopen as pending
- **WHEN** a task is claimed, the store instance is abandoned without acknowledgement (simulating SIGKILL), and a fresh instance opens the same file
- **THEN** the task is pending again and deserializes to an equivalent task object

#### Scenario: S12 all four task types round-trip through the durable store
- **WHEN** search (with non-zero refine depth), acquisition, check (with nested service fields), and inspect tasks are enqueued, the store is closed and reopened, and all are dequeued and acknowledged
- **THEN** each dequeued task equals its original on all serialized fields, including top-level additive metadata and nested service data

#### Scenario: S13 aged-out rows are purged loudly at startup
- **WHEN** the store contains a row whose creation time is older than `max_age_hours` and startup maintenance runs
- **THEN** the row is deleted, a log message states the number of aged-out tasks, and newer rows are untouched

### Requirement: Non-destructive pending snapshot

Under the `sqlite` backend, reading the pending-task snapshot SHALL be a non-destructive query: it SHALL NOT remove, reorder, or race with concurrent producers/consumers, and the historical loss channel ("lost task during persistence") SHALL be unreachable. Periodic queue-state saving SHALL NOT serialize live queues to JSON under this backend.

#### Scenario: S14 snapshot reads leave the queue intact
- **WHEN** a pending snapshot is taken while tasks are queued
- **THEN** it returns every pending task, the pending count is unchanged afterwards, and all tasks are still dequeued exactly once in order

#### Scenario: S15 concurrent churn loses nothing
- **WHEN** producers enqueue and workers consume-and-acknowledge concurrently while snapshots are taken repeatedly (stress with exact accounting)
- **THEN** processed + still-pending equals produced, with zero losses and zero duplicates among acknowledged work

### Requirement: One-shot idempotent legacy import with opportunistic parsing

At startup under the `sqlite` backend, an existing legacy `{stage}_queue.json` snapshot SHALL be imported into the durable store when it is parseable and within the age gate, then renamed so re-import is impossible; a second startup SHALL NOT double-import. Both known envelope formats (current typed envelope and the legacy fallback shape) SHALL be accepted opportunistically: parseable content is imported; unparseable or corrupt files are skipped with a loud warning and never crash startup. Aged-out snapshots SHALL be renamed without import, loudly.

#### Scenario: S16 current-format snapshot imports once and is neutralized
- **WHEN** startup finds a `{stage}_queue.json` in the current envelope format with two tasks and the durable store is empty
- **THEN** both tasks become pending rows, the JSON file is renamed with an `.imported-` marker, and a second startup leaves the row count unchanged

#### Scenario: S17 legacy-format and corrupt files degrade safely
- **WHEN** startup finds one snapshot in the legacy fallback shape and another file that is not valid JSON
- **THEN** the legacy-shaped tasks are imported, the corrupt file is skipped with a warning, and startup completes normally

#### Scenario: S18 aged-out snapshot is parked, not imported
- **WHEN** startup finds a snapshot whose save time is older than `max_age_hours`
- **THEN** no rows are imported from it, the file is renamed with an `.expired-` marker, and a warning states the age decision

### Requirement: Shutdown durability on both backends

Graceful pipeline stop SHALL persist the final queue state before the queue manager stops: under `memory` a fresh snapshot save SHALL run at stop time (closing the gap where a clean shutdown could lose up to one save interval of work); under `sqlite` stop-time maintenance SHALL checkpoint the store and leave all pending and claimed rows intact for the next run. A failing final save SHALL be logged and SHALL NOT abort the remaining shutdown sequence.

#### Scenario: S19 graceful stop persists final state
- **WHEN** a running pipeline with queued-but-unprocessed tasks is stopped gracefully under each backend
- **THEN** under `memory` the stage snapshot file reflects the tasks queued at stop time, and under `sqlite` every unacknowledged task is still present in the store after stop; in both cases a following startup can recover them

### Requirement: Fail-open construction and cross-backend semantic parity

If the durable store cannot be opened or bootstrapped at stage construction, the stage SHALL fall back to the in-memory backend with a loud error naming the stage and cause, SHALL mark itself degraded, and SHALL remain fully functional. On BOTH backends the pinned stage contracts SHALL hold identically: the dedup gate (duplicate rejection for fresh tasks, admission of bounded requeues, loud drop at the retry cap), worker-loop retry counters and log wording, bounded draining stop, and finished/busy truthfulness.

#### Scenario: S20 unopenable store degrades to functional memory queue
- **WHEN** stage construction requests `backend: sqlite` but opening the store raises (injected fault such as an unwritable directory)
- **THEN** an error is logged naming the stage, the stage exposes a degraded-queue marker, and enqueue/dequeue continue to work on the in-memory fallback

#### Scenario: S21 dedup gate contract is identical on both backends
- **WHEN** the pinned dedup sequence runs against a stage on each backend (first enqueue; duplicate with zero attempts; requeue with attempts within the retry bound; requeue beyond the bound)
- **THEN** the outcomes are respectively accepted, rejected, accepted, rejected-with-loud-drop-counter on both backends

#### Scenario: S22 worker retry flow is identical on both backends
- **WHEN** a task that always fails transiently is processed by a started stage in strict failure-handling mode on each backend
- **THEN** the task is requeued with incremented attempts up to the bound, the requeue counter tracks each successful requeue, and at the cap the task is discarded with the pinned drop counter and warning wording on both backends
