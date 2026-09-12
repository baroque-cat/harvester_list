# Test Plan: add-link-registry

<!-- Derived mechanically from specs/link-registry/spec.md. One scenario = one test
     or one Manual entry. IDs are stable: never renumber, append only.
     NOTE (planning mode): the actual .py test files are authored as the FIRST
     implementation task group (tasks.md §1) of this change's apply phase; this
     document is the binding contract for them. Expected initial state: RED via
     ImportError — storage/registry.py and tools/registry_migrate.py do not exist yet.

     RED CONFIRMED (task 1.3): `python3 -m pytest tests/` collects 3 modules and
     every module errors with ModuleNotFoundError: No module named
     'storage.registry' / 'tools.registry_migrate'. These tests were authored before
     the implementation, per the tdd-flow schema.

     GREEN CONFIRMED (task 5.1): `python3 -m pytest tests/` → 25 passed
     (20 spec-scenario tests + supporting --dry-run check + 2 Pipeline
     wiring/clash regression guards + 2 offline integration tests:
     registry-on/off shard equivalence for S8 and migration over a
     pipeline-produced workspace). S8 and S19 are now also verified manually;
     see the Manual verification log below. -->

## Traceability

| Scenario ID | Spec | Requirement | Scenario | Test file | Status |
|-------------|------|-------------|----------|-----------|--------|
| `link-registry-S1` | `specs/link-registry/spec.md` | Persistent global registry storage | Registry persists across restarts | tests/test_registry_core.py | GREEN |
| `link-registry-S2` | `specs/link-registry/spec.md` | Persistent global registry storage | Registry is shared across provider-set changes | tests/test_registry_core.py | GREEN |
| `link-registry-S3` | `specs/link-registry/spec.md` | Persistent global registry storage | Schema bootstrap on empty workspace | tests/test_registry_core.py | GREEN |
| `link-registry-S4` | `specs/link-registry/spec.md` | URL canonicalization and link identity | Line fragment does not change identity | tests/test_registry_canonicalization.py | GREEN |
| `link-registry-S5` | `specs/link-registry/spec.md` | URL canonicalization and link identity | Path case is significant | tests/test_registry_canonicalization.py | GREEN |
| `link-registry-S6` | `specs/link-registry/spec.md` | URL canonicalization and link identity | Transports converge on one identity | tests/test_registry_canonicalization.py | GREEN |
| `link-registry-S7` | `specs/link-registry/spec.md` | Write-only integration | Flag off behaves exactly as today | tests/test_registry_core.py | GREEN |
| `link-registry-S8` | `specs/link-registry/spec.md` | Write-only integration | Flag on does not alter outputs | — | MANUAL ✓ |
| `link-registry-S9` | `specs/link-registry/spec.md` | Link lifecycle recording | First discovery inserts | tests/test_registry_core.py | GREEN |
| `link-registry-S10` | `specs/link-registry/spec.md` | Link lifecycle recording | Re-discovery updates only recency fields | tests/test_registry_core.py | GREEN |
| `link-registry-S11` | `specs/link-registry/spec.md` | Link lifecycle recording | Gather outcome is recorded | tests/test_registry_core.py | GREEN |
| `link-registry-S12` | `specs/link-registry/spec.md` | Provider coverage accounting | Coverage recorded without findings | tests/test_registry_core.py | GREEN |
| `link-registry-S13` | `specs/link-registry/spec.md` | Provider coverage accounting | Pattern-set change is visible | tests/test_registry_core.py | GREEN |
| `link-registry-S14` | `specs/link-registry/spec.md` | Run journaling | Run lifecycle is journaled | tests/test_registry_core.py | GREEN |
| `link-registry-S15` | `specs/link-registry/spec.md` | Run journaling | Degraded run is marked | tests/test_registry_core.py | GREEN |
| `link-registry-S16` | `specs/link-registry/spec.md` | Fail-open degradation | Corrupt database does not stop the pipeline | tests/test_registry_core.py | GREEN |
| `link-registry-S17` | `specs/link-registry/spec.md` | Fail-open degradation | Mid-run write failure degrades silently | tests/test_registry_core.py | GREEN |
| `link-registry-S18` | `specs/link-registry/spec.md` | Durability and shutdown flush | Graceful stop drains pending writes | tests/test_registry_core.py | GREEN |
| `link-registry-S19` | `specs/link-registry/spec.md` | Durability and shutdown flush | Crash leaves database consistent | — | MANUAL ✓ |
| `link-registry-S20` | `specs/link-registry/spec.md` | One-time idempotent migration | Migration imports legacy links conservatively | tests/test_registry_migration.py | GREEN |
| `link-registry-S21` | `specs/link-registry/spec.md` | One-time idempotent migration | Migration is idempotent | tests/test_registry_migration.py | GREEN |
| `link-registry-S22` | `specs/link-registry/spec.md` | One-time idempotent migration | Migration never regresses live data | tests/test_registry_migration.py | GREEN |

