# Runbook: consolidated live gate — fix-credential-liveness + R5.1 gates 8.1/8.2/8.3

**When to run:** AFTER tasks.md groups 1–8 are complete (full suite GREEN,
`openspec validate` passes, docs/examples/live config updated). Executor: the
programmer agent or the operator. Discipline: the archived R2/R5.1 live-gate
rules — temp workspace under **`/var/tmp`** (never `/tmp`: it is a 3.9 GB tmpfs
and one 600 s soak writes ~2.5 GB — see `AGENTS.md` §1), credentials read at
runtime from git-ignored files, project `logs/` and `data/` untouched, **nothing
secret-bearing ever written into evidence** (tokens/cookies masked, log
excerpts redacted).

**What this runbook closes in ONE production-shaped cycle:**

| Gate | Origin | Steps below |
|---|---|---|
| R5.1 8.1 — full soak on the incident seed | archived `2026-09-25-fix-gather-transport/tasks.md` | 1–4 |
| R5.1 8.2 — mid-backlog rollback drill (transport flip) | same | 6a |
| R5.1 8.3 — secondary-limit accounting, organic ONLY | same | 5 |
| R5.2 — credential-liveness acceptance | this change | 1–6b |
| failure-handling FH2-S3 — restart recovery (Manual) | delta spec | 4 |
| credential-liveness S6 live half — blocking-mode drill | tests.md Manual | 6b |

**NEVER deliberately provoke abuse detection / secondary limits** (R5.1 8.3
rule). If a real secondary limit occurs organically, step 5 records it.

---

## 0. Preparation

```bash
TS=$(date -u +%Y%m%dT%H%M%S)
RUN=/var/tmp/opencode-cl-gate_$TS        # run root: workspace, logs, evidence
mkdir -p $RUN/evidence
cd /home/openuser/scrap/harverster_gist
cp config.yaml $RUN/config.yaml
cp .secrets $RUN/.secrets 2>/dev/null || true
md5sum config.yaml $RUN/config.yaml | tee $RUN/evidence/00-config-md5.txt
df -h /var/tmp | tail -1                 # need ≥ 5 GB free for a 600 s soak
```

**Sizing (measured 2026-09-25):** a 600 s soak at the committed fan-out writes
~2.5 GB — `data/providers/**` NDJSON shards+snapshots 2.3 GB, `queue_state/*.sqlite`
78 MB, console capture 56 MB. Budget ~4 MB/s of `data/`. Do NOT run the pytest
suite while a gate run is active (AGENTS.md §2).

Edit `$RUN/config.yaml` (keep everything else byte-identical):
- workspace path → `./data` relative to `$RUN` (verify the resolved value; the
  project `data/` must NOT be used);
- confirm present: `queue.backend: "sqlite"`, `queue.visibility_timeout_s: 300`,
  `refine_governor.mode: "on"`, `gather.transport: "raw"`,
  `aggregation.mode: "on"`, `credential_liveness:` section from task 7.2
  (`wait_mode: bounded`, `max_wait_s: 60.0`, `early_release: true`,
  `emergency_threshold: 3`), `ratelimits.github_raw` present;
- incident seed `/sk-[a-zA-Z0-9]{32}/` on all four providers (as committed).

Preflight:

```bash
cd $RUN && python /home/openuser/scrap/harverster_gist/main.py -c config.yaml --validate
ls -la $RUN/data 2>/dev/null || echo "OK: fresh workspace"
stat -c '%Y %n' /home/openuser/scrap/harverster_gist/logs/*.log > $RUN/evidence/00-project-logs-mtime.txt
```

Accept: validate exits 0; workspace fresh; project logs' mtimes recorded (they
must not change during the run — if they do, the run writes into the project
tree: ABORT and record why).

## 1. Soak run (600 s, precedent: R2/R5.1 soaks)

Put this in a **script file** and run it detached (`bash soak.sh &`): a job
started directly from an interactive shell dies with that shell, and under `zsh`
an unmatched glob aborts the whole script (`AGENTS.md` §3).

```bash
cd $RUN
setsid nohup timeout -k 30 900 python /home/openuser/scrap/harverster_gist/main.py \
  -c config.yaml --timeout 600 --log-level INFO > $RUN/evidence/soak.stdout 2>&1 &
SOAK_PID=$!
echo "SOAK_PID=$SOAK_PID start=$(date -u +%s)" > $RUN/evidence/01-soak-pid.txt
# sample every 30 s: RSS of the process + queue depths
while kill -0 $SOAK_PID 2>/dev/null; do
  { date -u +%s; ps -o rss=,vsz= -p $SOAK_PID 2>/dev/null;
    for f in "$RUN"/data/queue_state/*.sqlite; do
      [ -e "$f" ] || continue
      echo -n "$(basename "$f") "
      sqlite3 "file:$f?mode=ro" "SELECT state, COUNT(*) FROM tasks GROUP BY state;" 2>/dev/null;
    done; echo "---"; } >> $RUN/evidence/10-samples.txt
  sleep 30
done
echo "SOAK_EXIT=$? end=$(date -u +%s)" >> $RUN/evidence/01-soak-pid.txt
```

