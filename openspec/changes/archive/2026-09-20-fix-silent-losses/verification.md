# Verification Notes: fix-silent-losses

Recorded 2026-09-20. Environment: local sandbox, Python 3.14. Live-network
verification was executed in-session using GitHub credentials supplied in the
git-ignored `.secrets` file (read at runtime, passed only in-process, never
written to any file, fixture, log or commit).

## Automated evidence (executed)

- RED baseline reproduced exactly as recorded in `tests.md`:
  - collection `ImportError` on `core.exceptions.TransientFetchError`
    (`test_fh_client_taxonomy.py`, `test_fh_stage_modes.py`,
    `test_fh_gather_fidelity.py`);
  - `test_cql_lint.py`: S1/S3/S4 FAILED, S2 GREEN;
  - `test_fh_protections.py`: 2 GREEN.
- Post-implementation: the five new files pass
  (21 passed), and the complete suite is **132 passed, 0 failed**
  (`python3 -m pytest tests/`).
- `openspec validate --strict fix-silent-losses` → valid.
- `logs/` was snapshotted to a temp dir before any tooling run; no credentials
  were read from or written to files.

## Offline fault-injection E2E (tasks.md 7.1) — executed

Harness: `/tmp/opencode/fh_e2e.py` (not committed). Drives the real stage
classes and a real SQLite `Registry` in a per-mode temp workspace with its own
logging dir, against an injected mock GitHub (no network, no credentials).
Two providers (`prov-a`, `prov-b`) share one query `'"sk-"'`.

Client-boundary taxonomy fault matrix (real `search.client` functions):

| Injected fault | Observed |
|---|---|
| API 500 after retries | `TransientFetchError` |
| API timeout | `TransientFetchError` |
| Limiter suppression (blank payload) | `TransientFetchError` |
| API legitimate zero (HTTP 200, `items: []`) | `ok` (`[]`, total=0) — no error |
| Blob gather 404 | `TransientFetchError` |
| Blob gather OK, zero keys extracted | `ok` (0 services) — no error |

Stage behaviour per mode (search worker loop, `max_retries=2`, injected
`TransientFetchError`; gather with injected blob 503):

| Mode | search total_errors | failure_empties_detected | tasks_requeued | tasks_dropped_max_retries | gather visit_status | coverage rows | gather raise |
|---|---|---|---|---|---|---|---|
| strict | 6 | 6 | 4 | 2 | `failed` | 0 | `TransientFetchError` |
| shadow | 0 | 2 | 0 | 0 | `gathered_ok` | 1 | none |
| legacy | 0 | 0 | 0 | 0 | `gathered_ok` | 1 | none |

Observed log lines (strict): `[...] requeued successfully/failed after 0.0s delay`
and `[...] task=[...] discarded, max retries=[2] reached`; warning
`[...] failure-empty detected, provider: prov-a, ...`. `get_stats()` exposed the
three counters (`stats_exposes = (6, 4, 2)` in strict). Shadow and legacy search
outcomes are identical (0 errors / 2 processed / no requeue / no drop), shadow
additionally detecting 2 failure-empties; gather outcomes are identical between
shadow and legacy; strict produced **zero false `gathered_ok`** and no
silent-success accounting.

## Live GitHub probes & shadow run (tasks.md 1.3, 7.3) — executed

Credentials were read from the git-ignored `.secrets` at runtime and passed only
in-process. No secret appears in any committed file, fixture, or log (verified by
scanning `tests/` for both the token and session strings).

### 1.3 Live probe — captured fixtures

- `tests/fixtures/live_2026_09_code_search_content_qualifier.json` —
  `GET https://api.github.com/search/code?q="sk-" AND content:"llm"`, Bearer auth
  → **HTTP 200 `{"total_count":0,"incomplete_results":false,"items":[]}`**.
- `tests/fixtures/live_2026_09_code_search_control.json` — same endpoint,
  `q="sk-"` → **`total_count=46006272`** (control proving the qualifier is the
  cause of the zero).
- `tests/fixtures/live_2026_09_blob_404.json` — absent blob path
  `github.com/octocat/Hello-World/blob/master/THIS-FILE-DOES-NOT-EXIST-xyz123.py`
  → **HTTP 404** (HTML body excerpt captured).

All three carry a `provenance` header (endpoint, capture timestamp, auth mode,
notes), matching the existing live-fixture convention.

### 7.3 Real-GitHub shadow run (real client + `SearchStage`, `api.github.com`)

| Check | Result |
|---|---|
| Config lint on a real API provider with `content:"llm"` | warning fired, config still loads |
| Link-rich API query (`"sk-"`) under shadow vs legacy | 100 links / 127 child tasks — **identical** |
| Dead `content:` API query under shadow | legitimate zero, `failure_empties_detected == 0` (not a failure) |
| Real HTTP 401 (invalid token) under shadow | `failure_empties_detected == 1`, stage returns `None` (legacy outcome) |
| Same 401 under strict | `TransientFetchError` escapes (requeue path) |
| Token restored, query re-run | 100 links again — recovery clean |

Note (pre-existing, **not** this change): `RefineEngine.clean_regex('"sk-" filename:.env')`
produces `'""sk-" filename:" AND "env"'`, so that particular query yields zero.
Confirmed independent of the failure-handling work.

## Restart recovery (tasks.md 7.2 / failure-handling-S17) — executed

Round-trip through the real `QueueManager`: wrote 3 tasks (Search/Acquisition/
Check, including `attempts` of 2 and 1) to a temp workspace, then recovered them
in a **fresh process**. Result: 3 tasks recovered, `attempts` preserved
(`[1,1,2]`), top-level schema keys unchanged
(`type, task_id, provider, created_at, attempts, data`), on-disk queue shape
unchanged, and `git diff HEAD` shows **no** changes to `core/models.py`,
`manager/queue.py`, or `stage/factory.py`. No format drift.

## Remaining (operator decision)

- **Production shadow-cycle promotion gate** (tests.md Manual, Migration Plan
  step 2) — requires a representative production cycle (including a rate-limit
  storm) and wall-clock time; the `shadow → strict` promotion remains an operator
  decision.

## Promotion guidance

Remaining `shadow -> strict` promotion stays a config flip and an operator
decision per the Migration Plan; rollback is a flip back to `legacy`.
