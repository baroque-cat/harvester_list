# Tasks: add-link-registry

## 1. Test infrastructure & RED baseline

- [x] 1.1 Add `requirements-dev.txt` with `pytest`; create `tests/` package with `conftest.py` (tmp-workspace fixture, synthetic shard-builder helpers)
- [x] 1.2 Author test files from tests.md: `tests/test_registry_canonicalization.py` (link-registry-S4..S6), `tests/test_registry_core.py` (S1..S3, S7, S9..S18), `tests/test_registry_migration.py` (S20..S22); assertions encode spec THEN clauses verbatim
- [x] 1.3 Run pytest and confirm every new test fails for the expected reason (ImportError: `storage.registry` / `tools.registry_migrate` do not exist) — record RED status in tests.md

## 2. Registry core (`storage/registry.py`)

- [x] 2.1 Implement DDL bootstrap: schema creation, `PRAGMA journal_mode=WAL`, `PRAGMA user_version`, indexes (link-registry-S3)
- [x] 2.2 Implement `canonical_url()` + `url_hash()` per design D3; golden-vector table drives tests (link-registry-S4, S5)
- [x] 2.3 Implement `RegistryWriter`: bounded queue, single daemon thread, batched `executemany` upserts, periodic flush (`flush_interval`), drop-counter on full queue (design D2)
- [x] 2.4 Implement link lifecycle upserts: discover (insert first_seen/last_seen, transport, query_origin), re-observe (last_seen only), gather outcome (gathered_ts, visit_status transitions with no-downgrade rule) (link-registry-S1, S6, S9, S10, S11)
- [x] 2.5 Implement coverage upsert `(url_hash, provider, patterns_hash)` written on every successful gather including zero-finding gathers; compute `patterns_hash` at provider construction (link-registry-S12, S13)
- [x] 2.6 Implement run journaling: start/finish rows, `config_digest` (providers, use_api flags, pattern hashes, flag states), degraded marker (link-registry-S14, S15)
- [x] 2.7 Implement fail-open guard: catch-all around all entry points, warn-once-per-kind, suppress writes after failure, set degraded; corrupt-file open path (link-registry-S16, S17)
- [x] 2.8 Implement `stop()`: drain queue, final flush, close connection; wire durability expectations (link-registry-S18)

## 3. Pipeline wiring & configuration

- [x] 3.1 Add `RegistryConfig` to `config/schemas.py` (enabled=false, path, batch_size=50, flush_interval=5) + parsing in `config/loader.py` + validation rules in `config/validator.py`; extend `examples/config-full.yaml`
- [x] 3.2 Initialize/shutdown registry in `manager/pipeline.py` next to `result_manager` (start after, stop before result_manager flush completes); no-op singleton when disabled (link-registry-S7)
- [x] 3.3 Add write hook in SearchStage at the `output.add_links` point (`stage/definition.py:127`): enqueue discovered links with transport + query_origin
- [x] 3.4 Add write hooks in AcquisitionStage near `stage/definition.py:363`: gather success → gathered_ok + coverage row; failure → failed
- [x] 3.5 Journal run start/finish from the app lifecycle (`manager/task.py` start path / shutdown path), including degraded propagation
- [x] 3.6 Verify zero read-paths: grep audit that no stage consults the registry for decisions (spec: Write-only integration)

## 4. Migration tool (`tools/registry_migrate.py`)

- [x] 4.1 Implement shard scan: `providers/*/shards/links/*.ndjson` (+ `.index.json` timestamps) → canonical URL, provider from folder, `seen_ts` from index; conservative `visit_status='discovered'`, `gathered_ts=NULL` (link-registry-S20)
- [x] 4.2 Implement keys import: `shards/material|valid|invalid|no_quota|wait_check` + `summary.json` → `keys` rows with statuses and masked refs
- [x] 4.3 Implement never-regress upsert policy + idempotency; `--dry-run` and `--workspace` flags (link-registry-S21, S22)

## 5. GREEN & verification

- [x] 5.1 Drive all automated tests to GREEN; update Status column in tests.md
- [x] 5.2 Manual: A/B staging runs (registry on/off) produce identical shards (link-registry-S8); document procedure + result
- [x] 5.3 Manual: SIGKILL mid-run → `PRAGMA integrity_check` passes, committed batches intact (link-registry-S19); document procedure + result
- [x] 5.4 Manual: run migration over a real existing workspace once; sanity-check `links` row count ≈ links-shard record count

## 6. Documentation (Phase-0 deliverables)

- [x] 6.1 Write `docs/specs/url_canonicalization.md`: canon rules, rationale (false-known vs false-novel asymmetry), golden vector table used by tests
- [x] 6.2 Write `docs/specs/registry_flags_metrics.md`: feature-flag matrix (`registry.enabled`, `skip_known`, `early_stop`, `check_skip`, `recheck_cron`) and metrics dictionary (`novel_links_per_run`, `skipped_known`, `regathered_*`, `early_stop_*`, `check_skipped_by_status`, `date_fill_rate_*`) with owning-change annotations
- [x] 6.3 README section: registry purpose, config, migration usage, ops notes (local-FS requirement, VACUUM, disk estimate)

## Manual verification results

- 5.2 — **PASSED with live GitHub data** (2026-09-12). One real web
  code-search page (26 URLs) was fetched through the app client using a live
  session; six real gathers were captured. The real `SearchStage` +
  `AcquisitionStage` + shard persistence then ran twice over the identical live
  input set (registry off, then on): `AB_SHARDS_EQUAL True`, links shard = 32
  records in both runs, flag-off produced no `registry.sqlite`, flag-on recorded
  26 `links` + 6 `link_coverage` rows. The API token supplied for this check was
  rejected by GitHub (HTTP 401 — auto-revoked once posted), so the live leg used
  the session cookie only. See tests.md → Manual verification log.
- 5.4 — **PASSED over a workspace produced by the real stack from live data**.
  Migration reported `links=26`, exactly the 26 distinct canonical live URLs in
  the links shards; a second run was idempotent. Keys = 0 (the captured live
  gathers yielded no full key matches, so no key shards existed).