`--timeout 600` does not stop the process at 600 s: the graceful stop measured
**150 s** with ~36 k queued rows (precedent 121 s), hence the `-k 30 900` guard.

## 2. Post-run counter extraction

From `$RUN` logs (`logs/*.log` under the run root) and the final PipelineStatus
block in `soak.stdout`, extract into `$RUN/evidence/20-counters.txt`:

```bash
L=$RUN/logs
grep -c "HTTP 429" $L/*.log; grep -c "HTTP 403" $L/*.log
grep -c "lost task during persistence" $L/*.log            # must be 0
grep -c "secondary rate limit\|abuse detection" $L/*.log   # expected 0 (organic only)
grep -c "all .* credentials are cooling down" $L/*.log     # episode WARNINGs
grep -c "credentials exhausted" $L/*.log                   # typed exhaustion episodes
grep -c "EMERGENCY\|emergency" $L/*.log | grep -v ":0"     # must be absent
grep -c "ignored.*backoff_factor\|ignored.*recovery_factor\|ignored.*multiplier" $L/*.log  # acceptance-period WARNINGs if old keys remained
sqlite3 "file:$RUN/data/registry.sqlite?mode=ro" \
  "SELECT visit_status, COUNT(*) FROM links GROUP BY visit_status;"
sqlite3 "file:$RUN/data/registry.sqlite?mode=ro" \
  "SELECT COUNT(DISTINCT url) FROM links;"
```

Plus the metrics blocks printed by the pipeline (aggregation_metrics,
refine_metrics, gather_transport_metrics, credential_metrics, per-stage
tasks_deferred / tasks_dropped_max_retries / tasks_requeued / total_errors).

**Two measurement caveats found while executing this gate (2026-09-25):**

1. `logs/*.log` under the run root can be **incomplete** (rotation + the shutdown
   flush); the console capture `evidence/soak.stdout` is the complete record.
   Count from it, and record which source each number came from. Example: the
   same run showed `0` for every pattern in `logs/*.log` but `1708` gather
   limiter suppressions in `soak.stdout`.
2. Of the four metric dicts only `credential_metrics` is rendered
   (`Credential liveness: exhausted_episodes=…, …` — one INFO line at shutdown,
   design D20). `aggregation_metrics` / `refine_metrics` /
   `gather_transport_metrics` are populated into `PipelineStatus` but never
   printed, so A1/A2/A6 rows that cite them must be evidenced from log lines or
   carried over from the R5.1 gate — say which, explicitly.

## 3. Acceptance table — R5.1 8.1 (transport) + R5.2 (liveness)

