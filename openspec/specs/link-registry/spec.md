# link-registry Specification

## Purpose

Provides a persistent, global, cross-provider ledger of every GitHub link the harvester has discovered and gathered, together with per-provider coverage accounting and a run journal. It gives the pipeline durable memory across restarts and provider-set changes without altering current behavior (write-only), forming the trusted data foundation for all later skip/prioritization features.

## Requirements

### Requirement: Persistent global registry storage

The system SHALL maintain a single SQLite database file at `<workspace>/registry.sqlite` (configurable path), stored **outside** any `providers/<folder>/` directory, so that its contents survive restarts and changes of the enabled provider set. The database SHALL operate in WAL journal mode and SHALL be created automatically with the current schema version on first use. Schema evolution SHALL be **additive only**: a database created at an earlier version SHALL be migrated in place on open by an idempotent guard, migrating twice SHALL be equivalent to migrating once, and all pre-existing rows SHALL survive unchanged. Because migrations only add, a build that predates the current schema version SHALL still be able to read a database at the newer version, so rolling back the binary SHALL NOT require rolling back the data.

#### Scenario: Registry persists across restarts

- **WHEN** a run records links into the registry and the process exits gracefully, and a new run starts against the same workspace
- **THEN** all previously recorded rows are present in the database before any new writes occur

#### Scenario: Registry is shared across provider-set changes

- **WHEN** run A executes with provider `openai` enabled and records links, and run B executes with only provider `anthropic` enabled against the same workspace
- **THEN** run B observes the same registry file and the rows written during run A are intact and attributable via their coverage/provider columns

#### Scenario: Schema bootstrap on empty workspace

- **WHEN** the registry is enabled and no database file exists yet
- **THEN** the system creates the file with all tables (`links`, `link_coverage`, `keys`, `runs`, `repos`, `meta`, `file_meta`) and a recorded schema version, without failing the run

#### Scenario: Database at an earlier schema version migrates in place

- **WHEN** the registry opens a database whose recorded schema version predates the current one
- **THEN** the missing columns and tables are added, the recorded version becomes current, and every pre-existing row keeps its prior values

#### Scenario: Schema migration is idempotent on reopen

- **WHEN** an already-migrated database is opened again, or the migration guard runs twice in one process
- **THEN** no error is raised, no column is duplicated and the recorded schema version is unchanged

#### Scenario: Older build reads a newer database

- **WHEN** a build that does not know the newest columns opens a database at the newer schema version
- **THEN** reads and writes of the columns it does know succeed, and the unknown columns retain their values

### Requirement: URL canonicalization and link identity

The system SHALL derive link identity from a canonical form of the GitHub blob URL: lowercase scheme and host; `owner/repo/path` with the path's original letter case preserved (GitHub paths are case-sensitive); the branch/ref segment preserved as-is; line fragments (`#L…`) and query strings discarded; trailing slash normalized. The primary key SHALL be `url_hash = sha256(canonical_url)`. The same file reached through the API transport and the web transport SHALL produce the same identity. Branch renames intentionally produce a "new" link (safe direction); branch-agnostic normalization is explicitly rejected.

#### Scenario: Line fragment does not change identity

- **WHEN** the URLs `https://github.com/o/r/blob/main/a.py#L10` and `https://github.com/o/r/blob/main/a.py#L42-L50` are recorded
- **THEN** both map to the same `url_hash` and update the same row

#### Scenario: Path case is significant

- **WHEN** the URLs `https://github.com/o/r/blob/main/Config.env` and `https://github.com/o/r/blob/main/config.env` are recorded
- **THEN** they map to different `url_hash` values and occupy separate rows

#### Scenario: Transports converge on one identity

- **WHEN** the same blob URL is discovered once via the API transport and once via the web transport
- **THEN** a single row exists whose `transport` column reflects the most recent observation while `first_seen_ts` remains from the earliest

### Requirement: Write-only integration

In this change the pipeline SHALL NOT read the registry to influence any decision (task creation, skipping, stopping, checking). Runs with `registry.enabled=true` and `registry.enabled=false` SHALL produce identical NDJSON shard outputs for identical inputs.

#### Scenario: Flag off behaves exactly as today

