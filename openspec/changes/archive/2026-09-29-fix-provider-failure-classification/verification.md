# Verification: fix-provider-failure-classification

Schema `tdd-flow`. Groups 1–11 were executed and are recorded here. Group 12
(handoff: `plan.md`, sync-specs, archive, commit) is left UNCHECKED because
archiving and committing need the operator's explicit go-ahead.

---

## §1 — RED baseline

**1.1 — new-file RED baseline (verbatim), captured before any implementation:**

```
$ python -m pytest tests/test_pfc_signals.py tests/test_pfc_defer.py tests/test_pfc_config.py \
    tests/test_pfc_render.py tests/test_pfc_check_starve.py -q --continue-on-collection-errors
23 failed, 4 passed, 2 errors in 5.08s
```

Matched the task's predicted **23 failed, 4 passed, 2 errors** (29 collected)
exactly. The four passes were the intended pins on unchanged behavior
(`test_s3_forbidden_without_marker_stays_an_auth_failure`,
`test_s4_rate_limit_without_published_wait_keeps_legacy_transient`,
`test_s8_fail_open_wrapper_lets_the_signal_through`,
`test_s11_empty_or_absent_surface_renders_nothing[surface1]`). Drivers matched
the task's list (missing `tools.http_signals`, `RateLimitDeferral(reason=...)`,
missing `_provider_refusal_stat_inc`, missing `PipelineStatus.provider_refusal_metrics`,
`DID NOT RAISE RateLimitDeferral`, `retry.py ... Last error: Rate limit exceeded (HTTP 429)`).

**1.3 — full-suite RED.** Not re-run as a separate RED step: implementation
groups 2–9 were completed in the same pass, so re-constructing the RED tree would
have required reverting them. The task's predicted full-suite RED
(**342 passed, 23 failed, 2 errors**) is arithmetically confirmed by the GREEN
result below (see D13): the 35 new tests collapse to 23 failed + 4 passed + 6
tests hidden by the two collection errors, and the pre-change baseline
(proposal §Impact) is 343 passed, less the one deleted obsolete pin = 342.

**1.2 — harness defect fixed.** `tests/test_pfc_render.py::test_s12…`'s `_Refusing`
stub now subclasses `core.types.IProvider` (adds `result`, `get_patterns`,
`check`), so the worker exercises the *error path* rather than the
unknown-provider configuration path. See D12 for the additional harness fixes
this surfaced.

---

## §2 — Live third-party dialect probe (design D11, task 1.4)

Script: `/tmp/opencode/pfc_probe.py` (throwaway). Evidence:
`/var/tmp/opencode-pfc-gate/probe_auth_failure.json`. Captured 2026-09-28T23:44Z.

Smallest request that answers the format question: `GET <base>/v1/models` with a
deliberately **invalid** bearer key (`sk-INVALIDPROBE…`). No real credential was
transmitted for this capture, and every captured byte passed through a
token-shaped-string masker.

| provider | URL | status | `Retry-After` / `X-RateLimit-*` | body shape |
|---|---|---|---|---|
| dashscope | `dashscope.aliyuncs.com/compatible-mode/v1/models` | **401** | **none** | `{"error":{"message","type","param","code"},"request_id"}` |
| dashscope-intl | `dashscope-intl.aliyuncs.com/compatible-mode/v1/models` | **401** | **none** | `{"error":{…},"request_id"}` |
| qwen-maas | `maas.qwencloudapi.com/compatible-mode/v1/models` | **401** | **none** | `{"error":{…},"request_id"}` |
| deepseek | `api.deepseek.com/v1/models` | **401** | **none** | `{"error":{…}}` (`authentication_error`) |

**Capacity refusal: `NOT OBSERVED`.** `AGENTS.md` §3 forbids provoking a limit,
and no organic refusal occurred during the probe window. Consequently **no
provider-specific dialect rule was added** (task 1.5 is not triggered): the
shipped predicate recognises only the dialect-independent signals — 429, 403 +
`rate limit|abuse detection|secondary rate limit`, and a published wait.

