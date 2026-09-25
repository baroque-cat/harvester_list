# Test Plan

Derived mechanically from the delta specs under `specs/` (3 capabilities, 27 scenarios). Scenario IDs are stable and append-only; numbering continues from the main specs for the two MODIFIED capabilities (`failure-handling` has 17 scenarios today, so new ones start at S18; `date-extraction` has 12, so new ones start at S13). House conventions: flat `tests/` dir, feature-prefix filenames (`gt_` = gather transport), real objects over mocks where possible, `caplog` for log assertions, live-captured sanitized fixtures under `tests/fixtures/`.

**Modules under test do not exist yet.** At plan time there is no `GatherConfig` in `config/schemas.py`, no `SERVICE_TYPE_GITHUB_RAW` in `constant/system.py`, no `RateLimitDeferral` in `core/exceptions.py`, and no `derive_raw_url` / `fetch_gather_content` / `get_gather_transport_stats` in `search/client.py`. Every new file below therefore fails at import, which IS the expected RED state. `_service()` (`search/client.py:276-287`) currently returns `None` for `raw.githubusercontent.com` because neither `"api.github.com"` nor `"github.com"` is a substring of it - that silent-unthrottled path is what S9 pins shut.

**Repair before adding** (design D11, operator directive): three existing pins in `tests/test_date_extraction.py` assert on rendered-page markup and are re-scoped to the `html` transport rather than deleted; `tests/test_fh_gather_fidelity.py` gains the deferral case alongside its existing s7/s8 instead of being rewritten. No test is weakened: each re-scope keeps its original assertion and adds the transport precondition that was previously implicit.

## Traceability

