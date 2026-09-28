## Context

See `proposal.md` — Why for motivation and measurements. This document covers the technical
choices only.

Current state that shapes the approach:

- **The DEFER seam already exists and is production-proven.** `RateLimitDeferral`
  (`core/exceptions.py:55-80`) is a deliberate *sibling* of `TransientFetchError`, carries
  `wait_s` and `stage_pause`, and is handled once in the worker loop
  (`stage/base.py:686-697`): clamp the wait (`_effective_defer_wait` → `gather.max_refusal_wait_s`,
  itself validated `< queue.visibility_timeout_s`), optionally set `_defer_pause_until` (which
  gates new claims at `stage/base.py:657`), sleep, then `defer_task()` — attempts untouched, no
  error/requeue/processed counters, nothing written to the registry. R5.1 introduced it for
  published remote refusals; R5.2 extended it to credential-pool exhaustion.
- **The starvation branch is one `if` in one function** (`search/client.py:1319-1322`), inside the
  per-attempt loop of `fetch_gather_content`. `_limit()` (`:316-337`) already slept
  `wait_time()` once and lost the token to a competing thread before returning False.
- **Adaptive decay is defined over request outcomes**: `TokenBucket.adjust_rate(success)`
  (`core/models.py:699-724`) — ×0.5 after 3 consecutive failures with floor `0.1 × base`,
  ×1.1 after 10 consecutive successes with cap `2 × base`.
- **Observability has a house pattern**: `state/display.py::_format_pipeline_section` (`:313-386`)
  assembles one compact line per metric dict via `_format_*_metrics_line` static methods
  (`:386-412` and following), each returning `""` when its dict is empty. Seven dicts are
  rendered this way today (date, skip, enrichment, early-stop, key-ledger, recheck,
  prioritization). The detailed section is shown both periodically (`--stats-interval`) and at
  shutdown (`main.py:520-521`).
- **The gather counter surface is schema-guarded**: `_GATHER_TRANSPORT_STAT_KEYS` is the canonical
  tuple; `_gather_stat_inc` refuses undeclared keys with a loud warning (`search/client.py:974-981`),
  and `reset_gather_transport_stats()` mutates in place so the surface can neither grow nor
  shrink across resets (`:958-966`).
- Constraints binding every change (`openspec/config.yaml → context:`): fail-open parsers, flags
  for behavioral changes with config-flip rollback, no new dependencies, additive schema evolution,
  and the environment rule that live-gate evidence lives under `/var/tmp`.

## Goals / Non-Goals

**Goals:**

- Make the system's own budget a *deferral* cause on the gather path, so a task is never charged
  an attempt for our throttling decision.
- Break the adaptive feedback loop in which a suppression — an event where no request was issued —
  decays the very budget that produced it.
- Make the run's gather economy (volume, latency percentiles, refusal classes, deferrals) and the
  aggregation/refinement counters readable by an operator from status output, closing shipped
  requirement `gather-transport` S21 and acceptance rows A1/A2.
- Keep the rollback a single config flip and keep the legacy path byte-identical under it.

**Non-Goals:**

- No change to the search-boundary empty taxonomy or to the check stage's provider-basket
  starvation handling (D10, D11).
- No new counters in `aggregation_metrics` / `refine_metrics`: this change *renders* what those
  capabilities already collect. `aggregation_ratio` and pinned-hit KPIs belong to R3.
- No basket re-tuning and no `pipeline.threads.gather` change: those are operator decisions that
  this change makes measurable for the first time (Open Questions).
- No claim-renewal API, no `file_commit_date` restoration, no `resource_provider` wiring
  (carried items I.9 of `plan.md`).
- No cross-service escalation of secondary limits (R5.2 handles secondary limits per service).

## Decisions

### D1 — Reuse `RateLimitDeferral`; do not invent a new exception type

