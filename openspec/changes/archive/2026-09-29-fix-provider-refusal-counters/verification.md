# Verification: fix-provider-refusal-counters

Method note: `openspec status` / `instructions apply` return `change_error` for an **archived**
predecessor, so the predecessor's artifacts were read directly from
`openspec/changes/archive/2026-09-29-fix-provider-failure-classification/`. This change's own
artifacts were read through the CLI (`openspec validate fix-provider-refusal-counters --strict`).

Baseline for every "unchanged" claim below: commit `698bbf5`, and for the transport table a **fresh
measurement** of that commit (`/tmp/opencode/prc_baseline.py`, run before any implementation) rather
than the numbers quoted in the predecessor's report.

---

## 1. Completeness

| Item | Result |
|---|---|
| Tasks | 36/36 checked in `tasks.md` |
| Scenarios | PRT-S15…PRT-S25 (11 new) all pinned; PRT-S1…S14 preserved and still green |
| Requirements | all 4 of `provider-refusal-taxonomy` amended; no requirement left unimplemented |
| Spec validation | `openspec validate --specs --strict` → see §5 |
| Declared counters with a producer | **7 of 7** (was 4 of 7) |

Producer census after the change (`grep -rn "_provider_refusal_stat_inc" --include=*.py` over product
code, tests excluded):

| key | producers | site |
|---|---|---|
| `refusals_rate_limit` | 1 | `search/client.py` precedence row 1, counted once by the `http_get` wrapper |
| `refusals_quota` | 1 | rows 2 and 4 |
| `refusals_auth` | 1 | row 5 |
| `refusals_transient` | 1 | rows 3 and 6 |
| `deferred_provider_budget` | 1 | `stage/definition.py` `CheckStage._check_worker` (unchanged) |
| `inspect_refused` | 2 | `InspectStage._inspect_worker`: the swallow path (new) and the `except RateLimitDeferral` path |
| `inspect_empty_answers` | 1 | `InspectStage._inspect_worker`, genuine empty answer only |

**W1 is closed**: no declared key is inert, and the requirement sentence *"A refusal SHALL never be
recorded as 'the provider has no models'"* now holds on every measured row (§2, table B).

---

## 2. Correctness

### Table A — the transport is unchanged (PRT-S22, design D1)

Measured on `698bbf5` before implementation and again after; both columns are from
`/tmp/opencode/prc_baseline.py` against the same loopback harness. `hits` is the number of times the
transport reached the wire.

| case | exception | hits | message | before → after |
|---|---|---|---|---|
| 429 + `Retry-After: 30` | `RateLimitDeferral` | 1 | `provider rate limit (HTTP 429) for URL: …` | identical |
| 429 + wait in body | `RateLimitDeferral` | 1 | same | identical |
| 429 no wait | `ConnectionError` | 3 | `Rate limit exceeded (HTTP 429)` | identical |
| 429 quota no wait | `ConnectionError` | 3 | `Rate limit exceeded (HTTP 429)` | identical |
| 403 + marker + `Retry-After: 12` | `RateLimitDeferral` | 1 | `provider rate limit (HTTP 403) for URL: …` | identical |
| 403 + marker no wait | `ConnectionError` | 3 | `Rate limit exceeded (HTTP 403)` | identical |
| 403 no marker | `NetworkError` | 1 | `Authentication failed (HTTP 403)` | identical |
| 403 quota | `NetworkError` | 1 | `Authentication failed (HTTP 403)` | identical |
| 401 no marker | `NetworkError` | 1 | `Authentication failed (HTTP 401)` | identical |
| 503 | `ConnectionError` | 3 | `Server error (HTTP 503): {body}` | identical |
| 500 | `ConnectionError` | 3 | `Server error (HTTP 500): {body}` | identical |
| 404 | `FileNotFoundError` | 1 | `File not found (HTTP 404), url: …` | identical |
| 400 | `NetworkError` | 1 | `HTTP 400 error: {body}` | identical |

Not one exception type, message or retry count moved. The same 13 rows are pinned by
`test_s22_counting_never_re_classifies_a_legacy_path`, so a future change to any of them fails a
regression guard instead of passing unnoticed.

