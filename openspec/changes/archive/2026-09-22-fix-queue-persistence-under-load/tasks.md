# Tasks: fix-queue-persistence-under-load

Scenario IDs refer to `specs/durable-task-queue/spec.md` (S1–S22); test-file mapping in `tests.md`; implementation decisions D1–D14 in `design.md`.

## 1. RED baseline & pre-flight probes

- [x] 1.1 Re-run `python3 -m pytest tests/test_tq_sqlite_queue.py tests/test_tq_stage_backend.py tests/test_tq_queue_manager.py -q` and confirm all three fail at collection with `ModuleNotFoundError: No module named 'storage.task_queue'` (plan-time run recorded in tests.md; zero unexpected passes required)
- [x] 1.2 Confirm parity pins are green UNMODIFIED in the same window: `python3 -m pytest tests/test_fh_stage_modes.py tests/test_sa_baskets_e2e.py -q` → 11 passed
- [x] 1.3 Burst-commit latency probe (design risk gate): single-connection SQLite with registry pragmas (WAL, synchronous=NORMAL, timeout=30.0) on the target disk must sustain ≥ 10⁴ commits/s for small INSERT transactions; record measured rate — if below the gate, stop and revisit D2/D3 before writing the engine

## 2. Storage engine `storage/task_queue.py` (durable-task-queue S7–S15)

- [x] 2.1 Module skeleton: `SqliteTaskQueue(path, *, serializer, deserializer, name="", visibility_timeout_s=300.0, max_age_hours=24.0)`; DDL per D1 (`tasks(seq INTEGER PRIMARY KEY AUTOINCREMENT, dedup_id TEXT NOT NULL, payload_json TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL CHECK(state IN ('pending','claimed')), claimed_until REAL, created_at REAL NOT NULL)` + `idx(state,seq)` + partial index on `claimed_until`); registry pragmas verbatim; single connection + internal `threading.Lock` (one-writer, D3); `PRAGMA user_version` bootstrap mirroring `bootstrap_schema` style
- [x] 2.2 Enqueue path: `put(obj, timeout=None, *, dedup_id="", created_at=None)` and `put_nowait` — never raise `queue.Full`, commit-per-put so True means durable (D6); `qsize()` = COUNT pending, `empty()`, `full()` → False (S7 order foundation)
- [x] 2.3 Claim path: `get(timeout=None)` / `get_nowait()` — single `UPDATE … WHERE seq = (SELECT … WHERE state='pending' OR (state='claimed' AND claimed_until < now) ORDER BY seq LIMIT 1) … RETURNING` claim; FIFO by seq (S7); exclusive until ack (S8); blocking wait via `threading.Condition` signaled by put/ack/sweep — no polling (D2); raises `queue.Empty` on timeout (S9); thread-local claim stack (D5)
- [x] 2.4 Ack path: `task_done()` pops the thread-local claim and DELETEs the row; unpaired call raises `ValueError` like stdlib (D5); expired claims become re-claimable (S9/S10 boundary)
- [x] 2.5 Maintenance & introspection: `sweep_expired_claims()` → int reclaimed (S10), `snapshot_pending()` non-destructive FIFO list (S14), `counts()` → {"pending": n, "claimed": m}, `checkpoint()` (PASSIVE), `bulk_insert(objs)` single transaction (importer feed), `close()`, `journal_mode` property (S2 assertion hook)
- [x] 2.6 Construction-time recovery: reclaim orphaned `claimed` rows → `pending` (S11, at-least-once after crash); purge rows older than `max_age_hours` with a loud WARNING naming count and stage (S13) — both run inside the constructor, before any worker can attach (D14)
- [x] 2.7 Drive `tests/test_tq_sqlite_queue.py` fully green (9 tests, S7–S15 incl. concurrency churn S15) without modifying any pinned assertion

## 3. Config surface (durable-task-queue S1, S3, S4)

