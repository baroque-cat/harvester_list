# key-ledger Specification

## Purpose

Maintains a persistent ledger of credential identities and their verification statuses so that redundant provider API calls are suppressed within status-dependent freshness windows, stored statuses are periodically refreshed toward reality, and no plaintext secret is ever persisted. Validated on a live staging harness (2026-09-13): a repeat run dropped provider calls 20 → 0 with `check_skipped_by_status` exactly mirroring the ledger, and cron cycles refreshed every TTL-expired `valid` key with zero stale rows afterwards (measurement protocol and numbers in `docs/specs/registry_flags_metrics.md`).

## Requirements
### Requirement: Key ledger recording

Every completed credential check SHALL upsert a ledger row identified by `key_hash = sha256(provider|key|address|endpoint)`, carrying the resulting status, a masked key reference, `first_seen_ts` (set once, never overwritten), `last_recheck_ts` (set whenever the provider was actually called), `last_seen_ts` (set on every observation), and the source link identity. Status transitions SHALL preserve history timestamps needed for audit via the runs journal.

#### Scenario: First verification creates the ledger row

- **WHEN** a previously unknown key completes a provider check with status `valid`
- **THEN** a `keys` row exists with that status, `first_seen_ts == last_recheck_ts == last_seen_ts` (within tolerance), and the masked reference

#### Scenario: Re-verification updates status and recency, preserves origin

- **WHEN** a known `wait_check` key is checked again and now returns `valid`
- **THEN** the row's status becomes `valid`, `last_recheck_ts` advances, and `first_seen_ts` remains the original value

### Requirement: Inline check-skip with per-status TTL

When `check_skip=on`, before invoking a provider the system SHALL consult the ledger: if the `key_hash` is known AND `last_recheck_ts` is within the TTL configured for its stored status (`wait_check` 12 h, `no_quota` 72 h, `invalid` 168 h, `valid` 336 h by default; all configurable), the provider SHALL NOT be called and NO duplicate result record SHALL be appended to result shards; only ledger observation fields update. Unknown hashes SHALL always be checked. Any ledger inconsistency (missing/NULL `last_recheck_ts`) SHALL resolve to "check it".

#### Scenario: Fresh valid key skips the provider call

- **WHEN** a key verified `valid` 2 days ago is encountered again (TTL 14 d)
- **THEN** no provider call occurs, `check_skipped_by_status{valid}` increments, and `last_seen_ts` advances

#### Scenario: Unknown key is always checked

- **WHEN** a key whose hash is absent from the ledger arrives at CheckStage
- **THEN** the provider IS called regardless of any other state

#### Scenario: Expired wait_check triggers re-verification

- **WHEN** a key stored as `wait_check` was last re-checked 13 hours ago (TTL 12 h)
- **THEN** the provider IS called and the ledger status updates with the outcome

#### Scenario: Skipped checks do not duplicate shard records

- **WHEN** the same valid key is skipped on three consecutive runs
- **THEN** `valid` shards contain exactly one record for it from the original verification (no new appends from skipped observations)

### Requirement: Periodic re-check driver

When `recheck.enabled=true` (planning name `recheck_cron=on`), a periodic driver SHALL select ledger keys whose status TTL has expired — prioritizing `valid` and `wait_check` over `invalid`/`no_quota`, oldest `last_recheck_ts` first — in bounded batches, and SHALL enqueue ordinary CheckTasks that flow through the existing pipeline rate limiting. When the flag is `off`, no re-check tasks SHALL originate.

#### Scenario: Expired keys are re-queued by priority

- **WHEN** the driver tick finds 5 expired keys (2 valid, 2 wait_check, 1 invalid) with batch limit 3
- **THEN** 3 CheckTasks are enqueued covering both wait_check keys and the oldest valid key, and the remainder waits for the next tick

#### Scenario: Cron disabled produces no background checks

- **WHEN** `recheck.enabled=false` and many ledger keys are past TTL
- **THEN** zero re-check tasks are generated outside the normal pipeline flow

### Requirement: Plaintext secret non-persistence

The ledger SHALL store only the salt-free identity hash and a masked reference (prefix+suffix masking in the same family as the existing log redaction; exact format `<first6>…<last4>` per design D2); the full secret SHALL NOT appear in the registry database, its WAL files, decision logs, or metrics.

#### Scenario: Database contains no plaintext secrets

- **WHEN** keys have been recorded and the database file content is scanned for the original secret strings
- **THEN** no plaintext secret is found anywhere in the file, while masked references and hashes are present

### Requirement: Fail-open check path

Any ledger read/write error around CheckStage SHALL degrade to current behavior: the provider check executes, shard records are written as today, a warning is logged, and the run is marked degraded. A skip decision SHALL never be derived from an errored lookup.

#### Scenario: Ledger outage never suppresses verification

- **WHEN** the ledger query raises for a batch of incoming keys
- **THEN** all of them are checked by the provider exactly as pre-change and the run continues

### Requirement: Rollout flags

`check_skip.mode` SHALL accept `off|on` (default `off`) and `recheck.enabled` SHALL accept `true|false` (default `false`; planning name `recheck_cron`, see tasks 4.1 / design D5 for the canonical config shape), each parsed and validated via the standard config path; flipping either back to its default SHALL fully restore pre-change behavior without code changes.

#### Scenario: Defaults preserve current behavior

- **WHEN** the harvester runs with a config lacking both flags
- **THEN** every arriving key is provider-checked and no background re-checks occur, while ledger recording still populates the keys table
