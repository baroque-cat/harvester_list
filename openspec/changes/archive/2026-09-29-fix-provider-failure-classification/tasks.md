# Tasks: fix-provider-failure-classification

Schema `tdd-flow`. Groups are ordered so that every group ends with a runnable
check; the implementation groups (2–9) may be interleaved, but **group 1 must
run first** and **group 10 last**. House rules that apply throughout:
`AGENTS.md` §1–§3 (run roots under `/var/tmp`, never `pytest` during a live run,
`setsid nohup` from a script file, `timeout -k 30 <N+300>`, mask credentials,
never provoke a secondary limit), rollback = a config flag flip, policy visible
in config, fail-open but loud, and no sleep inside a claimed task beyond the
remaining visibility window (invariant 6).

Expected GREEN total: **377 passed** = 343 (current baseline) − 1 (the deleted
obsolete pin) + 35 (the new files). (The original estimate of 371 used the RED
collected count of 29; two collection errors hid 6 tests — see
`verification.md` D13.)

## 1. RED baseline and the live third-party probe

- [x] 1.1 Run `python -m pytest tests/test_pfc_signals.py tests/test_pfc_defer.py tests/test_pfc_config.py tests/test_pfc_render.py tests/test_pfc_check_starve.py -q --continue-on-collection-errors` and confirm **23 failed, 4 passed, 2 errors** (29 collected). The four passes are the pins on behavior this change must *not* alter: `test_s3_forbidden_without_marker_stays_an_auth_failure`, `test_s4_rate_limit_without_published_wait_keeps_legacy_transient`, `test_s8_fail_open_wrapper_lets_the_signal_through` (`exclude=` already exists since R5.2) and `test_s11_empty_or_absent_surface_renders_nothing[surface1]` (an absent surface already renders nothing). Recorded drivers: `ImportError: cannot import name 'ProviderConfig'`; `module 'tools' has no attribute 'http_signals'`; `RateLimitDeferral.__init__() got an unexpected keyword argument 'reason'`; `DID NOT RAISE RateLimitDeferral` (a `ConnectionError`/`NetworkError` arrives instead); `retry.py:243 All 3 attempts failed. Last error: Rate limit exceeded (HTTP 429)`; `module 'search.client' has no attribute '_provider_refusal_stat_inc'`; `PipelineStatus.__init__() got an unexpected keyword argument 'provider_refusal_metrics'`. Any other driver is a harness bug — fix the test before implementing.
- [x] 1.2 Fix the one known harness defect from the baseline: `test_s12_a_refusal_is_logged_by_hash_and_never_by_secret` currently logs `[inspect] unknown provider: probe`, i.e. the stub provider is not accepted by `InspectStage`'s lookup. Make the stub satisfy the real lookup contract (mirror `tests/test_fh_stage_modes.py`'s `FakeProvider(IProvider)` shape) so the test exercises the *error path* rather than the unknown-provider path.
- [x] 1.3 Run `python -m pytest tests/ -q` and confirm the baseline regression count is unchanged apart from the deleted pin: **342 passed, 23 failed, 2 errors**. Record both numbers in `verification.md` §1.
- [x] 1.4 **Live third-party probe (design D11 — required before any dialect rule is written).** Against the four configured providers (`dashscope.aliyuncs.com/compatible-mode`, `dashscope-intl.aliyuncs.com/compatible-mode`, `maas.qwencloudapi.com/compatible-mode`, `api.deepseek.com`), capture the *organic* response for a capacity refusal and for an auth failure: HTTP status, the `Retry-After` / `X-RateLimit-*` header set, and the JSON body shape. Use the runtime credentials from the git-ignored `config.yaml`, mask every token with `tools.state.mask_credential`, and **never provoke a limit on purpose** (`AGENTS.md` §3) — if no organic refusal occurs, record `NOT OBSERVED` and ship only the dialect-independent predicate (429, 403 + `rate limit|abuse detection|secondary rate limit`, a published wait). Script under `/tmp/opencode/`, evidence under `/var/tmp/opencode-pfc-gate/`, distilled into `verification.md` §2.
- [x] 1.5 If — and only if — 1.4 captured a refusal that the shipped predicate misclassifies, add the narrowest possible rule **together with** the captured fixture and its provenance (date, provider, auth mode, masked). No rule without a fixture.

