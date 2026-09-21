# Verification: fix-queue-persistence-under-load

Status: **COMPLETE — implementation, automated gates and the live verification gate (8.1–8.4) all executed and green.**

This file records verifiable evidence. Nothing here is fabricated: every number
below was measured in this environment. Sections 1–3 are the offline gates,
section 4 is the live gate run against real provider/GitHub credentials on
2026-09-22 01:41–02:07 UTC+3.

## 1. Pre-flight probe (task 1.3, design risk gate D2/D3)

Probe: single connection, `PRAGMA journal_mode=WAL` + `PRAGMA synchronous=NORMAL`
+ `timeout=30.0`, one small INSERT transaction per commit; measured on the
workspace target disk (`/dev/vda2`, ext4, ~94% full). `/tmp` is tmpfs and is
**not** representative.

| Measurement | Runs | Result |
|---|---|---|
| 10 000 sequential puts (design's own criterion, "10 k puts < 2 s") | 5 | **0.715 / 0.720 / 0.825 / 0.712 / 0.796 s → 12 114–14 037 commits/s** |
| 20 000 sequential puts (sustained, checkpoint noise under load) | 8 | 3 151–14 186 commits/s (median ≈ 10.7 k; cold/contended runs lower) |
| 1 000 sequential puts (realistic worst burst) | 3 | 0.070–0.206 s |
| `synchronous=OFF` (for isolation only; not used) | 1 | 57 708 /s |
| batch=100 (for isolation only; not used) | 1 | 220 169 /s |

**Conclusion: PASS.** The design's concrete acceptance criterion (10 k puts < 2 s)
passes with ~2.4× margin on all five runs (≥ 10⁴ commits/s sustained over 10 k
puts). The 20 k-point series is noisy under concurrent test load; its low runs
are sustained-checkpoint contention, not a per-burst blocker. D2/D3 stand: a
realistic worst burst (~10³ links) commits in ≤ 0.21 s against a ~6.7 s API call.

## 2. Automated gates

| Gate | Command | Result |
|---|---|---|
| Full suite (task 7.1) | `python3 -m pytest tests/ -q` | **223 passed** (199 pre-existing + 24 new) — exact expected count |
| New engine contract | `python3 -m pytest tests/test_tq_sqlite_queue.py -q` | 9 passed |
| Stage/config/parity | `python3 -m pytest tests/test_tq_stage_backend.py -q` | 11 passed |
| Facade/importer/shutdown | `python3 -m pytest tests/test_tq_queue_manager.py -q` | 4 passed |
| Pinned parity (tests.md) | `tests/test_fh_stage_modes.py` + `tests/test_sa_baskets_e2e.py` | 11 passed unmodified |
| Modified tracked tests (task 7.2) | `git diff --name-only -- tests/` | **empty** (only 3 untracked `tests/test_tq_*.py`) |
| Golden pins (task 7.2) | `-k fh_` / sa / `-k rfg` / early-stop | 17 / 6 / 27 / 9 passed |
| Spec validity (task 7.3) | `openspec validate fix-queue-persistence-under-load --strict` | valid |
| Import cycle smoke | `import main, manager.*, stage.*, storage.task_queue` | OK |

## 3. Scenario coverage (S1–S22)

All 22 spec scenarios are covered by the automated tests above and are GREEN
(see `tests.md` traceability): S1–S6, S20–S22 in `tests/test_tq_stage_backend.py`;
S7–S15 in `tests/test_tq_sqlite_queue.py`; S16–S19 in
`tests/test_tq_queue_manager.py`.

Behavioral proofs already established by the suite:

- **Non-destructive snapshot (S14):** `snapshot_pending()` returns all rows and
  leaves `qsize()` unchanged; the historical `lost task during persistence`
  channel is unreachable under sqlite.
- **Crash at-least-once (S11):** a claimed-but-unacknowledged row is reclaimed to
  pending when the file is reopened (engine-level SIGKILL simulation).
- **Concurrent churn (S15):** 2 producers × 250 + 2 consumers with interleaved
  snapshots → consumed set == produced set, zero loss/duplicates.
- **Legacy import (S16–S18):** production-written current envelope imports once
  and is renamed `.imported-*`; legacy envelope imports; corrupt file skipped
  loudly and left in place; aged snapshot parked `.expired-*`.
- **Shutdown durability (S19):** `Pipeline._on_stop` saves before
  `queue_manager.stop()`; memory writes a fresh JSON snapshot, sqlite checkpoints
  without draining (pending rows intact and re-claimable).

### Design note: in-process task identity (S22)

The durable store deserializes rows, so the object a worker mutates is not the
same Python object the caller passed to `put_task`. S22 asserts on the original
object (`task.attempts == 3`). To satisfy that pinned contract without retaining
the task graph in RAM, `SqliteTaskQueue` keeps a **weak** `seq -> object`
identity cache: when a producer still references the enqueued object, `get`
returns it; otherwise the entry is collected and the payload is deserialized.
This preserves the O(workers) RAM guarantee of design D2.

## 4. Live verification gate (Group 8.1–8.4) — EXECUTED, PASS

### 4.0 Harness and isolation

Eight live runs against real provider + GitHub credentials, 2026-09-22 01:41–02:07.

- Temp CWDs `/tmp/opencode/r2live/{runA,runB,runD}`; launched as
  `PYTHONPATH=<repo> python3 <repo>/main.py -c <cwd>/config.yaml --log-level INFO`
  with `cwd=<temp dir>`. Because `Logger._logs_dir = Path("logs")` is CWD-relative
  (`tools/logger.py:540`) and `global.workspace: "./data"` is relative too, **the
  repository's own `logs/` and `data/` were never touched** (both still 28 K / 8 K).
- Scratch configs were generated from the repo `config.yaml` overriding only
  `global.workspace`, `queue`, `persistence.queue_interval` (60 → 20 s, so a
  periodic save definitely precedes the final one) and an explicit
  `refine_governor: {mode: on, max_refine_depth: 2, max_partitions_per_refine: 128,
  max_search_tasks_per_run: 10000}`. All four production providers stayed enabled,
  `failure_handling: strict`, production thread counts (search 1 / gather 8 /
  check 4 / inspect 2), incident seed `/sk-[a-zA-Z0-9]{32}/`.
- Credentials were copied verbatim into the temp configs under `/tmp` only, never
  printed and never written into the repo (see §5).
- A sampler recorded process RSS and read-only (`file:...?mode=ro`) queue depths
  every 5 s, then signalled the process.

| Run | Backend | Workspace state | Sampled | Signal | Purpose |
|---|---|---|---|---|---|
| A1 | memory | fresh | 75 s | SIGTERM | 8.4-memory + produce the legacy fixture |
| A2 | memory | restart of A1 | 45 s | SIGTERM | restart-parity control for 8.2/8.4 |
| B1 | sqlite | fresh | 90 s | SIGTERM | **8.1** smoke + 8.4-sqlite |
| B2 | sqlite | restart of B1 | 45 s | SIGTERM | 8.4 "next start recovers" |
| B3 | sqlite | restart of B2 | 35 s | **SIGKILL** | **8.2** crash |
| B4 | sqlite | restart of B3 | 30 s | SIGTERM | **8.2** orphan reclaim + resume |
| D1 | sqlite | fresh + planted JSON | 25 s | SIGTERM | **8.3** one-shot import |
| D2 | sqlite | restart + re-planted JSON | 25 s | SIGTERM | **8.3** idempotency |

Every SIGTERM run exited `rc=0`; the SIGKILL run exited `rc=-9` instantly.
Graceful shutdown takes 75–125 s because the four result managers flush
sequentially (~30 s each) — pre-existing behavior, unrelated to this change.

### 4.1 Task 8.1 — offline smoke on `queue.backend: sqlite` → PASS

| Criterion | Measured |
|---|---|
| Per-stage stores created | `check/gather/inspect/search_queue.sqlite` all present |
| WAL mode | `journal_mode=wal` and `user_version=1` on **all four** |
| Row size | `search_queue.sqlite` 2 727 936 B / 5 083 rows = **537 B/row** (design predicted 0.3–0.6 KB) |
| Search depth governed | reached **5 083** and held flat (t = 45/60/75 s all 5 083) — matches R1's observed ~4–5 k |
| RSS flat | window t = 30…85 s, n = 12: min 145.7 / mean 148.1 / max 154.0 MB, **spread 8.3 MB** while depth grew 4 575 → 5 083 |
| `lost task during persistence` | **ZERO** occurrences in any log |
| `queue_sizes`-ignored WARNING | emitted once at `manager/pipeline.py:324`, listing `{search: 4000000, gather: 1500000, check: 2000000, inspect: 3000000}` |
| Fail-open / degradation | none: zero `falling back to memory`, zero `durable queue write failed`, zero `Final queue save failed` |
| Queue-related ERRORs | **zero** (no line matching `sqlite|task_queue|durable|database|locked`) |

ERROR baseline is network noise, not a regression: run B1 logged 195 ERROR lines
vs 159 in memory run A1, and both consist of the same two classes — 97 ×
`All 3 attempts failed. Last error: Rate limit exceeded (HTTP 429)` and ~98 ×
`[gather] error processing task: gather fetch failed for url: https://github.com/...`.

### 4.2 Task 8.2 — SIGKILL proof (at-least-once) → PASS

State probed immediately after `kill -9` (process died instantly, `rc=-9`):

| Stage | pending | claimed (orphans) | worker threads |
|---|---|---|---|
| search | 14 615 | **1** | 1 |
| gather | 4 322 | **8** | 8 |
| check | 3 | **4** | 4 |
| inspect | 0 | 0 | 2 |

The orphan count per stage equals exactly the number of in-flight worker threads —
every interrupted claim was left dangling, and committed rows survived in the WAL
files (up to 4 136 512 B uncheckpointed).

Restart (run B4) logged, at construction time and before any worker attached:

```
01:59:20,616 | INFO | [task_queue.py:137] | [search] reclaimed 1 orphaned claimed task(s) at startup
01:59:20,621 | INFO | [task_queue.py:137] | [check]  reclaimed 4 orphaned claimed task(s) at startup
01:59:20,622 | INFO | [task_queue.py:137] | [gather] reclaimed 8 orphaned claimed task(s) at startup
```

1 + 4 + 8 = **13 = exactly the orphans measured after the kill** — full accounting,
zero loss. No aged-out purge fired (correct: rows were minutes old, gate is 24 h).
Work resumed ~2 s later (`[search] search completed for qwen-china-dash: 100 links`),
and the run ended cleanly with `claimed=0` and `wal_bytes=0` on every stage.

### 4.3 Task 8.3 — legacy migration proof → PASS

Fixture: a genuine production snapshot written by the memory backend's own
`save_queue_state` in run A2 — `search_queue.json`, 4 911 210 bytes,
`task_count=10166`, `saved_at=2026-09-22T01:56:15.247622`, envelope
`{provider, saved_at, stage, status, task_count, tasks}`.

- **First start (D1):** `02:02:54,719 | INFO | [queue.py:501] | Imported 10166 task(s) from legacy snapshot for search as search_queue.json.imported-20260921T230254719283`.
  The imported count equals the fixture's `task_count` exactly; the original file
  is gone and the renamed file is byte-identical in size (4 911 210 B). The marker
  timestamp is UTC, as designed.
- **Second start (D2)** with the identical fixture re-planted as
  `search_queue.json`: **zero** `Imported ... from legacy snapshot` lines, the file
  was left in place untouched (mtime preserved), and search pending grew by only
  **+1 820** (new fan-out work) instead of the +10 166 a double import would add.
  The zero-rows guard tripped correctly.

### 4.4 Task 8.4 — clean SIGTERM proof, both backends → PASS

**memory** (two independent runs; `queue_interval=20 s`, so periodic saves fall on
a :07/:27/:47 grid and the observed off-grid timestamps can only be the final save):

| Run | SIGTERM received | `search_queue.json` `saved_at` | `Stopped QueueManager` | Save→stop gap |
|---|---|---|---|---|
| A1 | 01:43:09.525 | **01:43:21.337116** | 01:43:21.350 | 13 ms before stop |
| A2 | 01:56:01.216 | **01:56:15.247622** | 01:56:15.277 | 30 ms before stop |

Both snapshots are strictly newer than the last periodic save and were written
*after* SIGTERM, capturing 5 083 (A1) and 10 166 (A2) tasks. Pre-fix this file
would have been up to 60 s stale.

**sqlite:** after SIGTERM all rows were intact (`search pending=5083, claimed=0`)
with `wal_bytes=0`, i.e. the stop-time PASSIVE checkpoint ran and nothing was
drained; the next start (B2) recovered them and resumed work.

No `Final queue save failed` line appeared in any of the eight runs.

### 4.5 Observations from the live gate (non-blocking)

1. **Restart growth is backend-symmetric, not duplication.** Restarting grows the
   search backlog identically on both backends: memory 5 083 → **10 166**, sqlite
   5 083 → **10 167**. Cause: `max_search_tasks_per_run` is a *per-process* budget
   that resets on boot, so each restart admits a fresh fan-out on top of the
   recovered backlog. The sqlite store held 10 167 rows with **10 167 distinct
   `payload_json` and 10 167 distinct `dedup_id` — zero duplicates.** This is
   pre-existing behavior, unchanged by this design.
2. **Recovery logging differs by design.** memory logs
   `Loaded 5741 total tasks from previous session` (`manager/queue.py:381`);
   sqlite logs nothing because recovery is intentionally a no-op (D12/D14 — the
   rows are already in the store the workers claim from). Verified working, but
   operators comparing backends will see the asymmetry.
3. **Residual dedup asymmetry across restarts (worth tracking for R3).** The
   in-memory dedup set starts empty on every boot. Under memory, recovery replays
   tasks through `put_task`, which *seeds* dedup; under sqlite it does not, so a
   restart that regenerates an identical `(provider, query, page, regex)` tuple
   would be rejected on memory but admitted as a second row on sqlite. In these
   runs it never triggered (the regenerated fan-out explored a different keyspace
   slice, hence 0 duplicate `dedup_id`s). Downstream stays idempotent (registry
   upserts, shard dedup), so this is consistent with the at-least-once doctrine —
   recorded here rather than fixed, since seeding dedup from the durable store is
   a behavioral change beyond this change's scope.
4. **Graceful stop can leave one in-flight claim unacked** (run B2 ended with
   `search claimed=1`). It is reclaimed on the next startup (proved in 4.2), so it
   falls inside the documented "no queue content lost beyond in-flight task
   semantics" bound.
5. **The zero-rows guard skips silently** (`manager/queue.py:455-457`). An operator
   who leaves a stale `{stage}_queue.json` beside a non-empty store gets no log
   explaining why it was not imported. A one-time INFO would improve operability;
   deliberately not added now to keep the change surface frozen after the gate passed.

## 5. Secrets

Verified after the live gate, not just asserted:

- A recursive scan of every changed/new source file and every change artifact for
  the credential shapes used by this project (`ghp_[A-Za-z0-9]{36}` and both
  session-cookie values from `.secrets`) returned **zero matches**. Log excerpts
  quoted in §4 contain only regex *patterns* (e.g. `/sk-o[uvwxyz][a-zA-Z0-9]{30}/`)
  and public GitHub repository URLs — no key material.
- `config.yaml` and `.secrets` are git-ignored (`.gitignore:140` and `:112`), so the
  real credentials never entered version control. The scratch configs that *do*
  contain them live only under `/tmp/opencode/r2live/` and are outside the repo.
- The repository's own `logs/` (28 K) and `data/` (8 K) are byte-for-byte
  untouched by the eight live runs — all logging and workspace I/O went to the
  temp CWDs, because both paths are CWD-relative.
- Repository `config.yaml` was **not** flipped (task 9.3): it still has no
  `queue:` section and the code default remains `backend: memory`. Promotion is an
  operator decision; the evidence in §4 is what authorizes it.