- [x] 3.1 `config/schemas.py`: `TaskQueueConfig` dataclass (`backend: str = "memory"`, `visibility_timeout_s: float = 300.0`, `max_age_hours: float = 24.0`) with `__post_init__` normalization + loud rejection of unknown backend and non-positive numerics (D8, mirrors `RefineGovernorConfig` house style); add `queue` field to `Config` + `to_dict()` entry
- [x] 3.2 `config/loader.py`: import + `if "queue" in data:` parse hook + `_parse_task_queue_config` (house pattern)
- [x] 3.3 `config/validator.py`: `_validate_task_queue_config` called from `validate()` — errors aggregate into the existing `ValueError("Configuration validation failed")` funnel (S3/S4 at config layer)
- [x] 3.4 Verify S1 semantics: absent `queue:` section → memory backend, byte-identical behavior; drive `test_s3_invalid_backend_raises` / `test_s4_non_positive_numerics_raise` green

## 4. Stage wiring (durable-task-queue S1, S2, S5, S6, S20, S21, S22)

- [x] 4.1 `stage/base.py` ctor: new kwargs `queue_backend="memory"`, `queue_dir=None`, `queue_visibility_timeout_s`, `queue_max_age_hours`; memory branch untouched (`queue.Queue(maxsize=queue_size)`); sqlite branch lazily imports serializers (`json.dumps(task.to_dict())` / `TaskFactory.from_dict`) and constructs `SqliteTaskQueue` at `{queue_dir}/{stage_name}_queue.sqlite` (S2, D3/D11)
- [x] 4.2 Fail-open construction (D7, S20): catch `OSError`/`sqlite3.Error` from engine construction → ERROR log naming the stage, set `self.queue_degraded = True`, fall back to a functional memory `queue.Queue`; stage must remain operable
- [x] 4.3 `put_task` durable branch: pass `dedup_id=self._generate_id(task)`; wrap `queue.put` in broad `except Exception` → WARNING + increment `tasks_dropped_backend_errors` + return False (never silent, S6); memory branch keeps drop-on-Full exactly as-is (rollback honesty, D6); dedup gate itself unchanged and in-memory (D4, S21)
- [x] 4.4 `get_pending_tasks`: duck-branch on `getattr(self.queue, "snapshot_pending", None)` → non-destructive read; legacy drain-and-put-back path preserved verbatim for memory (S14/S15 at stage level)
- [x] 4.5 `core/metrics.py`: add `tasks_dropped_backend_errors: int = 0` to `StageMetrics` counters block; expose via `get_stats()` (S6 observability)
- [x] 4.6 `manager/pipeline.py::_create_stages`: pass queue kwargs from `config.queue` + `{workspace}/queue_state`; emit one-time WARNING when `queue_sizes` entries are set while backend is sqlite (they are ignored, D8)
- [x] 4.7 Drive `tests/test_tq_stage_backend.py` fully green (11 collected: S1–S6, S20, parametrized S21/S22 across both backends) without modifying pinned fh-mirror assertions

## 5. QueueManager facade & legacy importer (durable-task-queue S16–S18)

- [x] 5.1 `manager/queue.py::QueueManager`: new kwargs `backend="memory"`, `visibility_timeout_s`, `max_age_hours`; add `attach_stages(stages: dict)` called by Pipeline right after `_create_stages` (facade needs stage handles for sweep/checkpoint/state-info, D11/D12)
- [x] 5.2 One-shot legacy importer (D13): for each `{stage}_queue.json` in persistence dir — parse BOTH envelope shapes exactly as `load_queue_state` does (current ISO `saved_at` + legacy numeric); age-gate against `max_age_hours` → fresh files import via `bulk_insert` (single tx, `dedup_id=""`, per-task deserialize failures skipped with WARNING mirroring manager/queue.py:244-246) then rename `*.imported-<utc-ts>`; aged files park as `*.expired-<ts>` (never deleted); corrupt JSON → loud WARNING, file left in place; zero-rows guard makes repeat runs idempotent (S16, S17, S18)
- [x] 5.3 `load_all_queues` sqlite branch: run importer + construction already did reclaim/age-purge → return `{}` for durable stages so `manager/task.py` recovery flow is a no-op for them (D12/D14); memory branch unchanged; startup ORDER in `manager/task.py` untouched
- [x] 5.4 `save_all_queues` sqlite branch: `sweep_expired_claims()` + `checkpoint(PASSIVE)` per attached durable queue — NO drain, no O(N) serialization (D12); memory branch unchanged (still snapshot-based)
- [x] 5.5 `get_state_info` sqlite branch: populate `QueueStateMetrics` from `counts()` + db file size instead of task lists
- [x] 5.6 Drive `test_s16_*`, `test_s17_*`, `test_s18_*` green in `tests/test_tq_queue_manager.py` (S16 fixture is written by production `save_queue_state` — dogfooding, do not replace with hand-rolled JSON)

