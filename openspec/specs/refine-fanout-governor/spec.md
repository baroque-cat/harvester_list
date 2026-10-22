# refine-fanout-governor Specification

## Purpose

Bounds search-work generation: refinement recursion gets a depth cap, each refine gets a deterministic partition clamp, and each run gets a loud task budget — converting the historical silent truncation (queue-full drops, OOM kills) of unbounded refine fan-out into a declared, observable, config-tunable policy with a tri-mode rollout flag.

## Requirements

### Requirement: Depth-bounded refinement
Search tasks SHALL carry an additive refinement depth (roots start at zero; absent depth in previously persisted tasks deserializes to zero). Refined children SHALL be created with the parent's depth incremented by one. A task whose depth has reached the configured maximum SHALL NOT produce refined children even when its reported total exceeds the transport limit; it SHALL fall through to ordinary pagination within the existing transport page caps, and the event SHALL be counted. Pagination tasks SHALL inherit the parent's depth unchanged.

#### Scenario: Children inherit incremented depth
- **WHEN** a search task at refinement depth d produces refined children
- **THEN** every created child task carries depth d+1, and root tasks created from configured conditions carry depth 0

#### Scenario: At depth cap pagination replaces refinement
- **WHEN** a task at depth equal to `max_refine_depth` reports a total above the transport limit
- **THEN** no refined children are created, pagination tasks are emitted per the existing page-cap rules, and the depth-cap pagination counter increments

#### Scenario: Legacy persisted tasks deserialize as roots
- **WHEN** a search task serialized before this change (no depth key in its JSON) is loaded from a queue file
- **THEN** its depth is 0 and it processes normally, including eligibility to refine up to the configured caps

### Requirement: Partition sanity clamp with deterministic truncation
The partition count passed to the query generator SHALL be clamped to `max_partitions_per_refine`, bounding generator-side materialization. The generated child list SHALL be sorted into a deterministic order — ascending wire fingerprint, with the raw query as tiebreaker so that candidates sharing a wire query also have a deterministic relative order — and truncated to the cap; surplus children SHALL be counted as truncated. The truncation counter SHALL reflect cap surplus only and SHALL NOT double-count children withheld for any other reason. Admission outcomes MUST be reproducible for identical inputs regardless of the generator's internal ordering nondeterminism, including which candidate survives among those sharing a wire query.

#### Scenario: Astronomical totals yield at most the cap
- **WHEN** a parent's total implies partitions far above the cap (e.g., total 46 000 000 with limit 1000 and cap 3)
- **THEN** at most 3 refined children are admitted for that parent and the truncation counter reflects the surplus

#### Scenario: Generator receives the clamped partition count
- **WHEN** the governor clamps a parent's partitions
- **THEN** the generator is invoked with the clamped value (observable via spy/stub), so the uncapped candidate list is never materialized

#### Scenario: Identical inputs admit identical children
- **WHEN** the same parent query and total are refined twice under identical caps (including shuffled generator output order)
- **THEN** both runs admit the same child list in the same order

#### Scenario: Truncation counts cap surplus only
- **WHEN** a batch loses candidates both to wire collapse and to the partition cap
- **THEN** the truncation counter equals the number of post-guard candidates beyond the cap, the collapse counter equals the number withheld by the guard, and no candidate is counted in both

### Requirement: Per-run search-task budget with loud deterministic admission
A per-process-run budget SHALL limit the number of admitted refined children (`max_search_tasks_per_run`). Root tasks from configured conditions and pagination tasks SHALL be exempt. Within each refine batch, children SHALL be admitted in deterministic sorted order until the budget is exhausted; every refused child SHALL increment the budget-refusal counter, and all refusals for a single parent SHALL be represented in exactly ONE aggregated warning line per parent (stable token `budget_refusal:`, fields: provider, parent query, refused count, first/last refused child in sorted order, reason). Per-child warning lines are FORBIDDEN (volume scales with clamp: 46 656 children = 46 k lines per starving root; measured incident 56 048 lines). No refusal SHALL be silent in the sense that every refusal is counted and represented in the aggregated line. The budget SHALL reset with each new process run.

#### Scenario: Budget exhaustion refuses loudly and deterministically
- **WHEN** admitting a batch of children would exceed the remaining budget
- **THEN** the highest-sorted children beyond the remaining budget are refused, each refusal increments the refusal counter, exactly one aggregated warning line is emitted per parent batch (naming provider, parent query, refused count, first and last refused child in sorted order, and reason), and the admitted set is identical for identical inputs

