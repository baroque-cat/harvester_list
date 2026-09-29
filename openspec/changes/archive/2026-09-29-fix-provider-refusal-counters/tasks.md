# Tasks: fix-provider-refusal-counters

## 1. Shared vocabulary (S1, D7)

- [x] 1.1 Export `LIMIT_MARKERS` from `tools/http_signals.py` as the single public definition of the
      throttling-marker vocabulary; keep `_LIMIT_MARKERS` as a private alias so existing internal
      references and the predecessor's pins keep working.
- [x] 1.2 Add `QUOTA_MARKERS` to `tools/http_signals.py`, transcribed from the five shipped `_judge`
      methods, with the exclusions from design D3 stated in a comment next to the pattern.
- [x] 1.3 Add `is_quota_refusal(text) -> bool` to `tools/http_signals.py`: stdlib only, fail-open on
      any exception, returns a real `bool`, no status code involved (opportunistic contract).
- [x] 1.4 Make `GitHubClient.is_rate_limited_content` (`search/client.py:615-636`) consume
      `LIMIT_MARKERS` and delete its private pattern list. Leave the `SERVICE_TYPE_GITHUB_WEB` branch
      and the JSON `message` extraction untouched (D7).
- [x] 1.5 Verify the credential-cooling verdicts are unchanged: `tests/test_cl_detectors.py` passes
      without modification.

## 2. Producers for the three inert classes (W1, D1, D2, D4)

- [x] 2.1 Restructure the `HTTPError` handler of the module-level `http_get` (`search/client.py:881-922`)
      so the whole classification block — counting included — is gated by
      `_configured_classify_refusals()`, and so every `raise` keeps its existing type, message and
      position (D1).
- [x] 2.2 Implement the precedence table of design D2 exactly: published wait → `refusals_rate_limit`
      + deferral; capacity without wait → `refusals_quota` when a quota marker is present else
      `refusals_transient`; non-capacity quota marker → `refusals_quota`; `401/403` →
      `refusals_auth`; `>= 500` → `refusals_transient`; anything else counts no refusal class.
- [x] 2.3 Confirm no `raise` statement changed: `429` → `ConnectionError("Rate limit exceeded …")`,
      `404` → `FileNotFoundError`, `401/403` → `NetworkError("Authentication failed …")`,
      `>=500` → `ConnectionError("Server error …")`, else `NetworkError`. Wire-hit counts unchanged.
- [x] 2.4 Confirm the counters cannot be reached by GitHub traffic (D5): module-level `http_get` has
      no caller outside `provider/*` and `tests/*`.
- [x] 2.5 Add the thread-local refusal scope to `search/client.py` (D9): `begin_refusal_scope()`,
      `take_refusal_class()`, and recording of the class inside `_provider_refusal_stat_inc` for the
      four `refusals_*` keys only.
- [x] 2.6 Route the inspect outcome three ways in `stage/definition.py::_inspect_worker` (D9):
      models → answer; empty **and** a refusal class was counted → `inspect_refused`; empty and no
      refusal → `inspect_empty_answers`. Read the scope in a `finally` so the raise path clears it too.
- [x] 2.7 Tag the escaping exception with its class and count it once in a thin public `http_get`
      wrapper over the retried body (D10), so a refusal burned across three attempts counts once.
- [x] 2.8 Confirm the two counting dimensions stay independent: a deferral advances
      `refusals_rate_limit` and `inspect_refused`, one per dimension, exactly as the predecessor's B1
      soak measured 1:1:1.
- [x] 2.9 Confirm the scope boundary of D11: the check surface (`chat`) does not touch the refusal
      counters, so a harvested credential's rejection is never counted as our provider refusing us.

## 3. Signal escape on the remaining transports (W2, D6)

- [x] 3.1 `provider/vertex.py`: add `except RateLimitDeferral: raise` immediately before
      `except Exception` at `:325` (first publisher loop) and at `:353` (fallback block); add the
      import.
- [x] 3.2 `provider/bedrock.py`: add `except RateLimitDeferral: raise` immediately before
      `except Exception` at `:493` in `inspect`; the import and the `_send_request` re-raise at `:240`
      already exist (PFC-D10).
- [x] 3.3 Confirm `provider/gemini.py`, `provider/openai_like.py` and `provider/anthropic.py` need no
      change, and record the confirmation as a pin rather than an assertion (D6).

## 4. Pins (PRT-S15…PRT-S25, W3, W4, W5)

- [x] 4.1 `tests/test_prc_classes.py` (new): drive each row of the D2 precedence table through the
      real module-level `http_get` against a loopback server, asserting counter deltas, exception
      type, exception message and wire-hit count (PRT-S18, S19, S20, S21).
