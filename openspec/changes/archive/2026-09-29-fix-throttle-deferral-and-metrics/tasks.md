# Tasks — fix-throttle-deferral-and-metrics

Implementation order follows the RED baseline already recorded in `tests.md` and the decisions in
`design.md` (D1–D13). Every automated scenario listed in `tests.md` must be green before archive;
the live gate (group 9) is operator-gated and closes acceptance rows A1/A2 that the offline suite
cannot reach.

Expected GREEN totals: **343 passed** (320 pre-existing + 23 new across the four `tests/test_tdm_*.py`
files). The RED baseline is **22 failed, 321 passed**; the single pre-existing pass is
`run-observability-RO-S2`, kept as a regression guard (documented in `tests.md`).

## 1. RED baseline and live band probe

- [x] 1.1 Run `python -m pytest tests/test_tdm_starvation.py tests/test_tdm_latency.py tests/test_tdm_render.py tests/test_tdm_stage_defer.py -q` and confirm **22 failed, 1 passed**, with every failure matching one of the expected drivers recorded in `tests.md` (`search/client.py:1322` `TransientFetchError: gather fetch suppressed by local limiter`; `TypeError … unexpected keyword argument 'defer_local_suppression'`; `AttributeError: 'GatherConfig' object has no attribute 'defer_local_suppression'`; `KeyError: 'latency_samples_raw'`; missing `_GATHER_LATENCY_BANDS`/`_gather_latency_observe`; no `Gather:`/`Aggregation:`/`Refine:` line; no `_RENDERED_METRIC_SURFACES`). Any failure with a different driver is a harness bug, not a RED signal — fix the test before implementing. — **Confirmed 22 failed, 1 passed (2026-09-28). Drivers matched.**
- [x] 1.2 Run `python -m pytest tests/ -q` and confirm the 320 pre-existing pins still pass (`22 failed, 321 passed`), i.e. the new files interfere with nothing. Record both numbers in `verification.md` §1. — **Confirmed 22 failed, 321 passed / 343 collected.**
- [x] 1.3 **Live staging probe (required early by the artifact rules — this change publishes latency percentiles of a real external wire format).** Probe `raw.githubusercontent.com` anonymously over ~80 distinct URLs from the harvested corpus, record the true latency distribution and the per-file byte mean, and determine **which histogram band the true p50 falls into**. Provenance to capture: date, URL count, auth mode (anonymous), and the reference figures it must be compared against (probe 2026-09-25: p50 338 ms / p99 390 ms / 17.1 KiB per file; R5.1 limited gate: p50 239 ms / 12.6 KiB). Script under `/tmp/opencode/`, output kept as evidence. — **Done 2026-09-28: 80/80 HTTP 200 anonymous, true p50 255 ms → band (250,300] upper edge 300 ms; true p99 493 ms → edge 500 ms; mean 25.5 KiB/file. Evidence: `/var/tmp/opencode-tdm-probe/probe-*.json`.**
- [x] 1.4 From the 1.3 measurement, pin the A2 verdict rule in `runbook.md` **before implementing**: with band edges `…,250,300,350,…` and conservative upper-edge reporting (design D6), a true p50 of 338 ms publishes as **350 ms**, which is outside the inherited window "240–340 ms". Decide and write down whether A2 is judged on the reported upper edge (window widened by one band, i.e. reported p50 ≤ 350 ms and ≥ 250 ms) or whether the edges need refinement in the 200–400 ms region. Do not leave this to be discovered during the gate. — **Pinned: judged on the reported upper edge, window widened to `[250, 350]` ms and `p99 < 1000` ms; edges unchanged.**

## 2. Configuration surface for the flag (gather-transport-S26, D5/D13)

- [x] 2.1 Add `defer_local_suppression: bool = True` to `GatherConfig` in `config/schemas.py` with loud `__post_init__` type validation matching the house style (a non-bool must raise, not coerce).
- [x] 2.2 Parse `gather.defer_local_suppression` in `config/loader.py::_parse_gather_config` as a **bool only** (no truthy strings), and include the key in `Config.to_dict` output if that method enumerates gather fields.
- [x] 2.3 Validate the key in `config/validator.py` so a non-bool value produces an error naming `defer_local_suppression`; add no warning for `false` (it is a supported rollback position, not a hazard).
- [x] 2.4 Add the client-side seam in `search/client.py`: `_configured_defer_local_suppression()` mirroring `_configured_gather_transport()` (guarded lazy `from config import get_config`, module default `True`), a `defer_local_suppression: Optional[bool] = None` parameter on `fetch_gather_content(...)`, and pass the resolved value from `collect()` (D13).
- [x] 2.5 Drive `gather-transport-S26` (supporting) green: `tests/test_tdm_starvation.py` flag-config-surface test.

## 3. Withholding becomes a bounded deferral (gather-transport-S22, S23, S24, S25; FH3-S1..S5)

