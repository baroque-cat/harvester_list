# Test Plan: add-date-extraction

<!-- Derived mechanically from specs/date-extraction/spec.md. One scenario = one test
     or one Manual entry. IDs stable: never renumber, append only.
     NOTE (planning mode): .py test files are authored in apply-phase task group 1.
     AMNESTY (September 2026): S11/S12 appended after live probe invalidated the
     free-source premise (design D7). They are regression PINS against live-captured
     fixtures — expected to pass immediately (degradation paths already implemented),
     so no RED phase applies to them. -->

## Traceability

| Scenario ID | Spec | Requirement | Scenario | Test file | Status |
|-------------|------|-------------|----------|-----------|--------|
| `date-extraction-S1` | `specs/date-extraction/spec.md` | Repository metadata from API search results | Metadata extracted from well-formed items | tests/test_date_extraction.py | GREEN |
| `date-extraction-S2` | `specs/date-extraction/spec.md` | Repository metadata from API search results | Missing repository object degrades to NULL | tests/test_date_extraction.py | GREEN |
| `date-extraction-S3` | `specs/date-extraction/spec.md` | Repository metadata from API search results | pushed_at preferred over updated_at | tests/test_date_extraction.py | GREEN |
| `date-extraction-S4` | `specs/date-extraction/spec.md` | File commit date from gathered blob pages | Date captured from blob HTML | tests/test_date_extraction.py | GREEN |
| `date-extraction-S5` | `specs/date-extraction/spec.md` | File commit date from gathered blob pages | Layout without markers yields NULL safely | tests/test_date_extraction.py | GREEN |
| `date-extraction-S6` | `specs/date-extraction/spec.md` | Transport asymmetry enforced | Web search produces no dates | tests/test_date_flow.py | GREEN |
| `date-extraction-S7` | `specs/date-extraction/spec.md` | Non-regressing delivery into the registry | Known date survives later NULL observation | tests/test_date_flow.py | GREEN |
| `date-extraction-S8` | `specs/date-extraction/spec.md` | Non-regressing delivery into the registry | Fresher date replaces older | tests/test_date_flow.py | GREEN |
| `date-extraction-S9` | `specs/date-extraction/spec.md` | Fill-rate observability | Rates computed over a run | tests/test_date_flow.py | GREEN |
| `date-extraction-S10` | `specs/date-extraction/spec.md` | Fail-open parsing | Garbage payload does not break search or gather | tests/test_date_extraction.py | GREEN |
| `date-extraction-S11` | `specs/date-extraction/spec.md` | Repository metadata from API search results | Live-captured trimmed payload yields NULLs across the board | tests/test_date_extraction.py | GREEN |
| `date-extraction-S12` | `specs/date-extraction/spec.md` | File commit date from gathered blob pages | Live-captured blob page yields NULL safely | tests/test_date_extraction.py | GREEN |

## Automated

### File: tests/test_date_extraction.py

Describe: Parser-level extraction (fixtures, no network)

<!-- Uses tests/fixtures/: api_search_page.json (SYNTHETIC unit vector — well-formed + item without repository), blob_page_with_relative_time.html (SYNTHETIC unit vector), blob_page_without_markers.html, garbage payloads; PLUS live_2026_09_api_search_page.json and live_2026_09_blob_page.html (LIVE-CAPTURED, provenance headers per design D6 — authoritative external-contract fixtures, added by amnesty task 7.5). S1–S5/S10 GREEN; S11/S12 GREEN as live-captured regression pins (no RED phase expected — degradation paths were already implemented; task 7.6 also added the `missing_date` counter so field-absence is counted). -->

- [x] `date-extraction-S1` — it("Metadata extracted from well-formed items") <!-- WHEN fixture item has html_url+repository.pushed_at+size THEN enrichment map holds parsed epoch+int size AND returned URL set equals pre-change set -->
- [x] `date-extraction-S2` — it("Missing repository object degrades to NULL") <!-- WHEN item lacks repository THEN URL still in result set, metadata NULLs, warning counter incremented -->
- [x] `date-extraction-S3` — it("pushed_at preferred over updated_at") <!-- WHEN both present THEN recorded ts == parsed pushed_at -->
- [x] `date-extraction-S4` — it("Date captured from blob HTML") <!-- WHEN page has multiple relative-time datetimes THEN file_commit_date == max of them -->
- [x] `date-extraction-S5` — it("Layout without markers yields NULL safely") <!-- WHEN page has zero relative-time elements THEN NULL + warning counter, key extraction unaffected -->
- [x] `date-extraction-S10` — it("Garbage payload does not break search or gather") <!-- WHEN structurally invalid item / undecodable bytes THEN NULLs, counters, no exception escapes -->
- [x] `date-extraction-S11` — it("Live-captured trimmed payload yields NULLs across the board") <!-- WHEN parsing live_2026_09_api_search_page.json (trimmed repository objects, no date/size fields) THEN all metadata entries NULL, warnings counted (missing_date counter), URL set identical to pre-change, date_fill_rate_api == 0.0 -->
- [x] `date-extraction-S12` — it("Live-captured blob page yields NULL safely") <!-- WHEN collect() parses live_2026_09_blob_page.html (client-side-rendered timestamps) THEN file_commit_date NULL, warning counter increments, key extraction unaffected, date_fill_rate_web == 0.0 -->

### File: tests/test_date_flow.py

Describe: Delivery through StageOutput into the registry (tmp workspace, synthetic outputs)

<!-- Depends on add-link-registry test harness (conftest fixtures). Authored in apply-phase group 1; RED baseline (missing StageOutput.link_metadata / COALESCE upsert) confirmed, now GREEN. -->

- [x] `date-extraction-S6` — it("Web search produces no dates") <!-- WHEN search-stage output built from web transport results THEN link_metadata carries no repo_pushed_at/repo_size_kb; registry rows keep NULL until gather -->
- [x] `date-extraction-S7` — it("Known date survives later NULL observation") <!-- WHEN row has repo_pushed_at and re-observation delivers NULL THEN stored value unchanged -->
- [x] `date-extraction-S8` — it("Fresher date replaces older") <!-- WHEN re-observation delivers newer pushed_at THEN stored value updated -->
- [x] `date-extraction-S9` — it("Rates computed over a run") <!-- WHEN 100 API items (95 dated) and 50 gathers (40 dated) processed THEN date_fill_rate_api==0.95, date_fill_rate_web==0.80 in run stats -->

## Manual

<!-- None: all twelve scenarios are automatable offline against recorded/live-captured fixtures and the tmp-workspace registry harness. The live staging probe (task 5.3) was EXECUTED in September 2026 — results documented in design D7 and the proposal's amnesty section; it validated degradation in production rather than serving as a pending manual scenario. -->