- [x] 4.2 Pin PRT-S15 in the same file: `403 + limit marker + no published wait` → `ConnectionError`
      whose message is `Rate limit exceeded (HTTP 403)`, **3** wire hits, `refusals_transient == 1`.
- [x] 4.3 Pin PRT-S22: capture exception type, message and hit count for the full status table, and
      assert they are identical to the values recorded from the shipped pre-change transport.
- [x] 4.4 Pin PRT-S25 (D11 boundary) and PRT-S23: with `classify_refusals` false, an auth refusal, a quota refusal and a
      transient refusal leave every counter at zero and keep the legacy exceptions.
- [x] 4.5 `tests/test_prc_transports.py` (new): pin PRT-S17 for **all five** shipped transports —
      `openai_like`, `anthropic`, `gemini`, `vertex`, `bedrock` — each raising the typed signal out of
      `_fetch_models`/`inspect` rather than returning `[]`.
- [x] 4.6 Pin PRT-S16: `is_rate_limited_content` and `is_capacity_refusal` agree on a table of marker
      and non-marker bodies, the shared constant is the identical object, and no second private copy
      of the limit markers remains in `search/client.py`.
- [x] 4.7 **Rewrite** the PRT-S9 pin (`tests/test_pfc_defer.py:191-207`) to drive one refused inspect
      call and one `{"data": []}` answer through a real `InspectStage`, asserting both counters and
      `stage.total_errors == 0` (W4). Model the harness on `tests/test_pfc_check_starve.py`.
- [x] 4.8 **Extend** the PRT-S6 pin (`tests/test_pfc_defer.py:87-105`) with a worker-level half: drive
      a transport-produced signal through a real stage worker and assert `defer_task` is reached with
      the wait clamped to the configured cap (W5).

## 5. Harness stability (W6, D8)

- [x] 5.1 In `tests/test_gt_stage_integration.py::test_s18_a_deferred_task_is_never_executed_twice`,
      fix all three harness defects found by D8: take the claimed-count snapshot under
      `stage.queue._cond` (the two consumers share one SQLite connection with the claim path); narrow
      `consumer()`'s `except Exception: return` to `except queue.Empty: return`; and move
      `assert stage.defer_task(t) is True` out of the broad handler, since `AssertionError` is an
      `Exception` and was being swallowed along with the task.
- [x] 5.2 Run the full suite repeatedly (≥ 5 consecutive full runs, plus ≥ 20 targeted runs of
      `test_s18`) and record the results; the target is zero failures.

## 6. Predecessor documentation (W3, S2, D12)

- [x] 6.1 Append **PFC-D17** to the archived predecessor `design.md`: the 403-with-marker-and-no-wait
      deviation from predecessor task 4.1, its resolution in favour of PRT-S2, and the pin that now
      covers it (PRT-S15).
- [x] 6.2 Amend **PFC-D6** in the same file: the undeclared-counter guard is deliberately *stricter*
      than `_gather_stat_inc` (raises `ValueError` rather than logging a WARNING), because an
      undeclared key is a programming error, not a degradation.
- [x] 6.3 Mark both edits as amendments with this change's name as provenance; do not rewrite
      history silently.

## 7. Verification

- [x] 7.1 Full suite GREEN, repeated per 5.2.
- [x] 7.2 Re-run the predecessor's offline probes (`/tmp/opencode/pfc_verify_auth_path.py`,
      `/tmp/opencode/pfc_probe_403b.py`) through the shipped code and confirm the counter column of
      the W1 table now reads `refusals_auth` / `refusals_quota` / `refusals_transient` instead of
      `inspect_empty_answers`, with the outcome column unchanged.
- [x] 7.3 Live confirmation run under `/var/tmp` per AGENTS.md §1 (run root, `cd "$RUN"`,
      `setsid nohup` from a script file, `timeout -k 30 <N+300>`), with no `pytest` running
      concurrently (§2): confirm throughput is not degraded, stage errors stay 0, the
      `ProviderRefusals:` line renders, and the counters agree 1:1:1 with the log lines.
- [x] 7.4 Credential hygiene: mask every evidence file, scan for operator credentials and harvested
      keys, delete run data, and confirm `df -h /tmp` returns to its pre-task level (§3, §1.4).
- [x] 7.5 Sync the delta into `openspec/specs/provider-refusal-taxonomy/spec.md`, run
      `openspec validate --specs --strict`, then archive and commit.
- [x] 7.6 Update `plan.md` (git-ignored): registry row for this change, route line, and the closure of
      the verification findings.
