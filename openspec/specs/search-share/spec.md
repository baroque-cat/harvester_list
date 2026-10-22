# search-share Specification

## Purpose

Removes duplicate search-task creation when multiple providers run the same wire query: the answer is a function of (wire_fp, page), not the provider. When two providers request the same (wire_fp, page), only the first creates a search task; the second becomes a beneficiary and reuses the first provider's results. This is the search-stage mirror of the gather-stage share ledger (R17), addressing the same multiplication problem at the search layer rather than the gather layer.

## Requirements

### Requirement: Run-scoped search-task sharing

When `search.share_fetch` is `on`, the pipeline SHALL create at most one search task per `(wire_fp, page)` per run. The first provider to claim a key owns the search; every later claimant for the same key becomes a beneficiary and SHALL NOT produce a task of its own. The wire fingerprint includes the transport (API vs web), so different transports are different keys.

#### Scenario: Two providers requesting the same wire query share one search

- **WHEN** provider A and provider B both request the same (wire_fp, page)
- **THEN** exactly one search task is created, owned by A, and B is recorded as a beneficiary

#### Scenario: Different pages are never shared

- **WHEN** two providers request the same wire_fp but different pages
- **THEN** two search tasks are created and neither claim suppresses the other

#### Scenario: Different transports are never shared

- **WHEN** the same logical query is requested over API by one provider and over web by another
- **THEN** both claims create tasks, because the wire fingerprints differ

### Requirement: Beneficiary fan-out (F-07)

A successful search by the owner SHALL deliver results to EVERY beneficiary. The beneficiaries are resolved via a callback that maps patterns_hash to the sorted list of enabled providers with that hash. The owner is always first in the beneficiary list, followed by other providers in sorted order.

#### Scenario: Fan-out distributes results to all beneficiaries

- **WHEN** a search succeeds for three beneficiaries with the same patterns_hash
- **THEN** each beneficiary receives the search results in its own output shard

#### Scenario: Disabled providers are excluded from beneficiaries

- **WHEN** a provider is disabled in the config
- **THEN** it does not appear in the beneficiaries list even if its patterns_hash matches

### Requirement: Abandon on failure (NEW-3/N-09)

When a search task fails after claiming a key, the ledger SHALL release the key so another provider can claim it. Abandon is called in three cases: (a) the stage returns None (shadow-swallow), (b) a non-deferral exception is raised, (c) the worker loop catches an unexpected exception. Abandon is NEVER called on the RateLimitDeferral path (the deferral exits before take).

#### Scenario: Transient failure releases the key

- **WHEN** a search task raises TransientFetchError after claiming a key
- **THEN** abandon is called, the key is released, and a fresh provider can claim it

#### Scenario: Deferral does not release the key

- **WHEN** a search task raises RateLimitDeferral after claiming a key
- **THEN** abandon is NOT called, the key remains claimed, and the task is deferred for retry

#### Scenario: Shadow-swallow releases the key

- **WHEN** process_task returns None (shadow mode swallowed the exception)
- **THEN** abandon is called and the key is released

### Requirement: Three-mode rollout flag

Sharing SHALL be controlled by `search.share_fetch` with values `off` (default), `shadow` and `on`. In `off` mode the ledger SHALL NOT be consulted at all and behaviour SHALL be indistinguishable from the pre-change implementation. In `shadow` mode would-be suppressions SHALL be counted while every task is still created and no beneficiary state is registered. In `on` mode suppression is enforced. A value outside the three SHALL be rejected loudly with the identical message from both validation sites and SHALL NOT be coerced.

#### Scenario: Off mode is inert

- **WHEN** `search.share_fetch` is `off`
- **THEN** no ledger calls are made, no state is registered, and behaviour is byte-identical to the pre-change implementation

#### Scenario: Shadow mode counts but does not suppress

- **WHEN** `search.share_fetch` is `shadow` and two providers claim the same key
- **THEN** `would_suppress` increments but both tasks are created and `tracked` stays 0; the counter is observable on the `SShare:` render line as the `wsuppr=<n>` token between `suppressed=` and `shared=` (gate 3b.G arm-B evidence, 07 §7.4)

#### Scenario: On mode enforces suppression

- **WHEN** `search.share_fetch` is `on` and two providers claim the same key
- **THEN** the first creates a task, the second is suppressed, and `suppressed` increments

### Requirement: Cap with fail-open and loud-once warning

The ledger SHALL bound the number of tracked keys by `search.share_ledger_max_tracked` (default 50,000). When the cap is reached, new keys SHALL fail open (claim accepts without tracking) and `ledger_full` SHALL increment. A WARNING SHALL be logged exactly once when the cap is first reached, regardless of how many subsequent refusals occur.