| Scenario ID | Spec | Requirement | Scenario | Test file | Status |
|-------------|------|-------------|----------|-----------|--------|
| `gather-transport-S1` | `specs/gather-transport/spec.md` | Transport selection with loud configuration validation | absent section resolves to the measured-cheapest transport | `tests/test_gt_config.py` | GREEN |
| `gather-transport-S2` | `specs/gather-transport/spec.md` | Transport selection with loud configuration validation | unknown transport name fails validation loudly | `tests/test_gt_config.py` | GREEN |
| `gather-transport-S3` | `specs/gather-transport/spec.md` | Transport selection with loud configuration validation | non-positive numeric gather fields fail validation loudly | `tests/test_gt_config.py` | GREEN |
| `gather-transport-S4` | `specs/gather-transport/spec.md` | Transport address derived opportunistically from the discovered link | immutable revision is used verbatim | `tests/test_gt_transport.py` | GREEN |
| `gather-transport-S5` | `specs/gather-transport/spec.md` | Transport address derived opportunistically from the discovered link | unusable revision falls back loudly | `tests/test_gt_transport.py` | GREEN |
| `gather-transport-S6` | `specs/gather-transport/spec.md` | Transport address derived opportunistically from the discovered link | blob content hash is never mistaken for a revision | `tests/test_gt_transport.py` | GREEN |
| `gather-transport-S7` | `specs/gather-transport/spec.md` | Transport address derived opportunistically from the discovered link | unparseable link degrades safely | `tests/test_gt_transport.py` | GREEN |
| `gather-transport-S8` | `specs/gather-transport/spec.md` | Transport address derived opportunistically from the discovered link | persisted tasks survive a transport switch | `tests/test_gt_stage_integration.py` | GREEN |
| `gather-transport-S9` | `specs/gather-transport/spec.md` | Gather traffic is throttled under its own budget | plain-content host resolves to a real budget | `tests/test_gt_transport.py` | GREEN |
| `gather-transport-S10` | `specs/gather-transport/spec.md` | Gather traffic is throttled under its own budget | gathering does not consume the search budget | `tests/test_gt_transport.py` | GREEN |
| `gather-transport-S11` | `specs/gather-transport/spec.md` | Gather traffic is throttled under its own budget | authenticated transports keep their existing budgets | `tests/test_gt_transport.py` | GREEN |
| `gather-transport-S12` | `specs/gather-transport/spec.md` | Refusals are classified by signal and produce bounded counted outcomes | forbidden-with-rate-limit-marker is a deferral | `tests/test_gt_transport.py` | GREEN |
| `gather-transport-S13` | `specs/gather-transport/spec.md` | Refusals are classified by signal and produce bounded counted outcomes | genuine authentication failure drops loudly once | `tests/test_gt_transport.py` | GREEN |
| `gather-transport-S14` | `specs/gather-transport/spec.md` | Refusals are classified by signal and produce bounded counted outcomes | missing resource drops loudly once | `tests/test_gt_transport.py` | GREEN |
| `gather-transport-S15` | `specs/gather-transport/spec.md` | Refusals are classified by signal and produce bounded counted outcomes | finite quota exhaustion defers without burning attempts | `tests/test_gt_transport.py` | GREEN |
| `gather-transport-S16` | `specs/gather-transport/spec.md` | Refusals are classified by signal and produce bounded counted outcomes | actor-scoped abuse refusal pauses the stage | `tests/test_gt_transport.py` | GREEN |
| `gather-transport-S17` | `specs/gather-transport/spec.md` | Refusal waits respect the durable queue visibility window | contradictory wait configuration is rejected | `tests/test_gt_config.py` | GREEN |
| `gather-transport-S18` | `specs/gather-transport/spec.md` | Refusal waits respect the durable queue visibility window | a deferred task is never executed twice | `tests/test_gt_stage_integration.py` | GREEN |
| `gather-transport-S19` | `specs/gather-transport/spec.md` | Payload handling is economical, bounded and observable | plainer payload loses no findings | `tests/test_gt_transport.py` | GREEN |
| `gather-transport-S20` | `specs/gather-transport/spec.md` | Payload handling is economical, bounded and observable | oversized payload is truncated and still searched | `tests/test_gt_transport.py` | GREEN |
| `gather-transport-S21` | `specs/gather-transport/spec.md` | Payload handling is economical, bounded and observable | transport economics are observable | `tests/test_gt_stage_integration.py` | GREEN |
| `failure-handling-S18` | `specs/failure-handling/spec.md` | Empty-result taxonomy at the fetch boundary | Rate-limit refusal is a deferral, not an answer and not a task fault | `tests/test_fh_client_taxonomy.py` | GREEN |
| `failure-handling-S19` | `specs/failure-handling/spec.md` | Bounded retry and honest accounting for failed tasks | Deferral does not consume the retry budget | `tests/test_fh_stage_modes.py` | GREEN |
| `failure-handling-S20` | `specs/failure-handling/spec.md` | Bounded retry and honest accounting for failed tasks | Deferral cannot circulate forever | `tests/test_fh_stage_modes.py` | GREEN |
| `failure-handling-S21` | `specs/failure-handling/spec.md` | Gather-outcome fidelity | Deferred gather writes no registry outcome | `tests/test_fh_gather_fidelity.py` | GREEN |
| `date-extraction-S13` | `specs/date-extraction/spec.md` | File commit date from gathered blob pages | Plain-content transport yields NULL without a parse attempt | `tests/test_date_extraction.py` | GREEN |
| `date-extraction-S14` | `specs/date-extraction/spec.md` | File commit date from gathered blob pages | Transport switch does not worsen the fill rate | `tests/test_date_extraction.py` | GREEN |

## Automated

### File: tests/test_gt_config.py

Describe: `GatherConfig surface and cross-field validation (config/schemas.py + config/validator.py - GatherConfig absent at plan time, ImportError is the expected RED)`

