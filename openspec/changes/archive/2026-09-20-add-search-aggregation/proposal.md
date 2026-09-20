# Proposal: add-search-aggregation

## Why

Providers with identical search conditions each run their own full HTTP chain against GitHub today: task creation iterates providers independently (`manager/task.py:368-432`), stage deduplication includes the provider in the task id (`stage/definition.py:76-80`), and the client has no cache at all — while the search response is a pure function of `(wire_query, transport, page)` and is completely provider-agnostic (patterns are applied only after the answer arrives). The waste multiplies through refine: one shared broad web condition (measured total=62 976, `WEB_LIMIT=100`) deterministically expands into ~630 identical subquery chains **per provider** (plan_agr.md §1, §3.2). A drift experiment (28 live fetches, 2026-09-20) showed Jaccard = 1.000 for identical queries over 900 s on both transports, so sharing one response between providers within short TTL windows loses nothing. The precondition change `fix-silent-losses` is implemented and archived (2026-09-20): failure-empties now raise `TransientFetchError`, so a cache layer can never admit or serve a suppressed failure (invariant I8 satisfied).

## What Changes

- **Phase 1 — wire-query fingerprint + queue locality:** extract query preprocessing (`clean_regex` for API, raw for web) into a single source of truth; stably group initial search tasks by `(transport, fingerprint)` so identical queries become FIFO neighbors — tasks are NOT merged or dropped: every provider keeps its own task and its own basket; expose an `aggregatable_pairs` planning metric.
- **Phase 2 — shared-response layer (flag-gated, default off):** TTL+LRU cache keyed by `(transport, wire_query, page)` plus singleflight coalescing of concurrent identical calls, wrapped around the stage-facing dispatchers `search_with_count`/`search_code`. Hits bypass rate-limit accounting entirely (no bucket consumption, no adaptive reporting, no cooldown-state touches); mutable results are deep-copied per consumer; exceptions (including `TransientFetchError` and `GithubCredentialLimited`) propagate verbatim to every joiner and are never cached; joiners bounded by a join timeout, after which they issue their own request (worst case = today's behavior).
- **Phase 3 — shadow measurement:** `mode: shadow` performs every real request as before, maintains the cache in parallel, compares would-be hits (Jaccard of URL sets, total-count delta) and appends records to `<workspace>/aggregation_decisions.jsonl` (pattern of `registry_decisions.jsonl`); promotion gate to `on`: p95 Jaccard ≥ 0.95 (measured baseline 1.000).
- New config section `aggregation: {mode: off|shadow|on (default off), ttl_web_s: 120, ttl_api_s: 300, max_bytes, join_timeout_s}` — rollback is a config flip.
- New metrics surface `aggregation_metrics` in PipelineStatus (hits/misses/joins/join_timeouts/evictions/bytes/shadow_comparisons/aggregatable_pairs).
- **Unchanged:** task model (one task = one provider), per-provider shards/baskets, registry hooks and coverage semantics, early-stop trackers `(provider, query)`, credential cooldown mechanics (60→900s), recovery from queues+shards, wire-query construction itself.

## Capabilities

### New Capabilities
- `search-aggregation`: wire-query fingerprinting as single source of truth; initial-task locality grouping without merging; cross-provider sharing of identical search responses via TTL/LRU cache and singleflight within one transport; tri-mode rollout flag with shadow comparison log; accounting bypass on hits; basket/attribution and durability-neutrality guarantees.

### Modified Capabilities
None. `failure-handling` and `config-query-lint` (archived 2026-09-20) keep their requirements — this change consumes the taxonomy (failure-empties raise, legitimate zeros return) without altering it. `gather-skip`, `link-registry`, `search-early-stop`, `key-ledger` requirements are untouched: aggregation lives strictly below task execution, so every consumer still receives per-provider results.

## Impact

- **Code:** new `search/querykey.py` (fingerprint helper) and `search/aggregation.py` (cache/singleflight/wrappers/metrics); `stage/definition.py` (`_preprocess_query` delegates to the helper); `manager/task.py` (locality ordering + `aggregatable_pairs`); `search/client.py` (dispatchers routed through the wrapper); `config/schemas.py`/`loader.py`/`validator.py` (new section); `core/metrics.py` + status builder (metrics surface).
- **Behavior:** with `mode: on`, identical `(transport, wire_query, page)` fetches within TTL cost one HTTP request instead of N; harvest outputs per provider remain individually produced and stored (basket invariant). With `off`/`shadow`, every call performs a real request exactly as today.
- **Dependencies:** requires `fix-silent-losses` (archived — taxonomy of empties). Independent of `fix-refine-query-split` (that change alters `clean_regex` internals; fingerprints derive from actual preprocessed output at call time, so the two compose in any order — fixing refine first merely stops caching traffic for mangled zero-queries sooner).
- **Risks:** staleness bounded by TTL ≤ measured 900 s stability horizon and validated in shadow before enforcement; cross-token private-visibility asymmetry resolves to superset (over-collection, aligned with the false-novel doctrine — documented decision, live probe verifies set equality early); memory bounded by byte-cap LRU.
- **Explicit non-goals:** check-stage aggregation (different provider APIs by design), API↔web transport unification (trap №5: wire semantics differ), gather-blob caching and semantic fingerprint v2 (deferred Phase 5 extensions, separate changes).