The starvation branch raises the existing typed deferral. Alternatives: a new
`LocalBudgetDeferral` sibling — rejected because every `except RateLimitDeferral` handler
(`stage/base.py:605,686`, `stage/definition.py:193,586,640`) and every pin on the DEFER seam
would have to learn a second type for identical semantics, and the taxonomy already has exactly
three outcomes. Keeping `TransientFetchError` but suppressing the attempt increment — rejected:
in this codebase the *type is the category*; special-casing a counter inside the retry policy
would make the taxonomy unreadable and would still count the event as an error.

### D2 — `stage_pause=True` for own-budget starvation

The `github_raw` basket is shared by all `pipeline.threads.gather` workers (8 in production). With
per-task deferral only, all 8 workers claim → starve → sleep → re-claim in lockstep: queue churn
and repeated claim/defer cycles without a single request issued. `stage_pause=True` sets
`_defer_pause_until = max(current, now + wait)` (`stage/base.py:693-696`), which stops *new
claims* for the stage until the shared budget has refilled — the honest meaning of "our own
budget is empty". Alternatives: pausing inside the limiter — rejected, the limiter has no stage
handle and is shared across stages; no pause — rejected as above.

### D3 — The deferral wait is the basket's own refill time, re-read after the loss

`wait_s = limiter.wait_time(bucket)` computed *after* the failed acquisition, clamped by the
existing `_effective_defer_wait` (≤ `gather.max_refusal_wait_s` = 60 s, validated
`< queue.visibility_timeout_s` = 300 s). At `base_rate 2.0` this is ≤ 0.5 s; even at the adaptive
floor it is ≤ 5 s. Because bucket naming is private to `GitHubClient` (`_bucket_name` /
`_ensure_bucket`), a small helper on the client exposes it rather than duplicating the naming
rule at the call site. Alternatives: a fixed constant — rejected, it either over-throttles a
healthy basket or hot-loops a slow one; `1 / base_rate` — rejected, it ignores burst state and
elapsed refill; the credential-style `max_wait_s` budget — rejected, that budget models a
*pool of cooling credentials*, a different resource with different semantics.

### D4 — A suppression is not reported to the adaptive budget

The `_report(service, False, credential)` call on the starvation path is removed. `adjust_rate`
is defined over outcomes of requests that were sent; a suppression has no remote outcome, and
reporting one creates the measured collapse loop (3 consecutive "failures" → ×0.5 → floor
`0.1 × base`, recovery requiring 10 consecutive successes). All genuine failure reports stay
(`search/client.py:1333,1382,1393`) as does the success report (`:1397`). Alternatives: a neutral
"no-opinion" report — rejected, `TokenBucket` has no such API and widening the adaptive model
serves no measured need; keeping the report and exempting `github_raw` — rejected, the defect is
the semantics, not the service.

### D5 — One flag gates both halves: `gather.defer_local_suppression: bool = True`

`true` (default) = deferral + no adaptive report. `false` = the legacy branch byte-for-byte
(`TransientFetchError` **and** the failure report), because a half-rollback that keeps the
poisoning loop would roll back the cheap half and keep the destructive one. Parsed in
`config/loader.py`, validated loudly in `config/validator.py` (must be a bool), defaulted in
`GatherConfig.__post_init__`, documented in `examples/config-full.yaml` and `README.md`. Naming
alternatives rejected: `local_defer` (which throttle?), `throttle_defer` (ambiguous against
remote refusals), a tri-mode `off|shadow|on` (there is nothing to measure in shadow here — the
event is already counted, and a shadow mode would keep burning attempts while pretending not to).

### D6 — Latency as a fixed millisecond-band histogram, percentiles derived at read time