- [x] 3.1 Stop poisoning the adaptive basket: remove `client._report(service, False, credential)` from the withheld-request branch at `search/client.py:1319-1322` (D4). The surviving pin `tests/test_gt_transport.py::test_s9_failure_is_reported_to_the_adaptive_budget` covers **issued** requests only and must stay green — verify it is not weakened.
- [x] 3.2 Add the small client helper that re-reads the basket's own `wait_time()` after the token was lost (bucket naming is private to `RateLimiter`, hence the helper) and clamps it by the caller's `max_refusal_wait_s` (D3). Expect ≤ 0.5 s at `base_rate 2.0` and ≤ 5 s at the adaptive floor.
- [x] 3.3 Raise `RateLimitDeferral(message, wait_s=<helper value>, stage_pause=True)` instead of `TransientFetchError` when the flag is enabled (D1, D2 — eight gather workers share one basket, so the pause must cover the stage, not just this worker).
- [x] 3.4 Declare and increment the new counter `deferred_local_budget`: add the key to `_GATHER_TRANSPORT_STAT_KEYS`, bump it via `_gather_stat_inc` on the withheld branch only (D12), and keep it separate from `deferred_rate_limit`/`deferred_secondary`/`deferred_credentials`.
- [x] 3.5 Preserve the legacy classification byte-for-byte behind `defer_local_suppression=false`: `TransientFetchError` with the unchanged message **and** the `_report(..., False, ...)` failure report (S26). This is the rollback path; it must be reachable without any other edit.
- [x] 3.6 Confirm the **search** boundary is untouched (D11): `search_api_with_count` keeps raising `TransientFetchError` on suppression regardless of the flag (FH3-S6). This is verification, not code change — the preserved pin `tests/test_fh_client_taxonomy.py::test_s3_limiter_suppression_is_failure_empty` must stay green.
- [x] 3.7 Confirm the **check** stage is untouched (D10): `stage/definition.py:741-756` keeps raising `TransientFetchError` on provider-basket starvation and reports nothing to the basket; `tests/test_fh_stage_modes.py::test_s9_starved_check_requeues_instead_of_dropping` must stay green.
- [x] 3.8 Drive `tests/test_tdm_starvation.py` green (8 tests: S22, S23, S24, S25, S26, S26-supporting, S29, FH3-S6).

## 4. Stage-level deferral semantics (FH3-S5, FH3-S12)

- [x] 4.1 Verify no change is needed in `stage/base.py`: the existing `RateLimitDeferral` handler (`:686-697`) already clamps `wait_s` via `_effective_defer_wait` → `gather.max_refusal_wait_s`, honours `stage_pause` through `_defer_pause_until` (`:657`) and defers via `defer_task` (`:381-421`) preserving `attempts`/`created_at`/`dedup_id`. If a change turns out to be necessary, record it as a new numbered decision in `design.md` rather than adapting the tests.
- [x] 4.2 Drive `tests/test_tdm_stage_defer.py` green (FH3-S5: `tasks_deferred` advances while `failure_empties_detected`, `total_errors`, `tasks_requeued`, `tasks_dropped_max_retries`, `total_processed` stay 0, no `visit_status`/`link_coverage` row written, no HTTP request issued; FH3-S12: identity and age preserved across four withholdings).

## 5. Latency histogram (gather-transport-S27, S28, S29; RO-S9, RO-S10)

- [x] 5.1 Add `_LATENCY_BAND_EDGES_MS` (module-level tuple, first edge `25`, ending with the open band) and `_GATHER_LATENCY_BANDS` (kind → fixed-length counter list for `raw`, `html`, `rest`) per design D6 — the exact edges confirmed or refined by task 1.4.
- [x] 5.2 Add `_gather_latency_observe(kind, ms)` appending to the fixed band structure only (S28: 20 000 observations must not change the published key set or the band lengths).
- [x] 5.3 Time the span from request issuance through `_read_capped_payload` for each transport kind, and ensure a **withheld** fetch is never observed (S29: `latency_samples_raw == 0` and `requests_raw == 0`).
- [x] 5.4 Publish derived `latency_samples_<kind>`, `latency_p50_ms_<kind>`, `latency_p99_ms_<kind>` from `get_gather_transport_stats()` using the **upper edge of the band containing the rank** (RO-S9: never below the true percentile), as plain integers, with `bytes_<kind>`/`requests_<kind>` staying raw integers so the mean is computable (RO-S11).
- [x] 5.5 Extend `reset_gather_transport_stats()` to zero the band counters in place (RO-S10, D12).
- [x] 5.6 Drive `tests/test_tdm_latency.py` green (4 tests).

## 6. Operator rendering path (RO-S1..S8, S11; D7, D8)