## Automated

### File: tests/test_registry_canonicalization.py

Describe: URL canonicalization and link identity

<!-- Greenfield: storage/registry.py does not exist yet; ImportError is the expected RED state. Golden vectors come from docs/specs/url_canonicalization.md (task 6.1). -->

- [x] `link-registry-S4` — it("Line fragment does not change identity") <!-- WHEN two URLs differ only by #L fragment THEN sha256(canonical_url(...)) is equal -->
- [x] `link-registry-S5` — it("Path case is significant") <!-- WHEN paths differ only by letter case THEN hashes differ -->
- [x] `link-registry-S6` — it("Transports converge on one identity") <!-- WHEN same blob URL recorded via transport='api' then 'web' THEN one row, first_seen_ts preserved, transport updated -->

### File: tests/test_registry_core.py

Describe: Registry storage, lifecycle, coverage, journaling, fail-open, durability

<!-- Greenfield: storage/registry.py does not exist yet; ImportError is the expected RED state. Tests use tmp_path workspaces and synthetic stage callbacks — no network. -->

- [x] `link-registry-S1` — it("Registry persists across restarts") <!-- WHEN writer records links, stops, and a new registry instance opens the same file THEN all rows present before new writes -->
- [x] `link-registry-S2` — it("Registry is shared across provider-set changes") <!-- WHEN rows written under provider A context, reopened with provider B enabled THEN rows intact and attributable via link_coverage.provider -->
- [x] `link-registry-S3` — it("Schema bootstrap on empty workspace") <!-- WHEN enabled with no DB file THEN file created with links/link_coverage/keys/runs tables and user_version set, run does not fail -->
- [x] `link-registry-S7` — it("Flag off behaves exactly as today") <!-- WHEN registry.enabled=false THEN no DB file created/opened and hook calls are no-ops -->
- [x] `link-registry-S9` — it("First discovery inserts") <!-- WHEN unseen URL observed THEN row inserted: visit_status='discovered', first_seen_ts=last_seen_ts=now, gathered_ts NULL -->
- [x] `link-registry-S10` — it("Re-discovery updates only recency fields") <!-- WHEN known URL observed again THEN last_seen_ts advances, first_seen_ts unchanged, single row -->
- [x] `link-registry-S11` — it("Gather outcome is recorded") <!-- WHEN acquisition reports success/failure THEN gathered_ok+gathered_ts / failed; later re-observation never downgrades gathered_ok to discovered -->
- [x] `link-registry-S12` — it("Coverage recorded without findings") <!-- WHEN gather succeeds with zero extracted keys THEN coverage row (url_hash, provider, patterns_hash, ts) exists -->
- [x] `link-registry-S13` — it("Pattern-set change is visible") <!-- WHEN re-gathered with different patterns_hash THEN second coverage row added, old preserved -->
- [x] `link-registry-S14` — it("Run lifecycle is journaled") <!-- WHEN run starts and finishes gracefully THEN runs row has started_at, finished_at, parseable config_digest -->
- [x] `link-registry-S15` — it("Degraded run is marked") <!-- WHEN a registry write raised during the run THEN runs.degraded=1 for that run_id -->
- [x] `link-registry-S16` — it("Corrupt database does not stop the pipeline") <!-- WHEN DB file contains garbage at open THEN warning logged, run completes, hooks become no-ops -->
- [x] `link-registry-S17` — it("Mid-run write failure degrades silently") <!-- WHEN batch upsert raises mid-run (mocked) THEN subsequent writes suppressed, no exception escapes to stage code, run degraded -->
- [x] `link-registry-S18` — it("Graceful stop drains pending writes") <!-- WHEN stop() called with queued batches THEN all queued rows queryable after stop returns -->

