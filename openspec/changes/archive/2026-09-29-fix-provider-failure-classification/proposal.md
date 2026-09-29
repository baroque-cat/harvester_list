# Proposal: fix-provider-failure-classification

## Why

The pipeline now classifies a **capacity refusal** correctly on two surfaces — the GitHub
credential pool (`fix-credential-liveness`) and gather's own token basket
(`fix-throttle-deferral-and-metrics`) — but the **LLM-provider surface still classifies it as a
task failure or swallows it silently**. Two independent defects remain, and they violate the same
contract: *a refusal caused by capacity is not a fault of the task; it must be deferred, counted by
class, and must never burn a retry attempt.*

1. **D10 — our own provider basket starves the `check` stage into failure-empties.**
   `stage/definition.py:720-738`: when `limiter.acquire(get_service_name(task.provider))` fails
   twice, the worker sleeps an **unclamped** `wait_time` inside the claim and then raises
   `TransientFetchError("provider limiter starved for provider: …")`. That is a failure-empty: it
   burns an attempt and requeues. The basket is *ours* (`base_rate 2.0`, `burst_limit 10`, per task),
   so this is the exact pathology R5.3 removed from gather — on the second surface R5.3 explicitly
   declared out of scope.
2. **Ф7 — a provider's capacity refusal is misread as an authentication failure, then erased.**
   `search/client.py:http_get` classifies by status code alone: `401|403 →
   NetworkError("Authentication failed (HTTP {code})")`, which `tools/retry.py:39-67` does **not**
   retry, and which `@handle_exceptions(default_result=[])` on `_fetch_models`
   (`provider/openai_like.py:135`, `provider/anthropic.py:157`) turns into `[]` — indistinguishable
   from "this provider has no models". `provider/gemini.py` and `provider/vertex.py` have no wrapper,
   so the exception reaches `InspectStage._inspect_worker`'s catch-all
   (`stage/definition.py:933-935`) → ERROR + `return None`: the task is consumed with **no retry, no
   requeue, no drop counter**. `provider/bedrock.py:211-241` converts a 403 into `(500, str(e))`,
   losing the signal twice. Meanwhile the *gather* surface already classifies this same wire signal
   correctly (`_gather_is_rate_limit_signal`: 429, or 403 carrying `rate limit|abuse detection|
   secondary rate limit`), so **one signal gets two different verdicts depending on which surface
   reads it**.

### Evidence and provenance

| Claim | Evidence | Status |
|---|---|---|
| D10 fires in production shape | `provider limiter starved` — **558 occurrences** across the R5.3 gate captures (`/var/tmp/opencode-tdm-gate/*/evidence/*.stdout`, 2026-09-28/29, real pipeline, production `config.yaml`): `qwen-intl-dash` 262, `qwen-china-dash` 256, `qwen-maas` 40. Per 600 s run: **57** (basket 2.0/4, `threads.gather` 8) and **102** (4.0/8, 4). Every one burned an attempt and requeued. | **live-captured** |
| `http_get` misclassifies 403-with-limit-text and never reads `Retry-After` | Controlled probe of **our own classifier**: `http.server` on `127.0.0.1:35113`, 2026-09-29 01:52:26, **auth mode: none**, body `{"error": {"message": "rate limit exceeded, please retry after 30 seconds"}}`, `Retry-After: 30` on the 429. Result: `403 → NetworkError("Authentication failed (HTTP 403)")`, not retried, 1 hit; `429 → ConnectionError("Rate limit exceeded (HTTP 429)")`, retried **3×** at 0.10 s/0.22 s with `Retry-After` never read; `503 + the same limit body → ConnectionError("Server error (HTTP 503): …")`, retried 3× and classified as a server error. | **probe of our code**, not of a provider |
| What the four configured providers (`dashscope`, `dashscope-intl`, `maas.qwencloudapi`, `api.deepseek`) actually return on quota exhaustion or throttling — status, headers, body shape | **No live third-party sample exists in this repository.** | **UNKNOWN — must be probed before any spec scenario asserts a provider body** (artifact rule: *probe before spec*) |

The third row is a hard constraint on this change: no scenario may hard-code a provider's error
shape until it has been captured from that provider. The taxonomy must therefore be built on
**signal classes we can recognise without knowing a provider's dialect** (status code + limit
markers in headers/body + a published wait), exactly as the gather surface already does, and the
live probe is the first task.

## What Changes

- **One signal predicate for the whole codebase.** Consolidate the three existing copies of the
  wait/limit parsing (`_is_http_rate_limited` + `_wait_from_headers` + `_wait_from_content` at
  `search/client.py:716-780`, and `_gather_wait_from_headers` / `_gather_wait_from_content` /
  `_gather_is_rate_limit_signal` at `:1276-1327`) into a single implementation used by both
  surfaces. No fourth copy. Gather's observable behavior must not change.
