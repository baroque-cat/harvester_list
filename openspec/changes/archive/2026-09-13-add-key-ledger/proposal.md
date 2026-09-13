# Proposal: add-key-ledger

## Why

Links are the unit of discovery; **keys are the unit of value** — and the expensive one: every CheckTask costs a real `chat()`-style call to a third-party provider API. Today nothing persists key identities: dedup lives in bounded in-memory task-id sets, so each restart re-verifies every previously seen key, `valid/*.ndjson` accumulates duplicate lines across runs, and statuses never age out (a key marked `wait_check` during a provider rate-limit storm is never revisited; a `valid` key is never confirmed again). Field data shows the waste is structural: ~43% of leaked AWS keys are observed in multiple sources, record-holders appear in thousands of locations — the same secret arrives at CheckStage over and over. This change closes the two-ledger model: persistent key identities, status-aware skip of redundant provider calls, and a scheduled re-check loop that keeps stored statuses fresh.

## What Changes

- **Ledger writes:** CheckStage upserts every check outcome into the registry `keys` table: `key_hash = sha256(provider|key|address|endpoint)` (mirrors the existing task-id semantics), masked key reference (no plaintext), status, `first_seen_ts`, `last_recheck_ts`, `source_url_hash`; additive column `last_seen_ts` tracks observations that did not trigger a provider call.
- **Inline check-skip:** before invoking the provider, CheckStage consults the ledger; if `key_hash` is known and its status is fresher than the per-status TTL, the provider is NOT called, no duplicate shard record is written, and only ledger observation fields update. Unknown hashes are ALWAYS checked. Default TTLs (configurable): `wait_check` 12 h, `no_quota` 72 h, `invalid` 168 h, `valid` 336 h (14 d).
- **Re-check driver:** `RecheckManager`, a `PeriodicTaskManager` subclass (`manager/base.py:177` pattern), periodically selects ledger keys whose status TTL expired (prioritizing `valid` and `wait_check`) and feeds ordinary CheckTasks into the pipeline — inheriting all existing provider rate limits and token buckets.
- **Secret hygiene:** the registry NEVER stores plaintext secrets — only `key_hash` plus a masked reference consistent with existing log-redaction conventions.
- Flags: `check_skip: off|on` (default off), `recheck_cron: off|on` (default off); fail-open: any ledger error ⇒ the check executes exactly as today.
- Metrics: `check_skipped_by_status{valid,wait_check,invalid,no_quota}`, `rechecks_enqueued`, `provider_calls_saved`.

## Capabilities

### New Capabilities
- `key-ledger`: persistent identity/status accounting for discovered credentials, TTL-gated suppression of redundant provider verification calls, scheduled status refresh, and plaintext-secret non-persistence.

### Modified Capabilities
<!-- none: link-registry/gather-skip/search-early-stop requirements unchanged; this capability adds writes to the registry's pre-existing (and migration-seeded) keys table -->

## Impact

- **Modified code:** `stage/definition.py` (CheckStage worker: ledger upsert + skip consultation), `storage/registry.py` (keys queries/upserts, additive `last_seen_ts` column, schema_version bump), `manager/pipeline.py` or `manager/task.py` (RecheckManager lifecycle), config schemas/loader/validator, stats display.
- **New code:** `manager/recheck.py` (RecheckManager), TTL policy unit.
- **Depends on:** add-link-registry (keys table + writer + runs journal). Independent of gather-skip/early-stop by design (different error currency: a false key status vs a missed file), but operationally sequenced after them so the registry is mature.
- **Behavior:** flags off — zero change. With `check_skip=on` — repeat runs make dramatically fewer provider calls; `valid/*.ndjson` stops accumulating cross-run duplicates. With `recheck_cron=on` — stored statuses converge to reality within TTL bounds (`wait_check` keys get promoted/demoted automatically).
- **Ethics/quota:** embodies the `--only-verified` philosophy — don't poke dead keys repeatedly, don't hammer live ones; all re-check traffic flows through existing per-provider rate limiters.
