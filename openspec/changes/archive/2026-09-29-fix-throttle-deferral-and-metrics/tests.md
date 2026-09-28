# Test Plan

Derived mechanically from the delta specs under `specs/`. Scenario IDs are stable and
append-only: `gather-transport` continues its shipped numbering (S22+ are new, S9–S21 are
preserved scenarios whose existing pins are cited), `failure-handling` uses the `FH3-` prefix for
this change's delta (precedent: `FH2-` in `fix-credential-liveness`), `run-observability` is new
and uses `RO-`.

## Traceability

| Scenario ID | Spec | Requirement | Scenario | Test file | Status |
|-------------|------|-------------|----------|-----------|--------|
| `gather-transport-S9` | `specs/gather-transport/spec.md` | Gather traffic is throttled under its own budget | plain-content host resolves to a real budget | `tests/test_gt_transport.py::test_s9_plain_content_host_resolves_to_a_real_budget`, `::test_s9_failure_is_reported_to_the_adaptive_budget` | EXISTING (unchanged) |
| `gather-transport-S10` | `specs/gather-transport/spec.md` | Gather traffic is throttled under its own budget | gathering does not consume the search budget | `tests/test_gt_transport.py::test_s10_gathering_does_not_consume_the_search_budget` | EXISTING (unchanged) |
| `gather-transport-S11` | `specs/gather-transport/spec.md` | Gather traffic is throttled under its own budget | authenticated transports keep their existing budgets | `tests/test_gt_transport.py` (S11 pin) | EXISTING (unchanged) |
| `gather-transport-S22` | `specs/gather-transport/spec.md` | Gather traffic is throttled under its own budget | a withheld request does not decay the budget | `tests/test_tdm_starvation.py::test_s22_withheld_request_does_not_decay_the_budget` | GREEN |
| `gather-transport-S23` | `specs/gather-transport/spec.md` | Gather traffic is throttled under its own budget | sustained contention does not collapse the budget | `tests/test_tdm_starvation.py::test_s23_sustained_contention_does_not_collapse_the_budget` | GREEN |
| `gather-transport-S12` | `specs/gather-transport/spec.md` | Refusals are classified by signal | forbidden-with-rate-limit-marker is a deferral | `tests/test_gt_transport.py` (S12 pin) | EXISTING (unchanged) |
| `gather-transport-S13` | `specs/gather-transport/spec.md` | Refusals are classified by signal | genuine authentication failure drops loudly once | `tests/test_gt_transport.py` (S13 pin) | EXISTING (unchanged) |
| `gather-transport-S14` | `specs/gather-transport/spec.md` | Refusals are classified by signal | missing resource drops loudly once | `tests/test_gt_transport.py` (S14 pin) | EXISTING (unchanged) |
| `gather-transport-S15` | `specs/gather-transport/spec.md` | Refusals are classified by signal | finite quota exhaustion defers without burning attempts | `tests/test_gt_transport.py` (S15 pin) | EXISTING (unchanged) |
| `gather-transport-S16` | `specs/gather-transport/spec.md` | Refusals are classified by signal | actor-scoped abuse refusal pauses the stage | `tests/test_gt_transport.py` (S16 pin) | EXISTING (unchanged) |
| `gather-transport-S24` | `specs/gather-transport/spec.md` | Refusals are classified by signal | own-budget withholding defers with a bounded wait and a stage pause | `tests/test_tdm_starvation.py::test_s24_own_budget_withholding_defers_with_bounded_wait_and_stage_pause` | GREEN |
| `gather-transport-S25` | `specs/gather-transport/spec.md` | Refusals are classified by signal | own-budget withholding is counted apart from remote refusals | `tests/test_tdm_starvation.py::test_s25_own_budget_withholding_is_counted_apart_from_remote_refusals` | GREEN |
| `gather-transport-S26` | `specs/gather-transport/spec.md` | Refusals are classified by signal | legacy classification is a configuration flip | `tests/test_tdm_starvation.py::test_s26_legacy_classification_is_a_configuration_flip` (+ supporting config-surface pin `::test_s26b_flag_config_surface`) | GREEN |
| `gather-transport-S19` | `specs/gather-transport/spec.md` | Payload handling is economical, bounded and observable | plainer payload loses no findings | `tests/test_gt_transport.py` (S19 pin) | EXISTING (unchanged) |
| `gather-transport-S20` | `specs/gather-transport/spec.md` | Payload handling is economical, bounded and observable | oversized payload is truncated and still searched | `tests/test_gt_transport.py` (S20 pin) | EXISTING (unchanged) |
| `gather-transport-S21` | `specs/gather-transport/spec.md` | Payload handling is economical, bounded and observable | transport economics are observable | `tests/test_gt_stage_integration.py::test_s21_transport_economics_are_observable` | EXISTING — see note 1 |
| `gather-transport-S27` | `specs/gather-transport/spec.md` | Payload handling is economical, bounded and observable | latency percentiles are published per transport | `tests/test_tdm_latency.py::test_s27_latency_percentiles_are_published_per_transport` | GREEN |
| `gather-transport-S28` | `specs/gather-transport/spec.md` | Payload handling is economical, bounded and observable | latency summary costs bounded memory | `tests/test_tdm_latency.py::test_s28_latency_summary_costs_bounded_memory` | GREEN |
| `gather-transport-S29` | `specs/gather-transport/spec.md` | Payload handling is economical, bounded and observable | a withheld fetch is not timed as a slow request | `tests/test_tdm_starvation.py::test_s29_withheld_fetch_is_not_timed` | GREEN |
| `failure-handling-FH3-S1` | `specs/failure-handling/spec.md` | Empty-result taxonomy at the fetch boundary | Legitimate zero passes through cleanly | `tests/test_fh_client_taxonomy.py::test_s1_legitimate_zero_passes_through_cleanly` | EXISTING (unchanged) |
| `failure-handling-FH3-S2` | `specs/failure-handling/spec.md` | Empty-result taxonomy at the fetch boundary | Network failure after retries is a failure-empty | `tests/test_fh_client_taxonomy.py::test_s2_api_network_failure_is_transient_fetch_error`, `::test_s2_web_blank_content_is_transient_fetch_error` | EXISTING (unchanged) |
| `failure-handling-FH3-S3` | `specs/failure-handling/spec.md` | Empty-result taxonomy at the fetch boundary | Limiter suppression is a failure-empty | `tests/test_fh_client_taxonomy.py::test_s3_limiter_suppression_is_failure_empty` | EXISTING (**deliberately preserved**, design D11) |
| `failure-handling-FH3-S4` | `specs/failure-handling/spec.md` | Empty-result taxonomy at the fetch boundary | Rate-limit refusal is a deferral, not an answer and not a task fault | `tests/test_fh_client_taxonomy.py::test_s18_rate_limit_refusal_is_a_deferral_not_an_answer` | EXISTING (unchanged) |
| `failure-handling-FH3-S5` | `specs/failure-handling/spec.md` | Empty-result taxonomy at the fetch boundary | Gather withholding by the system's own budget is a deferral | `tests/test_tdm_stage_defer.py::test_fh3_s5_own_budget_withholding_is_a_deferral_not_a_failure_empty` | GREEN |
| `failure-handling-FH3-S6` | `specs/failure-handling/spec.md` | Empty-result taxonomy at the fetch boundary | The search boundary keeps its failure-empty classification | `tests/test_tdm_starvation.py::test_fh3_s6_search_boundary_is_unchanged_by_the_flag` | GREEN |
| `failure-handling-FH3-S7` | `specs/failure-handling/spec.md` | Bounded retry and honest accounting | Transient failure triggers bounded requeue | `tests/test_fh_stage_modes.py::test_s12_strict_propagates_typed_failure` | EXISTING (unchanged) |
| `failure-handling-FH3-S8` | `specs/failure-handling/spec.md` | Bounded retry and honest accounting | Re-enqueue passes the deduplication gate | `tests/test_fh_stage_modes.py::test_s5_dedup_gate_admits_bounded_requeue` | EXISTING (unchanged) |
| `failure-handling-FH3-S9` | `specs/failure-handling/spec.md` | Bounded retry and honest accounting | Exhausted retries drop loudly | `tests/test_fh_stage_modes.py::test_s4_s6_strict_requeues_within_bound_then_drops_loudly` | EXISTING (unchanged) |
| `failure-handling-FH3-S10` | `specs/failure-handling/spec.md` | Bounded retry and honest accounting | Deferral does not consume the retry budget | `tests/test_fh_stage_modes.py::test_s19_deferral_does_not_consume_the_retry_budget` | EXISTING (unchanged) |
| `failure-handling-FH3-S11` | `specs/failure-handling/spec.md` | Bounded retry and honest accounting | Deferral cannot circulate forever | `tests/test_fh_stage_modes.py::test_s20_deferral_cannot_circulate_forever` | EXISTING (unchanged) |
| `failure-handling-FH3-S12` | `specs/failure-handling/spec.md` | Bounded retry and honest accounting | Own-budget withholding preserves the task's identity and age | `tests/test_tdm_stage_defer.py::test_fh3_s12_own_budget_withholding_preserves_identity_and_age` | GREEN |
| `run-observability-RO-S1` | `specs/run-observability/spec.md` | Every published metric surface has an operator rendering path | collected surfaces are rendered in detailed status output | `tests/test_tdm_render.py::test_ro_s1_collected_surfaces_are_rendered` | GREEN |
| `run-observability-RO-S2` | `specs/run-observability/spec.md` | Every published metric surface has an operator rendering path | an empty or absent surface renders nothing | `tests/test_tdm_render.py::test_ro_s2_empty_or_absent_surface_renders_nothing` | GREEN¹ |
| `run-observability-RO-S3` | `specs/run-observability/spec.md` | Every published metric surface has an operator rendering path | rendering is deterministic | `tests/test_tdm_render.py::test_ro_s3_rendering_is_deterministic` | GREEN |
| `run-observability-RO-S4` | `specs/run-observability/spec.md` | Every published metric surface has an operator rendering path | no published surface lacks a renderer | `tests/test_tdm_render.py::test_ro_s4_every_published_surface_has_a_renderer` | GREEN |
| `run-observability-RO-S5` | `specs/run-observability/spec.md` | Rendering is allowlisted, fail-open and free of credential material | an unexpected key does not reach operator output | `tests/test_tdm_render.py::test_ro_s5_unexpected_key_does_not_reach_output` | GREEN |
| `run-observability-RO-S6` | `specs/run-observability/spec.md` | Rendering is allowlisted, fail-open and free of credential material | rendered output carries no credential material | `tests/test_tdm_render.py::test_ro_s6_rendered_output_carries_no_credential_material` | GREEN |
| `run-observability-RO-S7` | `specs/run-observability/spec.md` | Rendering is allowlisted, fail-open and free of credential material | a malformed figure degrades instead of raising | `tests/test_tdm_render.py::test_ro_s7_malformed_figure_degrades_instead_of_raising` | GREEN |
| `run-observability-RO-S8` | `specs/run-observability/spec.md` | Rendering is allowlisted, fail-open and free of credential material | compact output is unchanged | `tests/test_tdm_render.py::test_ro_s8_compact_output_is_unchanged` | GREEN |
| `run-observability-RO-S9` | `specs/run-observability/spec.md` | Derived transport figures answer the acceptance questions | percentiles come from the band containing the rank | `tests/test_tdm_latency.py::test_ro_s9_percentiles_come_from_the_band_containing_the_rank` | GREEN |
| `run-observability-RO-S10` | `specs/run-observability/spec.md` | Derived transport figures answer the acceptance questions | resetting the surface resets latency with it | `tests/test_tdm_latency.py::test_ro_s10_reset_clears_the_latency_distribution` | GREEN |
| `run-observability-RO-S11` | `specs/run-observability/spec.md` | Derived transport figures answer the acceptance questions | mean bytes per file is computable from published output | `tests/test_tdm_render.py::test_ro_s11_mean_bytes_per_file_is_computable` | GREEN |

