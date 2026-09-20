# Verification: add-search-aggregation

Recorded 2026-09-20 on branch workspace `/home/openuser/scrap/harverster_gist`
(Python 3.14, `python3 -m pytest`).

## 1. RED baseline (task 1.1)

Pre-implementation, the six driver files failed collection exactly as the test
plan predicted:

```
ERROR tests/test_sa_fingerprint.py      ModuleNotFoundError: No module named 'search.querykey'
ERROR tests/test_sa_locality.py         ImportError: cannot import name 'order_tasks_for_locality'
ERROR tests/test_sa_cache_core.py       ModuleNotFoundError: No module named 'search.aggregation'
ERROR tests/test_sa_singleflight.py     ModuleNotFoundError: No module named 'search.aggregation'
ERROR tests/test_sa_modes_shadow.py     ModuleNotFoundError: No module named 'search.aggregation'
ERROR tests/test_sa_baskets_e2e.py      ModuleNotFoundError: No module named 'search.aggregation'
```

Existing suite baseline the same day: **132 passed** (unaffected).

## 2. GREEN run (tasks 2.5, 3.8, 4.3, 5.1, 5.2, 5.5, 6.1)

| Suite | Result |
|-------|--------|
| `tests/test_sa_fingerprint.py` (S1–S3 + aux pin) | 4 passed |
| `tests/test_sa_locality.py` (S4–S6) | 3 passed |
| `tests/test_sa_cache_core.py` (S7, S9, S12, S13, S14, S16, S21) | 7 passed |
| `tests/test_sa_singleflight.py` (S8, S11, S15) | 4 passed |
| `tests/test_sa_modes_shadow.py` (S17, S18, S19, S10) | 3 passed |
| `tests/test_sa_baskets_e2e.py` (S20, S22) | 2 passed |
| `tests/test_sa_hardening.py` (task-level: 5.3/5.4/6.1 mode matrix) | 8 passed |
| **Full suite** `python3 -m pytest tests/` | **163 passed** |

All 22 spec scenarios in `tests.md` are GREEN; statuses updated there.

## 3. Offline hardening evidence (tasks 5.3, 5.4, 6.1)

`tests/test_sa_hardening.py` drives the real dispatcher seam
(`search_with_count`) with a mock GitHub client:

- **Mode matrix** (`off|shadow|on`) x duplicate-query pairs: HTTP call counts
  are `2 / 2 / 1` respectively; the shadow decision log is created and its last
  record carries `mode="shadow"`, `jaccard=1.0`.
- **Failure-empties in all three modes**: a blank payload (limiter suppression)
  and a transport error both raise `TransientFetchError`, store nothing
  (`entries == 0`, `poisoned_rejected == 0`) and never serve poison — a later
  healthy fetch still performs a real request. (Gather-stage blob 404 is out of
  scope here: aggregation wraps only the search dispatchers; the archived
  `fh_*` suite covers that path unchanged and remains GREEN.)
- **Credential-limit storm**: four concurrent identical calls collapse onto one
  real request (singleflight) and every waiter receives
  `GithubCredentialLimited` verbatim; `poisoned_rejected == 0`.

## 4. Configuration validation (task 3.1)

`AggregationConfig` defaults (`off`, 120 s / 300 s, 64 MiB, 60 s) load from the
loader; unknown mode and out-of-range TTL / `max_bytes` / `join_timeout_s`
raise loud `ValueError`s. `examples/config-full.yaml` and
`examples/config-simple.yaml` both parse with the new section.

## 5. Live verification against real GitHub (tasks 1.3, 1.4, 6.2)

Executed 2026-09-20 with credentials read at runtime from the gitignored
`.secrets` (never written to any repository file; a post-run scan of the whole
working tree for both credential values returned **no leaks**). Guardrails
observed: `logs/` was snapshotted to
`/tmp/opencode/aggregation_logs_snapshot_2026-09-20/`; API code-search requests
were paced under 10/min.