## 2. One shared signal predicate (`tools/http_signals.py`)

- [x] 2.1 Create `tools/http_signals.py` with `is_capacity_refusal(status, text)`, `wait_from_headers(headers)` and `wait_from_content(content)`, consolidating the three existing copies (design D1). Keep them dependency-light: no import of `search.client`, no provider knowledge.
- [x] 2.2 Turn `search/client.py`'s `_is_http_rate_limited`, `_wait_from_headers`, `_wait_from_content` (`:716-780`) and `_gather_wait_from_headers`, `_gather_wait_from_content`, `_gather_is_rate_limit_signal` (`:1276-1327`) into thin delegations that keep their names, signatures and behavior byte-for-byte. Do **not** delete them: they are referenced by existing pins and by the gather refusal path.
- [x] 2.3 Run `python -m pytest tests/test_gt_transport.py tests/test_gt_stage_integration.py tests/test_tdm_starvation.py tests/test_tdm_latency.py -q` and confirm the gather surface is unchanged (all green).

## 3. The typed signal

- [x] 3.1 Extend `core/exceptions.py::RateLimitDeferral` **additively** with `reason: Optional[str] = None` and `provider: Optional[str] = None` (design D2), keeping `__init__(message, wait_s=0.0, stage_pause=False, **kwargs)` compatible with every existing caller (`search/client.py` gather path, `stage/definition.py`, `tools/state.py`).
- [x] 3.2 Use `ErrorReason` **names** (`core/enums.py:51-73`) for `reason`, never free text, so the counters can be keyed by class.
- [x] 3.3 Run `python -m pytest tests/ -q -k "defer or deferral"` and confirm every pre-existing `RateLimitDeferral` pin still passes (R5.2/R5.3: `tests/test_cl_stage_defer.py`, `tests/test_tdm_stage_defer.py`).

## 4. Transport classification in `http_get`

- [x] 4.1 In `search/client.py::http_get` (`:837-940`), classify the `HTTPError` branch through `tools.http_signals`: a capacity refusal **with a published wait** raises `RateLimitDeferral(wait_s=<clamped>, reason=RATE_LIMITED, provider=<caller hint>)`; everything else keeps its current exception type and message (design D4 — a 429 with no published wait stays the legacy retryable `ConnectionError`).
- [x] 4.2 Read the wait from the response headers first, then the body, and clamp it to `provider.max_refusal_wait_s`. The transport **never sleeps it** (invariant 6); the stage does, clamped again by `_effective_defer_wait`.
- [x] 4.3 Leave `:895-896` (`status_code != 200 → NetworkError`) alone and note in the code comment that it is unreachable for error codes because the module-level `request()` (`:209-213`) calls `raise_for_status()`. Do not "fix" dead code in this change.
- [x] 4.4 Run `python -m pytest tests/test_pfc_signals.py tests/test_pfc_defer.py -q` → the transport scenarios (PRT-S1..S8) go green.

## 5. Propagation guards

- [x] 5.1 `tools/retry.py::RetryCore.should_retry_error` (`:39-67`): add an explicit non-retryable **class** check for `RateLimitDeferral` *before* the message-word matching, so the decision is by type and not by the words "rate limit" (design D5). Keep every legacy branch intact.
- [x] 5.2 Add `exclude=(RateLimitDeferral,)` to the `@handle_exceptions(default_result=[])` wrappers on `provider/openai_like.py::_fetch_models` (`:135`) and `provider/anthropic.py::_fetch_models` (`:157`). Do not change `default_result`, `log_level` or any other argument.
- [x] 5.3 `provider/gemini.py` (`:96,:103`) and `provider/vertex.py` (`:269,:304,:342`) have no wrapper — confirm the signal propagates unaided and add nothing.
- [x] 5.4 `provider/bedrock.py::_send_request` (`:211-241`): let the typed signal propagate instead of collapsing it into `(500, str(e))` (design D10). Leave `check()` (`:419-425`, a direct `request("POST")`) untouched.
- [x] 5.5 Run `python -m pytest tests/test_pfc_defer.py -q -k s8` → green, and `python -m pytest tests/ -q -k "provider or bedrock or gemini or vertex or anthropic"` → no regression.

