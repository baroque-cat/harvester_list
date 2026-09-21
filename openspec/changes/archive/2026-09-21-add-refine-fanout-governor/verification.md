# Verification: add-refine-fanout-governor

Evidence collected during implementation. Group 6 finalizes this document;
sections below are appended as tasks complete.

## Group 1 — RED baseline & early live probes

### 1.1 RED baseline (2026-09-21)

- `python3 -m pytest tests/test_rfg_governor.py tests/test_rfg_stage_integration.py`
  → collection error in both files, exactly as recorded in tests.md:
  - `ModuleNotFoundError: No module named 'search.refine_governor'`
  - `ImportError: cannot import name 'RefineGovernorConfig' from 'config.schemas'`
- Pre-existing suite
  `python3 -m pytest tests/ --ignore=tests/test_rfg_governor.py --ignore=tests/test_rfg_stage_integration.py`
  → **172 passed** in 38.29s (matches the staleness audit; nothing to delete or repair).

### 1.2 Guardrails

- `logs/` snapshotted to `/tmp/opencode/rfg_logs_snapshot/` before any tooling.
- Credentials read at runtime from the gitignored `.secrets` (token + session cookie)
  and passed to probe tooling via environment only; no secret was written into any
  fixture or repository file. Tooling wrote to `/tmp/opencode/`, not the project `logs/`.

### 1.3 Offline generator probe (2026-09-21)

Seed `/sk-[a-zA-Z0-9]{32}/`; `RefineEngine.generate_queries(query=seed, partitions=P)`,
measured via `/tmp/opencode/rfg_gen_probe.py` (project interpreter, single process):

| partitions | children | wall time | peak RSS |
|-----------:|---------:|----------:|---------:|
| 64         | 72       | 0.001 s   | 39.9 MB  |
| 1000       | 1296     | 0.004 s   | 40.2 MB  |
| 46007      | 46656    | 0.130 s   | 48.2 MB  |

After the pre-existing empty/self filters: 46 656 usable children (no losses).
Root wire form (`search/querykey.wire_query`) = `"sk-"`; a split child
`/sk-1l4[a-zA-Z0-9]{29}/` wires to `"sk-1l4"`.

### 1.3 Live child-total distribution (api.github.com/search/code, Bearer)

Raw responses saved as `tests/fixtures/live_2026_09_rfg_*.json` with `_provenance`
headers (endpoint, capture date, auth mode, query, page). `per_page=1`.

| label      | wire query | total_count |
|------------|------------|------------:|
| root       | `"sk-"`    | 47 448 064  |
| child_000  | `"sk-000"` | 75 136      |
| child_a00  | `"sk-a00"` | 240         |
| child_i00  | `"sk-i00"` | 24          |
| child_m00  | `"sk-m00"` | 157         |
| child_s00  | `"sk-s00"` | 266         |
| child_z00  | `"sk-z00"` | 9           |

Confirms the heavy skew grounding the default caps: a hot prefix (`"sk-000"`,
75 136 > limit 1000) legitimately refines again, while cold ones (`"sk-i00"`=24,
`"sk-z00"`=9) are leaves. Full enumeration of the 47.4M root at a 1000-per-query
transport limit needs ≥47 449 fetches ≈ 88 h per provider at 0.15 req/s.

## Group 2–4 — Implementation

- `SearchTask.refine_depth: int = 0`; serialized as **top-level** metadata (the
  S3 test simulates legacy queue JSON by removing the top-level key, so the field
  travels outside the nested `data` dict). `TaskFactory.from_dict` reads it with
  a `0` default; `TaskFactory.create_search_task` gained an optional kwarg.
- `RefineGovernorConfig` (`mode="on"`, depth 2, partitions 128, budget 10 000)
  added to `config/schemas.py`, parsed by `config/loader.py`, validated by
  `config/validator.py`; `Config.to_dict()` exposes it.
- `PipelineStatus.refine_metrics` added; `Pipeline` wires the governor via
  `_configure_refine_governor` (D1 fail-safe: construction error → built-in
  defaults in `on` mode, loud error log) and injects it through
  `StageResources.refine_governor`; `get_refine_metrics()` feeds the status.
