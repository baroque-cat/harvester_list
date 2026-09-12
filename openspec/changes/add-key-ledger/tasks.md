# Tasks: add-key-ledger

## 1. RED baseline

- [ ] 1.1 Author tests from tests.md: `tests/test_key_ledger.py` (key-ledger-S1, S2, S9), `tests/test_check_skip.py` (S3..S6, S10, S11), `tests/test_recheck_manager.py` (S7, S8); mocked-provider harness in conftest
- [ ] 1.2 Run pytest; confirm all new tests fail for expected reasons (no ledger write path, no skip consultation, no `manager/recheck.py`) — record RED in tests.md

## 2. Schema evolution & ledger writes

- [ ] 2.1 Additive migration: `keys.last_seen_ts` column + `PRAGMA user_version` bump with idempotent ALTER guard (design D6)
- [ ] 2.2 Implement `key_hash` computation (`sha256(provider|key|address|endpoint)`) and masked-reference helper `<first6>…<last4>` aligned with log redaction (key-ledger-S1; design D1, D2)
- [ ] 2.3 Wire CheckStage outcome upserts through the registry writer: status, first_seen once, last_recheck on real calls, last_seen on every observation, source_url_hash (key-ledger-S1, S2)

## 3. Inline check-skip

- [ ] 3.1 Add `check_skip` config section: flag off|on (default off) + `ttl_hours` map (wait_check 12, no_quota 72, invalid 168, valid 336) to schemas/loader/validator + examples
- [ ] 3.2 Implement batched ledger lookup in `_check_worker` before `provider.check` via the read-only connection pattern; NULL/inconsistent `last_recheck_ts` ⇒ expired ⇒ check (key-ledger-S3, S4, S5)
- [ ] 3.3 On skip: emit StageOutput without results (no shard append), update observation fields, increment `check_skipped_by_status{status}` + `provider_calls_saved` (key-ledger-S6)
- [ ] 3.4 Fail-open guard: lookup/write errors ⇒ execute checks as pre-change, warn-once, mark run degraded (key-ledger-S10)

## 4. Re-check driver

- [ ] 4.1 Implement `manager/recheck.py`: `RecheckManager(PeriodicTaskManager)` with `recheck.{enabled,interval_hours=6,batch_size=50}` config (key-ledger-S8)
- [ ] 4.2 Selection query: TTL-expired statuses, priority order wait_check→valid→no_quota→invalid, oldest `last_recheck_ts` first, LIMIT batch; legacy NULL `last_recheck_ts` counts expired (key-ledger-S7; design D5)
- [ ] 4.3 Enqueue via the standard task entry point so provider rate limits/token buckets apply; lifecycle wiring start/stop alongside QueueManager

## 5. Secret hygiene

- [ ] 5.1 Audit all new code paths for plaintext leakage (registry rows, decision logs, metrics, warnings); enforce hash/mask-only (key-ledger-S9)
- [ ] 5.2 Byte-scan test hardened: plant unique canary secrets, assert absence in DB + WAL + JSONL after full lifecycle

## 6. GREEN & verification

- [ ] 6.1 Drive all automated tests to GREEN; update tests.md statuses
- [ ] 6.2 Regression: suites of changes 1–4 green; flags-off A/B confirms unchanged shards and provider-call counts
- [ ] 6.3 Staging measurement: repeat-run provider calls drop while novel-find rate holds; no `valid` ledger key older than max-recheck-age after cron cycle; document numbers

## 7. Documentation

- [ ] 7.1 README: two-ledger model summary, TTL table + rationale, recheck cron operation, secret-hygiene guarantee
- [ ] 7.2 `docs/specs/registry_flags_metrics.md`: fill `check_skip`/`recheck_cron` rows + `check_skipped_by_status`, `rechecks_enqueued`, `provider_calls_saved` definitions