**RED baseline (measured 2026-09-28, before any production code was written).**

```
python -m pytest tests/test_tdm_starvation.py tests/test_tdm_latency.py \
                 tests/test_tdm_render.py tests/test_tdm_stage_defer.py -q
22 failed, 1 passed in 31.33s

python -m pytest tests/ -q
22 failed, 321 passed in 74.80s      # 320 pre-existing pins still GREEN + the 1 guard below
```

Observed failure reasons are the intended RED drivers, not harness faults:

| Driver | Tests |
|---|---|
| `TransientFetchError: gather fetch suppressed by local limiter` still raised at `search/client.py:1322` | S22, S23, S24, S25, S29, FH3-S5, FH3-S12 |
| `TypeError: fetch_gather_content() got an unexpected keyword argument 'defer_local_suppression'` | S26 |
| `AttributeError: 'GatherConfig' object has no attribute 'defer_local_suppression'` | S26b, FH3-S6 |
| `KeyError: 'latency_samples_raw'`; `AttributeError: module 'search.client' has no attribute '_GATHER_LATENCY_BANDS' / '_gather_latency_observe'` | S27, S28, RO-S9, RO-S10 |
| No `Gather:` / `Aggregation:` / `Refine:` line from `_format_pipeline_section`; `state.display` declares no `_RENDERED_METRIC_SURFACES` | RO-S1, RO-S3 … RO-S8, RO-S11 |