## 6. Counters, the status surface and its renderer

- [x] 6.1 In `search/client.py`, add a declared key tuple `_PROVIDER_REFUSAL_STAT_KEYS` = `refusals_rate_limit`, `refusals_quota`, `refusals_auth`, `refusals_transient`, `deferred_provider_budget`, `inspect_refused`, `inspect_empty_answers`, plus `_provider_refusal_stat_inc` (rejecting an undeclared key loudly, exactly like `_gather_stat_inc`), `reset_provider_refusal_stats()` and `get_provider_refusal_stats()` (design D6).
- [x] 6.2 Add `provider_refusal_metrics: Dict[str, int] = field(default_factory=dict)` to `core/metrics.py::PipelineStatus` and populate it in `manager/pipeline.py` next to `gather_transport_metrics` / `credential_metrics`.
- [x] 6.3 Add `_format_provider_refusal_metrics_line` to `state/display.py` following the existing `_format_*_metrics_line` statics: an explicit key allowlist (never `dict.items()`), the fail-open `_metric_int` helper, a `ProviderRefusals:` prefix, nothing rendered when the surface is empty, and register the name in `_RENDERED_METRIC_SURFACES`.
- [x] 6.4 Wire the line into **both** branches of `_format_pipeline_section` (the populated branch and the `not status.pipeline.stages` early-return branch), as R5.3 did.
- [x] 6.5 Run `python -m pytest tests/test_pfc_render.py tests/test_tdm_render.py -q` → green, including the RO-S4 completeness pin (registry set == `PipelineStatus` `*_metrics` fields) and the compact-unchanged pin.

## 7. Check-stage starvation becomes a deferral

- [x] 7.1 In `stage/definition.py::CheckStage._check_worker` (`:720-764`), **delete** the unclamped `time.sleep(wait_time)` at `:727` and replace the `TransientFetchError("provider limiter starved …")` at `:736-738` with `RateLimitDeferral(wait_s=min(1/base_rate, cap), stage_pause=False, reason=RATE_LIMITED, provider=task.provider)` when `provider.classify_refusals` is true (design D3). `stage_pause=False` because the basket is per provider (invariant 1).
- [x] 7.2 Count it as `deferred_provider_budget` and report **nothing** to the adaptive budget; leave `report_result(service_type, True)` at `:749` as the success-only report it is.
- [x] 7.3 Keep the legacy branch byte-for-byte behind `classify_refusals: false`, including the failure report and the burned attempt (design D7 — one flag governs both halves).
- [x] 7.4 Confirm `stage/base.py`'s worker loop needs **no** change (its `RateLimitDeferral` handler at `:686-698` already clamps, honours `stage_pause` and calls `defer_task`). Record the confirmation in `verification.md`.
- [x] 7.5 Run `python -m pytest tests/test_pfc_check_starve.py -q` → green.

## 8. `InspectStage` honesty and secret-free logging

- [x] 8.1 In `stage/definition.py::InspectStage._inspect_worker` (`:905-935`), keep `ERROR + return None` **only** for an unknown provider (a configuration fault). A typed refusal must propagate (counted `inspect_refused`); a genuine empty model list must return an output and be counted `inspect_empty_answers` (design D8).
- [x] 8.2 Replace `task: {task}` in the error log with the hashed id from `_generate_id` (`:886-895`), which already hashes provider/key/address/endpoint precisely because "task ids are rendered in log lines" (design D9).
- [x] 8.3 Run `python -m pytest tests/test_pfc_render.py -q -k s12` → green, and grep the captured log for the fake secret to confirm it never appears.

## 9. Configuration surface