- [ ] `gather-transport-S1` — it("absent section resolves to the measured-cheapest transport") <!-- WHEN Config() with no gather section THEN cfg.gather.transport=="raw", max_payload_bytes==8MiB, max_refusal_wait_s>0; AND a stage/client built with transport="html" issues its request to the github.com rendered path (pre-change behavior) -->
- [ ] `gather-transport-S2` — it("unknown transport name fails validation loudly") <!-- WHEN GatherConfig(transport="ftp") THEN ValueError naming "ftp"; AND ConfigValidator on a Config whose gather.transport was mutated to "ftp" raises ValueError naming the field -->
- [ ] `gather-transport-S3` — it("non-positive numeric gather fields fail validation loudly") <!-- WHEN GatherConfig(max_payload_bytes=0) or GatherConfig(max_refusal_wait_s=-1) THEN ValueError identifying the field; validator agrees -->
- [ ] `gather-transport-S17` — it("contradictory wait configuration is rejected") <!-- WHEN gather.max_refusal_wait_s >= queue.visibility_timeout_s (e.g. 300 vs 300, and 900 vs 300) THEN validation fails with an error naming BOTH values; AND a strictly-smaller cap validates cleanly -->

### File: tests/test_gt_transport.py

Describe: `Client-layer transport contract (search/client.py - derive_raw_url / fetch_gather_content / get_gather_transport_stats / SERVICE_TYPE_GITHUB_RAW absent at plan time; network is stubbed at the HTTP boundary so the suite stays offline, design D11)`

Address derivation uses the real shapes captured live on 2026-09-25 (design Context): `https://github.com/<owner>/<repo>/blob/<40-hex-sha>/<path>` with an optional `?plain=1` suffix, and a discovery item carrying BOTH `sha` (blob hash) and a separate `ref` (commit sha).

