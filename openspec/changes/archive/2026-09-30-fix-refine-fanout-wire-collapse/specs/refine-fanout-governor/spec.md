## ADDED Requirements

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

## MODIFIED Requirements

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
