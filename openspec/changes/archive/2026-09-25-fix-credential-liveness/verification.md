# Verification: fix-credential-liveness

Evidence style follows the archived R2 / R5.1 verifications: verbatim command
output, dated provenance, and every implementation deviation recorded as a
numbered decision in `design.md` (never a silent edit). Secrets (tokens,
cookies) never appear in this file.

## §1 RED baseline (verbatim, before any implementation)

```
$ python -m pytest tests/ -q --continue-on-collection-errors
...
277 passed, 7 errors in 42.81s
```

HEAD at baseline: `904f9af`. The 7 errors were all collection-time
`ImportError`s for symbols that did not yet exist
(`tools.state.CredentialsExhausted`, `tools.state.credential_liveness_metrics`,
`config.schemas.CredentialLivenessConfig`,
`manager.task._probe_github_credentials`) — the expected RED state per house
TDD doctrine. `tests/test_fh_protections.py` was among the 7 errors (it imports
`CredentialsExhausted`). No other failure: no collateral damage at planning time.

## §2 Live GitHub wire-signal probe (task 1.2)

Command: `python /tmp/opencode/probe_cl_headers.py`
(script reads a token at runtime from the git-ignored `.secrets`; the token
value is never printed). Full output: `/tmp/opencode/probe_cl_headers.out`.

- **Date:** 2026-09-25T17:14:22 (local).
- **Endpoints:** `GET https://api.github.com/rate_limit`;
  `GET https://api.github.com/repos/octocat/Hello-World/contents/README`.
- **Auth mode:** `Authorization: Bearer <token>` read at runtime from
  `.secrets`; `X-GitHub-Api-Version: 2022-11-28`.
- **Status:** both `200`.
- **Exact header casing received (both responses):**
  `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`,
  `X-RateLimit-Resource`, `X-RateLimit-Used`.
  `Retry-After` was **absent** on the normal 200 (as expected).
- **Parser confirmation** (`search/client.py::GitHubClient._wait_from_headers`):
  `_wait_from_headers(r1.headers) -> 3592.19`, `...(r2.headers) -> 3592.19`
  (reset − now), i.e. the production parser reads exactly the `X-RateLimit-*`
  names observed, case-insensitively via lowercasing.
- **Discipline:** no secondary limit was provoked; the D5 wire-format caveat
  stands (only already-matched markers are classified).

## §3 GREEN suite + validation

```
$ python -m pytest tests/ -q
316 passed in 42.51s      # run 1
316 passed in 42.59s      # run 2 (repeat for stability)
```

> **Addendum (appended during the live gate, §5 — the 316 above is the group 1–8 state and is
> not retro-edited).** Each live-gate finding added pins, so the count moved:
> `316` (groups 1–8) → `318` (D18 auth-chain pin + D19 probe pin) → `319` (D20
> `credential_metrics_summary` pin) → **`320 passed in 42.68s`** (D21 per-load WARNING dedupe
> pin), the final state of the shipped tree. `openspec validate fix-credential-liveness
> --type change --strict` → `Change 'fix-credential-liveness' is valid` re-confirmed at 320.

Targeted new/amended set (task 8.1):

```
$ python -m pytest tests/test_cl_config.py tests/test_cl_selector.py \
    tests/test_cl_state.py tests/test_cl_detectors.py \
    tests/test_cl_stage_defer.py tests/test_cl_periphery.py \
    tests/test_fh_protections.py -q
39 passed in 0.15s
```

- 37 new tests across the six `test_cl_*.py` files; 2 in the amended
  `test_fh_protections.py`.
- Pre-existing suite preserved: excluding the six new `test_cl_*.py` modules,
  `279 passed` — the original 277 passes plus the two amended
  `test_fh_protections.py` pins (the obsolete
  `test_s16_full_pool_cooldown_waits_never_drops` was deliberately REPLACED,
  never weakened, by `test_fh_s2_full_pool_cooldown_defers_never_drops`;
  `test_aux_backoff_schedule_unchanged` survives untouched).
- **Note on flakiness:** one early full-suite run showed 2 failures in
  `test_gt_stage_integration.py` (`test_s18_a_deferred_task_is_never_executed_twice`,
  `test_defer_warning_names_the_bounded_wait`) alongside a background-thread
  `PytestUnhandledThreadExceptionWarning` (`stage.queue._conn` is `None` when a
  test consumer thread reads after queue close — a pre-existing test-harness
  race, unrelated to this change: both tests pass in isolation and the
  subsequent three full runs were green). Recorded, not hidden.

OpenSpec validation:

```
$ openspec validate fix-credential-liveness --type change
Change 'fix-credential-liveness' is valid

$ openspec validate fix-credential-liveness --type change --strict
Change 'fix-credential-liveness' is valid
```

(This CLI build rejects the plan's `--change` spelling; see design D16. Used the
positional form above.)

Live config (task 7.2) — backup first:

```
$ cp config.yaml /tmp/opencode/config.yaml.cl-backup
$ md5sum config.yaml /tmp/opencode/config.yaml.cl-backup
dddafb0b2e8c6459e374a98e1d5da26a  config.yaml
dddafb0b2e8c6459e374a98e1d5da26a  /tmp/opencode/config.yaml.cl-backup
```

After adding the explicit `credential_liveness:` section and removing the 16 dead
`rate_limit:` coefficient keys (4 per provider task):

```
$ python -c "from config.loader import ConfigLoader; c=ConfigLoader('config.yaml').load(); ..."
LOAD OK
wait_mode= bounded
max_wait_s= 60.0
early_release= True
emergency_threshold= 3
queue.backend= sqlite visibility= 300.0
ratelimit keys sample: ['adaptive', 'base_rate', 'burst_limit']
```

Zero validation errors/warnings on load; the cross-section invariant holds
(60.0 < 300.0).

## §4 Implementation deviations (recorded in design.md D12–D17)

1. **D12 — config logger capture / circular import.** Moved
   `from config import get_config` inside `ResourceManager.__init__`
   (`tools/coordinator.py`); `config/validator.py` and `config/loader.py` now
   create `logger = get_logger("config")` at module import. Required for pytest
   caplog to observe S6/S21; no behavior change.
2. **D13 — `_github_client` default instance.** Module global initializes to a
   credential-less `GitHubClient()` so the S26 injection seam exists; all
   production paths unchanged (`resource_provider` is `None` until wired).
3. **D14 — `_search_worker` re-raises `RateLimitDeferral`.** Added the explicit
   guard so the credential-exhaustion deferral is not swallowed by the search
   catch-all and silently completed-empty.
4. **D15 — cumulative sleep accounting.** The bounded selector tracks planned
   sleep (`spent`) rather than a wall-clock deadline; guarantees total in-task
   sleep ≤ `max_wait_s` even with a patched/no-op clock (S15). One log per
   episode.
5. **D16 — CLI spelling.** `openspec validate <name> --type change [--strict]`.
6. **D17 — emergency trip semantics.** One ERROR + one `emergency_trips` bump
   per ongoing emergency; a successful acquisition resets and re-arms.

No shipped evidence was edited; all deviations were added as new numbered
decisions rather than retrofitting the plan.

## §5 Live gate — `runbook.md` (tasks 9.1/9.2) — EXECUTED 2026-09-25

Gate verdict: **PASS with 2 non-measurable R5.1 rows** (12 of 14 acceptance rows PASS,
A1/A2 NOT MEASURED). Four live findings were made and fixed as new numbered decisions
(D18–D21); none of the shipped evidence below was retro-edited.

## 5.1 Environment incident: the gate filled the tmpfs (recorded, not hidden)

`runbook.md` step 0 originally prescribed `RUN=/tmp/opencode/cl_gate_$TS`. `/tmp` on
this host is a **3.9 GB tmpfs (RAM-backed)**. Measured consumption:

| run root | total | data/providers | queue_state | console capture |
|---|---|---|---|---|
| soak #1 (`cl_gate_20260925T150311`) | 2.5 GB | 2.3 GB | 78 MB | 56 MB |
| soak #2 (`cl_gate2_20260925T160816`) | 425 MB | 337 MB | 60 MB | 28 MB |

Consequences, in order:
1. soak #2 hit `OSError: [Errno 122] Disk quota exceeded` **from 19:11, i.e. 3 minutes
   into its 600 s** — 5 951 `--- Logging error ---` blocks and repeated
   `Failed to write links to shard` ERRORs. Its counters are therefore only partly
   usable and it is NOT treated as the acceptance soak.
