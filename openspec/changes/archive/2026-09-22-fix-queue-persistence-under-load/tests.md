# Test Plan

Derived mechanically from `specs/durable-task-queue/spec.md` (8 requirements, 22 scenarios). Scenario IDs `durable-task-queue-S1..S22` are stable/append-only. House conventions: flat `tests/` dir, feature-prefix filenames (`tq_` = task queue), real objects over mocks where possible (real `SearchStage` with monkeypatched search client, real `QueueManager`, real SQLite files in `tmp_path`), `caplog` for log assertions, `process_task`-direct drive preferred; worker-loop drive only where the scenario is about the loop (mirrors `tests/test_fh_stage_modes.py`).

**Module under test did not exist at plan time**: `storage/task_queue.py` (`SqliteTaskQueue`) is absent at plan time — every file below fails at import (`ModuleNotFoundError`), which IS the expected RED state. `TaskQueueConfig` (config/schemas.py), stage ctor kwargs (`queue_backend`/`queue_dir`), `StageMetrics.tasks_dropped_backend_errors`, `QueueManager(backend=...)`/`attach_stages`, and the `Pipeline._on_stop` final save are likewise absent pre-implementation.

**Observed RED run (2026-09-22, plan time)**: `python3 -m pytest tests/test_tq_sqlite_queue.py tests/test_tq_stage_backend.py tests/test_tq_queue_manager.py -q` → `3 errors in 0.27s`, each collection failing with `ModuleNotFoundError: No module named 'storage.task_queue'` — exactly the predicted RED; **zero unexpected passes**. Pins baseline in the same run window: `tests/test_fh_stage_modes.py` + `tests/test_sa_baskets_e2e.py` → **11 passed** unmodified. S19 carries a second behavioral RED beyond the import failure (`Pipeline._on_stop` performs no final save today — the ordering assertion cannot pass pre-fix).

**Observed GREEN run (2026-09-22, implementation):** `python3 -m pytest tests/ -q` → **223 passed** (199 pre-existing + 24 new); all 22 scenarios green with zero tracked test files modified (`git diff --name-only -- tests/` empty). Full evidence in `verification.md`.

## Traceability

