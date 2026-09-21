# Test Plan

Derived mechanically from `specs/refine-fanout-governor/spec.md`. One scenario = exactly one canonical automated test (a few integration tests serve several observable outcomes of a single governed run; supporting unit tests are marked aux). Scenario IDs are stable: never renumber, append only.

Runner: `python3 -m pytest` (project convention: flat `tests/test_*.py`, SearchStage harness style mirrors `tests/test_gather_skip_modes.py` / `tests/test_fh_stage_modes.py`). Observed statuses recorded from the RED baseline run of 2026-09-21 (pre-implementation).

**RED-by-import note:** `search/refine_governor.py` and `config.schemas.RefineGovernorConfig` do not exist yet; both files fail collection with ImportError — this IS the expected RED state. Additional RED drivers encoded in the integration file: `StageResources(refine_governor=...)` kwarg, `SearchTask(refine_depth=...)` kwarg, `PipelineStatus().refine_metrics` field.

**Staleness audit (per operator instruction):** the full pre-existing suite was run alongside the RED baseline — **172 passed**, zero stale tests found. Nothing needed deletion or correction: no existing test pins unbounded refine behavior (the only `partitions` mention in `tests/test_early_stop.py:230` is an early-stop test name unrelated to refinement), and search-aggregation golden vectors / query-refinement pins touch wire forms, not fan-out volume.

**Live-data note:** real-credential calibration is permitted via gitignored `.secrets` / `config.yaml` (operator authorization 2026-09-21). Live probes already captured offline+online evidence for the proposal (generator: 46 656 children in 0.13 s at partitions=46 007; api.github.com/search/code Bearer 2026-09-21: `"sk-000"`→75 136, `"sk-zzz"`→934, `"sk-i00"`→24). Implementation-phase tasks re-capture them as provenance-headed fixtures under `tests/fixtures/`; unit/integration tests here stay network-free by design (deterministic, CI-safe).

## Traceability

| Scenario ID | Spec | Requirement | Scenario | Test file | Status |
|-------------|------|-------------|----------|-----------|--------|
| `refine-fanout-governor-S1` | `specs/refine-fanout-governor/spec.md` | Depth-bounded refinement | Children inherit incremented depth | `tests/test_rfg_stage_integration.py::test_s1_root_refines_capped_children_with_depth_and_attribution` | GREEN |
| `refine-fanout-governor-S2` | 〃 | Depth-bounded refinement | At depth cap pagination replaces refinement | `tests/test_rfg_stage_integration.py::test_s2_depth_cap_paginates_instead_of_refining` | GREEN |
| `refine-fanout-governor-S3` | 〃 | Depth-bounded refinement | Legacy persisted tasks deserialize as roots | `tests/test_rfg_stage_integration.py::test_s3_depth_serializes_and_legacy_defaults_to_zero` | GREEN |
| `refine-fanout-governor-S4` | 〃 | Partition sanity clamp | Astronomical totals yield at most the cap | `tests/test_rfg_stage_integration.py::test_s1_root_refines_capped_children_with_depth_and_attribution` | GREEN |
| `refine-fanout-governor-S5` | 〃 | Partition sanity clamp | Generator receives the clamped partition count | `tests/test_rfg_stage_integration.py::test_s1_root_refines_capped_children_with_depth_and_attribution` (engine spy) | GREEN |
| `refine-fanout-governor-S6` | 〃 | Partition sanity clamp | Identical inputs admit identical children | `tests/test_rfg_governor.py::test_identical_inputs_admit_identical_children` | GREEN |
| `refine-fanout-governor-S7` | 〃 | Per-run budget | Budget exhaustion refuses loudly and deterministically | `tests/test_rfg_governor.py::test_budget_exhaustion_refuses_loudly` | GREEN |
| `refine-fanout-governor-S8` | 〃 | Per-run budget | Roots and page tasks never consume the budget | `tests/test_rfg_stage_integration.py::test_s8_pagination_runs_with_exhausted_budget` | GREEN |
| `refine-fanout-governor-S9` | 〃 | Per-run budget | Fresh process resets the budget | `tests/test_rfg_governor.py::test_fresh_instance_resets_budget` | GREEN |
| `refine-fanout-governor-S10` | 〃 | Tri-mode rollout flag | Off reproduces pre-change behavior | `tests/test_rfg_stage_integration.py::test_s10_off_matches_unclamped_generator_output` (aux: `test_rfg_governor.py::test_off_passthrough_preserves_input_order`) | GREEN |
| `refine-fanout-governor-S11` | 〃 | Tri-mode rollout flag | Shadow measures without enforcing | `tests/test_rfg_stage_integration.py::test_s11_shadow_enqueues_everything_but_counts` (aux: `test_rfg_governor.py::test_shadow_counts_without_enforcing`) | GREEN |
| `refine-fanout-governor-S12` | 〃 | Tri-mode rollout flag | On enforces | `tests/test_rfg_governor.py::test_on_select_children_caps_and_sorts` | GREEN |
| `refine-fanout-governor-S13` | 〃 | Tri-mode rollout flag | Rollback is a config flip | `tests/test_rfg_governor.py::test_mode_flip_is_the_only_difference` | GREEN |
| `refine-fanout-governor-S14` | 〃 | Coverage observability | Metrics visible after governed refinement | `tests/test_rfg_stage_integration.py::test_s14_metrics_reach_status_surface` | GREEN |
| `refine-fanout-governor-S15` | 〃 | Coverage observability | Coverage estimate is honest arithmetic | `tests/test_rfg_governor.py::test_coverage_estimate_is_honest_arithmetic` | GREEN |
| `refine-fanout-governor-S16` | 〃 | Existing protections preserved | Children keep parent attribution | `tests/test_rfg_stage_integration.py::test_s1_root_refines_capped_children_with_depth_and_attribution` | GREEN |
| `refine-fanout-governor-S17` | 〃 | Existing protections preserved | Strict failure retry works on governed children | `tests/test_rfg_stage_integration.py::test_s17_strict_failure_propagates_for_governed_child` | GREEN |
| `refine-fanout-governor-S18` | 〃 | Existing protections preserved | Early-stop still observes executed children | `tests/test_rfg_stage_integration.py::test_s18_early_stop_observes_governed_pages` | GREEN |

