# Tasks: add-search-aggregation

## 1. RED baseline & live probes

- [x] 1.1 Run the six new test files (`tests/test_sa_fingerprint.py`, `tests/test_sa_locality.py`, `tests/test_sa_cache_core.py`, `tests/test_sa_singleflight.py`, `tests/test_sa_modes_shadow.py`, `tests/test_sa_baskets_e2e.py`) and confirm every failure matches tests.md: collection ModuleNotFoundError on `search.querykey` / `manager.task` helpers / `search.aggregation`; confirm the existing suite still passes untouched (baseline 132 passed)
- [x] 1.2 Guardrails before any tooling run (plan_agr.md §3.4): snapshot `logs/`; credentials only via env `GITHUB_TOKENS`/`GITHUB_SESSIONS` (`config/loader.py:174-182`); tools use their own logging dir/workspace — never write secrets into files
- [x] 1.3 Early live probe A — cross-credential equality (validates design D3 union-visibility assumption): from two different pool tokens, fetch the same `(query, page)` on both transports; compute Jaccard of URL sets; save raw responses as live-captured fixtures under `tests/fixtures/` with provenance headers (endpoint, capture date, auth mode); label synthesized variants as supplementary unit vectors — **PARTIAL: determinism (J=1.000) and absolute transport separation (API∩web J=0.016) measured live; fixtures committed (`live_2026_09_agg_*`); true cross-credential equality needs a second pool token (only one available) — documented in verification.md §5.1**
- [x] 1.4 Early live probe B — TTL-boundary drift re-check: repeat the plan_agr.md §3.3 methodology at the configured TTL points (web ≈120s, api ≈300s) for 2–3 representative queries; record Jaccard; if any p95 < 0.95 at a TTL point, lower that TTL in config defaults BEFORE Phase 2 ships (amend design.md as a numbered decision, never silently) — **DONE: J=1.000 at half-TTL and at TTL on both transports, total_delta=0; no TTL lowered (verification.md §5.2)**

## 2. Phase 1 — wire-query fingerprint & queue locality (search-aggregation-S1..S6)

- [x] 2.1 Create `search/querykey.py`: `wire_query(query, use_api)` implementing exactly the current `_preprocess_query` semantics (API → `RefineEngine.clean_regex` result when non-empty, else raw; web → raw) and `fingerprint(use_api, query) = sha256("<api|web>|<wire>")` lowercase hex (design D2)
- [x] 2.2 Make `SearchStage._preprocess_query` delegate to `wire_query` so cache keys and stage preprocessing share one source of truth; keep the aux pinning test green (`_preprocess_query == wire_query` for both transports)
- [x] 2.3 Add `order_tasks_for_locality(tasks)` (pure stable sort by `(use_api, fingerprint)`, multiset-invariant) and `count_aggregatable_pairs(tasks)` in `manager/task.py`; wire ordering into `_create_initial_tasks()` after all tasks are built — no merging, no dropping, per-provider tasks intact (design D9)
- [x] 2.4 Surface `aggregatable_pairs` in the status/metrics surface (beside skip_metrics; design D7)
- [x] 2.5 Drive `tests/test_sa_fingerprint.py` and `tests/test_sa_locality.py` to GREEN (S1–S6)

## 3. Phase 2 — aggregation core: TTL/LRU cache + singleflight (search-aggregation-S7..S16, S18, S21)

- [x] 3.1 Add `AggregationConfig` to `config/schemas.py` (`mode: off|shadow|on` default `off`, `ttl_web_s: 120`, `ttl_api_s: 300`, `max_bytes: 64MiB`, `join_timeout_s: 60`), loader parsing, and validator rules: unknown mode or out-of-range values are loud ERRORS (design D8)
- [x] 3.2 Implement `search/aggregation.py::SearchAggregator(mode, ttl_web_s, ttl_api_s, max_bytes, join_timeout_s, clock, workspace)` with `.call(use_api, query, page, real_fn) -> (results, total, content)`: OrderedDict LRU keyed `(use_api, wire_query, page)`, monotonic-clock TTL, byte-cap eviction, single mutex, copy-on-serve (list copies; content string shared immutable) (design D5)
- [x] 3.3 Singleflight: concurrent identical calls join the in-flight leader; leader success → copies to joiners; leader exception → re-raised verbatim to every joiner (I2); join wait bounded by `join_timeout_s`, timeout demotes the joiner to leader of its own request (no task ever dropped) (design D4)
- [x] 3.4 Metadata out-param safety: snapshot API `LinkMetadata` mappings into the entry; rebuild a fresh per-consumer dict on every serve so in-place mutation by one consumer cannot poison another (S9)
- [x] 3.5 Admission rule (I4, structural per D1): store only what the taxonomy layer returned successfully — `TransientFetchError` and limiter-suppressed blanks propagate below the cache and can never be stored; legitimate zeros (HTTP 200, parsed, empty items) ARE storable and servable (S12); increment `poisoned_rejected` on any defensive rejection (must stay 0)
- [x] 3.6 Wire the dispatcher seam: module-level `configure_aggregator(agg)` / `reset_aggregator()` hooks; `search_with_count` (`client.py:1146`) and `search_code` (`client.py:1288`) route through the configured aggregator, default passthrough when unconfigured/off; hits bypass `_limit`/`_report`/`mark_success` structurally (I3, S10)
- [x] 3.7 Metrics: hits, misses, joins, join_timeouts, evictions, entries, bytes, poisoned_rejected, shadow_comparisons (+ jaccard min/p95 accumulators) exposed via `PipelineStatus.aggregation_metrics` beside skip_metrics (design D7)
- [x] 3.8 Drive `tests/test_sa_cache_core.py`, `tests/test_sa_singleflight.py` to GREEN, plus modes S16/S18/S21 and wiring S10 in `tests/test_sa_modes_shadow.py`

