# Design: add-link-registry

## Context

See proposal.md — Why. Current state constraints that shape this design:

- Dedup today is in-memory only: bounded `processed` sets (≤100k) per stage (`stage/base.py:141-145`); everything is re-fetched after restart.
- Link recording points already exist: `output.add_links` fires in SearchStage (`stage/definition.py:127`, all search-result URLs) and AcquisitionStage (`stage/definition.py:363`). Critically, the search-stage call happens **before** any gather attempt, so "seen" ≠ "gathered".
- Storage conventions: NDJSON shards + `.index.json` sidecars written by a buffered writer thread (`ResultBuffer`, `storage/persistence.py:31-85`), atomic writes via temp+fsync+`os.replace` (`storage/atomic.py`). The registry must feel native to these conventions.
- Concurrency: OS threads (search 1, gather 8, check 4, inspect 2 by default) share the pipeline; any DB access must tolerate ~10 concurrent producers.
- Runtime dependencies are minimal by project policy (`requirements.txt`: PyYAML, requests). sqlite3 is stdlib.
- `recover_tasks()` (`storage/persistence.py:446-484`) rebuilds tasks from shards and MUST remain untouched — the registry complements, never replaces, the shard audit log.

## Goals / Non-Goals

**Goals:**
- Durable global memory of links/coverage/runs surviving restarts and provider-set switches.
- Zero behavior change: strictly write-only integration behind `registry.enabled` (default false).
- Canonical URL identity fixed by a written spec before code (Phase-0 deliverable).
- Fail-open everywhere: registry problems can never break harvesting.
- Establish the pytest test infrastructure later phases build on.

**Non-Goals:**
- No reads for decisions (skip/stop/check logic lives in later changes).
- No population of `keys` beyond migration imports (key-ledger change owns writes).
- No date extraction (columns created nullable; filled by add-date-extraction).
- No cross-machine/shared registry (one workspace = one registry; deliberate v1 limitation).
- No changes to RefineEngine, credential mechanics, or shard formats.

## Decisions

**D1 — SQLite (WAL) single global file vs alternatives.**
Chosen: `<workspace>/registry.sqlite`, WAL journal mode, `PRAGMA user_version` for schema evolution. Alternatives rejected: (a) per-provider NDJSON sidecars — fragments cross-provider identity, which is the whole point; (b) LMDB/sqlite3-alternate deps — violates minimal-dependency policy; (c) extending QueueManager JSON persistence — no indexed queries, no concurrent writers. WAL gives one writer + many readers, which matches both this change (writer only) and the next ones (readers in stage threads).

**D2 — Single writer thread with batched upserts.**
Replicates the proven `ResultBuffer` pattern: producers `put()` lightweight tuples onto a bounded queue; one daemon thread drains in batches (`batch_size`, default 50) every `flush_interval` (default 5 s) using `executemany` inside one transaction. Alternative rejected: per-thread connections with `busy_timeout` — multiplies lock contention with 8+ gather threads and complicates shutdown ordering. Bounded queue with drop-and-count-on-full mirrors `put_task` backpressure semantics; drops increment a `registry_dropped` counter and mark the run degraded (data loss here is tolerable pre-skip-phases, silent loss is not).

**D3 — URL canonicalization canon (Phase-0 artifact).**
`canonical_url()`: lowercase scheme+host; strip `#…` fragment and `?…` query; strip trailing slash; preserve owner/repo/path/ref letter case exactly (GitHub paths are case-sensitive; refs usually are too). `url_hash = sha256(canonical)` hex digest as TEXT PK. Branch-agnostic normalization (collapsing the ref segment) was considered and **rejected**: a false "known" is far more dangerous than a false "novel" — it would silently skip unseen files. Branch rename therefore yields a new identity (re-gather once; safe direction). The canon is published as `docs/specs/url_canonicalization.md` with a golden test-vector table; every later phase references it.

**D4 — Conservative migration.**
Legacy `shards/links/*` cannot distinguish seen-only from gathered (both went through `add_links`), so migrated rows get `visit_status='discovered'`, `gathered_ts=NULL`. Cost: the first skip-enabled run may re-gather known links once (skipping requires `gathered_ok`). Benefit: zero risk of falsely-skipped files. Migration upserts with "never regress" semantics: existing fresher rows win over older imported values.

**D5 — Coverage rows even with zero findings.**
`link_coverage` is written on every successful gather regardless of extraction outcome. Without this, "provider switch found nothing" is indistinguishable from "never researched under this provider" — exactly the ambiguity that would cause false skips after config changes. `patterns_hash = sha256` over the provider's effective sorted pattern strings (key/address/endpoint/model), computed deterministically from the patterns actually applied to the gather and cached once per distinct pattern set (the gather stage keeps the per-pattern-set cache, so each set is hashed a single time per process).