Stage-level RED evidence (FH3-S5 log): the withheld task burned its whole retry budget without
ever reaching the network — `requeued failed after 8.0s delay, task: AcquisitionTask(… attempts=4 …)`
followed by `[gather] discarded, max retries=[3] reached`. That is exactly the backlog
self-burning described in the proposal.

¹ **GREEN¹ (`RO-S2`) is a regression guard, expected GREEN already and required to stay green.**
Its claim ("an empty or absent surface renders nothing and does not raise") is vacuously true
while nothing renders at all; after implementation it binds the fail-open path — in particular
that a renderer must use `getattr(..., None)` rather than attribute access, since the pin feeds it
a status object whose pipeline has no `*_metrics` attributes whatsoever.

**Note 1 (`gather-transport-S21`).** The existing pin asserts the *counter surface*
(`get_gather_transport_stats()`), which is why the live gate could not read A1/A2 from a run: the
dict had no consumer. S21's "status reporting exposes" half is pinned by `RO-S1` and `RO-S11`;
the existing pin's **exact key set was extended** with the keys this change declares in the
canonical surface (`deferred_credentials`, `deferred_local_budget`, and the nine derived latency
keys), so the exact-set guard is preserved rather than weakened — every member is a declared
counter now (see `design.md` D14/D15). No assertion was removed or relaxed.