- [x] 9.1 Add `ProviderConfig` to `config/schemas.py` with `classify_refusals: bool = True` and `max_refusal_wait_s: float = 60.0`, a `provider:` field on `Config`, and loud `__post_init__` validation (non-boolean rejected, non-positive rejected) mirroring `GatherConfig`.
- [x] 9.2 Parse the section in `config/loader.py` (`_parse_provider_config`, bool-only, no truthy coercion) alongside `_parse_gather_config`.
- [x] 9.3 Validate in `config/validator.py`: the non-boolean check, the positive check, and the durability invariant `provider.max_refusal_wait_s < queue.visibility_timeout_s` when `queue.backend: sqlite` — modelled on the existing gather/credential-liveness checks.
- [x] 9.4 Resolve the flag at runtime with a guarded lazy import (`_configured_classify_refusals()`), mirroring `_configured_gather_transport()` / `_configured_defer_local_suppression()`, and pass the resolved value from the callers so an embedded caller cannot bypass the operator's rollback flag.
- [x] 9.5 Document the section in `README.md` (next to `gather:`) and in `examples/config-full.yaml` with the rollback semantics spelled out, and add the "every published `*_metrics` surface needs a renderer" convention line to `openspec/config.yaml → context:` if R5.3's wording does not already cover it.
- [x] 9.6 Run `python -m pytest tests/test_pfc_config.py tests/test_gt_config.py tests/test_cl_config.py -q` → green.

## 10. Obsolete pins, full suite and validation

- [x] 10.1 Delete `tests/test_fh_stage_modes.py::test_s9_starved_check_requeues_instead_of_dropping` together with its now-unused `_StarvingLimiter` harness and the imports it alone needed (`CheckStage`, `Service`), leaving a comment that names the superseding pins. **Delete, never weaken** — the behavior it pinned survives only behind `provider.classify_refusals: false` and is pinned by FH4-S3 / PRT-S13. *(Already applied while authoring the RED baseline; verify the file still collects and its sibling mode-policy scenarios are untouched.)*
- [x] 10.2 Search the whole suite for any other pin that asserts the replaced behavior (`provider limiter starved`, `Authentication failed`, `_fetch_models` returning `[]` on refusal) and delete or rewrite it against the new contract. Record what was found in `verification.md`.
- [x] 10.3 Run `python -m pytest tests/ -q` and confirm **371 passed**. Then run it **three times consecutively** with the same result (house rule for a GREEN claim).
- [x] 10.4 Run `openspec validate fix-provider-failure-classification --type change --strict` and `openspec validate --specs --strict` (16 main specs must still pass; this change adds no main spec until it is synced).
- [x] 10.5 Tick every scenario in `tests.md`, write `verification.md` §1–§4 (RED baseline verbatim, probe provenance, GREEN suite, deviations as new numbered design decisions), and re-validate.

## 11. Live gate (operator-gated; this schema has no `runbook` artifact, so the steps live here)

All run roots under `/var/tmp/opencode-pfc-gate/<run>/`; `cd "$RUN"` before launching so the repo's own `data/` and `logs/` stay untouched; launch with `setsid nohup … &` from a script file; wrap in `timeout -k 30 <N+300>`; guard every glob for `zsh`. Never run `pytest` while a live run is active (`AGENTS.md` §2). Expect roughly 4 MB of `data/` per second of soak.

- [x] 11.1 Snapshot the evidence baseline before launching: `md5sum config.yaml`, the basket values, `df -h /tmp`, and the repo `data/` mtime.
- [x] 11.2 **B1 — no regression under the fixed classification.** 600 s soak with the live config (`github_raw 4.0/8`, `threads.gather 4`, `provider.classify_refusals` defaulted true). Accept: gather ≈ 8.5 req/s (R5.3 D20 reference), `defer[budget]` > 0, **0** gather failure-empties, **0** burned gather attempts, 429/403/secondary = 0, RSS ≤ 300 MB.
- [x] 11.3 **B2 — the new surface renders live.** Accept: a `ProviderRefusals:` line appears in the periodic detailed block, its counters agree with the stage counters, and a run with no provider refusals renders **no** such line (the empty-surface rule observed live, as R5.3 observed it for `Gather:`).
- [x] 11.4 **B3 — check-stage starvation defers.** Accept: `provider limiter starved` no longer appears as a failure-empty; `[check]` deferrals appear instead; `SUM(attempts)` over the check queue does **not** grow with them; `tasks_deferred` accounts for every one (1:1 with the log lines).
- [x] 11.5 **B4 — rollback drill.** Flip `provider.classify_refusals: false` in the run-root copy, run ~120 s on the same backlog, and accept: the legacy signature returns (`provider limiter starved` failure-empties, attempts burned, `deferred_provider_budget` flat at 0). Flip back and accept: deferrals resume, no task lost across either flip.
- [x] 11.6 **B5 — no secret in any output.** Scan the run root for `ghp_`, `github_pat_`, the four configured session values and `sk-[A-Za-z0-9]{20,}`; mask harvested candidate keys, delete secret-bearing config copies, and re-scan to zero. Distinguish operator credentials from harvested payload content, as R5.3 did.
- [x] 11.7 **B6 — durability.** Graceful stop leaves `claimed = 0`; `kill -9` on the process group with claims in flight yields `reclaimed == in flight`, byte-identical queue files and `PRAGMA integrity_check = ok`.
- [x] 11.8 Clean up (`AGENTS.md` §1): delete `data/providers/**` and `data/queue_state/**`, keep `evidence/`, return `df -h /tmp` to its pre-task level, and distil everything the report cites into small text files first.
- [x] 11.9 Record every measurement, command and per-row accept/fail in `verification.md` §5. Any acceptance failure → amend code or specs as a new numbered decision and re-run the affected steps. — **Executed 2026-09-29: B1, B3, B4, B5, B6 PASS on every row. B2 FAILED its empty-surface row on the first observation (an all-zero surface still rendered `ProviderRefusals: …=0`, because `get_provider_refusal_stats()` always returns the full declared key set) → amended as design decision D15 (all-zero guard, mirroring the `Gather:` renderer) and re-observed live in B4: 0 of 11 blocks with the flag off, 6 of 11 with it on. Full suite re-ran 377 passed three consecutive times afterwards.**

