# Delta Spec: search-aggregation

## Purpose

Shares one GitHub search response between all providers whose conditions produce the identical wire query on the identical transport, via a TTL/LRU cache and singleflight coalescing below the task boundary — cutting duplicate HTTP chains (amplified by deterministic refine expansion) while every provider task still executes, attributes and stores its results individually.

## ADDED Requirements

### Requirement: Wire-query fingerprint as single source of truth
The system SHALL derive a wire-query fingerprint from exactly the transformation that produces the on-the-wire query (regex-cleaning for API transport, raw query for web), through one shared helper used by BOTH task planning and runtime response sharing, so the two can never diverge. The fingerprint SHALL be deterministic for identical inputs across calls and process restarts, and SHALL incorporate the transport.

#### Scenario: API fingerprint reflects preprocessed wire form
- **WHEN** two raw condition queries converge to the same preprocessed API wire query
- **THEN** they yield the same fingerprint and are treated as one aggregatable identity

#### Scenario: Transport separation is absolute
- **WHEN** the identical raw query is used by one provider with API transport and another with web transport
- **THEN** their fingerprints differ and their fetches are never shared with each other

#### Scenario: Fingerprint determinism
- **WHEN** the same (query, transport) pair is fingerprinted repeatedly, including in a fresh process
- **THEN** the fingerprint value is identical every time

### Requirement: Initial-task locality without merging
Initial search-task planning SHALL stably group tasks by (transport, fingerprint) so identical queries become queue neighbors, while guaranteeing: the output task multiset is identical to the ungrouped creation (no merging, no dropping, no field mutation — every provider keeps its own task and basket), and relative order within each group preserves original provider/condition sequence. Planning SHALL expose an `aggregatable_pairs` metric counting cross-provider fingerprint collisions.

#### Scenario: Grouping preserves the task multiset
- **WHEN** initial tasks for several providers with overlapping queries are ordered for locality
- **THEN** the resulting list contains exactly the same tasks (same count, same providers, same fields) as the unordered creation, only reordered

#### Scenario: Identical queries become neighbors
- **WHEN** two providers declare conditions with the same wire query on the same transport
- **THEN** their search tasks are adjacent in the enqueued order (stable within the group)

#### Scenario: Aggregatable pairs metric
- **WHEN** initial planning completes for a config with N conditions sharing M distinct fingerprints across providers
- **THEN** the status surface reports the number of cross-provider duplicate pairs implied by N − M

### Requirement: Cross-provider response sharing within a transport
With aggregation enabled, fetches for an identical (transport, wire query, page) SHALL be served from a shared in-memory store when a fresh entry exists (age ≤ transport TTL), and concurrent identical fetches SHALL coalesce so exactly one HTTP request is performed for all waiters. Stored responses SHALL only come from fully successful fetches; typed transient failures and credential-limit signals SHALL propagate to every waiter verbatim and SHALL never be stored. Successful responses with zero results (legitimate zeros) SHALL be storable and servable. Consumers SHALL receive independent mutable copies (URL lists, metadata mappings); immutable content MAY be shared. A cache hit SHALL NOT consume rate-limit buckets, SHALL NOT feed adaptive rate reporting, and SHALL NOT touch credential cooldown state. Waiting for an in-flight leader SHALL be bounded by a join timeout, after which the waiter performs its own real request. Store memory SHALL be bounded by a byte cap with LRU eviction. Entries older than the transport TTL SHALL be treated as misses.

#### Scenario: Sequential identical fetch within TTL costs one HTTP request
- **WHEN** provider A's task fetches (web, query Q, page 1) and provider B's task fetches the same key within the TTL window
- **THEN** exactly one HTTP request occurs and B receives the same URL set and total as A

#### Scenario: Concurrent identical fetches coalesce
- **WHEN** N identical fetches start concurrently
- **THEN** exactly one HTTP request is performed and all N callers receive equal results

#### Scenario: Consumers get independent copies
- **WHEN** two consumers receive the same cached response and one mutates its URL list or metadata mapping
- **THEN** the other consumer's data and the stored entry are unaffected

#### Scenario: Hit bypasses rate-limit accounting
- **WHEN** a fetch is served from the store
- **THEN** no rate-limit token is consumed for it, no adaptive success/failure is reported, and no credential cooldown state changes