- **WHEN** the harvester runs with `registry.enabled=false`
- **THEN** no registry file is created or opened and pipeline behavior is byte-for-byte equivalent to the pre-change implementation

#### Scenario: Flag on does not alter outputs

- **WHEN** two equivalent runs process the same search results, one with the registry enabled and one disabled
- **THEN** the produced shards (`links`, `material`, `valid`, …) contain the same records in both runs

### Requirement: Link lifecycle recording

The system SHALL record link discovery at the search stage (upsert with `first_seen_ts` on first insert, `last_seen_ts` refresh on re-observation, plus `transport` and `query_origin`) and gather outcome at the acquisition stage (`gathered_ts` and `visit_status ∈ {discovered, gathered_ok, failed}`). Links seen but never successfully gathered SHALL remain distinguishable from gathered ones; failed gathers SHALL be marked so later phases can prioritize retry. The registry SHALL additionally store, per link, the conditional-revalidation token returned by the transport that fetched it together with an identification of that transport, so that a later run can ask whether the content changed without downloading it again. A token SHALL only ever be replayed to the transport that produced it.

#### Scenario: First discovery inserts

- **WHEN** a search result URL not present in the registry is processed
- **THEN** a new `links` row is inserted with `visit_status='discovered'`, `first_seen_ts` and `last_seen_ts` set to the observation time, and `gathered_ts` NULL

#### Scenario: Re-discovery updates only recency fields

- **WHEN** a URL already present in the registry is observed again in a later run
- **THEN** `last_seen_ts` advances, `first_seen_ts` stays unchanged, and no duplicate row is created

#### Scenario: Gather outcome is recorded

- **WHEN** the acquisition stage completes fetching a registered link, successfully or not
- **THEN** the row's `visit_status` becomes `gathered_ok` with `gathered_ts` set on success, or `failed` on error, and a previously successful `gathered_ok` status is never downgraded to `discovered` by later re-observations

#### Scenario: Revalidation token is stored with its transport

- **WHEN** a link is gathered successfully and the response carries a revalidation token
- **THEN** the token is stored on that link's row together with the transport that produced it, replacing any token previously stored from a different transport

#### Scenario: Absent token leaves the row usable

- **WHEN** a link is gathered successfully but the response carries no revalidation token
- **THEN** the row records the gather outcome normally, stores no token, and later runs fetch it unconditionally rather than failing
### Requirement: Provider coverage accounting

After each successful gather the system SHALL write a `link_coverage(url_hash, provider, patterns_hash, gathered_ts)` row identifying which provider and which extraction-pattern set was applied to that link. A coverage row SHALL be written even when the gather extracted zero keys, so that "researched under this provider, nothing found" is distinguishable from "never researched under this provider".

#### Scenario: Coverage recorded without findings

- **WHEN** a link is successfully gathered for provider `openai` and no key matches are extracted
- **THEN** a coverage row `(url_hash, 'openai', <patterns_hash>, ts)` exists for that link

#### Scenario: Pattern-set change is visible

- **WHEN** the same link is gathered again for the same provider after its `key_pattern` configuration changed (different `patterns_hash`)
- **THEN** a second coverage row with the new `patterns_hash` is added while the old row is preserved

### Requirement: Run journaling

The system SHALL journal every run in a `runs` table: `run_id`, `started_at`, `finished_at` (set on graceful completion), and a `config_digest` capturing the effective configuration relevant to interpretation of results (enabled providers, `use_api` flags, per-provider pattern hashes, registry flag states). A run during which any registry error occurred SHALL be marked degraded.

#### Scenario: Run lifecycle is journaled

- **WHEN** a run starts and later finishes gracefully
- **THEN** a `runs` row exists with non-null `started_at` and `finished_at` and a parseable `config_digest`

#### Scenario: Degraded run is marked

- **WHEN** at least one registry write operation failed during a run
- **THEN** that run's journal entry carries a degraded marker distinguishing it from clean runs

### Requirement: Fail-closed liveness (Д14, 2026-10-22)