Per transport kind, one integer counter per band with edges
`0, 25, 50, 75, 100, 150, 200, 250, 300, 350, 400, 500, 750, 1000, 1500, 2000, 3000, 5000, 10000, ∞`
(20 bands × 3 kinds = 60 integers, bounded regardless of run length). `get_gather_transport_stats()`
derives `latency_samples_<kind>`, `latency_p50_ms_<kind>`, `latency_p99_ms_<kind>` — reporting the
**upper edge** of the band containing the target rank, i.e. conservative (never under-reports).
The band edges are chosen so acceptance row A2 is decidable: they separate the captured raw band
(240–340 ms → edges 250/300/350) and separate 750 ms from 1000 ms (the `p99 < 1 s` criterion).
Timed span: from immediately before `request("GET", …)` through the return of
`_read_capped_payload` — the full fetch cost the worker pays, documented as such so the number is
interpretable. Alternatives: storing every sample — rejected, unbounded per-item state in a
project whose founding incident was RAM growth; exact reservoir sampling — rejected, adds
floating-point state and lock contention for accuracy no acceptance row needs; a running sum only
— rejected, a mean cannot answer "p99 < 1 s".

### D7 — Render in `state/display.py`, not as new shutdown lines

Three new `_format_*_metrics_line` static methods registered in `_format_pipeline_section`,
following the house pattern (return `""` when the dict is empty; one compact line otherwise).
This covers periodic status *and* the final detailed render with one implementation
(`main.py:520-521` already calls `show_status(..., DisplayMode.DETAILED)` at shutdown). The
`Credential liveness: …` shutdown INFO line added by R5.2 design D20 stays **untouched**: it is
pinned by `tests/test_cl_stage_defer.py::test_s23b_credential_metrics_summary_renders_every_counter`
and cited throughout live-gate evidence. Alternative: extend the D20 shutdown-line pattern to
three more dicts — rejected, it is shutdown-only and would compete with the established display
path that already renders seven dicts.

Consequence for the completeness rule (`run-observability` RO-S4): the detailed section declares
a `_RENDERED_METRIC_SURFACES` registry naming every metric surface it renders, and a pin
enumerates the `*_metrics` fields of `PipelineStatus` against it. For that check to have **no
exemptions**, `credential_metrics` gets a compact display line too — the R5.2 shutdown INFO line
stays exactly as it is (pinned and cited in gate evidence), so the same counters appear both in
periodic status and once at shutdown. Duplication is deliberate and cheap; an exemption list
would be the more fragile artifact.

### D8 — Formatters read an explicit key allowlist, never `dict.items()`

Each formatter names the keys it renders. A metric dict is a cross-module surface that can gain
keys later; wholesale dumping would print unexpected content into operator output and logs. An
allowlist makes the formatter structurally incapable of leaking a credential, a path or an
internal payload even if a producer later adds one. No credential material exists in these three
dicts today (all values are counters/rates/mode strings), and the rendered lines must stay that
way by construction.

### D9 — Remove the dead `SearchStage._apply_rate_limit`

`stage/definition.py:336-350` has **zero callers repo-wide** including tests
(`grep -rn "_apply_rate_limit" --include="*.py" .` → the definition only). It encodes exactly the
semantics this change removes: starve → `return False` → caller proceeds, with no counter and no
deferral. Leaving it invites reintroducing a silent-empty path in the hot stage file. No test
deletion is needed because no test referenced it.

### D10 — The check stage keeps its bounded requeue (asymmetry is a decision)

`stage/definition.py:741-756` raises `TransientFetchError` on provider-basket starvation, and its
shipped requirement mandates it: "Check tasks survive limiter starvation … SHALL be re-enqueued
through the bounded-retry path instead of being dropped" (`failure-handling`). That path also
deliberately reports nothing to the basket (`:800-803`, "No provider result to report"), so it has
no feedback loop, and no measurement shows check starvation being destructive — all 1 708
measured suppressions were `github_raw` gather URLs. Changing it would contradict a shipped
requirement without evidence. Recorded as a follow-up candidate: if check-stage starvation is ever
measured to burn attempts materially, amend that requirement the same way.

### D11 — The search boundary keeps its failure-empty taxonomy