#### Scenario: Roots and page tasks never consume the budget
- **WHEN** initial condition tasks and pagination tasks are created
- **THEN** they are enqueued regardless of budget state and the budget counter does not change

#### Scenario: Fresh process resets the budget
- **WHEN** a new governor instance is constructed (process restart)
- **THEN** the remaining budget equals the configured maximum again

### Requirement: Tri-mode rollout flag
The governor SHALL operate in one of three modes configured via `refine_governor.mode`: `off` (behavior byte-equivalent to the pre-change system, counters inert), `shadow` (all children enqueued exactly as in `off`, while every clamp/depth/budget/wire-collapse decision is computed, counted and logged as a would-be refusal), and `on` (decisions enforced). The default SHALL be `on`; switching modes SHALL require only a configuration change. Invalid modes or non-positive caps SHALL fail configuration validation loudly. The wire-collapse guard SHALL be additionally controlled by its own boolean `refine_governor.drop_wire_indistinguishable` (default true), orthogonal to `mode`: it SHALL have no effect in `off`, and in `shadow`/`on` disabling it SHALL restore the admission behaviour that predates the guard. A non-boolean value for that key SHALL fail configuration validation loudly.

#### Scenario: Off reproduces pre-change behavior
- **WHEN** mode is `off` and a broad parent refines
- **THEN** the admitted child set equals the unclamped generator output (minus the pre-existing empty/duplicate filters) and no governor counter increments

#### Scenario: Shadow measures without enforcing
- **WHEN** mode is `shadow` and caps/budget would refuse children
- **THEN** every child is still enqueued, and the would-be refusals are counted and logged with the same reasons and deterministic order that `on` would apply

#### Scenario: On enforces
- **WHEN** mode is `on`
- **THEN** depth caps, partition clamps, the wire-collapse guard and the run budget apply to enqueued children

#### Scenario: Rollback is a config flip
- **WHEN** an operator switches `on` → `off` and restarts
- **THEN** enforcement ceases with no code change and no data migration

#### Scenario: The guard boolean is validated like the mode
- **WHEN** `refine_governor.drop_wire_indistinguishable` is set to a value that is not a boolean
- **THEN** configuration validation fails loudly before any run starts, naming the key

### Requirement: Coverage observability
The system SHALL expose refinement governance metrics through the pipeline status surface: children generated and admitted, refusals split by reason (depth, budget, wire collapse), truncation surplus, depth-cap paginations, remaining budget, the number of distinct wire queries among admitted children, and a coverage estimate per refined parent defined as `min(1.0, admitted_children × transport_limit / max(total, 1))`, aggregated as min and average. The two figures added by the wire-collapse guard SHALL reach operator output through the refinement line's rendering path rather than being collected only. Per-parent outcomes SHALL also be logged at info level with generated/admitted/refused counts.

#### Scenario: Metrics visible after governed refinement
- **WHEN** governed refinements have occurred during a run
- **THEN** the status surface exposes the refinement metrics with non-zero generated/admitted counts and refusal breakdown matching the run's decisions

#### Scenario: Coverage estimate is honest arithmetic
- **WHEN** a parent with total T admits A children under transport limit L
- **THEN** its recorded coverage estimate equals `min(1.0, A×L/max(T,1))` and the per-parent log line names the same numbers

#### Scenario: The three refusal reasons are separable in published output
- **WHEN** a run produces depth refusals, budget refusals and wire-collapse withholdings
- **THEN** the rendered refinement line reports three distinguishable counts that reconcile with the run's decisions, so a reader can tell "never generated" from "generated and withheld" and from "withheld as cap surplus"

#### Scenario: Distinguishable work is published next to volume
- **WHEN** refined children have been admitted during a run
- **THEN** the surface reports both the number of admitted children and the number of distinct wire queries they represent, so the ratio between them is readable without querying the queue

### Requirement: Existing protections preserved
Governed children SHALL be ordinary search tasks: they keep the parent's provider, extraction patterns and transport (baskets and dedup identities remain per-provider), they pass through the existing stage deduplication gate, the failure-handling contract applies to them unchanged (typed transient failures propagate for bounded requeue under `failure_handling: strict`), and early-stop observation continues per `(provider, query)` for executed pages. The refine engine's outputs (wire queries) SHALL NOT be rewritten by the governor: it may sort, truncate and **withhold** candidates, and the query of every admitted child SHALL be a verbatim generator output.