## Automated

### File: `tests/test_tdm_starvation.py`

Describe: local-budget withholding is a deferral, not a task fault (gather-transport S22–S26, S29; failure-handling FH3-S6)

RED at plan time: `deferred_local_budget` is not in `_GATHER_TRANSPORT_STAT_KEYS`, so the
withholding path still raises `TransientFetchError` and still reports a failure to the adaptive
budget (`search/client.py:1319-1322`); `GatherConfig.defer_local_suppression` does not exist. The
harness reuses the offline seam from `tests/test_gt_transport.py` (stubbed `_HTTP_SESSION`, real
`GitHubClient` over a real `RateLimiter`), so throttling attribution and classification run for
real while the suite stays offline.

- [x] `gather-transport-S22` — it("a withheld request does not decay the budget") <!-- WHEN the basket has no token and no request is sent THEN nothing is reported to the adaptive budget: rate unchanged, consecutive-failure tally unchanged -->
- [x] `gather-transport-S23` — it("sustained contention does not collapse the budget") <!-- WHEN 50 successive withholdings happen THEN the effective rate is still the configured base rate and the withheld count is published -->
- [x] `gather-transport-S24` — it("own-budget withholding defers with a bounded wait and a stage pause") <!-- WHEN a fetch is withheld THEN RateLimitDeferral with 0 < wait_s <= max_refusal_wait_s and stage_pause True, and no request reached the session -->
- [x] `gather-transport-S25` — it("own-budget withholding is counted apart from remote refusals") <!-- WHEN one remote rate-limit refusal and one withholding occur THEN deferred_rate_limit == 1 and deferred_local_budget == 1, neither leaking into the other -->
- [x] `gather-transport-S26` — it("legacy classification is a configuration flip") <!-- WHEN the flag is false THEN TransientFetchError and a failure report, byte-for-byte legacy -->
- [x] `gather-transport-S26` (supporting) — it("flag config surface") <!-- schema default true, loader parses an explicit false, validator rejects a non-bool -->
- [x] `gather-transport-S29` — it("a withheld fetch is not timed as a slow request") <!-- WHEN a fetch is withheld THEN latency_samples_<kind> does not advance -->
- [x] `failure-handling-FH3-S6` — it("the search boundary is unchanged by the flag") <!-- WHEN the search fetch is suppressed with the flag enabled THEN it still raises TransientFetchError (failure-empty), so the preserved S3 pin cannot be broken by this change -->

