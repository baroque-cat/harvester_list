# Verification: fix-gather-transport

## 1. RED baseline (task 1.1, 2026-09-25)

Command:

```
python -m pytest tests/ --continue-on-collection-errors --tb=line -q
```

Result: **12 failed, 220 passed, 2 collection errors in 41.14s** — matches
the task-1.1 prediction exactly.

Expected failing set, all observed:

- `tests/test_gt_config.py` — collection `ImportError` (`GatherConfig` absent
  from `config.schemas`) covering S1–S3 + S17.
- `tests/test_gt_transport.py` — collection `ImportError`
  (`SERVICE_TYPE_GITHUB_RAW` absent from `constant.system`) covering S4–S7,
  S9–S16, S19, S20.
- `tests/test_gt_stage_integration.py` — 3 failures: S8/S21
  `AttributeError: 'Config' object has no attribute 'gather'`, S18
  `ImportError: RateLimitDeferral`.
- `tests/test_fh_client_taxonomy.py::test_s18_rate_limit_refusal_is_a_deferral_not_an_answer`
  — `ImportError: RateLimitDeferral`.
- `tests/test_fh_stage_modes.py::test_s19_deferral_does_not_consume_the_retry_budget`
  — `AttributeError: 'AcquisitionStage' object has no attribute 'defer_task'`;
  `::test_s20_deferral_cannot_circulate_forever` — same.
- `tests/test_fh_gather_fidelity.py::test_s21_deferred_gather_writes_no_registry_outcome`
  — `ImportError: RateLimitDeferral`.
- `tests/test_date_extraction.py` — s4/s5/s12 (re-scopes) + S13/S14 both fail
  (`collect() got an unexpected keyword argument 'transport'` → downstream
  `KeyError: 'file_commit_date'`).

Every other test passed; no failure outside the predicted list.

## 3. Automated suite (tasks 2.4–6.3, 2026-09-25)

Implementation complete; full suite green:

```
python -m pytest tests/ -q
276 passed in 42.31s
```

Targeted set (`test_gt_config.py`, `test_gt_transport.py`,
`test_gt_stage_integration.py`, `test_fh_gather_fidelity.py`,
`test_fh_stage_modes.py`, `test_fh_client_taxonomy.py`,
`test_date_extraction.py`): **76 passed**.

### Test-harness repairs (assertions preserved; no test weakened or deleted)

The authored tests contained several harness defects that made them fail for
reasons unrelated to the implementation. Each was repaired minimally:

1. `test_gt_config.py` — S17 "validates cleanly" built a bare `Config()`, which
   always fails the pre-existing global/task validation (no credentials, no
   tasks). Added a local `_valid_base_config()` supplying the minimum the
   existing validator requires; the gather assertions are unchanged.
2. `test_gt_transport.py` — `real_client` fixture passed the non-existent
   `GitHubClient(rate_limiter=...)`; corrected to the real `limiter=`.
3. `test_gt_transport.py` — S20 compared `inside[:16]` against a 20-character
   regex match (`AKIA` + 16). Corrected to `[:20]`, preserving the intent
   (retained-prefix match present, beyond-cap match absent).
4. `test_gt_transport.py` — S11 called `get_with_status(..., service=...)`, but
   the method derives the service from the URL. Dropped the kwarg.
5. `test_gt_stage_integration.py` — S21 used `RateLimitDeferral` without
   importing it; added the import.
6. `test_gt_stage_integration.py` / `test_fh_stage_modes.py` — referenced
   `acquisition_queue.sqlite`, but the durable file is named from the stage
   name (`gather_queue.sqlite`). Corrected both paths (the reopen was opening a
   brand-new empty DB, so the age-gate purge assertion could never fire).
7. `test_gt_stage_integration.py` — the S8 fixture `SHA` was 38 hex chars, so it
   correctly fell back to `HEAD` and could never appear in the derived URL.
   Extended to a valid 40-hex SHA (the scenario tests persistence, not ref
   length).
8. `test_gt_stage_integration.py` — S18's `slow_then_ok` stub deferred every URL
   (not just the victim), contradicting its own "every other task executed
   exactly once" assertion. Scoped the refusal to `url == victim`.
9. `test_fh_gather_fidelity.py` — s7/s8 monkeypatched `http_get`, but the
   architecture routes `collect()` through `fetch_gather_content`. Repointed the
   stubs at the new fetch seam; the assertions are unchanged.

### Non-test deviations

- `manager/pipeline.py` needs no explicit transport plumbing: the acquisition
  worker reads `resources.config.gather.transport` directly. Added
  `gather_transport_metrics` to `PipelineStatus` populated from
  `search.client.get_gather_transport_stats()` (task 6.2).
