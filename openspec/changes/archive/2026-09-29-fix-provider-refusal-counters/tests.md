# Tests: fix-provider-refusal-counters

## Approach

TDD per the `tdd-flow` schema: every new scenario is pinned **before** the producer exists, so the
pin is RED for the right reason (counter stays at 0 / signal absorbed / vocabulary duplicated) and
turns GREEN only when the implementation lands. The two strengthened pins (PRT-S9, PRT-S6) are
rewritten first as well: the PRT-S9 rewrite must be RED against shipped code, because that is exactly
the weakness that let W1 through.

## Harness

- **Loopback wire server.** `ThreadingHTTPServer` with a scripted handler that counts hits
  (`handler.hits`), so "the transport retried N times" is measured, not inferred. Same pattern as
  `tests/test_pfc_signals.py` / `tests/test_pfc_defer.py`.
- **Session isolation.** `monkeypatch.setattr(sc, "_HTTP_SESSION", isolated_session_with_trust_env_False)`
  so no proxy/env leakage reaches the loopback server.
- **Counter isolation.** `sc.reset_provider_refusal_stats()` before each case; assertions are on
  deltas read via `sc.get_provider_refusal_stats()`.
- **Real stages.** PRT-S9 and the worker half of PRT-S6 drive a real `InspectStage` /
  `AcquisitionStage`-shaped worker through `_resources(...)` from `tests/test_fh_stage_modes.py`,
  with `_registry`/`_rows`/`PROVIDER` from `tests/test_fh_gather_fidelity`, as
  `tests/test_pfc_check_starve.py` already does. Providers used by these pins subclass
  `core.types.IProvider` and implement `result`/`get_patterns`/`check`, otherwise the worker takes the
  unknown-provider path (PFC-D12).
- **Transport pins (PRT-S17).** Each of the five shipped providers is instantiated with a dummy
  credential and pointed at the loopback server, which answers `429` + `Retry-After: 30`; the pin
  asserts `RateLimitDeferral` escapes `_fetch_models` / `inspect` rather than `[]` being returned.
- **Fixture provenance.** No live quota or capacity refusal was ever captured: the PFC-D11 probe of
  all four production providers returned HTTP 401 `invalid_api_key` with no `Retry-After` and no
  `X-RateLimit-*` (endpoint list, capture date 2026-09-28, auth mode: deliberately invalid key).
  Every quota body used below is therefore a **synthetic supplementary unit vector**, transcribed from
  the marker strings already shipped in `provider/*._judge`, and is labeled as such in the test
  module docstring.

## Traceability