| Scenario ID | Spec | Requirement | Scenario | Test file | Status |
|-------------|------|-------------|----------|-----------|--------|
| `durable-task-queue-S1` | `specs/durable-task-queue/spec.md` | Backend selection and loud configuration validation | absent config selects memory backend with unchanged behavior | `tests/test_tq_stage_backend.py` | GREEN |
| `durable-task-queue-S2` | `specs/durable-task-queue/spec.md` | Backend selection and loud configuration validation | sqlite backend creates per-stage WAL store | `tests/test_tq_stage_backend.py` | GREEN |
| `durable-task-queue-S3` | `specs/durable-task-queue/spec.md` | Backend selection and loud configuration validation | invalid backend name fails validation loudly | `tests/test_tq_stage_backend.py` | GREEN |
| `durable-task-queue-S4` | `specs/durable-task-queue/spec.md` | Backend selection and loud configuration validation | non-positive numeric queue fields fail validation loudly | `tests/test_tq_stage_backend.py` | GREEN |
| `durable-task-queue-S5` | `specs/durable-task-queue/spec.md` | Loss-free durable enqueue | burst far beyond configured queue size is fully retained | `tests/test_tq_stage_backend.py` | GREEN |
| `durable-task-queue-S6` | `specs/durable-task-queue/spec.md` | Loss-free durable enqueue | runtime write error is loud and counted | `tests/test_tq_stage_backend.py` | GREEN |
| `durable-task-queue-S7` | `specs/durable-task-queue/spec.md` | FIFO order with claim/ack lifecycle | dequeue order equals insertion order | `tests/test_tq_sqlite_queue.py` | GREEN |
| `durable-task-queue-S8` | `specs/durable-task-queue/spec.md` | FIFO order with claim/ack lifecycle | claim is exclusive until acknowledged | `tests/test_tq_sqlite_queue.py` | GREEN |
| `durable-task-queue-S9` | `specs/durable-task-queue/spec.md` | FIFO order with claim/ack lifecycle | empty-store dequeue honors timeout and raises empty | `tests/test_tq_sqlite_queue.py` | GREEN |
| `durable-task-queue-S10` | `specs/durable-task-queue/spec.md` | FIFO order with claim/ack lifecycle | expired claims return to pending on sweep | `tests/test_tq_sqlite_queue.py` | GREEN |
| `durable-task-queue-S11` | `specs/durable-task-queue/spec.md` | Crash recovery is at-least-once with an age gate | unacknowledged claims survive reopen as pending | `tests/test_tq_sqlite_queue.py` | GREEN |
| `durable-task-queue-S12` | `specs/durable-task-queue/spec.md` | Crash recovery is at-least-once with an age gate | all four task types round-trip through the durable store | `tests/test_tq_sqlite_queue.py` | GREEN |
| `durable-task-queue-S13` | `specs/durable-task-queue/spec.md` | Crash recovery is at-least-once with an age gate | aged-out rows are purged loudly at startup | `tests/test_tq_sqlite_queue.py` | GREEN |
| `durable-task-queue-S14` | `specs/durable-task-queue/spec.md` | Non-destructive pending snapshot | snapshot reads leave the queue intact | `tests/test_tq_sqlite_queue.py` | GREEN |
| `durable-task-queue-S15` | `specs/durable-task-queue/spec.md` | Non-destructive pending snapshot | concurrent churn loses nothing | `tests/test_tq_sqlite_queue.py` | GREEN |
| `durable-task-queue-S16` | `specs/durable-task-queue/spec.md` | One-shot idempotent legacy import with opportunistic parsing | current-format snapshot imports once and is neutralized | `tests/test_tq_queue_manager.py` | GREEN |
| `durable-task-queue-S17` | `specs/durable-task-queue/spec.md` | One-shot idempotent legacy import with opportunistic parsing | legacy-format and corrupt files degrade safely | `tests/test_tq_queue_manager.py` | GREEN |
| `durable-task-queue-S18` | `specs/durable-task-queue/spec.md` | One-shot idempotent legacy import with opportunistic parsing | aged-out snapshot is parked, not imported | `tests/test_tq_queue_manager.py` | GREEN |
| `durable-task-queue-S19` | `specs/durable-task-queue/spec.md` | Shutdown durability on both backends | graceful stop persists final state | `tests/test_tq_queue_manager.py` | GREEN |
| `durable-task-queue-S20` | `specs/durable-task-queue/spec.md` | Fail-open construction and cross-backend semantic parity | unopenable store degrades to functional memory queue | `tests/test_tq_stage_backend.py` | GREEN |
| `durable-task-queue-S21` | `specs/durable-task-queue/spec.md` | Fail-open construction and cross-backend semantic parity | dedup gate contract is identical on both backends | `tests/test_tq_stage_backend.py` | GREEN |
| `durable-task-queue-S22` | `specs/durable-task-queue/spec.md` | Fail-open construction and cross-backend semantic parity | worker retry flow is identical on both backends | `tests/test_tq_stage_backend.py` | GREEN |

## Automated

### File: tests/test_tq_sqlite_queue.py

Describe: `SqliteTaskQueue engine contract (storage/task_queue.py — module absent at plan time; ModuleNotFoundError is the expected RED state)`

Engine-level tests drive the queue with plain dict payloads (`serializer=json.dumps`, `deserializer=json.loads`) — no task models needed except S12, which wires the stage-equivalent serializer pair (`ProviderTask.to_dict` / `TaskFactory.from_dict`) and therefore also pins the captured snapshot payload format end-to-end.