| # | Metric | Threshold | Source | Result |
|---|---|---|---|---|
| A1 | gather bytes per file | ≤ 20 KiB mean (~30× vs the 523 KiB HTML baseline; R5.1 limited gate measured 12.6 KiB) | gather_transport_metrics / log byte counters | ☐ |
| A2 | gather latency | p50 in the captured raw band (~240–340 ms), p99 < 1 s | same | ☐ |
| A3 | unclassified refusals | **ZERO** (every 403/429 either deferred, secondary-cooled, or transient-counted) | counters + log audit of each refusal line | ☐ |
| A4 | HTTP 429 count | order-of-magnitude below the 2 715/600 s pre-fix baseline | step 2 | ☐ |
| A5 | `code_search` cadence | search stage proceeds; no starvation attributable to gather traffic (search tasks executed > 0 throughout; no new limiter-suppression warnings beyond baseline) | search logs / refine_metrics | ☐ |
| A6 | `date_metrics` NULL share | NOT worse than the html baseline (baseline is already high — extraction is broken on HTML since GitHub's React migration, plan Ф4) | date_metrics block | ☐ |
| A7 | durability | zero `lost task during persistence`; zero unexpected purges; zero duplicated rows; `sweep_expired_claims()` reclaims nothing while the process runs (no in-task sleep outlives the claim → proof of the WAIT_CAP invariant) | step 2 + queue sqlite states | ☐ |
| A8 | credential wait bound | every "cooling down" episode slept ≤ `max_wait_s` (60 s); NO 900-s in-task sleeps | episode WARNING lines vs sample timestamps | ☐ |
| A9 | exhaustion accounting | each `CredentialsExhausted` episode produced exactly one WARNING + one `exhausted_episodes` increment; deferred tasks show in `tasks_deferred` AND `deferred_by_credentials`; attempts of deferred rows unchanged (spot-check ≥3 rows) | credential_metrics + sqlite `SELECT attempts, created_at FROM tasks` | ☐ |
| A10 | emergency trips | `emergency_trips == 0` expected; if > 0 → investigate before accepting (all-bench or false detector) | credential_metrics | ☐ |
| A11 | secondary incidents | expected 0; if organically > 0 → step 5 | credential_metrics | ☐ |
| A12 | early release | `early_releases ≥ 0` and no credential benched while successful requests used it (spot-check log ordering) | credential_metrics + logs | ☐ |
| A13 | blocking flag | `blocking_mode_active == false` during the soak | credential_metrics | ☐ |
| A14 | RSS | plateau ≲ 300 MB (soak precedent 241–255 MB); no growth correlated with queue depth | 10-samples.txt | ☐ |

## 4. Graceful stop + restart recovery (FH2-S3 Manual)

The `--timeout 600` run stops itself; record the graceful-stop duration
(precedent 121.3 s; systemd `TimeoutStopSec=300` rationale). Then:

```bash
cd $RUN && python /home/openuser/scrap/harverster_gist/main.py -c config.yaml --timeout 60
```

Accept: restart logs reclaimed orphan claims ≤ in-flight threads at stop;
no task loss vs the pre-stop queue snapshot; dedup prevents re-execution of
completed work (spot-check registry `visit_status` counts monotonic);
second graceful stop completes. Record both cycles in evidence.

## 5. Organic secondary-limit window (R5.1 8.3 — only if A11 > 0)

If (and ONLY if) `secondary_limit_incidents > 0` occurred organically:
- capture the triggering body/reason **redacted** (mask tokens/cookies/URLs
  with credentials) into `evidence/50-secondary-capture.txt` with timestamp;
- verify: exactly one ERROR per episode; WHOLE service pool cooled (state
  dump); other services' pools untouched; stage paused within the bounded
  defer cap; no worker slept inside a claim past `max_wait_s`;
- verify the deferred backlog drains after the pause with **no attempt
  inflation** (`attempts` column unchanged for deferred rows);
- append the captured real phrasing to design.md as an amendment (D5
  wire-format caveat) — never a silent edit.

## 6. Rollback drills (R5.1 8.2 + CL S6 live half)

**6a. Transport flip over the SAME durable backlog.** With ≥100 pending gather
rows in `$RUN/data/queue_state`:

```bash
sed -i 's/transport: "raw"/transport: "html"/' $RUN/config.yaml
cd $RUN && python .../main.py -c config.yaml --timeout 120   # full path as in step 1
# observe: HTML-sized payloads (~300–1300 KiB/file), pre-change behavior, no payload rewrite
sqlite3 "file:$RUN/data/queue_state/gather_queue.sqlite?mode=ro" "SELECT COUNT(*) FROM tasks;"
sed -i 's/transport: "html"/transport: "raw"/' $RUN/config.yaml
cd $RUN && python .../main.py -c config.yaml --timeout 120   # raw again: resumes, byte counts drop
```

Accept: recovered rows process under both transports with no payload rewrite;
row accounting continuous across flips; config flip is the ONLY change (no code
edits). This proves the flag-flip rollback story on live data.

**6b. wait_mode blocking drill (short!).**

```bash
sed -i 's/wait_mode: "bounded"/wait_mode: "blocking"/' $RUN/config.yaml
cd $RUN && python .../main.py -c config.yaml --validate      # expect loud WARNING naming the duplicate-execution hazard
cd $RUN && timeout 90 python .../main.py -c config.yaml --timeout 60  # observe blocking_mode_active=true in metrics
sed -i 's/wait_mode: "blocking"/wait_mode: "bounded"/' $RUN/config.yaml  # flip back immediately
```

Accept: WARNING present at load; `blocking_mode_active == true` surfaced; run
does not crash; after flip-back, bounded mode resumes. Do NOT soak under
blocking+sqlite (deliberate hazard).

## 7. Optional: kill -9 drill (R2 pattern)

If time permits: `kill -9` mid-run; restart; accept reclaimed == in-flight
threads (search 1, gather 8, check 4 precedent), zero losses.

## 8. Evidence & handoff

- Every measurement, command, and outcome → `verification.md` §5 (accept/fail
  per row of the A-table). Evidence files stay under `$RUN/evidence/`; copy
  redacted excerpts only.
- Any acceptance failure → do NOT archive: amend code/specs as a new numbered
  decision in design.md, re-run the affected steps.
- On full pass: tick tasks.md 9.1/9.2, update `plan.md` (R5.1 gates CLOSED,
  R5.2 promoted, governor-cap raise block LIFTED — next: R3
  `add-aggregation-locality-v2`), then sync-specs + archive (task 9.3).
- Redaction rule for ALL evidence: `ghp_…` tokens → `ghp_<REDACTED>`;
  `user_session` cookie values → `<REDACTED>`; never paste a full config.yaml.
- **Cleanup duty (AGENTS.md §1):** after the report cites what it needs, delete
  the heavy parts of every run root — `data/providers/**` first (2.3 GB of the
  2.5 GB), then `logs/` and the raw console captures; keep `evidence/*.txt`
  excerpts and `data/queue_state/` only while a later step still needs the
  durable backlog. Finish with `df -h /tmp /var/tmp` in the evidence: a gate
  must not leave gigabytes behind, and must never have written to `/tmp` at all.
