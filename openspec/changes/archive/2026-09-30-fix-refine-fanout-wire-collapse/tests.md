# Test Plan

Derived mechanically from `specs/refine-fanout-governor/spec.md`. The main spec
carries 18 scenarios (`refine-fanout-governor-S1`…`S18`, already pinned by
`tests/test_rfg_governor.py`, `tests/test_rfg_stage_integration.py`,
`tests/test_rfg_pipeline_wiring.py`); this delta appends **S19–S33** and reuses
no ID. Existing scenarios keep their IDs and their tests: the fixtures in
`tests/test_rfg_governor.py` use five children whose wire queries are pairwise
distinct (`"sk-a"`…`"sk-e"`, parent `"sk-x"`), so the guard withholds nothing
there and **no existing test becomes obsolete** — verified by running the suite,
not assumed. One existing scenario's THEN clause is amended in place
(S16 "Children keep parent attribution": `sorted/truncated` →
`sorted/truncated/filtered`); its test asserts attribution and verbatim wire
queries, both unchanged, so it needs no edit.

## Traceability

| Scenario ID | Spec | Requirement | Scenario | Test file | Status |
|-------------|------|-------------|----------|-----------|--------|
| `refine-fanout-governor-S19` | `specs/refine-fanout-governor/spec.md` | Wire-indistinguishable children are withheld | A child collapsing onto its parent's wire query is withheld | `tests/test_rfg_wire_collapse.py` | GREEN |
| `refine-fanout-governor-S20` | `specs/refine-fanout-governor/spec.md` | Wire-indistinguishable children are withheld | Siblings sharing one wire query admit exactly one survivor | `tests/test_rfg_wire_collapse.py` | GREEN |
| `refine-fanout-governor-S21` | `specs/refine-fanout-governor/spec.md` | Wire-indistinguishable children are withheld | Enforced admission admits one child per distinct wire query | `tests/test_rfg_wire_collapse.py` | GREEN |
| `refine-fanout-governor-S22` | `specs/refine-fanout-governor/spec.md` | Wire-indistinguishable children are withheld | Withheld children consume neither cap nor budget | `tests/test_rfg_wire_collapse.py` | GREEN |
| `refine-fanout-governor-S23` | `specs/refine-fanout-governor/spec.md` | Wire-indistinguishable children are withheld | The survivor is deterministic under generator reordering | `tests/test_rfg_wire_collapse.py` | GREEN |
| `refine-fanout-governor-S24` | `specs/refine-fanout-governor/spec.md` | Wire-indistinguishable children are withheld | A child whose regex yields no fixed literal is not spuriously collapsed | `tests/test_rfg_wire_collapse.py` | GREEN |
| `refine-fanout-governor-S25` | `specs/refine-fanout-governor/spec.md` | Wire-indistinguishable children are withheld | The web transport withholds nothing | `tests/test_rfg_wire_collapse.py` | GREEN |
| `refine-fanout-governor-S26` | `specs/refine-fanout-governor/spec.md` | Wire-indistinguishable children are withheld | Withholding is reported once per parent | `tests/test_rfg_wire_collapse.py` | GREEN |
| `refine-fanout-governor-S27` | `specs/refine-fanout-governor/spec.md` | Wire-indistinguishable children are withheld | Shadow mode counts and logs the would-be withholding without enforcing | `tests/test_rfg_wire_collapse.py` | GREEN |
| `refine-fanout-governor-S28` | `specs/refine-fanout-governor/spec.md` | Wire-indistinguishable children are withheld | The guard is a single boolean rollback independent of mode | `tests/test_rfg_wire_collapse.py` | GREEN |
| `refine-fanout-governor-S29` | `specs/refine-fanout-governor/spec.md` | Wire-indistinguishable children are withheld | A live enforced run collapses fan-out without losing distinguishable work | *(live gate)* | MANUAL |
| `refine-fanout-governor-S30` | `specs/refine-fanout-governor/spec.md` | Partition sanity clamp with deterministic truncation | Truncation counts cap surplus only | `tests/test_rfg_wire_collapse.py` | GREEN |
| `refine-fanout-governor-S31` | `specs/refine-fanout-governor/spec.md` | Tri-mode rollout flag | The guard boolean is validated like the mode | `tests/test_rfg_pipeline_wiring.py` | GREEN |
| `refine-fanout-governor-S32` | `specs/refine-fanout-governor/spec.md` | Coverage observability | The three refusal reasons are separable in published output | `tests/test_tdm_render.py` | GREEN |
| `refine-fanout-governor-S33` | `specs/refine-fanout-governor/spec.md` | Coverage observability | Distinguishable work is published next to volume | `tests/test_tdm_render.py` | GREEN |

## Automated

### File: `tests/test_rfg_wire_collapse.py`

Describe: `RefineGovernor wire-collapse guard`

New file. The module under test (`search.refine_governor`) exists, but the guard,
the `drop_wire_indistinguishable` constructor keyword and the two new metric keys
do not — so the expected RED state is a mix of `TypeError` (unexpected keyword),
`KeyError` (absent metric key) and assertion failures (children not withheld).
Fixtures use the shipped `search.querykey.wire_query` and the real
`RefineEngine.clean_regex`; no stubbing of the wire function, because a stub could
be written to make the web-inertness property (S25) hold trivially.