- [x] `durable-task-queue-S7` — it("dequeue order equals insertion order") <!-- WHEN 9 distinguishable payloads enqueued THEN get() yields them in exact insertion order -->
- [x] `durable-task-queue-S8` — it("claim is exclusive until acknowledged") <!-- WHEN one row claimed and unacked THEN qsize excludes it, second get(timeout=0.05) raises queue.Empty, task_done empties the store -->
- [x] `durable-task-queue-S9` — it("empty-store dequeue honors timeout and raises empty") <!-- WHEN get(timeout=0.2) on empty store THEN queue.Empty raised, elapsed >= 0.15s -->
- [x] `durable-task-queue-S10` — it("expired claims return to pending on sweep") <!-- WHEN claim with visibility_timeout_s=0.05 left unacked past expiry THEN sweep_expired_claims()==1, row re-gettable exactly once -->
- [x] `durable-task-queue-S11` — it("unacknowledged claims survive reopen as pending") <!-- WHEN instance abandoned mid-claim (simulated SIGKILL) and same file reopened THEN counts pending==1, payload intact -->
- [x] `durable-task-queue-S12` — it("all four task types round-trip through the durable store") <!-- WHEN SearchTask(refine_depth=2)/AcquisitionTask/CheckTask(Service nested)/InspectTask enqueued via to_dict-serializer, closed, reopened THEN dequeued to_dict() equals original to_dict() for all four -->
- [x] `durable-task-queue-S13` — it("aged-out rows are purged loudly at startup") <!-- WHEN row with created_at=now-25h plus a fresh row, reopened with max_age_hours=24 THEN only fresh row remains, log states aged-out count -->
- [x] `durable-task-queue-S14` — it("snapshot reads leave the queue intact") <!-- WHEN snapshot_pending() over 3 rows THEN returns all 3 in FIFO order, qsize still 3, all still dequeued exactly once afterwards -->
- [x] `durable-task-queue-S15` — it("concurrent churn loses nothing") <!-- WHEN 2 producer threads x 250 unique payloads + 2 consumer threads ack-ing every claim + 20 interleaved snapshots THEN consumed set == produced set (500, no dups), final qsize==0 -->

### File: tests/test_tq_stage_backend.py

Describe: `Stage/config integration (BasePipelineStage backend selection, config surface, parity matrix — harness mirrors tests/test_fh_stage_modes.py: FakeAuth/_task_config/_resources/real SearchStage/monkeypatched search_client)`

- [x] `durable-task-queue-S1` — it("absent config selects memory backend with unchanged behavior") <!-- WHEN Config() defaults + stage built without new kwargs THEN config.queue.backend=="memory", visibility_timeout_s==300, max_age_hours==24, stage.queue is queue.Queue, put/get FIFO works -->
- [x] `durable-task-queue-S2` — it("sqlite backend creates per-stage WAL store") <!-- WHEN SearchStage(queue_backend="sqlite", queue_dir=tmp) THEN <tmp>/search_queue.sqlite exists and stage.queue.journal_mode=="wal" -->
- [x] `durable-task-queue-S3` — it("invalid backend name fails validation loudly") <!-- WHEN TaskQueueConfig(backend="redis") THEN ValueError; AND validator on Config with mutated cfg.queue.backend=="redis" THEN ValueError naming backend -->
- [x] `durable-task-queue-S4` — it("non-positive numeric queue fields fail validation loudly") <!-- WHEN TaskQueueConfig(visibility_timeout_s=0) or (max_age_hours=-1) THEN ValueError; validator agrees -->
- [x] `durable-task-queue-S5` — it("burst far beyond configured queue size is fully retained") <!-- WHEN stage built with queue_size=10, backend sqlite, 5000 unique-query search tasks put_task'ed THEN all return True, qsize()==5000, no Full -->
- [x] `durable-task-queue-S6` — it("runtime write error is loud and counted") <!-- WHEN SqliteTaskQueue.put monkeypatched to raise sqlite3.OperationalError THEN put_task False, WARNING logged, tasks_dropped_backend_errors==1, other counters unchanged -->
- [x] `durable-task-queue-S20` — it("unopenable store degrades to functional memory queue") <!-- WHEN SqliteTaskQueue.__init__ monkeypatched to raise OSError, stage built with backend sqlite THEN ERROR log names stage, stage.queue_degraded is True, queue is queue.Queue, put/get still work -->
- [x] `durable-task-queue-S21` — it("dedup gate contract is identical on both backends") <!-- parametrized backend in (memory, sqlite): WHEN pinned fh-S5 sequence (first/dup-attempts-0/requeue-attempts-1/requeue-over-bound) THEN True/False/True/False + tasks_dropped_max_retries==1 on BOTH -->
- [x] `durable-task-queue-S22` — it("worker retry flow is identical on both backends") <!-- parametrized backend: WHEN always-transient-failing task through started strict stage (mirror of fh test_s4_s6) THEN calls==3, attempts==3, total_errors==3, tasks_requeued==2, tasks_dropped_max_retries==1, "discarded"/"max retries" wording, AND under sqlite the store ends empty (every claim acked, refused requeue never inserted) -->

