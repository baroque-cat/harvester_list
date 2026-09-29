# failure-handling delta

## MODIFIED Requirements

### Requirement: Check tasks survive limiter starvation

When the provider-side rate-limit bucket cannot be acquired even after the scheduled wait, the check
task SHALL be **deferred** through the durable-queue deferral seam instead of being re-enqueued as a
failure, so no harvested key silently loses its validation *and* no retry attempt is consumed by a
condition the task did not cause. The deferral SHALL preserve the task's identity and age
(`attempts`, `created_at`, deduplication key), SHALL be counted in a dedicated own-basket counter
that is separate from remote refusals, and SHALL NOT be reported to the adaptive budget, because no
request was issued. The wait carried by the deferral SHALL be derived from the basket's own refill
time and clamped by the configured cap. The stage SHALL NOT pause as a whole: the basket belongs to
one provider, so pausing every provider's validation to protect one starved basket would idle
healthy capacity.

> Supersession note (append-only scenario IDs): the former scenario *"Starved check requeues"*
> (attempts incremented) described the pre-change classification and is replaced by
> *"Starved check defers without burning an attempt"*. The former behavior remains reachable through
> the rollback flag and is pinned by *"Legacy starvation classification returns with the rollback
> flag"*.

#### Scenario: Starved check defers without burning an attempt
- **WHEN** a check task finds its provider limiter bucket unavailable after the scheduled wait
- **THEN** the task is deferred with its `attempts`, `created_at` and deduplication identity
  unchanged, no failure-empty and no task error is recorded, nothing is written to the registry, and
  no silent completion occurs

#### Scenario: Starvation is counted apart from a remote refusal
- **WHEN** a check task is deferred because our own provider basket was empty
- **THEN** the dedicated own-basket counter advances while the remote rate-limit deferral counter
  does not, and the adaptive budget receives no report for an request that was never issued

#### Scenario: Legacy starvation classification returns with the rollback flag
- **WHEN** the provider classification flag is false and a check task is starved by its provider
  basket
- **THEN** the task is re-enqueued through the bounded-retry path with attempts incremented exactly
  as before this change, and the own-basket deferral counter stays at zero
