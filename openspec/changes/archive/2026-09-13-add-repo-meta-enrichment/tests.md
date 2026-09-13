# Test Plan: add-repo-meta-enrichment

<!-- Derived mechanically from specs/repo-meta-enrichment/spec.md. One scenario = one
     test or one Manual entry. IDs stable: never renumber, append only.
     NOTE (planning mode): .py test files are authored in apply-phase task group 1,
     AFTER the early live probe (task 1.1) captures authoritative fixtures — per
     openspec/config.yaml rules, external-contract tests are built on live-captured
     samples, synthetic vectors are labeled supplements. Expected initial state: RED. -->

## RED baseline (task 1.3)

`python3 -m pytest tests/test_repo_meta_enrichment.py tests/test_enrichment_integration.py -q`
→ both modules fail collection with `ModuleNotFoundError: No module named
'storage.repo_meta'` (no cache module, no `repos` table, no trigger hooks yet).
This is the expected RED for all twelve scenarios.

## Traceability

| Scenario ID | Spec | Requirement | Scenario | Test file | Status |
|-------------|------|-------------|----------|-----------|--------|
| `repo-meta-enrichment-S1` | `specs/repo-meta-enrichment/spec.md` | Repository metadata acquisition | Successful fetch populates cache and link columns | tests/test_repo_meta_enrichment.py | GREEN |
| `repo-meta-enrichment-S2` | `specs/repo-meta-enrichment/spec.md` | Repository metadata acquisition | Taken-down repository marks gone | tests/test_repo_meta_enrichment.py | GREEN |
| `repo-meta-enrichment-S3` | `specs/repo-meta-enrichment/spec.md` | Repository metadata acquisition | Transient failure degrades fail-open | tests/test_repo_meta_enrichment.py | GREEN |
| `repo-meta-enrichment-S4` | `specs/repo-meta-enrichment/spec.md` | Conditional refresh economics | Unchanged repository refreshes via 304 | tests/test_repo_meta_enrichment.py | GREEN |
| `repo-meta-enrichment-S5` | `specs/repo-meta-enrichment/spec.md` | Conditional refresh economics | Changed repository replaces values | tests/test_repo_meta-enrichment.py | GREEN |
| `repo-meta-enrichment-S6` | `specs/repo-meta-enrichment/spec.md` | Cache-first lazy triggering | Fresh cache serves offline | tests/test_enrichment_integration.py | GREEN |
| `repo-meta-enrichment-S7` | `specs/repo-meta-enrichment/spec.md` | Cache-first lazy triggering | Stale cache at skip evaluation triggers one shared refresh | tests/test_enrichment_integration.py | GREEN |
| `repo-meta-enrichment-S8` | `specs/repo-meta-enrichment/spec.md` | Cache-first lazy triggering | Duplicate encounters fetch once | tests/test_enrichment_integration.py | GREEN |
| `repo-meta-enrichment-S9` | `specs/repo-meta-enrichment/spec.md` | Credential and throttling inheritance | Rate-limited credential rotates like search | tests/test_enrichment_integration.py | GREEN |
| `repo-meta-enrichment-S10` | `specs/repo-meta-enrichment/spec.md` | Credential and throttling inheritance | Tokenless deployment disables silently | tests/test_enrichment_integration.py | GREEN |
| `repo-meta-enrichment-S11` | `specs/repo-meta-enrichment/spec.md` | Flag isolation and crash-safe durability | Disabled flag means zero footprint | tests/test_enrichment_integration.py | GREEN |
| `repo-meta-enrichment-S12` | `specs/repo-meta-enrichment/spec.md` | Flag isolation and crash-safe durability | Crash-resume completes without redo | tests/test_repo_meta_enrichment.py | GREEN |

## Automated

### File: tests/test_repo_meta_enrichment.py

Describe: Acquisition, conditional refresh, takedown, durability (unit level, mocked HTTP)

