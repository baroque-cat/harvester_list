# Design: add-search-aggregation

## Context

See proposal.md — Why. Current-state seams this design attaches to (verified post-`fix-silent-losses`, 2026-09-20):

- Stages call two unified dispatchers: `client.search_with_count(query, session, page, with_api, peer_page, ...)` (`search/client.py:1146`) and `client.search_code(...)` (`client.py:1288`). Preprocessing happens in the stage before the call: `_preprocess_query` applies `RefineEngine.clean_regex` for API only (`stage/definition.py:268-275` region).
- The empties taxonomy now lives inside the transport functions: failure-empties raise `TransientFetchError` (`client.py:981-987, 1027-1033, 1112-1119, 1316-1322`), legitimate zeros return normally. `collect()`'s decorator excludes `TransientFetchError` (`client.py:1347`).
- Rate limiting is entirely client-internal: `GitHubClient._limit()` per `(service, credential-hash)` bucket with adaptive `_report()` (`client.py:307-333`); `_apply_rate_limit` in `stage/definition.py:315` is dead code (never called) — no stage-level limiter to worry about.
- Credential logistics: rotation loop `while True` around the dispatcher call catching `GithubCredentialLimited` (`stage/definition.py:241-265`); pool blocks in `_get_available` when all cooling (`tools/credential.py:126-154`).
- Failure policy is centralized in `stage/base.py` (`_failure_handling_mode()`, counters) — aggregation does not interact with it except through exception transparency.
- Empirical base (plan_agr.md §3.3): Jaccard 1.000 over 900 s both transports; web approximate total drifts ±0.5% without page-composition change.

## Goals / Non-Goals

**Goals:**
- One HTTP request per distinct `(transport, wire_query, page)` within a TTL window, regardless of how many providers ask (mode `on`).
- Zero harvest impact in `off`/`shadow`; measurable staleness price in `shadow` before enforcement.
- Every provider task still executes fully with its own StageOutput, shards, registry hooks, early-stop observations (basket invariant I7).
- Bounded memory, bounded joiner waits, ephemeral state (I6), honest rate-limit accounting (I3).

**Non-Goals:**
- No task-model changes (one task = one provider stays), no queue-format changes, no shard/registry schema changes.
- No gather-blob caching, no semantic (AST-level) query normalization — deferred Phase 5 extensions.
- No cross-transport substitution, no check-stage aggregation (permanent non-goals).
- No persistence of the cache across restarts.

## Decisions

**D1 — Layer placement: wrap the two stage-facing dispatchers.**
The aggregation module exposes wrapped entry points used in place of `client.search_with_count` / `client.search_code` at the stage seam (import-site swap or thin delegation inside `client.py`). This sits below the credential-rotation loop (I1 — the loop keeps rotating per task untouched) and above the transport functions where the taxonomy raises (so admission rule I4 is structural: exceptions never reach the cache store). Alternatives rejected: (a) wrapping `GitHubClient.get` raw bytes — would double-parse and cannot distinguish legitimate zero from suppressed blank without re-implementing the taxonomy; (b) stage-level task dedup/fan-out ("variant B" of the deep-explore) — breaks ~15 one-task-one-provider invariants (dedup ids, recovery, ledger, status routing) for the same HTTP savings; (c) wrapping each transport function separately — duplicates key logic already unified by the dispatchers.

**D2 — Fingerprint: single source of truth in a new `search/querykey.py`.**
`wire_query(query, use_api)` = `clean_regex(query)` result when API and non-empty, else raw query — exactly today's `_preprocess_query` semantics, moved so that TaskManager planning and the runtime cache key cannot diverge; `SearchStage._preprocess_query` delegates to it. `fingerprint(query, use_api) = sha256(f"{transport}|{wire_query}")` hex. Cache key = `(use_api, wire_query, page)` (readable), logs/decision records use the fingerprint hash. Composes with `fix-refine-query-split` in any order: fingerprints derive from actual preprocessed output at call time; when refine is fixed, wire queries (and thus keys) change consistently for all providers.

**D3 — Credential-independence and private visibility: accept union semantics (resolves plan_agr.md open question №2).**
A response fetched under credential X may be served to a task that would have used credential Y. Rationale: the pool belongs to a single operator; GitHub code search results are public-index dominated; a superset slice means over-collection, which the project doctrine ranks strictly better than false-known/loss. Alternative rejected: visibility-class in the cache key — unobservable without extra API calls and it fragments the cache, killing hit rate. Mitigation: an early live probe (tasks group 1) measures cross-credential Jaccard on representative queries; if an operator ever mixes accounts with materially different private access, the documented guidance is separate workspaces/pools, not key changes.

**D4 — Singleflight with bounded joins (resolves plan_agr.md open question №3).**
Flight map keyed like the cache: first caller leads (executes the real dispatcher), concurrent callers join an event wait bounded by `join_timeout_s` (default 60). Outcomes: leader success → entry stored, joiners receive their own deep copies; leader exception → the exact exception object/type re-raised for every joiner (I2 — each joiner's stage loop then rotates its own credential; `mark_limited` was called once, by the leader's client path, so backoff escalation stays truthful); join timeout → the joiner issues its own real request **without re-registering in the flight map** — the slow leader's flight stays authoritative until it completes, so concurrently demoted joiners are not re-coalesced among themselves (fail-open; worst case equals today's behavior: N parallel real requests instead of 1; re-registering would require evicting an in-flight leader's flight and complicate its cleanup for no bounded gain). Joiner wait time counts as ordinary task execution time — no special stage-timeout accounting; the bound guarantees shutdown draining cannot hang (I: `queue.py` drain sees workers return).

