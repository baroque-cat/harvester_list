# purge-cli Specification

## Purpose

Provides a CLI command to purge workspace data by profile with fail-CLOSED interlocks, optional backup, and atomic receipt generation. The purge command supports six profiles (all, links, keys, queues, telemetry, shards) and enforces mutual exclusion with the pipeline via the run-lock mechanism (symmetry, slice 4.1).

Traceability: 05_purge_cli.md §5.1–5.6, slice 4.2 wave 4.

## Requirements

### Requirement: Profile-based deletion

The purge command SHALL support six profiles: `all`, `links`, `keys`, `queues`, `telemetry`, `shards`. Each profile SHALL delete a specific set of data as defined in 05 §5.2. The `all` profile SHALL delete in order: queues → shards/snapshots → telemetry → registry (last) to ensure a crashed purge leaves the system in a recoverable state.

#### Scenario: Links profile deletes target tables

- **WHEN** `purge links` is executed
- **THEN** tables `links`, `link_coverage`, `file_meta`, `repos`, `wire_coverage` are emptied (DELETE FROM)
- **AND** other tables (`keys`, `key_sources`, `runs`, `meta`) remain intact
- **AND** the registry.sqlite file itself is NOT deleted

#### Scenario: Queues profile deletes queue files

- **WHEN** `purge queues` is executed
- **THEN** all files in `<workspace>/queue_state/` are deleted
- **AND** -wal/-shm sidecars are deleted along with .sqlite files (N-10)

#### Scenario: Shards profile deletes shards and snapshots

- **WHEN** `purge shards` is executed
- **THEN** all files in `<workspace>/providers/*/shards/**` and `<workspace>/providers/*/snapshots/*` are deleted
- **AND** the registry remains intact

#### Scenario: All profile deletes in order

- **WHEN** `purge all` is executed
- **THEN** deletion proceeds in order: queues → shards → telemetry → registry
- **AND** the registry.sqlite file and its -wal/-shm sidecars are deleted last (N-10)

### Requirement: Fail-CLOSED interlocks

The purge command SHALL enforce fail-CLOSED interlocks before any deletion:

1. **Lock symmetry (F-10)**: purge SHALL acquire the run-lock with `holder="purge"` before proceeding. A live pipeline lock SHALL cause refusal with nonzero exit.
2. **WAL freshness fallback**: if no lock file exists but WAL files are fresh (< 120s), purge SHALL refuse (conservative threshold, 05 §5.3.1).
3. **Shards gate (F-15)**: the `shards` profile SHALL require the `wire_coverage` table to exist (post-3a). Without it, purge SHALL refuse unless `--force` is provided, in which case a warning SHALL be recorded in the receipt.
4. **Missing area**: if a target area does not exist, purge SHALL skip it with a loud INFO message and zero counters (idempotency).
5. **Ambiguity**: if the workspace does not exist or is not a directory, or the profile is unknown, purge SHALL refuse BEFORE any deletion.

#### Scenario: Purge refuses with live pipeline lock

- **WHEN** a pipeline is running (lock exists with `holder="pipeline"`, pid is alive)
- **AND** a purge command attempts to acquire the lock
- **THEN** the purge command raises `LockHeldError` and exits with nonzero code
- **AND** no deletion occurs

#### Scenario: Stale lock is ignored loudly

- **WHEN** a lock file exists with a dead pid
- **THEN** purge logs INFO "stale lock ignored" and proceeds
- **AND** the stale lock is taken over (atomic replacement)

#### Scenario: Fresh WAL refuses

- **WHEN** no lock file exists
- **AND** a WAL file (registry or queue) has mtime < 120s old
- **THEN** purge refuses with nonzero exit and message "WAL files are too fresh"

#### Scenario: Shards gate refuses without wire_coverage

- **WHEN** `purge shards` is executed
- **AND** the `wire_coverage` table does not exist in registry.sqlite
- **THEN** purge refuses with nonzero exit and message naming the 3a requirement
- **AND** `--force` bypasses the gate and records a warning in the receipt

### Requirement: Dry-run mode

The `--dry-run` flag SHALL enumerate what would be deleted (areas, rows, files, bytes) but make ZERO changes. Dry-run SHALL exit 0 and write NO receipt.

