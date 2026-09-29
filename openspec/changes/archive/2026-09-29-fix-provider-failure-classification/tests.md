# Test Plan

<!-- Derived mechanically from the delta specs under specs/. One spec scenario
     = exactly one automated test case. Scenario IDs are stable: never
     renumber, append only. -->

House conventions applied: real objects over mocks wherever the real collaborator is cheap
(a real `RateLimiter`, a real `TokenBucket`, a real `ConfigValidator`, a real local
`http.server` for wire behavior); a stubbed HTTP session only at the network boundary; no test
asserts on prose. IDs for the modified `failure-handling` requirement use `FH4-*` because
"Check tasks survive limiter starvation" is the 4th requirement of the main spec.

## Traceability

| Scenario ID | Spec | Requirement | Scenario | Test file | Status |
|-------------|------|-------------|----------|-----------|--------|
| `PRT-S1` | `specs/provider-refusal-taxonomy/spec.md` | Capacity refusals are recognised by wire signal | a throttled call carrying a published wait defers with that wait | `tests/test_pfc_signals.py::test_s1_published_wait_defers_instead_of_blind_retry` | GREEN |
| `PRT-S2` | `specs/provider-refusal-taxonomy/spec.md` | Capacity refusals are recognised by wire signal | a 403 carrying a limit marker is a capacity refusal, not an auth failure | `tests/test_pfc_signals.py::test_s2_forbidden_with_limit_marker_is_capacity_not_auth` | GREEN |
| `PRT-S3` | `specs/provider-refusal-taxonomy/spec.md` | Capacity refusals are recognised by wire signal | a 403 with no limit marker stays an authentication failure | `tests/test_pfc_signals.py::test_s3_forbidden_without_marker_stays_an_auth_failure` | GREEN |
| `PRT-S4` | `specs/provider-refusal-taxonomy/spec.md` | Capacity refusals are recognised by wire signal | a refusal with no published wait keeps the legacy transient path | `tests/test_pfc_signals.py::test_s4_rate_limit_without_published_wait_keeps_legacy_transient` | GREEN |
| `PRT-S5` | `specs/provider-refusal-taxonomy/spec.md` | Capacity refusals are recognised by wire signal | one recognition rule, two surfaces, no divergence | `tests/test_pfc_signals.py::test_s5_both_surfaces_classify_the_same_response_identically` | GREEN |
| `PRT-S6` | `specs/provider-refusal-taxonomy/spec.md` | A published wait is handed to the stage | the wait travels on the signal instead of blocking the transport | `tests/test_pfc_defer.py::test_s6_transport_carries_the_wait_and_does_not_sleep_it` | GREEN |
| `PRT-S7` | `specs/provider-refusal-taxonomy/spec.md` | A published wait is handed to the stage | the wait is capped below the visibility window | `tests/test_pfc_defer.py::test_s7_wait_is_clamped_by_the_configured_cap`, `tests/test_pfc_config.py::test_s7_defaults_are_the_fixed_behavior_and_the_cap_is_bounded`, `::test_s7_cap_not_below_the_visibility_window_is_rejected_loudly`, `::test_s7_non_positive_cap_fails_in_both_guards` | GREEN |
| `PRT-S8` | `specs/provider-refusal-taxonomy/spec.md` | A published wait is handed to the stage | the signal is neither retried nor swallowed | `tests/test_pfc_defer.py::test_s8_retry_policy_rejects_the_signal_by_type_not_by_words`, `::test_s8_fail_open_wrapper_lets_the_signal_through`, `::test_s8_fetch_models_propagates_a_refusal_end_to_end` | GREEN |
| `PRT-S9` | `specs/provider-refusal-taxonomy/spec.md` | Refusals are counted by class | per-class counters distinguish refusal from empty answer | `tests/test_pfc_defer.py::test_s9_refusal_and_empty_answer_are_counted_apart_and_neither_is_an_error` | GREEN |
| `PRT-S10` | `specs/provider-refusal-taxonomy/spec.md` | Refusals are counted by class | starvation by our own basket is counted apart from a remote refusal | `tests/test_pfc_defer.py::test_s10_own_basket_starvation_is_counted_apart_from_remote_refusals` | GREEN |
| `PRT-S11` | `specs/provider-refusal-taxonomy/spec.md` | Refusals are counted by class | the refusal surface is rendered and registered | `tests/test_pfc_render.py::test_s11_the_surface_is_rendered_with_every_declared_key`, `::test_s11_empty_or_absent_surface_renders_nothing`, `::test_s11_the_surface_is_registered_for_completeness`, `::test_s11_unexpected_keys_are_ignored_and_malformed_figures_degrade`, `::test_s11_compact_output_is_unchanged` | GREEN |
| `PRT-S12` | `specs/provider-refusal-taxonomy/spec.md` | Refusals are counted by class | no credential material in the refusal path | `tests/test_pfc_render.py::test_s12_a_refusal_is_logged_by_hash_and_never_by_secret` | GREEN |
| `FH4-S1` | `specs/failure-handling/spec.md` | Check tasks survive limiter starvation | Starved check defers without burning an attempt | `tests/test_pfc_check_starve.py::test_fh4_s1_starved_check_defers_without_burning_an_attempt`, `::test_fh4_s1_no_unclamped_sleep_inside_the_claimed_task` | GREEN |
| `FH4-S2` | `specs/failure-handling/spec.md` | Check tasks survive limiter starvation | Starvation is counted apart from a remote refusal | `tests/test_pfc_check_starve.py::test_fh4_s2_starvation_is_counted_apart_and_reports_nothing_to_the_budget` | GREEN |
| `FH4-S3` | `specs/failure-handling/spec.md` | Check tasks survive limiter starvation | Legacy starvation classification returns with the rollback flag | `tests/test_pfc_check_starve.py::test_fh4_s3_legacy_starvation_returns_with_the_rollback_flag` | GREEN |
| `PRT-S14` | `specs/provider-refusal-taxonomy/spec.md` | Rollback is a configuration flip | a non-boolean flag is rejected loudly | `tests/test_pfc_config.py::test_s14_non_boolean_flag_is_rejected_loudly_without_coercion`, `::test_s14_flag_is_a_real_configuration_surface` | GREEN |
| `PRT-S13` | `specs/provider-refusal-taxonomy/spec.md` | Rollback is a configuration flip | the legacy classification returns with the flag | `tests/test_pfc_check_starve.py::test_prt_s13_flag_off_restores_the_legacy_transport_classification` | GREEN |