When the registry is enabled (`registry.enabled=true`) and its SQLite store is dead (open failure or database-class error), the program SHALL terminate loudly with a nonzero exit code and SHALL NOT continue operating in a degraded state. Transient busy/locked errors (`database is locked`, `database is busy`, `[SQLITE_BUSY]`, `[SQLITE_LOCKED]`) are NOT fatal and keep the existing degrade-and-retry behaviour. When the registry is disabled, the module is a complete no-op (no exceptions raised).

Auxiliary read-side fail-open (gather_skip, key_ledger, priority) is unchanged in this slice — the central fatal path crashes the run; read-side consumers continue to degrade gracefully.

#### Scenario: Corrupt database terminates the run

- **WHEN** the registry file is corrupt or unreadable at startup and `registry.enabled=true`
- **THEN** the system raises `RegistryUnavailableError`, logs a CRITICAL message naming the database path and cause, and exits with a nonzero code

#### Scenario: Mid-run database-class error aborts via liveness poll

- **WHEN** a database-class error (e.g., `database disk image is malformed`) occurs mid-run
- **THEN** the periodic liveness poll detects the fatal state via `check_alive()` and triggers graceful shutdown with a CRITICAL log and nonzero exit

#### Scenario: Transient busy/locked does not terminate

- **WHEN** a transient `sqlite3.OperationalError` with message matching `database is locked` or `database is busy` occurs
- **THEN** the existing degrade-and-retry behaviour applies; `check_alive()` does NOT raise

#### Scenario: Disabled registry is a no-op

- **WHEN** `registry.enabled=false`
- **THEN** no registry file is opened, no exceptions are raised, and pipeline behaviour is identical to the pre-registry implementation

### Requirement: Durability and shutdown flush

The registry writer SHALL batch writes through an internal queue flushed periodically and SHALL drain the queue completely during the pipeline's graceful stop sequence, before process exit. Committed batches SHALL survive abrupt termination (SIGKILL) via WAL recovery without structural corruption.

#### Scenario: Graceful stop drains pending writes

- **WHEN** the pipeline stops gracefully while registry batches are still queued
- **THEN** all queued rows are present in the database after `stop()` returns

#### Scenario: Crash leaves database consistent

- **WHEN** the process is killed abruptly (SIGKILL) during a run with the registry enabled
- **THEN** on next open the database passes integrity check and contains at least all batches committed before the kill

### Requirement: One-time idempotent migration from shard data

The system SHALL provide a migration tool that populates the registry from existing workspace data: `shards/links/*` NDJSON → `links` rows (`seen_ts` derived from shard index timestamps, provider from folder name, `gathered_ts=NULL` conservatively because historical shards cannot distinguish seen-only from gathered); `shards/material|valid|invalid|no_quota|wait_check` and `summary.json` → `keys` rows with their statuses. Running the migration repeatedly SHALL NOT create duplicates or modify newer in-place data (older migrated values never overwrite fresher registry state).

#### Scenario: Migration imports legacy links conservatively

- **WHEN** the migration tool runs over a workspace containing `shards/links/*.ndjson`
- **THEN** every distinct canonical URL appears exactly once in `links` with `gathered_ts` NULL and `visit_status='discovered'`

#### Scenario: Migration is idempotent

- **WHEN** the migration tool is executed twice over the same unchanged workspace
- **THEN** row counts in `links` and `keys` are identical after the first and second execution

#### Scenario: Migration never regresses live data

- **WHEN** the registry already contains a link with `visit_status='gathered_ok'` and the migration processes an older shard mentioning the same URL
- **THEN** the existing row keeps its `gathered_ok` status and `gathered_ts`

### Requirement: Wire coverage accounting (schema v7)

The system SHALL maintain a `wire_coverage` table (schema version 7, additive migration from v6) recording which pages of each wire query have been fetched. Identity is `(wire_fp, patterns_hash)` where `wire_fp = sha256("<api|web>|<wire_query>")` (the same fingerprint used by the refine governor). The table SHALL track a gap-safe bitmap of completed pages and a contiguity watermark (`pages_done`) so that later probes can decide whether a bucket is fully covered without being fooled by holes or out-of-order completions.

#### Scenario: Schema contract on fresh bootstrap

