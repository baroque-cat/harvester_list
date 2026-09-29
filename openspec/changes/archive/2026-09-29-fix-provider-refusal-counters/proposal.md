# Change: Produce every declared provider-refusal class and let the signal escape every transport

## Why

`fix-provider-failure-classification` (archived `2026-09-29`, commit `698bbf5`) shipped the
`provider-refusal-taxonomy` capability. Post-archive verification found the implementation delivers
less than its own specification states:

1. **Three of the seven declared counters have no producer.** `refusals_quota`, `refusals_auth` and
   `refusals_transient` are declared in `_PROVIDER_REFUSAL_STAT_KEYS`, rendered, registered — and
   never incremented anywhere. Measured through shipped code on a loopback provider
   (`/tmp/opencode/pfc_verify_auth_path.py`): a 403 auth refusal, a 403 quota refusal, a 429 with no
   published wait and a 503 all return `[]` from `_fetch_models` and are therefore recorded as
   `inspect_empty_answers`. That is precisely the defect the capability exists to remove — its
   requirement says *"A refusal SHALL never be recorded as 'the provider has no models'"* — and it
   survives only because `@handle_exceptions(default_result=[], exclude=(RateLimitDeferral,))`
   lets the typed signal through while still swallowing `NetworkError`/`ConnectionError` into `[]`.
   The indistinguishability was eliminated **only** for capacity refusals that publish a wait.

2. **Two shipped transports absorb the typed signal.** `provider/vertex.py` calls `http_get` at
   `:304` and `:342`, both inside a `try:` whose `except Exception` (`:325`, `:353`) converts the
   deferral into `continue`; `provider/bedrock.py` re-raises it correctly out of `_send_request`
   (`:240`) but the calling `inspect` swallows it at `:493-495` into `return []`. PRT-S8 and the
   predecessor's tasks 5.3/5.4 therefore hold for `openai_like`, `anthropic` and `gemini` only. The
   predecessor proposal's claim that "gemini and vertex have no wrapper" is false for vertex.

3. **One behaviour change is undocumented and unpinned.** A 403 carrying a limit marker with **no**
   published wait now yields `ConnectionError: Rate limit exceeded (HTTP 403)` after 3 wire hits;
   before the change it yielded `NetworkError: Authentication failed (HTTP 403)` after 1 hit. The
   resolution is correct (PRT-S2 forbids the authentication verdict) but it contradicts the
   predecessor's task 4.1 ("everything else keeps its current exception type and message"), is
   recorded only as a code comment, and no scenario pins it — PRT-S2 uses a 403 *with* `Retry-After`,
   PRT-S3 a 403 *without* a marker.

4. **Two pins are weaker than `tests.md` specifies**, which is why (1) was not caught offline:
   PRT-S9's pin increments the counters by hand instead of driving outcomes through a real
   `InspectStage` and never asserts `total_errors == 0`; PRT-S6's pin never drives the signal
   through a worker to `defer_task`.

5. **The limit-marker vocabulary still exists twice** (`tools/http_signals.py:25` and
   `search/client.py:631-636`). Duplicated vocabularies diverging was the root cause of the original
   defect; the predecessor's D1 consolidated three copies and missed this one.

6. **The suite is not 100 % stable.** `tests/test_gt_stage_integration.py::
   test_s18_a_deferred_task_is_never_executed_twice` flakes ~1-in-8 full runs. Attribution is proven
   pre-existing (the predecessor commit changed 0 lines of `AcquisitionStage`, and
   `storage/task_queue.py`, `stage/base.py`, `manager/queue.py`, `tests/conftest.py` and the test
   file are byte-identical to `20c15ce`); the mechanism is the harness itself — `consumer()` wraps
   `queue.get(timeout=0.2)` in `except Exception: return`, so any transient queue error silently ends
   both threads and the victim never executes.

## What Changes

- **Produce all seven declared classes.** Add producers for `refusals_auth`, `refusals_transient`
  and `refusals_quota` in the provider transport, gated by the existing rollback flag.
  **Counting is strictly observational**: no exception type, message, retry count or deferral
  decision changes on any path. This is stated as a new invariant in the requirement text so it is
  testable rather than implied.
- **Consolidate the quota vocabulary the same way D1 consolidated the limit vocabulary.** The quota
  markers are *not* a newly assumed dialect: they are transcribed from the five already-shipped
  `_judge` methods (`provider/openai_like.py:123,127`, `provider/anthropic.py:151`,
  `provider/gemini.py:72`, `provider/vertex.py:158`). The loose single-word tokens those methods use
  in status-scoped positions (`quota`, `billing`, `purchase`, `RESOURCE_EXHAUSTED`) are deliberately
  **excluded** — see design D3.
- **Export the marker vocabulary** from `tools/http_signals.py` and make
  `GitHubClient.is_rate_limited_content` consume it, so a future vocabulary change is one edit.
- **Let the typed signal escape every shipped transport**: `except RateLimitDeferral: raise` before
  the broad handler at `provider/vertex.py:325`, `:353` and `provider/bedrock.py:493`.
- **Pin the unpinned 403 path** and record the deviation as predecessor decision **D17** in the
  archived `design.md`, as the design rule requires.
- **Strengthen the two weak pins** to what `tests.md` already demanded: PRT-S9 driven through a real
  `InspectStage` with `total_errors == 0`; PRT-S6 driven through a real worker to `defer_task` with
  the clamp asserted.
- **Fix the flaky harness** so a transient queue error is visible instead of silently ending the
  consumer thread.
- **Correct the predecessor's D6 wording** (S2): it claims the undeclared-key guard behaves "exactly
  like `_gather_stat_inc`", but that helper logs a WARNING and returns while
  `_provider_refusal_stat_inc` raises `ValueError` — the stricter behaviour is what the pin requires.

## Impact

- Affected specs: `provider-refusal-taxonomy` (MODIFIED requirements 1, 2, 3 and 4; scenarios
  PRT-S15…PRT-S24 appended — IDs are append-only, nothing renumbered or reused).
- Affected code: `tools/http_signals.py`, `search/client.py`, `provider/vertex.py`,
  `provider/bedrock.py`, `tests/test_pfc_defer.py`, `tests/test_pfc_signals.py`,
  `tests/test_gt_stage_integration.py`, plus new `tests/test_prc_*.py`.
- No configuration surface changes: `provider.classify_refusals` and `provider.max_refusal_wait_s`
  keep their names, defaults and validation. No new flag is introduced — the new producers ride the
  existing rollback flag, so `classify_refusals: false` restores the pre-capability behaviour
  *including* silence of the new counters.
- No wire-format assumption is added without provenance: the D11 live probe of all four production
  providers returned 401 `invalid_api_key` with no `Retry-After`/`X-RateLimit-*`, and **no capacity or
  quota refusal was observed live**. Quota fixtures are therefore labeled synthetic supplementary
  unit vectors, as the `tasks` rule requires, and the quota rule is written as an opportunistic
  contract (marker present → count as quota; absent → fall through unchanged).
- Production exposure today is unchanged in behaviour and improved in observability: the live config
  runs four `openai_like` tasks, whose transport already propagated the signal.
