# maintenance-cli Specification

## Purpose

Provides a CLI command `main.py maintain --vacuum` to manually VACUUM registry.sqlite and all queue_state/*.sqlite databases, returning high-water pages to the filesystem after queue drain (25–35 GB in wide mode).

Traceability: 06_disk_budget.md §6.5 (D-06.2 expanded F-04), OPEN-8/12 resolved 2026-10-23.

## Requirements

### Requirement: Lock interlock

The maintain command SHALL acquire the run-lock with `holder="maintain"` before proceeding. A live pipeline lock SHALL cause refusal with nonzero exit. Stale locks (dead pid) SHALL be taken over with a loud INFO message.

#### Scenario: Maintain refuses with live pipeline lock

- **WHEN** a pipeline is running (lock exists with live pid)
- **AND** a maintain command attempts to acquire the lock
- **THEN** the maintain command raises `LockHeldError` and exits with nonzero code
- **AND** no VACUUM occurs

#### Scenario: Stale lock is ignored loudly

- **WHEN** a lock file exists with a dead pid
- **THEN** maintain logs INFO "stale lock ignored" and proceeds
- **AND** the stale lock is taken over (atomic replacement)

### Requirement: WAL-freshness

If no lock file exists but WAL files are fresh (< 120s), maintain SHALL refuse with nonzero exit (conservative threshold, reused from purge).

#### Scenario: Fresh WAL refuses

- **WHEN** a WAL file (registry or queue) has mtime < 120s old
- **THEN** maintain refuses with nonzero exit and message "WAL files are too fresh"

### Requirement: Coverage registry + queues

Maintain SHALL VACUUM both `<workspace>/registry.sqlite` AND all `<workspace>/queue_state/*.sqlite` files. Missing targets SHALL be logged as INFO skips. If NO targets exist at all, maintain SHALL refuse (fail-CLOSED ambiguity).

#### Scenario: Both registry and queues are vacuumed

- **WHEN** `maintain --vacuum` is executed on a workspace with registry and queues
- **THEN** registry.sqlite is vacuumed
- **AND** ALL queue_state/*.sqlite files are vacuumed
- **AND** per-DB log lines report bytes_before, bytes_after, bytes_reclaimed

#### Scenario: Missing targets skip loudly

- **WHEN** registry.sqlite does not exist but queue_state/*.sqlite files do
- **THEN** registry skip is logged as INFO
- **AND** queue databases are vacuumed
- **AND** exit code is 0

#### Scenario: Empty workspace refuses

- **WHEN** no registry.sqlite and no queue_state/*.sqlite exist
- **THEN** maintain refuses with nonzero exit and message "no vacuum targets"

### Requirement: Non-destructive integrity

VACUUM is non-destructive by nature, but maintain SHALL verify post-VACUUM that:
- `user_version` is preserved (== SCHEMA_VERSION)
- Row counts are preserved across all tables
- The database can be reopened and queried

#### Scenario: Data integrity preserved after vacuum

- **WHEN** `maintain --vacuum` completes
- **THEN** `user_version` is unchanged
- **AND** all table row counts are unchanged
- **AND** the database can be reopened and queried normally

### Requirement: Loud skips

Missing targets SHALL be logged as INFO with a counter. The summary SHALL report the number of skipped targets.

#### Scenario: Skip counter in summary

- **WHEN** some targets are missing
- **THEN** the summary includes `skipped=N`
- **AND** each missing target is logged as INFO

### Requirement: Masked summary

The summary output SHALL be passed through `redact_api_keys_in_text` (structural pin). The summary SHALL contain only file names and byte counts, never row content.

#### Scenario: Summary contains no secret values

- **WHEN** `maintain --vacuum` completes
- **THEN** the output contains only file names and numeric sizes
- **AND** no row content (URLs, key values) appears in the output

### Requirement: VACUUM order

For each database: `PRAGMA wal_checkpoint(TRUNCATE)` SHALL be executed BEFORE `VACUUM`. Checkpoint before VACUUM maximises page return in WAL mode (flushes WAL pages into the main DB file, then VACUUM compacts the complete file).

#### Scenario: Checkpoint before VACUUM

- **WHEN** a database is vacuumed
- **THEN** `PRAGMA wal_checkpoint(TRUNCATE)` is executed first
- **AND** `VACUUM` is executed second

### Requirement: N-10 sidecars preserved

VACUUM + checkpoint(TRUNCATE) leaves -wal/-shm files alive. This is normal for a subsequently restarted WAL-mode database. Maintain SHALL NOT delete -wal/-shm sidecars.

#### Scenario: Sidecars not deleted

- **WHEN** `maintain --vacuum` completes
- **THEN** -wal/-shm files may exist (normal WAL-mode behavior)
- **AND** the database is valid and can be reopened

### Requirement: Parser compatibility

The maintain subparser SHALL be added to main.py with `dest="command"`. All existing CLI forms (`--validate`, `--create-config`, run path, purge, restore) SHALL remain byte-identical. Without `--vacuum`, maintain SHALL print an error and exit 1.

#### Scenario: Existing forms unchanged

- **WHEN** `main.py --validate` is executed
- **THEN** it behaves identically to before the maintain subparser was added

#### Scenario: Maintain requires --vacuum

- **WHEN** `main.py maintain` is executed without `--vacuum`
- **THEN** maintain prints "Error: maintain requires --vacuum" and exits 1

## Scenarios

### Scenario: Dry-run mode

- **WHEN** `maintain --vacuum --dry-run` is executed
- **THEN** targets are enumerated with current sizes
- **AND** no VACUUM is performed
- **AND** exit code is 0

### Scenario: Maintain works without credentials

- **WHEN** the config file has no `github_credentials` section
- **AND** `maintain --vacuum` is executed
- **THEN** maintain proceeds normally (YAML-only parse, no credential validation)