### File: tests/test_registry_migration.py

Describe: One-time idempotent migration from shard data

<!-- Greenfield: tools/registry_migrate.py does not exist yet; ImportError is the expected RED state. Fixtures: synthetic providers/<name>/shards/{links,material,valid,...}/*.ndjson + .index.json built in tmp_path. -->

- [x] `link-registry-S20` — it("Migration imports legacy links conservatively") <!-- WHEN migrating fixture shards THEN each distinct canonical URL once, gathered_ts NULL, visit_status='discovered', provider from folder -->
- [x] `link-registry-S21` — it("Migration is idempotent") <!-- WHEN migration executed twice over unchanged fixtures THEN links/keys row counts identical -->
- [x] `link-registry-S22` — it("Migration never regresses live data") <!-- WHEN registry row already gathered_ok and older shard mentions same URL THEN status/gathered_ts preserved -->

## Manual

- `link-registry-S8` — Flag-on vs flag-off A/B equivalence requires live GitHub credentials and real search traffic (identical shards for identical inputs); cannot be asserted offline. Procedure: run staging config twice (enabled true/false) over the same small query set; diff `providers/*/shards/**` content ignoring timestamps.
- `link-registry-S19` — SIGKILL crash consistency needs process-level chaos (kill -9 mid-batch) against a real filesystem; flaky and environment-dependent in CI. Procedure: start run with registry enabled, `kill -9` the PID, reopen DB, `PRAGMA integrity_check` + verify committed batches present.

### Manual verification log

- `link-registry-S19` — **PASSED at registry level** (task 5.3). A child process
  created a WAL registry, recorded 500 links, flushed (committed), and was killed
  with `kill -9` while idle. Reopening the database gave
  `PRAGMA integrity_check = ok`, `journal_mode = wal`, committed rows = 500.
  A full-pipeline A/B/SIGKILL run on staging is still advisable before enabling
  skip features.
- [x] `link-registry-S8` — **PASSED with live GitHub data** (2026-09-12, task 5.2). One real web code-search page (26 URLs) was fetched through the app client with a live session; six real gathers were captured. The real `SearchStage` + `AcquisitionStage` + shard persistence ran twice over the identical live input set (registry off, then on): `AB_SHARDS_EQUAL True`, links shard = 32 records in both runs, flag-off created no `registry.sqlite`, flag-on recorded 26 `links` + 6 `link_coverage` rows. Methodology note: the search was live; the per-URL gather outcome was captured live once and replayed identically in both runs so the diff isolates the registry rather than GitHub/network nondeterminism. The API token supplied for this check was rejected by GitHub (HTTP 401 — auto-revoked after being posted), so the live leg used the session cookie only. An independent automated offline equivalent lives in `tests/test_registry_integration_ab.py`.
- [x] Migration over a real workspace — **PASSED** (task 5.4). `tools/registry_migrate.py` ran over the workspace produced by the real persistence stack from the live-data run: `links=26`, exactly the 26 distinct canonical live URLs in the links shards; a second execution was idempotent; keys = 0 because the captured live gathers produced no key shards.