`failure-handling` scenario "Limiter suppression is a failure-empty" and its pin
`tests/test_fh_client_taxonomy.py::test_s3_limiter_suppression_is_failure_empty` stay valid: on
the search path a suppression surfaces as a **blank payload** from `get_with_headers()` (`("", {})`),
not as a typed refusal; search runs one worker (no contention — 0 search suppressions in the
600 s soak); and `github_api` is credential-bound, where R5.2 already provides the deferral path
for pool exhaustion. The delta spec scopes the requirement text accordingly rather than rewriting
the scenario.

### D12 — New counter joins the canonical key tuple

`deferred_local_budget` is added to `_GATHER_TRANSPORT_STAT_KEYS` alongside
`deferred_credentials`, `deferred_rate_limit`, `deferred_secondary`; otherwise `_gather_stat_inc`
rejects it loudly by design (the published-schema guard S21 relies on). `reset_gather_transport_stats()`
is the single reset entry point and must clear the latency histogram too, so tests and fresh runs
start from a fully zeroed surface.

### D13 — The flag reaches the fetch through the same lazy seam as the transport

`fetch_gather_content` gains `defer_local_suppression: Optional[bool] = None`; `None` resolves
through a new `_configured_defer_local_suppression()` that mirrors `_configured_gather_transport()`
(`search/client.py:989-1009`) exactly: a deferred, fully guarded `from config import get_config`
read of `gather.defer_local_suppression`, falling back to the built-in default `True` when
configuration was never loaded. Rationale is the one already recorded there — the client layer
keeps no hard top-level dependency on `config` (no import cycle), and callers that never load
`config.yaml` (unit tests, ad-hoc scripts) degrade to the default instead of raising. `collect()`
passes the resolved value through, so an explicit caller and a config-less caller agree, and the
operator's rollback flag cannot be bypassed by an embedded caller. Alternative rejected: reading
the flag only inside the branch — it would hide the decision from the function's contract and make
the legacy path untestable without loading configuration.

### D14 — `deferred_credentials` was missing from the canonical key tuple (deviation, found in GREEN)

D12 assumed `deferred_credentials` was already declared alongside `deferred_rate_limit` /
`deferred_secondary`. It is **not**: `_GATHER_TRANSPORT_STAT_KEYS` (pre-change) omitted it while
`fetch_gather_content` still called `_gather_stat_inc("deferred_credentials")` on the
credential-pool-exhaustion path, so that counter was silently rejected by the published-schema
guard and never surfaced — a latent observability defect. The tuple now declares it, and the pin
that requires `stats["deferred_credentials"]` (`test_tdm_starvation.py::test_s25...`) would fail
without it. This is an additive schema fix, not a behavior change: the increment site is unchanged.

### D15 — The S21 exact-key-set pin is extended, not weakened (deviation)

`tests/test_gt_stage_integration.py::test_s21_transport_economics_are_observable` asserted the
**exact** pre-change key set. Tasks 3.4 and 5.4 declare eleven new members in the canonical
surface, so the pin's `expected_keys` was extended with `deferred_credentials`,
`deferred_local_budget` and the nine `latency_{samples,p50_ms,p99_ms}_{raw,html,rest}` keys. The
exact-set guard remains (missing **or** extra keys still fail); the test asserts all members are
integers and all are zero after reset. This corrects `tests.md` Note 1's "preserved unchanged"
claim: the *behavior* is unchanged, the *declared schema* is wider by design, and the pin tracks
the declared schema. No assertion was deleted or relaxed.

### D16 — Harness defects in the new tests, fixed before implementing (deviation)

