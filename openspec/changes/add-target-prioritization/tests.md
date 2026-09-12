# Test Plan: add-target-prioritization

<!-- Derived mechanically from specs/target-prioritization/spec.md. One scenario = one
     test or one Manual entry. IDs stable: never renumber, append only.
     NOTE (planning mode): .py test files are authored in apply-phase task group 1;
     expected initial state: RED (scorer/export tool do not exist yet). -->

## Traceability

| Scenario ID | Spec | Requirement | Scenario | Test file | Status |
|-------------|------|-------------|----------|-----------|--------|
| `target-prioritization-S1` | `specs/target-prioritization/spec.md` | Repository priority scoring | Valid-key repo outranks keyless repo | tests/test_priority_scoring.py | RED |
| `target-prioritization-S2` | `specs/target-prioritization/spec.md` | Repository priority scoring | Fresher activity outranks stale | tests/test_priority_scoring.py | RED |
| `target-prioritization-S3` | `specs/target-prioritization/spec.md` | Repository priority scoring | Oversized repo is penalized | tests/test_priority_scoring.py | RED |
| `target-prioritization-S4` | `specs/target-prioritization/spec.md` | Repository priority scoring | Sparse rows degrade neutrally | tests/test_priority_scoring.py | RED |
| `target-prioritization-S5` | `specs/target-prioritization/spec.md` | Score convergence with ledger state | Status transition rescores without waiting for sweep | tests/test_priority_scoring.py | RED |
| `target-prioritization-S6` | `specs/target-prioritization/spec.md` | Score convergence with ledger state | Run-finish sweep reconciles everything | tests/test_priority_scoring.py | RED |
| `target-prioritization-S7` | `specs/target-prioritization/spec.md` | Candidate export utility | NDJSON export is ordered and complete | tests/test_export_candidates.py | RED |
| `target-prioritization-S8` | `specs/target-prioritization/spec.md` | Candidate export utility | CSV variant carries equivalent content | tests/test_export_candidates.py | RED |
| `target-prioritization-S9` | `specs/target-prioritization/spec.md` | Read-side isolation | Export leaves operational artifacts untouched | tests/test_export_candidates.py | RED |
| `target-prioritization-S10` | `specs/target-prioritization/spec.md` | Configurable weights and thresholds | Custom weights change ordering | tests/test_priority_scoring.py | RED |

## Automated

### File: tests/test_priority_scoring.py

Describe: Scoring formula, neutral degradation, convergence triggers, weight configuration

<!-- Harness: tmp registry seeded via change-1 writer API (links rows with controlled dates/sizes, keys rows with statuses and source_url_hash attribution); scorer invoked directly on (owner,repo) and via dirty-set/sweep paths. RED state: scorer module absent (ImportError). -->

- [ ] `target-prioritization-S1` — it("Valid-key repo outranks keyless repo") <!-- WHEN repo A has valid-status key link, repo B identical evidence minus keys THEN priority(A) − priority(B) ≥ W1 -->
- [ ] `target-prioritization-S2` — it("Fresher activity outranks stale") <!-- WHEN identical evidence, pushed_at differs by 60 days THEN fresher scores strictly higher on W4 component -->
- [ ] `target-prioritization-S3` — it("Oversized repo is penalized") <!-- WHEN identical evidence, size crosses threshold T toward ramp R THEN oversized scores lower by computed penalty -->
- [ ] `target-prioritization-S4` — it("Sparse rows degrade neutrally") <!-- WHEN NULL pushed_at/size and no keys THEN priority == 0, no exception -->
- [ ] `target-prioritization-S5` — it("Status transition rescores without waiting for sweep") <!-- WHEN wait_check→valid upsert flushes mid-run THEN repo priority includes W1 before run finish -->
- [ ] `target-prioritization-S6` — it("Run-finish sweep reconciles everything") <!-- WHEN mutations bypassed dirty-marking (direct seed) THEN post-sweep priorities equal freshly computed values for all repos -->
- [ ] `target-prioritization-S10` — it("Custom weights change ordering") <!-- WHEN W1=0, W4>0 THEN fresh keyless repo can outrank stale valid-key repo -->

### File: tests/test_export_candidates.py

Describe: Export utility determinism, formats, read-side isolation

<!-- Harness: seeded tmp workspace incl. dummy shard/snapshot files hashed before/after; tool invoked as library function and as subprocess CLI. RED state: tools/export_candidates.py absent. -->

- [ ] `target-prioritization-S7` — it("NDJSON export is ordered and complete") <!-- WHEN exporting mixed-priority registry THEN lines parse as JSON, strict priority-desc order (pushed_at desc, owner/repo tiebreak), every record has schema_version+required fields, --min-priority/--limit honored -->
- [ ] `target-prioritization-S8` — it("CSV variant carries equivalent content") <!-- WHEN --csv over same registry THEN same repos, same order, equivalent field values -->
- [ ] `target-prioritization-S9` — it("Export leaves operational artifacts untouched") <!-- WHEN export completes THEN shard/snapshot byte-hashes unchanged; registry diff confined to priority columns; zero network calls (mock socket guard) -->

## Manual

<!-- None: all ten scenarios are automatable offline against seeded tmp registries. Real-corpus sanity (ranking plausibility on accumulated production data, what-if weight tuning) is an operator activity documented in task 7.1, not a spec scenario. -->
