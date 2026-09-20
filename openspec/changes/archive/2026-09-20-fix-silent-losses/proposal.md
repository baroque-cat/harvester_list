# Proposal: fix-silent-losses

## Why

Transient network failures are silently converted into "successful" empty results in every pipeline stage. A failed search page is counted as processed and deduplication blocks its retry for the rest of the run (`stage/definition.py:191-193` → `stage/base.py:450-455`, dead retry branch at `base.py:457-470`); a failed gather HTTP fetch is recorded in the persistent registry as `gathered_ok` plus a coverage row (`search/client.py:1312` swallows the error into `[]`, so `stage/definition.py:561` reports success), suppressing re-gather until TTL expiry — precisely the "false-known" catastrophe the URL-identity doctrine calls worse than any duplicate; a check task dropped on provider-limiter exhaustion vanishes without requeue (`definition.py:661-667`). Separately, a configuration trap silently zeroes whole API-mode conditions: web-only qualifiers such as `content:` return HTTP 200 with zero matches from the code-search API (live probe 2026-09-20, `api.github.com/search/code`, Bearer-token auth: `q="sk-" AND content:"llm"` → `{"total_count":0,"incomplete_results":false,"items":[]}`, while `q="sk-"` → `total_count=46006272`).

These behaviors violate the project's core durability contract ("no request is ever lost") and poison the registry state that gather-skip, early-stop, and the key ledger depend on. They are also the hard precondition for the planned search-request aggregation (`plan_agr.md` → `add-search-aggregation`): a cache/singleflight layer built over a lossy foundation would inherit and multiply these losses (invariant I8).

## What Changes

- Introduce an outcome taxonomy at the client boundary: **legitimate zero** (HTTP 200 + successful parse, possibly empty items) vs **failure-empty** (exception after retries, limiter suppression `"", {}`, blank content). Failure-empties must raise a typed error instead of returning empty values.
- Search / Check / Inspect stages: propagate transient failures to the worker loop so the existing retry policy (`should_retry` → requeue, bounded by `max_retries_requeued`) fires; a failed task is never counted as a successful completion.
- Acquisition stage: `record_gather(success=True)` only when the HTTP fetch actually happened; fetch failure → `success=False` → the existing `regathered_failed_retry` re-gather path becomes reachable as designed.
- Check stage: provider-limiter exhaustion → requeue instead of silent `return None`.
- New tri-mode flag `failure_handling: legacy | shadow | strict` (project paradigm: ship risky behavior behind a flag; default `shadow` at release — detect, log, count, but keep legacy swallowing; promote to `strict` after measuring real-world frequency; rollback is a config flip).
- New per-stage observability counters: failure-empties detected, tasks requeued, tasks dropped after max retries (today search failures do not even increment `total_errors`).
- Config-validator lint: warning when a `use_api: true` provider carries conditions with web-only qualifiers (`content:` and similar), citing the live probe above.
- **Unchanged:** NDJSON shard semantics, `recover_tasks()`, registry schema and SQL, dedup-ID structure, wire-query construction, cooldown/backoff mechanics (60→900s), `_get_available()` blocking behavior.

## Capabilities

### New Capabilities
- `failure-handling`: empty-result taxonomy at the client boundary; failure propagation into the existing retry machinery; gather-outcome fidelity (no false `gathered_ok`); limiter-starvation requeue for checks; tri-mode rollout flag (`legacy|shadow|strict`); failure observability counters.
- `config-query-lint`: configuration-time validation warnings for transport-incompatible query syntax (web-only qualifiers under `use_api: true`).

### Modified Capabilities
None. Existing capability requirements (`link-registry`, `gather-skip`, `key-ledger`, `search-early-stop`, `date-extraction`, `repo-meta-enrichment`, `target-prioritization`) do not change: `record_gather(success=False)` → `visit_status='failed'` and the `regathered_failed_retry` counter already exist in specs and code — this change makes the stages actually reach those paths on HTTP failure. They will merely receive honest inputs.

## Impact

- **Code:** `search/client.py` (`collect()` decorator and blank-content paths, `search_api_with_count`/`search_web_with_count`, `_limit()` suppression path), `stage/definition.py` (all four stage workers), `stage/base.py` (accounting via propagated exceptions only — no structural change to the worker loop), `config/validator.py` (lint rule), `config/schemas.py` (flag), `core/metrics.py` + `state/` (counters).
- **Behavior:** failed tasks retry up to the configured bound, then drop loudly (counted + logged) instead of silently "succeeding"; under flaky networks with `strict` mode the harvested volume may slightly increase (previously lost pages get recovered); registry truthfulness improves (no false `gathered_ok`), which raises the quality of every downstream consumer (gather-skip decisions, early-stop trust gate, coverage rows).
- **Risks:** retry amplification under sustained outage — bounded by `max_retries_requeued`, retry-policy delays, and credential-cooldown blocking; `shadow` mode measures the real frequency before enforcement.
- **Sequencing:** this change is the precondition for `add-search-aggregation` (Phases 1–5 in `plan_agr.md`); Phase 5 gather-blob caching additionally depends on vector #1 being fixed here.