## Automated

### File: `tests/test_pfc_signals.py`

Describe: `provider-refusal-taxonomy — wire-signal recognition (PRT-S1..S5)`

The module under test (`tools/http_signals.py`) does not exist yet; the resulting `ImportError` is
part of the expected RED state. Harness: a real `http.server.HTTPServer` bound to `127.0.0.1` on an
ephemeral port, driven through the **shipped** `search.client.http_get` with
`search.client._HTTP_SESSION` left real (no external traffic, no GitHub involvement). Each case
asserts the exception *type*, the carried wait and the reason class — never a message substring
alone, because message text is exactly the fragile coupling design D5 removes.

- [x] `PRT-S1` — it("a throttled call carrying a published wait defers with that wait") <!-- WHEN the server answers 429 with `Retry-After: 30` THEN `http_get` raises `RateLimitDeferral` with `wait_s == 30.0` and `reason == ErrorReason.RATE_LIMITED.name`, and the server was hit **once** (no blind 3× retry) -->
- [x] `PRT-S2` — it("a 403 carrying a limit marker is a capacity refusal, not an auth failure") <!-- WHEN the server answers 403 with body `{"error":{"message":"rate limit exceeded"}}` THEN `RateLimitDeferral` with `reason == RATE_LIMITED`, and the raised type is NOT `NetworkError`-with-"Authentication failed"; the same body given to `tools.http_signals.is_capacity_refusal(403, body)` returns True -->
- [x] `PRT-S3` — it("a 403 with no limit marker stays an authentication failure") <!-- WHEN the server answers 403 with body `{"error":{"message":"invalid api key"}}` THEN `NetworkError` is raised, its message still starts with "Authentication failed", no `RateLimitDeferral`, and `is_capacity_refusal(403, body)` is False -->
- [x] `PRT-S4` — it("a refusal with no published wait keeps the legacy transient path") <!-- WHEN the server answers 429 with no `Retry-After`, no reset header and a body without a wait THEN a retryable `ConnectionError` is raised (not `RateLimitDeferral`), the server is hit `retries` times, and `RetryCore.should_retry_error` returns True for it -->
- [x] `PRT-S5` — it("one recognition rule, two surfaces, no divergence") <!-- WHEN the identical (status, body, headers) triple is offered to the provider path and to the gather path THEN `is_capacity_refusal` gives the same verdict as `search.client._gather_is_rate_limit_signal` for every case in a parametrised table (429 plain, 403+marker, 403 bare, 403+`secondary rate limit`, 500), and `wait_from_headers` equals `_gather_wait_from_headers` for `Retry-After` digits, an HTTP-date and `x-ratelimit-reset` — i.e. the two surfaces are delegations of one rule -->

