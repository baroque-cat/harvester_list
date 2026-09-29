## Context

See `proposal.md` — Why for the measured motivation. What shapes the approach:

- `RefineGovernor` is defined as sitting *strictly between* "the engine produced candidate children" and "children enter the search queue", and it **never rewrites wire queries** (`search/refine_governor.py:3-30`). It already holds `use_api` (`select_children` trailing keyword, design D5 of `add-refine-fanout-governor`), the tri-mode contract, the partition cap, the run budget and the deterministic admission order.
- `search/querykey.py` is the single source of truth for "what goes on the wire" (design D2 of `add-search-aggregation`): API → `RefineEngine.clean_regex` output when non-empty, otherwise raw; web → raw verbatim. `stage/definition.py:288-296` (`_preprocess_query`) delegates to the same function, so the request and the aggregation cache key are already identical by construction.
- The engine's candidate order is set-based and cross-process salted; determinism of admission today comes **only** from sorting on `fingerprint(q, use_api)` = `sha256("<api|web>|<wire_query>")`.
- `refine_metrics` is a published surface with a renderer and an explicit per-key allowlist (`state/display.py:657-674`); RO-S5 pins that an unexpected key never reaches operator output, RO-S4's registry `_RENDERED_METRIC_SURFACES` is keyed on `PipelineStatus` **surfaces**, not on individual keys.
- Config validation already fails loudly on an invalid `refine_governor.mode` or non-positive caps.

## Goals / Non-Goals

**Goals:**
- Make "the transport cannot tell this child from one already admitted" a first-class, counted, deterministic withholding reason.
- Spend the partition cap and the run budget only on distinguishable work.
- Keep the guard's own footprint observable without adding log volume proportional to the fan-out.
- Rollback by a single boolean, orthogonal to `mode`.

