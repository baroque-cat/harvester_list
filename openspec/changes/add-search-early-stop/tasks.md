# Tasks: add-search-early-stop

## 1. RED baseline

- [ ] 1.1 Author tests from tests.md: `tests/test_early_stop.py` (search-early-stop-S1..S4, S6, S11), `tests/test_early_stop_modes.py` (S5, S7..S10); reuse conftest harness + canned page-sequence builders
- [ ] 1.2 Run pytest; confirm all new tests fail for expected reasons (no tracker, no `early_stop` config, no `meta` table) — record RED in tests.md

## 2. Window tracker & stop rule

- [ ] 2.1 Implement per-partition tracker (key = provider+query): rolling deque of W identities with in-window dedupe, pages counter, hypothetical-stop bookkeeping (design D3)
- [ ] 2.2 Implement known-classification by reusing the AMENDED gather-skip conjunction (status ∧ TTL ∧ NULL-tolerant push condition ∧ coverage; discovered/TTL-expired/foreign-coverage count novel — spec R1 aligned to design D1, September 2026) (search-early-stop-S1, S4, S11)
- [ ] 2.3 Implement stop conjunction: ratio ≥ θ ∧ pages ≥ min_pages ∧ trust gate; page-boundary evaluation only (search-early-stop-S1, S2, S3)

## 3. Trust gate, kill-switch, schema evolution

- [ ] 3.1 Add additive `meta(key,value)` table; bump `PRAGMA user_version`; extend `tools/registry_migrate.py` to write `migration_complete=<ts>` (search-early-stop-S7)
- [ ] 3.2 Implement gate queries (COUNT(links) ≥ min_trust, meta marker, ¬degraded) and wire kill-switch to the existing degraded flag (search-early-stop-S7, S8)

## 4. Pipeline wiring & configuration

- [ ] 4.1 Add `early_stop` config section (mode off|shadow|on default off; window=100; theta=0.9 validated ∈[0.5,1.0]; min_pages=2; min_trust=1000) to schemas/loader/validator + examples
- [ ] 4.2 Wire detector into `_handle_first_page_results` / page-task generation (`stage/definition.py:224-296`) for API transport only; hard code-level guard skipping any evaluation for web tasks (search-early-stop-S5, S6)
- [ ] 4.3 Ensure stopped partitions emit no page tasks while refine fan-out and sibling partitions stay untouched (search-early-stop-S6)

## 5. Shadow instrumentation & metrics

- [ ] 5.1 Append `would_stop_at` records (type='early_stop') to `registry_decisions.jsonl` in shadow and on modes (search-early-stop-S9)
- [ ] 5.2 Compute `novel_after_stop` per partition (first_seen within run ∧ arrived after hypothetical stop page); expose counters `early_stop_would_fire`, `early_stop_fired`, `novel_after_stop` in run stats (search-early-stop-S10)

## 6. GREEN & verification

- [ ] 6.1 Drive all automated tests to GREEN; update tests.md statuses
- [ ] 6.2 Regression: suites of changes 1–3 green; `early_stop=off` A/B confirms unchanged pagination
- [ ] 6.3 Staging promotion gate: shadow runs over ≥3 diverse query sets (different providers/patterns); require `novel_after_stop ≈ 0`; document numbers before any `on` recommendation

## 7. Documentation

- [ ] 7.1 README: early-stop modes, four-gate safety model, why web transport is excluded, θ/W tuning guidance
- [ ] 7.2 `docs/specs/registry_flags_metrics.md`: fill `early_stop` row + metric definitions + promotion procedure