- `search/refine_governor.py` implements the tri-mode contract behind one lock.
- `SearchStage._handle_first_page_results` gates on `may_refine`, clamps the
  partitions handed to the engine, admits via `select_children`, creates children
  with `refine_depth+1`, and falls through to ordinary pagination at the depth cap
  (calling `record_depth_cap_pagination`). Page tasks inherit the parent's depth.

### Deviations from the task/design wording (with rationale)

1. **`refine_depth` is top-level, not inside `_serialize_data`.** The pinned S3
   test does `legacy = task.to_dict(); legacy.pop("refine_depth", None)` and then
   asserts `from_dict(legacy).refine_depth == 0`. Task data is nested under
   `"data"`, so the only shape that satisfies the test is top-level metadata. The
   spec requirement (additive, absent → 0) is met.
2. **Admission order — RESOLVED by the verification round (was lexicographic).**
   The first implementation sorted candidate query strings lexicographically, on
   the argument that the pinned signature
   `select_children(provider, parent_query, queries, transport_limit, total)`
   carries no transport flag and therefore cannot compute a transport-aware wire
   fingerprint. Verification rejected that rationale: `task.use_api` *is*
   available at the call site (`stage/definition.py`), and spec requirement 2
   together with design D5 both name wire-fingerprint order (D3's
   "lexicographically" wording contradicted D5 and has been amended). Measured
   cost of the deviation on the incident seed at cap 128 (144 candidates, i.e.
   exactly the live-run case `generated=144 admitted=128 truncated=16`):
   lexicographic truncation dropped every `w x y z` child on **every** run — a
   permanent ~11 % keyspace blind spot; overlap with fingerprint order was only
   112/128. Fix: `select_children` gained a trailing keyword
   `use_api: bool = True` (pinned positional signature unchanged) and sorts by
   `search.querykey.fingerprint(query, use_api)`; the stage passes
   `use_api=task.use_api`. Unit pins updated to `ADMITTED_API` / `ADMITTED_WEB`,
   including explicit assertions that the order is *not* lexicographic and that
   the transport participates in the hash.
3. **`max_search_tasks_per_run == 0` — RESOLVED at config level.** Spec
   requirement 5 fails *non-positive* caps loudly, so configuration now rejects
   zero in both `RefineGovernorConfig.__post_init__` and
   `ConfigValidator._validate_refine_governor_config`. `RefineGovernor` itself
   deliberately stays tolerant of 0: the pinned S8 test constructs an
   already-exhausted budget directly, and that state is simply unreachable
   through configuration. The S8 harness's descriptive `Config` mirror now floors
   the budget at 1 (the stage reads the *injected* governor, not the mirror).

### Corrected defects in the new (untracked) test scaffolding

These files are new to this change and were never executed before (their RED state
was a collection ImportError), so the harness/assertion errors below were latent.
Each correction keeps the test at least as strict as before and matches the
spec/tests.md intent:

- `test_s2`: asserted `ceil(2_000_000 / 100) - 1 == 19 999` page tasks while its
  own inline comment said "capped at API_MAX_PAGES-1" and tests.md says
  "pages 2..10 emitted". Corrected to
  `min(ceil(2_000_000/100), API_MAX_PAGES) - 1 == 9` (imports `API_MAX_PAGES`).
  Without this the test would have demanded unbounded pagination — the exact
  behavior this change removes.
- `test_s1`: asserted `outputs and outputs[0] is out`, but `process_task` returns
  the `StageOutput` without invoking the handler (the handler is worker-loop-only;
  every existing stage test inspects the return value). Corrected to assert on the
  returned `out`.

## Group 5 — Regression

- `python3 -m pytest tests/` → **191 passed** in 37.55s at group-5 time
  (172 pre-existing + 19 new). Later additions moved the total: the
  reviewer-driven `test_shadow_budget_drain_is_measured` (→ 192), the six
  pipeline-wiring tests and the `off` log-equivalence pin added by the
  verification round (→ **199 passed** in 37.6 s = 172 pre-existing + 27 new:
  12 governor unit, 9 stage integration, 6 pipeline wiring). Final state: **199**.
