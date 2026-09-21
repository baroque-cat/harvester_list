# Delta Spec: refine-fanout-governor

## Purpose

Bounds search-work generation: refinement recursion gets a depth cap, each refine gets a deterministic partition clamp, and each run gets a loud task budget — converting the historical silent truncation (queue-full drops, OOM kills) of unbounded refine fan-out into a declared, observable, config-tunable policy with a tri-mode rollout flag.

## ADDED Requirements

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
The partition count passed to the query generator SHALL be clamped to `max_partitions_per_refine`, bounding generator-side materialization. The generated child list SHALL be sorted into a deterministic order (stable wire-fingerprint order) and truncated to the cap; surplus children SHALL be counted as truncated. Admission outcomes MUST be reproducible for identical inputs regardless of the generator's internal ordering nondeterminism.

#### Scenario: Astronomical totals yield at most the cap
- **WHEN** a parent's total implies partitions far above the cap (e.g., total 46 000 000 with limit 1000 and cap 3)
- **THEN** at most 3 refined children are admitted for that parent and the truncation counter reflects the surplus

#### Scenario: Generator receives the clamped partition count
- **WHEN** the governor clamps a parent's partitions
- **THEN** the generator is invoked with the clamped value (observable via spy/stub), so the uncapped candidate list is never materialized

#### Scenario: Identical inputs admit identical children
- **WHEN** the same parent query and total are refined twice under identical caps (including shuffled generator output order)
- **THEN** both runs admit the same child list in the same order

### Requirement: Per-run search-task budget with loud deterministic admission
A per-process-run budget SHALL limit the number of admitted refined children (`max_search_tasks_per_run`). Root tasks from configured conditions and pagination tasks SHALL be exempt. Within each refine batch, children SHALL be admitted in deterministic sorted order until the budget is exhausted; every refused child SHALL produce a warning identifying provider, parent query and refusal reason, and SHALL increment the budget-refusal counter. No refusal SHALL be silent. The budget SHALL reset with each new process run.

#### Scenario: Budget exhaustion refuses loudly and deterministically
- **WHEN** admitting a batch of children would exceed the remaining budget
- **THEN** the highest-sorted children beyond the remaining budget are refused, each refusal logs a warning (provider, parent query, reason) and increments the refusal counter, and the admitted set is identical for identical inputs

#### Scenario: Roots and page tasks never consume the budget
- **WHEN** initial condition tasks and pagination tasks are created
- **THEN** they are enqueued regardless of budget state and the budget counter does not change

#### Scenario: Fresh process resets the budget
- **WHEN** a new governor instance is constructed (process restart)
- **THEN** the remaining budget equals the configured maximum again

### Requirement: Tri-mode rollout flag
The governor SHALL operate in one of three modes configured via `refine_governor.mode`: `off` (behavior byte-equivalent to the pre-change system, counters inert), `shadow` (all children enqueued exactly as in `off`, while every clamp/depth/budget decision is computed, counted and logged as a would-be refusal), and `on` (decisions enforced). The default SHALL be `on`; switching modes SHALL require only a configuration change. Invalid modes or non-positive caps SHALL fail configuration validation loudly.

#### Scenario: Off reproduces pre-change behavior
- **WHEN** mode is `off` and a broad parent refines
- **THEN** the admitted child set equals the unclamped generator output (minus the pre-existing empty/duplicate filters) and no governor counter increments

#### Scenario: Shadow measures without enforcing
- **WHEN** mode is `shadow` and caps/budget would refuse children
- **THEN** every child is still enqueued, and the would-be refusals are counted and logged with the same reasons and deterministic order that `on` would apply

#### Scenario: On enforces
- **WHEN** mode is `on`
- **THEN** depth caps, partition clamps and the run budget apply to enqueued children

#### Scenario: Rollback is a config flip
- **WHEN** an operator switches `on` → `off` and restarts
- **THEN** enforcement ceases with no code change and no data migration

### Requirement: Coverage observability
The system SHALL expose refinement governance metrics through the pipeline status surface: children generated and admitted, refusals split by reason (depth, budget), truncation surplus, depth-cap paginations, remaining budget, and a coverage estimate per refined parent defined as `min(1.0, admitted_children × transport_limit / max(total, 1))`, aggregated as min and average. Per-parent outcomes SHALL also be logged at info level with generated/admitted/refused counts.

#### Scenario: Metrics visible after governed refinement
- **WHEN** governed refinements have occurred during a run
- **THEN** the status surface exposes the refinement metrics with non-zero generated/admitted counts and refusal breakdown matching the run's decisions

#### Scenario: Coverage estimate is honest arithmetic
- **WHEN** a parent with total T admits A children under transport limit L
- **THEN** its recorded coverage estimate equals `min(1.0, A×L/max(T,1))` and the per-parent log line names the same numbers

### Requirement: Existing protections preserved
Governed children SHALL be ordinary search tasks: they keep the parent's provider, extraction patterns and transport (baskets and dedup identities remain per-provider), they pass through the existing stage deduplication gate, the failure-handling contract applies to them unchanged (typed transient failures propagate for bounded requeue under `failure_handling: strict`), and early-stop observation continues per `(provider, query)` for executed pages. The refine engine's outputs (wire queries) SHALL NOT be rewritten by the governor.

#### Scenario: Children keep parent attribution
- **WHEN** refined children are admitted for a parent task
- **THEN** each child carries the parent's provider, regex/address/endpoint/model patterns and use_api flag, and child wire queries are exactly the generator's outputs (sorted/truncated, never rewritten)

#### Scenario: Strict failure retry works on governed children
- **WHEN** an admitted child task executes and its fetch fails transiently under `failure_handling: strict`
- **THEN** the typed failure propagates for bounded requeue exactly as for any search task, and the governor neither swallows nor double-counts it

#### Scenario: Early-stop still observes executed children
- **WHEN** a governed child page executes under an enabled early-stop engine (API transport)
- **THEN** the frontier tracker records the observation under the child's `(provider, query)` as before this change