## 6. Shutdown durability (durable-task-queue S19)

- [x] 6.1 `manager/pipeline.py::_on_stop`: insert final `self.queue_manager.save_all_queues(self.stages)` BEFORE `queue_manager.stop()` (currently pipeline.py:385 stops without saving), wrapped in try/except-log so shutdown never hard-fails; applies to BOTH backends (memory gets its fresh JSON snapshot — closes the discovered ≤60 s clean-SIGTERM loss window); `GracefulShutdown` class left untouched (D10)
- [x] 6.2 Drive `test_s19_final_save_runs_before_queue_manager_stop` green (ordering spy: save event strictly precedes stop event; memory phase asserts fresh JSON with both queries; sqlite phase asserts pending==2 recoverable after `_on_stop`)

## 7. Regression & static gates

- [x] 7.1 Full suite: `python3 -m pytest tests/ -q` → all green, expected **223 passed** (199 pre-existing + 24 new: 9 engine + 11 stage-backend + 4 queue-manager); any delta must be explained, not silenced
- [x] 7.2 `git diff --name-only -- tests/` shows ZERO modified tracked test files (new `tests/test_tq_*.py` untracked additions only); golden pins re-run green: fh family (17 via `-k fh_`), sa suites, rfg suites, early-stop modes (22)
- [x] 7.3 `openspec validate fix-queue-persistence-under-load --strict` → valid

## 8. Live verification gate (real credentials, temp workspace — secrets NEVER committed)

- [x] 8.1 Offline smoke first: scratch config (temp CWD under /tmp, credentials stripped to env-from-.secrets pattern used by the R1 live runs) with `queue.backend: sqlite`, incident seed `/sk-[a-zA-Z0-9]{32}/`, refine governor on → per-stage `*_queue.sqlite` files appear in WAL mode, search queue depth stays governed (~4–5 K), RSS flat, zero `lost task during persistence` lines
- [x] 8.2 SIGKILL proof (at-least-once): `kill -9` mid-run → restart → orphaned claims reclaimed, work resumes from sqlite, no queue content lost beyond in-flight task semantics; capture logs
- [x] 8.3 Legacy migration proof: plant a production-shaped `search_queue.json` (written by real `save_queue_state`) into the temp workspace → start with sqlite backend → rows imported once, file renamed `.imported-*`, second start is a no-op
- [x] 8.4 Clean SIGTERM proof: graceful stop writes the final state on BOTH backends (memory: fresh JSON newer than last periodic save; sqlite: rows intact, next start recovers)
- [x] 8.5 Record everything in `verification.md`: probe numbers from 1.3, live-run queue depths/RSS/log excerpts for 8.1–8.4, any deviations with rationale; confirm no secret values in any committed artifact

## 9. Docs & rollout (ship dark)

- [x] 9.1 README: new `### Durable task queues (queue.backend)` section — flag semantics (default `memory` = byte-identical rollback), durability doctrine (at-least-once, dedup-ID idempotency), visibility timeout & age gate operations, one-shot importer behavior, promotion/rollback = config flip
- [x] 9.2 `examples/config-full.yaml`: commented `queue:` block (backend/visibility_timeout_s/max_age_hours) in house comment style, noting queue_sizes is ignored under sqlite
- [x] 9.3 Ship-dark confirmation: repo `config.yaml` NOT flipped in this change (promotion is an operator decision after the 8.x gate, mirroring the aggregation shadow→on track); default backend remains `memory` everywhere in code
