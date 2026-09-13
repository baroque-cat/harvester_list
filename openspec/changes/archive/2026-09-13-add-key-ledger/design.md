# Design: add-key-ledger

## Context

See proposal.md — Why. Constraints:

- CheckStage worker (`stage/definition.py:401-462`) calls `provider.check(...)` (a real network `chat()` call, `provider/base.py:186-236`) and maps outcomes to ResultTypes (valid/no_quota/wait_check/invalid) which become shard appends via StageOutput → Pipeline → ResultManager.
- In-memory task dedup id is `check:{provider}:{key}:{address}:{endpoint}` (`definition.py:384-391`) — the natural identity semantics to persist.
- The registry `keys` table exists since change 1 (created empty, seeded by migration from `material/valid/...` shards and `summary.json`); the writer thread and fail-open wrapper are established patterns.
- `PeriodicTaskManager` (`manager/base.py:177`) is the project's standard for in-process periodic drivers (QueueManager, StatusManager use it).
- Existing log redaction masks secrets (`tools/patterns.py`, `tools/logger.py`); plaintext keys currently live only transiently in task/Service objects.

## Goals / Non-Goals

**Goals:**
- Persistent key identity + status memory; provider-call suppression within per-status freshness windows; scheduled convergence of stored statuses to reality.
- Absolute secret hygiene in persisted artifacts (hash + mask only).
- Zero behavior change under default flags; fail-open everywhere.

**Non-Goals:**
- No cross-provider key correlation (same secret under two providers = two ledger rows by design — statuses genuinely differ per provider).
- No notification/disclosure workflow (downstream of this project).
- No external scheduler integration (in-process cron only; system-cron orchestration is deployment-level).

## Decisions

**D1 — Identity: `key_hash = sha256(f"{provider}|{key}|{address}|{endpoint}")`.**
Mirrors the in-memory task id exactly, so memory-dedup and persistent ledger agree on identity. Unsalted on purpose: the hash is an identity join key, not a secret-protection mechanism (the registry is a local file; masking handles exposure). Alternative rejected: hashing key alone — address/endpoint variants legitimately have different statuses.

**D2 — Masked reference format `<first6>…<last4>`**, the same prefix+suffix family as the logger redaction (`tools/patterns.py` uses `first6...last6`), deliberately tightened to `last4` per tasks 2.2; strings ≤16 chars are masked entirely so a short secret is never reconstructible. Enough for human triage ("which key is this"), useless for reconstruction.

**D3 — Per-status TTL map (config `check_skip.ttl_hours`).**
Defaults: `wait_check: 12`, `no_quota: 72`, `invalid: 168`, `valid: 336`. Rationale: `wait_check` literally means "provider refused temporarily — retry soon" (rate-limit/no-model noise; short window absorbs flapping); `no_quota` reflects billing state that changes on recharge (days); `invalid` can resurrect via rotation but rarely (week); `valid` keys are empirically long-lived (field studies: ~88% of leaked cloud keys still authenticate after years) — fortnightly confirmation balances freshness against API etiquette. Resolves plan open question #1 (key part). NULL/inconsistent `last_recheck_ts` ⇒ treat as expired ⇒ check (safe direction).

**D4 — Skip point inside `_check_worker`, before `provider.check`.**
On skip: emit StageOutput with NO results (⇒ no `add_result` ⇒ no shard append — this is what stops `valid/*.ndjson` cross-run duplication), enqueue a registry observation update (`last_seen_ts`), increment `check_skipped_by_status{status}` and `provider_calls_saved`. Lookup is batched per worker iteration through the read-only connection pattern proven in add-gather-skip (D1 there). Implementation note: `KeyLedger.decide()` accepts a sequence (batch-ready API); since `BasePipelineStage._worker_loop` processes one task per iteration, batches are size 1 in practice — semantics are identical and no extra reads occur (one indexed point lookup per check).

**D5 — `RecheckManager(PeriodicTaskManager)` in `manager/recheck.py`.**
Config `recheck: {enabled, interval_hours=6, batch_size=50}`. Selection SQL: `WHERE status_ttl_expired ORDER BY CASE status WHEN 'wait_check' THEN 0 WHEN 'valid' THEN 1 WHEN 'no_quota' THEN 2 ELSE 3 END, last_recheck_ts ASC LIMIT batch`. Tasks are injected via the same entry point TaskManager uses for initial tasks, so they pass through CheckStage queues and inherit every existing rate limit/token bucket/cooldown — no new throttling code. Migration-seeded legacy keys (`last_recheck_ts=NULL`) count as expired and drain gradually through the batch cap (deliberate: refreshes stale imported statuses without storms).

**D6 — Schema evolution: additive `keys.last_seen_ts` column + `user_version` bump.**
Permitted by change-1's additive-only rule; no semantic change to existing columns.

**D7 — Secret hygiene enforced by test, not convention.**
The S9 test scans the raw database file bytes (and WAL) for planted plaintext secrets after full lifecycle operations. Decision logs and metrics carry hashes only. This makes accidental persistence (e.g., debug dump of a Service object) a failing build.

## Risks / Trade-offs

- [False-fresh status: key dies right after a skip] → bounded by TTLs; `valid` worst-case staleness 14 d; recheck cron converges it. Acceptable: statuses are operational metadata, not guarantees.
- [Re-check storm after long downtime or fresh migration import] → batch cap + interval + inherited provider rate limits; drain is gradual by construction.
- [Status flapping under provider rate-limit waves] → `wait_check` short TTL + existing credential cooldown mechanics absorb; ledger records transitions with timestamps for audit.
- [Ledger query latency in check workers] → batched read-only lookups; WAL concurrency proven in change 3.
- [Clock skew] → all timestamps UTC epoch floats at write time (convention since change 2).

## Migration Plan

1. Deploy with both flags off: ledger recording starts immediately (writes only), behavior unchanged.
2. Let recording run ≥1 cycle; verify row sanity vs `summary.json`.
3. Enable `check_skip=on`; monitor `provider_calls_saved` and spot-check that new finds still appear.
4. Enable `recheck_cron=on` with conservative batch (e.g., 20/tick); watch provider error rates.
5. Rollback: flip flags off; ledger keeps recording harmlessly.

## Open Questions

- None blocking. (Whether `material` records should also feed observations into `last_seen_ts` for unverified keys can be decided later from metrics; schema already supports it.)
