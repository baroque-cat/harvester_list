# snapshot-lifecycle Specification

## Purpose

Configurable lifecycle for periodic and final JSON snapshots built from NDJSON result shards. Snapshots are merged pretty-JSON artifacts with zero consumers in the repository (grep-verified); the default is OFF. When enabled, the system provides incremental builds (only new/updated shards), generation-based rotation (keep last N), size-guarded final builds (protect graceful stop), and compact JSON output. The starter-ordering bug (periodic snapshots never started because managers were empty at init time) is fixed by auto-starting periodic snapshots inside `get_manager` when a manager is created.

## Requirements

### Requirement: Master switch with disabled default

Snapshot building (both periodic and final) SHALL be controlled by `persistence.snapshots_enabled` with default `false`. When disabled, NO snapshot files SHALL be created, NO periodic threads SHALL start, and the final build on stop SHALL be skipped entirely. This is the only sanctioned default flip in the program (wave 1, slice 1.4).

#### Scenario: Disabled by default produces no snapshots

- **WHEN** the pipeline runs with default persistence config (no explicit `snapshots_enabled`)
- **THEN** no snapshot files are created in `data/providers/*/snapshots/`, no periodic snapshot threads start, and the final build on graceful stop is skipped

#### Scenario: Explicit enable activates both periodic and final

- **WHEN** `persistence.snapshots_enabled: true` and `format: ndjson`
- **THEN** periodic snapshot threads start for each provider (interval from `snapshot_interval_s`), and the final build runs on graceful stop (subject to size-guard)

#### Scenario: Simple mode (format: txt) ignores the flag

- **WHEN** `format: txt` (simple mode) regardless of `snapshots_enabled` value
- **THEN** no snapshot files are created, no periodic threads start, and TXT output is byte-identical to legacy simple-mode behavior (the flag does not leak into simple mode)

### Requirement: Configurable periodic interval

Periodic snapshot interval SHALL be controlled by `persistence.snapshot_interval_s` (default 3600 seconds = 1 hour). The value SHALL be validated as positive (>0) in both schema `__post_init__` and validator, with byte-identical error messages (`persistence.snapshot_interval_s must be positive`). The previously dead key `snapshot_interval` (never parsed by loader) is removed.

#### Scenario: Interval is respected

- **WHEN** `snapshots_enabled: true` and `snapshot_interval_s: 60`
- **THEN** periodic snapshot threads sleep for 60 seconds between builds

#### Scenario: Negative interval is rejected

- **WHEN** `snapshot_interval_s: -100`
- **THEN** config loading raises `ValueError: persistence.snapshot_interval_s must be positive`

### Requirement: Generation-based rotation

When `persistence.snapshot_keep_last_n > 0`, the system SHALL maintain versioned snapshot files (`{type}.json.{generation}`) and after each build SHALL delete all generations except the last N. Deletion SHALL be logged at INFO level with byte count. A generation is a complete set of `{type}.json` files from one build across all result types. When `keep_last_n = 0`, all generations are retained.

#### Scenario: Rotation keeps only last N generations

- **WHEN** `snapshot_keep_last_n: 2` and 3 builds complete
- **THEN** only generations 2 and 3 remain on disk; generation 1 is deleted with an INFO log showing bytes removed

#### Scenario: Zero keeps all generations

- **WHEN** `snapshot_keep_last_n: 0`
- **THEN** all generations are retained (no deletion)

#### Scenario: Negative value is rejected

- **WHEN** `snapshot_keep_last_n: -1`
- **THEN** config loading raises `ValueError: persistence.snapshot_keep_last_n must be non-negative`

### Requirement: Incremental builds with ts-based filtering

Snapshot builds SHALL be incremental: only shards with `last_ts` (from their `.index.json` sidecar) newer than the build marker timestamp (stored in `snapshots/.build_meta.json`) SHALL be read. The sidecar `.build_meta.json` stores `ts` (ISO-8601 timestamp of last build) and `shards` (list of shard filenames included). When the marker is absent or incompatible (missing `ts` field, corrupted JSON), a full rebuild of all shards SHALL occur with a loud WARNING log.

#### Scenario: First build reads all shards