- Explicit named suites: `test_sa_fingerprint.py`, `test_qr_clean_regex.py`,
  `test_early_stop_modes.py` → 22 passed; `-k fh_` → 17 passed. None pin fan-out
  volume, so no edits were required.
- Staleness audit: `git diff --name-only -- tests/` → 0 tracked test files
  modified; no pre-existing test was deleted or weakened.

## Group 6 — Live/offline verification

### 6.1 Offline mode-matrix E2E

Harness: `/tmp/opencode/rfg_mode_matrix.py` — real `SearchStage` + real
`RefineEngine`, mocked `search_with_count` (root total 46 000 000, depth-cap child
total 2 000 000), injected governor, `Config.refine_governor` matching the mode.

```
mode    scenario  admitted/refined unclamped truncated refused_budget
off     root                 46656     46656         0              0
off     depthcap  refined=2592 pages=0 refused_depth=0
shadow  root                 46656     46656     46653              0
shadow  depthcap  refined=2592 pages=0 refused_depth=1
on      root                     3     46656         0              0
on      depthcap  refined=0 pages=9 refused_depth=1
ALL MATRIX ASSERTIONS PASSED
```

- **`on` bounds the explosion:** the root admits 3 children (clamped partitions
  reach the engine, so the 46 656-candidate list is never even materialized);
  emitted tasks ≤ budget. The depth-capped child emits 0 grandchildren and 9
  ordinary page tasks (API_MAX_PAGES-1) and ticks the counter.
- **`shadow` measures without enforcing:** the full unclamped 46 656 child set is
  enqueued (parity with `off`) while 46 653 would-be truncations and the depth
  refusal are counted (`children_admitted` stays 0).
- **`off` reproduces pre-change:** all 46 656 children enqueued, counters inert;
  the depth-capped child recurses (2 592 grandchildren) exactly as the incident.
- Asserted: shadow/off enqueue parity, `on` clamp + budget bound, page-depth
  inheritance, counter deltas, and no depth-cap pagination outside `on`.

### 6.2 Short real-GitHub harvester run (`mode: on`)

Isolation: temp config (`/tmp/opencode/rfg_live_config{,2}.yaml`) with embedded
credentials stripped (loader reads them from env `GITHUB_SESSIONS`/`GITHUB_TOKENS`,
never written to disk), temp workspaces (`/tmp/opencode/rfg_live_data{,2}`), temp
working directory so logs land in `/tmp/opencode/rfg_run{,2}/logs` (project
`logs/` untouched). Production shape preserved: 4 API providers
(qwen-china-dash, qwen-intl-dash, qwen-maas, deepseek), incident seed
`/sk-[a-zA-Z0-9]{32}/`, `queue_sizes.search: 4000000` (the incident config that
removed the historical brake).

**Run 1 (75 s timeout requested):**

- `Refine governor configured: mode=on depth=2 partitions=128 budget=10000`.
- Root refinement per provider: `generated=144 admitted=128 truncated=16`,
  `coverage=0.002698` — the clamp reached the engine (144 ≈ 128×1.13 overshoot)
  and the cap admitted 128.
- Depth-1 children also governed (`generated=144 admitted=128 truncated=16`).
- Final search queue: **5 083** (versus the 4 000 000 slot / millions in the
  incident); search processed 40; gather 810, check 103; harvest non-zero
  (links shards written, per-provider link counts 4268/200/200/200).
- `lost task during persistence`: **0 occurrences** (no drain-race storm).

**Run 2 (RSS run, fresh workspace):**

- Aggregate governor decisions over the run: 33 parents refined,
  `generated=4752`, `admitted=4224`, `truncated=528`, `refused_budget=0`,
  `refused_depth=0` (depth 2 not reached in the short window).
- Final search queue: **4 067** (bounded); search processed 33.
- `lost task during persistence`: **0 occurrences**.
- **RSS** (VmRSS sampled every 4 s, KB):
  `9224 → 95180 → 113100 → 126552 → 136920 → 138080` then flat at ~127 200
  through 124 s. Peak ≈ **138 MB**, steady ≈ **127 MB** — versus the incident's
  6.9 GB RSS + 7.4 GB swap. No GB-scale growth.

