# Test Plan

Derived mechanically from the delta specs under `specs/`. One spec scenario = exactly one automated test, or one Manual entry with a reason. Scenario IDs (`<capability>-S<n>`) are stable: never renumber, append only.

Runner: `python3 -m pytest` (project convention: flat `tests/test_*.py`, harness style mirrors `tests/test_gather_skip_modes.py`). Observed statuses recorded from the run of 2026-09-20 (pre-implementation).

**GREEN run (post-implementation, 2026-09-20):** all 21 automated scenarios pass (`python3 -m pytest tests/test_fh_client_taxonomy.py tests/test_fh_stage_modes.py tests/test_fh_gather_fidelity.py tests/test_fh_protections.py tests/test_cql_lint.py` → 21 passed); the complete suite is 132 passed with zero regressions. RED baselines below are retained as the pre-implementation record.

**RED-by-import note:** `core.exceptions.TransientFetchError` does not exist yet; for the three files importing it, the collection ImportError IS the expected RED state (per artifact instructions). Scenarios marked "guard" encode behavior that exists today (legacy equivalence, dedup-gate bounds, coverage semantics, cooldown mechanics) — they are expected GREEN once the import resolves and MUST stay GREEN after the fix. Unexpected passes observed today (fh-S16, cql-S2, aux backoff) were investigated and are intentional regression guards, not covered-new-behavior accidents.

## Traceability

| Scenario ID | Spec | Requirement | Scenario | Test file | Status |
|-------------|------|-------------|----------|-----------|--------|
| `failure-handling-S1` | `specs/failure-handling/spec.md` | Empty-result taxonomy at the fetch boundary | Legitimate zero passes through cleanly | `tests/test_fh_client_taxonomy.py` | GREEN |
| `failure-handling-S2` | `specs/failure-handling/spec.md` | Empty-result taxonomy at the fetch boundary | Network failure after retries is a failure-empty | `tests/test_fh_client_taxonomy.py` | GREEN |
| `failure-handling-S3` | `specs/failure-handling/spec.md` | Empty-result taxonomy at the fetch boundary | Limiter suppression is a failure-empty | `tests/test_fh_client_taxonomy.py` | GREEN |
| `failure-handling-S4` | `specs/failure-handling/spec.md` | Bounded retry and honest accounting | Transient failure triggers bounded requeue | `tests/test_fh_stage_modes.py` | GREEN |
| `failure-handling-S5` | `specs/failure-handling/spec.md` | Bounded retry and honest accounting | Re-enqueue passes the deduplication gate | `tests/test_fh_stage_modes.py` | GREEN |
| `failure-handling-S6` | `specs/failure-handling/spec.md` | Bounded retry and honest accounting | Exhausted retries drop loudly | `tests/test_fh_stage_modes.py` | GREEN |
| `failure-handling-S7` | `specs/failure-handling/spec.md` | Gather-outcome fidelity | Fetch failure never produces gathered_ok | `tests/test_fh_gather_fidelity.py` | GREEN |
| `failure-handling-S8` | `specs/failure-handling/spec.md` | Gather-outcome fidelity | Zero-key success keeps coverage semantics | `tests/test_fh_gather_fidelity.py` | GREEN |
| `failure-handling-S9` | `specs/failure-handling/spec.md` | Check tasks survive limiter starvation | Starved check requeues | `tests/test_fh_stage_modes.py` | GREEN |
| `failure-handling-S10` | `specs/failure-handling/spec.md` | Tri-mode rollout flag | Legacy mode is indistinguishable | `tests/test_fh_stage_modes.py` | GREEN |
| `failure-handling-S11` | `specs/failure-handling/spec.md` | Tri-mode rollout flag | Shadow mode measures without enforcing | `tests/test_fh_stage_modes.py` | GREEN |
| `failure-handling-S12` | `specs/failure-handling/spec.md` | Tri-mode rollout flag | Strict mode enforces | `tests/test_fh_stage_modes.py` | GREEN |
| `failure-handling-S13` | `specs/failure-handling/spec.md` | Tri-mode rollout flag | Rollback is a config flip | `tests/test_fh_stage_modes.py` | GREEN |
| `failure-handling-S14` | `specs/failure-handling/spec.md` | Failure observability counters | Counters visible in status | `tests/test_fh_stage_modes.py` | GREEN |
| `failure-handling-S15` | `specs/failure-handling/spec.md` | Existing protections are preserved | Cooldown rotation unchanged under the new contract | `tests/test_fh_stage_modes.py` | GREEN |
| `failure-handling-S16` | `specs/failure-handling/spec.md` | Existing protections are preserved | Full-pool cooldown waits, never drops | `tests/test_fh_protections.py` | GREEN |
| `failure-handling-S17` | `specs/failure-handling/spec.md` | Existing protections are preserved | Restart recovery unaffected | — | MANUAL (executed 2026-09-20; see verification.md) |
| `config-query-lint-S1` | `specs/config-query-lint/spec.md` | Web-only qualifier warning under API transport | API provider with content qualifier is warned | `tests/test_cql_lint.py` | GREEN |
| `config-query-lint-S2` | `specs/config-query-lint/spec.md` | Web-only qualifier warning under API transport | Web provider with the same query is not warned | `tests/test_cql_lint.py` | GREEN |
| `config-query-lint-S3` | `specs/config-query-lint/spec.md` | Web-only qualifier warning under API transport | Every occurrence is reported separately | `tests/test_cql_lint.py` | GREEN |
| `config-query-lint-S4` | `specs/config-query-lint/spec.md` | Data-driven qualifier list | New qualifier extends linting without code change | `tests/test_cql_lint.py` | GREEN |

