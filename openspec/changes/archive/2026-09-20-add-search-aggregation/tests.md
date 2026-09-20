# Test Plan

Derived mechanically from the delta spec under `specs/search-aggregation/spec.md`. One spec scenario = exactly one automated test, or one Manual entry with a reason. Scenario IDs (`search-aggregation-S<n>`) are stable: never renumber, append only.

Runner: `python3 -m pytest` (project convention: flat `tests/test_*.py`, harness style mirrors `tests/test_gather_skip_modes.py`). Observed statuses recorded from the GREEN run of 2026-09-20 (post-implementation): all six driver files collect and every scenario passes (23 tests), the hardening file `tests/test_sa_hardening.py` (task-level mode matrix, not a spec scenario) passes (8 tests), and the full suite is **163 passed** (baseline 132 + 31 new). The pre-implementation RED baseline the same day was collection `ModuleNotFoundError` on the planned modules with the existing suite at 132 passed.

## Traceability

| Scenario ID | Spec | Requirement | Scenario | Test file | Status |
|-------------|------|-------------|----------|-----------|--------|
| `search-aggregation-S1` | `specs/search-aggregation/spec.md` | Wire-query fingerprint as single source of truth | API fingerprint reflects preprocessed wire form | `tests/test_sa_fingerprint.py` | GREEN |
| `search-aggregation-S2` | `specs/search-aggregation/spec.md` | Wire-query fingerprint as single source of truth | Transport separation is absolute | `tests/test_sa_fingerprint.py` | GREEN |
| `search-aggregation-S3` | `specs/search-aggregation/spec.md` | Wire-query fingerprint as single source of truth | Fingerprint determinism | `tests/test_sa_fingerprint.py` | GREEN |
| `search-aggregation-S4` | `specs/search-aggregation/spec.md` | Initial-task locality without merging | Grouping preserves the task multiset | `tests/test_sa_locality.py` | GREEN |
| `search-aggregation-S5` | `specs/search-aggregation/spec.md` | Initial-task locality without merging | Identical queries become neighbors | `tests/test_sa_locality.py` | GREEN |
| `search-aggregation-S6` | `specs/search-aggregation/spec.md` | Initial-task locality without merging | Aggregatable pairs metric | `tests/test_sa_locality.py` | GREEN |
| `search-aggregation-S7` | `specs/search-aggregation/spec.md` | Cross-provider response sharing within a transport | Sequential identical fetch within TTL costs one HTTP request | `tests/test_sa_cache_core.py` | GREEN |
| `search-aggregation-S8` | `specs/search-aggregation/spec.md` | Cross-provider response sharing within a transport | Concurrent identical fetches coalesce | `tests/test_sa_singleflight.py` | GREEN |
| `search-aggregation-S9` | `specs/search-aggregation/spec.md` | Cross-provider response sharing within a transport | Consumers get independent copies | `tests/test_sa_cache_core.py` | GREEN |
| `search-aggregation-S10` | `specs/search-aggregation/spec.md` | Cross-provider response sharing within a transport | Hit bypasses rate-limit accounting | `tests/test_sa_modes_shadow.py` | GREEN |
| `search-aggregation-S11` | `specs/search-aggregation/spec.md` | Cross-provider response sharing within a transport | Leader failure propagates verbatim and stores nothing | `tests/test_sa_singleflight.py` | GREEN |
| `search-aggregation-S12` | `specs/search-aggregation/spec.md` | Cross-provider response sharing within a transport | Legitimate zero is cached | `tests/test_sa_cache_core.py` | GREEN |
| `search-aggregation-S13` | `specs/search-aggregation/spec.md` | Cross-provider response sharing within a transport | TTL expiry forces a real fetch | `tests/test_sa_cache_core.py` | GREEN |
| `search-aggregation-S14` | `specs/search-aggregation/spec.md` | Cross-provider response sharing within a transport | Byte cap evicts least-recently-used entries | `tests/test_sa_cache_core.py` | GREEN |
| `search-aggregation-S15` | `specs/search-aggregation/spec.md` | Cross-provider response sharing within a transport | Join timeout degrades to a direct request | `tests/test_sa_singleflight.py` | GREEN |
| `search-aggregation-S16` | `specs/search-aggregation/spec.md` | Tri-mode rollout flag with shadow measurement | Off mode is indistinguishable from pre-change behavior | `tests/test_sa_cache_core.py` | GREEN |
| `search-aggregation-S17` | `specs/search-aggregation/spec.md` | Tri-mode rollout flag with shadow measurement | Shadow measures without serving | `tests/test_sa_modes_shadow.py` | GREEN |
| `search-aggregation-S18` | `specs/search-aggregation/spec.md` | Tri-mode rollout flag with shadow measurement | On mode serves shared responses | `tests/test_sa_modes_shadow.py` | GREEN |
| `search-aggregation-S19` | `specs/search-aggregation/spec.md` | Tri-mode rollout flag with shadow measurement | Rollback is a config flip | `tests/test_sa_modes_shadow.py` | GREEN |
| `search-aggregation-S20` | `specs/search-aggregation/spec.md` | Basket and attribution invariance | Shared response feeds two baskets correctly | `tests/test_sa_baskets_e2e.py` | GREEN |
| `search-aggregation-S21` | `specs/search-aggregation/spec.md` | Ephemerality and durability neutrality | Restart starts cold | `tests/test_sa_cache_core.py` | GREEN |
| `search-aggregation-S22` | `specs/search-aggregation/spec.md` | Ephemerality and durability neutrality | Shutdown drain is not blocked by joiners | `tests/test_sa_baskets_e2e.py` | GREEN |

## Automated

### File: tests/test_sa_fingerprint.py

Describe: Wire-query fingerprint single source of truth