2. Phase 2 (steps 4/6a/6b/7) died mid-way: `step4-restart` and `6a-html` exited 120
   (Python's "failed to flush output at exit"), `6a-raw` was SIGKILLed (exit 137),
   the 6b copy failed with `cp: … Disk quota exceeded`, step 7 never ran.
3. `/tmp` peaked at 81 % used (3.1 GB of 3.9 GB).

Remediation (done):
- `evidence/` (55 MB + 28 MB) and `data/queue_state/` (78 MB + 60 MB) moved to
  `/var/tmp/opencode-cl-gate/`; the disposable `data/providers/**` (2.6 GB) deleted.
  `/tmp` back to 172 MB used (5 %).
- Rule automated, not just noted: **`AGENTS.md` created** (§1 temp-storage policy with
  the measured numbers, §2 "live gates and the test suite do not mix", §3 live-gate
  hygiene) + the same rule added to `openspec/config.yaml` → `context:` so every
  artifact-creating agent sees it.
- `runbook.md` amended in place: step 0 → `RUN=/var/tmp/opencode-cl-gate_$TS` + sizing
  table (~4 MB of `data/` per soak second) + `df -h` preflight; step 1 → `setsid nohup`
  from a script file, `timeout -k 30 900` guard (graceful stop measured 150 s), zsh-safe
  glob; step 2 → two measurement caveats (log files can be incomplete — count from the
  console capture; only `credential_metrics` is rendered, D20); step 8 → cleanup duty.

## 5.2 Soak #1 — the clean production-shaped run (runbook steps 1–3)

Provenance: `RUN=/tmp/opencode/cl_gate_20260925T150311` (now
`/var/tmp/opencode-cl-gate/cl_gate_20260925T150311`), config md5
`1b9f3396031a000363670057d17aba06` (identical to the live `config.yaml`), `.secrets`
copied, project `data/` mtime `1789245236` unchanged before/after, project `logs/`
empty before and after. Preflight `main.py -c config.yaml --validate` → exit 0,
"Configuration file 'config.yaml' is valid", fresh workspace.

- Launch `1790349576`, exit `0`, end `1790350326` → **750.1 s wall** = 600 s processing
  + **150.1 s graceful stop** ("Graceful shutdown completed successfully in 150.1s").
  This is the datum behind the `timeout -k 30 900` guard and the systemd
  `TimeoutStopSec=300` rationale (precedent 121.3 s).
- Final stage table: search 79 393 processed / **0 errors**, gather 980 / 854,
  check 70 / 22, inspect 0 / 0; queues at stop: search 16 003, gather 20 282 pending.
- `Overall Status: Healthy`, `Error Rate: 0.0 %`, `Critical Alerts: 0`.
- RSS samples (25 × 30 s): **max 168 656 KB ≈ 165 MB**, no growth correlated with
  queue depth (queues went 0 → 36 285 rows while RSS stayed in the 150–165 MB band).
- Counters from the **console capture** (`evidence/soak.stdout`, 277 141 lines — the
  `logs/*.log` files under the run root were incomplete: every pattern returned 0
  there while the capture shows the real numbers):

| pattern | count |
|---|---|
| `HTTP 429` | 0 |
| `HTTP 403` | 0 |
| `lost task during persistence` | 0 |
| `secondary rate limit` / `abuse detection` | 0 |
| `all … credentials are cooling down` | 0 |
| `credentials exhausted` | 0 |
| `EMERGENCY` / `emergency` | 0 |
| `deferred task on rate limit` | 0 |
| `reclaim` / `orphan` | 0 |
| `dropped` | 0 |
| `suppressed by local limiter` (gather, `github_raw` bucket) | 1 708 |
| `failure-empty detected` → `requeued successfully` | 876 → 869 |
| `not accepting tasks, discard` (shutdown-time only) | 26 207 |
| `--- Logging error ---` | **0** (soak #2: 5 951 — see 5.1) |

- Composition of the 79 393 "search processed": 16 384×3 + 6 896 `refine_governor`
  budget refusals, 9 488 depth refusals, 129×4 cap truncations, and **158 real
  `[search] search completed …` responses** (aggregation `mode: on` collapses the four
  providers onto one HTTP chain, so completions are attributed to one provider).
  `Rate limit hit for github_raw, waiting …` × 976.
- Code state at launch: everything through task 8.3 **plus D12–D17**, but **before**
  D18/D19/D20 (found later the same evening). D18/D19/D20 only alter behaviour on the
  exhaustion/probe/rendering paths; soak #1 recorded **zero** exhaustion episodes, so
  its evidence is unaffected by those three fixes.

## 5.3 Positive live checks on the shipped paths (`/tmp/opencode/cl_probe/live_probe.py`)

The soak never organically hit a credential limit (pool healthy: 4 tokens + 4 sessions,
`X-RateLimit-Remaining: 5000`, no 429/403 at all), so acceptance rows A8–A12 would be
vacuous from it. They are instead evidenced by 13 targeted live checks against the real
`config.yaml`, the real credential pool and the real GitHub API — one benign
`GET /rate_limit` per invocation, tokens masked, **no secondary limit provoked**,
isolated CWD so project `logs/`/`data/` stay untouched.

Result: **13/13 PASS** (final run; the first run was 12/13 and produced finding D18,
the second 12/13 and produced finding D19 — both fixed, then 13/13).

| # | check | evidence |
|---|---|---|
| L1 | live `config.yaml` → selector seam | `wait_mode=bounded max_wait_s=60.0 early_release=True emergency_threshold=3`, and `_liveness_settings() is cfg.credential_liveness` (identity, not a copy) |
| L1b | cross-section invariant | `queue.backend=sqlite`, `60.0 < 300.0` |
| L2a | live wire headers | `GET /rate_limit` → 200, `X-RateLimit-Reset=1790355589`, `Remaining=5000` |
| L2b | healthy 200 is not a limit | `_classify_credential_limit` → `ambiguous` → nothing cooled, no raise |
| L2c | primary classification | live-shaped headers (`Remaining=0` + real `Reset` epoch) → `primary` |
| L2d | primary cools ONLY the credential in use | one WARNING, `wait=119.9 s`, cooling mates `[]`, `Cooldown.kind == "primary"`, `GithubCredentialLimited` raised for rotation |
| L2e | no ERROR / no pool-wide cooldown on primary | `secondary_limit_incidents == 0` |
| L3a | config drives the budget | temporary `max_wait_s: 2.0` copy → seam reports 2.0 |
| L3b | all-cooling real pool → typed exhaustion within budget | `CredentialsExhausted(reason=budget_spent, service=github_api, wait_estimate_s=898.0)`, **elapsed 2.00 s** (not 900 s), `exhausted_episodes +1` |
| L3c | no 0.1 s hot spin | elapsed 2.00 s == exactly one bounded sleep |
| L4 | early release | a 900 s bench freed at once, `early_releases 0 → 1`, one INFO |
| L5 | startup probe on an all-cooling real pool | chain raises `CredentialsExhausted`; `_probe_github_credentials()` → `(False, False)`, elapsed 0.00 s, `sleeps=[]`, exactly 1 WARNING |
| L6 | live search stage | `RateLimitDeferral("credentials exhausted (budget_spent), service=github_api")`, output `None`, elapsed 2.00 s, `deferred_by_credentials +1`, `total_processed == 0` |

Final collector snapshot from that process:
`{'exhausted_episodes': 4, 'deferred_by_credentials': 1, 'secondary_limit_incidents': 0,
'early_releases': 1, 'emergency_trips': 0, 'blocking_mode_active': 0}`.

## 5.4 Two CRITICAL findings the live gate caught (unit suite could not)

**D18 — typed exhaustion was swallowed by the production auth chain.**
Real wiring: `stage/definition.py` → `core.auth.GithubAuthProvider` (providers configured
by `manager/pipeline.py:58`) → `tools.coordinator.get_token/get_session` →
`Credentials`. BOTH intermediate layers wrapped the call in `except Exception: return None`
(`tools/coordinator.py:144-154`, `core/auth.py:63-79`). Measured live: with every token
benched, `coordinator.get_token()` returned `None` instead of raising. Consequences had it
shipped: the stage's `if not auth_token: return [], "", 0` branch would complete search
tasks **silently empty** (violates `Exhaustion defers tasks, never drops or empties them`,
S8); `storage/repo_meta.py::_next_credential` would see `None` and trip its **permanent
tokenless latch** (violates S25); `search/client.py::_gather_credential` would fall back to
an **anonymous REST request** guaranteed to fail (violates S26).
Fixed: `except CredentialsExhausted: raise` in the coordinator; lazy-import
`_is_typed_exhaustion()` guard in `core/auth.py` (lazy because `core/__init__ → core.auth →
tools.state → tools/__init__ → config → core.models` closes a cycle).
Pinned: `test_s24c_exhaustion_propagates_through_the_production_auth_chain`.
Why the suite missed it: every scenario injects a fake auth that raises directly.

**D19 — the startup probe waited out the cooldown.**
With D18 fixed the probe reached the real bounded selector and slept the whole budget
before degrading: measured `sleeps=[2.0]` at `max_wait_s: 2.0`, i.e. **60 s at the
production value**, contradicting S24 ("no startup sleep occurs") and task 6.4
("never sleep, never raise").
Fixed: `tools/credential.py` gained `_resolve_liveness_settings()`, `_settings_override`,
`_FailFastSettings` (`wait_mode="bounded"`, `max_wait_s=0.0`) and the
`fail_fast_liveness()` context manager; `manager/task.py::_probe_github_credentials()`
wraps its two calls in it. Pinning `bounded` also guarantees a `blocking` rollback config
can never hang boot.
Pinned: `test_s24d_capability_probe_never_waits_out_a_cooldown`.
Live re-check: L5 now reports `elapsed=0.00 s, sleeps=[], probe_warnings=1`.

**D20 — the counters were not evidenceable from a real run.**
`PipelineStatus.credential_metrics` satisfies the spec (S23), but nothing printed it
(`main.py` logs only runtime + monitoring stats; `state/display.py` renders the stage
table; the same is true of `refine_metrics`/`aggregation_metrics`/`gather_transport_metrics`).
Runbook rows A9–A13 cite `credential_metrics` as their evidence source, so the gate could
not be executed as written. Fixed minimally: `tools/state.py::credential_metrics_summary()`
+ one INFO line in `main.py`'s shutdown block (defensive try/except).
Pinned: `test_s23b_credential_metrics_summary_renders_every_counter`.

## 5.5 Step 4 (restart recovery) and step 6a (transport flip) — partial, pre-crash

Both ran before the tmpfs filled; their queue accounting is intact and citable.

Step 4 on the soak #2 workspace (`PHASE2.txt`):
- before restart: `gather claimed|8 pending|13168`, `search pending|70453`
- restart log: `[gather] reclaimed 8 orphaned claimed task(s) at startup` →
  **reclaimed == in-flight gather threads (8)**, not more (FH2-S3 / A7)
- after: `gather claimed|8 pending|26980`, `search pending|66116` → search drained
  4 337 rows, gather backlog grew from live search output; no loss
- exit 120 = Python's "failed to flush output at exit" under `Errno 122`; the run itself
  lasted 154 s (60 s + ~94 s graceful stop) and completed recovery + stop.

Step 6a on the soak #1 workspace (20 282 pending gather rows — the ≥100 precondition):
- config flip `transport: "raw"` → `"html"` (line 133), 120 s run, exit 120;
  after: `gather claimed|8 pending|41041`, `search claimed|5 pending|7346`
- flip back `"html"` → `"raw"`, 120 s run, **exit 137 (SIGKILL — tmpfs full)**;
  the following start logged `[search] reclaimed 5 orphaned claimed task(s)` and
  `[gather] reclaimed 8 orphaned claimed task(s)` → reclaimed exactly equals the
  claims left in flight by the kill (5 + 8), zero losses
- after: `gather claimed|8 pending|47051`, `search claimed|1 pending|7342`
- Accept: the SAME durable backlog was processed under both transports with continuous
  row accounting and **the config flip as the only change** (no code edits). The
  accidental SIGKILL additionally satisfies step 7's accept criterion
  (reclaimed == in-flight threads, zero losses); a deliberate kill -9 drill is recorded
  separately below.

## 5.6 Suite state during the gate (AGENTS.md §2)

One full-suite run launched at the exact moment phase 2 restarted a pipeline (15 worker
threads recovering ~36 k rows) produced ~30 failures across unrelated files. The same
suite passed **318** before the D18–D20 edits, **319** immediately after them, and **319**
again under load average 2.24. Diagnosis: timing-sensitive harness artifacts, not code
defects. Rule added to `AGENTS.md` §2: never run the suite concurrently with a live gate.

## 5.7 Phase 3 — rollback drill 6b, kill -9 (step 7), D20 rendering

Run root `/var/tmp/opencode-cl-gate/drill` (real disk, per the amended runbook step 0);
evidence `PHASE3.txt` + four console captures; `data/providers/**` deleted between runs,
`queue_state/` preserved. `/var/tmp` never exceeded 62 % of 117 GB.

**6b. `wait_mode: blocking` drill (S6 live half, A13 contrast).**
- `main.py -c config.yaml --validate` → exit 0 **with the loud WARNING**:
  `Configuration warning: credential_liveness.wait_mode=blocking under queue.backend=sqlite
  deliberately re-accepts the duplicate-execution hazard: an in-task wait may outlive the
  visibility window because the queue exposes no claim renewal` (`validator.py:114`).
- 60 s run under `blocking`: exit **0**, `Runtime 180.0s` (60 s + graceful stop 120.1 s),
  no crash, and the rendered counters show the rollback flag:
  `Credential liveness: exhausted_episodes=0, deferred_by_credentials=0,
  secondary_limit_incidents=0, early_releases=0, emergency_trips=0, blocking_mode_active=1`.
- Flipped back to `bounded` immediately: 60 s run, exit **0**, `Runtime 150.0s`
  (graceful stop 90.1 s), `blocking_mode_active=0` → bounded mode resumes from config alone.
- Accept: WARNING present at load; flag surfaced; no crash; flip-back restores bounded.
  Not soaked under blocking+sqlite (deliberate hazard, as instructed).
- This is also the live proof of **D20**: the counter block is readable from a real run's
  output on both sides of the flip.

**Step 7. Deliberate `kill -9` (durability under SIGKILL).**
- 60 s into a `--timeout 300` run: `gather claimed|8 pending|6248`, `search claimed|1 pending|29295`.
- `kill -9` on the process **group** (`setsid` + `kill -9 -- -$PID`; killing a `timeout`
  wrapper would orphan python, since SIGKILL cannot be forwarded) → `survivors=0`.
- Immediately after: queue states byte-identical (`claimed|8`, `claimed|1` still claimed) —
  the WAL store survived an unclean kill with no corruption and no loss.
- Restart logged `[search] reclaimed 1 orphaned claimed task(s) at startup` and
  `[gather] reclaimed 8 orphaned claimed task(s) at startup` → **reclaimed == in-flight
  threads (search 1, gather 8)**, exactly the step 7 / A7 criterion.
- After the restart cycle: `gather pending|10494`, `search claimed|1 pending|39179` —
  continuous accounting, zero losses.

**Observation O1 (pre-existing, out of scope): SIGABRT at interpreter finalization.**
The post-kill restart run exited **134** after printing `Logs flushed to disk`:
`Fatal Python error: _enter_buffered_busy: could not acquire lock for
<_io.BufferedWriter name='<stdout>'> at interpreter shutdown, possibly due to daemon threads`
(`step7-restart.stdout:7156`). A stage daemon thread was still emitting
`[gather] not accepting tasks, discard:` WARNINGs to stdout while the main thread finalized.
Not caused by this change (no thread-lifecycle code touched; soak #1 and the smoke run exited
0 with the same discard flood — it is a shutdown race). No data was lost: the graceful
shutdown had completed and the queue states above were read after the abort.
Recommendation for a follow-up change: join stage workers before interpreter finalization
(or make them non-daemon), and stop emitting discards once a stage is closed.

**Observation O2 (pre-existing, out of scope): gather is limiter-bound, not credential-bound.**
Soak #1: `suppressed by local limiter` 1 708, `failure-empty detected` 876 →
`requeued successfully` 869, gather 980 processed / 854 errors, `dropped` 0.
Soak #2: 602 / 301 / 301, gather 348 / 301 errors, `dropped` 0.
Cause: `ratelimits.github_raw base_rate 2.0 burst 4` against `pipeline.threads.gather 8`.
The suppression path burns `attempts` via strict requeue, so a long-lived backlog can
approach `max_retries`; no drops were observed in 600 s. Unrelated to credential liveness
(no credential counter moved), but it is the dominant gather failure mode and belongs in the
R3/R5 follow-up list: raise `github_raw.base_rate` or lower `threads.gather`.

## 5.8 Soak #3 — clean 600 s confirmation run on the SHIPPED code

Soak #1 ran on pre-D18 code and soak #2 was disk-starved from minute 3, so the acceptance
table needed one clean run of the exact shipped tree (D18 + D19 + D20 + D21).

- Run root `/var/tmp/opencode-cl-gate/soak3_20260925T171447` (real disk), config md5
  `1b9f3396031a000363670057d17aba06` (identical to the project copy — recorded in
  `evidence/00-config-md5.txt`), `SOAK_PID=298321 start=1790356487 timeout=600`,
  **`SOAK_EXIT=0`**, `Runtime 720.3s` (600 s + 120.3 s graceful stop).
- **Zero `--- Logging error ---`** in a 165 960-line capture; zero `Disk quota` events.
- Stage table at stop: `search 68 906 queue / 26 490 processed / 0 errors / 1 worker`,
  `gather 13 614 / 980 / 854 / 8`, `check 0 / 5 / 0 / 4`, `inspect 0 / 0 / 0 / 2`.
  Health: `Overall Status: Healthy`, `Error Rate: 0.0%`, `Critical Alerts: 0`.
  Seeded with `Created 4 initial search tasks for 4 providers` into an EMPTY workspace;
  final queue states `search pending|68906`, `gather pending|13614`, no rows left claimed
  (graceful stop released every claim).
- Rendered counters (D20, `main.py:544`):
  `Credential liveness: exhausted_episodes=0, deferred_by_credentials=0,
  secondary_limit_incidents=0, early_releases=0, emergency_trips=0, blocking_mode_active=0`.
- Refusal audit of the capture: `HTTP 429` 0, `HTTP 403` 0, `refusal without usable limit
  headers or secondary marker` (the ambiguous branch) **0**, `lost task during persistence` 0,
  `reclaimed`/`orphan` 0 (nothing reclaimed while the process ran → no in-task sleep outlived
  a claim), `dropped` 0, `secondary rate limit`/`abuse detection` 0, EMERGENCY 0.
  (`grep -c 429`/`403` raw substrings return 826/770 — all inside task UUIDs and
  `created_at` floats, verified by context extraction; the HTTP-status patterns are 0.)
- RSS series (24 samples, `evidence/10-samples.txt`): 14 MB → 119 MB → plateau
  **150–156 MB** (max 156 376 KB ≈ 153 MB) while search depth moved 77 056 → 68 906:
  a plateau, not growth proportional to queue depth.
- Gather-side noise, all pre-existing and limiter-caused (see O2): `suppressed by local
  limiter` 1 708, `failure-empty detected` 854 → `requeued successfully` 847,
  `Rate limit hit for github_raw, waiting …` 984, `not accepting tasks, discard` 20 907
  (shutdown-only).

**Organic primary-limit episode (20:15:27, 39 s into the run) — the gate's best evidence.**
A real GitHub refusal hit one API token and the shipped classification chain handled it
end-to-end without any test double:

```
20:15:27,855 | WARNING | [client.py:558]   | [GithubCrawl] GitHub API token rate limited: ghp_<REDACTED>, cooling down 60.0s
20:15:27,856 | WARNING | [definition.py:284]| [search] GitHub credential cooling during first-page search, retry with another credential, wait: 60.0s
20:15:27,856 | INFO    | [ratelimit.py:84] | Added rate limit for service: github_api:87cbce1862e1
```

Read against the spec: `_classify_credential_limit` returned **primary** (a usable limit
header was present — otherwise the ambiguous branch would have logged and cooled nothing);
exactly **one WARNING**, no ERROR; **only that credential** was cooled (`secondary_limit_incidents`
stayed 0, the pool kept working); the applied 60.0 s is the clamp floor
`GITHUB_CREDENTIAL_COOLDOWN_MIN = 60` (`constant/system.py:48`, bounds 60–900, factor 2.0),
i.e. "until the indicated reset, clamped by the existing cooldown bounds" per requirement
*Primary and secondary limits react differently*; the stage then **rotated to another
credential** (`definition.py:284`, the pre-existing `GithubCredentialLimited` path) instead of
dropping or emptying the task — search finished the soak with **0 errors** and
`[search] search completed` 26 490 == processed 26 490. This is S14 + FH2-S1 observed live.

Note the credential was already masked in the log line by `mask_credential`; it is redacted
again here.

## 5.9 Run inventory (code state per run — evidence is never retro-fitted)

| run | root | code state | duration | exit | verdict |
|---|---|---|---|---|---|
| smoke | `/tmp/opencode/cl_smoke` (deleted) | task 8.3 | 20 s (+100 s stop) | 0 | preflight only |
| soak #1 | `/var/tmp/opencode-cl-gate/cl_gate_20260925T150311` | 8.3 + D12–D17 (pre-D18) | 600 s / 750.1 s | 0 | CLEAN, but pre-fix code |
| soak #2 | `/var/tmp/opencode-cl-gate/cl_gate2_20260925T160816` | + D18/D19 (pre-D20) | 600 s / 751 s | 120 | CONTAMINATED by tmpfs `Disk quota exceeded` from minute 3 (5 951 logging errors); usable: 429/403 = 0, lost-task 0, secondary 0, cooling/exhausted/EMERGENCY/deferred 0, dropped 0, RSS max 153 MB, search 23 418 / 0 errors |
| step 4 + 6a | soak #2 / soak #1 workspaces | + D18/D19 | 120–154 s each | 120 / 120 / 137 | restart recovery PASS; transport flip PASS (the 137 was the tmpfs OOM kill, which also satisfied step 7's criterion) |
| 6b + step 7 | `/var/tmp/opencode-cl-gate/drill` | + D20 | 60–300 s | 0 / 0 / 134 | blocking drill PASS; kill -9 PASS; the 134 is pre-existing finalization race O1 |
| soak #3 | `/var/tmp/opencode-cl-gate/soak3_20260925T171447` | **shipped tree** (D18–D21) | 600 s / 720.3 s | **0** | CLEAN — anchors the acceptance table |
| live probes | `/tmp/opencode/cl_probe` | shipped tree | n/a | 0 | 13/13 PASS on real credentials |

`registry.sqlite` does not exist in any of these workspaces (`config.yaml` has no `registry:`
section), so the runbook's registry queries are N/A. Project `data/` mtime stayed
`1789245236` throughout. Project `logs/*.log` (main/refine/search/stage/tools, 820 lines,
20:35) were created by the **pytest** runs, not by any gate run — they are git-ignored
(`.gitignore:59 *.log`) and contain only fixture output (`Created stage: check, threads: 1`,
the synthetic secondary-limit ERROR with a `**`-masked fake credential). See O4.

## 5.10 Acceptance table (runbook step 3) — verdicts

| # | criterion | measured | verdict |
|---|---|---|---|
| A1 | gather bytes/file ≤ 20 KiB mean | material shards hold 5 records / 240 B total (gather is limiter-bound, O2); `gather_transport_metrics` is never rendered | **NOT MEASURED** — R5.1 transport metric, not evidenceable from this gate; needs `gather_transport_metrics` rendering (D20 follow-up) |
| A2 | gather latency p50 240–340 ms, p99 < 1 s | no latency instrumentation in the output (`elapsed|latency|took N` → 0 matches) | **NOT MEASURED** — same reason as A1 |
| A3 | unclassified refusals ZERO | ambiguous-branch log `refusal without usable limit headers or secondary marker` = **0**; the single real refusal was classified primary (§5.8) | **PASS** |
| A4 | HTTP 429 ≪ 2 715/600 s baseline | `HTTP 429` = **0**, `HTTP 403` = **0**; one primary limit episode total | **PASS** (≥3 orders of magnitude below baseline) |
| A5 | `code_search` cadence, no gather-induced starvation | search 26 490 processed / **0 errors** / 26 490 real completions; all 1 708 limiter suppressions are `github_raw` gather URLs | **PASS** |
| A6 | `date_metrics` NULL share not worse than html baseline | raw (soak #3) `api=0.000 (0/2649000), web=0.000 (0/126)`; html (6a drill) `api=0.000 (0/700), web=0.000 (0/6)` | **PASS** (equal — extraction broken on both paths, pre-existing plan Ф4) |
| A7 | durability: no lost tasks, no purges, no duplicates, no in-run reclaims | `lost task during persistence` = 0, `dropped` = 0, `reclaimed`/`orphan` = 0 during the run, queue rows continuous across every stop/restart (§5.5, §5.7) | **PASS** |
| A8 | every cooling episode slept ≤ `max_wait_s`; no 900-s in-task sleeps | one episode, applied wait 60.0 s == clamp floor, and the stage **rotated** instead of sleeping; `deferred_by_credentials` = 0; bounded budget itself proven at 2.00 s by probe L3b | **PASS** |
| A9 | exhaustion accounting: 1 WARNING + 1 `exhausted_episodes` per episode; deferred rows keep `attempts` | **no organic episode** (pool stayed healthy all runs). Exercised on real credentials by probes L3b/L6: `CredentialsExhausted(budget_spent)` in 2.00 s, `exhausted_episodes` +1, live `SearchStage.process_task` → `RateLimitDeferral`, output `None`, `total_processed` 0, `deferred_by_credentials` +1 | **PASS (by probe, not by soak)** |
| A10 | `emergency_trips == 0` | 0 in soak #1, #2, #3, all drills | **PASS** |
| A11 | `secondary_limit_incidents == 0` expected; organic → step 5 | 0 everywhere; no organic window occurred → step 5 N/A (never provoked, AGENTS §3) | **PASS** (step 5 stays open by design) |
| A12 | `early_releases ≥ 0`, no bench overlapping a success | 0 in every run (no bench+success overlap); increment path proven by probe L4 (`0→1` + exactly one INFO) | **PASS** |
| A13 | `blocking_mode_active == false` during the soak | `=0` in soak #1/#2/#3 and every bounded run; `=1` only inside the 6b blocking drill, back to `=0` after the flip | **PASS** |
| A14 | RSS plateau ≲ 300 MB, no growth with queue depth | max 153 MB (soak #3), 165 MB (soak #1), 153 MB (soak #2); plateau from sample 11 onward while depth stayed ~69–77 k | **PASS** |

12 PASS (A9 via live probe), 2 NOT MEASURED (A1, A2 — R5.1 transport metrics whose source
dict is never rendered; carried to R5.1, see O3).

## 5.11 Findings made during verification, and open items

- **D21 (new, fixed):** dead-key WARNING was emitted per *occurrence*, not per *load* —
  spec S21 promises exactly one per key per load, and a stale config with 4 task-level
  `rate_limit:` blocks would print 16 lines. Per-loader dedupe set, reset in `load()`,
  never process-global. Pin `test_s21b_dead_keys_warn_once_per_load_across_all_blocks`.
  (`config/loader.py`, design.md D21.) Suite after the fix: **320 passed**,
  `openspec validate fix-credential-liveness --type change --strict` → valid.
- **O3 (recommendation, not fixed here):** the primary-limit WARNING prints the *clamped*
  wait only, so evidence cannot distinguish "reset in 12 s, floored to 60" from
  "Retry-After: 60". Logging both the header-derived and applied values would make future
  gates self-explanatory. Out of scope at gate time (would touch a log line existing pins
  assert on) → follow-up.
- **O4 (pre-existing):** the pytest suite writes git-ignored `logs/*.log` into the CWD
  (`Logger._logs_dir = Path("logs")`). Harmless, but tests should point logging at
  `tmp_path` → follow-up.
- **O1, O2** above (finalization SIGABRT with daemon threads; gather limiter-bound at
  `github_raw base_rate 2.0` vs `threads.gather 8`) are both pre-existing and out of scope.
- **Open by design:** organic secondary-limit observation (runbook step 5) — cannot be
  closed without a real secondary window; never provoked.
- **Cleanup duty (AGENTS §1.3/§1.4):** `data/providers/**` deleted in all four roots
  (soak #3 alone was 777 MB); `evidence/` + `data/queue_state/` kept under
  `/var/tmp/opencode-cl-gate` (429 MB total); `/tmp` back to **158 MB (5 %)**.

## 5.12 Secret hygiene audit (post-gate)

`config.yaml` is git-ignored (`.gitignore:140 config.yaml*`) but carries **four real GitHub
PATs inline** (lines 10–13), and `.secrets` is git-ignored too (`.gitignore:112`). The gate
copied both into every run root, so after the gate completed:

- Deleted `/var/tmp/opencode-cl-gate/*/.secrets` (4 files) and `/var/tmp/opencode-cl-gate/*/config.yaml`
  (4 files). Provenance survives as md5 `1b9f3396031a000363670057d17aba06` in each run's
  `evidence/00-config-md5.txt`, which is what the acceptance rows cite.
- Scanned every kept artifact for unmasked credentials
  (`ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9]{20,}`): the change directory, all
  `evidence/*.txt` and all `evidence/*.stdout` captures are **clean**. The one live
  primary-limit WARNING (§5.8) was already masked by `mask_credential` at the source and is
  redacted again in this file.
- `git grep` over HEAD and the working tree finds no real credential — only the redaction
  pattern itself in `tools/patterns.py:19` and its documentation reference.

**Recommendation to the operator (out of scope for this change):** those four PATs live in
plaintext in a git-ignored file and were handled by an automated verification session;
rotating them is cheap insurance. Separately, consider moving them to `.secrets` only, so a
config copy can never carry credentials.