**Non-Goals (design-level boundaries beyond the proposal's):**
- No change to how partitions are *generated* (`RefineEngine.generate_queries`, `_divide`, `_divide_with_language`) — the guard selects among outputs, it does not reshape them.
- No change to `_log_budget_refusals`; its per-child loudness is pinned by an existing scenario (see D7).
- No change to `coverage_estimate`'s formula or to the `total_count` denominator (R6), even though the guard lowers the numerator.
- No change to `stage/definition.py`'s refine branch beyond what the call site already provides.

## Decisions

**D1 — The guard lives in `RefineGovernor.select_children`, not in the engine and not in the stage.**
The governor is the only place that is (a) after generation, (b) before enqueue, (c) mode-aware, and (d) already in possession of `use_api`. *Rejected:* teaching `RefineEngine` about wire collapse — the engine is transport-agnostic, has no mode, and `off`'s byte-equivalence guarantee would be lost. *Rejected:* filtering in `stage/definition.py` after `select_children` returns — the budget would already have been consumed by children that are then discarded, which forfeits the entire coverage gain that the A/B measured.

**D2 — Compare on `wire_query(q, use_api)`, never on `clean_regex` directly and never on the fingerprint hash.**
Using the same function the cache key and the actual request use is what makes disagreement impossible by construction. Calling `clean_regex` directly would bypass the web fallback and the empty-result fallback, i.e. exactly the two paths that make the guard inert when it must be. Comparing `fingerprint()` values is equivalent but opaque in logs and hides that the equivalence being asserted is *on the wire value*.

**D3 — Full wire-dedupe, not merely parent-equality.**
Two children can share a wire without either equalling the parent's (parent `/sk-[a-z]{2}[0-9]{30}/` → `"sk-"`; children `/sk-a[0-9]X/` and `/sk-a[0-9]Y/` → both `"sk-a"`). A parent-only guard leaves that collapse in place. The chosen post-condition is expressible and checkable from the status line: **`children_admitted == distinct_wire_admitted`** whenever the guard is enforcing. *Rejected:* parent-only guard — cheaper, but incomplete, and it would not yield a post-condition an operator can read.

**D4 — Deterministic survivor: sort key becomes `(fingerprint, raw)`; the lexicographically smallest raw survives each wire group.**
Today same-wire siblings tie on fingerprint and Python's stable sort preserves the engine's salted order, so *which* raw string survives is already nondeterministic across processes even though the surviving wire set is not. That is latent, not harmless: `dedup_id` derives from the task's query, so a restart can recover a different identity for the same wire. Adding `raw` as a tiebreaker fixes this and **strengthens** the existing determinism guarantee rather than relaxing it. It does not change which *wires* survive cap truncation, because ties are exactly the same-wire groups that get deduplicated anyway. *Rejected:* keep generator order (nondeterministic); keep longest/shortest raw (no semantic meaning, length ties possible).

**D5 — Order of operations: empty/self filter → count `generated` → wire guard → sort → cap → budget.**
Withheld children must consume neither `max_partitions_per_refine` nor `max_search_tasks_per_run`; that is where the measured coverage gain comes from. Consequence for existing counters: `truncated_to_cap` is computed against the **post-guard** population, otherwise a single withheld child would be counted both as a wire collapse and as a cap truncation. `children_generated` keeps its current meaning (what the engine produced, minus empty/self), so the guard's effect is readable as `generated − wire_collapsed − truncated − refused_budget = admitted`.

**D6 — Two new figures, both rendered: `refused_wire_collapse` and `distinct_wire_admitted`.**
The first discharges the obligation that "never generated" be distinguishable from "generated and withheld" (`plan_races.md` §8 item 8б). The second is the distinguishable-work measure without which an efficiency ratio is misleading — the same A/B produced `aggregation_ratio` 307.6 for the arm doing ×1.7 *less* distinguishable work. Both keys must be added to the renderer's explicit allowlist; RO-S4's surface registry needs no change. Rendered tokens are fixed here so the pins and the implementation cannot drift: the third refusal reason renders inside the existing bracket as `refused[depth=…, budget=…, wire=…]`, and the distinct-wire figure renders as `uniq_wires=…` immediately after `trunc=…`. *Rejected:* publishing `aggregation_ratio` here — it belongs to `search-aggregation` and must be introduced paired with this measure, not before it.

**D6 amendment (implementation).** `distinct_wire_admitted` is implemented as a
**per-parent distinct-wire count summed over the run**, not as a single global
set of fingerprints as first written above. The reason is correctness of the
spec's own post-condition: the same wire can legitimately be admitted under two
different parents (the guard deduplicates only within a parent's batch), so a
global set would under-count distinguishable work and violate
`children_admitted == distinct_wire_admitted` (spec S21; live-gate acceptance).
The global-set figure asserted in D8 (`distinct_wire=19`) is a *global* SQL count
of distinct wire queries, not this per-run metric, and is not reproducible from
the status line; the status metric under the rollback flag reads the number of
distinct wires among the children the legacy path would have admitted, summed per
parent. The counter is bounded by `max_search_tasks_per_run` and resets with the
process, like every other governor counter.

**D7 — Withholdings are logged once per parent, never per child.**
`_log_budget_refusals` emits one WARNING per refused query; in the measured arm that was 56 048 of 70 059 WARNING lines, and the console was ×7.8 larger than the low-fanout arm's. The guard emits a single INFO per parent that withheld anything, naming provider, parent query, `generated`, `wire_collapsed` split into `parent_equal` and `sibling_duplicate`, and `admitted`. *Rejected:* per-child logging (reproduces the measured noise); silence (violates the observability obligation). **Explicitly out of scope:** changing `_log_budget_refusals` — its per-child loudness is pinned by the "Budget exhaustion refuses loudly and deterministically" scenario, so amending it would widen this change into the budget contract. Recorded as a follow-up in `tasks.md`.

**D8 — Rollback flag `refine_governor.drop_wire_indistinguishable: true`, orthogonal to `mode`.**
House pattern (`gather.defer_local_suppression`, `provider.classify_refusals`). With `false` the guard never runs in any mode, `refused_wire_collapse` stays zero, and the aggregated guard line is not emitted at all — so `false` is byte-equivalent in tasks *and* in log output, matching the standard `off`-equivalence bar this repo holds rollback flags to. **`distinct_wire_admitted` keeps being measured with the flag off**: it is an observation, not a decision, and measuring it under `false` is precisely what makes the flag A/B self-evidencing (the legacy arm then reads `adm=82 171 distinct_wire=19` instead of requiring SQL). Validation: boolean, invalid type fails config validation loudly, same treatment as `mode`. *Rejected:* relying on `mode: off` — that also disables depth, clamp and budget governance, a far larger rollback than the guard. *Rejected:* no flag — invariant "rollback is a config flip" requires one. *Rejected:* zeroing `distinct_wire_admitted` when the flag is off — it would blind the rollback arm of the gate.

**D9 — Tri-mode semantics for the new decision.**
`off`: verbatim passthrough, guard never runs, counters inert — the existing contract returns `list(queries)` including empty and self-referential entries, and that is untouched. `shadow`: guard computed and counted, would-be withholdings logged with the same aggregated line marked `(shadow)`, **everything still enqueued**, and shadow's budget bookkeeping continues to track the would-be `on` trajectory so a shadow run predicts the enforced one. `on`: enforced.

**D10 — Web-transport inertness is proved against the real function, not asserted.**
On web `wire_query(q, False) == q` for every `q`, therefore (a) the parent-equality test reduces to the pre-existing `child != parent` filter and (b) two distinct children cannot share a wire. The guard cannot withhold anything on web. The scenario exercises the shipped `wire_query`, not a stub, because a stub could trivially be written to make the property hold.

**D11 — No new assumption about any third-party response format.**
The guard rests only on "identical wire request → identical response", which the
shipped shared-response cache has relied on since `2026-09-20-add-search-aggregation`
and which the two 2026-09-29 live runs re-evidenced (`hits=13 183` served from
`misses=43`, zero capacity refusals). It claims nothing about a provider response
shape, so no live staging format probe is added to groups 1–2; the live gate
(S29) is a fan-out/coverage A/B, not a format probe.

## Risks / Trade-offs

- **[The guard withholds a child that would have harvested something the survivor does not]** → The harvest filter is the parent's root pattern inherited verbatim by every child (`data.regex`; measured: exactly one distinct value across 74 670 rows, additional patterns empty), and an identical wire request returns an identical response. The aggregation cache already depends on precisely this equivalence (`hits=13 183` served from `misses=43`). Pinned by a scenario asserting admitted children carry the parent's provider, all four patterns and `use_api` unchanged.
- **[Fewer queue rows starve gather/check]** → Measured in the A/B: gather is basket-bound (2 548 vs 2 555 requests, 93.4 vs 91.6 MB) and its *pending* backlog grew in the low-fanout arm (14 584 vs 9 136), i.e. the bottleneck received more distinct work, not less. The gate re-measures this on the flag rather than on the depth cap.
- **[Determinism change breaks a test that asserts a specific surviving raw string from a tie group]** → Per D4 today's survivor is already process-dependent, so such a test would be asserting an accident. Any test made obsolete by the change is **deleted together with its fixture**, not weakened — the surviving property (identical inputs admit identical children) is pinned more strongly than before.
- **[`distinct_wire_admitted` set grows]** → Bounded by the run budget (D6); worst case ≈640 KB, released with the process.
- **[3 × 2 matrix of `mode` × flag]** → Pinned explicitly: the guard's decisions exist only in `shadow`/`on`, and with the flag `false` both modes behave exactly as before this change.
- **[`coverage_estimate` drops further and is misread as lost coverage]** → The formula is unchanged and the metric is already documented as not a coverage measure (`plan.md` II.5.2: its denominator is the match count of the degenerate literal `"sk-"`). The guard line names the withheld count so the drop is attributable. Fixing the denominator is R6.

## Migration Plan

No schema change, no data migration, no `PRAGMA user_version` bump, nothing touching `storage/task_queue.py`. Deploy is code plus a default-on boolean. Persisted queues keep their existing rows — the guard affects only newly generated children, so restarting mid-run simply stops adding redundant rows; no backfill and no cleanup of the existing backlog is attempted or required. Rollback: set `drop_wire_indistinguishable: false` and restart.

**Live gate.** Config A/B on the **flag**, not on the depth cap: two duration-matched 300 s soaks on the shipped tree, `true` vs `false`, arm configs produced by a single-line edit of the live config with a masked-diff proof, credential-bearing copies destroyed the moment each arm exits (`AGENTS.md` §1/§3). Comparison is taken on a **defined boundary** — the final status block after graceful stop — and coverage is compared by **unique** harvested links/keys, never by record counts (`plan_races.md` §8 item 8, lessons И6/И7). Expected: `refused_wire_collapse > 0` and `children_admitted == distinct_wire_admitted` in the enforcing arm; queue rows, `data/providers` bytes and console bytes down by one to two orders of magnitude; distinct wire queries and unique harvested links not lower; gather/check throughput unchanged; 429 = 403 = secondary = 0.

## Open Questions

None that would change the specs, the approach or the task breakdown. Two items are knowingly deferred and recorded in `tasks.md` rather than left implicit: per-child budget-refusal log volume (D7), and the `aggregation_ratio` KPI paired with `distinct_wire_admitted` (D6, belongs to `search-aggregation`).