- **WHEN** the registry is enabled and bootstraps a new database
- **THEN** the `wire_coverage` table exists with columns `(wire_fp, patterns_hash, total_count, pages_bitmap, pages_done, pages_needed, results, first_ts, last_ts)`, primary key `(wire_fp, patterns_hash)`, index `idx_wire_coverage_ts`, and `PRAGMA user_version = 7`

#### Scenario: Page-1 seam writes totals and needed

- **WHEN** the search stage processes a first page with `total=950`, `per_page=100`, and 100 results
- **THEN** a `wire_coverage` row is written with `total_count=950`, `pages_needed=10`, `pages_bitmap` having bit 0 set, `pages_done=1`, and `results=100`

#### Scenario: Bitmap OR and contiguity watermark

- **WHEN** pages are recorded out of order (page 1, then page 5)
- **THEN** `pages_bitmap` has bits {0, 4} set and `pages_done=1` (only page 1 is contiguous from bit 0); after pages 2, 3, 4 are recorded, `pages_done=5`

#### Scenario: Duplicate page idempotence

- **WHEN** page 3 is recorded twice
- **THEN** the watermark does not advance on the second recording and `results` is incremented only once (the CASE-guard dedup ensures retries do not double-count)

#### Scenario: Out-of-order safety

- **WHEN** pages 1-4 and 6 are recorded but page 5 is missing
- **THEN** `pages_done=4 < pages_needed=6` (the hole prevents the watermark from advancing past it)

#### Scenario: Bit-64 clamp insurance

- **WHEN** a page number exceeds 63
- **THEN** the bit is clamped to bit 62 (SQLite signed 64-bit safe) without crashing; the watermark behavior is sane (bit 0 not set → `pages_done=0`)

#### Scenario: Disabled no-op

- **WHEN** the registry is disabled (`enabled=false`)
- **THEN** `record_wire_page` does nothing, raises nothing, and no database file is created

### Requirement: Wire-coverage probe (slice 3a.2, F-05)

The registry SHALL expose a `probe_wire_covered` method that accepts a list of wire queries, a `use_api` flag, a `patterns_hash_value`, and a `wire_ttl_hours` value, and returns the set of query strings that are fully covered. A query is covered iff: (a) `pages_done >= pages_needed` (watermark ≥ needed; `pages_needed` NOT NULL and > 0), (b) `last_ts` is fresher than `time.time() - wire_ttl_hours * 3600`, and (c) the row's `patterns_hash` matches `patterns_hash_value`. The method SHALL use the same `fingerprint(query, use_api)` function from `search.querykey` as `record_wire_page`. IN queries SHALL be chunked to ≤ 897 wires per SELECT (C3 precedent: ≤ 900 bind params). The method SHALL be fail-open: any read exception returns an empty set and logs a WARNING (loud-once). `RegistryUnavailableError` from `check_alive()` SHALL NOT be suppressed (typed fatal, Д14). When the registry is disabled or unavailable, the method SHALL return an empty set.

#### Scenario: Probe returns covered queries

- **WHEN** the wire_coverage table has a row with `pages_done >= pages_needed`, fresh `last_ts`, and matching `patterns_hash`
- **THEN** `probe_wire_covered` includes that query in the returned set

#### Scenario: Probe excludes stale rows

- **WHEN** a wire_coverage row has `last_ts` older than `wire_ttl_hours * 3600` seconds
- **THEN** `probe_wire_covered` does NOT include that query

#### Scenario: Probe excludes mismatched patterns

- **WHEN** a wire_coverage row has a different `patterns_hash` than the caller's
- **THEN** `probe_wire_covered` does NOT include that query

#### Scenario: Probe chunks large inputs

- **WHEN** more than 897 queries are probed
- **THEN** the implementation issues multiple SELECT statements with at most 897 wire fingerprints each, and all covered queries are returned correctly

### Requirement: SQL recovery source (slice 3a.3, D-03.4, F-14, F-17)