### Table B — the counter column, which is the whole point (W1)

| case | counters before | counters after |
|---|---|---|
| 429 + `Retry-After: 30` | `refusals_rate_limit=1` | `refusals_rate_limit=1` |
| 429 + wait in body | `refusals_rate_limit=1` | `refusals_rate_limit=1` |
| 429 no wait | **`{}`** | `refusals_transient=1` |
| 429 quota no wait | **`{}`** | `refusals_quota=1` |
| 403 + marker + `Retry-After: 12` | `refusals_rate_limit=1` | `refusals_rate_limit=1` |
| 403 + marker no wait | **`{}`** | `refusals_transient=1` |
| 403 no marker | **`{}`** | `refusals_auth=1` |
| 403 quota | **`{}`** | `refusals_quota=1` |
| 401 no marker | **`{}`** | `refusals_auth=1` |
| 503 | **`{}`** | `refusals_transient=1` |
| 500 | **`{}`** | `refusals_transient=1` |
| 404 | `{}` | `{}` — not a refusal |
| 400 | `{}` | `{}` — not a refusal |

Seven rows went from *silent* to exactly one class; the two rows that are not refusals stayed silent.
Every value is `1`, never `3`, including the rows that burn three attempts — that is design D10 (the
class rides on the escaping exception and is counted once by the wrapper), and it is the defect the
first implementation attempt actually shipped with before the pins caught it.

### Table C — end to end through a real stage (W1 at the place an operator reads it)

`test_s24_a_swallowed_refusal_is_never_recorded_as_an_empty_answer` drives a wire **401** through a
real `OpenAILikeProvider` and a real `InspectStage` over a real registry:

| | before | after |
|---|---|---|
| `_fetch_models` returns | `[]` | `[]` (the fail-open wrapper stays) |
| `refusals_auth` | 0 | **1** |
| `inspect_refused` | 0 | **1** |
| `inspect_empty_answers` | **1** | **0** |
| `stage.total_errors` | 0 | 0 |

The predecessor's measurement of the same input was `[] → inspect_empty_answers=1`; that row is the
defect, and it is now inverted by the thread-local refusal scope (design D9).

### Table D — signal escape per transport (W2, PRT-S17)

| transport | entry point | before | after |
|---|---|---|---|
| `openai_like` | `_fetch_models` | escapes | escapes |
| `anthropic` | `_fetch_models` | escapes | escapes |
| `gemini` | `inspect` | escapes | escapes |
| `vertex` | `inspect` (publisher loop) | **absorbed → `continue`** | escapes |
| `vertex` | `inspect` (fallback block) | **absorbed** | escapes |
| `bedrock` | `inspect` | **absorbed → `return []`** | escapes |

`test_s17_the_census_of_transport_callers_is_complete` additionally asserts that the set of modules
calling the provider transport is exactly `[anthropic, bedrock, gemini, openai_like, vertex]`, so a
new transport cannot be added without the escape property being extended deliberately.

### Table E — the previously unpinned 403 path (W3, PFC-D17)

`403 + limit marker + no published wait` → `ConnectionError("Rate limit exceeded (HTTP 403)")`, 3 wire
hits, `refusals_transient=1`. Now pinned by
`test_s15_a_marked_403_with_no_published_wait_keeps_the_legacy_transient_exception` and recorded as
**PFC-D17** in the archived predecessor `design.md`, with the pre-capability behaviour
(`NetworkError("Authentication failed (HTTP 403)")`, 1 hit) written down beside it.

### Findings closed