| Scenario | Pin | File |
|---|---|---|
| PRT-S1…S5 *(preserved)* | existing pins unchanged | `tests/test_pfc_signals.py` |
| PRT-S6 transport half *(preserved)* | `test_s6_transport_carries_the_wait_and_does_not_sleep_it` | `tests/test_pfc_defer.py` |
| PRT-S6 worker half *(new — W5)* | `test_s6_the_worker_defers_on_a_transport_produced_signal_with_the_wait_clamped` | `tests/test_pfc_defer.py` |
| PRT-S7 *(preserved)* | `test_s7_wait_is_clamped_by_the_configured_cap`, `tests/test_pfc_config.py` | unchanged |
| PRT-S8 *(preserved)* | the three `test_s8_*` pins | `tests/test_pfc_defer.py` |
| PRT-S9 *(strengthened — W4)* | `test_s9_refusal_and_empty_answer_are_counted_apart_and_neither_is_an_error` rewritten against a real `InspectStage`, asserting `stage.total_errors == 0` | `tests/test_pfc_defer.py` |
| **PRT-S24** *(new — W1 end to end, D9)* | `test_s24_a_swallowed_refusal_is_never_recorded_as_an_empty_answer` — a wire 401 through a real `OpenAILikeProvider` and a real `InspectStage`: `refusals_auth >= 1`, `inspect_refused >= 1`, `inspect_empty_answers == 0`, `total_errors == 0` | `tests/test_pfc_defer.py` |
| PRT-S10…S12 *(preserved)* | existing pins unchanged | `tests/test_pfc_defer.py`, `tests/test_pfc_render.py` |
| PRT-S13, S14 *(preserved)* | existing pins unchanged | `tests/test_pfc_check_starve.py`, `tests/test_pfc_config.py` |
| **PRT-S15** *(new — W3)* | `test_s15_a_marked_403_with_no_published_wait_keeps_the_legacy_transient_exception` — asserts `ConnectionError`, message `Rate limit exceeded (HTTP 403)`, **3** wire hits, `refusals_transient == 1` | `tests/test_prc_classes.py` |
| **PRT-S16** *(new — S1)* | `test_s16_the_marker_vocabulary_has_exactly_one_definition` — identity of the shared constant, verdict agreement table, and absence of a second private copy in `search/client.py` | `tests/test_prc_vocabulary.py` |
| **PRT-S17** *(new — W2)* | `test_s17_every_shipped_transport_lets_the_signal_escape` — parametrised over `openai_like`, `anthropic`, `gemini`, `vertex`, `bedrock` | `tests/test_prc_transports.py` |
| **PRT-S18** *(new — W1)* | `test_s18_a_quota_refusal_is_counted_as_quota_and_keeps_its_legacy_exception` — parametrised: 403 `exceeded_current_quota_error`; 429 `insufficient_quota` with no published wait | `tests/test_prc_classes.py` |
| **PRT-S19** *(new — W1)* | `test_s19_an_auth_refusal_is_counted_as_auth_and_keeps_its_legacy_exception` — parametrised: 401; bare 403 | `tests/test_prc_classes.py` |
| **PRT-S20** *(new — W1)* | `test_s20_a_transient_refusal_is_counted_as_transient` — parametrised: 429 no wait; 503 | `tests/test_prc_classes.py` |
| **PRT-S21** *(new — W1)* | `test_s21_exactly_one_class_per_outcome` — parametrised: 429 + `Retry-After` + quota marker → rate-limit; 404 → no class; 400 → no class; genuine empty 200 → empty answer only | `tests/test_prc_classes.py` |
| **PRT-S22** *(new — D1)* | `test_s22_counting_never_re_classifies_a_legacy_path` — full status table of (type, message, hits) asserted equal to the pre-change recorded values | `tests/test_prc_classes.py` |
| **PRT-S23** *(new — D4)* | `test_s23_with_the_flag_off_no_refusal_class_is_counted` | `tests/test_prc_classes.py` |
| **PRT-S25** *(new — D11 boundary)* | `test_s25_a_harvested_credential_failure_is_not_a_provider_refusal` — a 401 on the check surface (`chat`) leaves every `refusals_*` counter at zero while still returning its own reason code | `tests/test_prc_classes.py` |
| harness stability *(W6, D8)* | `test_s18_a_deferred_task_is_never_executed_twice` — no new assertion; the `consumer()` clause is narrowed. Verified by repeated runs, not by a pin | `tests/test_gt_stage_integration.py` |

## Expected RED

Before implementation, these must fail for the stated reason:

| Pin | RED reason |
|---|---|
| `test_s15_…` | `refusals_transient` stays 0 (no producer) |
| `test_s16_…` | `LIMIT_MARKERS` not exported; `search/client.py` still holds a private list |
| `test_s17_…[vertex]`, `[bedrock]` | signal absorbed → `[]` returned |
| `test_s18_…`, `test_s19_…`, `test_s20_…`, `test_s21_…`, `test_s23_…` | the three counters have no producer at all |
| `test_s25_…` | passes immediately by design — it is a **boundary guard** holding D11's scope, expected GREEN before and after |
| `test_s22_…` | passes immediately by design — it is a **regression guard**, and is expected GREEN both before and after; recorded here so it is not mistaken for a missing RED |
| rewritten `test_s9_…` | passes immediately by design: `inspect_refused` already has a producer (PFC-D8), so driving a *deferrable* refusal through the stage was never the gap. The gap is the non-deferrable refusal, which `test_s24_…` below covers |
| `test_s24_a_swallowed_refusal_is_never_recorded_as_an_empty_answer` | `refusals_auth` stays 0 and the 401 lands in `inspect_empty_answers` — W1 seen from the stage, end to end |
| `test_s6_the_worker_defers_…` | no worker-level path exercised yet |

Expected GREEN count: the suite grows from **377** to **377 + N**, where N is the number of new test
functions and parametrisations; N is recorded in `verification.md` once measured (PFC-D13 precedent:
state the number, do not assume it).

## Non-goals

- No pin asserts a specific quota **verdict** on the `check` surface; `_judge` behaviour is untouched.
- No pin covers `provider/vertex.py` or `provider/bedrock.py` against a real cloud endpoint — neither
  is in the live config and no credentials for them exist on this host.
- The flake fix (D8) adds no assertion; its evidence is repeated-run stability.
