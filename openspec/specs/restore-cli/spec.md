# restore-cli Specification

## Purpose

Provides a CLI command to restore registry tables from NDJSON dumps created by `purge --backup-to`. Minimum for registry: all tables that purge can back up (links, link_coverage, file_meta, repos, wire_coverage, keys, key_sources). Shards, queues, and telemetry are NOT restored by this command — manual untar/byte-copy (documented in README).

Traceability: 05_purge_cli.md §5.4 (restore canon), slice 4.3 wave 4.

## Requirements

### Requirement: Registry table restoration

The restore command SHALL restore registry tables from NDJSON dumps in the `--from` directory. Each `.ndjson` file corresponds to one table (e.g., `links.ndjson` → `links` table). Records are inserted by column names from JSON keys (robust to column order). If `registry.sqlite` does not exist, it SHALL be created via the standard bootstrap (DDL from `storage/registry.py`, `user_version=8`).

#### Scenario: Full round-trip (purge backup → restore → counters match)

- **WHEN** `purge links keys --backup-to DIR` creates NDJSON dumps
- **AND** tables are emptied (by purge or manually)
- **AND** `restore --from DIR --yes` is executed
- **THEN** each restored table has the same row count as before purge
- **AND** specific column values are preserved (e.g., `links.file_commit_date`, `link_coverage.patterns_hash`, `wire_coverage.owner_provider`)

#### Scenario: Restore by column names (robust to order)

- **WHEN** an NDJSON file has keys in a different order than the table columns
- **THEN** records are inserted correctly by matching JSON keys to column names
- **AND** extra keys not in the table schema are silently ignored

### Requirement: Fail-CLOSED interlocks

The restore command SHALL enforce fail-CLOSED interlocks before any insertion:

1. **Lock symmetry (F-10)**: restore SHALL acquire the run-lock with `holder="restore"` before proceeding. A live pipeline/purge lock SHALL cause refusal with nonzero exit.
2. **WAL freshness fallback**: if WAL files are fresh (< 120s), restore SHALL refuse (conservative threshold, reuse `tools/purge.py::WAL_FRESHNESS_THRESHOLD_S`).
3. **Non-empty target**: if any table PRESENT IN THE BACKUP has rows, restore SHALL refuse BEFORE any insertion (restore is NOT a merge tool; merge is ambiguous). Tables absent from the backup are left untouched, so a single-profile purge can be undone without emptying unrelated tables.
4. **Missing --from**: if `--from` directory does not exist or is not readable, restore SHALL refuse.
5. **Corrupted NDJSON**: if any NDJSON file has invalid JSON lines, restore SHALL refuse BEFORE any insertion of that table (no partial insert).

#### Scenario: Restore refuses with live lock

- **WHEN** a pipeline or purge is running (lock exists with live pid)
- **AND** restore attempts to acquire the lock
- **THEN** restore raises `LockHeldError` and exits with nonzero code
- **AND** no insertion occurs

#### Scenario: Restore refuses non-empty table

- **WHEN** a table present in the backup (e.g., `links`) has rows
- **THEN** restore refuses with nonzero exit and message "non-empty"
- **AND** no insertion occurs in any table

#### Scenario: Single-profile undo with unrelated non-empty tables

- **WHEN** a backup containing only the links-family dumps is restored into a registry whose `keys`/`key_sources` tables still hold rows
- **THEN** restore succeeds, the backed-up tables are repopulated, and the tables absent from the backup are left untouched

#### Scenario: Restore refuses corrupted NDJSON

- **WHEN** an NDJSON file has a line that is not valid JSON
- **THEN** restore refuses with nonzero exit and message naming the table
- **AND** no partial insertion occurs (the table remains empty)

### Requirement: Partial backup support

If the `--from` directory contains only a subset of tables (e.g., only `links.ndjson`), restore SHALL restore the present tables and skip the absent ones with a loud INFO message and counter (idempotency, mirror T8 purge semantics).

#### Scenario: Partial backup restores present, skips absent

- **WHEN** `--from` contains only `links.ndjson`
- **THEN** `links` table is restored
- **AND** other tables (`keys`, `repos`, etc.) are skipped with "skipped" message
- **AND** exit code is 0

### Requirement: Dry-run mode

The `--dry-run` flag SHALL enumerate what would be restored (tables, row counts) but make ZERO changes. Dry-run SHALL exit 0.

#### Scenario: Dry-run enumerates without modifying

- **WHEN** `restore --from DIR --dry-run` is executed
- **THEN** the output lists the number of rows that would be restored from each table
- **AND** no insertion occurs
- **AND** exit code is 0

### Requirement: Secret masking

Restore SHALL NEVER print secret values (API keys, tokens) to stdout or stderr. All output SHALL be passed through `redact_api_keys_in_text` (structural pin, mirror purge C9 discipline).

#### Scenario: Secret plant values absent from output

- **WHEN** an NDJSON file contains a field with value `SECRETVALUE_PLAINTEXT_1`
- **THEN** this value does NOT appear in stdout or stderr (combined output)

### Requirement: Parser compatibility

The restore subparser SHALL be added to main.py with `dest="command"`, `default=None`. All existing CLI forms (`--validate`, `--create-config`, `purge`, run path) SHALL remain byte-identical.

#### Scenario: Existing forms unchanged

- **WHEN** `main.py --validate` is executed
- **THEN** it behaves identically to before the restore subparser was added
- **AND** `main.py restore --from DIR --yes` works as a new form

### Requirement: N-11 documentation

The restore command SHALL document that registry tables have no `dedup_id` column (N-11 caveat). Queue restore is manual byte-copy (not supported by this command). The docstring, README, and spec SHALL note this.

#### Scenario: N-11 registry tables have no dedup_id

- **WHEN** restore is executed
- **THEN** the docstring and README note that registry tables do not have `dedup_id`
- **AND** queue restore is documented as manual byte-copy

## Scenarios

### Scenario: Restore requires --yes or --dry-run

- **WHEN** `restore --from DIR` is executed without `--yes` or `--dry-run`
- **THEN** restore refuses with nonzero exit and message "requires --yes or --dry-run"

### Scenario: Restore works without credentials

- **WHEN** the config file has no `github_credentials` section
- **AND** `restore --from DIR --yes` is executed
- **THEN** restore proceeds normally (YAML-only parse, no credential validation)

### Scenario: Shards/queues/telemetry restore = manual

- **WHEN** restore is executed
- **THEN** the README documents that shards/queues/telemetry restore is manual (untar tar.gz, byte-copy .sqlite)
- **AND** the restore command does NOT attempt to restore these areas