| Finding | Resolution | Evidence |
|---|---|---|
| **W1** three inert counters; refusals recorded as "no models" | producers for all three (D2 precedence), plus the stage-level scope so a swallowed refusal is not an empty answer (D9), plus once-per-call counting (D10) | Tables B, C |
| **W2** `vertex`/`bedrock` absorb the signal | `except RateLimitDeferral: raise` at `vertex.py` (both blocks) and `bedrock.py::inspect` | Table D |
| **W3** undocumented, unpinned 403 deviation | PFC-D17 appended to the archived design; PRT-S15 pinned; row also in the PRT-S22 baseline table | Table E |
| **W4** PRT-S9 pin weaker than `tests.md` | rewritten against a real `InspectStage` with `total_errors == 0`; the gap it could not see is now covered by PRT-S24 | Table C |
| **W5** PRT-S6 half-covered | worker half added: a wire-produced signal driven through a real stage to `defer_task`, wait clamped to the cap, 3600 s published wait never slept | `test_s6_the_worker_defers_…` |
| **W6** pre-existing flake | root-caused to a real race, three harness defects fixed | §3 |
| **S1** marker vocabulary duplicated | `LIMIT_MARKERS` exported and consumed by `is_rate_limited_content`; the private list is gone and its absence is pinned | `tests/test_prc_vocabulary.py` |
| **S2** D6/task 6.1 wording contradicts the code | PFC-D6 amended in the archived design; the archived task 6.1 annotated append-only | §4 |

---

## 3. Harness stability (W6)

Root cause found, not merely masked (design D8): the two consumer threads read
`stage.queue._conn.execute(…)` **without** holding `SqliteTaskQueue._cond`, while the other consumer
could be inside `_claim_locked()` on that same connection (opened with `check_same_thread=False`). The
resulting unhandled `sqlite3` error killed a thread outside any `try`; if both died before the victim
was claimed, `executions` never contained it and the assertion reported `assert 0 >= 2`. Two further
defects sat behind it: the stop condition caught bare `Exception` instead of `queue.Empty`, and
`assert stage.defer_task(t) is True` was inside a `try` whose `except Exception: pass` swallowed
`AssertionError` along with the task.

| measurement | before | after |
|---|---|---|
| full suite, consecutive runs | 3×377 twice, then **1 failed / 376 passed** on the 8th | **8 consecutive green runs**: 5 × 445, then 3 × 446 after the PRT-S25 pin was added; 0 failures |
| `test_s18` targeted | 2 failures / 40 runs | **30 / 30 pass** |
| `test_gt_stage_integration.py` file-level | — | **15 / 15 pass** |

No product code was touched for this, so no scenario is attached; the attribution that it is
pre-existing is in the proposal (0 changed lines in `AcquisitionStage`; `storage/task_queue.py`,
`stage/base.py`, `manager/queue.py`, `tests/conftest.py` and the test file byte-identical to
`20c15ce`).

---

## 4. Deviations recorded

Appended to **this** change's `design.md` as numbered decisions, per the `design` rule:

- **D8** — the flake was a real race, not just an invisible symptom; three harness defects, all fixed.
- **D9** — a thread-local, *nesting* refusal scope; `end_refusal_scope()` propagates the class to the
  enclosing scope so the transport's answer survives into the stage.
- **D10** — found during implementation: counting in the retried body counted **per attempt**, so a
  503 burning three attempts produced `refusals_transient=3`. Moved to a thin `http_get` wrapper that
  counts the escaping exception once; the retried body became `_http_get_once`.
- **D11** — the refusal surface is scoped to *our own* provider credentials; harvested-credential
  rejections stay on the check surface. Stated in the requirement text and pinned (PRT-S25) so it is
  not rediscovered as a gap.
- **D6 addendum** — `bedrock.check` was examined, a re-raise was written and then **removed as
  unreachable**: `check` uses the POST branch of `_send_request`, which calls `request()` directly,
  converts `HTTPError` to `(code, message)` at `:233-234`, and has its own inner `except Exception` at
  `:235-236` inside the frame PFC-D10 guards. Shipping a clause that cannot execute would recreate the
  "unpinned claim" problem this change removes.

Appended to the **archived predecessor** `design.md`, with this change named as provenance:

- **PFC-D17** (new) — the 403-with-marker-and-no-published-wait deviation from predecessor task 4.1,
  its resolution in favour of PRT-S2, before/after table, and the pin that now covers it.
- **PFC-D6** (amended) — the undeclared-key guard is deliberately **stricter** than `_gather_stat_inc`
  (raises `ValueError` rather than logging a WARNING), because an undeclared key would silently widen a
  published, rendered, completeness-registered surface. The archived `tasks.md` 6.1 parenthetical
  ("exactly like `_gather_stat_inc`") is annotated append-only; no history rewritten.