#### Scenario: Children keep parent attribution
- **WHEN** refined children are admitted for a parent task
- **THEN** each child carries the parent's provider, regex/address/endpoint/model patterns and use_api flag, and child wire queries are exactly the generator's outputs (sorted/truncated/filtered, never rewritten)

#### Scenario: Strict failure retry works on governed children
- **WHEN** an admitted child task executes and its fetch fails transiently under `failure_handling: strict`
- **THEN** the typed failure propagates for bounded requeue exactly as for any search task, and the governor neither swallows nor double-counts it

#### Scenario: Early-stop still observes executed children
- **WHEN** a governed child page executes under an enabled early-stop engine (API transport)
- **THEN** the frontier tracker records the observation under the child's `(provider, query)` as before this change

### Requirement: Wire-indistinguishable children are withheld
When the governor enforces (`mode: on`) and `refine_governor.drop_wire_indistinguishable` is true, admission SHALL withhold any refined child that is not distinguishable from work already selected on the wire: a child whose wire query equals its parent's wire query, and all but one child of each group of children sharing a wire query. The wire query SHALL be obtained from the same single source of truth that produces the value sent on the wire and the key of the shared-response cache, so that planning and response sharing cannot disagree about which children are equal. Withholding SHALL be a selection among the generator's own outputs: the surviving child's query SHALL be a verbatim candidate, never rewritten or normalized. Withheld children SHALL be counted, SHALL NOT consume the partition cap or the per-run task budget, and SHALL be reported once per parent in an aggregated log line that names the provider, the parent query, the number withheld and the reason split — never one line per withheld child. On the web transport the requirement SHALL be satisfiable without withholding anything, because there the wire query is the raw query verbatim.

#### Scenario: A child collapsing onto its parent's wire query is withheld
- **WHEN** a parent whose wire query is `W` refines and every generated child also has wire query `W`
- **THEN** no child is admitted for that parent, the wire-collapse counter increments by the number of candidates, and the partition cap and run budget are unchanged by the batch

#### Scenario: Siblings sharing one wire query admit exactly one survivor
- **WHEN** a batch of children collapses onto fewer distinct wire queries than it has members
- **THEN** exactly one child per distinct wire query is admitted, the wire-collapse counter equals the number withheld, and every admitted child's query is one of the generator's verbatim outputs

#### Scenario: Enforced admission admits one child per distinct wire query
- **WHEN** a run has refined under enforcement and the guard enabled
- **THEN** the published figures satisfy `children_admitted == distinct_wire_admitted`, which an operator can read from the status line without querying the queue

#### Scenario: Withheld children consume neither cap nor budget
- **WHEN** a parent generates more candidates than the partition cap and most of them collapse onto already-selected wire queries
- **THEN** the collapse counter, the truncation counter and the budget-refusal counter account for disjoint children, the admitted count equals candidates minus collapsed minus truncated minus budget-refused, and budget remaining is reduced only by admitted children

#### Scenario: The survivor is deterministic under generator reordering
- **WHEN** the same parent and total are refined twice with the generator's candidate order shuffled between runs
- **THEN** both runs admit the same child list in the same order, and the surviving raw query of each wire group is the same in both runs

#### Scenario: A child whose regex yields no fixed literal is not spuriously collapsed
- **WHEN** parent and children both reduce to no fixed literal, so the wire query falls back to the raw query for each
- **THEN** children with distinct raw queries are all admitted and the wire-collapse counter stays zero, because the comparison is made on the transport's wire value and not on the literal-extraction step alone

#### Scenario: The web transport withholds nothing
- **WHEN** `use_api` is false and a parent refines into children with distinct raw queries
- **THEN** every non-empty child distinct from the parent is admitted exactly as before this requirement existed, and the wire-collapse counter stays zero

#### Scenario: Withholding is reported once per parent
- **WHEN** a single parent has a large batch of candidates all collapsing onto one wire query
- **THEN** exactly one aggregated log line is emitted for that parent naming provider, parent query, generated count, withheld count and the reason split, and the number of emitted lines does not grow with the number of withheld children

#### Scenario: Shadow mode counts and logs the would-be withholding without enforcing
- **WHEN** `mode` is `shadow` and a batch contains wire-indistinguishable children
- **THEN** every candidate is still enqueued, the wire-collapse counter and the aggregated line are produced with the same values and the same deterministic grouping that `on` would apply, and the line is marked as shadow

#### Scenario: The guard is a single boolean rollback independent of mode
- **WHEN** `refine_governor.drop_wire_indistinguishable` is false
- **THEN** admission in both `shadow` and `on` is identical to the behaviour before this requirement existed, the aggregated guard line is not emitted, the wire-collapse counter is zero, and the distinct-wire figure still reports how many distinct wire queries the admitted children represent