- [ ] `refine-fanout-governor-S19` — it("A child collapsing onto its parent's wire query is withheld") <!-- WHEN every candidate of a parent shares the parent's wire query THEN admitted == [], refused_wire_collapse == len(candidates), budget_remaining and truncated_to_cap unchanged -->
- [ ] `refine-fanout-governor-S20` — it("Siblings sharing one wire query admit exactly one survivor") <!-- WHEN 3 candidates collapse onto 2 distinct wires, neither equal to the parent's THEN exactly 2 admitted, one per wire, refused_wire_collapse == 1, every admitted query is a verbatim candidate -->
- [ ] `refine-fanout-governor-S21` — it("Enforced admission admits one child per distinct wire query") <!-- WHEN several parents refine under enforcement THEN metrics["children_admitted"] == metrics["distinct_wire_admitted"] -->
- [ ] `refine-fanout-governor-S22` — it("Withheld children consume neither cap nor budget") <!-- WHEN a batch loses candidates to collapse, to the cap and to the budget THEN generated == collapsed + truncated + refused_budget + admitted and budget_remaining drops only by admitted -->
- [ ] `refine-fanout-governor-S23` — it("The survivor is deterministic under generator reordering") <!-- WHEN the same batch is admitted twice with shuffled candidate order THEN both admitted lists are equal element-wise, including which raw query survived each wire group -->
- [ ] `refine-fanout-governor-S24` — it("A child whose regex yields no fixed literal is not spuriously collapsed") <!-- WHEN parent and children all reduce to no fixed literal so the wire falls back to raw THEN all distinct children admitted and refused_wire_collapse == 0 -->
- [ ] `refine-fanout-governor-S25` — it("The web transport withholds nothing") <!-- WHEN use_api=False and candidates are distinct raw strings THEN every candidate distinct from the parent is admitted and refused_wire_collapse == 0 -->
- [ ] `refine-fanout-governor-S26` — it("Withholding is reported once per parent") <!-- WHEN one parent has 40 candidates all collapsing onto one wire THEN exactly one guard log line for that parent, naming provider, parent query, generated and withheld counts, and the line count does not grow with the withheld count -->
- [ ] `refine-fanout-governor-S27` — it("Shadow mode counts and logs the would-be withholding without enforcing") <!-- WHEN mode=shadow on a collapsing batch THEN all candidates returned, refused_wire_collapse counted as on would, one line marked shadow -->
- [ ] `refine-fanout-governor-S28` — it("The guard is a single boolean rollback independent of mode") <!-- WHEN drop_wire_indistinguishable=False in both shadow and on THEN admission equals the pre-guard admission, no guard line is emitted, and both new figures are 0 -->
- [ ] `refine-fanout-governor-S30` — it("Truncation counts cap surplus only") <!-- WHEN a batch loses candidates to both collapse and the cap THEN truncated_to_cap counts only post-guard surplus and no candidate is counted twice -->

### File: `tests/test_rfg_pipeline_wiring.py`

Describe: `RefineGovernor configuration wiring` (existing file — appended, not duplicated)

- [ ] `refine-fanout-governor-S31` — it("The guard boolean is validated like the mode") <!-- WHEN RefineGovernorConfig carries a non-boolean drop_wire_indistinguishable THEN construction/validation raises naming the key, in both mirrored validation sites -->

### File: `tests/test_tdm_render.py`

Describe: `run-observability rendering` (existing file — appended, not duplicated)

- [ ] `refine-fanout-governor-S32` — it("The three refusal reasons are separable in published output") <!-- WHEN refine_metrics carries non-zero depth, budget and wire-collapse refusals THEN the rendered Refine: line shows three distinguishable counts reconciling with the input -->
- [ ] `refine-fanout-governor-S33` — it("Distinguishable work is published next to volume") <!-- WHEN refine_metrics carries children_admitted and distinct_wire_admitted THEN the rendered line names both, so their ratio is readable without querying the queue -->

## Manual

- `refine-fanout-governor-S29` — requires a live run against the code-hosting service on the shipped tree with the operator's credential pool: it asserts queue-row and harvested-byte reduction, non-lower distinct-wire and unique-link counts, unchanged gather/check throughput and zero capacity refusals. Cannot be automated offline without asserting on synthesized traffic, and capacity-refusal evidence may only come from organic windows (`AGENTS.md` §3). Executed as a two-arm config A/B on the flag, compared at a defined boundary (final status block after graceful stop) and by **unique** harvested entities rather than record counts (`plan_races.md` §8 item 8, lessons И6/И7); run roots under `/var/tmp`, credential-bearing config copies destroyed on arm exit.

---

## Obsolete tests

None. Checked explicitly, because the guard changes admission output and the
metric surface:

- `tests/test_rfg_governor.py` — its `QUERIES` fixture yields five pairwise
  distinct wire queries and a parent wire distinct from all of them, so the guard
  withholds nothing; `ADMITTED_API`/`ADMITTED_WEB` are computed by sorting on
  `fingerprint(q, use_api)`, and since all fingerprints in the fixture are
  distinct, adding `raw` as a tiebreaker (design D4) cannot reorder them. All 12
  tests remain valid unchanged.
- `tests/test_rfg_stage_integration.py`, `tests/test_rfg_pipeline_wiring.py` —
  assert per-key metric values and stage-level behaviour, not an exact key set, so
  two additional keys do not invalidate them. **Amendment applied during GREEN:**
  `test_rfg_stage_integration.py`'s `_governor` helper now passes
  `drop_wire_indistinguishable=False`. The incident seed it uses
  (`/sk-[a-zA-Z0-9]{32}/`) generates children whose wire query equals the
  parent's (`"sk-"`), so the default-on guard withheld them and masked the
  depth/cap/budget behaviour S1/S8/S11 pin. The guard is covered by
  `tests/test_rfg_wire_collapse.py`; no test function was deleted or weakened.
- `tests/test_tdm_render.py` — RO-S1 asserts **fragments** (`mode=on`, `gen=144`,
  `depth=0`, `trunc=16`, `cov[`) rather than whole-line equality, so extending the
  line does not break it; its `REFINE` fixture is left untouched and S32/S33 derive
  their own metrics from it, which also keeps RO-S1 pinned against a surface that
  predates the two new keys.

If running the suite contradicts any of the above, the contradicted test is
deleted together with its fixture when the behaviour it pinned is genuinely gone,
or amended when the behaviour moved — never weakened into a tautology.

---

## RED baseline (observed 2026-09-29, `python -m pytest`)

Command: `python -m pytest tests/test_rfg_wire_collapse.py tests/test_rfg_pipeline_wiring.py tests/test_tdm_render.py tests/test_rfg_governor.py tests/test_rfg_stage_integration.py -q`

Result: **15 failed, 35 passed** in 0.72 s. Repository `logs/` empty afterwards.

| Failing test | Scenario | Observed RED reason |
|---|---|---|
| `test_s19_child_collapsing_onto_parent_wire_is_withheld` | S19 | `AssertionError: assert ['/sk-a[0-9][…]b[a-z]{28}/'] == []` — children not withheld |
| `test_s20_siblings_sharing_one_wire_admit_exactly_one_survivor` | S20 | `AssertionError: assert 3 == 2` — both same-wire siblings admitted |
| `test_s21_enforced_admission_admits_one_child_per_distinct_wire` | S21 | `KeyError: 'distinct_wire_admitted'` |
| `test_s22_withheld_children_consume_neither_cap_nor_budget` | S22 | `KeyError: 'refused_wire_collapse'` |
| `test_s23_survivor_is_deterministic_under_generator_reordering` | S23 | `AssertionError` — the eight shuffled runs admit **different** survivors for the same wire group, empirically confirming the latent nondeterminism design D4 predicted |
| `test_s24_child_without_fixed_literal_is_not_spuriously_collapsed` | S24 | `KeyError: 'refused_wire_collapse'` |
| `test_s25_web_transport_withholds_nothing` | S25 | `AssertionError: assert 5 == 6` — the `SELF_COLLAPSING`/`SIBLINGS` fixtures shared one raw string; corrected during GREEN so the precondition ("candidates are distinct raw strings") is true |
| `test_s26_withholding_is_reported_once_per_parent` | S26 | `AssertionError: assert ['/sk-a[0][a-…z]{28}/', …] == []` |
| `test_s27_shadow_counts_without_enforcing` | S27 | `KeyError: 'refused_wire_collapse'` |
| `test_s28_guard_flag_is_a_single_boolean_rollback` | S28 | `TypeError: RefineGovernor.__init__() got an unexpected keyword argument 'drop_wire_indistinguishable'` |
| `test_s30_truncation_counts_cap_surplus_only` | S30 | `KeyError: 'refused_wire_collapse'` |
| `test_s31_guard_boolean_is_validated_like_the_mode` | S31 | `TypeError: RefineGovernorConfig.__init__() got an unexpected keyword argument 'drop_wire_indistinguishable'` |
| `test_rfg_s32_three_refusal_reasons_are_separable` | S32 | `AssertionError: {'depth': '3', 'budget': '5'}` — only two reasons render |
| `test_rfg_s33_distinguishable_work_is_published_next_to_volume` | S33 | `AssertionError` — rendered line is `Refine: mode=on gen=144 adm=82171 refused[depth=0, budget=0] trunc=16 cov[…]`, no `uniq_wires=` |
| `test_metrics_reach_the_status_surface` *(pre-existing)* | — | `KeyError` on the two keys appended to `D9_KEYS`; this is an existing pin **strengthened** by this change, not an obsolete one |

Every failure is a missing behaviour, a missing keyword or a missing metric key —
no fixture or import errors. The 35 passes include all 12 pre-existing
`tests/test_rfg_governor.py` tests and the whole stage-integration file, which is
the empirical half of the "Obsolete tests: none" claim above.