Observation worth recording: all four providers return an auth failure as **401**
(no throttling markers), which the shipped `http_get` classifies as
`NetworkError("Authentication failed (HTTP 401)")` exactly as before — no
behavior change for the observed dialect, and no rule was invented for an
unobserved one.

---

## §3 — GREEN suite

**10.3 — full suite, three consecutive runs:**

```
$ python -m pytest tests/ -q
377 passed, 7 warnings          (run 1, 49.79s)
377 passed, 7 warnings          (run 2, 49.79s)
377 passed, 7 warnings          (run 3, 50.44s)
```

The 7 warnings are pre-existing `asyncio.iscoroutinefunction` deprecations in
`tools/retry.py` (unrelated to this change).

Re-run after the D15 renderer fix (found by live gate row B2): **377 passed**
again on three further consecutive runs (50.44 s / 49.88 s / 49.76 s), with
`openspec validate` still reporting the change valid and 16/16 main specs.

Targeted guards from groups 2–9, all green:

| Task | Command | Result |
|---|---|---|
| 2.3 | `pytest tests/test_gt_transport.py tests/test_gt_stage_integration.py tests/test_tdm_starvation.py tests/test_tdm_latency.py -q` | 53 passed |
| 3.3 | `pytest tests/ -q -k "defer or deferral"` | 28 passed, 349 deselected |
| 5.5 | `pytest tests/ -q -k "provider or bedrock or gemini or vertex or anthropic"` | 6 passed, 371 deselected |
| 6.5 | `pytest tests/test_pfc_render.py tests/test_tdm_render.py -q` | green (included above) |
| 9.6 | `pytest tests/test_pfc_config.py tests/test_gt_config.py tests/test_cl_config.py -q` | 37 passed (with render) |

**10.4 — OpenSpec validation:**

```
$ openspec validate fix-provider-failure-classification --type change --strict
Change 'fix-provider-failure-classification' is valid
$ openspec validate --specs --strict
Totals: 16 passed, 0 failed (16 items)
```

**7.4 — `stage/base.py` needs no change (confirmed).** Its `RateLimitDeferral`
handler (base.py:686–698) already clamps via `_effective_defer_wait`, honours
`stage_pause`, performs the bounded sleep and calls `defer_task` with identity
preserved; the new producer (check-basket starvation) feeds that seam unchanged.
Observed live in the green check-starve tests.

**8.3 — no secret in the refusal path (confirmed).** `test_s12…` asserts the
captured ERROR log contains the `_generate_id` hash and none of the fake token,
cookie or address; it passes. `Service.__repr__` already masks the key
(`key='…'`), so the check-stage error log carries no secret either.

**10.2 — other pins asserting the replaced behavior.** Searched the suite for
`provider limiter starved`, `Authentication failed`, `_fetch_models` returning
`[]`, `_is_http_rate_limited` and the gather delegates. The only pin on the
replaced starvation contract is the intended `FH4-S3`/`PRT-S13` pair;
`tests/test_gt_transport.py:322` ("Authentication failed" not in log) is a gather
assertion that still passes. No other pin required deletion or rewrite.

---

## §4 — Deviations from the plan (numbered as new design decisions)

The authoritative, append-only copies of D12–D16 live in this change's
`design.md → ## Decisions` (R5.3 convention); they are reproduced here in the
verification narrative because task 10.5 asks §1–§4 to carry them. D15 (found by
gate row B2) is recorded in §5 next to the measurement that produced it, and D16
(an out-of-scope defect found by gate row B5) in §5 next to the scan.

### D12 — Harness determinism fixes (beyond the single defect named in task 1.2)

