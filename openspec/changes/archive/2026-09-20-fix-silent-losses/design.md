# Design: fix-silent-losses

## Context

See proposal.md — Why. Current-state mechanics relevant to the approach (all verified 2026-09-20, file:line in proposal/plan_agr.md §3.1):

- Every stage worker wraps its body in a catch-all `except Exception → return None`; `process_task` passes `None` through as a result; `_worker_loop` then counts the task into `total_processed` (success accounting) and the retry branch (`should_retry` → re-enqueue) is unreachable because no exception ever propagates (`stage/base.py:446-475`).
- `put_task` marks the dedup id into `processed` at enqueue time; re-enqueue of a task with `attempts == 0` is rejected, but a task with `0 < attempts <= max_retries` passes the gate (`stage/base.py:250-257`) — the existing gate already supports bounded requeue once exceptions reach the loop.
- `collect()` is decorated `@handle_exceptions(default_result=[])`, converting every fetch failure into an empty list; `AcquisitionStage` interprets "no services" as success and calls `record_gather(success=True)` (`search/client.py:1312`, `stage/definition.py:561`).
- Client transport internals: `_http_get` raises after retries are exhausted (`client.py:604-605`); `_limit()` suppression returns `"", {}` without an exception (`client.py:370-372`); blank content collapses to `([], 0, "")` inside `search_api_with_count`/`search_web_with_count` (`client.py:1093-1094`, `1016-1017`).
- Project convention tension: parsers are strictly fail-open (malformed/absent data → NULL/degradation + counted warning, never an exception into worker loops). This design preserves that convention for **data-quality degradation** and deliberately ends it for **transport failure masquerading as data** — the two are different contracts (see D1).
- Credential protection (cooldown 60→900s ×2.0, `GithubCredentialLimited` rotation loop, blocking `_get_available`) works correctly today and is untouched.

## Goals / Non-Goals

**Goals:**
- No transient failure is ever accounted as a successful completion in any stage.
- No `gathered_ok`/coverage row is written unless the blob HTTP fetch actually succeeded.
- Failures become observable (counters) in ALL modes, and retryable within bounds in `strict` mode.
- Ship behind a tri-mode flag with a measurable shadow phase, per project paradigm.
- Static warning for the API-mode web-qualifier trap at config validation time.

**Non-Goals:**
- No change to credential cooldown/rotation mechanics, shard formats, `recover_tasks()`, registry schema/SQL, dedup-id structure, wire-query construction.
- No retroactive cleanup of historical false-`gathered_ok` rows (indistinguishable from real ones; they self-heal via `gather_ttl_hours` expiry).
- No aggregation/caching work (that is `add-search-aggregation`, which depends on this change — invariant I8 in plan_agr.md).
- No restructuring of `_worker_loop` itself: the retry machinery already exists; we make failures reach it.

## Decisions

**D1 — Typed transient-failure propagation instead of sentinels.**
Client-boundary functions raise a typed transient error (reuse/extend the existing `NetworkError`/`TimeoutError`/`ConnectionError` family surfaced by `_http_get`) when the fetch did not produce a usable response. Alternative considered: sentinel returns (`None` vs `[]`) — rejected: ambiguous at call sites, silently ignorable, and incompatible with the pure-functional `StageOutput` style; an exception is the only channel the existing retry policy listens to (`base.py:457-470`). The fail-open parsing convention is preserved: extraction/parsing of a successfully fetched payload still degrades to empty + counted warning, never raises.

**D2 — Empty-result taxonomy lives in the client layer.**
Only `search_api_with_count`/`search_web_with_count`/`collect` know whether HTTP actually happened. Contract: legitimate zero = HTTP 200 + successful parse (possibly `items: []` / zero links) → returned as an empty result, no error. Failure-empty = (a) exception after retries exhausted, (b) `_limit()` suppression `"", {}`, (c) blank/undecodable content → typed raise (strict) or counted detection (shadow). Alternative: taxonomy in the stage layer — rejected: stages cannot distinguish "GitHub answered zero" from "we never got an answer" once blanks collapse.

**D3 — Tri-mode flag `failure_handling: legacy | shadow | strict`, default `shadow` at release.**
`legacy`: byte-for-byte current behavior (kill-switch/rollback). `shadow`: failure-empties are detected, counted, and logged with full context, but the legacy swallow still happens — zero harvest impact, measures real-world frequency (which the lost historical logs can no longer tell us, plan_agr.md §3.4). `strict`: typed failures propagate → bounded requeue. Promotion shadow→strict after one representative cycle (including a rate-limit storm) shows acceptable failure rates; rollback is a config flip, never code removal. Alternative: direct strict default — rejected by the project's ship-behind-flags paradigm and absent frequency data.

