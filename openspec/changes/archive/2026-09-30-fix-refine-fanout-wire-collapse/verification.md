# Verification — `fix-refine-fanout-wire-collapse`

Schema `tdd-flow`. Evidence recorded 2026-09-30. Live gate run root
`/var/tmp/opencode-rfg-gate_20260930T004958` (all raw figures distilled into its
`evidence/`; harvested shards deleted after extraction, per task 7.4/7.6).

## 1. Summary

| Check | Result |
|---|---|
| RED baseline | **15 failed, 35 passed** — reproduced exactly |
| GREEN (post-gate) | **475 passed** × 3 consecutive runs |
| Repository `logs/` after each run | empty (invariant holds) |
| Repository `data/` | untouched |
| Delta scenarios | 27 (11 ADDED + 16 restated); main spec 18 → **33** |
| `openspec validate --specs --strict` | 18 passed / 0 failed |
| `openspec validate … --type change --strict` | valid |
| S29 live gate | both arms `SOAK_EXIT=0`; **12/14 acceptance sub-criteria clean PASS, 2 qualified** (§5) |
| Tests deleted / weakened | none (one pre-existing helper amended, §6.2) |

## 2. RED baseline

Command (task 1.1):

```
python -m pytest tests/test_rfg_wire_collapse.py tests/test_rfg_pipeline_wiring.py \
  tests/test_tdm_render.py tests/test_rfg_governor.py tests/test_rfg_stage_integration.py -q
```

Result **15 failed, 35 passed**. Every failure was a missing behaviour, a missing
keyword or a missing metric key — no fixture or import errors. The 35 passes
included all 12 `tests/test_rfg_governor.py` tests and the whole of
`tests/test_rfg_stage_integration.py`. Per-test reasons: `tests.md` → "RED
baseline" (two rows corrected during GREEN — see §6.3).

No live pipeline run was active before or during any suite run (`AGENTS.md` §2);
the live gate was started only after the third pre-gate GREEN run and pytest was
not run again until both arms had exited.

## 3. GREEN

Three consecutive full-suite runs **before** the gate (475 / 475 / 475) and three
consecutive full-suite runs **after** the gate (475 / 475 / 475, 73.78 s /
73.88 s / 73.71 s). `logs/` empty before and after each; `data/` untouched.

**Count reconciliation (task 5.1).** Observed **475**, not the 476 the plan
predicted: 461 pre-existing + **14** new tests. The plan's "15 new" double-counted
`test_metrics_reach_the_status_surface`, which is a *pre-existing* pin that failed
in RED only because two keys were appended to `D9_KEYS`. New tests:
`test_rfg_wire_collapse.py` 11 (S19–S28, S30), `test_rfg_pipeline_wiring.py` +1
(S31), `test_tdm_render.py` +2 (S32, S33). No other delta.

## 4. Scenario → evidence

| Scenario | Test / evidence | Status |
|---|---|---|
| S19 child collapsing onto parent's wire is withheld | `test_s19_child_collapsing_onto_parent_wire_is_withheld` | GREEN |
| S20 siblings sharing one wire admit one survivor | `test_s20_siblings_sharing_one_wire_admit_exactly_one_survivor` | GREEN |
| S21 enforced admission admits one per distinct wire | `test_s21_enforced_admission_admits_one_child_per_distinct_wire` | GREEN (see §6.1) |
| S22 withheld children consume neither cap nor budget | `test_s22_withheld_children_consume_neither_cap_nor_budget` | GREEN |
| S23 survivor deterministic under reordering | `test_s23_survivor_is_deterministic_under_generator_reordering` (8 shuffles) | GREEN |
| S24 no fixed literal → not spuriously collapsed | `test_s24_child_without_fixed_literal_is_not_spuriously_collapsed` | GREEN |
| S25 web transport withholds nothing | `test_s25_web_transport_withholds_nothing` (real `wire_query`, no stub) | GREEN (see §6.3) |
| S26 withholding reported once per parent | `test_s26_withholding_is_reported_once_per_parent` (40 withheld → 1 line) | GREEN |
| S27 shadow counts/logs without enforcing | `test_s27_shadow_counts_without_enforcing` | GREEN |
| S28 guard is a single boolean rollback | `test_s28_guard_flag_is_a_single_boolean_rollback` | GREEN |
| **S29 live enforced run collapses fan-out** | two-arm A/B, §5 | **MANUAL — executed** |
| S30 truncation counts cap surplus only | `test_s30_truncation_counts_cap_surplus_only` | GREEN |
| S31 guard boolean validated like the mode | `test_s31_guard_boolean_is_validated_like_the_mode` (both mirrored sites) | GREEN |
| S32 three refusal reasons separable | `test_rfg_s32_three_refusal_reasons_are_separable` | GREEN |
| S33 distinguishable work published next to volume | `test_rfg_s33_distinguishable_work_is_published_next_to_volume` | GREEN |
| RO-S1 / RO-S2 / RO-S5 / RO-S7 (task 4.3) | `tests/test_tdm_render.py`, unmodified, all pass | GREEN |
| Pre-existing `test_metrics_reach_the_status_surface` | strengthened by the two new `D9_KEYS`; green in group 3, not reverted | GREEN |