Fixing only `test_pfc_render.py::test_s12`'s stub was not sufficient: the same
class of harness defect existed in `tests/test_pfc_check_starve.py` and surfaced
only once the unknown-provider path was removed. Three minimal, contract-
preserving fixes were required; **no semantic assertion was weakened**:

1. `_RefusingProvider` now subclasses `core.types.IProvider` (adds `result`), so
   `CheckStage._check_worker` reaches the rate-limiting block instead of the
   unknown-provider configuration branch. Without this the worker never starves.
2. `_starved_limiter` pins the bucket's **capacity** to `0` as well as its
   current fill, because a real `TokenBucket` refills at `base_rate=100/s`
   (~1 token / 10 ms); the worker's `acquire` would otherwise succeed after the
   harness's own setup latency and the starvation precondition would be a race,
   not a fixture. `rate=100` is preserved so the refill-time wait stays ~10 ms.
3. `test_fh4_s1_starved_check_defers_without_burning_an_attempt` asserted
   `tasks_deferred == 1`. An always-starved task is re-queued and re-claimed as
   soon as its bounded sleep elapses, so the exact count is a timing artifact
   (the worker can begin a second deferral while `stop()` joins it). Relaxed to
   `>= 1`, matching the established R5.3 gather precedent (`tasks_deferred >= 3`
   in `tests/test_tdm_stage_defer.py`). Every semantic assertion
   (`failure_empties == 0`, `total_errors == 0`, `tasks_requeued == 0`,
   `tasks_dropped_max_retries == 0`, `total_processed == 0`, identity/age
   preservation, empty registry) is unchanged.

### D13 — Expected GREEN total is 377, not 371

`tasks.md` predicted 371 = 343 − 1 + 29, taking the RED collection count (29) as
the number of new tests. Both `tests/test_pfc_config.py` (module-level
`ImportError: cannot import name 'ProviderConfig'`) and one
`tests/test_pfc_check_starve.py` case were **collection errors** under
`--continue-on-collection-errors`, so six tests were never counted. The new-file
total is **35**, and 343 − 1 + 35 = **377**, which is the observed stable GREEN
across three consecutive runs. This is a counting artifact of the RED harness,
not extra coverage: the 17 spec scenarios are exactly the tests that turned green.

### D14 — `_check_worker` needed an explicit `except RateLimitDeferral: raise`

The generic `except Exception` at the end of `CheckStage._check_worker` swallowed
the typed deferral (logging it as a task error and returning `None`) before it
could reach the worker loop. A propagation branch was added *before*
`except TransientFetchError`, mirroring that handler. This is a direct
consequence of design D3 raining `RateLimitDeferral` through a method that only
knew `TransientFetchError`; it changes no classification.

### No other deviations

No provider dialect rule shipped (D11/§2). `provider.gemini`/`provider.vertex`
were inspected and left untouched (task 5.3): they have no fail-open wrapper, so
the typed signal propagates unaided. `bedrock.check()` was left untouched, as was
the unreachable `status_code != 200` branch (task 4.3, annotated in place). The
two further numbered decisions raised by the live gate — **D15** (all-zero
refusal surface must render nothing) and **D16** (pre-existing console-redaction
gap, reported not fixed) — are recorded in §5 beside the measurements that
produced them.

---

## §5 — Live gate (Group 11) — EXECUTED, ALL ROWS PASS

Host facts: `/var/tmp` on `/dev/vda2` (131 GB free at start); `/tmp` is the 5.7 GB
tmpfs. All run roots under `/var/tmp/opencode-pfc-gate/`, launched with
`setsid nohup` from script files under `/tmp/opencode/`, each wrapped in
`timeout -k 30 <N+300>`, `cd "$RUN"` before launch so the repo's own `data/`
and `logs/` were never written. No `pytest` was run while a live run was active
(`AGENTS.md` §2). Local date 2026-09-29 / UTC 2026-09-28 throughout.

### 11.1 — evidence baseline