<!-- Harness: tmp registry via change-1 writer API; mocked transport returning LIVE-CAPTURED fixture payloads (task 1.1: 200 full body, 304 empty + ETag echo, 404, 500) with provenance headers; SYNTHETIC vectors (field-absent variants) labeled as supplements. RED state: storage/repo_meta.py absent (ImportError), repos table missing. -->

- [x] `repo-meta-enrichment-S1` — it("Successful fetch populates cache and link columns") <!-- WHEN novel repo gathered, mocked 200 with pushed_at/size THEN repos row persisted (values+etag+fetched_at) AND links rows of that repo carry non-NULL repo_pushed_at/repo_size_kb after merge; assert UPDATE-only (no phantom link rows) -->
- [x] `repo-meta-enrichment-S2` — it("Taken-down repository marks gone") <!-- WHEN mocked 404 THEN repos.gone=1, no exception escapes worker, second encounter within TTL issues zero requests (mock call count) -->
- [x] `repo-meta-enrichment-S3` — it("Transient failure degrades fail-open") <!-- WHEN mocked 500/network raise THEN enrichment_failures+1, warn-once, link columns NULL, gather flow completes -->
- [x] `repo-meta-enrichment-S4` — it("Unchanged repository refreshes via 304") <!-- WHEN stale entry refreshed with If-None-Match and mock answers 304 THEN values+etag unchanged, fetched_at advanced, enrichment_304s+1, NO merge op enqueued (writer spy) -->
- [x] `repo-meta-enrichment-S5` — it("Changed repository replaces values") <!-- WHEN refresh answers 200 with newer pushed_at THEN cache fields+etag replaced AND links merge enqueued/visible -->
- [x] `repo-meta-enrichment-S12` — it("Crash-resume completes without redo") <!-- WHEN writer persisted half the run's repos then harness simulates kill (drop in-memory state, reopen WAL DB) and resumes THEN only stale/missing entries fetched; persisted repos produce zero new mock calls within TTL -->

### File: tests/test_enrichment_integration.py

Describe: Lazy triggers, dedup, credential inheritance, flag isolation (stage-level, mocked client)

<!-- Harness: AcquisitionStage/SearchStage workers driven with synthetic tasks against tmp registry + mocked GitHubClient layer (bucket/cooldown behavior stubbed per change-B patterns); tokenless pool config variant. RED state: trigger hooks absent. -->

- [x] `repo-meta-enrichment-S6` — it("Fresh cache serves offline") <!-- WHEN page's repos all cached fresh THEN zero enrichment HTTP calls during processing (mock call count == 0) -->
- [x] `repo-meta-enrichment-S7` — it("Stale cache at skip evaluation triggers one shared refresh") <!-- WHEN skip-eval batch has 5 links of one stale repo THEN exactly 1 conditional refresh; outcome applied to all 5 decisions; on refresh failure decisions proceed fail-open (condition 3 vacuous) -->
- [x] `repo-meta-enrichment-S8` — it("Duplicate encounters fetch once") <!-- WHEN same novel repo across 30 links on 2 pages in one run THEN enrichment_fetches delta == 1 for it -->
- [x] `repo-meta-enrichment-S9` — it("Rate-limited credential rotates like search") <!-- WHEN fetch hits rate-limit signal THEN credential marked limited (GithubCredentialState asserted) and fetch retried on next credential or abandoned fail-open; worker never blocks past bucket wait -->
- [x] `repo-meta-enrichment-S10` — it("Tokenless deployment disables silently") <!-- WHEN token pool empty, web-only config THEN zero enrichment requests, dates NULL, ≤1 informational log line, pipeline completes -->
- [x] `repo-meta-enrichment-S11` — it("Disabled flag means zero footprint") <!-- WHEN enrichment.enabled=off full simulated run THEN request count and registry writes identical to baseline A/B; no enrichment metrics emitted -->

## Manual

<!-- None: all twelve scenarios are automatable offline with live-captured fixtures and mocked transport. The real-quota validation (units consumed vs unique repos, 304 share on second run, fill-rate and push_signal_coverage rise) is the staging economics gate executed as task 6.3 — a promotion measurement, not a spec scenario. -->