---

## 5. Evidence

### Offline

| item | value |
|---|---|
| RED before implementation | 46 failed / 21 passed across the new files, each failure for the reason stated in `tests.md` |
| RED, PRT-S24 | `refusals_auth` = 0 and the 401 landed in `inspect_empty_answers` — W1 seen from the stage |
| RED, PRT-S17 | `[vertex]` and `[bedrock]` "DID NOT RAISE RateLimitDeferral"; the other three transports already green |
| GREEN, new files | `tests/test_prc_classes.py` **29**, `tests/test_prc_vocabulary.py` **32**, `tests/test_prc_transports.py` **6** = 67 |
| GREEN, strengthened pins | `tests/test_pfc_defer.py` 7 → **9** (PRT-S24 and the PRT-S6 worker half added) |
| Full suite | **446 passed** = 377 baseline + 69 new; **3 consecutive runs at 446**, preceded by **5 consecutive runs at 445** before the PRT-S25 pin was added — 8 green full runs, 0 failures |
| Targeted stability | `test_s18` 30/30; `test_gt_stage_integration.py` 15/15 |
| Credential-detector regression | `tests/test_cl_detectors.py` passes **unmodified** after the vocabulary consolidation (task 1.5) |
| Spec validation | `openspec validate --specs --strict` → **17 passed / 0 failed**; `openspec validate fix-provider-refusal-counters --strict` → valid |
| Main spec after sync | `provider-refusal-taxonomy` **4 requirements / 25 scenarios** (was 4/14); sync diff is `+90 / −4`, the four deletions being the last line of each amended requirement paragraph — every pre-existing scenario preserved byte-for-byte, no blockquote introduced (repo convention) |

### Live

Two consecutive 300 s soaks on the **production** `config.yaml` (referenced by absolute path, never
copied — so this gate created no credential-bearing duplicate, unlike the predecessor's B5 which found
16 operator hits inside its two run-root config copies). Run roots under `/var/tmp` per AGENTS.md §1,
launched with `setsid --wait nohup` from `/tmp/opencode/prc_gate.sh` and wrapped in
`timeout -k 30 600` per §3. No `pytest` ran concurrently (§2).

| metric | run 1 `07:46:18` | run 2 `08:04:15` | predecessor B1 (600 s) |
|---|---|---|---|
| exit code | 0 | 0 | 0 |
| wall / `Runtime` | 421 s / 420.2 s | 421 s / 420.2 s | 690 s / 690.2 s |
| gather `raw req=` | 2551 | 2556 | 5120 |
| **gather req/s over the window** | **8.50** | **8.52** | **8.53** |
| p50 / p99 | 250 ms / 400 ms | 200 ms / 350 ms | 250 ms / 350 ms |
| `[check] provider basket starved` | 247 | 188 | 530 |
| `[check] deferred task on rate limit` | 247 | 188 | 530 |
| `deferred_provider_budget` | 247 | 188 | 530 |
| `[gather] deferred` / `defer[budget=]` | 2052 / 2052 | 2617 / 2617 | 4847 / 4847 |
| `provider limiter starved` (legacy path) | **0** | **0** | 0 |
| `[check] requeued` | 0 | 0 | 0 |
| `refusals_rate_limit/quota/auth/transient` | **0 / 0 / 0 / 0** | **0 / 0 / 0 / 0** | did not exist |
| `inspect_refused` / `inspect_empty_answers` | 0 / 0 | 0 / 0 | 0 / 0 |
| `HTTP 429` / `403` / secondary / abuse | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 |
| `Authentication failed` / `Rate limit exceeded` / `Server error` | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 |
| stage errors (`errors=[1-9]`) | none | none | none |
| `Traceback` / `database is locked` | 0 / 0 | 0 / 0 | 0 / 0 |
| `ProviderRefusals:` lines rendered | 20 | 20 | 6 of 11 blocks |
| RSS max / mean | sampler defect (see below) | **160 MiB / 146 MiB** (83 samples) | 171 MiB / 78.5 MiB |
| `claimed` at graceful stop (all 4 queues) | 0 | 0 | 0 |
| `PRAGMA integrity_check` | ok × 4 | ok × 4 | ok |
| `SUM(attempts)` / `MAX(attempts)` | 0 / 0 | 0 / 0 | 0 / 0 |