`/var/tmp/opencode-pfc-gate/evidence_baseline.txt`:
`config.yaml` md5 `a8685be3b149aa3780871948587806d8` (20 416 bytes);
basket `github_raw base_rate=4.0 burst_limit=8 adaptive=True`;
`threads {search:1, gather:4, check:4, inspect:2}`;
`queue {backend: sqlite, visibility_timeout_s: 300, max_age_hours: 24}`;
`gather {transport: raw, defer_local_suppression: true}`;
**no `provider:` section** ⇒ defaults `classify_refusals=True`,
`max_refusal_wait_s=60.0`; credentials 4 sessions + 4 tokens (values never
printed); `/tmp` at 357 M / 7 %; repo `data/` mtime 2026-09-12 23:33:56 (empty).
`main.py --validate` on the live config: **valid**.

### 11.2 — B1: no regression under the fixed classification — **PASS**

600 s soak, run root `b1_soak`, `exit_code=0`, `Runtime 690.2 s`
(600 s work + ~90 s graceful stop). Raw: `b1_soak/evidence/b1_summary.txt`.

| accept row | measured | verdict |
|---|---|---|
| gather ≈ 8.5 req/s (R5.3 D20 reference) | `raw req=5120` over the 600 s window = **8.53 req/s** (7.42 over the full 690 s incl. stop) | **PASS** |
| `defer[budget]` > 0 | `defer[rl=0, budget=4847, sec=0, cred=0]` | **PASS** |
| 0 gather failure-empties | gather stage `errors=0`; `drop[auth=0, 404=0]`; zero `failure.empt*` wording in 40 MB of console | **PASS** |
| 0 burned gather attempts | gather queue `SUM(attempts)=0`, `MAX(attempts)=0` over 10 414 pending rows | **PASS** |
| 429/403/secondary = 0 | `HTTP 429`=0, `HTTP 403`=0, `secondary rate limit`=0, `abuse detection`=0, `Authentication failed`=0, `Rate limit exceeded`=0 | **PASS** |
| RSS ≤ 300 MB | 138 samples, **max 171 MB**, mean 78.5 MB | **PASS** |

`p50=250 ms p99=350 ms`, `bytes=187 553 657`; `Credential: exhausted=0
deferred=0 secondary=0 early=0 emergency=0 blocking=0`.

### 11.3 — B2: the new surface renders live — **PASS (after one code fix)**

`ProviderRefusals:` appears in the periodic detailed block:
`refusals_rate_limit=0 refusals_quota=0 refusals_auth=0 refusals_transient=0
deferred_provider_budget=530 inspect_refused=0 inspect_empty_answers=0`.

Counters agree with the stage counters: `deferred_provider_budget=530` equals the
530 `[check] provider basket starved` INFO lines **and** the 530
`[check] deferred task on rate limit` WARNING lines (1:1:1).

**Defect found by the gate and fixed.** The first B1 periodic block (02:53:31,
before any refusal) still rendered `ProviderRefusals: …=0`, because
`get_provider_refusal_stats()` always returns the full declared key set, so
`if not metrics` never fired. That violates PRT-S11's "absent when the surface is
empty" and gate row B2. Fixed by mirroring the `Gather:` renderer's all-zero
guard (see **D15**). Re-observed live in B4: with the flag off, **0 of 11**
periodic blocks rendered the line while all 11 rendered `Gather:`; with the flag
on, **6 of 11** rendered it — the first 5 pre-date the first refusal and are
all-zero, so both halves of B2 are observed in a single run.

### 11.4 — B3: check-stage starvation defers — **PASS**

