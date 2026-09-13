# Test Plan: add-key-ledger

<!-- Derived mechanically from specs/key-ledger/spec.md. One scenario = one test or
     one Manual entry. IDs stable: never renumber, append only.
     NOTE (planning mode): .py test files are authored in apply-phase task group 1;
     expected initial state: RED (ledger write path/skip/RecheckManager absent).

     RED baseline (apply task 1.2): `python -m pytest tests/test_key_ledger.py
     tests/test_check_skip.py tests/test_recheck_manager.py` fails collection with
     ModuleNotFoundError for `storage.key_ledger` / `manager.recheck` and missing
     `Config.check_skip` / `StageResources.key_ledger` / `CheckTask.source_url_hash`
     -- exactly the absent write/skip/driver paths this change adds. -->

## Traceability

| Scenario ID | Spec | Requirement | Scenario | Test file | Status |
|-------------|------|-------------|----------|-----------|--------|
| `key-ledger-S1` | `specs/key-ledger/spec.md` | Key ledger recording | First verification creates the ledger row | tests/test_key_ledger.py | GREEN |
| `key-ledger-S2` | `specs/key-ledger/spec.md` | Key ledger recording | Re-verification updates status and recency, preserves origin | tests/test_key_ledger.py | GREEN |
| `key-ledger-S3` | `specs/key-ledger/spec.md` | Inline check-skip with per-status TTL | Fresh valid key skips the provider call | tests/test_check_skip.py | GREEN |
| `key-ledger-S4` | `specs/key-ledger/spec.md` | Inline check-skip with per-status TTL | Unknown key is always checked | tests/test_check_skip.py | GREEN |
| `key-ledger-S5` | `specs/key-ledger/spec.md` | Inline check-skip with per-status TTL | Expired wait_check triggers re-verification | tests/test_check_skip.py | GREEN |
| `key-ledger-S6` | `specs/key-ledger/spec.md` | Inline check-skip with per-status TTL | Skipped checks do not duplicate shard records | tests/test_check_skip.py | GREEN |
| `key-ledger-S7` | `specs/key-ledger/spec.md` | Periodic re-check driver | Expired keys are re-queued by priority | tests/test_recheck_manager.py | GREEN |
| `key-ledger-S8` | `specs/key-ledger/spec.md` | Periodic re-check driver | Cron disabled produces no background checks | tests/test_recheck_manager.py | GREEN |
| `key-ledger-S9` | `specs/key-ledger/spec.md` | Plaintext secret non-persistence | Database contains no plaintext secrets | tests/test_key_ledger.py | GREEN |
| `key-ledger-S10` | `specs/key-ledger/spec.md` | Fail-open check path | Ledger outage never suppresses verification | tests/test_check_skip.py | GREEN |
| `key-ledger-S11` | `specs/key-ledger/spec.md` | Rollout flags | Defaults preserve current behavior | tests/test_check_skip.py | GREEN |

## Automated

### File: tests/test_key_ledger.py

Describe: Ledger recording and secret hygiene

<!-- Harness: tmp registry + mocked provider.check returning canned statuses; CheckStage worker invoked directly with synthetic CheckTasks. RED state: ledger upsert path absent. -->

- [x] `key-ledger-S1` — it("First verification creates the ledger row") <!-- WHEN unknown key checked → valid THEN keys row with status, masked ref, first_seen==last_recheck==last_seen (±tol), source_url_hash -->
- [x] `key-ledger-S2` — it("Re-verification updates status and recency, preserves origin") <!-- WHEN known wait_check key re-checked → valid THEN status valid, last_recheck advances, first_seen unchanged -->
- [x] `key-ledger-S9` — it("Database contains no plaintext secrets") <!-- WHEN full lifecycle ran with planted unique secret strings THEN raw DB+WAL byte scan finds zero plaintext occurrences; hashes+masks present -->

### File: tests/test_check_skip.py

Describe: Inline skip decisions, shard non-duplication, fail-open, flags

<!-- Harness: pre-populated ledger rows with controlled last_recheck_ts ages; capturing StageOutput collector asserting absence of result records; registry errors injected via mocks. RED state: skip consultation absent. -->

- [x] `key-ledger-S3` — it("Fresh valid key skips the provider call") <!-- WHEN valid key re-checked 2d ago (TTL 14d) arrives THEN provider.check NOT called, check_skipped_by_status{valid}=1, last_seen_ts advances -->
- [x] `key-ledger-S4` — it("Unknown key is always checked") <!-- WHEN hash absent from ledger THEN provider.check called -->
- [x] `key-ledger-S5` — it("Expired wait_check triggers re-verification") <!-- WHEN wait_check last_recheck 13h ago (TTL 12h) THEN provider.check called and ledger updated with outcome -->
- [x] `key-ledger-S6` — it("Skipped checks do not duplicate shard records") <!-- WHEN same valid key skipped on 3 simulated runs THEN StageOutput carries zero result records each time (shard appends happen once, at original verification) -->
- [x] `key-ledger-S10` — it("Ledger outage never suppresses verification") <!-- WHEN ledger lookup raises THEN all keys in batch provider-checked, warning logged, run degraded -->
- [x] `key-ledger-S11` — it("Defaults preserve current behavior") <!-- WHEN config lacks check_skip/recheck sections THEN every key checked, no background tasks, ledger rows still recorded -->

### File: tests/test_recheck_manager.py

Describe: Periodic re-check driver

<!-- Harness: ledger seeded with expired keys across statuses; manager tick invoked synchronously; CheckStage queue inspected. RED state: manager/recheck.py absent (ImportError). -->

- [x] `key-ledger-S7` — it("Expired keys are re-queued by priority") <!-- WHEN 5 expired keys (2 valid, 2 wait_check, 1 invalid), batch=3 THEN 3 tasks enqueued: both wait_check + oldest valid; rest deferred -->
- [x] `key-ledger-S8` — it("Cron disabled produces no background checks") <!-- WHEN recheck.enabled=false with many expired keys THEN zero tasks generated by the driver -->

## Manual

<!-- None: all eleven scenarios are automatable offline with mocked providers and the tmp-registry harness. The production-economics validation (provider-call count drops on repeat runs while novel-find rate holds; no valid key older than max-recheck-age) is executed as staging task 6.3. -->