Aux (no own scenario): `test_rfg_governor.py::test_may_refine_depth_gate` (unit half of S1/S2), `test_clamp_partitions_math` (supports S4/S12), `test_shadow_budget_drain_is_measured` (strengthens S11: shadow's would-be budget trajectory must match `on` across batches), `test_off_leaves_stage_filters_authoritative` (strengthens S10: `off` must not pre-filter, or the stage's pre-existing empty/self WARNINGs disappear and rollback stops being log-equivalent). Wiring aux lives in `tests/test_rfg_pipeline_wiring.py` (see below).

## Automated

### File: tests/test_rfg_governor.py

Describe: RefineGovernor unit contract (clamp math, determinism, budget, modes, coverage arithmetic)

Module under test `search/refine_governor.py` does not exist yet — the import failure is the expected RED state. The file docstring pins the driven API contract (`may_refine`, `clamp_partitions`, `select_children`, `record_depth_cap_pagination`, `metrics`).

- [x] `refine-fanout-governor-S6` — it("Identical inputs admit identical children") <!-- WHEN same parent refined twice with shuffled generator order THEN identical admitted lists -->
- [x] `refine-fanout-governor-S7` — it("Budget exhaustion refuses loudly and deterministically") <!-- WHEN batch exceeds remaining budget THEN sorted survivors admitted, refusals warned+counted -->
- [x] `refine-fanout-governor-S9` — it("Fresh process resets the budget") <!-- WHEN new governor instance THEN budget_remaining == configured max -->
- [x] `refine-fanout-governor-S12` — it("On enforces") <!-- WHEN mode=on THEN cap+sort applied to admitted children -->
- [x] `refine-fanout-governor-S13` — it("Rollback is a config flip") <!-- WHEN mode on→off with same inputs THEN enforcement ceases, no other delta -->
- [x] `refine-fanout-governor-S15` — it("Coverage estimate is honest arithmetic") <!-- THEN estimate == min(1, A×L/max(T,1)) in metrics -->
- [x] (aux) — it("Depth gate unit") <!-- may_refine: on refuses at cap + counts; shadow counts would-be; off inert -->
- [x] (aux) — it("Clamp math") <!-- clamp_partitions: min(raw, cap); off = identity -->
- [x] (aux S10) — it("Off passthrough preserves input order") <!-- off returns generator order untouched, counters inert -->
- [x] (aux S11) — it("Shadow counts without enforcing") <!-- shadow returns everything, would-be truncation equals on-decisions -->
- [x] (aux S11) — it("Shadow budget drain is measured") <!-- shadow's would-be refused_budget/budget_remaining/parents_refined match on across batches -->
- [x] (aux S10) — it("Off leaves stage filters authoritative") <!-- off returns empty/self candidates verbatim so the stage's pre-existing WARNINGs still fire; shadow/on filter them so counters describe real enqueues -->