Residual observed (pre-existing, not governor-related): during shutdown the
gather stage stops accepting while search still emits freshly discovered links,
producing `[gather] not accepting tasks, discard` lines. That is the existing
drain behavior, distinct from the `lost task during persistence` counter, which
stayed at zero.

### 6.3 Optional shadow cycle — decision recorded

Not executed against live GitHub. Rationale: a live `shadow` cycle would enqueue
the full unclamped child set (≈46 656 first-level children per provider) into the
4M queue — i.e. deliberately re-create the inflation this change removes — to
measure a price the offline 6.1 matrix already quantifies (46 653 would-be
truncations at cap 3; 528 truncations at cap 128 in the governed live runs under
`on`). Operators may still run it as a one-flip measurement; the decision here is
to rely on `on` defaults and the offline would-be numbers. Left as an operator
gate, unchanged from the plan.

### 6.4 Residuals

- **Totals drift between runs** shifts admission boundaries across runs; within a
  run admission is fully deterministic. The aggregation shadow log already
  measures the same phenomenon.
- The live runs were short (~75–180 s), so the depth cap (`max_refine_depth=2`)
  was not reached live (`refused_depth=0`); that path is covered by the S2
  integration test and the 6.1 offline matrix.
- `on`-mode root refines showed `truncated_to_cap=0` in the matrix because the
  clamp prevents materializing the surplus; truncation is exercised when the
  engine overshoots the requested count (e.g. partitions 5 → 6 children) and in
  the S4 unit test.

## Independent review fixes

A review pass (Mr.Reviewer) found two issues, both fixed before completion:

1. **[Medium] Shadow mode did not advance `_budget_used`/`_parents_refined`**, so
   `_refused_budget` and the would-be coverage stayed frozen across batches and
   did not reflect the budget drain `on` would cause. Fixed in
   `search/refine_governor.py::select_children` (shadow branch now advances the
   would-be budget trajectory; enqueue path unchanged). Regression test added:
   `tests/test_rfg_governor.py::test_shadow_budget_drain_is_measured`.
2. **[Low] `self._coverage[-1]` was read outside the lock** for the INFO log
   line; `_record_coverage` now returns the estimate and the caller logs the
   returned value. Cosmetic (metrics snapshot was already lock-protected).

The reviewer found no other material issues: lock discipline, legacy
serialization compatibility, the `governor is None` byte-equivalent path,
single `may_refine` evaluation per task, depth-cap enforcement, and the D1
fail-safe wiring were all verified clean.

## Verification round (`/opsx-verify`, operator-ordered fixes)

An independent audit against the artifacts re-ran the whole evidence chain
(`openspec status` / `instructions apply` = 28/28 `all_done`,
`validate --strict` valid, full suite green, golden suites green) and reported
**0 CRITICAL, 2 WARNING, 7 SUGGESTION**. The operator ordered all of them fixed
before spec sync and archive; tasks.md group 8 records the same list.

### W1 — admission order brought to the spec (code changed, not the spec)