#### Scenario: A live enforced run collapses fan-out without losing distinguishable work
- **WHEN** the shipped tree runs two duration-matched soaks on the same live configuration differing only in this boolean (**Manual** — live gate, credential handling and run-root placement per the repository's operational rules)
- **THEN** the enforcing arm reports a non-zero wire-collapse counter, `children_admitted == distinct_wire_admitted`, distinct wire queries and unique harvested links not lower than the legacy arm, search queue rows and harvested-bytes down by at least an order of magnitude, gather and check throughput unchanged, and zero capacity refusals from the code-hosting service

### Requirement: Coverage-probe consultation (slice 3a.2, F-05)

When the registry carries wire-coverage rows (schema v7) the governor SHALL consult a coverage probe between the wire-collapse guard and the partition cap. A child whose wire fingerprint is fully covered (watermark ≥ needed AND fresh within `wire_ttl_hours` AND matching `patterns_hash`) SHALL be excluded from the population BEFORE cap/budget accounting, so covered children consume neither the partition cap nor the per-run task budget (mirror S22 wire-collapse accounting). The probe is passed as a trailing keyword `coverage_probe: Optional[Callable[[List[str], bool], Set[str]]] = None`. When `coverage_probe` is None or raises an exception, the governor SHALL suppress zero children (fail-open). A new counter `refused_wire_covered` SHALL count the excluded children and SHALL appear in the metrics property. The admission identity SHALL satisfy `generated == admitted + refused_wire_collapse + refused_wire_covered + truncated_to_cap + refused_budget`.

#### Scenario: Consultation order is guard → coverage → cap → budget

- **WHEN** a batch of children enters admission and some are wire-collapsed, some are covered, some exceed the cap, and some exceed the budget
- **THEN** the wire-collapse guard runs first, the coverage probe runs second, the partition cap runs third, and the budget runs fourth, and each layer's refusals are disjoint from the others

#### Scenario: Covered children consume neither cap nor budget

- **WHEN** a coverage probe reports some children as covered
- **THEN** those children are excluded before the cap and budget are applied, the partition cap and budget remaining are unaffected by the covered children, and the `refused_wire_covered` counter increments by the number excluded

#### Scenario: Shadow mode counts but does not suppress

- **WHEN** `coverage_mode` is `shadow` and the coverage probe reports covered children
- **THEN** the `wire_covered_shadow_hits` counter increments for covered children, `refused_wire_covered` stays at zero (preserving the disjoint admission identity), and all children are still admitted (no suppression)

#### Scenario: Fail-open probe

- **WHEN** the coverage probe raises an exception
- **THEN** zero children are suppressed for coverage, the governor continues without crashing, and the `refused_wire_covered` counter stays at zero for that batch

#### Scenario: Probe None is inert

- **WHEN** `coverage_probe` is None
- **THEN** the governor behaves identically to the pre-change implementation: no coverage consultation, no `refused_wire_covered` increments

### Requirement: Coverage-probe tri-mode rollout (slice 3a.2, F-05)

The coverage-probe feature SHALL be controlled by `refine_governor.coverage_mode`: `off` (probe not constructed, byte-pre-change behavior), `shadow` (probe called, `wire_covered_shadow_hits` counter grows, `refused_wire_covered` stays zero, no suppression), `on` (active suppression, `refused_wire_covered` increments). The default SHALL be `off`. The wire-coverage TTL SHALL be controlled by `refine_governor.wire_ttl_hours` (default 168, must be positive). The stage SHALL construct the probe closure per-task with the task's `patterns_hash` and pass it as a trailing keyword to `select_children` only when `coverage_mode != "off"` and the registry is available.

#### Scenario: Off mode is byte-identical to pre-change

- **WHEN** `coverage_mode` is `off`
- **THEN** no probe is constructed or passed, and the governor's behavior and metrics are byte-identical to the pre-change implementation (with `refused_wire_covered` always zero)

#### Scenario: TTL freshness

- **WHEN** a wire-coverage row has `last_ts` older than `wire_ttl_hours * 3600` seconds before the current time
- **THEN** the probe does NOT consider that row covered, even if `pages_done >= pages_needed`

#### Scenario: Patterns-change isolation

- **WHEN** a wire-coverage row exists with a different `patterns_hash` than the current task's
- **THEN** the probe does NOT consider that row covered (pattern change ⇒ re-execute)