- **WHEN** no `.build_meta.json` exists
- **THEN** all shards are read, `.build_meta.json` is created with current `ts` and shard list, and a full snapshot is written

#### Scenario: Second build reads only new shards

- **WHEN** a second build occurs after adding a new shard with `last_ts` newer than the marker
- **THEN** only the new shard is read (spy on file opens confirms old shards are skipped), and the snapshot is updated incrementally (existing records + new records)

#### Scenario: Updated shard is re-read

- **WHEN** a shard's `last_ts` is updated to be newer than the build marker
- **THEN** the shard is re-read in the next build (ts-based filter, not name-based), and the snapshot includes the updated records

#### Scenario: Missing marker triggers full rebuild

- **WHEN** `.build_meta.json` is missing or has no `ts` field
- **THEN** all shards are read (full rebuild), a WARNING is logged, and the marker is created

### Requirement: Size-guarded final build

The final snapshot build on graceful stop SHALL be skipped if the total size of all shard files exceeds `persistence.snapshot_final_max_bytes` (default 2 GB). When skipped, a single WARNING SHALL be logged with the actual size and limit. This protects graceful stop from SIGKILL when shards are large (measured: ~65 s/GB build time; 4.6 GB exceeds 300 s TimeoutStopSec).

#### Scenario: Small shards build normally

- **WHEN** total shard size is 100 MB and `snapshot_final_max_bytes: 2000000000`
- **THEN** the final build completes normally

#### Scenario: Large shards are skipped

- **WHEN** total shard size is 3 GB and `snapshot_final_max_bytes: 2000000000`
- **THEN** the final build is skipped, a WARNING is logged: "Skipping final snapshot for {provider}: shard size {size} bytes exceeds limit {limit} bytes"

#### Scenario: Negative limit is rejected

- **WHEN** `snapshot_final_max_bytes: -100`
- **THEN** config loading raises `ValueError: persistence.snapshot_final_max_bytes must be positive`

### Requirement: Compact JSON output

Snapshot JSON output SHALL be compact (no indentation, minimal separators) to reduce file size by 1.5–2.5×. The format remains valid JSON for hypothetical external consumers.

#### Scenario: Compact output has no indentation

- **WHEN** a snapshot is built
- **THEN** the output contains no `\n  ` (newline + 2 spaces) patterns, and uses `separators=(",", ":")` for minimal whitespace

### Requirement: Starter-ordering fix

Periodic snapshot threads SHALL be started inside `MultiResultManager.get_manager` when a manager is created (not in `Pipeline.__init__` before managers exist). This fixes the starter-ordering bug where `start_periodic_snapshots` iterated an empty `managers` dict at init time.

#### Scenario: Periodic threads start when managers are created

- **WHEN** `snapshots_enabled: true` and a provider's first result is added (triggering `get_manager`)
- **THEN** the periodic snapshot thread for that provider starts immediately (not waiting for `Pipeline.start()`)

### Requirement: Full persistence validation

All numeric keys in the `persistence` section SHALL be validated for positivity (or non-negativity for `snapshot_keep_last_n`) in both schema `__post_init__` and validator, with byte-identical error messages prefixed by `persistence.`. Unknown keys in the section SHALL produce a WARNING (not an error) listing the unknown key and known keys.

#### Scenario: All numeric keys validated

- **WHEN** any of `batch_size`, `save_interval`, `queue_interval`, `shutdown_timeout`, `snapshot_interval_s`, `snapshot_final_max_bytes` is ≤ 0, or `snapshot_keep_last_n` is < 0
- **THEN** config loading raises `ValueError: persistence.{key} must be positive` (or `must be non-negative` for `snapshot_keep_last_n`)

#### Scenario: Unknown key produces warning

- **WHEN** config contains `persistence.snapshot_interval: 300` (old dead key)
- **THEN** a WARNING is logged: "persistence.snapshot_interval is ignored (unknown key; known keys: ...)"

## Rollback

Setting `snapshots_enabled: true` restores pre-wave-1 behavior (periodic + final snapshots), but with interval 3600 instead of the old 300 (which was never parsed). All other improvements (incremental builds, rotation, size-guard, compact JSON) remain active.