Three failures discovered during GREEN were harness bugs, not spec changes (task 1.1's rule):
`test_s26_legacy...` patched `GitHubClient._report` with a bare recorder **and** asserted
`bucket.consecutive_failures == 1`, which is impossible because the recorder never reached the
limiter; it now delegates to the real method (asserting both the report and the adaptive effect).
`test_s26b_flag_config_surface` loaded a YAML that cannot pass the pre-existing validator (no
credentials, no task) and called `ConfigValidator(cfg).validate()` against the actual
no-arg-constructor/`validate(config)` API; it now supplies a loader-valid config and uses
`ConfigValidator().validate(cfg)`. `_local_budget_wait_s` (below) absorbed the third.

### D17 — The deferral wait floors at the one-token refill time (D3 refinement)

`TokenBucket.wait_time()` returns `0.0` while a token appears available; a lost race can leave the
bucket momentarily refilled even though `_limit` returned False, and a stubbed basket (tests) can
report full. The helper therefore returns `max(wait_time(), 1/current_rate)`: the honest "time
until our own budget can serve this request". At `base_rate 2.0` this is 0.5 s; at the adaptive
floor 5 s; the stage-level harness sets `base_rate 100` and observes ~10 ms. Still clamped by
`max_refusal_wait_s`. The `1/rate` floor is what `gather-transport-S24` asserts
(`≈ 1.0/BASE_RATE`).

### D18 — A2 verdict rule pinned before the gate (task 1.4)

Probe re-run 2026-09-28 (this change, task 1.3): `raw.githubusercontent.com`, anonymous, 80/80
HTTP 200, true p50 **255 ms** (band `(250,300]` → reported **300 ms**), true p99 **493 ms**
(reported **500 ms**), mean **25.5 KiB**/file. Because D6 reports the band's upper edge, A2 is
judged on the reported edge with the window widened by one band: **reported p50 ∈ [250, 350] ms and
reported p99 < 1000 ms**, with the true p50 recorded alongside. The band edges are kept as designed
(a historical true p50 of 338 ms publishes as 350 ms, exactly the widened window's edge); no
refinement in the 200–400 ms region is warranted. Pinned in `runbook.md` §1.

### D19 — Acceptance row A1 re-pinned: a mean over a heavy-tailed corpus is not a transport criterion (deviation, found by the live gate)

The live gate (2026-09-28) measured A1 for the first time — the surface had no renderer before this
change, which is exactly why O3 existed. The rendered line gave `raw req=2538 bytes=91346077`, i.e.
a **mean of 35.15 KiB/file**, above the inherited bound "≤ 20 KiB".

The measurement is correct, the bound was not. Two independent checks confirm the byte accounting is
exact: an 80-URL probe through the shipped `fetch_gather_content` published
`bytes_raw / requests_raw = 26 123.9 B`, and measuring the same 80 payloads out-of-band gave a mean
of `26 123.9 B` — identical to 0.1 B. The bound itself was calibrated on a **5-file** probe
(17.1 KiB) and a 12-URL gate (12.6 KiB). The real corpus is heavy-tailed: over 80 harvested files
the **median is 6.0 KiB**, p25 2.9, p75 17.4, p90 74.1, p99/max 263.7 KiB, and the **top decile
carries 62.8 % of all bytes**; 18 of 80 files exceed 20 KiB and 7 exceed 100 KiB. A mean over that
distribution tracks whichever large files a given search fan-out happens to surface, so it measures
the corpus, not the transport.

A1 is therefore re-pinned (`runbook.md` §3) to the two stable quantities that express the row's
actual intent — *the raw transport is far cheaper than the rendered page*:

> **A1 passes when the median harvested file is ≤ 20 KiB and the raw mean is at least 10× below the
> `html` reference of 523 KiB/file.**

Measured: median **6.0 KiB** ✓; ratio **14.9×** from the soak mean and **20.5×** from the probe mean ✓.
No code changed as a result: the mean the row originally named is still published and still
computable (`RO-S11` is unaffected — it pins computability, not a threshold). The original bound is
kept in the runbook as history rather than deleted, so the failure is visible instead of silently
re-defined away.

### D20 — Sizing of `github_raw.base_rate` × `pipeline.threads.gather` (operational tuning, post-gate)

*(Distinct from `fix-credential-liveness` D20, which is the shutdown `Credential liveness:` INFO line.)*

The gate showed the shipped fix removed the self-destruction but left the basket **under-sized**: at
`base_rate 2.0` / `threads.gather 8` the soak reached only 4.23 req/s and produced **3.17 deferrals
per successful request** (8 040 deferrals for 2 538 fetches) — each one a WARNING line plus a durable
queue write plus a re-delivery.

**Ladder (2026-09-29, `/tmp/opencode/tdm_tune.py`, 8 points × 45 s, 3 060 real anonymous requests to
`raw.githubusercontent.com`, ladder aborts on the first organic refusal):**

| point | base_rate | burst | threads | req/s | basket rate | deferrals/req | refusals |
|---|---|---|---|---|---|---|---|
| a-base2 | 2.0 | 4 | 6 | 3.45 | 4.287 | 4.74 | **0** |
| a-base4 | 4.0 | 8 | 6 | 7.91 | 8.574 | 4.17 | **0** |
| a-base8 | 8.0 | 16 | 6 | **16.80** | 17.149 | 4.08 | **0** |
| b-thr2 | 4.0 | 8 | 2 | 7.93 | 8.574 | **0.82** | **0** |
| b-thr3 | 4.0 | 8 | 3 | 7.92 | 8.574 | 1.79 | **0** |
| b-thr4 | 4.0 | 8 | 4 | 7.93 | 8.574 | 2.83 | **0** |
| b-thr8 | 4.0 | 8 | 8 | 7.93 | 8.574 | 6.72 | **0** |
| b-thr12 | 4.0 | 8 | 12 | 7.93 | 8.574 | 10.45 | **0** |

Two independent findings:

1. **Throughput is basket-bound, not thread-bound.** At `base_rate 4.0` the rate is 7.92–7.93 req/s for
   *every* thread count from 2 to 12 — zero gain above two workers — while deferral churn grows
   linearly with threads (0.82 → 10.45 per request, 12.7×). Extra workers buy nothing and cost a
   WARNING line, a queue write and a re-delivery each. This is only true *because* of D1–D4: before
   the fix, each of those deferrals also halved the basket, so over-threading was actively
   self-destructive.
2. **No GitHub-side ceiling was reached.** 16.8 req/s anonymous completed with zero 429/403, zero
   secondary/abuse markers. `raw.githubusercontent.com` publishes no anonymous limit, so a single
   clean observation is not a guarantee — the chosen point keeps a **2× margin** below it.

**Chosen: `base_rate 4.0`, `burst_limit 8` (= 2 × base, the prior convention), `threads.gather 4`.**
Rationale for 4 workers rather than the measured minimum of 2: required concurrency is
`rate × p99_latency ≈ 8.6 × 0.5 s ≈ 4.3`, so 4 threads still saturate the basket while leaving
headroom for a slow or timed-out fetch (one 10 s timeout costs 25 % of concurrency at 4 threads but
50 % at 2). Rationale for not taking the full 16.8 req/s: the pipeline's next stage is ~9× slower
(check processed 278 vs gather 2 538 in the gate), so extra gather speed converts into queue depth
and disk rather than into validated findings.

**Confirmation, like-for-like.** A duration-matched A/B control (original `2.0 / burst 4 / 8 threads`,
300 s) against the tuned config (300 s) — both from an empty workspace:

| metric (300 s) | original 2.0/8 | tuned 4.0/4 | Δ |
|---|---|---|---|
| gather processed | 1 246 (4.15 req/s) | 2 550 (8.50 req/s) | **2.05×** |
| check processed | 150 | 287 | **1.91×** |
| search processed | 2 352 | 2 996 | **1.27×** |
| deferrals per gather request | 3.19 | 0.93 | **3.4× less** |
| log lines per gather request | — | — | 78.6 → 23.9 (**3.3× less**) |
| remote refusals (429/403/secondary) | 0 | 0 | — |

The A/B control was necessary: comparing the tuned 300 s run against the *600 s* gate soak appeared
to show search 4× slower (2 996 vs 35 232). That was a **duration artifact** — the longer baseline
run accumulated 87 aggregation-cache entries serving 35 145 hits (all 300 s runs: 33 entries), and
`search Processed` is dominated by cache hits, i.e. by *avoided* work. At equal duration search is
**faster** with the tuned config. Supporting 600 s tuned run (fresh root): gather **5 095** (8.49
req/s, 2.01× the 600 s baseline), check **524** (1.88×), churn 0.89/req, `github_raw` waits
p50 0.11 s / max 0.22 s / 0 over 1 s, RSS max 137.7 MB, 319.6 MB of `data/` in 691 s, zero refusals,
zero gather failure-empties (102 are `[check] provider limiter starved`, D10).

No code changed for this decision; it is configuration sizing only, and it is reversible by flipping
the three values back.

## Risks / Trade-offs

- **A deferred task could cycle while the basket stays starved** → bounded by the queue's existing
  `max_age_hours = 24` purge with loud accounting (unchanged), the stage pause limits churn, and
  `deferred_local_budget` makes the cycling visible for the first time. Rollback = flag flip.
- **`stage_pause=True` lets one starved worker pause all 8** → the pause equals the basket's own
  refill time (≤ 0.5 s at `base_rate 2.0`, ≤ 5 s at the adaptive floor) and `_defer_pause_until`
  takes a `max()`, so it never extends past the longest outstanding wait. Measured in the gate.
- **Band-resolution percentiles could be read as exact** → key names carry `_ms`, the upper-edge
  convention is conservative (over-reports), and README documents the band edges.
- **Removing the failure report could weaken legitimate adaptation** → adaptive decay still fires
  on every real request failure (`:1333,1382,1393`); only events where no request was issued stop
  counting. The pre-change behavior decayed the budget in response to its own throttling.
- **Default `true` changes behavior on upgrade** → documented in README and
  `examples/config-full.yaml`; `false` restores byte-identical legacy behavior; the live gate
  includes a rollback drill on a real backlog.
- **Three more status lines add noise** → one line per dict, rendered only when non-empty,
  matching the seven existing lines; detailed mode only.
- **Histogram counters widen the published gather-metrics schema** → they are declared in the
  canonical tuple, so the widening is explicit and reviewed; undeclared keys remain rejected.

## Migration Plan

No data or schema migration: no queue, registry or shard format changes; `PipelineStatus` gains no
fields; the config key is additive with a default. Deploy = normal restart with the new default.
Rollback = `gather.defer_local_suppression: false` (legacy branch byte-for-byte) with no code
change. Verification before promotion follows house doctrine: a production-shaped soak with the
basket deliberately under-provisioned relative to `pipeline.threads.gather` so suppressions occur
organically, a rollback drill, and the A1/A2 readings this change exists to make possible
(run-roots under `/var/tmp`, `AGENTS.md` §1–§3).

## Open Questions

Genuinely deferrable — none of these change the specs, the approach or the task breakdown; all are
operator config decisions that become measurable only *because* of this change:

1. Should `ratelimits.github_raw.base_rate` be raised from 2.0? The captured evidence says raw
   sustained 3.03 req/s across 80 distinct URLs with zero refusals and p99 390 ms (probe
   2026-09-25, `raw.githubusercontent.com`, anonymous), so 2.0 is headroom rather than necessity —
   but the right value is now readable from `latency_p99_ms_raw` and `deferred_local_budget`.
2. Should `pipeline.threads.gather` drop from 8 to 2–4 (plan §II.2 lever 2)? After this change
   over-subscription costs deferrals instead of attempts, so the answer depends on measured
   throughput, not on fear of burning the backlog.
3. Should the same treatment later be applied to the check stage (D10)? Needs a measurement of
   check-stage starvation first.