What the live runs prove for this change specifically:

1. **No behavioural or throughput regression.** 8.50 / 8.52 req/s against the 8.53 req/s baseline,
   identical latency profile, zero stage errors, zero tracebacks, zero lock contention, and the same
   121 s graceful-stop tail the predecessor measured.
2. **The new producers do not fire spuriously.** All four new counters read **0** across both runs
   while the pipeline made ~2 550 provider-basket deferrals and ~10 000 gather requests. That is the
   live confirmation of design **D5** (GitHub traffic cannot reach the provider counters — 0 `HTTP 403`
   and 0 `Authentication failed` on the provider surface) and of design **D3** (no quota-marker false
   positive across ~180 MB of real provider and GitHub payloads).
3. **The 1:1:1 correspondence still holds** (247 = 247 = 247 and 188 = 188 = 188), so the counter
   surface the predecessor's B2 row validated is undisturbed.
4. **Memory is not worse**: 160 MiB max against the predecessor's 171 MiB.

**Sampler defect found and fixed (recorded, not hidden).** Run 1's RSS column reported a flat
`max_kib=4084 mean_kib=4084` over 84 samples, which is impossible for the pipeline. `pgrep -f
"main.py -c …"` also matched the `timeout` wrapper (its argv contains the same string) and `head -1`
picked the lower PID, so the sampler measured the wrapper's constant ~4 MB. Fixed to select the
process whose `argv[0]` is `python*` and `argv[1]` is exactly `$REPO/main.py`; run 2's figure is the
valid one. Run 1's RSS is therefore **not evidence** and is reported as such rather than quoted.

**What the live runs do NOT exercise.** Both runs show `inspect rows=0` and `check rows=0` in the
durable queues: the inspect stage never ran, because an inspect task is only created for a credential
that validated, and none did in 300 s. So PRT-S24 / the refusal scope (design D9) and the
`vertex`/`bedrock` escape (PRT-S17) are covered **offline only** — by `test_s24_…` over a real
`InspectStage` with a real registry, and by `test_s17_…` over all five transports. The predecessor's
B1 had the same property. This is stated rather than left for the next verifier to discover.

### Credential hygiene (AGENTS.md §3, §1.4)

| | run 1 | run 2 |
|---|---|---|
| files scanned | 407 (619.9 MB) | 464 (720.0 MB) |
| **OPERATOR credentials** | **0** | **0** |
| **`ghp_` / `github_pat_`** | **0** | **0** |
| harvested `sk-` (pipeline payload) | 2927 | 2844 |
| masked in evidence | 9 harvested + 8 header values | 8 harvested + 7 header values |

Zero operator-credential hits **before** masking, because no config copy was created. The harvested
`sk-` hits are pipeline payload in `data/providers/**` plus the 7-8 lines the pre-existing
`search/client.py::chat` error path renders into the console log — i.e. exactly the PFC-D16 / plan
II.13 leak, reproduced here and still open (see §6). Evidence files were masked in place; no secret
value was printed by any command in this change.

Run-root sizes: 589 MB → 43 MB (run 1) and 683 MB → 90 MB (run 2) after distilling and deleting
`data/providers`, `data/queue_state` and `logs/`. `df -h /tmp` reads **331 MB** both before and after
— the tmpfs was never used for run data (§1.4 satisfied).

### Predecessor probes re-run through the shipped code (task 7.2)

`/tmp/opencode/pfc_verify_auth_path.py` — the same probe that produced the W1 table. It was **patched
first** (original kept as `.orig`): it hardcoded the pre-change stage rule
(`returned [] → inspect_empty_answers`) and never opened a refusal scope, so re-running it unpatched
would have reported the stage half of W1 as unfixed. It now brackets the call with
`begin_refusal_scope()` / `end_refusal_scope()` exactly as `InspectStage._inspect_worker` does.

