# Design: fix-gather-transport

## Context

See proposal.md — Why. Current-state facts that shape the approach, all verified by reading the code and by live capture on **2026-09-25** (probe scripts retained at `/tmp/opencode/probe_surfaces.py`, `probe_raw_burst.py`, `probe_date_extraction.py`, `probe_raw_ref.py`, `probe_raw_ref2.py`).

### Code path as it exists today

- Gather fetch is module-level `http_get(url, headers=None, params=None, retries=3, interval=1.0, timeout=10)` (`search/client.py:746-848`). It builds `DEFAULT_HEADERS.copy()` (:785), calls `managed_network(request("GET", ...))` (:796-798), decodes UTF-8 with a gzip fallback (:807-813), and classifies errors at :815-848. There is **no** `limiter.acquire()`, **no** credential, **no** outcome reporting, and **no** `Retry-After` parsing anywhere in it.
- Error classification in that block: `429 → ConnectionError("Rate limit exceeded (HTTP 429)")` (:819-821, retryable); `404 → FileNotFoundError` (:822-823, not retryable); **`401|403 → NetworkError("Authentication failed (HTTP {code})")`** (:824-826, not retryable); `>=500 → ConnectionError` (:827-829, retryable).
- `RetryCore.should_retry_error` retries only `ConnectionError`/`TimeoutError`, or messages containing `"rate limit"`/`"too many requests"`; everything else returns False (`tools/retry.py:51-67`). Therefore a 403 produces exactly one request and no requeue.
- `collect(key_pattern, url="", retries=3, address_pattern="", endpoint_pattern="", model_pattern="", text=None, metadata=None)` (`client.py:1397-1490`) fetches only when `text` is falsy (:1426-1439), wraps any fetch exception into `TransientFetchError` (:1434-1439), fills `metadata["file_commit_date"]` from the payload it already holds (:1447-1452), then extracts via `extract()` — a pure `re.findall` with a set-dedup and no HTML-specific logic (:1494-1517).
- Call site: `AcquisitionStage._acquisition_worker` passes `url=task.url, retries=task.retries, metadata=metadata` (`stage/definition.py:557-565`); on `TransientFetchError` in strict mode it records a failed gather and re-raises for bounded requeue (:566-571), while legacy/shadow collapse to an empty success (:572-576).
- Retry arithmetic: inner `@network_retry` gives `max_attempts = retries = 3` with ~1 s and ~2 s sleeps (`delay=1.0`, `backoff_factor=2.0`, jitter ±10%, `tools/retry.py:311-356`); the worker-loop policy is built as `ExponentialBackoff(max_retries + 1)` (`stage/base.py:193`, the deliberate `+1` documented at :189-192) giving 4 rounds with 1+2+4+8 s sleeps. One doomed task = **12 HTTP requests ≈ 27 s of a blocked worker**.
- Shared client plumbing that gather does not use: `GitHubClient._service(url)` maps `"api.github.com" → SERVICE_TYPE_GITHUB_API` and `elif "github.com" → SERVICE_TYPE_GITHUB_WEB`, else **`None`** (`client.py:276-287`; constants at `constant/system.py:99-100`). `_bucket_name`/`_ensure_bucket` build per-(service, credential) buckets (:289-306). `_build_url` only encodes and appends params — it does **not** prefix a base host (:609-615), so the shared client can address any hostname. `get_with_status` runs the full chain: `_limit` (:414) → request → `_report` (:421) → content-based rate-limit detection (:425-427) → `mark_credential_limited` (:429-431) → `mark_success` (:437-438).
- Durable-queue interaction: `queue.visibility_timeout_s = 300` in the promoted production config, `GITHUB_CREDENTIAL_COOLDOWN_MAX = 900` (`constant/system.py:49`), and `storage/task_queue.py` exposes **no** claim extend/renew/heartbeat API. A worker sleeping inside a claimed row past the visibility window has its row returned to `pending` by `sweep_expired_claims()` and re-delivered to another worker.

### Captured external formats (fixture record)

`GET https://api.github.com/rate_limit`, token auth, 2026-09-25 — exact per-token budgets:

