## Why

Gather's own local token basket is now the binding constraint on the pipeline, and it is
both **self-destructive** and **invisible**.

Self-destructive: when the basket has no token, `fetch_gather_content` reports the event to
the adaptive budget as a *request failure* and then raises a typed transient failure
(`search/client.py:1319-1322`):

```python
if not client._limit(service, credential):
    client._report(service, False, credential)          # (a) no request was ever issued
    raise TransientFetchError(f"gather fetch suppressed by local limiter for url: {target}")   # (b)
```

(a) `TokenBucket.adjust_rate(False)` halves the rate after 3 consecutive failures down to the
floor `0.1 × base_rate` (`core/models.py:717-724`) — for `github_raw` (`base_rate 2.0`,
`adaptive: true`, `config.yaml:70-72`) that is **0.2 req/s**. A slower basket produces more
contention, which produces more suppressions, which halves it again: a positive feedback loop
that collapses the budget to its floor and holds it there, because recovery needs **10
consecutive successes** at ×1.1 each. (b) `TransientFetchError` is the *failure-empty*
category, so the worker-loop policy requeues with `attempts++` and drops loudly at the cap
(`max_retries_requeued = 3`): a task can be discarded **having issued zero network requests**.
`_limit()` returns False only after it already slept the computed wait and lost the token to a
competing thread (`search/client.py:316-337`) — i.e. this is contention among the 8 gather
workers, not a remote refusal and not a fault of the task.

Measured, not inferred (600 s production-shaped soak on the shipped tree, 2026-09-25,
`/var/tmp/opencode-cl-gate/cl_gate_20260925T150311/evidence/soak.stdout`; archive
`2026-09-25-fix-credential-liveness/verification.md` §5.2, §5.7 observation O2):
**1 708** `suppressed by local limiter`, **876** `failure-empty detected` → **869** `requeued
successfully`, 0 drops in that run, HTTP 429 = 0 and HTTP 403 = 0. Every suppression was a
`github_raw` gather URL. The remote was never the limiting party — our own basket was, and it
charged the backlog for it.