Implementation anchors: guard `search/refine_governor.py:186-224`; deterministic
order `:193`; aggregated log `:312-336`; counters `:107`, `:112`; metrics `:361`,
`:366`; config `config/schemas.py:570,600`, `config/validator.py:527`,
`config/loader.py:492`; wiring `manager/pipeline.py:249`; render
`state/display.py:671` (`wire=`), `:673` (`uniq_wires=`). `clean_regex` is **not**
called by the governor (D2) — it appears only in a docstring.

## 5. S29 — live two-arm A/B on the flag

**Method (tasks 7.1–7.3).** Run root under `/var/tmp` (never `/tmp`), one CWD per
arm because `main.py` resolves `global.workspace` and `logs/` relative to CWD.
Live `config.yaml` referenced by provenance **md5 `a8685be3b149aa3780871948587806d8`**
(the same config the proposal cites). Each arm config was produced by a
**single-line** insertion of `drop_wire_indistinguishable: true|false` after
`max_search_tasks_per_run`; proof that nothing else changed:
`diff <(grep -v drop_wire_indistinguishable arm.yaml) config.yaml` → empty for both
arms. Both passed `main.py --validate`. Each credential-bearing copy was destroyed
by the gate script the moment that arm's process exited. Launched from a script
file, duration-matched (300 s each, sequential), wrapped in `timeout -k 30 600`,
`--stats-interval 30`. No pytest ran while an arm was live.

Comparison is taken on the **defined boundary** — the final status block after
graceful stop (lesson И6) — and coverage is compared by **unique** harvested
entities, never record counts (lesson И7).

### 5.1 Arm results

| Figure | arm_true (guard **on**) | arm_false (guard **off**) |
|---|---|---|
| `SOAK_EXIT` / Runtime / Overall / Alerts | 0 / 361.1 s / Healthy / 0 | 0 / 364.3 s / Healthy / 0 |
| `Refine:` | `gen=21312 adm=144 refused[depth=0, budget=0, wire=21168] trunc=0 uniq_wires=144` | `gen=74304 adm=10000 refused[depth=1214, budget=56048, wire=0] trunc=8256 uniq_wires=203` |
| search queue rows (final) | **0** (fully drained) | 19 173 |
| `search_queue.sqlite*` bytes | 126 976 | 10 297 344 |
| `data/providers` bytes | 3 986 955 | 40 434 175 |
| console bytes / lines | 2 600 350 / 9 652 | 19 462 018 / 75 020 |
| harvest records / **unique** value | 17 690 / **3 191** | 175 940 / **2 941** |
| **unique harvested links** | **3 056** | 2 784 |
| unique candidate keys (`material`) | 135 | 157 |
| distinct wire executed (`Aggregation misses/entries`) | **37 / 37** | 34 / 33 |
| queue `unique_wire` | n/a (0 rows) | 19 (of 9 485 raw, 44 `(wire,page)`) |
| `Gather: raw req` / bytes | **2 524** / 97 134 016 | 2 379 / 88 146 927 |
| gather pending backlog | 9 700 | 8 757 |
| search / check processed | 148 / **156** | 1 730 / **232** |
| `wire_collapse` INFO lines | **148** (for 21 168 withholdings) | **0** (guard off → no line, D8) |
| 429 / 403 / `secondary_limit_incidents` | 0 / 0 / 0 | 0 / 0 / 0 |
| `refusals_rate_limit` / `refusals_auth` | 0 / 0 | 0 / 0 |
| `Traceback` / `Logging error` / `_enter_buffered_busy` | 0 / 0 / 0 | 0 / 0 / 0 |
| console `ghp_<36 alnum>` / `github_pat_` / `sk-<20+ alnum>` | 0 / 0 / 0 | 0 / 0 / 0 |

### 5.2 Acceptance (task 7.5)