### File: `tests/test_tdm_latency.py`

Describe: bounded-memory latency percentiles per transport (gather-transport S27, S28; run-observability RO-S9, RO-S10)

RED at plan time: no latency instrumentation exists in `search/client.py`
(`grep -n "latency|elapsed|perf_counter|monotonic"` → empty) and no `latency_*` key is declared.
Tests drive the real fetch path through the offline session seam for S27/S29-style assertions and
the band observer directly for distribution arithmetic (documented as an internal seam, the way
`tests/test_gt_transport.py` uses `_github_client`).

- [x] `gather-transport-S27` — it("latency percentiles are published per transport") <!-- WHEN three real fetches complete over raw THEN latency_samples_raw == 3 and p50/p99 are published integers in ms, while html/rest stay at 0 -->
- [x] `gather-transport-S28` — it("latency summary costs bounded memory") <!-- WHEN 20 000 samples are recorded THEN the published key set is unchanged and the band structure holds a fixed number of counters -->
- [x] `run-observability-RO-S9` — it("percentiles come from the band containing the rank") <!-- WHEN a known distribution is recorded THEN p50/p99 equal the upper edge of the band containing that rank, never below the true percentile -->
- [x] `run-observability-RO-S10` — it("resetting the surface resets latency with it") <!-- WHEN the transport surface is reset THEN samples and percentiles return to zero -->

### File: `tests/test_tdm_render.py`

Describe: operator rendering of collected metric surfaces (run-observability RO-S1…RO-S8, RO-S11)

RED at plan time: `state/display.py` has no formatter for `gather_transport_metrics`,
`aggregation_metrics` or `refine_metrics` and declares no rendered-surface registry. Tests call
`StatusDisplayEngine._format_pipeline_section` (which **returns** its lines, so no log capture is
needed) and the individual formatters; only the compact-mode pin uses `caplog`.
There are currently no display tests in the suite, so this file is the first.

- [x] `run-observability-RO-S1` — it("collected surfaces are rendered in detailed status output") <!-- WHEN the three surfaces are populated THEN one line each appears with their principal figures -->
- [x] `run-observability-RO-S2` — it("an empty or absent surface renders nothing") <!-- WHEN a surface is empty or the attribute is missing THEN no line, no placeholder, no exception -->
- [x] `run-observability-RO-S3` — it("rendering is deterministic") <!-- WHEN rendered twice from the same state THEN both outputs are identical -->
- [x] `run-observability-RO-S4` — it("every published surface has a renderer") <!-- WHEN PipelineStatus's *_metrics fields are enumerated THEN each is in the declared rendered-surface registry -->
- [x] `run-observability-RO-S5` — it("an unexpected key does not reach operator output") <!-- WHEN a producer adds an undeclared key THEN the rendered line is unchanged -->
- [x] `run-observability-RO-S6` — it("rendered output carries no credential material") <!-- WHEN a surface holds a token-like value under any key THEN no part of it appears in the rendered lines -->
- [x] `run-observability-RO-S7` — it("a malformed figure degrades instead of raising") <!-- WHEN a declared figure is None/a string/NaN THEN the line still renders and nothing raises -->
- [x] `run-observability-RO-S8` — it("compact output is unchanged") <!-- WHEN rendering in compact mode THEN none of the new labels appear -->
- [x] `run-observability-RO-S11` — it("mean bytes per file is computable from published output") <!-- WHEN a transport shows bytes and request counts THEN their ratio is the mean bytes per file, parseable from the line itself -->