| input | `_fetch_models` | counters **before** | counters **after** | hits |
|---|---|---|---|---|
| auth 403 (no marker) | `[]` | `inspect_empty_answers=1` | **`refusals_auth=1, inspect_refused=1`** | 1 |
| quota 403 | `[]` | `inspect_empty_answers=1` | **`refusals_quota=1, inspect_refused=1`** | 1 |
| transient 429 (no published wait) | `[]` after 3 attempts | `inspect_empty_answers=1` | **`refusals_transient=1, inspect_refused=1`** | 3 |
| transient 503 | `[]` after 3 attempts | `inspect_empty_answers=1` | **`refusals_transient=1, inspect_refused=1`** | 3 |
| capacity 429 + `Retry-After` | `RateLimitDeferral` | `refusals_rate_limit=1` | `refusals_rate_limit=1` | 1 |
| genuine empty answer 200 | `[]` | `inspect_empty_answers=1` | `inspect_empty_answers=1` | 1 |

Four rows flipped from "the provider has no models" to a named refusal class, and the genuine empty
answer is now the **only** row that counts `inspect_empty_answers`. The three-attempt rows report `1`,
not `3` (design D10). `_fetch_models`'s return value and every hit count are unchanged.

`/tmp/opencode/pfc_probe_403b.py` — the W3 path, re-measured and identical to the predecessor's
recorded values: `403 + marker, no Retry-After → ConnectionError "Rate limit exceeded (HTTP 403)"`,
hits=3; `403 + marker + Retry-After → RateLimitDeferral(wait_s=12.0, RATE_LIMITED)`, hits=1;
`403 no marker → NetworkError "Authentication failed (HTTP 403)"`, hits=1; `401 no marker →
NetworkError "Authentication failed (HTTP 401)"`, hits=1; `429 + Retry-After →
RateLimitDeferral(wait_s=30.0)`, hits=1; `429 no wait → ConnectionError`, hits=3.

### Spec / archive

| item | value |
|---|---|
| Archive | `openspec/changes/archive/2026-09-29-fix-provider-refusal-counters/` — 7 files (`proposal.md`, `design.md`, `specs/`, `tasks.md`, `tests.md`, `verification.md`, `.openspec.yaml`); named by **local** date, which on 2026-09-29 agrees with UTC (the R8 precedent) |
| Archive entries | 16 → **17** |
| `openspec list` | no active changes |
| `openspec validate --specs --strict` | **17 passed / 0 failed** |
| Main spec | `provider-refusal-taxonomy` **4 requirements / 25 scenarios** (was 4/14); the other 16 specs untouched |
| Sync method | agent-driven per the `openspec-sync-specs` skill, then `openspec archive --skip-specs` so the delta could not be applied twice |
| Tasks | 36/36 checked |
| Proposal lint | one non-blocking warning ("Why section should not exceed 1000 characters") — left as is: the section carries the six findings' file:line evidence, and trimming it would remove the provenance the `proposal` rule demands |

---

## 6. Consciously not done

- **No behaviour change on any legacy path** (D1) — the whole of Table A is the evidence.
- **No quota rule invented.** `QUOTA_MARKERS` is transcribed from five shipped `_judge` methods; the
  loose tokens (`quota`, `billing`, `purchase`, `RESOURCE_EXHAUSTED`) are excluded with reasons (D3).
  No live quota refusal has ever been captured — the PFC-D11 probe returned 401 `invalid_api_key` on all
  four production providers — so every quota fixture is labeled a synthetic supplementary unit vector.
- **The check surface is untouched**: `_judge`, `CheckResult`, `chat()`, the credential-liveness
  cooldowns and the adaptive budget all behave exactly as before (D11, PRT-S25).
- **`provider/vertex.py` and `provider/bedrock.py` are not exercised live**: neither is in the live
  config and no credentials for them exist on this host. Their pins stub the transport at the module
  boundary, which is the right altitude for "does a broad handler absorb one typed exception".
- **Console redaction (PFC-D16 / plan II.13) remains open.** This change does not touch logging. It is
  worth noting that the pre-existing `[chat]` error path that renders a whole `headers` dict is the
  source of the harvested keys found in every console capture, including this gate's (masked below).