| # | Criterion | Verdict |
|---|---|---|
| 1 | enforcing arm `refused_wire_collapse > 0` | **PASS** — 21 168 |
| 2 | `children_admitted == distinct_wire_admitted` | **PASS** — 144 == 144 (legacy arm 10 000 vs 203) |
| 3 | search queue rows down ≥ order of magnitude | **PASS** — 19 173 → 0 |
| 4 | `search_queue.sqlite` bytes down ≥ order of magnitude | **PASS** — ×81.1 |
| 5 | `data/providers` bytes down ≥ order of magnitude | **PASS** — ×10.1 |
| 6 | console bytes down ≥ order of magnitude | **QUALIFIED** — ×7.48 (see below) |
| 7 | distinct wire queries **not lower** | **PASS** — 34 → 37 executed (`Aggregation misses`); queue-derived `unique_wire` is not comparable because the enforcing arm's search queue drained to 0 rows |
| 8 | unique harvested links **not lower** | **PASS** — 2 784 → 3 056 (+9.8 %), from ×9.95 **fewer** records |
| 9 | gather throughput unchanged | **PASS** — 2 379 → 2 524 req (+6.1 %), 88.1 → 97.1 MB (+10.2 %), pending backlog grew 8 757 → 9 700 exactly as design predicted (gather is basket-bound and received *more* distinct work) |
| 10 | check throughput unchanged | **QUALIFIED** — 232 → 156 processed (see below) |
| 11 | `SOAK_EXIT=0` on both arms | **PASS** |
| 12 | HTTP 429 = 403 = `secondary_limit_incidents` = 0 | **PASS** — literal `HTTP 429`/`HTTP 403`/`status=429`/`status: 403` all 0; `refusals_rate_limit=0`, `refusals_auth=0`, `secondary rate limit`=0, `abuse detection`=0. Bare `\b429\b`/`\b403\b` token counts (10/7 and 92/97) were inspected line-by-line: all are **timestamp milliseconds** (e.g. `00:50:31,403`), not status codes |
| 13 | zero `Traceback` / `Logging error` / `_enter_buffered_busy` | **PASS** — 0 in both arms (also 0 `Fatal Python error`, `queue is full`, `lost task`, `Disk quota exceeded`) |
| 14 | console carries no operator credential and no harvested-key token form | **PASS** — see §7 |

**Qualified #6 — console bytes ×7.48, not ×10.** The fan-out-driven component was
eliminated completely: the legacy arm emitted **56 048** per-child budget-refusal
WARNINGs (`refused[budget]=56048`), the enforcing arm emitted **zero** and instead
**148** aggregated `wire_collapse` lines for 21 168 withholdings (a 143:1
compression — direct live evidence for design D7). The residual 2.60 MB in the
enforcing arm is ordinary operation (2 079 gather budget-deferral WARNINGs plus
`github_raw` rate-limit waits), which sets a floor on the achievable ratio. The
criterion as written is therefore not met literally; the *cause* it targeted
(per-child fan-out noise) is fully removed. Recommendation: either accept with
this explanation, or restate the criterion as "fan-out-attributable console lines
eliminated" — recorded as a finding, not silently passed.

**Qualified #10 — check processed 232 → 156.** Check-stage *capacity* was not
starved: the check queue drained to **0** rows in both arms with **0** errors and 4
workers each. The lower count is input-driven — check tasks derive from harvested
candidate keys, and the enforcing arm harvested 210 `material` records (135 unique)
against 329 (157 unique), i.e. 14 % fewer unique candidate keys, while harvesting
**more** unique links (3 056 vs 2 784). Criterion 8 names *links*, which pass.
Single-sample arms with no measured run-to-run variance, so this is reported as a
finding rather than a pass. Recommendation: if a clean verdict is required, repeat
the A/B once more and compare check counts across two samples per arm.

### 5.3 Deferral re-evidenced by this gate

`aggregation_ratio` **rewards waste**, exactly as design D6 predicted: legacy arm
`hits=1697 / misses=34` → **49.9**, enforcing arm `hits=111 / misses=37` → **3.0**.
The arm with the ×16.6 higher ratio did *less* distinguishable work (fewer unique
links, fewer distinct wire requests executed, 19 distinct wires for 9 485 raw
partitions). This is why the KPI must be published paired with
`distinct_wire_admitted` and belongs to `search-aggregation`, not here.

## 6. Deviations and amendments

All are recorded in `design.md` / `tests.md` / `tasks.md`; none is a silent edit.

