# Proposal: fix-queue-persistence-under-load

## Why

All pipeline state lives in RAM `queue.Queue`s and reaches disk only via a periodic JSON snapshot that drains each queue and puts tasks back (`stage/base.py:329-350`). Under load this loses work three ways, all observed in the 2026-09-21 OOM incident (thousands of `lost task during persistence`): drain-and-put-back races with workers and drops on `queue.Full`; `put_task` silently drops on `Full` after a 1 s timeout (`stage/base.py:274-289`); and an OOM kill loses everything queued since the last snapshot (up to `queue_interval`=60 s — the production shutdown path never runs a final save: `GracefulShutdown` is dead code, `Pipeline._on_stop` stops `QueueManager` without saving). Snapshotting 4 M tasks also costs +2–3 GB transient RAM (`to_dict` × N plus `json.dumps(indent=2)`), feeding the very OOM it is supposed to survive. R1 (`add-refine-fanout-governor`, archived) bounded work *generation*; disease B — work *materialization* — is untouched, and today's loss-freedom rests on governor policy that one config flag can reverse.

## What Changes

- New opt-in durable queue backend: per-stage SQLite (WAL) task store `<workspace>/queue_state/{stage}_queue.sqlite`, selected by new config section `queue.backend: memory | sqlite` (default `memory` — byte-identical rollback by config flip).
- New engine `storage/task_queue.py` (`SqliteTaskQueue`): FIFO by monotonic seq, claim-with-visibility-timeout `get()`, ack-on-`task_done()` delete, crash-orphan reclaim at startup, periodic expired-claim sweep, duck-typed to the exact `queue.Queue` surface the codebase uses (`put/get/get_nowait/put_nowait/empty/qsize/task_done`, raises `queue.Empty`).
- `sqlite` backend makes enqueue loss structurally impossible: `put` is a committed INSERT (never `Full`, never blocks beyond SQLite busy timeout); `get_pending_tasks()` becomes a non-destructive SELECT — the drain race and `lost task during persistence` class disappear; periodic O(N) JSON serialization of live queues stops entirely.
- One-shot idempotent importer of legacy `{stage}_queue.json` snapshots (both envelope formats) into SQLite, honoring the existing `max_age_hours` gate; imported files renamed so re-import cannot double-apply.
- Shutdown durability fix for **both** backends: `Pipeline._on_stop` performs a final `save_all_queues` before stopping `QueueManager` (closes the dead-`GracefulShutdown` gap where a clean stop could lose up to 60 s of queued work).
- Fail-open construction: if the SQLite store cannot be opened/bootstrapped, the stage falls back to the memory backend with a loud ERROR and a degraded marker — harvest availability first, never silent.
- New loud counter `tasks_dropped_backend_errors` on `StageMetrics` for runtime write failures (parallels `tasks_dropped_max_retries`); a dropped task is always counted and logged.
- Semantics pinned identical across backends: dedup gate, bounded-retry requeue, worker-loop counters/log text, `stop()` drain, `is_finished`/`is_busy` — the `failure-handling` pins stay green unmodified.
- Docs: README section, `examples/config-full.yaml` block, promotion guidance for flipping `config.yaml` after live verification.
- Explicitly OUT of scope (design D9/D10): lazy child-generation cursor (superseded by R1 partition clamp), hot-RAM/cold-disk spill tiering (breaks strict FIFO that aggregation locality relies on), fingerprint column for R3 grouping (trivial additive migration later), resurrection of `GracefulShutdown`, `__slots__`/RSS watchdog (R4).

## Capabilities

### New Capabilities

- `durable-task-queue`: loss-free task enqueue/dequeue with a durable SQLite backend behind a config flag — FIFO order, claim/ack lifecycle with visibility timeout, at-least-once crash recovery, legacy JSON import with age gate, non-destructive pending snapshots, fail-open construction, cross-backend semantic parity, and shutdown durability.

### Modified Capabilities

(none — `failure-handling`, `search-aggregation`, and `refine-fanout-governor` requirements are untouched: the memory backend keeps today's behavior byte-identically, and the sqlite backend preserves every pinned contract.)

## Impact

- **Code**: new `storage/task_queue.py`; modified `stage/base.py` (ctor kwargs `queue_backend`/`queue_dir`, backend-aware `get_pending_tasks`, new drop counter), `manager/pipeline.py` (pass backend to stages, final save in `_on_stop`), `manager/queue.py` (backend-aware save/load/state-info, legacy importer, reclaim/sweep scheduling), `core/metrics.py` (one field), `config/schemas.py` + `config/loader.py` + `config/validator.py` (new `TaskQueueConfig`), docs/examples.
- **Data**: new `{stage}_queue.sqlite` (+`-wal`/`-shm`) files under existing `queue_state/`; legacy JSONs renamed to `*.imported-<ts>` after one-shot import. No result shards, registry, or fixture formats change.
- **Interfaces**: `BasePipelineStage.__init__` gains optional keyword-only args (all subclasses pass `**kwargs` through — verified `stage/definition.py:74,532,667,870`); `stage.queue` remains duck-compatible (`empty()` pin in `tests/test_fh_stage_modes.py:98` holds on both backends).
- **Dependencies**: stdlib `sqlite3` only (already project DNA: `registry.sqlite`, WAL + `synchronous=NORMAL` precedent in `storage/registry.py:462-464`).
- **Operations**: RAM footprint of queued work becomes O(workers) instead of O(queue length) under `sqlite`; disk grows instead (~0.3–0.6 KB/row); `queue_sizes` caps no longer apply under `sqlite` (disk is the bound; R1 governor bounds search inflow). Rollback = flip `queue.backend` to `memory`.
- **Security note**: CheckTask/InspectTask payloads contain plaintext `service.key` — already true of today's queue JSON snapshots; SQLite store inherits the same workspace-local trust boundary (no new exposure; documented).