#### Scenario: Dry-run enumerates without modifying

- **WHEN** `purge links --dry-run` is executed
- **THEN** the output lists the number of rows that would be deleted from each target table
- **AND** the registry.sqlite file remains unchanged
- **AND** no receipt is written

### Requirement: Backup with F-29 rights

The `--backup-to DIR` flag SHALL create backups of deleted data before deletion:
- Registry tables: NDJSON dumps (one file per table)
- Shards/snapshots: tar.gz archive
- Queues: byte copies of all files

The backup directory SHALL be created with mode 0700, and all backup artifacts SHALL have mode 0600 (F-29, explicit `os.chmod`, not umask-dependent). The receipt SHALL record the backup path, MD5 hashes of each artifact, and the actual backup directory mode as an octal string (e.g. `"0700"`) — canon 05 §5.4: the directory mode is part of the receipt audit trail.

#### Scenario: Backup creates artifacts with correct rights

- **WHEN** `purge links --backup-to /tmp/backup` is executed
- **THEN** `/tmp/backup` is created with mode 0700
- **AND** each backup file (e.g., `links.ndjson`) has mode 0600
- **AND** the receipt includes `backup.path` and `backup.md5s` fields
- **AND** the receipt `backup.mode` field records the actual directory mode as an octal string (`"0700"`)

### Requirement: Atomic receipt

Upon successful purge (non-dry-run), an atomic receipt SHALL be written to `<workspace>/purge_receipts/receipt_<ts>.json`. The receipt SHALL contain:
- `profile`: the purge profile
- `argv`: the command-line arguments
- `areas`: list of `{area, rows_deleted, files_deleted, bytes_freed}` for each area
- `backup`: `{path, md5s}` if `--backup-to` was provided, else null
- `config_md5`: MD5 of the config file if provided
- `git_head`: best-effort git HEAD (subprocess, null if unavailable)
- `started_ts`: Unix timestamp of purge start
- `duration_s`: purge duration in seconds
- `warnings`: list of warning strings (e.g., F-15 shards gate bypass)

The receipt SHALL contain ONLY counters and names, NEVER values (no secret material). The receipt SHALL be written atomically (temp file + `os.replace`).

#### Scenario: Receipt has correct schema

- **WHEN** `purge links` completes successfully
- **THEN** a receipt JSON file is written to `<workspace>/purge_receipts/`
- **AND** the receipt contains all required fields
- **AND** no secret values (sk-*, ghp_*, etc.) appear in the receipt

### Requirement: Idempotency

Purge SHALL be idempotent: a second run on the same workspace SHALL exit 0 with zero counters and skip messages for missing areas.

#### Scenario: Second purge has zero counters

- **WHEN** `purge links` is executed twice in succession
- **THEN** the first run deletes rows and records nonzero counters
- **AND** the second run exits 0 with zero counters
- **AND** two receipts are written (one per run)

### Requirement: Parser compatibility

The purge subparser SHALL be added to main.py with `dest="command"`, `default=None`. All existing CLI forms (`--validate`, `--create-config`, run path) SHALL remain byte-identical. The `-c` flag SHALL be a parent parser argument (not inherited by subparsers).

#### Scenario: Existing forms unchanged

- **WHEN** `main.py --validate` is executed
- **THEN** it behaves identically to before the purge subparser was added
- **AND** `main.py purge links --dry-run` works as a new form

## Scenarios

### Scenario: Purge requires --yes or --dry-run

- **WHEN** `purge links` is executed without `--yes` or `--dry-run`
- **THEN** purge refuses with nonzero exit and message "requires --yes or --dry-run"

### Scenario: Purge works without credentials

- **WHEN** the config file has no `github_credentials` section
- **AND** `purge links` is executed
- **THEN** purge proceeds normally (YAML-only parse, no credential validation)

### Scenario: N-11 legacy bulk_insert caveat

- **WHEN** a receipt is written
- **THEN** the docstring and receipt schema note that legacy `bulk_insert` rows have `dedup_id=''` (invisible to both dedup layers), which is relevant for future restore (slice 4.3)
