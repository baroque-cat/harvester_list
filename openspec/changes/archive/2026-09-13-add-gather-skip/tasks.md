# Tasks: add-gather-skip

## 1. RED baseline

- [x] 1.1 Author tests from tests.md: `tests/test_gather_skip.py` (gather-skip-S1..S5, S8, S10..S12), `tests/test_gather_skip_modes.py` (S6, S7, S9); reuse change-1 conftest harness (tmp registry, synthetic shards)
- [x] 1.2 Run pytest; confirm all new tests fail for expected reasons (no decision module, no `skip_known` config) — record RED in tests.md

## 2. Decision engine & read path

- [x] 2.1 Add read-only connection helper (`file:<path>?mode=ro`, URI) with lazy per-thread open and graceful error capture (gather-skip-S9)
- [x] 2.2 Implement batched page lookup: `SELECT` links + coverage `WHERE url_hash IN (…)` → decision map (design D2)
- [x] 2.3 Implement conjunctive rule with cheapest-first evaluation and first-failure reason attribution; NULL `repo_pushed_at` passes condition 3 vacuously (prevailing production mode per date-extraction D7 — spec amended September 2026); 60 s grace on push comparison when non-NULL (gather-skip-S1..S5, S8, S10, S11; design D3)

## 3. Pipeline wiring & configuration

- [x] 3.1 Add `skip` config section (`skip_known: off|shadow|on` default off, `gather_ttl_hours` default 168) to schemas/loader/validator + `examples/config-full.yaml`
- [x] 3.2 Wire decision point into `SearchStage._search_worker` before `create_acquisition_task` (`stage/definition.py:114-127`); ensure `output.add_links` still records every hit regardless of decision (design D7)
- [x] 3.3 Guarantee zero registry reads when `skip_known=off` (gather-skip-S6)

## 4. Shadow log & metrics

- [x] 4.1 Implement append-only JSONL decision log `<workspace>/registry_decisions.jsonl` (ts, run_id, url_hash, provider, conditions snapshot, decision) written in shadow and on modes (gather-skip-S7)
- [x] 4.2 Add counters `skipped_known`, `regathered_{changed,coverage_gap,ttl_expired,failed_retry}`, and `push_signal_coverage` (share of evaluated known links with non-NULL `repo_pushed_at` evidence) to run stats and status display (gather-skip-S10, S12)

## 5. GREEN & verification

- [x] 5.1 Drive all automated tests to GREEN; update tests.md statuses
- [x] 5.2 Regression: change-1 and change-2 suites stay green; off-mode A/B confirms unchanged shards
- [x] 5.3 Staging promotion gate: run ≥1 full cycle in `shadow`; join decision log with `material`/`valid` shards; compute false-skip price (keys found at would-skipped URLs); document result; only then flip `on`
  - Result (2026-09-13, live GitHub, workspace `/tmp/opencode/shadow_ws`, out of repo): cycle 1 `off` produced 26 `gathered_ok` links + coverage (`runs.degraded=0`); cycle 2 `shadow` re-found the same 26 URLs and logged 26 would-skip decisions (all `status_ok/coverage_ok/ttl_ok=true`, `push_evidence=false`); cycle 3 `off` baseline re-gathered the same set. Shadow and off `material` key sets are identical; **false-skip price = 0/26 = 0.0**. `push_signal_coverage = 0.000` (NULL `repo_pushed_at` fleet-wide) so this validates the TTL-only regime only. Limitation: shard rows carry no source URL until `add-key-ledger` (`keys.source_url_hash`), so the join is the documented conservative set-diff. Repo default stays `off`; flipping `on` is a deployment decision.

## 6. Documentation

- [x] 6.1 README: skip modes, TTL semantics, four-condition rule table, rollback procedure
- [x] 6.2 `docs/specs/registry_flags_metrics.md`: fill in `skip_known` row + `regathered_*` metric definitions and the shadow-promotion procedure
