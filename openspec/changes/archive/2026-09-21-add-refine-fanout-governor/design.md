# Design: add-refine-fanout-governor

## Context

See proposal.md — Why for incident data and probe evidence. Current-state mechanics that shape the design (all verified 2026-09-21):

- Explosion point: `_handle_first_page_results` (`stage/definition.py:333-401`) — `partitions = ceil(total/limit)` (:343), `generate_queries` (:344), child loop (:347-370). Children are created with `page=1`, so every child whose own total exceeds the limit refines again: recursion with no depth tracking.
- Generator behavior (offline probes 2026-09-21, seed `/sk-[a-zA-Z0-9]{32}/`): `partitions=64 → 72 children`, `1000 → 1296`, `46007 → 46 656 in 0.13 s` (overshoot up to ~1.35× from charset grouping). The regex-split path is order-deterministic within a process; the language/size paths return `list(candidates)` built from a `set` (`search/github/refine/engine.py:183-199`) — cross-process order is NOT guaranteed (string hashing is salted), so any admission policy must sort explicitly.
- Language fan-out multiplier: 27 languages, then 4 size ranges (`constant/search.py`), engaged when regex splitting cannot reach the requested partitions (`engine.py:179-199`).
- Live child-total distribution (api.github.com/search/code, Bearer auth, 2026-09-21, fixtures to be captured under `tests/fixtures/` per tasks): `"sk-000"` → 75 136 (recurses), `"sk-zzz"` → 934 (paginates), `"sk-i00"` → 24 (leaf) — heavy skew; full enumeration of a 46M seed needs ≥46K fetches ≈ 85 h at 0.15 req/s.
- Historical mask: default search queue 100 000 (`config/schemas.py:123`) silently dropped the excess — `put_task` on `queue.Full` warns and returns False, no counter (`stage/base.py:274,286-288`); callers ignore the bool (`manager/pipeline.py:513`). The incident config raised the queue to 4M, removing the brake → OOM.
- Integration seams available: `StageResources` optional-component pattern (gather_skip/early_stop/key_ledger fields, `stage/base.py:48-53`), pipeline wiring precedent `_configure_aggregation` (`manager/pipeline.py:190-217`), metrics dict precedent `PipelineStatus.aggregation_metrics` (`core/metrics.py:210`), tri-mode config precedent `AggregationConfig` (`config/schemas.py:511-527`, validator `:455-472`), additive task-field precedent `beneficiaries`-style serialization fallback (`core/models.py:116-134`).
- Concurrency: search runs 1 thread by default (`config/schemas.py:113`) but `WorkerManager` can scale it at runtime — governor state must be thread-safe.

## Goals / Non-Goals

**Goals:**
- Bound worst-case search-work generation per run: depth, per-refine width, and total volume — with deterministic, reproducible admission.
- Make partial coverage of astronomical seeds a declared, counted, logged policy instead of silent queue-full drops or OOM death.
- Zero changes to the refine engine internals, wire queries, fingerprints, baskets, dedup ids, early-stop, failure-handling and aggregation contracts.
- Ship behind the project's tri-mode flag with shadow measurement available and a one-flip rollback.

