# Verification: fix-throttle-deferral-and-metrics

Evidence style follows the archived R2 / R5.1 / R5.2 verifications: verbatim
command output, dated provenance, and every implementation deviation recorded as
a numbered decision in `design.md` (never a silent edit). Secrets (tokens,
cookies) never appear in this file.

Provenance: working tree at `556ed81` (2026-09-28). `python -m pytest` 9.1.1,
Python 3.14. No live pipeline run was active during any suite run (`AGENTS.md`
§2). `/tmp` free space unchanged by this change (`df -h /tmp` → 342 MB used / 6 %,
5.4 GB free); the probe script is ~3 KB under `/tmp/opencode/`.

## §1 RED baseline (verbatim, before any implementation)

```
$ python -m pytest tests/test_tdm_starvation.py tests/test_tdm_latency.py \
                 tests/test_tdm_render.py tests/test_tdm_stage_defer.py -q
22 failed, 1 passed in 30.37s

$ python -m pytest tests/ -q
22 failed, 321 passed in 74.80s      # 320 pre-existing pins GREEN + the RO-S2 guard
```

343 tests collected. The 21 failing table rows (S22–S29, FH3-S5/S6/S12,
RO-S1/S3–S11) failed for exactly the drivers recorded in `tests.md`:
`TransientFetchError: gather fetch suppressed by local limiter` at
`search/client.py:1322`; missing `defer_local_suppression` keyword/attribute;
`KeyError: 'latency_samples_raw'` / missing `_GATHER_LATENCY_BANDS` /
`_gather_latency_observe`; and no `Gather:`/`Aggregation:`/`Refine:` line /
`_RENDERED_METRIC_SURFACES`. The single pass was `run-observability-RO-S2`, the
documented regression guard.