### File: tests/test_tq_queue_manager.py

Describe: `QueueManager facade: legacy importer, startup maintenance, shutdown durability (manager/queue.py + Pipeline._on_stop contract — SqliteTaskQueue import absent at plan time => RED)`

Legacy snapshots for S16 are produced by the PRODUCTION memory-backend `QueueManager.save_queue_state` (dogfoods the captured wire format instead of synthesizing it); S17/S18 handcraft the legacy-fallback envelope and corrupt/aged files per the format captured in design.md Context.

- [x] `durable-task-queue-S16` — it("current-format snapshot imports once and is neutralized") <!-- WHEN production-written search_queue.json (2 tasks) + sqlite-backed QueueManager with attached stage THEN load_all_queues imports both rows (qsize==2), json renamed *.imported-*, second load leaves count unchanged, dequeued tasks equal originals -->
- [x] `durable-task-queue-S17` — it("legacy-format and corrupt files degrade safely") <!-- WHEN legacy-envelope json (numeric saved_at, bare tasks) for search + corrupt "{oops" file for gather THEN search rows imported, gather skipped with warning, file left in place, no exception -->
- [x] `durable-task-queue-S18` — it("aged-out snapshot is parked, not imported") <!-- WHEN snapshot saved_at 30h old THEN zero rows imported, file renamed *.expired-*, WARNING states age decision -->
- [x] `durable-task-queue-S19` — it("graceful stop persists final state") <!-- WHEN Pipeline._on_stop invoked (unbound on stub namespace, spy-wrapped real QueueManager) with 2 queued-unprocessed tasks per backend THEN memory: save_all_queues ran BEFORE queue_manager.stop and the json contains both tasks; sqlite: rows still pending after stop (checkpoint, no drain) -->

## Manual

<!-- All 22 scenarios are automatable offline; nothing here is manual. -->

None. The real-process durability proofs that cannot be expressed as unit tests (SIGKILL of a live harvest mid-run → restart resumes with zero loss; RSS-flat incident-seed run on the sqlite backend; importer against a real production `queue_state/` directory) are scheduled as the live-verification gate in `tasks.md` Group 7, executed with real credentials per operator authorization, following the R1 discipline (temp workspace, credentials stripped from temp configs, project `logs/` untouched, evidence recorded in verification.md).

## Staleness audit of the existing suite (operator directive)

Audited all 33 test files / 199 tests for obsolescence under this change (full symbol-level sweep: `put_task`, `get_pending_tasks`, `save/load_queue_state`, `QueueManager`, `GracefulShutdown`, direct `stage.queue.*`, dedup set, `AtomicFileWriter`, `max_age_hours`):

- **Zero existing tests cover the queue-persistence subsystem** — nothing is rendered stale by the sqlite backend; no test deletions or corrections are required.
- **Pins that MUST stay green unmodified** (they constrain the compatibility surface): `tests/test_fh_stage_modes.py` (dedup gate :179-192, worker retry counters/wording :151-173, `stage.queue.empty()` in `_wait_drain` :98, stats exposure :272-280) and `tests/test_sa_baskets_e2e.py:143-168` (start→put_task→stop bounded drain). Both exercise the default memory backend; S21/S22 re-pin the same contracts parametrized across both backends rather than editing the originals.
- `tests/test_rfg_stage_integration.py:193-203` (task-dict round-trip incl. legacy tolerance) is complementary — S12 extends the same guarantee through the durable store.
- Golden suites (`test_sa_fingerprint`, `test_qr_clean_regex`, `test_early_stop_modes`, `fh_` family) touch no queue internals except `test_fh_stage_modes.py` above.