## 12. Handoff

- [x] 12.1 Update `plan.md`: close **II.3 (Ф7/R8)** and the **D10** half of the check-stage starvation, add the round to the **I.8** registry table (commit hash, archive name, main-spec counts, test count, promoted config keys), extend the route line at the top of Part I, refresh the **II.12** summary table, and update the Part III status labels that point at II.3 / III.10. Note that `plan.md` is git-ignored (`.gitignore:141`), so this edit lives on disk only. — **Done after the archive/commit, because the I.8 row cites that commit's hash (R5.3 precedent). On disk only; not staged.**
- [x] 12.2 Run the `openspec-sync-specs` flow: create the new main spec `openspec/specs/provider-refusal-taxonomy/spec.md` from the delta, and apply the MODIFIED requirement to `openspec/specs/failure-handling/spec.md`. Then `openspec validate --specs --strict` must report **17 passed / 0 failed**. — **Done. New main spec `provider-refusal-taxonomy` (4 requirements / 14 scenarios, PRT-S1..S14) copied verbatim from the delta; `failure-handling` requirement "Check tasks survive limiter starvation" rewritten to the deferral contract, 1 → 3 scenarios ("Starved check requeues" superseded by "Starved check defers without burning an attempt", plus the counted-apart and rollback-flag scenarios). Main specs carry no blockquote annotations, so the delta's supersession note stays in the delta. `openspec validate --specs --strict` → **17 passed / 0 failed** (was 16).**
- [x] 12.3 Archive as `YYYY-MM-DD-fix-provider-failure-classification` after verifying the **local and UTC dates agree** (R5.3 needed the local date: the repo precedent `2026-09-22-fix-queue-persistence-under-load` was committed at `02:24+03:00` = the previous UTC day and is named by the local date). — **Done. Date check at archive time: local `2026-09-29 03:50+0300` and UTC `2026-09-29 00:50Z` AGREE, so the name is unambiguous: `2026-09-29-fix-provider-failure-classification`. (The gate itself ran while they still disagreed — local 09-29 / UTC 09-28 — and every evidence timestamp records both.) Archived with `--skip-specs` because 12.2 had already applied the deltas by hand.**
- [x] 12.4 Commit in English, staging files explicitly (never `git add -A`): the source, test, doc and artifact paths — and **not** `config.yaml`, `openspec/config.yaml` or `plan.md` (all git-ignored), and not `examples/harvester.service` (an operator decision that is still open). — **Done immediately after the archive: one commit carrying the source, the five new test files, the modified `tests/test_fh_stage_modes.py`, `README.md`, `examples/config-full.yaml`, the synced main specs and the archived change. `git status` verified clean of `config.yaml`, `openspec/config.yaml`, `plan.md` and `examples/harvester.service` before committing.**
