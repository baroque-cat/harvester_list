# Tasks: add-target-prioritization

## 1. RED baseline

- [x] 1.1 Author tests from tests.md: `tests/test_priority_scoring.py` (target-prioritization-S1..S6, S10), `tests/test_export_candidates.py` (S7..S9); seeding helpers reuse conftest registry harness
- [x] 1.2 Run pytest; confirm all new tests fail for expected reasons (no scorer module, no `tools/export_candidates.py`) — record RED in tests.md

## 2. Scoring core & configuration

- [x] 2.1 Implement scorer per design D2 formula: components valid_present/soft_present/freshness-decay/size-penalty with NULL-neutral degradation (target-prioritization-S1..S4)
- [x] 2.2 Add `prioritization` config section (weights W1/W2/W4/H/W5/T/R validated positive; `display_top_n` default 0) to schemas/loader/validator + examples (target-prioritization-S10)
- [x] 2.3 Key→repo attribution via `keys.source_url_hash → links.(owner,repo)`; unattributable legacy keys excluded from repo scores with documented limitation (design D2)

## 3. Recomputation wiring

- [x] 3.1 Dirty-repo set in the registry writer path: key-status upserts and date merges enqueue `(owner,repo)`; batch flush rescores via indexed group queries + batched UPDATE of `links.priority` (target-prioritization-S5)
- [x] 3.2 Run-finish full sweep (`DISTINCT owner,repo` iteration) hooked into pipeline stop/completion sequence (target-prioritization-S6)

## 4. Export utility

- [x] 4.1 Implement `tools/export_candidates.py`: argparse CLI (--workspace, --format ndjson|csv, --min-priority, --limit, --sample-links), read-only connection, streaming aggregation, deterministic sort, `schema_version:"1.0"` frozen field set (target-prioritization-S7, S8)
- [x] 4.2 Isolation guarantees: no writes beyond none (pure read), socket-guard test passes (target-prioritization-S9)

## 5. Optional visibility

- [x] 5.1 StatusManager top-N candidates line behind `display_top_n > 0` (off by default; cosmetic only)

## 6. GREEN & verification

- [x] 6.1 Drive all automated tests to GREEN; update tests.md statuses
- [x] 6.2 Regression: suites of changes 1–5 green; export against a change-1-migrated real-shaped workspace produces plausible ranking (operator eyeball, recorded in PR notes)

## 7. Documentation

- [x] 7.1 `docs/specs/candidates_export.md`: the handoff contract — field dictionary, schema_version policy, consumer guidance (re-ranking what-ifs from raw fields), advanced direct-SQLite option note
- [x] 7.2 README: prioritization formula summary + defaults table + export usage; `docs/specs/registry_flags_metrics.md`: fill `prioritization` rows