**Non-Goals:**
- No cross-provider task merging / beneficiary fan-out (R3 / narrow variant B territory — the governor reduces volume per provider independently).
- No disk-backed queues or backpressure redesign (R2 — this change is R2's safety precondition, not its substitute).
- No budgeting of gather/check/inspect stages (incident is search-side; those inflows are bounded by search output).
- No rewriting of `clean_regex`/wire forms (query-refinement capability untouched).

## Decisions

**D1 — Governor as an injected component, fail-safe toward governance.**
New module `search/refine_governor.py` with a `RefineGovernor` class (mode, caps, thread-safe counters behind a single lock), injected via a new optional `StageResources.refine_governor` field and wired by `Pipeline._configure_refine_governor(config)` following the `_configure_aggregation` precedent. Deviation from that precedent: on construction error the fallback is a governor with built-in safe defaults in `on` mode (loud error log + counted), NOT "off" — the ungoverned state is the proven incident, so failing open here means failing into OOM. Alternatives rejected: inline logic in `_handle_first_page_results` (untestable, bloats the hottest method); engine-side caps (touches refine internals, endangers query-refinement stability pins).

**D2 — Depth as an additive task field; cap falls through to pagination.**
`SearchTask.refine_depth: int = 0`, serialized additively; `_deserialize_data` uses `.get("refine_depth", 0)` so pre-change queue JSON loads as depth-0 roots (they may refine again — bounded by caps, harmless). Children get `parent.refine_depth + 1`. A task at `refine_depth >= max_refine_depth` with `total > limit` skips refinement and takes the existing pagination branch (`definition.py:377-401`), collecting up to the transport page cap (10×100 API / 5×20 web) — partial coverage, counted as `parents_at_depth_cap_paginated`. Page tasks inherit the parent's depth unchanged (pagination is not refinement). Alternative rejected: a visited-set of refined queries (non-serializable, RAM cost, breaks recovery semantics).

**D3 — Clamp before generation, sort before truncation.**
`partitions_effective = min(ceil(total/limit), max_partitions_per_refine)` is passed INTO `generate_queries`, bounding engine-side materialization at the source (the measured 46 656-string spike becomes ≤ cap×1.35 observed overshoot). The returned list is then sorted into the deterministic admission order defined by **D5** (ascending wire fingerprint) and truncated to the cap. Sorting is mandatory, not cosmetic: the language/size paths emit `list(set(...))` (`engine.py:196`) whose order varies across processes; sorting makes admitted subsets reproducible across restarts and identical twins (pre-R3) converge on the same survivors. Alternative rejected: truncating in engine order (coverage becomes a lottery across runs). *(Amended 2026-09-21 during verification: this decision originally read "sorted lexicographically", contradicting D5. D5 governs — the implementation now sorts by `search/querykey.fingerprint`. The contradiction was not cosmetic: on the incident seed at cap 128 (144 candidates), lexicographic truncation dropped every `w x y z` child on every run, a permanent ~11 % keyspace blind spot, whereas fingerprint order drops a scattered set that differs per parent.)*

**D4 — Per-run budget over refined children only; roots and pages exempt.**
`max_search_tasks_per_run` counts admitted refined children in the governor (lock-protected; WorkerManager-safe). Exempt: root tasks (configured conditions must always run — the operator's contract) and pagination tasks (bounded by transport page caps ≤10 per query). Exhaustion refuses the remainder of each batch with a WARNING naming provider, parent query and reason, incrementing `refused_budget`; nothing is refused silently. Budget is per-process-run: restart resets it, which composes correctly with recovery replay (dedup gate prevents double execution within a run; across runs the fresh budget re-admits deterministically). Alternatives rejected: persistent budget in the registry (schema change + unclear cross-run starvation semantics); per-provider budgets (before fan-out merging, twins would consume N separate budgets for identical work — misleading accounting).

**D5 — Admission order = sorted wire fingerprint within a batch.**
Children of one parent are admitted in ascending `search/querykey.fingerprint` order (transport included in the hash) — deterministic, provider-independent, and FIFO-friendly for the aggregation TTL cache (twins from different providers land on identical survivor sets, raising shadow hit/comparison density; synergy with R3). Cross-batch order remains execution order; totals drift between runs is acknowledged (shadow data quantifies it).

**D6 — Tri-mode flag; default `on` as a documented paradigm deviation.**
`refine_governor.mode: off | shadow | on`. `off`: byte-equivalent pre-change behavior (unclamped partitions, no depth stop, budget inert, counters silent). `shadow`: every child enqueued exactly as in `off`, but every clamp/depth/budget decision is computed, counted and logged as a would-be refusal — the coverage price becomes measurable before enforcement, per the shadow-first paradigm. `on`: enforced. Default `on` deviates from shadow-first shipping (aggregation shipped `off`, failure-handling `shadow`) because: (a) the ungoverned state is an active production defect with a recorded OOM kill, (b) the historical de-facto ceiling (100K queue + silent drops) was already stricter than these caps for any broad seed — `on` formalizes what effectively happened, minus the silence, (c) operators preferring measurement flip to `shadow` in one line. Validator rejects unknown modes and non-positive caps loudly (AggregationConfig precedent).

**D7 — Defaults grounded in the 2026-09-21 measurements.**
`max_refine_depth=2`: depth-1 children with hot prefixes (measured `"sk-000"` total 75 136 > limit 1000) legitimately need one more split; depth-2 stops the recursion there. `max_partitions_per_refine=128`: ≈15 minutes of fetches per parent at 0.15 req/s; accommodates the measured ~1.35× generator overshoot before truncation. `max_search_tasks_per_run=10000`: ≈18.5 h of search fetches ≈ one daily cycle at the configured rate; ~10× below the historical silent-truncation ceiling (100K queue), sized against the rate budget rather than RAM. All three are config-tunable; raising them is a config edit, not a code change.

**D8 — Coverage estimate, honestly labeled.**
Per refined parent: `estimate = min(1.0, admitted_children × limit / max(total, 1))` — the fraction of the reported result space reachable if every admitted child fills a page window. Aggregated min/avg exported in `refine_metrics`; per-parent INFO log carries generated/admitted/refused-by-reason counts. The estimate ignores child-total skew (live sample: 24 … 75 136) and is documented as an approximation; shadow-mode would-be data calibrates operator expectations before they rely on it.

**D9 — Metrics surface follows the aggregation precedent.**
`PipelineStatus.refine_metrics: Dict[str, Any]` (`core/metrics.py:210` neighborhood), collected via a `get_refine_metrics()` accessor mirroring `get_aggregation_metrics` (`manager/pipeline.py:18`): children_generated, children_admitted, refused_depth, refused_budget, truncated_to_cap, parents_refined, parents_at_depth_cap_paginated, budget_remaining, coverage_estimate_min/avg. Counters live in the governor instance (per process), thread-safe.

**D10 — Uniform child governance; engine untouched.**
Regex-split, `language:` and `size:` children all pass the same clamp→sort→truncate→budget pipeline; the governor never inspects query semantics beyond the wire fingerprint used for ordering. `search/github/refine/*` is not modified: query-refinement pins and search-aggregation golden vectors stay green by construction. Env seatbelts `REGEX_MAX_QUERIES`/`REGEX_MAX_DEPTH` (`refine/config.py:16-17`) remain as engine-level defense in depth, superseded as policy by this change.

## Risks / Trade-offs

- [Reduced harvest breadth on astronomical seeds] → caps are config-tunable; `shadow` mode quantifies the price before enforcement; `coverage_estimate` + refusal counters make the trade visible per run; historically the same work was dropped silently by queue overflow anyway.
- [Fallback-on-construction-error chooses governed defaults instead of off] → deliberate (D1): failing into the proven OOM state is worse than failing into caps; error is logged loudly and counted.
- [Totals drift between runs shifts admission boundaries] → within a run admission is fully deterministic; across runs the sorted-fingerprint order makes survivor sets stable for stable totals; drift is the same phenomenon the aggregation shadow log already measures.
- [Budget reset on restart re-admits children] → at-least-once philosophy consistent with recovery replay; stage dedup gate prevents duplicate execution within a run; re-admission after crash is desired (lost work returns).
- [Head-of-line blocking and RAM-per-task remain] → explicitly out of scope (R2/R3); the governor shrinks N so R2's durable queue and R3's grouped scheduling operate on thousands, not millions.
- [Interaction with early-stop] → none by construction: early-stop observes executed pages per `(provider, query)`; governed children are ordinary tasks; fewer children means fewer trackers (memory side-benefit, R4 hygiene unaffected).

## Migration Plan

1. Deploy with defaults (`mode: on`, depth 2, partitions 128, budget 10 000). Old `{stage}_queue.json` files load unchanged (missing `refine_depth` → 0).
2. Operators wanting measurement first: flip `mode: shadow` for one representative cycle, review `refine_metrics` (would-be refusals, coverage estimates), then promote to `on`.
3. Rollback: `mode: off` restores pre-change behavior byte-equivalently (including the historical silent-queue-full risk — documented in README section).
4. No data migration: registry, shards, queue formats, wire queries and fingerprints are untouched; `refine_depth` is additive and ephemeral across restarts by design.

## Open Questions

None blocking. (Per-provider vs global budget accounting deserves revisiting if/when narrow variant B fan-out lands and twins stop existing — recorded here as a future note, not a deferrable unknown for this change.)