Invisible: `gather_transport_metrics` has exactly two references in the whole codebase — the
`PipelineStatus` field (`core/metrics.py:223`) and its population
(`manager/pipeline.py:494`) — and **no consumer**. Nothing renders it, nor
`aggregation_metrics`, nor `refine_metrics`. Shipped requirement `gather-transport` S21 says
"status reporting exposes bytes and request counts per transport, refusals split by
classification, deferrals, revision fallbacks and truncations", and its requirement text
demands "per-transport volume, **latency**, refusal-class and deferral figures … exposed for
operators". Latency is not instrumented at all (`grep -n "latency|elapsed|perf_counter|monotonic"
search/client.py` → empty). Consequently the live gate had to record acceptance rows **A1
(bytes per file)** and **A2 (latency p50/p99)** as `NOT MEASURED` — the only two unmet rows of
14 — and any decision about basket size or worker count is currently taken blind.

Why now: plan §II.0 puts observability (O3) and the basket/parallelism reconciliation (O2)
ahead of everything else, because raising the refine-governor caps before them converts the
removed RAM ceiling into backlog self-burning rather than into coverage.

## What Changes

- **Gather starvation by the system's own budget becomes a deferral, not a task fault.**
  `fetch_gather_content` raises the existing typed `RateLimitDeferral` (bounded
  `wait_s` derived from the basket's own `wait_time`, clamped by `gather.max_refusal_wait_s`,
  `stage_pause=True` so the whole stage stops claiming while the shared budget refills) and
  counts it as `deferred_local_budget`. This reuses the DEFER seam already proven in production
  (`stage/base.py:686-697`): attempts unchanged, no error/requeue counters, nothing written to
  the registry, bounded by the queue's existing `max_age_hours` purge.
- **A request that was never issued is not reported as a failed request.** The starvation path
  stops calling `_report(service, False, …)`, which removes the adaptive collapse loop. Adaptive
  decay stays driven exclusively by outcomes of requests actually sent (unchanged at
  `search/client.py:1333,1382,1393,1397`).
- **Rollback flag `gather.defer_local_suppression: bool = True`.** `false` restores the legacy
  behavior byte-for-byte (`TransientFetchError` + failure report), per the house rule that
  rollback is a config flip, never code removal. Loud validation, documented in
  `examples/config-full.yaml` and `README.md`.
- **Per-transport latency instrumentation with bounded memory**: a fixed millisecond-band
  histogram per transport kind, exposed as derived integer keys
  (`latency_samples_<kind>`, `latency_p50_ms_<kind>`, `latency_p99_ms_<kind>`) through the
  existing flat `Dict[str, int]` surface. Makes A2 decidable (band resolution separates
  750 ms from 1000 ms) without unbounded per-request storage.
- **Operator rendering of the three collected-but-unread metric dicts** — `gather_transport_metrics`,
  `aggregation_metrics`, `refine_metrics` — in the existing detailed pipeline section, following
  the house `_format_*_metrics_line` pattern (`state/display.py:313-412`): one compact line per
  dict, rendered only when the dict is non-empty, never raising, never printing a credential.
  Closes S21 for operators and makes A1/A2 readable from a real run. The `credential_metrics`
  shutdown line added by design D20 stays untouched (pinned and cited in gate evidence).
- **Dead code removed**: `SearchStage._apply_rate_limit` (`stage/definition.py:336-350`) has
  zero callers repo-wide (including tests) and encodes the legacy starvation semantics
  (`return False` → caller proceeds); leaving it invites reintroducing a silent-empty path.
- **Explicitly NOT changed** (recorded as decisions, not omissions): the search-boundary
  taxonomy (`failure-handling` "Limiter suppression is a failure-empty" stays valid for the
  search fetch boundary, where a suppression surfaces as a blank payload and the stage runs a
  single worker with no measured starvation), and the check stage's provider-basket starvation
  (its own shipped requirement mandates a bounded requeue, and that path deliberately reports
  nothing to the basket — `stage/definition.py:753,800-803`). The resulting asymmetry is
  documented in the delta specs.

## Capabilities

### New Capabilities
- `run-observability`: run-level metric dicts that the pipeline collects SHALL be readable by an
  operator from status output — completeness (every collected dict gets a line), stability
  (deterministic key order), safety (no credential material, no exception on missing/empty
  data), and the derived per-transport latency percentiles and byte volumes that acceptance
  rows A1/A2 cite.

### Modified Capabilities
- `gather-transport`: (1) "Gather traffic is throttled under its own budget" — adaptive
  reporting is scoped to requests actually issued; a local-budget starvation is not a failure
  signal (S9 amended, new scenarios appended); (2) "Refusals are classified by signal and
  produce bounded counted outcomes" — a refusal by the system's *own* budget is a deferral with
  a bounded wait and a stage pause, counted separately, never a task-level fault and never an
  attempt-counter increment; (3) "Payload handling is economical, bounded and observable" —
  S21's exposure clause becomes operator-rendered output including latency percentiles.
- `failure-handling`: "Empty-result taxonomy at the fetch boundary" — the failure-empty
  classification of a local-limiter suppression is scoped to the search fetch boundary; a
  **gather** fetch refused by the system's own budget belongs to the third category (deferral),
  which is neither an answer nor a task fault. Existing scenario IDs and titles are preserved;
  new scenarios are appended.

## Impact

- **Code**: `search/client.py` (starvation branch, latency histogram, stat key tuple),
  `state/display.py` (three new formatter lines + registration), `config/schemas.py`,
  `config/loader.py`, `config/validator.py` (`gather.defer_local_suppression`),
  `stage/definition.py` (dead-method removal only), `examples/config-full.yaml`, `README.md`.
  No stage-loop, queue, registry or shard changes: the DEFER seam, the visibility-window
  invariant and the three-outcome taxonomy already exist and are reused.
- **Configuration**: one new boolean under the existing `gather:` section, default `true`
  (the fixed behavior); `false` = legacy. No new sections, no migration.
- **Metrics surface**: `gather_transport_metrics` gains declared keys (`deferred_local_budget`,
  `latency_samples_<kind>`, `latency_p50_ms_<kind>`, `latency_p99_ms_<kind>`); undeclared keys
  remain rejected loudly by `_gather_stat_inc` (the published-schema guard S21 relies on).
  `PipelineStatus` gains no fields.
- **Dependencies**: none (stdlib only).
- **Tests**: new pins for the deferral classification, the absence of adaptive poisoning, the
  histogram's bounded memory and percentile correctness, and the rendering contract. No
  existing pin becomes obsolete: `test_s3_limiter_suppression_is_failure_empty`
  (`tests/test_fh_client_taxonomy.py:87`) pins the **search** boundary, which this change does
  not touch, and the gather-transport limiter fixtures (`tests/test_gt_transport.py:102-106`)
  construct a client without exercising the starvation branch. Any pin found to contradict the
  amended taxonomy during implementation is replaced, not weakened, and the replacement is
  recorded in `tests.md`.
- **Live gate**: required before promotion (house doctrine). Needs a production-shaped soak with
  the basket deliberately under-provisioned relative to `pipeline.threads.gather` so suppressions
  occur organically, plus a rollback drill (`defer_local_suppression: false`) and the A1/A2
  readings this change exists to make possible.
- **Risk**: low. Behavior change is confined to one branch of one function and is flag-gated;
  the rendering change is additive and fail-open. The main hazard — a deferred task cycling
  forever — is already bounded by `max_age_hours` purge with loud accounting, and the stage
  pause prevents a hot claim/defer loop across 8 workers.