#### Scenario: Cap reached, new keys fail open

- **WHEN** the ledger has tracked 50,000 keys and a new key is claimed
- **THEN** the claim returns False (fail open), `ledger_full` increments, and a WARNING is logged

#### Scenario: Loud-once warning fires exactly once

- **WHEN** the cap is reached and three subsequent claims are refused
- **THEN** exactly one WARNING is logged, not three

### Requirement: Composition with aggregation (D-03.8)

The search-share ledger lives ABOVE the aggregation boundary. Aggregation (TTL-based response caching) lives BELOW. The two mechanisms are complementary: aggregation deduplicates responses within a TTL window, while the ledger deduplicates task creation across the run. Counters are separate and do not overlap.

#### Scenario: Aggregation and ledger counters are disjoint

- **WHEN** both aggregation and the search-share ledger are enabled
- **THEN** aggregation_metrics and search_share_metrics report independent counters

### Requirement: Run-scoped in-RAM

The ledger SHALL be run-scoped and in-RAM. It SHALL NOT be persisted to disk. On restart, the ledger is lost and cross-provider duplicates can reappear. This is acceptable because within-run duplicates are cheap (aggregation + basket pays again), and the skip.skip_known mechanism suppresses 86.5% of post-restart re-discoveries at creation.

#### Scenario: Restart loses the ledger

- **WHEN** the pipeline restarts
- **THEN** the ledger is empty and providers can re-claim keys they claimed in the previous run

### Requirement: Root-order rotation (F-03a, slice 3b.2)

When `search.share_fetch` is `on`, the pipeline SHALL rotate the order in which providers are iterated when creating initial search tasks. The rotation SHALL be deterministic given a seed (`search.root_order_seed`): 0 = random per run (seeded from os.urandom), >0 = fixed seed for reproducibility. The rotation SHALL be a pure permutation (multiset-invariant: same elements, different order). The rotation SHALL be logged at INFO level with the seed and resulting order. When `search.share_fetch` is `off` or `shadow`, the rotation SHALL NOT be applied (byte-identical to pre-change behavior).

#### Scenario: Rotation active only when share_fetch=on

- **WHEN** `search.share_fetch` is `on` and `search.root_order_seed` is 42
- **THEN** the provider iteration order is permuted deterministically, and an INFO log records `seed=42` and the resulting order

#### Scenario: Rotation inactive when share_fetch=off

- **WHEN** `search.share_fetch` is `off`
- **THEN** the provider iteration order is byte-identical to the config declaration order (no rotation applied)

#### Scenario: Seed reproducibility

- **WHEN** `search.root_order_seed` is 42 and the pipeline is run twice
- **THEN** both runs produce the same provider iteration order

#### Scenario: Uniform distribution

- **WHEN** the pipeline is run 40 times with seeds 1..40 and 4 providers
- **THEN** each provider is first in the iteration order approximately 10 times (±5 tolerance)

#### Scenario: Locality preserved

- **WHEN** rotation is applied and `order_tasks_for_locality` is called
- **THEN** tasks with the same (transport, fingerprint) remain adjacent (locality grouping is not broken by rotation)

### Requirement: F1 × ledger interaction

When all refine children are suppressed by the ledger (share_suppressed > 0), the F1 pagination fallback SHALL NOT fire. The fallback condition changes from `enqueued == 0 and total > per_page` to `enqueued == 0 and share_suppressed == 0 and total > per_page`. This prevents creating pagination tasks for a parent whose children are already being handled by the twin's subtree.

#### Scenario: All children suppressed, no fallback

- **WHEN** refine generates 10 children, all 10 are suppressed by the ledger, and total > per_page
- **THEN** the F1 pagination fallback does NOT fire (share_suppressed > 0)

#### Scenario: Some children enqueued, no fallback

- **WHEN** refine generates 10 children, 5 are enqueued, 5 are suppressed, and total > per_page
- **THEN** the F1 pagination fallback does NOT fire (enqueued > 0)

#### Scenario: No children enqueued or suppressed, fallback fires

- **WHEN** refine generates 10 children, all 10 are dropped by the governor (not suppressed by ledger), and total > per_page
- **THEN** the F1 pagination fallback fires (enqueued == 0 and share_suppressed == 0)

## Non-Goals

- **Persistence.** The ledger is deliberately not persisted. Restart recovery is handled by skip.skip_known.
- **Cross-run sharing.** The ledger is run-scoped. Sharing across runs is out of scope.
- **Fine-grained beneficiary control.** The resolver maps patterns_hash to provider names. Per-query or per-page beneficiary selection is out of scope.

## Rollback

Set `search.share_fetch: off` (the default). This restores byte-identical pre-change behavior. No code change or data migration is required.