**D5 — Cache mechanics: TTL + byte-capped LRU, monotonic clock, copy-on-serve.**
`OrderedDict` guarded by a single mutex; per-entry `(stored_at_monotonic, ttl_for_transport, approx_bytes)`; `approx_bytes ≈ len(content) + Σ len(url) + fixed overhead`. Defaults: `ttl_web_s=120`, `ttl_api_s=300` (empirical §3.3, conservative vs the 900 s horizon; raise only on shadow data), `max_bytes=64 MiB`. Eviction: LRU by insertion/use order until under cap; evicted entries never affect consumers already holding copies. Serving: URL list copied (`list(...)`), metadata out-param rebuilt per consumer (the API transport fills it in-place inside `search_api_with_count`, `client.py:1111-1112` region — the wrapper passes a fresh dict down on misses and hands deep copies on hits), content string shared (immutable). Expired entries are treated as misses and replaced.

**D6 — Shadow comparison and decision log.**
In `shadow`, every call performs the real fetch; if a fresh entry exists for the key, compute `jaccard(live_urls, cached_urls)` and `total_delta`, append one JSON line to `<workspace>/aggregation_decisions.jsonl` (`{ts, transport, fingerprint, page, jaccard, total_delta, n_live, n_cached, mode:"shadow"}`) using the project's atomic-append pattern; results served to the caller are always the live ones. The cache is maintained (stores entries) so hit-rate and staleness are both observable. Any error inside comparison/logging fails open with a counted warning (project fail-open convention). Promotion gate `shadow → on`: p95 Jaccard ≥ 0.95 over a representative cycle including a rate-limit storm, divergences not concentrated on deep pages, hit-rate justifying the risk.

**D7 — Metrics surface.**
Module-level collector (per-run lifecycle like `date_metrics`), exposed via PipelineStatus as `aggregation_metrics`: `hits, misses, joins, join_timeouts, evictions, entries, bytes, poisoned_rejected, shadow_comparisons, shadow_jaccard_min/p95, aggregatable_pairs`. `poisoned_rejected` counts admission rejections (should stay 0 structurally — nonzero indicates a taxonomy leak and is investigated, mirroring `date_fill_rate` drift-detector philosophy). `aggregatable_pairs` is computed at planning time from initial-task fingerprints (methodology proven in plan_agr.md §3.2).

**D8 — Config schema and validation.**
```yaml
aggregation:
  mode: off            # off | shadow | on   (kill-switch: off)
  ttl_web_s: 120       # 1..3600
  ttl_api_s: 300       # 1..3600
  max_bytes: 67108864  # >= 1 MiB
  join_timeout_s: 60   # 1..600
```
Loader parses with defaults; validator rejects unknown modes and out-of-range values (errors, not warnings — misconfiguration here must be loud). Flag read once at wiring time; mode flip requires restart (consistent with other tri-mode features).

**D9 — Locality ordering in task planning.**
`manager/task.py` gains a pure helper `order_tasks_for_locality(tasks) -> tasks` applied to the initial list before enqueue: stable sort by `(use_api, fingerprint)` preserving original relative order inside each group (providers/conditions keep their sequence). Guarantees: output multiset == input multiset (no merge/drop/mutation — baskets intact); refined children inherit locality naturally (adjacent parents produce adjacent child bursts). Deterministic across runs for identical configs.

**D10 — Thread-safety model.**
One lock for cache dict + flight map (short critical sections; real HTTP and copying happen outside the lock). Wrapper adds O(1) overhead on miss paths. No interaction with WorkerManager scaling: joined waits occupy a worker thread exactly like a slow request would, bounded by `join_timeout_s`.

## Risks / Trade-offs

- [Stale slice hides a brand-new leak from the second provider] → TTL ≤ 300 s ≪ measured 900 s stability horizon; shadow gate p95 ≥ 0.95 before `on`; anything missed is re-seen next run (links are never deleted; gather-skip TTL re-gathers).
- [Cross-credential visibility asymmetry] → union semantics documented (D3), early live probe, operator guidance for mixed-account pools.
- [Joiner pile-up behind a slow/hanging leader] → `join_timeout_s` bound; timeout demotes joiner to leader (own request) — degradation ceiling is today's behavior.
- [Memory growth from heavy web HTML content] → byte-capped LRU (64 MiB default), `bytes`/`evictions` metrics visible.
- [Cache masks the empties taxonomy (regression of fix-silent-losses)] → admission is structural (exceptions bypass the store); hardening matrix re-runs the P0 fault injections under all three modes.
- [Fingerprint divergence between planning and runtime (would silently halve hit rate)] → single source of truth `search/querykey.py` (D2) + a test pinning planner/runtime key equality.
- [Shadow log growth] → one compact JSON line per would-be-hit only; same operational profile as `registry_decisions.jsonl`.

## Migration Plan

1. Ship with `mode: off` (default) — zero behavioral delta; metrics plumbing inert.
2. Enable `shadow` for one representative production cycle (must include a rate-limit storm to exercise I5-style post-cooldown freshness); review `aggregation_decisions.jsonl` (p95 Jaccard, hit-rate, deep-page divergence) and `aggregation_metrics`.
3. Promote to `on` via config flip when the gate passes; watch `hits/joins/poisoned_rejected` and harvest volumes.
4. Rollback at any time: flip to `off` (restart) — no code removal, no data migration; the decision log is append-only audit data, safe to keep.

## Open Questions

None blocking. (Deep-page drift beyond page 2 is unmeasured — explicitly delegated to the shadow phase, whose comparison log records `page`, making concentration detectable before promotion.)