- [x] 6.1 Add `_RENDERED_METRIC_SURFACES` to `state/display.py` and make it exactly the set of `PipelineStatus` fields ending in `_metrics` **plus** `credential_metrics` — no exemptions, no unknown names (RO-S4). This is the registry that makes "a surface nobody renders" a test failure instead of a silent gap.
- [x] 6.2 Add allowlisted `_format_*_metrics_line` statics following the house pattern (`getattr(status.pipeline, "<surface>", None)` → `""` when empty/absent; read only declared keys, never `dict.items()`), wired into `_format_pipeline_section` (D7, D8; RO-S1, RO-S2, RO-S5).
- [x] 6.3 Render the `Gather:` line with `req=`/`bytes=`/`p50=`/`p99=` per transport plus `defer[rl=…, budget=…, sec=…, cred=…]`, `drop[auth=…, 404=…]`, `head=…` (RO-S1, RO-S11).
- [x] 6.4 Render the `Aggregation:` line (`hits/misses/joins/entries/bytes/pairs`) and the `Refine:` line (`mode/gen/adm/depth/budget/trunc/cov[…]`) (RO-S1).
- [x] 6.5 Render a `Credential:` line for `credential_metrics` so the RO-S4 registry holds without exemptions; leave the D20 shutdown line from `fix-credential-liveness` untouched (D7 consequence paragraph).
- [x] 6.6 Make malformed figures degrade: `None`, non-numeric strings, `NaN`, `inf`, negatives and missing keys must render a line without raising and without emitting `nan`/`inf` text (RO-S7); output must be deterministic for identical state (RO-S3); no key may carry credential material into the line (RO-S6).
- [x] 6.7 Confirm compact output is unchanged — none of the new labels may appear in `_render_compact` (RO-S8).
- [x] 6.8 Drive `tests/test_tdm_render.py` green (9 tests).

## 7. Dead code, documentation, house conventions

- [x] 7.1 Delete `SearchStage._apply_rate_limit` (`stage/definition.py:336-350`) — zero callers repo-wide, verified by search (D9). Re-run the search after deletion to confirm nothing referenced it.
- [x] 7.2 Document the flag and the rendered lines in `README.md` and `examples/config-full.yaml`: what own-budget withholding means, why it is a deferral rather than a failure, what `defer_local_suppression: false` restores (legacy classification **and** the failure report), and how to read `p50=`/`p99=` given upper-edge band reporting.
- [x] 7.3 If task 6.1 establishes a reusable convention (every published metric surface must have a renderer), add one line to `openspec/config.yaml → context:` so the next change inherits it.

## 8. GREEN and validation

- [x] 8.1 Run the four new files: **23 passed, 0 failed**.
- [x] 8.2 Run the full suite: **343 passed** (320 pre-existing + 23 new), three consecutive clean runs, never concurrently with a live pipeline run (`AGENTS.md` §2).
- [x] 8.3 `openspec validate fix-throttle-deferral-and-metrics --type change --strict` → valid (note: this CLI build takes the change name positionally with `--type change`; `--change` is rejected by `validate`).
- [x] 8.4 Tick every automated scenario in `tests.md`, record the RED→GREEN progression and the validate output in `verification.md` (§1–§4), and append any deviation found during implementation to `design.md` as a new numbered decision (append-only — never rewrite shipped evidence).

## 9. Live gate — execute `runbook.md` (operator-gated)

- [x] 9.1 Execute `runbook.md` after code and tests are green: the deliberately under-provisioned 600 s soak (the live config **already is** that configuration — `github_raw` 2.0 req/s burst 4 against `threads.gather 8`), the A1/A2 readings from the rendered `Gather:` line, the comparison against the pre-fix figures (1 708 suppressions / 876 failure-empties / 869 requeues), the rollback drill with `defer_local_suppression: false`, the restart/`kill -9` recovery check, and the latency-band probe re-run. Run roots under `/var/tmp`, secrets read at runtime, nothing secret-bearing committed (`AGENTS.md` §1, §3).
- [x] 9.2 Record every measurement, command and per-row accept/fail in `verification.md` §5. Any acceptance failure → amend code or specs as a new numbered decision and re-run the affected steps. — **Executed 2026-09-28: 15/16 rows PASS. A1 failed its inherited "mean ≤ 20 KiB" bound (measured 35.15 KiB) → amended as design decision D19 and the runbook row re-pinned on distribution evidence (median 6.0 KiB, raw/html ratio 14.9–20.5×); no code changed, the offline suite re-ran 343 passed afterwards.**
- [x] 9.3 Handoff: update `plan.md` (§II.1 O3 and §II.2 O2 closed, the governor-cap precondition lifted or re-stated with post-fix numbers, §III.10 status labels), run `openspec` sync-specs (new main spec `run-observability`, deltas applied to `gather-transport` and `failure-handling`) and archive the change as `YYYY-MM-DD-fix-throttle-deferral-and-metrics` (verify local **and** UTC date agree before naming). — **Done 2026-09-29. sync-specs: `gather-transport` 21→29 scenarios (S22–S29), `failure-handling` 21→24, new main spec `run-observability` (3 req / 11 scen); `openspec validate --specs --strict` → 16 passed / 0 failed (was 15). Date check: local `2026-09-29 MSK` vs UTC `2026-09-28` DISAGREE — named by the LOCAL date, following the repository precedent `2026-09-22-fix-queue-persistence-under-load` (committed `2026-09-22T02:24+03:00` = UTC `2026-09-21T23:24`, archived under the local date). `plan.md` updated in the handoff commit that follows the archive commit (it cites that commit's hash).**
