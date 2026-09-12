# Delta Spec: link-registry

## Purpose

Provides a persistent, global, cross-provider ledger of every GitHub link the harvester has discovered and gathered, together with per-provider coverage accounting and a run journal. It gives the pipeline durable memory across restarts and provider-set changes without altering current behavior (write-only), forming the trusted data foundation for all later skip/prioritization features.

## ADDED Requirements

### Requirement: Persistent global registry storage

The system SHALL maintain a single SQLite database file at `<workspace>/registry.sqlite` (configurable path), stored **outside** any `providers/<folder>/` directory, so that its contents survive restarts and changes of the enabled provider set. The database SHALL operate in WAL journal mode and SHALL be created automatically with the current schema version on first use.

#### Scenario: Registry persists across restarts

- **WHEN** a run records links into the registry and the process exits gracefully, and a new run starts against the same workspace
- **THEN** all previously recorded rows are present in the database before any new writes occur

#### Scenario: Registry is shared across provider-set changes

- **WHEN** run A executes with provider `openai` enabled and records links, and run B executes with only provider `anthropic` enabled against the same workspace
- **THEN** run B observes the same registry file and the rows written during run A are intact and attributable via their coverage/provider columns

#### Scenario: Schema bootstrap on empty workspace

- **WHEN** the registry is enabled and no database file exists yet
- **THEN** the system creates the file with all tables (`links`, `link_coverage`, `keys`, `runs`) and a recorded schema version, without failing the run

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

The system SHALL record link discovery at the search stage (upsert with `first_seen_ts` on first insert, `last_seen_ts` refresh on re-observation, plus `transport` and `query_origin`) and gather outcome at the acquisition stage (`gathered_ts` and `visit_status ∈ {discovered, gathered_ok, failed}`). Links seen but never successfully gathered SHALL remain distinguishable from gathered ones; failed gathers SHALL be marked so later phases can prioritize retry.

#### Scenario: First discovery inserts

- **WHEN** a search result URL not present in the registry is processed
- **THEN** a new `links` row is inserted with `visit_status='discovered'`, `first_seen_ts` and `last_seen_ts` set to the observation time, and `gathered_ts` NULL

#### Scenario: Re-discovery updates only recency fields

- **WHEN** a URL already present in the registry is observed again in a later run
- **THEN** `last_seen_ts` advances, `first_seen_ts` stays unchanged, and no duplicate row is created

#### Scenario: Gather outcome is recorded

- **WHEN** the acquisition stage completes fetching a registered link, successfully or not
- **THEN** the row's `visit_status` becomes `gathered_ok` with `gathered_ts` set on success, or `failed` on error, and a previously successful `gathered_ok` status is never downgraded to `discovered` by later re-observations

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

### Requirement: Fail-open degradation

Any error in registry operations (open, write, flush, migrate) SHALL NOT interrupt harvesting: the system SHALL log a warning, continue operating as if the registry were disabled for the remainder of the run, and mark the run degraded. No registry failure SHALL ever raise into stage worker loops.

#### Scenario: Corrupt database does not stop the pipeline

- **WHEN** the registry file is corrupt or unreadable at startup and `registry.enabled=true`
- **THEN** the harvester logs a warning, completes the run normally, and produces the same shards as with the registry disabled

#### Scenario: Mid-run write failure degrades silently

- **WHEN** a batch upsert fails mid-run (e.g., disk full)
- **THEN** subsequent registry writes are suppressed for this run, harvesting continues unaffected, and the run is marked degraded

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