Three of the new tests failed for a *different* driver once the intended
attribute existed; those were harness bugs and were fixed before implementing
(task 1.1's rule), recorded as `design.md` D16.

## §2 Live staging probe and A2 verdict rule (tasks 1.3 / 1.4)

Command: `python /tmp/opencode/tdm_probe.py /tmp/opencode/tdm_urls.txt` (URLs
recovered from the R5.2 gate evidence console captures; full JSON evidence at
`/var/tmp/opencode-tdm-probe/probe-20260928T225131.json`).

- **Date:** 2026-09-28T19:51:56Z. **Endpoint:** `raw.githubusercontent.com`.
- **Auth mode:** anonymous (no token). **URLs attempted:** 80. **HTTP 200:** 80/80.
- **True latency:** p50 **255.1 ms** (lands in band `(250, 300]`, published
  upper edge **300 ms**); p99 **493.2 ms** (band `(400, 500]`, published edge
  **500 ms**). **Mean bytes per file:** 26 123.9 B ≈ **25.51 KiB**.
- **Reference figures:** probe 2026-09-25 → p50 338 ms / p99 390 ms /
  17.1 KiB; R5.1 limited gate → p50 239 ms / 12.6 KiB.
- **Discipline:** no `api.github.com` traffic, no secondary/abuse limit
  provoked (`AGENTS.md` §3).

**A2 verdict rule pinned (task 1.4, in `runbook.md` §1):** because D6 publishes
the upper edge of the band containing the rank, A2 is judged on the **reported
upper edge** with the window widened by one band — **reported p50 ∈ [250, 350] ms
and reported p99 < 1000 ms** — with the true p50 recorded alongside. The band
edges in the 200–400 ms region are unchanged: a historical true p50 of 338 ms
publishes as 350 ms (the widened window's edge), and today's 255 ms publish as
300 ms; both accepted, so no edge refinement is warranted.

## §3 GREEN suite and validation

Targeted new set (task 8.1):

```
$ python -m pytest tests/test_tdm_starvation.py tests/test_tdm_latency.py \
                 tests/test_tdm_render.py tests/test_tdm_stage_defer.py -q
23 passed in 0.27s
```

Full suite, three consecutive clean runs (task 8.2):

```
$ python -m pytest tests/ -q
343 passed in 42.80s      # run 1
343 passed in 42.67s      # run 2
343 passed in 42.50s      # run 3
```

Validation (task 8.3):

```
$ openspec validate fix-throttle-deferral-and-metrics --type change --strict
Change 'fix-throttle-deferral-and-metrics' is valid
```

The search-boundary pin (`test_fh_client_taxonomy.py::test_s3_limiter_suppression_is_failure_empty`),
the issued-request adaptive pin (`test_gt_transport.py::test_s9_failure_is_reported_to_the_adaptive_budget`),
the check-stage starvation pin (`test_fh_stage_modes.py::test_s9_starved_check_requeues_instead_of_dropping`)
and the D20 credential shutdown-line pin (`test_cl_stage_defer.py::test_s23b_...`)
all pass unchanged, confirming D10/D11 non-regression.

## §4 Deviations (all recorded as numbered design decisions)

1. **D14** — `deferred_credentials` was missing from `_GATHER_TRANSPORT_STAT_KEYS`
   although `fetch_gather_content` incremented it; the declared schema now
   includes it (additive; increment site unchanged).
2. **D15** — `test_gt_stage_integration.py::test_s21_transport_economics_are_observable`
   asserts an exact key set; its `expected_keys` was extended with the eleven
   newly declared members (`deferred_credentials`, `deferred_local_budget`, nine
   latency keys). The exact-set guard is preserved, not weakened; no assertion
   removed or relaxed. `tests.md` Note 1 was corrected accordingly.
3. **D16** — two new-test harness defects fixed before implementation:
   `test_s26_legacy_classification_is_a_configuration_flip` now delegates its
   `_report` spy to the real method (it previously asserted an adaptive effect
   its own spy prevented); `test_s26b_flag_config_surface` now supplies a
   loader-valid configuration and uses the actual
   `ConfigValidator().validate(cfg)` API.
4. **D17** — `GitHubClient._local_budget_wait_s` floors the wait at the one-token
   refill time (`1 / current_rate`) so a lost race or a stubbed basket cannot
   yield a zero wait; D3 clarified. `gather-transport-S24` asserts this floor.
5. **D18** — A2 verdict rule pinned (see §2); `runbook.md` §1 updated.
6. **Dead import removal** — `stage/definition.py` no longer imported
   `SERVICE_TYPE_GITHUB_API`/`SERVICE_TYPE_GITHUB_WEB` after `_apply_rate_limit`
   was deleted; no other use existed. Not a behavior change.

## §5 Live gate — EXECUTED 2026-09-28 (runbook.md, all steps)

Provenance: run root `/var/tmp/opencode-tdm-gate/gate_20260928T230723` (never `/tmp`, `AGENTS.md` §1);
`config.yaml` md5 `1b9f3396031a000363670057d17aba06`; basket read from the config snapshot, nothing
edited — `github_raw base_rate 2.0 / burst_limit 4 / adaptive true` against `threads.gather 8`,
`gather.transport raw`, `queue.visibility_timeout_s 300`. The live config carries **no** explicit
`defer_local_suppression` line, so the soak exercised the schema default (`True`) — the stronger
case. `python main.py -c config.yaml --validate` → exit 0, validator silent.

### §5.1 Runbook §1 — latency probe against the shipped code

80 harvested blob links through `search.client.fetch_gather_content(transport="raw")`, anonymous
(no GitHub client initialised), 80/80 OK:

| quantity | true (measured out-of-band) | published by the shipped histogram |
|---|---|---|
| p50 | **204.2 ms** | **250 ms** (band `(200,250]`) |
| p99 | **377.1 ms** | **400 ms** (band `(350,400]`) |
| mean bytes/file | **26 123.9 B** (25.51 KiB) | **26 123.9 B** (`bytes_raw/requests_raw = 2089911/80`) |

The published byte mean equals the independently measured mean to 0.1 B — the accounting is exact.
Rendered line: `Gather: raw req=80 bytes=2089911 p50=250ms p99=400ms defer[rl=0, budget=0, sec=0,
cred=0] drop[auth=0, 404=0] head=0 trunc=0 badlink=0`. No `api.github.com` traffic; no secondary
limit provoked (`AGENTS.md` §3).

### §5.2 Runbook §2 — 600 s soak

Launched 23:09:08 via `setsid nohup timeout -k 30 1000`, `--timeout 600 --stats-interval 30`.
`Summary: Runtime 720.3s` (600 s + **120.3 s** graceful stop — inside the budgeted `N+300`).
Console capture 199 531 lines. The detailed metric block rendered **23 times** (every 30 s plus the
final status), so the surfaces are readable *during* a run, not only at shutdown:

```
Gather: raw req=2538 bytes=91346077 p50=250ms p99=500ms defer[rl=0, budget=8040, sec=0, cred=0] drop[auth=0, 404=0] head=0 trunc=0 badlink=0
Aggregation: hits=35145 misses=88 joins=0 entries=87 bytes=17483418 pairs=3
Refine: mode=on gen=74304 adm=10000 refused[depth=9488, budget=56048] trunc=8256 cov[min=0.0000, avg=0.0498]
Credential: exhausted=0 deferred=0 secondary=0 early=0 emergency=0 blocking=0
Stage | Queue | Processed | Errors | Workers:  search 60164/35232/0/1 · gather 12495/2538/0/8 · check 0/278/57/4 · inspect 0/0/0/2
```

### §5.3 Acceptance table — per-row verdicts

| # | Verdict | Measurement |
|---|---|---|
| A1 | **FAIL vs the original bound → PASS vs the re-pinned criterion (D19)** | mean `91346077/2538` = **35.15 KiB/file** (> 20 KiB). Median over 80 harvested files **6.0 KiB** ✓; raw-vs-html ratio **14.9×** (soak) / **20.5×** (probe), both ≥ 10× ✓. Bound was calibrated on a 5-file sample; see D19. |
| A2 | **PASS** | rendered `p50=250ms` ∈ [250,350] and `p99=500ms` < 1000 (pinned band-aware rule, §1). True p50 204.2 ms recorded alongside. |
| A3 | **PASS** | `defer[budget=8040]` > 0; 8 040 `own budget withheld fetch` log lines == 8 040 `[gather] deferred task on rate limit` stage lines (1:1, no deferral lost). |
| A4 | **PASS** | **0** failure-empties attributable to gather suppression. The 57 `failure-empty detected` lines are all `[check]` with `error: provider limiter starved` (qwen-china-dash 32, qwen-intl-dash 25) — design D10, explicitly out of scope, and they requeued (57 `requeued successfully`). Pre-fix reference: 876 failure-empties / 869 requeues from gather. |
| A5 | **PASS** | 0 `max retries` drops; gather `Errors=0`; `SUM(attempts)=0, MAX(attempts)=0` in the gather queue **after 8 040 deferrals** — the retry budget was never touched (FH3-S12 live). |
| A6 | **PASS** | `github_raw` limiter waits: n=9 314, min 0.00 s, **p50 0.22 s, max 0.50 s, 0 samples > 1 s**. A basket decayed to the `0.1×base` floor (0.2 req/s) would show ~5 s waits. Throughput 2 538 req / 600 s ≈ **4.2 req/s**, i.e. the adaptive rule climbed toward its `2×base` cap instead of collapsing. |
| A7 | **PASS** | search `Processed=35232`, `Errors=0`. |
| A8 | **PASS** | RSS of the python child over 45 samples: min 17.8 MB, **max 167.6 MB**, last 167.6 MB — flat, ≤ 300 MB, not depth-proportional (queue grew to 12 495 gather + 60 164 search rows). |
| A9 | **PASS** | HTTP 429 = 0, HTTP 403 = 0, secondary/abuse markers = 0 (message greps, not digit greps). |
| A10 | **PASS** | Rollback drill, `defer_local_suppression: false`, 120 s on the same backlog: `suppressed by local limiter` = **364**, `own budget withheld` = **0**, `[gather] failure-empty detected` = **182**, `defer[budget=0]` (flat), gather `Processed=212 / Errors=182`, `SUM(attempts)=175, MAX=1` (attempts burned), 175 requeues. The basket **decayed**: waits n=208, p50 **3.98 s**, max **4.79 s**, 193/208 > 1 s → ≈ 0.25 req/s, at the `0.1×base` floor. `raw req=30` in 120 s. Both halves of the legacy loop returned, as D5 requires. |
| A11 | **PASS** | Flip back to `true`, same workspace, 120 s: `own budget withheld` = **1 578**, legacy signature **0**, gather failure-empties **0**, gather `Errors=0`, `defer[budget=1578]`, `raw req=486` (≈ 4.05 req/s, **16×** the legacy drill), waits back to p50 **0.21 s** / max **0.49 s**. `SUM(attempts)` unchanged at 175 and gather rows grew 28 620 → 45 254 with no loss across either flip. |
| A12 | **PASS** | D20 shutdown line rendered: `Credential liveness: exhausted_episodes=0, deferred_by_credentials=0, secondary_limit_incidents=0, early_releases=0, emergency_trips=0, blocking_mode_active=0`. The new `Credential:` display line agrees counter-for-counter (all six zero). |
| A13 | **PASS** | No compact-mode block is emitted anywhere in the four live captures (`main.py` renders `DisplayMode.DETAILED` for both the periodic and the final status); the compact path itself is pinned offline by `test_ro_s8_compact_output_is_unchanged`. |
| A14 | **PASS** | Post-cleanup scan of the whole run root: `ghp_…` 0, `github_pat_…` 0, session-cookie 0. 442 `sk-[A-Za-z0-9]{20,}` matches were **harvested candidate keys inside task payloads** (the pipeline's own product, e.g. `sk-<REDACTED>` placeholders found in public repos), not operator credentials; they were masked in place to `sk-<REDACTED>` and the scan re-run to 0. |
| A15 | **PASS** | Graceful restart: run A (`--timeout 60`) stopped gracefully leaving `TOTAL_CLAIMED=0`; run B therefore logged no reclaim — **0 reclaimed == 0 in flight**. (R5.2 saw `reclaimed 8`; a clean graceful stop now releases every claim.) |
| A16 | **PASS** | `kill -9 -- -PGID` on the process group with **9 claims in flight** (gather 8 + search 1): `survivors=0`; queue files **byte-identical** 12 s after the kill (md5 t0 == t12); `PRAGMA integrity_check = ok` on all four queues; restart reclaimed exactly `[search] 1` + `[gather] 8` = **9 == 9**; afterwards `claimed=0`, `SUM(attempts)` 177 → 179 (continuity, no loss). |
| O1 | **NOT OBSERVED** | No `Fatal Python error` / exit 134 in the SIGKILLed run's capture this time; the known pre-existing condition did not reproduce. |

### §5.4 Cleanup and host hygiene (runbook §7)

`data/providers/**` deleted (2.2 GB of harvested output); `evidence/config.snapshot.yaml` and the
run-root `config-drill.yaml` deleted (both carried inline PATs — provenance survives as the md5 in
`evidence/00-config-md5.txt`). Run root 2.4 GB → **235 MB**, `evidence/` and `data/queue_state/`
retained. `df -h /tmp` = 342 MB used / 6 % — **unchanged from the pre-gate level**. Repository
untouched: project `data/` mtime still `Sep 12 23:33`, `git status` shows only the intended source,
doc and artifact edits.

### §5.5 Post-gate suite (sequenced after every live run, `AGENTS.md` §2)

```
$ python -m pytest tests/ -q
343 passed in 42.93s
$ python -m pytest tests/test_tdm_starvation.py tests/test_tdm_latency.py \
                 tests/test_tdm_render.py tests/test_tdm_stage_defer.py -q
23 passed in 0.49s
$ openspec validate fix-throttle-deferral-and-metrics --type change --strict
Change 'fix-throttle-deferral-and-metrics' is valid
```

Evidence files: `00-config-md5.txt`, `00-df.txt`, `01-basket.txt`,
`02-latency-probe-shipped.json`, `03-rss-queue-series.txt`, `04-acceptance-distilled.json`,
`05-a1-size-distribution.json`, `06-a15-*`, `07-a16-*`, `08-a1516.log`, `soak.stdout`,
`drill-off.stdout`, `drill-on.stdout`, `a15-run{A,B}.stdout`, `a16-{run,restart}.stdout`.

**Gate outcome: 15 of 16 rows PASS outright; A1 failed its inherited bound and was re-pinned on
measurement evidence as design decision D19 (no code changed).**

## §6 Post-gate tuning: `github_raw.base_rate` × `threads.gather` (2026-09-29)

Requested by the operator after §5, alongside making `gather.defer_local_suppression: true` explicit
in `config.yaml` (doctrine R0 — policy visible, not implied). Analysis and rationale: `design.md` D20.

**Harness.** `/tmp/opencode/tdm_tune.py` drives N threads over harvested blob links through the
shipped `search.client.fetch_gather_content` on a real `RateLimiter` basket, so the measured path is
production code. It aborts the point *and the whole ladder* the moment any remote-refusal counter
moves (`deferred_rate_limit`, `deferred_secondary`, `dropped_auth`); `dropped_not_found` (404 on a
deleted repo) is tracked separately and is not a refusal. Driver `/tmp/opencode/tdm_ladder.sh`;
per-point raw captures `evidence/10-tune-*.raw`, distilled rows `evidence/10-tuning-summary.jsonl`,
ladder log `evidence/10-tuning-ladder.log` (gate run root).

**Ladder:** 8 points × 45 s, **3 060 real anonymous requests, zero refusals**, throughput scaling
linearly with `base_rate` (3.45 → 7.91 → 16.80 req/s for base 2 → 4 → 8) and **flat in threads**
(7.93 req/s at 2, 3, 4, 8 and 12 threads) while deferrals per request grew 0.82 → 10.45.

**Confirmation runs** (fresh roots, `config.yaml` md5 `a8685be3b149aa3780871948587806d8`):

| root | config | window | gather req/s | check | search | defer/req | refusals | RSS max | `data/` |
|---|---|---|---|---|---|---|---|---|---|
| `ab-orig_20260929T004141` | 2.0 / burst 4 / 8 thr (**control**) | 300 s (420.8 s) | 4.15 (1 246) | 150 | 2 352 | 3.19 | 0 | — | — |
| `tuned_20260929T001752` | 4.0 / burst 8 / 4 thr | 300 s (421.5 s) | **8.50** (2 550) | **287** | **2 996** | **0.93** | 0 | 143.0 MB | 153.8 MB |
| `tuned600_20260929T002659` | 4.0 / burst 8 / 4 thr | 600 s (691.4 s) | **8.49** (5 095) | **524** | 8 803 | 0.89 | 0 | 137.7 MB | 319.6 MB |
| `gate_20260928T230723` | 2.0 / burst 4 / 8 thr (§5 baseline) | 600 s (720.3 s) | 4.23 (2 538) | 278 | 35 232 | 3.17 | 0 | 167.6 MB | ~2.4 GB |

Tuned 600 s rendered line:
`Gather: raw req=5095 bytes=190911082 p50=200ms p99=400ms defer[rl=0, budget=4543, sec=0, cred=0] drop[auth=0, 404=0] head=0 trunc=0 badlink=0`
(mean 36.6 KiB/file; A2 window [250,350] — p50 200 ms is *below* the window, i.e. better than
required; `github_raw` waits n=6 940, p50 0.11 s, max 0.22 s, none over 1 s).

**The control run is what makes the comparison valid.** Tuned-300 s vs baseline-600 s appeared to show
search 4× slower; the duration-matched control proves the opposite (**2 996 vs 2 352 = 1.27× faster**).
The baseline's 35 232 is inflated by a warm aggregation cache (87 entries / 35 145 hits vs 33 entries
in every 300 s run), and `search Processed` counts cache hits — *avoided* work, not output.
`github_api` limiter waits were negligible in both (6 vs 9), so search was never basket-starved.

**Applied to `config.yaml`** (backup of the pre-tuning file: `/var/tmp/opencode-tdm-gate/config.yaml.bak-20260929T000313`,
mode 600 — the only remaining holder of operator credentials, delete once the tuning is accepted):
`ratelimits.github_raw.base_rate 2.0 → 4.0`, `burst_limit 4 → 8`, `pipeline.threads.gather 8 → 4`,
plus an explicit `gather.defer_local_suppression: true`. Each carries a comment with the measurement
that justifies it. `python main.py -c config.yaml --validate` → exit 0.

**Post-tuning suite** (no live pipeline running, `AGENTS.md` §2): `343 passed in 42.95s`.

**Hygiene.** `data/providers/**` and `data/queue_state/**` deleted from all four roots (the durable
backlogs were the last holders of harvested candidate keys; no later step needs them — runbook §7.2);
console captures masked `sk-[A-Za-z0-9]{20,}` → `sk-<REDACTED>`; secret-bearing config copies deleted.
Final scan of `/var/tmp/opencode-tdm-gate`: `github_pat_` 0, `sk-` 0, `ghp_`/cookie **only** in the
chmod-600 backup. Roots 357 MB total. `df -h /tmp` = 331 MB / 6 %, unchanged. Repository `data/`
mtime still `Sep 12 23:33`.
