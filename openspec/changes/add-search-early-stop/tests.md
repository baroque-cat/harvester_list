# Test Plan: add-search-early-stop

<!-- Derived mechanically from specs/search-early-stop/spec.md. One scenario = one test
     or one Manual entry. IDs stable: never renumber, append only.
     NOTE (planning mode): .py test files are authored in apply-phase task group 1;
     expected initial state: RED (tracker module/meta table/flag do not exist yet). -->

## Traceability

| Scenario ID | Spec | Requirement | Scenario | Test file | Status |
|-------------|------|-------------|----------|-----------|--------|
| `search-early-stop-S1` | `specs/search-early-stop/spec.md` | Frontier-based pagination stop | Saturated window stops pagination | tests/test_early_stop.py | RED |
| `search-early-stop-S2` | `specs/search-early-stop/spec.md` | Frontier-based pagination stop | Never stops on the first page | tests/test_early_stop.py | RED |
| `search-early-stop-S3` | `specs/search-early-stop/spec.md` | Frontier-based pagination stop | Unsaturated window continues to the cap | tests/test_early_stop.py | RED |
| `search-early-stop-S4` | `specs/search-early-stop/spec.md` | Frontier-based pagination stop | Discovered-only links count as novel | tests/test_early_stop.py | RED |
| `search-early-stop-S5` | `specs/search-early-stop/spec.md` | Web transport exclusion | Web run with flag on paginates unchanged | tests/test_early_stop_modes.py | RED |
| `search-early-stop-S6` | `specs/search-early-stop/spec.md` | Per-partition independence | Mixed partitions act independently | tests/test_early_stop.py | RED |
| `search-early-stop-S7` | `specs/search-early-stop/spec.md` | Trust gate and kill-switch | Fresh registry forces full passes | tests/test_early_stop_modes.py | RED |
| `search-early-stop-S8` | `specs/search-early-stop/spec.md` | Trust gate and kill-switch | Mid-run error mutes stopping | tests/test_early_stop_modes.py | RED |
| `search-early-stop-S9` | `specs/search-early-stop/spec.md` | Three-mode flag with shadow measurement | Shadow logs but never acts | tests/test_early_stop_modes.py | RED |
| `search-early-stop-S10` | `specs/search-early-stop/spec.md` | Three-mode flag with shadow measurement | False-stop price is measured | tests/test_early_stop_modes.py | RED |
| `search-early-stop-S11` | `specs/search-early-stop/spec.md` | Frontier-based pagination stop | TTL-expired links count as novel | tests/test_early_stop.py | RED |

## Automated

### File: tests/test_early_stop.py

Describe: Window tracker and stop rule (unit level)

<!-- Harness: tracker driven directly with canned page-result sequences; tmp registry pre-populated via change-1 writer API (gathered_ok/discovered/coverage variants). RED state: tracker module absent (ImportError). -->

- [ ] `search-early-stop-S1` — it("Saturated window stops pagination") <!-- WHEN pages 1-2 give 100 results, 97 known, trust gate ok THEN no page-3 task + early_stop_fired=1 -->
- [ ] `search-early-stop-S2` — it("Never stops on the first page") <!-- WHEN page 1 fully saturated THEN page 2 still requested -->
- [ ] `search-early-stop-S3` — it("Unsaturated window continues to the cap") <!-- WHEN ratio < θ on every page THEN pages up to API_MAX_PAGES generated as pre-change -->
- [ ] `search-early-stop-S4` — it("Discovered-only links count as novel") <!-- WHEN window contains discovered-status or foreign-coverage links THEN they lower the ratio (no stop at 0.9 threshold when their share > 0.1) -->
- [ ] `search-early-stop-S6` — it("Mixed partitions act independently") <!-- WHEN partition A saturates and B stays novel THEN A stops, B continues; trackers isolated by query key -->
- [ ] `search-early-stop-S11` — it("TTL-expired links count as novel") <!-- WHEN window contains gathered_ok links whose gathered_ts exceeds gather TTL THEN they lower the known-ratio (full amended conjunction per design D1 alignment note) -->

### File: tests/test_early_stop_modes.py

Describe: Transport exclusion, trust gate, kill-switch, shadow instrumentation

<!-- Harness: SearchStage pagination entry invoked with mocked client returning canned pages per transport; registry manipulated (row counts, degraded flag, meta marker); JSONL decision log asserted. RED state: early_stop config/meta table absent. -->

- [ ] `search-early-stop-S5` — it("Web run with flag on paginates unchanged") <!-- WHEN early_stop=on and use_api=false THEN page tasks identical to baseline and zero tracker evaluations (spy) -->
- [ ] `search-early-stop-S7` — it("Fresh registry forces full passes") <!-- WHEN COUNT(links) < min_trust OR meta.migration_complete missing THEN saturated windows do not stop -->
- [ ] `search-early-stop-S8` — it("Mid-run error mutes stopping") <!-- WHEN registry error sets degraded mid-run THEN later saturation does not stop pagination -->
- [ ] `search-early-stop-S9` — it("Shadow logs but never acts") <!-- WHEN early_stop=shadow and window saturates at page 2 THEN would_stop_at JSONL record written, early_stop_would_fire=1, pages 3+ fetched -->
- [ ] `search-early-stop-S10` — it("False-stop price is measured") <!-- WHEN 4 links first-seen this run arrive after the hypothetical stop page THEN novel_after_stop=4 for that partition -->

## Manual

<!-- None: all ten scenarios are automatable offline with canned page sequences and the tmp-registry harness. The live staging promotion gate (novel_after_stop ≈ 0 across ≥3 diverse query sets) is executed as task 6.3, not a spec scenario. -->