| accept row | measured (B1, 600 s) | verdict |
|---|---|---|
| `provider limiter starved` no longer a failure-empty | **0** occurrences (was 57–102 per 600 s in the R5.3 captures; 558 across those gates) | **PASS** |
| `[check]` deferrals appear instead | `[check] provider basket starved; deferring 0.50s` × **530** | **PASS** |
| `SUM(attempts)` over the check queue does not grow | check queue `SUM(attempts)=0`, `MAX(attempts)=0` (queue drained; 401 processed, 0 errors) | **PASS** |
| `tasks_deferred` accounts for every one, 1:1 with the log lines | 530 starvation lines ↔ 530 `[check] deferred task on rate limit` lines; total across stages 4847 gather + 530 check = **5377** = the 5377 defer warnings | **PASS** |

### 11.5 — B4: rollback drill — **PASS**

Run root `b4_rollback`, seeded with **B1's own durable backlog**, two 120 s runs
on run-root config copies (deleted in B5). Raw: `b4_rollback/evidence/b4_summary.txt`.

| accept row | flag `false` | flag `true` (flipped back) | verdict |
|---|---|---|---|
| legacy signature returns | `provider limiter starved` × **14**, `[check] requeued` × **7**, check `errors=7` | × **0**, × **0**, check `errors=0` | **PASS** |
| attempts burned | 7 requeues (bounded-retry path) | 0 requeues | **PASS** |
| `deferred_provider_budget` flat at 0 | no `ProviderRefusals:` line at all (0/11 blocks) | `deferred_provider_budget=3`, matching 3 starvation + 3 defer lines | **PASS** |
| deferrals resume on flip-back | — | `[check]` deferrals × **3**, 1:1 with the counter | **PASS** |
| no task lost across either flip | search gone=1232 == processed=1232; gather gone=1014 == processed=1014 | search gone=1698 == processed=1698; gather gone=1014 ≤ processed=1020 | **PASS** |

Method for "no task lost": `dedup_id` sets snapshotted before/after each phase;
every disappeared id is legitimate only if that stage's own `processed` counter
accounts for it. Search matched **exactly 1:1** in both phases. `claimed=0` and
`integrity_check=ok` on all four queues at every snapshot; the only non-zero
`attempts` anywhere was one gather row (`sum=1, max=1`) from the pre-existing
gather transient-requeue path, unrelated to this change.

### 11.6 — B5: no secret in any output — **PASS**

Scanner: `/tmp/opencode/pfc_secret_scan.py`, categories kept apart per R5.3.
Pre-cleanup over 1 112 files / 1 683 MB of run root:

* **OPERATOR credentials (the 4 sessions + 4 `ghp_` tokens): 16 hits, all inside
  the two run-root config copies** — i.e. zero in any console log, evidence file,
  queue DB or harvested shard.
* `ghp_` / `github_pat_` shapes: 8, the same two config copies.
* harvested `sk-` shapes: 5 660 — pipeline **payload** (candidate keys the
  harvester found in public repositories), confined to `data/**` plus 7 lines in
  `b1_soak/evidence/console.log`.

Actions taken: masked the harvested material in the retained evidence
(14 `sk-` occurrences + 7 `Authorization` header values), **deleted both
secret-bearing config copies**, then deleted `data/providers/**` and
`data/queue_state/**` (11.8). **Re-scan to zero**: 87 files / 167 MB →
`OPERATOR=0`, `ghp_/github_pat_=0`, `harvested=0`.

Repo scan (367 files, `config.yaml`/`.secrets` excluded as the legitimate
credential store): `OPERATOR=0`, `ghp_/github_pat_=0`, one harvested-pattern hit
which is the **pre-existing synthetic literal** in `tools/logger.py:1007`'s
redaction self-test. Repo `data/` untouched (mtime still 2026-09-12 23:33:56).