**6.1 — `distinct_wire_admitted` semantics (`design.md` "D6 amendment").** The
design specified a *cumulative global set* of fingerprints. Implemented as a
**per-parent distinct-wire count summed over the run**. Reason: the same wire can
legitimately be admitted under two different parents (the guard deduplicates only
within a parent's batch), so a global set under-counts and violates the spec's own
post-condition `children_admitted == distinct_wire_admitted` (S21) and acceptance
criterion 2. Approved by the operator before implementation. Consequence recorded
here so it is not misread later: **design D8's `distinct_wire=19` figure is a
global SQL count of distinct wire queries, not this status metric.** The legacy arm
indeed read `uniq_wires=203` (per-parent sum) while its queue held 19 globally
distinct wires — both numbers are correct for what they measure.

**6.2 — pre-existing stage-integration tests amended.** `tests.md` claimed
"Obsolete tests: none"; that was empirically false. `tests/test_rfg_stage_integration.py`
S1/S8/S11 use the incident seed `/sk-[a-zA-Z0-9]{32}/`, whose generated children
all reduce to the parent's wire `"sk-"` (verified against the real
`RefineEngine`: all 3 children → `'"sk-"'`), so the default-on guard withheld them
and masked the depth/cap/budget behaviour those scenarios pin. Amended per the
`tests.md` rule ("amend when the behaviour moved"): the `_governor` helper now
passes `drop_wire_indistinguishable=False`, with a comment stating why. **No test
function was deleted and none weakened**; the guard itself is covered by
`tests/test_rfg_wire_collapse.py`. `tests.md` → "Obsolete tests" updated.

**6.3 — S25 fixture defect corrected.** `SELF_COLLAPSING[2]` was byte-identical to
`SIBLINGS[0]`, so the test's own precondition
(`len({wire_query(q, False)}) == len(collapsing)`) was `5 == 6` and could never
pass; it failed at that line in RED, not with the `KeyError` `tests.md` recorded.
Corrected the fixture to a distinct raw that still collapses to `"sk-a"`
(`/sk-a[0-9]e[a-z]{28}/`), preserving the declared intent ("candidates are distinct
raw strings"). The `tests.md` RED table rows for S25 and S30 were transposed and
have been corrected.

**6.4 — task 5.1 expected count.** 476 → **475**; the plan's "15 new" double-counted
a pre-existing test (see §3).

**6.5 — task 7.3 launch mechanism.** The soak was launched from a script file
(`run_gate.sh` → `run_arm.sh`) under the harness's background-execution facility
rather than a literal `setsid nohup … &`; the process tree was verified to be a
detached session (`timeout -k 30 600 python main.py …`). Everything else in 7.3
holds: duration-matched, `--stats-interval 30`, zsh-safe globs, no pytest while an
arm was live.

**6.6 — task 8.1 wording.** The delta also amends the THEN of the pre-existing
"On enforces" scenario (`… partition clamps, the wire-collapse guard and the run
budget …`), so **two** pre-existing scenario THENs changed, not only S16's. The
main spec was synced from the delta faithfully; scenario count 18 → 33, no
renumbering, no reuse, no blockquotes.

## 7. Hygiene and security (task 7.6, `AGENTS.md` §1/§3)

- Run root entirely under `/var/tmp/opencode-rfg-gate_20260930T004958`; nothing
  written to `/tmp` except the pre-existing small tooling footprint.
- `/tmp`: 413 M / 8 % before → **415 M / 8 %** after. Returned to its pre-task level.
- Credential-bearing arm configs destroyed on arm exit; `find` for leftover
  `*.yaml` under the run root → **none**.
- Run-root scan for operator-token shapes (`ghp_[A-Za-z0-9]{20,}`,
  `github_pat_[A-Za-z0-9_]{20,}`) → **0 files**.
- Console captures: `ghp_<36 alnum>` 0, `github_pat_` 0, harvested-key form
  `sk-[A-Za-z0-9]{20,}` **0** in both arms (ROI-S1/S6 hold live).
- `data/queue_state/*.sqlite` scanned for `sk-[A-Za-z0-9]{20,}` → **0** in this run.
  (Had any been present they would be **product data** — harvested third-party
  values — not operator secrets, and would be reported as such.)
- Evidence distilled to small text files: `evidence/00-meta.txt`,
  `10-arm_true-final-status.txt`, `11-arm_false-final-status.txt`,
  `20-extract.txt`, `30-comparison.txt`, `90-hygiene.txt`, `df_tmp_before.txt`.
- Harvested shards (`arm_*/data/providers`) deleted **after** every figure cited
  above was extracted (lesson И7); run root 126 M → 83 M.

## 8. Deferrals carried forward (task 6.3)

1. **Per-child budget-refusal log volume** (`_log_budget_refusals`, design D7).
   Out of scope here: its loudness is pinned by the existing "Budget exhaustion
   refuses loudly and deterministically" scenario, so amending it would widen this
   change into the budget contract. Live re-evidence: 56 048 per-child WARNINGs in
   the legacy arm vs 148 aggregated `wire_collapse` lines in the enforcing arm.
2. **`aggregation_ratio` KPI** (design D6). Belongs to `search-aggregation` and must
   be published **paired with** `distinct_wire_admitted`. Live re-evidence: §5.3
   (49.9 for the arm doing less distinguishable work vs 3.0 for the enforcing arm).

Both are recorded in `design.md` (D6/D7 and "Open Questions") and are to be carried
into `plan.md` at closure (task 8.4).