- **Rollback parity is behavioral, not literal** (verification S2). The replaced
  line was `http_get(url=url, retries=retries, interval=COLLECT_RETRY_INTERVAL)`.
  `http_get` applied `encoding_url()` (`tools/utils.py:87-99` — punycode for CJK
  characters only, a no-op for GitHub blob URLs) and retried through the
  `@network_retry` decorator's exponential backoff + jitter. `fetch_gather_content`
  calls `request("GET", target, headers, timeout)` directly with its own bounded
  backoff `min(2.0 ** attempt * 0.1, 1.0)`. Task 7.3's claim that `http_get`
  semantics are preserved for the rendered host therefore holds **in effect**
  (same URL, same retry count, same fail-open contract, same typed outcomes) but
  **not in code path**. Recorded here so the difference is auditable rather than
  implicit; the `html` rollback value still targets the identical discovered URL.
- `COLLECT_RETRY_INTERVAL` (`constant/system.py`) became dead once `collect()`
  stopped calling `http_get`. Confirmed zero references repo-wide (including
  `constant/__init__.py`'s explicit export list) and removed, per verification S2.


## 2. Live wire-format probe (task 1.2, 2026-09-25)

Probe: `/tmp/opencode/probe_gt_120.py`, anonymous, `User-Agent` set, no credential.

- (a) exact 40-hex commit ref
  `raw.githubusercontent.com/langchain-ai/docs/2095a3bf.../src/oss/langchain/guardrails.mdx`
  → **HTTP 200, 22060 bytes** (`x-cache` absent this run — CDN header variance,
  not a behavior change).
- (b) captured blob SHA `32cce148...` used as a ref → **HTTP 404, 14 bytes** —
  the trap in design D2 holds.
- (c) 22 distinct raw URLs back-to-back, no sleeping → **20/22 HTTP 200, 0
  rate-limit refusals** (the 2 non-200 are dead paths: 404, not refusal),
  3.30 req/s sustained.

No drift from the captured matrix in design.md — implementation proceeds against
the existing fixtures.

## 4. Live gate (task 8.1 core, 2026-09-25)

Probe: `/tmp/opencode/live_gate_gt.py`, run with cwd `/tmp/opencode/gt_run` so
the project logger wrote under `/tmp` (repository `logs/` and `data/`
untouched). Anonymous for the raw legs; the REST leg reads a token from the
git-ignored `.secrets` at runtime and never prints it.

Raw transport on 8 real files:

```
raw 270 ms 6034 B  torvalds/linux master README
raw 175 ms 8912 B  python/cpython main README.rst
raw  44 ms 3808 B  git/git master README.md
raw 209 ms 3304 B  rust-lang/rust master README.md
raw 201 ms 41882 B nodejs/node main README.md
raw 298 ms 4236 B  kubernetes master README.md
raw 530 ms 30083 B pytorch/pytorch main README.md
raw 482 ms 2179 B  django/django main README.rst
```

- mean **12 555 B**, median 5 135 B, max 41 882 B — at or below the captured
  17.1 KiB/file band.
- latency p50 **239 ms**, max 530 ms — inside the captured 327–634 ms band.
- derived address for the captured 40-hex ref is exact and fetched 22 060 B.
- `blob-sha-as-ref` → `TransientFetchError` (live HTTP 404), `dropped_not_found=1`.
- `HEAD` fallback counted (`head_fallback=1`); truncation at a 1024-byte cap
  returned exactly 1024 B (`truncated=1`).
- REST leg (public repo, anonymous in this run) fetched 13 213 B.
- **zero** `deferred_rate_limit` / `deferred_secondary` / `dropped_auth`:
  no unclassified refusals.
- final stats: `requests_raw=12, bytes_raw=129556, requests_rest=1,
  deferred_*=0, dropped_auth=0, dropped_not_found=1, head_fallback=1,
  truncated=1`.

**Operator-gated remainder of 8.1–8.3.** The full multi-minute production soak
(date-metrics NULL share vs the html baseline, 10/min `code_search` cadence
under concurrent gather, durable-queue integrity counters over a live backlog),
the mid-backlog transport-flip drill, and the secondary-limit accounting check
require the promoted production-shaped workspace and cannot be completed from
this bounded session; they are left for the operator with the acceptance
criteria already stated in tasks.md. The unit pins cover the mechanisms those
checks observe.


## 5. Post-verification remediation (2026-09-25)

An independent verify pass over the artifacts (`openspec validate --strict` → valid;
31/35 tasks; 27/27 scenarios GREEN; full suite 276 passed) raised three WARNINGs
and two SUGGESTIONs. All five were fixed in this session; none required weakening
an assertion, and three new regression pins were added.

### W1 — observability key set was unstable (FIXED)

`_GATHER_TRANSPORT_STATS` pre-declared only `bytes_raw`; `_gather_stat_inc` used
`.get(key, 0) + amount`, so `bytes_html` / `bytes_rest` were created **lazily on
first use** and `reset_gather_transport_stats()` (iterating the live dict) could
never shrink the set back. Measured before the fix: 11 keys at import → 13 after
one html/rest fetch → still 13 after reset. A pure-`raw` run therefore published a
narrower schema than README's documented `bytes_{raw,html,rest}`, contradicting
S21's "bytes and request counts **per transport**".

Fix: canonical `_GATHER_TRANSPORT_STAT_KEYS` tuple (13 keys, both counters for all
three transports), dict built from it at import, reset driven by the tuple and
mutating in place (no stale references), and `_gather_stat_inc` now **refuses an
undeclared key with a WARNING** instead of widening the surface. Verified: 13 keys
at import, 13 after increments, 13 after reset, undeclared `bytes_ftp` rejected.

The S21 pin previously asserted only `key in stats` (a subset check that cannot
detect drift); it now asserts `set(stats) == expected_keys`, plus zeroed
`bytes_html`/`bytes_rest` in a raw-only run, plus reset stability and undeclared-key
rejection. `tests/test_gt_stage_integration.py:235-283`.

### W2 — deferral WARNING omitted the bounded wait (FIXED)

Task 4.1 requires the WARNING to name stage/provider/task **and the bounded wait**.
The message named only the first three; the clamped value was computed in
`_worker_loop` and logged nowhere, so the D5 clamp was unobservable in production
logs during a soak.

Fix: `defer_task(task, wait_s=None)` accepts the already-clamped wait and names it
together with the cap (`bounded wait: 10.0s (cap 10.0s)`); the cap lookup was
extracted into `_defer_wait_cap()` (returns `inf` when config is unreachable, so
behaviour is unchanged) and is now shared by `_effective_defer_wait`. The worker
passes `wait_s=wait`. Callers that omit it stay valid and log `bounded wait: n/a`.
Pinned by `test_defer_warning_names_the_bounded_wait`, which also asserts exactly
one WARNING per episode and that the **unclamped** publish (999s) never appears.

### W3 — `collect(transport=None)` hardcoded `"raw"` (FIXED)

Task 3.6 says `None` resolves against the global config's `gather.transport`; the
implementation resolved it to a literal `"raw"` and `search/client.py` contained no
config access at all. No production impact (the acquisition worker passes the
configured value explicitly), but any future or embedded caller would silently
bypass the operator's rollback flag — the one mechanism D2 relies on.

Fix: `_configured_gather_transport()` performs a **deferred, fully guarded**
`from config import get_config` lookup and falls back to `_DEFAULT_GATHER_TRANSPORT`
(`"raw"`, identical to `GatherConfig`'s default) when no config is loaded or the
attribute access fails. The import stays inside the function, so the client layer
gains no hard top-level dependency on `config` (no cycle, D6 respected) and
config-less callers degrade instead of raising `RuntimeError`. Pinned by
`test_collect_without_explicit_transport_honours_configuration` (no config → raw
host; config `html` → the discovered URL and `requests_html=1`; explicit argument
beats config) and `test_configured_transport_lookup_degrades_instead_of_raising`.

### S1 — `stages.gather` vs `gather.*` ambiguity (FIXED, docs)

`StageConfig.gather: bool` (whether the stage runs) now visibly distinct from the
new top-level `GatherConfig` block (how it fetches). Added a callout with a
two-key YAML example to the README gather-transport section and a NOTE comment
above the `gather:` block in `examples/config-full.yaml`.

### S2 — rollback parity nuance and dead constant (FIXED, docs + cleanup)

Recorded under "Non-test deviations" above: parity with `http_get` is behavioral
rather than literal (direct `request(...)` with its own bounded backoff instead of
`@network_retry` + `encoding_url()`), and the now-dead `COLLECT_RETRY_INTERVAL`
was removed after confirming zero references repo-wide.

### Re-verification after the fixes

- Targeted scenario files (`test_gt_{config,transport,stage_integration}`,
  `test_fh_{client_taxonomy,stage_modes,gather_fidelity}`, `test_date_extraction`):
  **79 passed** (was 76; +3 new pins).
- Full suite `python -m pytest tests/ --continue-on-collection-errors -q`:
  **279 passed** in 42.65s (was 276 passed; no new failures, no regressions).
- `openspec validate fix-gather-transport --strict` → **valid**.
- Artifact sync: `tasks.md` 3.4 / 3.6 / 4.1 amended in place with the reason for
  each change; spec text untouched (S21's "per transport" wording is now satisfied
  literally rather than approximately).