**D4 — Gather-outcome fidelity via split error contract in `collect()`.**
The `@handle_exceptions(default_result=[])` decorator is narrowed: fetch-phase failures propagate (D1/D2); extraction-phase failures remain fail-open (`[]` + warning). `AcquisitionStage` records `success=True` only when the fetch happened — including the "fetched, zero keys extracted" case, which keeps the existing "researched, nothing found ≠ never researched" coverage semantics. Fetch failure (transient error, timeout, and HTTP 404 included) → `record_gather(success=False)` → `visit_status='failed'` → the already-specified `regathered_failed_retry` path becomes reachable. 404 is classified as failure, not legitimate zero: GitHub has served transient 404s during outages, and a cheap bounded re-probe beats a false-known that suppresses re-gather until TTL. Alternative: treat 404 as terminal "gone" — deferred (registry vocabulary has no `gone` state for links; adding one is a schema change out of scope).

**D5 — Check-stage limiter starvation requeues instead of dropping.**
`return None` on second failed acquire (`definition.py:661-667`) is replaced by raising the typed transient failure so the worker-loop retry policy requeues with its delay schedule; bound = `max_retries_requeued`, then loud drop + counter. Alternative: unbounded in-place sleep — rejected: pins a worker thread and distorts WorkerManager scaling signals.

**D6 — Honest accounting + new counters, no schema changes.**
With failures propagating, `total_errors` increments naturally (today search failures do not). New per-stage counters exposed in status/metrics: `failure_empties_detected` (shadow and strict modes — the shadow dataset; legacy stays byte-equivalent to pre-change with the new counters inert: present but zero, never incremented), `tasks_requeued`, `tasks_dropped_max_retries`. Counters live in the existing metrics structures (`core/metrics.py`, StageMetrics); no registry tables, no shard changes.

**D7 — Config lint is a warning, never an error.**
Validator emits a warning naming provider + condition index when `use_api: true` and the condition query contains a web-only qualifier (initial list: `content:`; kept in a constant so extension needs no validator change). Evidence base (rule: live-captured sample required): probe 2026-09-20, `api.github.com/search/code`, Bearer auth — `q="sk-" AND content:"llm"` → HTTP 200 `{"total_count":0,"incomplete_results":false,"items":[]}` vs `q="sk-"` → `total_count=46006272` (fixture recorded in plan_agr.md §3.1/§3.3). Warning-not-error because GitHub is a moving target: if the API ever supports the qualifier, the config remains loadable and the warning merely stale. Alternative: hard validation error — rejected: would brick working configs on a third-party whim.

**D8 — Uniform treatment of InspectStage.**
Same propagation contract for consistency, although its losses (model lists) are the least severe and recoverable via valid-shard replay. Keeping all four stages on one contract avoids a per-stage mental model.

## Risks / Trade-offs

- [Retry amplification under sustained outage] → bounded by `max_retries_requeued`, retry-policy delays, and credential-cooldown blocking (`_get_available` sleeps, tasks wait — verified `tools/credential.py:126-154`); `tasks_dropped_max_retries` makes exhaustion visible.
- [Shadow mode cannot prove requeue behavior in production] → compensated by live fault-injection verification (mock GitHub returning 500/timeouts) in tests.md before promotion; shadow proves only detection/frequency.
- [Longer runs under flaky networks in strict mode] → accepted: recovered pages are the point; counters quantify the cost; rollback = flag flip.
- [Legitimate zero vs dead-qualifier zero indistinguishable at runtime] → addressed statically by D7 lint; runtime makes no attempt.
- [`processed` dedup gate rejecting requeues] → gate already admits `0 < attempts <= max_retries` (`base.py:250-257`); covered by an explicit regression test.
- [Behavioral drift in downstream registry consumers] → none: `record_gather(success=False)` and `regathered_failed_retry` are already specified; this change only makes stages reach them.

## Migration Plan

1. Ship with `failure_handling: shadow` (default) — detection counters start accumulating immediately, harvest behavior identical to legacy.
2. Run one representative cycle (must include a rate-limit storm); review `failure_empties_detected` per stage and the dead-qualifier warnings.
3. Promote to `strict` via config flip; watch `tasks_requeued` / `tasks_dropped_max_retries` and registry `visit_status='failed'` inflow.
4. Rollback at any sign of trouble: flip to `legacy` (no code removal, no data migration). Historical false-`gathered_ok` rows age out via `gather_ttl_hours`.

## Open Questions

None blocking. (Terminal `gone` state for permanently-404 links is deferred to a possible future link-registry delta; the bounded re-probe chosen in D4 is safe meanwhile.)