Modules under test do not exist yet (`search/querykey.py`) — the import failure is the expected RED state. Includes an auxiliary pin: `SearchStage._preprocess_query` must delegate to the helper (design D2 divergence risk).

- [x] `search-aggregation-S1` — it("API fingerprint reflects preprocessed wire form") <!-- WHEN raw variants converge after clean_regex THEN one fingerprint; web keeps raw -->
- [x] `search-aggregation-S2` — it("Transport separation is absolute") <!-- WHEN same raw query under api and web THEN fingerprints differ -->
- [x] `search-aggregation-S3` — it("Fingerprint determinism and golden vector") <!-- sha256("<transport>|<wire>") pinned -->
- [x] (aux) — it("Stage preprocess delegates to single source") <!-- _preprocess_query == wire_query for both transports -->

### File: tests/test_sa_locality.py

Describe: Initial-task locality grouping

Planned pure helpers in `manager/task.py` (`order_tasks_for_locality`, `count_aggregatable_pairs`) — import failure is the expected RED state.

- [x] `search-aggregation-S4` — it("Grouping preserves the task multiset") <!-- same instances, same fields, only reordered -->
- [x] `search-aggregation-S5` — it("Identical queries become FIFO neighbors") <!-- adjacency + stable intra-group order + transport separation -->
- [x] `search-aggregation-S6` — it("Aggregatable pairs metric") <!-- N − M across providers, transport-aware -->

### File: tests/test_sa_cache_core.py

Describe: Shared-response store mechanics (TTL/LRU/copies/modes-off/ephemerality)

Planned `search/aggregation.py::SearchAggregator` with injected fake clock and counting fetchers — import failure is the expected RED state.

- [x] `search-aggregation-S7` — it("Sequential identical fetch within TTL costs one HTTP request") <!-- + page/query/transport key separation -->
- [x] `search-aggregation-S9` — it("Consumers get independent copies") <!-- consumer mutation cannot poison the store -->
- [x] `search-aggregation-S12` — it("Legitimate zero is cached") <!-- 200/total=0 storable and servable -->
- [x] `search-aggregation-S13` — it("TTL expiry forces real fetch") <!-- fake-clock aging + refresh -->
- [x] `search-aggregation-S14` — it("Byte cap evicts least-recently-used entries") <!-- held copies unaffected -->
- [x] `search-aggregation-S16` — it("Off mode performs every real fetch") <!-- + zero workspace artifacts -->
- [x] `search-aggregation-S21` — it("Restart starts cold") <!-- new instance = miss; nothing on disk -->

### File: tests/test_sa_singleflight.py

Describe: Singleflight coalescing semantics

Threads/barriers against the planned module — import failure is the expected RED state.

- [x] `search-aggregation-S8` — it("Concurrent identical fetches coalesce") <!-- 3 threads, 1 HTTP, joins ≥ 2 -->
- [x] `search-aggregation-S11` — it("Leader failure propagates verbatim and stores nothing") <!-- parametrized: TransientFetchError + GithubCredentialLimited -->
- [x] `search-aggregation-S15` — it("Join timeout degrades to direct request") <!-- bounded wait, own fetch, join_timeouts metric -->

### File: tests/test_sa_modes_shadow.py

Describe: Tri-mode flag, shadow measurement, client wiring

Planned module hooks (`configure_aggregator`/`reset_aggregator`) plus wired dispatchers driven through a fake GitHubClient singleton — import failure is the expected RED state.

- [x] `search-aggregation-S17` — it("Shadow measures without serving") <!-- live result served; JSONL record with jaccard/total_delta/page/fingerprint -->
- [x] `search-aggregation-S18` — it("On mode serves shared responses")
- [x] `search-aggregation-S19` — it("Flag flip rolls back") <!-- same code, off instance = all-real -->
- [x] `search-aggregation-S10` — it("Hit bypasses accounting and metadata is copied") <!-- get_calls==1, no cooldown touches, per-consumer metadata copies -->

### File: tests/test_sa_baskets_e2e.py

Describe: Basket/attribution invariance and shutdown draining

Real `SearchStage` + real SQLite `Registry` (conftest `workspace` fixture), monkeypatched web transport — import failure is the expected RED state.

- [x] `search-aggregation-S20` — it("Shared response feeds two baskets correctly") <!-- 1 HTTP; identical links both providers; own patterns downstream; per-provider extraction; first-discoverer preserved -->
- [x] `search-aggregation-S22` — it("Shutdown drain not blocked by joiners") <!-- bounded join timeout; stop() returns; zero zombies -->

## Manual

- Production shadow-cycle promotion gate (Migration Plan step 2; not a spec scenario): one representative production cycle under `mode: shadow` including a rate-limit storm; review `aggregation_decisions.jsonl` (p95 Jaccard ≥ 0.95, deep-page divergence concentration, hit-rate) before promoting to `on`. Requires real credentials, wall-clock time, and an operator decision. **Status (2026-09-20): bounded live shadow run executed against real GitHub — p95 Jaccard = 1.000 over 3 real would-be-hit comparisons (100/100 api, 29/29 web, 23/23 web), total_delta = 0; live `on` mode proved 1 HTTP call per duplicate key and singleflight joins = 2. A deliberate rate-limit storm was NOT induced (offline storm test covers the mechanism) and a full production cycle remains an operator step. See `verification.md` §5.3–5.6.**
- Cross-credential response-equality probe (validates design assumption D3 — union semantics): live task in tasks.md group 1, not a spec scenario, because specs must not assert unconditional third-party wire behavior (opportunistic-contract rule). **Status (2026-09-20): partially executed — determinism (J = 1.000) and absolute transport separation (API∩web J = 0.016) measured live and fixtures committed; true cross-credential equality could not be measured with a single pool token (caveat in `verification.md` §5.1).**