**Environment finding.** GitHub's REST code-search API does **not** support the
`AND` / `content:` operators that the web UI accepts; the same query text that
returns 3 results on the web returns 0 via REST. Live API probes therefore used
plain literals (`AKIA`, `sk_live_`), matching how `clean_regex` extracts fixed
literals for the API transport. Relevant for authoring future shadow configs.

### 5.1 Probe A — determinism & transport separation (task 1.3)

| Measurement | Result |
|-------------|--------|
| API: two identical `(query, page)` fetches | Jaccard **1.000**, totals equal (3 481 600) |
| Web: two identical fetches | Jaccard **1.000** (29 links) |
| API vs web for the same literal | Jaccard **0.016** (29 shared of ~100/29) |

Transport separation is absolute (design D2). **Cross-credential equality could
not be measured**: only one API token and one web session were available, so
the D3 union-visibility assumption is not directly exercised. It remains a
documented operator guidance item for pools that mix accounts with different
private visibility.

### 5.2 Probe B — TTL-boundary drift re-check (task 1.4)

Sampled at half-TTL and at the configured TTL point; both transports in
parallel.

| Transport | Query | n | J @ half-TTL | J @ TTL | total_delta @ TTL |
|-----------|-------|---|--------------|---------|-------------------|
| api (TTL 300 s) | `AKIA` | 100 | 1.000 | **1.000** | 0 |
| api (TTL 300 s) | `sk_live_` | 100 | 1.000 | **1.000** | 0 |
| web (TTL 120 s) | `"AKIA" AND content:"aws"` | 29 | 1.000 | **1.000** | 0 |
| web (TTL 120 s) | `"sk_live_"` | 25 | 1.000 | **1.000** | 0 |

No p95 < 0.95 at any configured TTL point, so **no TTL default was lowered** and
`design.md` needs no amendment.

### 5.3 Bounded shadow run (task 6.2)

`mode: shadow` with a temp workspace; each key fetched twice (second fetch is a
would-be hit), giving three real comparisons in
`aggregation_decisions.jsonl`:

| transport | n_cached | n_live | jaccard | total_delta |
|-----------|----------|--------|---------|-------------|
| api | 100 | 100 | 1.000 | 0 |
| web | 29 | 29 | 1.000 | 0 |
| web | 23 | 23 | 1.000 | 0 |

p95 Jaccard = **1.000** (gate is ≥ 0.95), hit-rate evidence positive.

### 5.4 Live `on` mode — hit path & singleflight

| Measurement | Result |
|-------------|--------|
| Sequential identical API fetches | **1** real HTTP call; `hits=1`, `misses=1`; results equal |
| 3 concurrent identical API fetches | **1** real HTTP call; `joins=2`; all three results equal |

Cache hits structurally bypass the transport/limiter path (I3); accounting
bypass itself is proven offline by `search-aggregation-S10`.

### 5.5 Live fixtures (task 1.3)

Trimmed live captures with `_provenance` headers committed under
`tests/fixtures/` following the existing `live_2026_09_*` convention:

- `live_2026_09_agg_api_akia.json` (100 URLs, total 3 481 600)
- `live_2026_09_agg_web_akia_aws.json` (29 URLs)
- `live_2026_09_agg_web_ghp.json` (23 URLs)

These are the URL-set unit vectors backing the Jaccard evidence above.

### 5.6 Not exercised live

A deliberate rate-limit storm was **not** induced against the real API (to avoid
self-inflicted abuse). The provider-limit coalescing/verbatim-propagation
mechanism is covered by the offline storm test
(`tests/test_sa_hardening.py::test_credential_limit_storm_coalesces_and_propagates`).
Promotion `shadow → on` over a full production cycle including a real storm
remains an operator decision (Migration Plan); the default stays `off`.

## 6. `openspec validate --strict` (task 7.3)

`openspec validate add-search-aggregation --strict` → **valid**. Every automated
scenario in `tests.md` is GREEN and the Manual entries (production shadow cycle
evidence above, cross-credential probe caveat above) are executed and
documented.

## 7. Secret hygiene

The live credential file is gitignored (`.gitignore:112`). Credentials were
loaded in-process only. A working-tree scan for both exact credential values
across all files (excluding `.git/` and `.secrets` itself) found **none**.