#### Scenario: Leader failure propagates verbatim and stores nothing
- **WHEN** the leading fetch raises a typed transient failure or a credential-limit signal while joiners wait
- **THEN** every joiner receives the same exception type, nothing is stored, and a subsequent fetch attempts a fresh HTTP request

#### Scenario: Legitimate zero is cached
- **WHEN** a fetch succeeds with HTTP 200 and zero items
- **THEN** the empty result is storable and a repeat fetch within TTL is served from the store without a new HTTP request

#### Scenario: TTL expiry forces a real fetch
- **WHEN** a fetch occurs for a key whose entry is older than the transport TTL
- **THEN** a real HTTP request is performed and the entry is refreshed

#### Scenario: Byte cap evicts least-recently-used entries
- **WHEN** storing a new entry would exceed the configured byte cap
- **THEN** oldest entries are evicted until the cap holds, and consumers already holding copies are unaffected

#### Scenario: Join timeout degrades to a direct request
- **WHEN** a joiner waits for the in-flight leader longer than the join timeout
- **THEN** the joiner performs its own real request and completes normally (no failure, no drop)

### Requirement: Tri-mode rollout flag with shadow measurement
Response sharing SHALL be governed by `aggregation.mode`: `off` (no sharing, no store maintenance, behavior identical to pre-change), `shadow` (every fetch performs its real request; the store is maintained; would-be hits are compared against live results — Jaccard similarity of URL sets and total-count delta — and appended as JSON lines to `<workspace>/aggregation_decisions.jsonl`; served results are always live), and `on` (full sharing). Default SHALL be `off`. Mode switching SHALL require only a configuration change. Comparison/logging failures SHALL fail open (counted warning, live result served).

#### Scenario: Off mode is indistinguishable from pre-change behavior
- **WHEN** the pipeline runs under `mode: off` with duplicate queries across providers
- **THEN** every fetch performs its own HTTP request, no decision-log file is created, and results match pre-change behavior

#### Scenario: Shadow measures without serving
- **WHEN** a fetch under `mode: shadow` finds a fresh stored entry for its key
- **THEN** the live request still executes and its result is served, and one comparison record (Jaccard, total delta, page, fingerprint) is appended to the decision log

#### Scenario: On mode serves shared responses
- **WHEN** the pipeline runs under `mode: on`
- **THEN** the response-sharing requirement takes effect (hits served, joins coalesced)

#### Scenario: Rollback is a config flip
- **WHEN** an operator switches `mode: on` → `off` and restarts
- **THEN** sharing ceases with no code change and no data migration; the decision log remains as audit data

### Requirement: Basket and attribution invariance
Response sharing SHALL NOT alter per-provider attribution: with two providers sharing one wire query but holding different extraction patterns, each provider's task SHALL still produce its own stage output; discovered-link records SHALL be written for both providers; registry hooks SHALL fire per provider (first discoverer preserved, coverage rows per provider+patterns_hash after gathers); early-stop observation SHALL occur per (provider, query); downstream acquisition tasks SHALL carry each provider's own patterns.

#### Scenario: Shared response feeds two baskets correctly
- **WHEN** providers A and B with the same wire query and different key patterns execute their search tasks under `mode: on` with one HTTP request
- **THEN** both providers' link outputs contain the identical URL set, each provider's downstream tasks carry its own patterns, and registry discovery hooks recorded both providers

### Requirement: Ephemerality and durability neutrality
The shared store SHALL be in-memory only: no cache state persists across restarts, and no workspace artifact is created by `off`/`on` modes (the shadow decision log is the sole on-disk output, append-only audit data). Queue persistence, shard formats, and recovery replay SHALL be unaffected; graceful-shutdown draining SHALL not be blocked by waiting joiners beyond the join timeout.

#### Scenario: Restart starts cold
- **WHEN** the process restarts after cached fetches
- **THEN** the first fetch for any key performs a real HTTP request and recovery from queues/shards behaves exactly as without aggregation

#### Scenario: Shutdown drain is not blocked by joiners
- **WHEN** graceful shutdown begins while joiners wait on an in-flight leader
- **THEN** waiters return within the join timeout bound and queue draining completes without hanging