### File: tests/test_rfg_stage_integration.py

Describe: SearchStage integration (real RefineEngine on the incident seed, mocked transport totals, injected governor)

RED drivers: `search.refine_governor`, `RefineGovernorConfig`, `StageResources.refine_governor`, `SearchTask.refine_depth`, `PipelineStatus.refine_metrics` — all absent pre-implementation.

- [x] `refine-fanout-governor-S1` — it("Root refines capped children with depth and attribution") <!-- WHEN root total=46M under on/cap3 THEN ≤3 children, refine_depth==1, provider/patterns/use_api inherited -->
- [x] `refine-fanout-governor-S4` — same test: children ≤ max_partitions_per_refine
- [x] `refine-fanout-governor-S5` — same test: engine spy proves generate_queries received the clamped partitions
- [x] `refine-fanout-governor-S16` — same test: child wire queries are generator outputs verbatim (sorted/truncated, never rewritten)
- [x] `refine-fanout-governor-S2` — it("Depth cap paginates instead of refining") <!-- WHEN depth-1 child total=2M at cap THEN zero grandchildren, pages 2..10 emitted, counter ticks -->
- [x] `refine-fanout-governor-S3` — it("Depth serializes and legacy defaults to zero") <!-- round-trip preserves 2; dict without the key loads as 0 -->
- [x] `refine-fanout-governor-S8` — it("Pagination runs with exhausted budget") <!-- budget=0: page tasks emitted, refused_budget==0; root children refused loudly -->
- [x] `refine-fanout-governor-S10` — it("Off matches unclamped generator output") <!-- child set == direct generate_queries(partitions=5) filtered; counters inert -->
- [x] `refine-fanout-governor-S11` — it("Shadow enqueues everything but counts") <!-- full child set enqueued AND truncated_to_cap ≥ 1 measured -->
- [x] `refine-fanout-governor-S14` — it("Metrics reach status surface") <!-- governor.metrics complete; PipelineStatus().refine_metrics exists -->
- [x] `refine-fanout-governor-S17` — it("Strict failure propagates for governed child") <!-- TransientFetchError escapes process_task unchanged -->
- [x] `refine-fanout-governor-S18` — it("Early-stop observes governed pages") <!-- fake engine records (provider, raw query, page) for the executed child -->

### File: tests/test_rfg_pipeline_wiring.py

Describe: `Pipeline` wiring (config → governor construction, D1 fail-safe fallback, metrics on the status surface). Added by the verification round: the stage-level file injects a governor directly, so nothing pinned the manager half of S14 or design D1. Methods are exercised unbound against a lightweight stand-in (they touch only `config.refine_governor` and `self.refine_governor`), keeping the file free of storage/worker/network dependencies.

- [x] (aux S14) — it("Configured values reach the governor") <!-- mode/depth/partitions/budget from RefineGovernorConfig land on the instance -->
- [x] (aux S14) — it("Absent config section gets governed defaults") <!-- Config() → mode=on, 2/128/10000 -->
- [x] (aux D1) — it("Construction failure falls back to safe defaults") <!-- config-derived construction raises → built-in defaults in `on`, ERROR logged; an operator-requested `off` must NOT survive a wiring failure -->
- [x] (aux S14) — it("Metrics reach the status surface") <!-- all ten D9 keys present; counters live -->
- [x] (aux S14) — it("Metrics without a governor are an empty dict") <!-- pre-wiring/legacy managers report {} instead of raising -->
- [x] (aux requirement 5) — it("Zero budget is rejected at config level") <!-- budget 0, partitions 0 and unknown mode all raise ValueError -->

## Manual

- Live calibration & governed-run verification (executed within tasks.md groups 1 and 6, real GitHub via env credentials from gitignored `.secrets`, guardrails of plan_agr.md §3.4): (a) capture child-total distribution fixtures with provenance headers (endpoint, date, auth mode) grounding the default caps; (b) short real harvester run on the incident seed under `mode: on` — search queue stays bounded, RSS flat, `refine_metrics` refusals/coverage visible, harvest non-zero; (c) optional operator cycle under `shadow` to measure would-be coverage price before relying on `on`. Cannot be automated in CI: real credentials, wall-clock, third-party rate limits.
- Promotion/rollback decision (`on` ↔ `shadow` ↔ `off`) remains an operator config choice — documented in README section added by tasks group 7.
