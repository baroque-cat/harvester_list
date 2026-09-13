# Test Plan: add-gather-skip

<!-- Derived mechanically from specs/gather-skip/spec.md. One scenario = one test or
     one Manual entry. IDs stable: never renumber, append only.
     NOTE (planning mode): .py test files are authored in apply-phase task group 1;
     expected initial state: RED (decision module/flag do not exist yet).
     AMENDMENT (September 2026): S11/S12 appended with the NULL-tolerant condition-3
     spec amendment (date-extraction D7 fallout); design D3 always intended vacuous
     pass — tests now pin it explicitly plus the push_signal_coverage metric.
     RED CONFIRMED (apply task 1.2): both files collect-error with
     ModuleNotFoundError: No module named 'storage.gather_skip' — the decision
     module and the skip_known config do not exist yet. -->

## Traceability

| Scenario ID | Spec | Requirement | Scenario | Test file | Status |
|-------------|------|-------------|----------|-----------|--------|
| `gather-skip-S1` | `specs/gather-skip/spec.md` | Conjunctive skip rule | Fully known fresh covered link is skipped | tests/test_gather_skip.py | GREEN |
| `gather-skip-S2` | `specs/gather-skip/spec.md` | Conjunctive skip rule | Never-gathered link is not skipped | tests/test_gather_skip.py | GREEN |
| `gather-skip-S3` | `specs/gather-skip/spec.md` | Conjunctive skip rule | TTL expiry forces re-gather | tests/test_gather_skip.py | GREEN |
| `gather-skip-S4` | `specs/gather-skip/spec.md` | Conjunctive skip rule | Fresh push invalidates prior gather | tests/test_gather_skip.py | GREEN |
| `gather-skip-S5` | `specs/gather-skip/spec.md` | Conjunctive skip rule | Provider or pattern switch forces re-gather | tests/test_gather_skip.py | GREEN |
| `gather-skip-S6` | `specs/gather-skip/spec.md` | Three-mode rollout flag | Off mode is indistinguishable from pre-change behavior | tests/test_gather_skip_modes.py | GREEN |
| `gather-skip-S7` | `specs/gather-skip/spec.md` | Three-mode rollout flag | Shadow mode logs without acting | tests/test_gather_skip_modes.py | GREEN |
| `gather-skip-S8` | `specs/gather-skip/spec.md` | Failed gathers are never skipped | Failed link re-gathered despite fresh timestamp | tests/test_gather_skip.py | GREEN |
| `gather-skip-S9` | `specs/gather-skip/spec.md` | Fail-open reads | Read error produces tasks, not skips | tests/test_gather_skip_modes.py | GREEN |
| `gather-skip-S10` | `specs/gather-skip/spec.md` | Reason-attributed decision metrics | Counters reflect a mixed page of links | tests/test_gather_skip.py | GREEN |
| `gather-skip-S11` | `specs/gather-skip/spec.md` | Conjunctive skip rule | NULL push date does not veto skip | tests/test_gather_skip.py | GREEN |
| `gather-skip-S12` | `specs/gather-skip/spec.md` | Reason-attributed decision metrics | Push-signal coverage reported | tests/test_gather_skip.py | GREEN |

## Automated

### File: tests/test_gather_skip.py

Describe: Conjunctive skip rule and reason attribution

<!-- Harness: tmp-workspace registry pre-populated via the change-1 writer API; decision function invoked directly with synthetic page URL lists; no network, no live GitHub. Initial RED state: decision module absent (ImportError); now GREEN. -->

- [x] `gather-skip-S1` — it("Fully known fresh covered link is skipped") <!-- WHEN gathered_ok 1d ago, same provider+patterns, no newer push THEN no task + skipped_known=1 -->
- [x] `gather-skip-S2` — it("Never-gathered link is not skipped") <!-- WHEN visit_status='discovered' THEN task created -->
- [x] `gather-skip-S3` — it("TTL expiry forces re-gather") <!-- WHEN gathered_ts older than skip.gather_ttl_hours THEN task + regathered_ttl_expired=1 -->
- [x] `gather-skip-S4` — it("Fresh push invalidates prior gather") <!-- WHEN repo_pushed_at non-NULL and > gathered_ts (+grace) THEN task + regathered_changed=1 -->
- [x] `gather-skip-S5` — it("Provider or pattern switch forces re-gather") <!-- WHEN coverage row exists only for other provider OR other patterns_hash THEN task + regathered_coverage_gap=1 -->
- [x] `gather-skip-S8` — it("Failed link re-gathered despite fresh timestamp") <!-- WHEN visit_status='failed' with recent gathered_ts THEN task + regathered_failed_retry=1 -->
- [x] `gather-skip-S10` — it("Counters reflect a mixed page of links") <!-- WHEN page of 6 links (skippable/ttl/changed/coverage/failed/unseen) THEN exact counter vector and 5 tasks -->
- [x] `gather-skip-S11` — it("NULL push date does not veto skip") <!-- WHEN gathered_ok within TTL + coverage match but repo_pushed_at IS NULL THEN no task, skipped_known=1, decision-log conditions snapshot marks push evidence absent -->
- [x] `gather-skip-S12` — it("Push-signal coverage reported") <!-- WHEN 100 known links evaluated, 30 with non-NULL repo_pushed_at THEN push_signal_coverage == 0.30 in run stats -->

### File: tests/test_gather_skip_modes.py

Describe: Flag modes and fail-open behavior

<!-- Harness: SearchStage worker invoked with mocked client returning canned URL sets; registry path manipulated to force read errors. Initial RED state: skip_known config/flag absent; now GREEN. -->

- [x] `gather-skip-S6` — it("Off mode is indistinguishable from pre-change behavior") <!-- WHEN skip_known=off THEN zero registry read calls (spy) and identical task list vs baseline -->
- [x] `gather-skip-S7` — it("Shadow mode logs without acting") <!-- WHEN skip_known=shadow over skippable links THEN JSONL decision entries with url_hash+conditions AND all tasks still created -->
- [x] `gather-skip-S9` — it("Read error produces tasks, not skips") <!-- WHEN batched lookup raises (corrupt/locked ro connection) THEN tasks for whole page, warning logged once, run completes -->

## Manual

<!-- None: all twelve scenarios are automatable offline against the tmp-workspace registry harness. The shadow-period empirical validation (false-skip price ≈ 0 via decision-log × shard-outcome join on live staging traffic) is a promotion gate executed as task 5.3, not a spec scenario. Note for staging analysis: while dates are NULL fleet-wide (pre-enrichment), false-skip price measures precisely the TTL-only regime's cost.
GATE RESULT (2026-09-13, live GitHub, task 5.3): 26 known links evaluated in `shadow`, all would-skip with `push_evidence=false`; `material` key sets of the `shadow` and `off` cycles identical -> false-skip price 0.0, `push_signal_coverage` 0.000. Full detail in tasks.md task 5.3. Default remains `off`. -->