## Automated

### File: tests/test_fh_client_taxonomy.py

Describe: Client-boundary empty-result taxonomy

Module under test exists (`search/client.py`) but the typed failure `core.exceptions.TransientFetchError` does not — the import failure is the expected RED state.

- [x] `failure-handling-S1` — it("Legitimate zero passes through cleanly") <!-- WHEN HTTP 200 with empty items THEN ([], 0, content), no error -->
- [x] `failure-handling-S2` — it("Network failure after retries is a failure-empty") <!-- WHEN TimeoutError from the client / blank web content THEN TransientFetchError raised -->
- [x] `failure-handling-S3` — it("Limiter suppression is a failure-empty") <!-- WHEN suppressed request yields blank payload THEN TransientFetchError raised -->

### File: tests/test_fh_stage_modes.py

Describe: Stage-mode contract for failure handling (tri-mode flag, bounded retry, counters)

Uses the SearchStage/CheckStage harness pattern from `tests/test_gather_skip_modes.py`; flag set via `config.pipeline.failure_handling` (field formalized by the fix). Import failure is the expected RED state.

- [x] `failure-handling-S10` — it("Legacy mode is indistinguishable from pre-change behavior") <!-- WHEN injected transient failure under legacy THEN swallowed completion, no counters -->
- [x] `failure-handling-S11` — it("Shadow mode measures without enforcing") <!-- THEN failure_empties_detected==1 + contextual log, outcome as legacy -->
- [x] `failure-handling-S12` — it("Strict mode enforces") <!-- THEN TransientFetchError escapes process_task -->
- [x] `failure-handling-S4` — it("Transient failure triggers bounded requeue") <!-- worker-loop E2E: 3 executions, attempts==3, total_errors==3 -->
- [x] `failure-handling-S6` — it("Exhausted retries drop loudly") <!-- tasks_dropped_max_retries==1 + discard log -->
- [x] `failure-handling-S5` — it("Re-enqueue passes the deduplication gate") <!-- put_task admits 0<attempts<=max, rejects attempts==0 dup and over-bound -->
- [x] `failure-handling-S9` — it("Starved check requeues") <!-- WHEN limiter starves THEN typed failure escapes instead of None -->
- [x] `failure-handling-S13` — it("Rollback is a config flip") <!-- strict propagates; legacy instance of the same code swallows -->
- [x] `failure-handling-S14` — it("Counters visible in status") <!-- get_stats() exposes the three counters -->
- [x] `failure-handling-S15` — it("Cooldown rotation unchanged under the new contract") <!-- GithubCredentialLimited rotates and is NOT counted as failure-empty -->

### File: tests/test_fh_gather_fidelity.py

Describe: Gather-outcome fidelity against a real SQLite registry

Drives the fix through the real chain (`http_get` → `collect()` → AcquisitionStage → Registry). Import failure is the expected RED state.

- [x] `failure-handling-S7` — it("Fetch failure never produces gathered_ok") <!-- WHEN http_get dies THEN visit_status='failed', zero coverage rows, typed failure escapes -->
- [x] `failure-handling-S8` — it("Zero-key success keeps coverage semantics") <!-- WHEN fetch ok, zero keys THEN gathered_ok + coverage row (guard) -->

### File: tests/test_fh_protections.py

Describe: Preserved protections — regression guards (expected GREEN before AND after)

- [x] `failure-handling-S16` — it("Full-pool cooldown waits, never drops") <!-- _get_available blocks on next_wait and returns the credential -->
- [x] (aux, supports S15) — it("Backoff schedule unchanged") <!-- 60→120→240→…→900 clamp; GithubCredentialLimited carries the wait -->

### File: tests/test_cql_lint.py

Describe: Config-time lint for transport-incompatible query syntax

- [x] `config-query-lint-S1` — it("API provider with content qualifier is warned") <!-- observed RED: warnings==[] -->
- [x] `config-query-lint-S2` — it("Web provider with the same query is not warned") <!-- observed GREEN guard -->
- [x] `config-query-lint-S3` — it("Every occurrence is reported separately") <!-- observed RED -->
- [x] `config-query-lint-S4` — it("New qualifier extends linting without validator code change") <!-- observed RED; drives WEB_ONLY_QUALIFIERS constant in constant/search.py -->

## Manual

- `failure-handling-S17` — Restart recovery unaffected: requires a full application boot → run-with-injected-failures → graceful shutdown → restart → recovery cycle (queue-file + shard replay). Executed during the live-verification task (tasks.md §Live), comparing recovered task sets against pre-change format expectations; cannot be meaningfully isolated into a unit test without duplicating the whole app wiring.
- Production shadow-cycle promotion gate (Migration Plan step 2, not a spec scenario): one representative production cycle under `failure_handling: shadow`, including a rate-limit storm; review `failure_empties_detected` per stage before promoting to `strict`. Requires real credentials and wall-clock time; human decision.