**D6 — pytest introduced as dev dependency.**
`requirements-dev.txt` with `pytest` only; tests live in top-level `tests/`. Resolves plan open question #4: phases 2–4 (parsers, skip rules, window math) are untestable-by-inspection; script/fixture checks alone would not give the RED→GREEN gate the tdd-flow schema demands. Runtime requirements stay untouched.

**D7 — Fail-open wrapper + degraded marker.**
All registry entry points go through a thin guard: `try: … except Exception: log warning once-per-kind; disable further writes this run; set runs.degraded=1`. The degraded flag later feeds the early-stop trust gate (a degraded run must not trust its own "known" counts). Alternative rejected: crash-fast — unacceptable for a long-running harvester where the registry is auxiliary.

**DDL (adjusted from plan: explicit first_seen/last_seen):**

```sql
CREATE TABLE links(
  url_hash TEXT PRIMARY KEY, url TEXT NOT NULL,
  owner TEXT, repo TEXT, path TEXT,
  first_seen_ts REAL, last_seen_ts REAL, gathered_ts REAL,
  visit_status TEXT NOT NULL DEFAULT 'discovered',   -- discovered|gathered_ok|failed
  transport TEXT,                                    -- api|web
  query_origin TEXT,
  provider TEXT,                                     -- discovering provider (migration + search hook)
  repo_pushed_at REAL, repo_size_kb INTEGER, file_commit_date REAL,  -- reserved, NULL here
  priority REAL                                      -- reserved, NULL here
);
CREATE TABLE link_coverage(
  url_hash TEXT NOT NULL, provider TEXT NOT NULL, patterns_hash TEXT NOT NULL,
  gathered_ts REAL NOT NULL,
  PRIMARY KEY(url_hash, provider, patterns_hash)
);
CREATE TABLE keys(
  key_hash TEXT PRIMARY KEY, provider TEXT, key_ref_masked TEXT,
  address TEXT, endpoint TEXT, status TEXT,
  first_seen_ts REAL, last_recheck_ts REAL, source_url_hash TEXT
);
CREATE TABLE runs(
  run_id TEXT PRIMARY KEY, started_at REAL, finished_at REAL,
  config_digest TEXT, degraded INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_links_repo ON links(owner, repo);
CREATE INDEX idx_links_last_seen ON links(last_seen_ts);
CREATE INDEX idx_links_provider ON links(provider);
```

Config section (`config/schemas.py` + loader): `registry: {enabled: bool=false, path: str="<workspace>/registry.sqlite", batch_size: int=50, flush_interval: int=5}`. Feature-flag matrix documented now, consumed later: `registry.enabled` (this change), `skip_known: off|shadow|on` (add-gather-skip), `early_stop: off|shadow|on` (add-search-early-stop), `check_skip: off|on`, `recheck_cron: off|on` (add-key-ledger).

## Risks / Trade-offs

- [Lock contention with 8+ producer threads] → single writer thread; producers only enqueue (non-blocking put with drop-counter).
- [Disk growth] → ≈200 B/row; 1M links ≈ 200 MB; acceptable; `VACUUM` documented as ops task, not automated.
- [Schema drift between phases] → `PRAGMA user_version` + additive-only migrations rule; later changes may ADD columns/tables, never rewrite semantics of existing ones.
- [Silent data loss on full queue] → drop counter + degraded marker; tolerable while write-only, must be zero-tolerant before skip phases read the registry (their trust gates check it).
- [Migration mis-dating legacy links] → conservative `gathered_ts=NULL`; worst case one extra re-gather wave.
- [Canonicalization bugs → false identities] → golden-vector tests in CI; canon frozen in docs; any future change to `canonical_url()` requires a spec update + full re-migration path.
- [WAL files on network filesystems] → documented limitation: workspace must be on a local FS (SQLite WAL requirement).

## Migration Plan

1. Ship with `registry.enabled=false` default — deploy is a no-op.
2. Enable on a staging workspace; verify row counts ≈ links-shard record counts; verify identical shards A/B.
3. Run `tools/registry_migrate.py --dry-run`, then for real, once per existing workspace.
4. Rollback: set `enabled=false`; deleting `registry.sqlite*` is harmless at this phase.

## Open Questions

- None blocking. TTL defaults and trust thresholds belong to add-gather-skip / add-search-early-stop / add-key-ledger designs (they do not affect this schema: all needed columns exist).