### File: `tests/test_tdm_stage_defer.py`

Describe: stage-level accounting of an own-budget withholding (failure-handling FH3-S5, FH3-S12)

RED at plan time: a withheld gather fetch currently reaches the worker loop as a
`TransientFetchError`, so it increments `failure_empties_detected`/`tasks_requeued` and burns an
attempt. The harness follows `tests/test_fh_gather_fidelity.py::test_s21_deferred_gather_writes_no_registry_outcome`
(real workspace under `tmp_path`, real acquisition task, registry inspected afterwards).

- [x] `failure-handling-FH3-S5` — it("own-budget withholding is a deferral, not a failure-empty") <!-- WHEN the gather fetch is withheld THEN tasks_deferred increments, failure_empties_detected and total_errors do not, and no visit_status/link_coverage row is written -->
- [x] `failure-handling-FH3-S12` — it("own-budget withholding preserves the task's identity and age") <!-- WHEN the same task is withheld repeatedly THEN attempts, created_at and dedup identity are unchanged, no error/requeue/processed counter advances, and the age gate remains the only ceiling -->

## Manual

| Scenario ID | Why it cannot be automated | How it is verified |
|-------------|---------------------------|--------------------|
| `gather-transport-S21` (live half), `S27` (live half) | Acceptance rows **A1** (mean bytes per file ≤ 20 KiB) and **A2** (p50 in the captured 240–340 ms band, p99 < 1 s) require a production-shaped soak against the real `raw.githubusercontent.com`; the offline suite can pin the arithmetic but not the wire. | `runbook.md` live gate: a 600 s soak with the basket deliberately under-provisioned relative to `pipeline.threads.gather`, reading A1/A2 from the rendered `Gather:` line. Provenance for the expected band: probe 2026-09-25, `raw.githubusercontent.com`, anonymous, 80 distinct URLs, p50 338 ms / p99 390 ms / 17.1 KiB per file; R5.1 limited gate 12.6 KiB per file, p50 239 ms. |
| `gather-transport-S23` (live half) | The claim "sustained contention does not collapse the budget" is pinned offline against a synthetic basket; the production-shaped confirmation needs the real 8-worker contention that produced the measured 1 708 suppressions. | `runbook.md`: compare the pre-fix soak figures (1 708 suppressions, 876 failure-empties, 869 requeues) against the post-fix soak (`deferred_local_budget` > 0, failure-empties from suppressions = 0, `tasks_dropped_max_retries` unchanged) and the rollback drill with the flag off. |

## Notes on obsolete tests

No existing test becomes obsolete, and none is deleted or weakened:

- `tests/test_fh_client_taxonomy.py::test_s3_limiter_suppression_is_failure_empty` pins the
  **search** boundary, which design D11 deliberately leaves untouched; `FH3-S6` adds a pin that
  the new flag cannot change it.
- `tests/test_gt_transport.py::test_s9_failure_is_reported_to_the_adaptive_budget` pins that
  failures of **issued** requests are reported; this change removes reporting only for the
  withheld-request branch, so the pin stays valid and is the guard against over-reach.
- `tests/test_fh_stage_modes.py::test_s9_starved_check_requeues_instead_of_dropping` pins the
  **check** stage's provider-basket starvation, which design D10 deliberately leaves untouched.
- Verified by search: no test references `SearchStage._apply_rate_limit` (the dead method this
  change removes) and no test asserts the `suppressed by local limiter` message
  (`grep -rn "suppressed\|_apply_rate_limit" tests/*.py` → only the search-boundary docstring and
  two unrelated fake-client `_report` stubs in aggregation tests).