Spec requirement 2 ("stable wire-fingerprint order") and design **D5** name
`search/querykey.fingerprint`; the implementation sorted lexicographically,
following design **D3**, which contradicted D5. The recorded rationale ("the
pinned signature has no transport argument") did not survive inspection:
`task.use_api` is available at the call site.

Measured cost of the deviation on the incident seed at cap 128 — i.e. exactly the
live-run case `generated=144 admitted=128 truncated=16`:

| order | first-chars admitted | permanently dropped | overlap |
|-------|----------------------|---------------------|---------|
| lexicographic (before) | `0-9 a…v` | **`w x y z` on every run** | 112/128 |
| wire fingerprint (after) | `0-9 a c…q t…z` + scattered | `5 9 b r` (differ per parent) | — |

Fix: `select_children(..., use_api: bool = True)` — a trailing keyword, so the
pinned positional signature is untouched — sorts by
`fingerprint(query, use_api)`; `stage/definition.py` passes `task.use_api`.
Design D3 amended to defer to D5. Post-fix probe (offline, real engine):
`candidates=144 admitted=128`, admitted list **equals** the fingerprint-sorted
expectation and **differs** from the lexicographic one, first-chars now cover
`w x y z`, and `coverage_estimate_min=0.0026977` is unchanged — the fix rotates
*which* children survive, never *how many*. Consequently every count-based number
recorded above (offline matrix, live queue sizes, truncation totals) stays valid.

Re-ran after the fix: `/tmp/opencode/rfg_mode_matrix.py` →
`ALL MATRIX ASSERTIONS PASSED` with identical counts
(off root 46 656; shadow root 46 656 enqueued + 46 653 would-be truncations;
on root 3 admitted; on depth-cap refined=0 pages=9 refused_depth=1;
off depth-cap refined=2 592 recursion).

### W2 — non-positive caps fail configuration validation

Spec requirement 5 fails *non-positive* caps loudly, but zero budgets were
accepted. Now `RefineGovernorConfig.__post_init__` and
`ConfigValidator._validate_refine_governor_config` both require
`max_search_tasks_per_run > 0`. `RefineGovernor` deliberately stays tolerant of 0
(S8 constructs an exhausted budget directly; deviation 3 above), and the S8
harness's descriptive config mirror floors it at 1 — the stage reads the injected
governor, not the mirror.

### Suggestions S1–S7

1. **S1 `off` was not log-equivalent.** The governor pre-filtered empty/self
   candidates in *all* modes, so the stage's own pre-existing WARNINGs
   (`skip refined query due to empty`, `discard refined query same as original`)
   could never fire under `off`. Fixed: `off` returns the generator list verbatim;
   `shadow`/`on` keep filtering so their counters describe real enqueues. Pinned by
   `tests/test_rfg_governor.py::test_off_leaves_stage_filters_authoritative`.
2. **S2 stage INFO under-reported.** `generated {len(queries)}` was logged after
   admission, so in `on` mode it printed the admitted subset as "generated". Now
   `generated N refined tasks, enqueued M` (visible in the matrix log above:
   `generated 2592 … enqueued 2592` under `off`, `generated 3 … enqueued 3` under
   `on`).
3. **S3 manager wiring was untested.** New `tests/test_rfg_pipeline_wiring.py`
   (6 tests) pins config → governor propagation, governed defaults for an absent
   section, the **D1 fail-safe** (construction failure yields built-in defaults in
   `on` even when the operator asked for `off`, with an ERROR log), all ten D9
   metric keys on the status surface, `{}` without a governor, and config-level
   rejection of zero/non-positive caps. Methods are exercised unbound against a
   stand-in: they touch only `config.refine_governor` and `self.refine_governor`.
4. **S4 mislabeled test.** `test_s17_early_stop_observes_governed_pages`
   implements **S18**; renamed to `test_s18_…` and the tests.md traceability row
   updated (S17 keeps its strict-failure test).
5. **S5 stale totals.** Group 5 above now shows 191 → 192 → 198 with the reason
   for each step instead of a single outdated number.
6. **S6 task 6.3** annotated in tasks.md with its recorded "NOT executed"
   decision (§6.3 here) rather than sitting as a bare checkbox.
7. **S7 README whitespace.** Two stray blank lines at the `### Failure handling`
   boundary removed; the width/volume bullets and `examples/config-full.yaml`
   comments now name wire-fingerprint order and `(> 0)` for the budget.

### Final state after the round

- `python3 -m pytest tests/` → **199 passed** in 37.6 s
  (172 pre-existing + 27 new: 12 governor unit, 9 stage integration, 6 wiring).
- Golden suites unchanged: `test_sa_fingerprint.py`, `test_qr_clean_regex.py`,
  `test_early_stop_modes.py` → 22 passed; `-k fh_` → 17 passed.
- `git diff --name-only -- tests/` → still no tracked test file modified (all RFG
  test files are new in this change).
- `search/github/refine/*` untouched (design D10); the engine was only ever read.




