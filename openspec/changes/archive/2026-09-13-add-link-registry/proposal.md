# Proposal: add-link-registry

## Why

Every restart, the harvester redoes its full pass: deduplication is in-memory only (bounded `processed` sets of 100k per stage), so all previously researched GitHub links are re-fetched and all found keys are re-verified against provider APIs. There is no persistent memory of "which links have we already researched, under which provider and pattern set". All later optimizations (date-based invalidation, gather-skip, API early-stop, key ledger) require a durable, global, cross-provider registry as their foundation. This change introduces that foundation in **write-only mode**: the pipeline records into it but never reads from it for decisions, so observable behavior does not change at all.

This change also captures the Phase-0 specification work: the URL canonicalization canon, the registry DDL, the feature-flag matrix, and the metrics dictionary become first-class documented artifacts here, because ~90% of future "falsely known / falsely novel" bugs would originate in URL normalization decisions made silently during coding.

## What Changes

- New module `storage/registry.py`: a SQLite database in WAL mode at `<workspace>/registry.sqlite` — **global**, outside `providers/<folder>/`, so it survives changes of the enabled provider set between runs.
- Tables: `links` (identity + lifecycle + nullable metadata columns reserved for later phases), `link_coverage` (per provider/patterns-set gather coverage), `keys` (created empty here; populated by the future key-ledger change), `runs` (run journal with config digest).
- Writes go through a dedicated writer thread with a bounded queue and batched upserts — replicating the existing `ResultBuffer` pattern (`storage/persistence.py:31-85`); flushed in `stop()` alongside `result_manager`.
- Write-only hooks: SearchStage upserts `links` at the existing `output.add_links` point (`stage/definition.py:127`); AcquisitionStage records `gathered_ts`/`visit_status` near `stage/definition.py:363`; run start/finish writes `runs`.
- One-time idempotent migration tool `tools/registry_migrate.py`: builds the registry from existing NDJSON shards (`shards/links/*` → links with `seen_ts` from index timestamps and provider from folder name, `gathered_ts=NULL` conservatively; `shards/material|valid|invalid|no_quota|wait_check` + `summary.json` → keys).
- New config section `registry` (`enabled`, `path`, `batch_size`, `flush_interval`) in `config/schemas.py` + `config/loader.py`, default `enabled: false`.
- Fail-open degradation: any registry error logs a warning, marks the run degraded, and the pipeline continues exactly as today.
- Introduction of a minimal pytest dev-dependency (`requirements-dev.txt`) — the project currently has no test infrastructure, while this and all following changes need real assertions.
- Documented specs: URL canonicalization canon (`docs/specs/url_canonicalization.md`), feature-flag matrix, metrics dictionary (`novel_links_per_run`, degraded-run marker; skip/early-stop/check metrics are defined but emitted by later changes).

**Non-breaking:** no reads from the registry influence any decision; NDJSON shards remain the audit log; `recover_tasks()` is untouched.

## Capabilities

### New Capabilities
- `link-registry`: persistent global ledger of researched GitHub links, per-provider gather coverage, and run journaling, written by the pipeline without influencing its behavior; includes URL canonicalization identity rules, fail-open degradation, durability guarantees, and one-time migration from existing shard data.

### Modified Capabilities
<!-- none: openspec/specs/ is empty; no existing capability requirements change -->

## Impact

- **New code:** `storage/registry.py`, `tools/registry_migrate.py`, `tests/` (new test package), `docs/specs/url_canonicalization.md`, `requirements-dev.txt`.
- **Modified code:** `manager/pipeline.py` (registry init/stop wiring), `stage/definition.py` (two write hooks), `manager/task.py` or `main.py` (run journal start/finish), `config/schemas.py`, `config/loader.py`, `config/validator.py`, `examples/config-full.yaml` (registry section), `README.md` (usage section), `__init__.py` (remove stale import of the no-longer-exported `TaskRecoveryStrategy` so the repo package can be imported by the new test suite).
- **Data:** new file `<workspace>/registry.sqlite` (+ `-wal`/`-shm`); disk growth ≈ 200 B/link row (1M links ≈ 200 MB).
- **Dependencies:** none new at runtime (sqlite3 is stdlib); pytest added as dev-only dependency.
- **Follow-up changes depending on this one:** add-date-extraction (fills nullable metadata columns), add-gather-skip (first reader), add-search-early-stop, add-key-ledger (populates `keys`), add-target-prioritization (reads dates + key statuses).