The registry SHALL expose a `fetch_recoverable_urls` method that streams acquisition URLs for links with `visit_status IN ('discovered', 'failed')`, ordered by `last_seen_ts DESC` (freshest-first, F-14) with a deterministic tie-breaker on `url_hash`. The method SHALL use keyset pagination with bounded RAM: each fetch retrieves at most 1000 rows (configurable chunk_size). The method SHALL support a `limit` parameter (0 = unlimited) and `before_ts`/`before_hash` continuation parameters for cross-boot resumption. The selection SHALL be GLOBAL (no provider filter): `links.provider` records the first discoverer only (measured live at gate 3a.G G2, 2026-10-22 — all rows carried the single first-discovering provider while per-provider fan-out lives in `link_coverage`), so a provider-filtered SELECT starves every non-first-discovering provider; each provider iterates the same freshest-first set and per-provider suppression happens below in the skip/share stack (P26), mirroring the legacy overlapping shard lists. Fatal database errors SHALL propagate as `RegistryUnavailableError` (fail-CLOSED, Д14); transient busy/locked errors SHALL be classified by `_is_fatal_db_error`.

The pipeline SHALL support a `recovery.source` configuration key with values `shards` (default), `registry`, or `auto`. When `source=registry` or `source=auto` with an enabled+available registry, acquisition URLs SHALL be read from the registry via `fetch_recoverable_urls` instead of shard aggregation. When `source=auto` with the registry disabled, the shard path SHALL be used (neutral fallback). When the registry is enabled but dead, `RegistryUnavailableError` SHALL propagate with no shard-fallback (Д14).

A per-boot cap (`recovery.per_boot_cap`, default 0 = unlimited) SHALL limit the number of URLs taken per boot. When the cap truncates the result set, a continuation marker SHALL be written to the `meta` table under the per-provider key `recovery_cursor:{provider}`, value = composite cursor `{last_seen_ts}|{url_hash}` of the last taken row. The next boot SHALL resume with the same compound keyset condition used within a boot (`last_seen_ts < ts OR (last_seen_ts = ts AND url_hash > hash)`), so rows sharing the boundary timestamp are neither lost nor duplicated. A legacy ts-only marker SHALL be honoured with a WARNING (its strict less-than resumption can lose rows at equal-ts boundaries). When the full result set is consumed without truncation, the marker SHALL be removed.

The P26 filter stack (gather-skip decision → share claim) SHALL be preserved on top of registry-sourced URLs via the same `_recover_acquisition_tasks` code path. A loud INFO publication with token `recovered_from_registry=N` SHALL be emitted when URLs are sourced from the registry.

#### Scenario: Freshest-first ordering

- **WHEN** the registry contains links with different `last_seen_ts` values and `recovery.source=registry`
- **THEN** URLs are returned in strict `last_seen_ts DESC` order (freshest first)

#### Scenario: Cap with continuation marker

- **WHEN** `recovery.per_boot_cap=K` and the registry contains N > K recoverable URLs
- **THEN** exactly K freshest URLs are taken, a loud INFO token `[recovery_truncated]` is logged, and the `meta` table contains `recovery_cursor:{provider}={last_seen_ts}|{url_hash} of the K-th row`

#### Scenario: Continuation across boots

- **WHEN** a prior boot wrote a continuation marker and the next boot starts with the same config
- **THEN** the reader resumes below the composite cursor using the compound keyset condition (rows sharing the boundary `last_seen_ts` are continued by `url_hash` ascending), with no duplicates and no losses against the prior boot, and after the full result set is consumed the marker is removed

#### Scenario: Equal-timestamp boundary loses nothing

- **WHEN** several rows share one `last_seen_ts` and the per-boot cap cuts through the middle of that group
- **THEN** the next boot takes the remainder of the group via the composite-cursor tie-breaker — zero loss, zero duplication

#### Scenario: Fail-closed auto mode

- **WHEN** `recovery.source=auto` and the registry is enabled but the database is dead
- **THEN** `RegistryUnavailableError` propagates and no shard-fallback occurs (crash with nonzero exit)

#### Scenario: P26 stack preserved on registry source

- **WHEN** `recovery.source=registry` and gather-skip/share-claim are enabled
- **THEN** the same `_recover_acquisition_tasks` code path applies the P26 filter stack (skip decide → share claim) to registry-sourced URLs

#### Scenario: Neutral default

- **WHEN** `recovery.source=shards` (default) or the key is absent
- **THEN** the shard-aggregate path is used unchanged, all existing pins survive, and no registry reader is invoked