### File: `tests/test_pfc_defer.py`

Describe: `provider-refusal-taxonomy — bounded hand-off, guards and counters (PRT-S6, S8, S9, S10)`

- [x] `PRT-S6` — it("the wait travels on the signal instead of blocking the transport") <!-- WHEN a 429 with `Retry-After: 30` is served and the call is timed THEN the elapsed wall time of `http_get` is < 1.0 s (the transport did not sleep the wait), the signal carries `wait_s == 30.0`, and driving the same signal through a real `AcquisitionStage`-shaped worker results in `defer_task` being reached with a wait clamped to the cap -->
- [x] `PRT-S8` — it("the signal is neither retried nor swallowed") <!-- WHEN `RateLimitDeferral("provider rate limit …")` is passed to `RetryCore.should_retry_error(err, 0, 3)` THEN it returns False **by type** (a control case proves a plain `NetworkError` with the same words also returns False only via the class rule, and a `ConnectionError` containing "rate limit" still returns True); and WHEN a function decorated `@handle_exceptions(default_result=[], exclude=(RateLimitDeferral,))` raises it THEN the signal propagates instead of returning `[]`; finally `provider.openai_like._fetch_models` propagates it end-to-end against the local 429 server -->
- [x] `PRT-S9` — it("per-class counters distinguish refusal from empty answer") <!-- WHEN one inspect call is refused with a published wait and another returns `{"data": []}` THEN `refusals_rate_limit == 1`, `inspect_refused == 1`, `inspect_empty_answers == 1`, `refusals_auth == 0`, and neither outcome advanced `total_errors` on the driving stage; an undeclared key passed to the increment helper raises loudly -->
- [x] `PRT-S10` — it("starvation by our own basket is counted apart from a remote refusal") <!-- WHEN a check task is starved by its own provider basket (real `RateLimiter`, `acquire` exhausted) THEN `deferred_provider_budget == 1` while `refusals_rate_limit == 0`, and the bucket's `consecutive_failures` is still 0 and `rate` unchanged (nothing was reported to the adaptive budget) -->

### File: `tests/test_pfc_config.py`

Describe: `provider-refusal-taxonomy — cap, validation and loud rejection (PRT-S7, S14)`

Uses the house pattern from `tests/test_gt_config.py::_valid_base_config()` so the global validator
(credentials, user agents, one enabled task) does not mask the assertions.

- [x] `PRT-S7` — it("the wait is capped below the visibility window") <!-- WHEN a 429 publishes `Retry-After: 600` and `provider.max_refusal_wait_s == 60.0` THEN the signal carries `wait_s == 60.0`; and WHEN `provider.max_refusal_wait_s == 300.0` with `queue.backend: sqlite` and `queue.visibility_timeout_s == 300` THEN `ConfigValidator().validate(cfg)` raises `ValueError` naming `provider.max_refusal_wait_s`; non-positive values raise in both `ProviderConfig.__post_init__` and the validator -->
- [x] `PRT-S14` — it("a non-boolean flag is rejected loudly") <!-- WHEN `cfg.provider.classify_refusals = "yes"` THEN `ConfigValidator().validate(cfg)` raises `ValueError` whose message contains `provider.classify_refusals` and the offending value, `ProviderConfig(classify_refusals="yes")` raises directly, and the loader parses an explicit YAML `false` as the boolean `False` (identity, not truthiness) -->

### File: `tests/test_pfc_render.py`

Describe: `provider-refusal-taxonomy — operator surface (PRT-S11, S12)`