```
core        limit 5000/h      code_search limit 10/min
search      limit 30/min      graphql     limit 5000/h
```

`GET https://api.github.com/search/code?q=/sk-[a-zA-Z0-9]{32}/`, token auth, 2026-09-25, `total_count = 81`. Item keys are exactly `{git_url, html_url, name, path, repository, score, sha, url}`. Captured sample (public repository paths, no key material):

```
name      = "guardrails.mdx"
path      = "src/oss/langchain/guardrails.mdx"
sha       = "32cce148466eaa5a31334ec2b479ace5b7c32914"      <-- BLOB sha
url       = "https://api.github.com/repositories/984376616/contents/src/oss/langchain/guardrails.mdx?ref=2095a3bf8af499e74caf5ccdb7ae783f5dec1afd"
git_url   = "https://api.github.com/repositories/984376616/git/blobs/32cce148466eaa5a31334ec2b479ace5b7c32914"
html_url  = "https://github.com/langchain-ai/docs/blob/2095a3bf8af499e74caf5ccdb7ae783f5dec1afd/src/oss/langchain/guardrails.mdx"
```

Ref-resolution matrix measured against `raw.githubusercontent.com` for two real repositories (anonymous, `User-Agent` set, 2026-09-25):

| ref used | result |
|---|---|
| exact 40-hex commit SHA from `html_url` | **HTTP 200**, `x-cache: HIT` |
| exact 40-hex commit SHA from `url?ref=` (identical value, 2/2 samples) | HTTP 200 |
| `HEAD` | HTTP 200 (default-branch content, *not* the indexed revision) |
| `master` / `main` | HTTP 200 on both sampled repos (repo-specific luck, **not** a general rule) |
| bogus non-hex ref | **HTTP 404**, 14-byte body |
| `0`×40 (well-formed but nonexistent SHA) | **HTTP 404**, 14-byte body |
| `item['sha']` (the **blob** SHA) used as a ref | **HTTP 404**, 14-byte body |

Three-surface comparison on the same five real files, 2026-09-25:

| surface | auth | status | latency | bytes/file | limit headers | `core` cost |
|---|---|---|---|---|---|---|
| `github.com` blob HTML (today's gather) | none | 200×5 | 738–1085 ms | 282 KB – 1.27 MB | **none at all** | 0 |
| `raw.githubusercontent.com` | none | 200×5 | 327–634 ms | 1.9 – 108 KB | none, but `x-cache: MISS/HIT` | **0** (`used` 13 before and after) |
| `api.github.com` REST contents | token | 200×5 | 398–591 ms | 3.8 – 150 KB (base64) | full `x-ratelimit-{limit,remaining,reset,resource,used}` | **1 per call** (`used` 1→2→3→4→5) |

Burst of **80 distinct** raw URLs with no sleeping: 80/80 HTTP 200, zero refusals, 3.03 req/s sustained (latency-bound, not rate-bound), p50 338 ms / p90 365 / p99 390 / max 390 ms, total 1366 KiB, mean **17.1 KiB/file**. Control on 12 distinct URLs through today's anonymous HTML: 12/12 HTTP 200 but only 1.23 req/s, total 6.13 MiB, mean **523 KiB/file**. Ratio **30.6×**; applied to the measured 2 343-task backlog, **1.20 GiB → 39 MiB**.

Date extraction, measured with the project's own function on a live blob page (2026-09-25): `_extract_file_commit_date(html)` returns `None` and logs `[date-extraction] no_relative_time` (`client.py:44-50`). Marker census on that page: `relative-time` ×1, **`datetime=` ×0**, `react-app` ×4 — GitHub serves a React shell, so `_RELATIVE_TIME_RE` (`client.py:38`) can never match. The raw payload also yields `None`. The REST contents response *does* carry `last-modified` with the file date (observed `Thu, 24 Sep 2026 18:39:05 GMT`) for 1 `core` unit.

Soak evidence on the promoted production config (600 s, 2026-09-22): 2 715 × `Rate limit exceeded (HTTP 429)`, gather backlog 2 343, 1 117 ERROR lines, zero queue-integrity failures.

## Goals / Non-Goals

**Goals:**
- Cut gather traffic per file by the measured ~30× and eliminate the anonymous-HTML refusal storm, without changing what a harvested key looks like downstream.
- Bring gather inside the throttling system it currently bypasses, under its own budget so it cannot starve code search.
- Make every refusal outcome explicit, counted and bounded: nothing silent, nothing immortal, no worker asleep inside a durable claim past its visibility window.
- Keep rollback a single config flip, and keep persisted tasks and legacy snapshots valid across that flip.

**Non-Goals:**
- No change to the credential selector's liveness (`tools/credential.py:126-154` unbounded `while True`, `mark_success` early release) — that is `fix-credential-liveness`, next in sequence.
- No global narrowing of the shared content detectors (`client.py:537-544`), no revival or deletion of the inert adaptive knobs (`calculate_adjusted_rate`, `backoff_factor`, `recovery_factor`, `max_rate_multiplier`, `min_rate_multiplier`).
- No cross-provider fetch sharing (research candidate R7), no aggregation promotion.
- No restoration of `file_commit_date` from the REST `last-modified` header; the measured working source is recorded here but the work is deliberately deferred.
- No claim-extend/heartbeat API on the durable queue; the invariant is enforced by bounding waits instead.
- Search transport selection (`use_api`) is untouched; this change governs the **gather** fetch only.

## Decisions

### D1 — Config surface `gather.transport: html | raw | rest`, default `raw`

New `GatherConfig` dataclass in `config/schemas.py` following the `TaskQueueConfig`/`RefineGovernorConfig` house pattern: `__post_init__` normalizes via `str().strip().lower()`, rejects anything outside the three values and any non-positive numerics loudly; loader hook `if "gather" in data:`; validator method appending to the existing `ValueError("Configuration validation failed")` funnel.

*Documented deviation from the tri-mode paradigm, same shape as R2's D8 and R1's default-`on`*: `off|shadow|on` exists for risk **policies** whose divergence is measurable in flight. A transport is binary infrastructure — a shadow mode would mean fetching every file twice through two surfaces, doubling the very load this change removes. Defaulting to `raw` rather than shipping dark follows R1's precedent (default `on` because the ungoverned state was a proven incident): the `html` path is measured-harmful (2 715 refusals per 600 s, 30.6× traffic, silent uncounted 403 losses), and `raw` was measured clean over an 80-URL burst with zero refusals. Rollback story is identical to tri-mode: flip one key, restart, no code change.

*Rejected alternative*: default `html` and promote later. Rejected because it leaves a measured loss channel active by default in every fresh deployment, and the evidence for `raw` is direct rather than inferred.

### D2 — Derive the transport URL at fetch time from the blob link; persist nothing new

`AcquisitionTask.data.url` stays exactly what search produced (`https://github.com/<owner>/<repo>/blob/<ref>/<path>#L…`, from `item["html_url"]` at `client.py:998,1135` or the href regex at `:1374-1380`). The transform runs inside the fetch layer at request time.

Derivation rule, grounded in the captured ref matrix above:

1. Parse `owner`, `repo`, `ref`, `path` from the blob URL, taking `ref` as the segment(s) between `/blob/` and the known `path` suffix.
2. If `ref` matches `[0-9a-f]{40}` → emit `https://raw.githubusercontent.com/<owner>/<repo>/<ref>/<path>` verbatim. This is the semantically correct address: an immutable commit, which also makes CDN caching (`x-cache: HIT`) safe by construction.
3. Otherwise → emit the same URL with `HEAD` in place of the ref, and increment a dedicated `gather_head_fallback` counter plus a WARNING naming the URL. Never guess a branch name silently.
4. **Never** use `item['sha']`: the capture proves it is the blob SHA and returns HTTP 404 as a ref. This trap is pinned by a test using the captured sample.
5. Unparseable or non-blob URL → keep today's behavior (fetch as-is under `html`, or fail-open to a counted skip under `raw`), never raise into the worker loop.

Because the persisted task is unchanged, durable-queue rows written before the flip, legacy `{stage}_queue.json` snapshots and the `.imported-*` archive all remain valid; rollback needs no data migration.

*Rejected alternatives*: storing the raw URL in the task at discovery time (breaks rollback parity, invalidates persisted payloads, and duplicates the same file under two identities in the registry); always using `HEAD` (silently substitutes default-branch content for the indexed revision, so a key that was removed after indexing would be reported missing for the wrong reason).

### D3 — A separate rate-limit resource class per transport, and `_service()` must learn the raw host

Add `SERVICE_TYPE_GITHUB_RAW = "github_raw"` to `constant/system.py` and extend `GitHubClient._service()` to recognize `raw.githubusercontent.com`. This is not cosmetic: `_service()` currently tests only for `"api.github.com"` and `"github.com"`, and the string `raw.githubusercontent.com` contains **neither** (`githubusercontent.com` ≠ `github.com`), so routing raw traffic through the client without this change yields `service = None` → `_limit` returns immediately → **no throttling at all**, reproducing disease D one level up while looking fixed.

Gather under `raw` gets its own bucket; under `rest` it uses `github_api` with a credential; under `html` it keeps `github_web`. Distinct classes matter because the budgets are genuinely separate and asymmetric — measured `code_search` at exactly **10/min** per token versus `core` at 5000/h — and gather must never consume search's budget. Buckets stay per-(service, credential) via the existing `credential_bucket_key`/`_ensure_bucket` machinery; no new limiter concept is introduced.

*Rejected alternative*: one process-wide GitHub brake. Rejected because it couples independent budgets — one abused surface freezes healthy ones for the full cooldown term, turning partial degradation into a full stop — and because 900 s was designed as a per-credential ceiling, not a global pause.

### D4 — Three refusal outcomes, kept strictly distinct

Today every gather fetch failure collapses into `TransientFetchError` → bounded requeue → loud drop. Introduce a typed `RateLimitDeferral` alongside it and map outcomes by signal, not by elapsed time:

| Signal observed | Classification | Outcome | Registry effect |
|---|---|---|---|
| `X-RateLimit-Remaining: 0` with a finite `reset`, or numeric `Retry-After` | primary quota exhaustion, wait **known and finite**, scoped to one credential | **DEFER**: row back to `pending`, `attempts` unchanged, bounded sleep ≤ `WAIT_CAP` | none (no gather recorded) |
| 429, or 403 whose body matches rate-limit markers | secondary/abuse limit, wait **unknown**, scoped to the actor | **DEFER** + stage-level pause + loud ERROR + `gather_secondary_limit` metric | none |
| 401, or 403 **without** rate-limit markers | genuine authentication failure | **LOUD DROP**, counted | failed gather |
| 404 (raw returns a 14-byte body) | dead link / deleted file | **LOUD DROP**, counted, existing `FileNotFoundError` path | failed gather |
| timeout, 5xx, connection reset | transient network | **BOUNDED REQUEUE** (`attempts++`, cap 3) — unchanged | failed gather |

Two properties are load-bearing. First, DEFER must not increment `attempts`: conflating defer with requeue burns the whole backlog at 12 requests per task, since a quota reset is not the task's fault. Second, DEFER needs a bounded stage pause, otherwise a deferred row is re-claimed immediately and hot-cycles. A natural ceiling already exists — `max_age_hours = 24` purges on the original `created_at` — so a deferred task cannot circulate forever.

This also fixes the 403 defect: `client.py:824-826` currently turns a secondary limit into a non-retryable `NetworkError`, costing one request, no requeue and no counter. The class client already knows how to distinguish it (`_is_http_rate_limited`, `client.py:626-628`); the gather path will use that judgment instead of the bare status code.

*Rejected alternative*: retry-through-everything with longer sleeps. Rejected because pressure on an actor-scoped secondary limit makes the situation worse, and because an unbounded wait inside a claimed durable row triggers D5.

### D5 — Hard invariant `WAIT_CAP < queue.visibility_timeout_s`; never sleep inside a claim beyond it

The durable queue has no claim-extend API, so any sleep longer than the visibility window causes the row to be swept back to `pending` and executed twice by two workers. With `visibility_timeout_s = 300` and a 900 s cooldown ceiling this is reachable today at `threads.gather = 8`. Enforce it in two places:

1. **Config validation**: if `gather.transport` implies waiting and the configured wait cap is ≥ `queue.visibility_timeout_s`, fail validation loudly naming both values (cross-section check in `config/validator.py`, aggregating into the existing funnel).
2. **Runtime clamp**: the effective sleep is `min(signalled_wait, WAIT_CAP)` where `WAIT_CAP` defaults to a fraction of the visibility timeout, and the remainder is expressed as a DEFER rather than a longer sleep.

*Rejected alternative*: adding claim renewal/heartbeat to `storage/task_queue.py`. It would solve the general case, but it enlarges a capability that shipped days ago and whose crash-recovery contract is freshly pinned; bounding the wait achieves the same guarantee for this change at a fraction of the surface. Recorded as a follow-up candidate, not an open question.

### D6 — The transform lives in the client fetch layer, not in the stage

`AcquisitionStage` keeps its current shape and gains only outcome mapping. Rationale: `collect()` already accepts `text=` and `extract()` is pure regex over text with no HTML awareness (`client.py:1494-1517`), so the stage has no business knowing which host served the bytes. A single new internal fetch helper resolves transport → URL → request → text, and `collect()` calls it in place of the bare `http_get` at `:1433`. Under `rest` the helper additionally base64-decodes the `content` field of the JSON response.

*Rejected alternative*: transforming URLs in `SearchStage` when emitting acquisition tasks. Rejected per D2 — it would persist transport-specific URLs and break rollback.

### D7 — Date extraction becomes transport-aware, and the NULL baseline is pinned not assumed

Under `html`, `_extract_file_commit_date` keeps being called exactly as today. Under `raw` there is no markup to parse, so the call is skipped and `metadata["file_commit_date"]` is `None` by construction; under `rest` the same, with the measured `last-modified` source documented but unused. The `date-extraction` fill-rate metrics stay in place as the drift detector they were designed to be.

The acceptance bar is comparative, not absolute: a test pins that the NULL share of `date_metrics` under the default transport is **no worse** than under `html`. Live capture shows it is already ~100% NULL on the HTML path (`datetime=` occurs 0 times on a current blob page), so this is a no-regression pin rather than a promise of dates.

### D8 — Payload size cap with streaming reads

Raw serves the file's actual bytes, so unlike a rendered blob page it has no practical upper bound: a minified bundle or a data dump can be hundreds of megabytes. Read the response in chunks and abort past a configurable cap (`gather.max_payload_bytes`, default 8 MiB), counting `gather_payload_truncated` and treating the truncated prefix as valid input to extraction (keys are line-local, so a prefix still yields findings) rather than as a failure. This is a new risk introduced by the transport switch and is handled here rather than discovered in production.

### D9 — `pipeline.threads.gather` is a politeness parameter, documented with its honest cost

Thread count is concurrency, not rate; the limiter owns rate. Recommended 8 → 2–4 while on `html`, relaxable under `raw` (which absorbed an 80-URL no-sleep burst with zero refusals). No code change; README and `examples/config-full.yaml` state the trade-off plainly — polite is slower.

### D10 — Scope cuts with rationale

1. **Credential-selector liveness** → `fix-credential-liveness`. The unbounded `while True` and the `mark_success` no-early-release behavior affect search and `repo_meta` too, so fixing them inside a gather-transport change would widen the blast radius over paths that currently work.
2. **Dead adaptive knobs** → same follow-up. Deleting them breaks operator configs that set them, so it needs a warn-period or a simultaneous config/examples edit.
3. **Cross-provider duplicate fetching** (all four configured providers share `key_pattern: sk-[a-zA-Z0-9]{32}`, and gather-skip decides per `(provider, patterns_hash)` at `storage/gather_skip.py:286`, so one URL can be fetched up to 4×) → research candidate R7. The duplicate share must be measured from the registry before it is formalized; note that `raw` cuts the price of each duplicate ~30×, which changes that cost-benefit.
4. **Automatic raw→rest fallback on 404.** Resolved here rather than left open: no fallback in this change. A raw 404 means the indexed revision is gone, which is the same fact an HTML 404 conveys today, and silently substituting default-branch content would report on a different file than the one that matched.

### D11 — Test strategy: offline by default, real network opt-in, repair before adding

Unit and integration tests run against captured payloads and injected failures, so the suite stays hermetic and fast. Real-network assertions (the ref matrix, the burst, the three-surface sizes) are recorded as captured evidence in this document and re-runnable through the retained probe scripts; a live gate in tasks.md repeats the decisive ones against production before an operator relies on the default.

Existing pins are **repaired, not deleted**: `tests/test_date_extraction.py` asserts on blob-HTML payloads (`_extract_file_commit_date` max-of-datetimes, the live-captured NULL case, the `no_relative_time` counter). Those assertions remain true and valuable for the `html` transport, so they are re-scoped to say so explicitly instead of being removed. `tests/test_fh_gather_fidelity.py` pins that a failed fetch never yields `gathered_ok`; it is extended with the DEFER case (no gather recorded at all) rather than rewritten.

## Risks / Trade-offs

- [Key-extraction fidelity could differ between rendered HTML and raw bytes] → `extract()` is pure `re.findall`, so the mechanism is unchanged, but HTML carries escaping (`&quot;`, `\u0026`) and page chrome that raw does not. Direction of risk is toward *more* clean matches, not fewer. Pinned by a test that runs the project's own `extract()` over both a captured HTML payload and the corresponding raw payload for the same file and requires the raw key set to be a superset; verified on real files during the live gate rather than assumed.
- [`HEAD` fallback silently serves newer content than the indexed revision] → counted (`gather_head_fallback`) and logged per URL; only reachable when the ref is not a 40-hex SHA, which the capture shows is not the case for API-sourced links.
- [Raw returns no rate-limit headers, so remaining budget is unknowable] → own token bucket plus refusal counters; operators who need visibility choose `rest`, which returns the full `x-ratelimit-*` set for 1 `core` unit per file.
- [`rest` spends 5000 `core` units/h, capping throughput at ~1.4 files/s] → `rest` is an explicit operator choice for header/date visibility, never the default; documented with the arithmetic.
- [CDN staleness on raw] → moot under D2: an exact commit SHA addresses immutable content, so a cache hit cannot serve a different revision.
- [Very large raw payloads] → D8 streaming cap with a truncation counter.
- [Deferral loops consuming queue churn] → bounded stage pause on defer plus the existing `max_age_hours = 24` purge as the natural ceiling.
- [Behavior drift on the paths that already work (search, `repo_meta`)] → `_service()` and the shared client are touched, so a pin asserts that authenticated search and `repo_meta` behavior with available credentials is unchanged; both keep their existing service classes.
- [Default `raw` changes behavior on upgrade] → accepted per D1 with the R1 precedent; `html` remains one config flip away and reproduces today's behavior byte-for-byte.

## Migration Plan

1. Land with `gather.transport` defaulting to `raw` and `html` fully intact as the rollback value; the automated suite proves both paths.
2. Live gate on the production-shaped config in a temp workspace (real credentials, secrets never committed): confirm per-transport byte and latency measurements reproduce the captured numbers, zero unclassified refusals, `gather_secondary_limit` and `gather_head_fallback` observed, `date_metrics` NULL share not worse, and the durable-queue integrity counters still zero.
3. Operator tuning: lower `pipeline.threads.gather` only if refusals persist under `raw`; consider `rest` where header visibility is worth 1 `core` unit per file.
4. Rollback: set `gather.transport: html` and restart. No data migration in either direction, because persisted tasks still carry blob URLs (D2).

## Open Questions

(none — every spec-shaping decision is resolved above; the raw→rest fallback question is answered in D10.4 and the claim-renewal question in D5.)