- **A capacity refusal becomes a typed deferral, not an in-thread sleep.** `http_get` publishes the
  wait it read (`Retry-After`, `X-RateLimit-Reset`, or a body marker) on a typed signal so the
  *stage* can DEFER through the existing seam. Sleeping inside `http_get` is forbidden: the retry
  decorator already sleeps in the calling thread, and invariant 6 caps any in-claim sleep below
  `queue.visibility_timeout_s` (300 s) because the durable queue has no claim-renewal API.
- **D10 fixed on the `check` surface.** Provider-basket starvation raises `RateLimitDeferral` with
  the basket's own refill time (floor `1/base_rate`, capped), counted in a **new, separate**
  counter, with **no** report to the adaptive budget; the unclamped `time.sleep(wait_time)` at
  `stage/definition.py:727` is bounded or removed. Identity (`attempts`, `created_at`, dedup) is
  preserved.
- **The silence is removed.** `@handle_exceptions(default_result=[])` is narrowed through its
  existing `exclude=` parameter so the typed signal propagates while a genuinely empty answer still
  yields `[]`; `InspectStage._inspect_worker` no longer returns `None` without an outcome — it
  distinguishes "provider answered: no models" from "provider refused", counts both by class, and
  defers or drops loudly. `bedrock._send_request` stops rewriting a 403 into a 500.
- **Refusals become observable by class.** A new published metric surface counts provider refusals
  by class (quota / auth / rate-limit / transient / own-basket-starved) and is **registered in
  `state/display.py::_RENDERED_METRIC_SURFACES` with a renderer** — the RO-S4 pin enumerates
  `PipelineStatus` fields ending in `_metrics`, so an unrendered surface fails the suite.
- **No secret in log lines.** `stage/definition.py:933-935` logs the whole `task` object, while
  `_generate_id` (`:886-895`) deliberately hashes the same secret because "task ids are rendered in
  log lines". The error path switches to the hashed id.
- **Rollback is a flag flip** (house invariant 4): one boolean in `config.yaml` restores the legacy
  classification byte-for-byte — including the failure report and the burned attempt — so a
  half-rollback cannot keep the destructive half. Non-boolean values are rejected loudly by both
  `__post_init__` and `ConfigValidator`.
- **Non-changes:** `provider.check()` and every `_judge()` verdict (the defect creates no false
  "key invalid" today and must not start to); the gather transport's observable behavior; the search
  boundary's failure-empty taxonomy (R5.3 D11); the three-outcome worker contract in
  `stage/base.py:662-719`.

## Capabilities

### New Capabilities
- `provider-refusal-taxonomy`: how a capacity refusal on an LLM-provider call is recognised,
  signalled, bounded, deferred and counted — one signal predicate shared with the gather surface, a
  typed deferral carrying a published wait, per-class counters, and an operator renderer.

### Modified Capabilities
- `failure-handling`: the empty-result taxonomy gains the provider surface. A capacity refusal
  (remote 429/403-with-limit-text, or starvation by our own provider basket) is a **deferral**, not
  a failure-empty; it preserves identity and burns no attempt. The existing scenario that pins
  check-stage starvation as a requeue is **superseded** by this change.
`run-observability` is **exercised but not modified**: its completeness requirement already ranges
over every `PipelineStatus` field ending in `_metrics`, so adding the new surface automatically falls
under it (the structural pin enumerates the dataclass fields), and the surface-specific rendering
scenarios live in `provider-refusal-taxonomy` (PRT-S11) rather than duplicating that requirement. No
delta file is created for it.

## Impact

- **Code:** `search/client.py` (`http_get` classification + helper consolidation),
  `provider/{base,openai_like,anthropic,gemini,vertex,bedrock}.py`, `stage/definition.py`
  (`CheckStage._check_worker`, `InspectStage._inspect_worker`), `core/exceptions.py` (typed signal),
  `core/metrics.py` (`PipelineStatus` surface), `state/display.py` (renderer + registry),
  `manager/pipeline.py` (wiring), `config/{schemas,loader,validator}.py` (rollback flag).
- **Tests:** baseline is **343 passed** / 16 main specs / `openspec validate --specs --strict` →
  16 passed, 0 failed. `tests/test_fh_stage_modes.py::test_s9_starved_check_requeues_instead_of_dropping`
  pins exactly the behavior this change replaces and **must be deleted or rewritten**, not kept
  green by weakening the change; any other pin asserting `TransientFetchError` from provider-basket
  starvation goes the same way. This is the house TDD rule: obsolete tests are removed in the same
  pass.
- **Live gate:** needs the four configured providers. Run roots under `/var/tmp`, `setsid nohup`
  from a script file, `timeout -k 30 <N+300>`, credentials read at runtime and masked in evidence.
  **No provider limit may be provoked deliberately** (`AGENTS.md` §3): refusal evidence comes from
  organic windows only, and the probe uses the smallest request that answers the format question.
- **Risk:** a typed signal that propagates through `@handle_exceptions` changes what `inspect()`
  returns. The dominant risk is turning a refusal into a verdict — mitigated by pinning `check()`
  and `_judge()` as unchanged, and by making the new signal a sibling of `RateLimitDeferral` rather
  than a subclass of anything `_judge` inspects.