Calls `StatusDisplayEngine()._format_pipeline_section(status, get_display_config(StatusContext.APPLICATION, DisplayMode.DETAILED))`, which returns lines (R5.3 pattern).

- [x] `PRT-S11` — it("the refusal surface is rendered and registered") <!-- WHEN `provider_refusal_metrics` carries non-zero per-class counters THEN exactly one line starts with `ProviderRefusals:` and contains every declared key's value; WHEN the surface is `{}` or the attribute is absent THEN no such line appears; and `provider_refusal_metrics` is a member of `state.display._RENDERED_METRIC_SURFACES`, whose set equals `{f.name for f in dataclasses.fields(PipelineStatus) if f.name.endswith("_metrics")}` -->
- [x] `PRT-S12` — it("no credential material in the refusal path") <!-- WHEN a refusal is logged and counted for a task whose service carries a token-shaped key, a cookie and an address THEN the captured log line, every rendered line and every counter key contain none of those values (the key is assembled from pieces so the test file itself holds no token-shaped literal), and the task is identified by the same hash `_generate_id` produces -->

### File: `tests/test_pfc_check_starve.py`

Describe: `failure-handling FH4 + rollback — check-stage starvation (FH4-S1..S3, PRT-S13)`

Real `CheckStage` from the registry over a real `RateLimiter` with a per-provider bucket whose rate
is set high so the deferral wait is ~10 ms; `_run_until_deferred` waits on `stage.tasks_deferred`
(R5.3 pattern). One **existing** pin is replaced here, not weakened — see the note below.

- [x] `FH4-S1` — it("Starved check defers without burning an attempt") <!-- WHEN the provider basket cannot be acquired THEN `tasks_deferred == 1`, `failure_empties`, `total_errors`, `tasks_requeued` and `tasks_dropped_max_retries` stay 0, the re-delivered task has the same `task_id`, `attempts` and `created_at`, no registry row was written, and the worker performed **no** `time.sleep` for the basket wait (patched and asserted not called with the unclamped value) -->
- [x] `FH4-S2` — it("Starvation is counted apart from a remote refusal") <!-- WHEN the same starvation happens THEN `deferred_provider_budget` advances and `refusals_rate_limit` / `deferred_rate_limit` do not, and `RateLimiter.report_result` was never called for that service (spy on the real limiter) -->
- [x] `FH4-S3` — it("Legacy starvation classification returns with the rollback flag") <!-- WHEN `provider.classify_refusals is False` THEN `TransientFetchError("provider limiter starved …")` is raised exactly as before, the stage requeues with `attempts` incremented, `tasks_deferred` stays 0 and `deferred_provider_budget` stays 0 -->
- [x] `PRT-S13` — it("the legacy classification returns with the flag") <!-- WHEN the flag is false and a *published-wait* 429 arrives on the provider surface THEN the legacy classification is used (retryable `ConnectionError`, no `RateLimitDeferral`) and every refusal counter stays 0 — i.e. one flag governs both halves of the change, so a half-rollback cannot keep the destructive half -->

> **Obsolete pin to remove (operator rule: delete tests that a refactor makes inaccurate, never
> weaken them).** `tests/test_fh_stage_modes.py::test_s9_starved_check_requeues_instead_of_dropping`
> asserts the *pre-change* contract (starvation ⇒ `TransientFetchError` ⇒ requeue with a burned
> attempt). That behavior survives only behind `provider.classify_refusals: false` and is pinned by
> `FH4-S3`/`PRT-S13` above. The old test is therefore **deleted**, and the traceability row in this
> file records where its intent now lives. Its sibling scenarios in the same file (strict/shadow/legacy
> mode policy) are untouched.

## Manual

No scenario requires manual verification: all 17 are automatable offline against a local
`http.server`, real limiters and real config objects.

The live third-party probe required by design D11 (what dashscope, dashscope-intl, maas.qwencloudapi
and api.deepseek actually return when throttled) is **not** a spec scenario — it is an evidence task
(tasks.md group 1) whose captured fixtures become the inputs of `PRT-S2`/`PRT-S5`. Until it runs, the
shipped predicate recognises only the dialect-independent signals listed in D11, and no provider body
is hard-coded.
