# Tasks: fix-silent-losses

## 1. RED baseline & live probes

- [x] 1.1 Run the five new test files (`tests/test_fh_client_taxonomy.py`, `tests/test_fh_stage_modes.py`, `tests/test_fh_gather_fidelity.py`, `tests/test_fh_protections.py`, `tests/test_cql_lint.py`) and confirm every failure matches the reason recorded in tests.md: collection ImportError on `core.exceptions.TransientFetchError` (3 files), assertion failures cql-S1/S3/S4, guards GREEN (fh-S16, aux backoff, cql-S2)
- [x] 1.2 Guardrail before any tooling run (plan_agr.md §3.4): snapshot `logs/` to a temp dir; pass GitHub credentials only via env `GITHUB_TOKENS`/`GITHUB_SESSIONS` (`config/loader.py:174-182`); never write secrets into files
- [x] 1.3 Live staging probe against real GitHub (early, per wire-format rule): (a) `api.github.com/search/code?q="sk-" AND content:"llm"` with Bearer auth → re-confirm HTTP 200 `{"total_count":0,...}`; (b) request a knowingly-deleted blob URL → capture the 404 shape; save both raw samples as live-captured fixtures under `tests/fixtures/` with provenance headers (endpoint, capture date 2026-09-20 or later, auth mode); label any synthesized variants as supplementary unit vectors

## 2. Client-boundary failure taxonomy (failure-handling-S1, S2, S3)

- [x] 2.1 Add `TransientFetchError(NetworkError)` to `core/exceptions.py` — the typed signal for "no usable answer obtained"
- [x] 2.2 Classify empties in `search_api_with_count` / `search_web_with_count` / `search_code` (`search/client.py`): legitimate zero = HTTP 200 + successful parse (return normally, S1 guard); failure-empty = exception after retries, `_limit()` suppression `"", {}` (`client.py:370-372`), blank/undecodable content → raise `TransientFetchError` (S2, S3)
- [x] 2.3 Narrow `collect()`'s `@handle_exceptions(default_result=[])` (`client.py:1312`): fetch-phase failures (including HTTP 404, design D4) propagate as `TransientFetchError`; extraction/parsing of a fetched payload stays fail-open (`[]` + counted warning) per project convention
- [x] 2.4 Drive `tests/test_fh_client_taxonomy.py` to GREEN (S1 stays green as the legitimate-zero guard)

## 3. Tri-mode flag and stage failure contract (failure-handling-S4, S5, S6, S9, S10, S11, S12, S13, S14, S15)

- [x] 3.1 Add `failure_handling: str = "shadow"` to `PipelineConfig` (`config/schemas.py`), parse it in `config/loader.py`, accept `legacy|shadow|strict` in `config/validator.py` (invalid value → validation error)
- [x] 3.2 Implement the mode policy for stage workers: `legacy` — behavior byte-equivalent to pre-change (typed failures swallowed, new counters inert); `shadow` — swallow + increment `failure_empties_detected` + contextual WARNING log (stage/provider/task); `strict` — let `TransientFetchError` escape `process_task` so `_worker_loop`'s existing retry branch fires (`stage/base.py:457-470`); unexpected non-transient exceptions keep the current log-and-None path (scope discipline)
- [x] 3.3 Apply the policy to `SearchStage._search_worker` (`stage/definition.py:191-193`) keeping the `GithubCredentialLimited` rotation channel untouched and NOT counted as failure-empty (S15)
- [x] 3.4 Replace CheckStage limiter-starvation `return None` (`stage/definition.py:661-667`) with the typed failure under the mode policy (S9); apply the same contract to `InspectStage` uniformly (design D8)
- [x] 3.5 Add counters `failure_empties_detected`, `tasks_requeued`, `tasks_dropped_max_retries` to stage state and `StageMetrics` (`core/metrics.py`), incrementing in the worker-loop requeue path and the `put_task` over-bound discard branch (`stage/base.py:253-256, 461-470`); surface via `get_stats()`/status builder (S14)
- [x] 3.6 Verify the dedup gate admits bounded requeues (`0 < attempts <= max_retries`, `stage/base.py:252`) with a regression test assertion — no structural change expected (S5 guard)
- [x] 3.7 Drive `tests/test_fh_stage_modes.py` to GREEN, including the end-to-end worker-loop cycle (3 executions, attempts==3, total_errors==3, loud drop) and the strict→legacy flag-flip rollback (S13)

## 4. Gather-outcome fidelity (failure-handling-S7, S8)

- [x] 4.1 In `AcquisitionStage._acquisition_worker` (`stage/definition.py:514-569`): on `TransientFetchError` from `collect()` record `record_gather(success=False)` (existing except branch) and apply the mode policy — under `strict` the failure escapes for bounded requeue; `success=True` is written only when the fetch actually completed (S7)
- [x] 4.2 Preserve "fetched, zero keys extracted" semantics: `gathered_ok` + coverage row unchanged (S8 guard stays GREEN)
- [x] 4.3 Drive `tests/test_fh_gather_fidelity.py` to GREEN against the real SQLite registry harness

## 5. Config query lint (config-query-lint-S1..S4)

- [x] 5.1 Add `WEB_ONLY_QUALIFIERS = ("content:",)` to `constant/search.py` (data-driven list, S4)
- [x] 5.2 Validator rule in `config/validator.py`: for every `use_api: true` task, scan condition queries for listed qualifiers; emit one WARNING per occurrence naming provider and condition index (never an error; config still loads) citing API-transport inertness (S1, S3); no warning for `use_api: false` (S2 guard)
- [x] 5.3 Drive `tests/test_cql_lint.py` to GREEN

## 6. Full-suite regression

- [x] 6.1 All five new test files GREEN; protection guards (fh-S16, aux backoff, cql-S2) still GREEN
- [x] 6.2 Run the complete existing suite (`python3 -m pytest tests/`) — zero regressions, especially registry/gather-skip/recheck suites that consume `record_gather` outcomes and stage metrics

## 7. Live verification that the fixes work (operator-visible proof)

- [x] 7.1 Fault-injection E2E on a local mock GitHub (500s, timeouts, 404s, suppressed blanks) with a two-provider config sharing one query, temp workspace and its own logging dir: under `strict` — observe requeue log lines, counters > 0, registry rows `visit_status='failed'`, ZERO false `gathered_ok`, no silent-success accounting; under `shadow` — harvest output identical to `legacy` while `failure_empties_detected` ticks; under `legacy` — outputs byte-equivalent to pre-change behavior
- [x] 7.2 Restart-recovery check (fh-S17 Manual): graceful shutdown mid-failure-run → restart → recovered task sets from queue files + shards match pre-change formats exactly (no schema/format drift)
- [x] 7.3 Short real-GitHub run under `shadow` (env credentials, §3.4 guardrails) on representative queries incl. one known-dead API `content:` condition: confirm lint warning fires on the real config, counters accumulate realistically, harvest shows no regression vs baseline
- [x] 7.4 Record probe/E2E/shadow-run results (dates, configs, counter snapshots) as verification notes in the change directory; promotion `shadow → strict` remains an operator decision per Migration Plan

## 8. Docs & rollout

- [x] 8.1 README: `failure_handling` section — modes, default `shadow`, counters, promotion criteria, rollback = config flip (project paradigm)
- [x] 8.2 `examples/config-full.yaml`: commented `failure_handling` entry under `pipeline:` with mode explanations
- [x] 8.3 `openspec validate --strict` passes; every automated scenario in tests.md GREEN and Manual entries executed; change ready for archive