- [ ] `gather-transport-S4` — it("immutable revision is used verbatim") <!-- WHEN derive_raw_url on a real-shaped blob url with a 40-hex revision (and separately with ?plain=1 and with #L12 fragment) THEN result == https://raw.githubusercontent.com/<owner>/<repo>/<sha>/<path> exactly, query and fragment dropped, head_fallback counter unchanged -->
- [ ] `gather-transport-S5` — it("unusable revision falls back loudly") <!-- WHEN the revision segment is "main", "HEAD~3", a 39-char hex, or uppercase hex THEN derived address targets HEAD for the same owner/repo/path, head_fallback increments by one per call, and a WARNING names the offending link -->
- [ ] `gather-transport-S6` — it("blob content hash is never mistaken for a revision") <!-- GIVEN the captured pair sha=32cce148... (blob) and ref=2095a3bf... (revision) for the same file WHEN deriving from the revision THEN address contains the revision; AND asserting the blob-hash-derived address is rejected: it does not equal the working address and resolving it is recorded as not-found, pinning the trap measured live (blob sha as ref -> HTTP 404) -->
- [ ] `gather-transport-S7` — it("unparseable link degrades safely") <!-- WHEN derive_raw_url receives "", "not a url", "https://example.com/a/b", a /tree/ url, or a bare path THEN no exception propagates, result is None, unparseable_link increments, and fetch_gather_content on such a link raises the existing TransientFetchError family (counted by the stage) rather than an AttributeError/TypeError into the worker loop -->
- [ ] `gather-transport-S9` — it("plain-content host resolves to a real budget") <!-- WHEN GitHubClient._service is given a raw.githubusercontent.com url THEN it returns SERVICE_TYPE_GITHUB_RAW ("github_raw"), not None; AND fetch_gather_content acquires a limiter token before sending and reports success/failure afterwards (spy on _limit/_report: exactly one acquire and one report per request, success reported True on 200 and False on 5xx) -->
- [ ] `gather-transport-S10` — it("gathering does not consume the search budget") <!-- WHEN N gather fetches run on the raw transport against a client whose buckets are observed THEN the github_api bucket's remaining allowance is unchanged and no acquire was attributed to it, while the github_raw bucket consumed N -->
- [ ] `gather-transport-S11` — it("authenticated transports keep their existing budgets") <!-- WHEN transport is "html" THEN _service attribution is SERVICE_TYPE_GITHUB_WEB and the requested host is github.com; WHEN transport is "rest" THEN attribution is SERVICE_TYPE_GITHUB_API and the host is api.github.com; AND get_with_status behavior for an already-working authenticated caller (repo_meta path) is unchanged - pinned by calling it with a stubbed response and asserting the pre-existing limit/report/mark_success sequence -->
- [ ] `gather-transport-S12` — it("forbidden-with-rate-limit-marker is a deferral") <!-- WHEN the stubbed response is 403 with a body containing "secondary rate limit" THEN fetch_gather_content raises RateLimitDeferral (NOT NetworkError/"Authentication failed"), the task is not abandoned after one attempt, deferred_secondary increments, and dropped_auth does NOT -->
- [ ] `gather-transport-S13` — it("genuine authentication failure drops loudly once") <!-- WHEN the stubbed response is 401, and separately 403 with a body carrying no rate-limit marker THEN the outcome is a loud counted drop after ONE attempt: exactly one HTTP request issued, dropped_auth increments by one, a WARNING is logged, and no requeue/deferral counter moves -->
- [ ] `gather-transport-S14` — it("missing resource drops loudly once") <!-- WHEN the stubbed response is 404 for the addressed revision/path THEN exactly one request is issued, dropped_not_found increments, the outcome is terminal (no retry, no deferral), and it is not reported as a transient failure -->
- [ ] `gather-transport-S15` — it("finite quota exhaustion defers without burning attempts") <!-- WHEN the stubbed response is 429 with Retry-After: 7 THEN RateLimitDeferral is raised carrying wait_s==7, deferred_rate_limit increments, the signalled wait is honored; AND when Retry-After exceeds max_refusal_wait_s the effective wait is clamped to the cap (runtime clamp of design D5) -->
- [ ] `gather-transport-S16` — it("actor-scoped abuse refusal pauses the stage") <!-- WHEN the stubbed response is 403 with an abuse-detection body and NO Retry-After and NO X-RateLimit-Reset THEN an ERROR is logged, deferred_secondary increments, the signalled stage pause is bounded by max_refusal_wait_s, and no further request is issued inside the same call -->
- [ ] `gather-transport-S19` — it("plainer payload loses no findings") <!-- USING the sanitized live-captured pair tests/fixtures/live_2026_09_gt_raw_payload.txt and live_2026_09_gt_blob_embedded.html (same file, same immutable revision, both transports, captured 2026-09-25) WHEN both payloads go through the project's own client.extract() with r"AKIA[0-9A-Z]{16}" THEN set(raw) >= set(html) AND set(raw) is non-empty (non-vacuous: the captured file carries one such token on both surfaces) -->
- [ ] `gather-transport-S20` — it("oversized payload is truncated and still searched") <!-- WHEN the stubbed payload exceeds max_payload_bytes (set small, e.g. 64) with a match placed inside the retained prefix and another beyond the cap THEN reading stops at the cap, truncated increments, the prefix match IS returned by extraction, the beyond-cap match is not, and no fetch failure is raised -->

### File: tests/test_gt_stage_integration.py

Describe: `Stage-level integration (AcquisitionStage over a real durable SqliteTaskQueue, mirroring tests/test_tq_stage_backend.py harness; network stubbed at the HTTP boundary)`

- [ ] `gather-transport-S8` — it("persisted tasks survive a transport switch") <!-- WHEN AcquisitionTasks are enqueued into a real SqliteTaskQueue under transport="html", the queue is closed, the process-equivalent is restarted with transport="raw", and the rows are recovered THEN every recovered task's stored payload still carries the ORIGINAL github.com blob url byte-for-byte (no rewrite), and processing succeeds under the new transport (stubbed raw fetch returns content, keys extracted) -->
- [ ] `gather-transport-S18` — it("a deferred task is never executed twice") <!-- GIVEN a real SqliteTaskQueue with visibility_timeout_s larger than the configured wait cap WHEN one worker is repeatedly deferred on a published rate limit while a second worker consumes other rows THEN at no sampling point is the same seq claimed by two workers, the deferred row's claim never expires while its worker sleeps (sweep_expired_claims() reclaims 0), and after final acknowledgement the execution count for that task is exactly 1 -->
- [ ] `gather-transport-S21` — it("transport economics are observable") <!-- WHEN a short run issues stubbed gather traffic on raw plus one deferral, one head-fallback and one truncation THEN get_gather_transport_stats() exposes bytes and request counts keyed per transport, refusals split by classification (rate-limit vs secondary vs auth vs not-found), plus deferrals, head fallbacks and truncations - all as integers, matching the flat Dict[str,int] shape of the existing get_date_parse_stats() -->

### File: tests/test_fh_client_taxonomy.py (appended to the existing describe block)

- [ ] `failure-handling-S18` — it("rate-limit refusal is a deferral, not an answer and not a task fault") <!-- WHEN a gather fetch is refused on a published rate limit before any payload was obtained THEN the outcome is a RateLimitDeferral, it is NOT an empty-result answer (not collapsed to a zero-key success), it is not recorded as a completed gather, and it increments a deferral counter distinct from failure_empties_detected / total_errors -->

### File: tests/test_fh_stage_modes.py (appended to the existing describe block)

- [ ] `failure-handling-S19` — it("deferral does not consume the retry budget") <!-- WHEN a started strict-mode stage defers a task on a published rate limit and the same task later succeeds within the run THEN task.attempts is unchanged by the deferral, tasks_dropped_max_retries stays 0, tasks_requeued does not account the deferral, total_errors does not increment for it, and the successful completion is accounted normally (total_processed +1) -->
- [ ] `failure-handling-S20` — it("deferral cannot circulate forever") <!-- GIVEN a real SqliteTaskQueue with a short max_age_hours WHEN a task is deferred repeatedly and its original created_at passes the age gate THEN the existing startup/age-gate maintenance purges it with a loud WARNING stating the count, and it is no longer claimable - deferral is bounded by the queue age gate, not by the retry budget -->

### File: tests/test_fh_gather_fidelity.py (appended to the existing describe block)

- [ ] `failure-handling-S21` — it("deferred gather writes no registry outcome") <!-- WHEN an acquisition task's fetch is deferred on a published rate limit THEN the registry shows no visit_status transition for that link and no link_coverage row, and a subsequent gather-skip decision for the same (provider, patterns_hash) still treats the link as never-attempted (contrast with the existing s7 pin where a FAILED fetch does write visit_status='failed') -->

### File: tests/test_date_extraction.py (appended; three existing pins re-scoped, none deleted)

Re-scopes (repair, not replacement): `test_s4_date_captured_from_blob_html`, `test_s5_layout_without_markers_yields_null_safely` and `test_s12_live_blob_page_yields_null_safely` keep every original assertion and gain an explicit `transport="html"` precondition, because after this change the rendered page is no longer the only payload shape reaching the extractor.

- [ ] `date-extraction-S13` — it("plain-content transport yields NULL without a parse attempt") <!-- WHEN acquisition runs under transport="raw" with the live-captured raw fixture THEN metadata["file_commit_date"] is None, the markup parser is never invoked (spy on _extract_file_commit_date: zero calls), key extraction proceeds unaffected, date_metrics records the web attempt so the fill-rate drift detector still fires, and no exception reaches the worker loop -->
- [ ] `date-extraction-S14` — it("transport switch does not worsen the fill rate") <!-- WHEN the same fixture-backed workload is gathered under transport="html" and under transport="raw" THEN date_fill_rate_web under raw is >= the value under html (both are 0.0 on the September 2026 captures, so the pin is a non-regression guard that announces drift if GitHub ever restores the attributes on one surface only) -->

## Manual

<!-- All 27 scenarios are automatable offline against stubbed HTTP boundaries plus
     sanitized live-captured fixtures; nothing here is manual. -->

None. The real-network confirmations that cannot be expressed as offline unit tests are scheduled as the live-verification gate in `tasks.md` (Group 8), following the discipline established by the archived `fix-queue-persistence-under-load`: temp workspace under `/tmp`, credentials read at runtime from the git-ignored `.secrets`, project `logs/` untouched, evidence recorded in `verification.md`, and nothing secret-bearing committed. Those checks are: the raw transport's refusal behavior under sustained real load, the true html/raw byte ratio on a representative sample (three independent measurements so far: 30.6x, 32.9x, 32.0x), and confirmation that the `github_raw` bucket does not perturb the 10/min code-search budget in a live run.
