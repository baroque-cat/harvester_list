# Delta Spec: target-prioritization

## Purpose

Orders the harvested corpus by deep-scan value: computes a configurable weighted priority per repository from accumulated registry evidence (key statuses, push freshness, size), keeps scores converged with ledger state through event-driven recomputation and run-finish sweeps, and publishes a deterministic, schema-versioned candidate export as the stable handoff contract for downstream deep-scan consumers.

## ADDED Requirements

### Requirement: Repository priority scoring

The system SHALL compute a numeric priority per repository as `W1·[has VALID key] + W2·[has wait_check/no_quota key] + W4·freshness(repo_pushed_at) − W5·size_penalty(repo_size_kb)` with documented default weights, denormalized into every `links.priority` row of that repository. Freshness SHALL decay monotonically with age toward zero; the size penalty SHALL grow with repository size above a configured threshold. Missing inputs (NULL dates, sizes, or absent keys) SHALL contribute zero to their components without error.

#### Scenario: Valid-key repo outranks keyless repo

- **WHEN** repository A contains a link whose key holds status `valid` and repository B has identical freshness/size but no recorded keys
- **THEN** priority(A) > priority(B) by at least the W1 component

#### Scenario: Fresher activity outranks stale

- **WHEN** two repositories have identical key evidence but repo_pushed_at differs by 60 days
- **THEN** the fresher repository scores strictly higher on the W4 component

#### Scenario: Oversized repo is penalized

- **WHEN** two repositories have identical evidence except repo_size_kb crosses the penalty threshold
- **THEN** the oversized repository scores lower by the computed penalty

#### Scenario: Sparse rows degrade neutrally

- **WHEN** a repository row has NULL repo_pushed_at, NULL repo_size_kb, and no key coverage
- **THEN** scoring completes with priority 0 (all components neutral) and no exception is raised

### Requirement: Score convergence with ledger state

Priority recomputation SHALL be triggered by evidence changes (key-status upserts, date merges mark the owning repository dirty; rescores execute batched through the registry writer path) and SHALL be completed by a full sweep at run finish, so persisted priorities never lag the ledger by more than one run.

#### Scenario: Status transition rescores without waiting for sweep

- **WHEN** a `wait_check` key transitions to `valid` mid-run
- **THEN** its repository's priority reflects the W1 component before the run finishes

#### Scenario: Run-finish sweep reconciles everything

- **WHEN** a run ends after evidence mutations that bypassed dirty-marking (e.g., migration import)
- **THEN** the post-sweep priorities of all repositories equal freshly computed values from current ledger state

### Requirement: Candidate export utility

A read-only CLI tool SHALL export deep-scan candidates aggregated per repository as newline-delimited JSON (default) or CSV (`--csv`), sorted deterministically by priority desc, then repo_pushed_at desc (NULLs last), then owner/repo. Each record SHALL carry at least: `schema_version`, owner, repo, best observed key status, counts by status, repo_pushed_at, repo_size_kb, priority, and a bounded sample of link URLs. Filters (`--min-priority`, `--limit`) SHALL be supported.

#### Scenario: NDJSON export is ordered and complete

- **WHEN** the export runs over a seeded registry with mixed priorities
- **THEN** output lines are valid JSON objects in strict priority-descending order, each containing all required fields including `schema_version`

#### Scenario: CSV variant carries equivalent content

- **WHEN** the export runs with `--csv` over the same registry
- **THEN** the CSV contains the same repositories in the same order with equivalent field values

### Requirement: Read-side isolation

Scoring and export SHALL NOT alter pipeline behavior or persisted results: result shards, snapshots, queues, and task flow remain untouched; the only registry mutations are `links.priority` values and internal recomputation bookkeeping. The export tool SHALL perform no network activity.

#### Scenario: Export leaves operational artifacts untouched

- **WHEN** the export tool completes against a workspace with existing shards and snapshots
- **THEN** shard/snapshot file contents are byte-identical to their pre-export state and registry diffs are confined to priority-related columns

### Requirement: Configurable weights and thresholds

All scoring parameters (W1, W2, W4 magnitude and decay half-life, W5 magnitude, threshold, ramp) SHALL be configurable via the standard config path with validated ranges: weight magnitudes are non-negative (zero disables a component, as exercised by the Custom-weights scenario), the decay half-life is positive, and the ramp end exceeds the threshold; defaults SHALL match the documented values used at introduction. An optional top-N candidates display SHALL be gated by configuration and disabled by default.

#### Scenario: Custom weights change ordering

- **WHEN** W1 is set to 0 while W4 remains positive
- **THEN** a keyless but fresh repository can outrank a stale valid-key repository, demonstrating weights drive the score