> **Out-of-scope defect discovered by B5 (reported, not fixed here).**
> The 7 harvested keys in `console.log` came from the pre-existing
> `search/client.py::chat` error path, which logs the full request `headers` dict
> (`[chat] failed to request URL: …, headers: {'authorization': 'Bearer sk-…'}`).
> The key reaches **stdout in plaintext** because the console handler is built
> with `ColoredFormatter` (no redaction) at `tools/logger.py:599-600`, while the
> file handlers use `FileFormatterWithRedaction`. The `RedactionFilter` is
> attached to the **root logger** (`tools/logger.py:869-871`), which has no
> handlers — and Python does not apply a logger's filters to records propagated
> from child loggers, so it never fires for `get_logger("search")` records.
> Verified live: the same record is written to file as `Bearer sk-a1b...n4o5p6`
> (redacted) and to stdout in full. This is **not** the refusal path that PRT-S12
> / design D9 governs (that path is fixed and pinned), so it is deliberately left
> to a follow-up change rather than smuggled into this one. Suggested fix: give
> the console handler a redacting formatter, or attach `RedactionFilter` to the
> handlers instead of the root logger.

### 11.7 — B6: durability — **PASS**

Run root `b6_durability`. Raw: `b6_durability/evidence/b6_summary.txt`.

* **Graceful stop leaves `claimed = 0`** — all four queues, in both the B1 soak
  and the B6 recovery run; all four `-wal` files checkpointed back to 0 bytes.
* **`kill -9` on the process group with claims in flight** — `setsid` gave
  `pypid=pgid=572628`; 4 gather rows were `claimed` when `kill -9 -572628`
  landed; `survivors=0`. Frozen post-kill state: gather `claimed=4 rows=10830`,
  all other stages `claimed=0`.
* **`reclaimed == in flight`** — the recovery run logged exactly
  `[gather] reclaimed 4 orphaned claimed task(s) at startup`
  (`storage/task_queue.py:137`), and no other stage logged a reclaim. **4 == 4.**
  An earlier probe 1 s before the kill reported 3 claimed rows; gather
  claims/completes continuously at ~8.5 req/s, so the authoritative in-flight
  count is the one frozen by the kill itself.
* **Byte-identical queue files** — the four main `.sqlite` md5s are unchanged
  across the kill (`check f54c74f1…`, `gather 9bd48e73…`, `inspect df5b2b2f…`,
  `search 8bba5844…`). The `-wal`/`-shm` sidecars for gather and search differ
  between the two readings, which is **not** a torn write: `kill -9` performs no
  I/O, and the pipeline kept committing to the WAL in the window between the
  "before" reading and the kill. Consistency is proven instead by
  `PRAGMA integrity_check = ok` on all four DBs *before* the kill, *immediately
  after* the kill, and *after* the recovery run; by the recovery run reopening
  every DB, replaying the WAL and checkpointing all four `-wal` files to 0 bytes;
  and by zero `database is locked` / `Traceback` / `lost task` lines.

### 11.8 — cleanup — **DONE**

Deleted `data/providers/**` (1.1 G + 88 M + 135 M) and `data/queue_state/**`
(37 M + 35 M + 35 M) from all three run roots; `evidence/` kept (160 MB total,
mostly the 40 MB B1 console capture). `/tmp` back to its pre-task level
(357 M → 358 M, 7 %; the delta is the throwaway gate scripts in `/tmp/opencode`).
Everything the report cites is distilled into `b1_summary.txt`,
`b4_summary.txt`, `b6_summary.txt`, `evidence_baseline.txt` and
`probe_auth_failure.json`.

### 11.9 — this section. No acceptance row failed except the B2 empty-surface
rule, which was fixed in code (D15) and re-observed live in B4.

### D15 — The refusal renderer must treat an all-zero surface as empty

`get_provider_refusal_stats()` returns the full declared key set with zeros before
any refusal, so `if not metrics` can never fire in a live run and a zero-filled
`ProviderRefusals:` line was rendered from the very first periodic block. The
renderer now computes the declared figures first and returns `""` when every one
of them is zero — exactly the guard the `Gather:` renderer already uses. Unit
tests were unaffected (they exercise `{}`, an absent attribute, and non-zero /
malformed surfaces), which is precisely why only the live gate could catch it:
the empty-surface rule is observable only against a real all-zero surface.