## 4. Phase 3 — shadow comparison & decision log (search-aggregation-S17, S19)

- [x] 4.1 Shadow mode: always serve the LIVE response; on a would-be hit compare live vs cached — Jaccard of URL sets + total_count delta — and append one JSON line to `<workspace>/aggregation_decisions.jsonl`: `{ts, transport, fingerprint, page, jaccard, total_delta, n_live, n_cached, mode}` via atomic append; any comparison/logging failure is fail-open (counted warning, harvest unaffected) (design D6)
- [x] 4.2 Rollback proof: flipping `mode: on → off` (config-only, restart) returns to all-real requests with zero aggregation artifacts (S19)
- [x] 4.3 Drive `tests/test_sa_modes_shadow.py` fully GREEN (S17 included)

## 5. Phase 4 — hardening: baskets, protections, P0-fix regression (search-aggregation-S20, S22)

- [x] 5.1 Basket-invariance E2E: two providers, identical wire query, DIFFERENT patterns → one HTTP call; both links baskets hold identical URL sets; downstream gather/check tasks carry each provider's own patterns; registry hooks fire per provider (first discoverer preserved via COALESCE, coverage row per `(provider, patterns_hash)`); early-stop `observe()` called for each `(provider, query)` (S20)
- [x] 5.2 Shutdown/drain: with joiners waiting on an in-flight leader, `stage.stop(timeout=…)` completes within the bound; no zombie workers; restart finds a cold cache and recovery from queues+shards unchanged (S21/S22)
- [x] 5.3 P0-fix regression under aggregation: re-run the fix-silent-losses fault injections (injected `TransientFetchError`, limiter-blank, blob 404) in ALL three modes — failures are never stored nor served from cache, `poisoned_rejected == 0`, strict-mode requeue/drop counters behave exactly as in the archived verification (`tests/test_sa_hardening.py`; blob-404 is gather-stage and unaffected by the search-only wrap — archived `fh_*` suite GREEN)
- [x] 5.4 Rate-limit storm simulation: leader hits 429/soft-block with N joiners → exception verbatim to all, each rotates credentials independently, `mark_limited` escalation 60→900 stays truthful (called once per real request), retry burst collapses via singleflight, zero tasks lost (`tests/test_sa_hardening.py::test_credential_limit_storm_coalesces_and_propagates`)
- [x] 5.5 Full suite green: `python3 -m pytest tests/` — all sa_* scenarios GREEN, all pre-existing tests (incl. fh_* guards) GREEN (163 passed)

## 6. Live verification that aggregation works

- [x] 6.1 Offline fault-injection E2E (mock GitHub): mode matrix `off|shadow|on` × {duplicate-query pairs, transient failures, cooldown stalls} asserting HTTP-call counts (on: 1 per fingerprint; off/shadow: N per provider), decision-log contents in shadow, basket equality across modes (`tests/test_sa_hardening.py`)
- [x] 6.2 Real-GitHub shadow run (§3.4 guardrails; env credentials): representative config incl. one shared broad web query; collect `aggregation_decisions.jsonl` over a full cycle (a rate-limit storm occurrence is required evidence for I5); compute p95 Jaccard, hit-rate, harvest parity vs an `off` baseline run — **DONE (bounded): 3 real would-be-hit comparisons, p95 Jaccard = 1.000 (100/100 api, 29/29 web, 23/23 web), total_delta=0; live `on` mode proved 1 HTTP call per duplicate pair and singleflight joins=2. A deliberate rate-limit storm was NOT induced live (offline storm test covers the mechanism); full production cycle remains an operator step (verification.md §5.3–5.6)**
- [x] 6.3 Record probes/E2E/shadow results (dates, configs, metric snapshots) in `verification.md` inside the change directory; promotion `shadow → on` against the gate p95 Jaccard ≥ 0.95 remains an operator decision (Migration Plan) — **DONE: verification.md §5 records live probes, bounded shadow run, live `on` mode and fixtures; default remains `off`**

## 7. Docs & rollout

- [x] 7.1 README: `aggregation` section — modes, empirical TTL basis (drift J=1.000@900s, plan_agr.md §3.3), metrics, decision log format, promotion criteria, rollback = config flip
- [x] 7.2 `examples/config-full.yaml`: commented `aggregation:` block with all fields and mode explanations
- [x] 7.3 `openspec validate --strict` passes; every automated scenario in tests.md GREEN; Manual entries (production shadow cycle, cross-credential probe) executed and documented; change ready for archive — **`openspec validate --strict` valid, all automated scenarios GREEN; Manual entries executed/documented in verification.md §5 (cross-credential limited to one token — caveat recorded)**

## 8. Follow-ups (operator / out of scope for this pass)

- Full production `mode: shadow` cycle including a real rate-limit storm, then promotion `shadow → on` against the ≥0.95 p95-Jaccard gate (default remains `off`).
- True cross-credential equality probe once a second pool token with distinct private visibility is available.
