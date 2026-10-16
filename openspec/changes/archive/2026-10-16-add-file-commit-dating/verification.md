# Verification — add-file-commit-dating

Date: 2026-10-16. Schema `tdd-flow`. Suite before the change: **475 passed**; after: **559 passed**.
Two live gates were run against the production-shaped configuration (four runs + three runs, 300 s each).

## 1. Summary

| Check | Result |
|---|---|
| Artifacts complete (`openspec status`) | 5/5, `openspec validate --type change --strict` valid |
| Tasks | 66 total; groups 1–13 complete, group 14 in progress at the time of writing |
| RED baseline | Established **twice**, non-destructively, in throwaway `git worktree` checkouts of HEAD (`8c5cd60`) |
| Full suite | **559 passed, 0 failed** — three consecutive runs before the gate (544 at that point) and two after (559) |
| Repository hygiene | `logs/` and `data/` contain **0 files** after every run |
| Live gates | 7 runs, all `exit=0`, `Overall Status: Healthy`, `Critical Alerts: 0`, `degraded=0`, zero banned signatures |
| Credentials | Zero occurrences of any live credential **value** in the repository, the fixtures, the plans and both run roots (post-destruction scan) |
| Defects found by the gate | **5** — all fixed, all pinned by a test, all recorded as design amendments D14–D18 |

## 2. RED baseline

The schema requires the new tests to fail for the expected reason before implementation. Two
sub-agents wrote implementation and tests in the same pass, so the RED state was reconstructed
afterwards and **non-destructively**: a detached `git worktree` at HEAD (`8c5cd60`) received copies
of the new/extended test files only, and pytest ran there.

Note on method: the worktree directory name must be a valid Python identifier. A first attempt under
`/var/tmp/opencode-fcd-red_<TS>` produced 98 spurious `ImportError: attempted relative import with no
known parent package` errors because the hyphenated directory broke the root package import — exactly
the failure mode `AGENTS.md` §4 and lesson И4 describe. Renaming to `/var/tmp/fcd_red_baseline` gave a
clean measurement.

| Worktree run | Result | Reasons observed |
|---|---|---|
| Layer 1 (store, config, fetch, registry, migration, date flow, gather transport) | **23 failed, 62 passed, 13 errors** | `ImportError: cannot import name 'FileDatingConfig' from 'config.schemas'`; missing `storage.file_meta`; `collect() got an unexpected keyword argument 'etag'`; `AttributeError` on the new client symbols; assertion failures for the monotonic-date, migration and S30–S35 cases |
| Layer 2 (pipeline wiring, stage integration, rendering) | **5 failed, 7 passed, 2 errors** | missing `StageResources.file_dating`, missing `PipelineStatus.file_dating_metrics`, absent `Dating:` renderer |

The 62 and 7 passing cases are the pre-existing tests inside the extended files, which is the expected
shape: only new behaviour failed. No fixture, syntax or collection bug appeared in either run. Both
worktrees were removed afterwards (`git worktree list` shows only the main checkout).

## 3. GREEN and suite reconciliation

| Stage | Total | Note |
|---|---|---|
| Before the change | 475 | Recorded in the R11 verification |
| After groups 3–11 | **544** | +69: `test_fcd_store` 11, `test_fcd_config` 20, `test_fcd_fetch` 12, `test_fcd_pipeline_wiring` 2, `test_fcd_stage_integration` 5, `test_fcd_render` 2, extensions to `test_registry_core` 3, `test_registry_migration` 2, `test_date_flow` 4, `test_gt_transport` 6, plus one adjusted key-set assertion in `test_gt_stage_integration` |
| After the gate fixes F1–F5 | **559** | +15 in `tests/test_gate_fixes_f1_f5.py` |

Three consecutive pre-gate runs returned identical totals (544, 90.8 s each) and two post-gate runs
returned identical totals (559, 92.0/91.6 s), with `logs/` and `data/` empty after every one.
One pre-existing test needed an update and was **not** weakened: `test_s21_transport_economics_are_observable`
asserts an exact set of gather stat keys, and design D12 legitimately added `gather_304` to it.

## 4. Scenario → evidence

All 35 new automated scenarios and the 13 re-pinned ones are GREEN; `tests.md` carries the row-level
mapping. Grouped by file:

| File | Scenarios | Count |
|---|---|---|
| `tests/test_fcd_store.py` | `file-commit-dating-S14, S16, S17, S18, S19` + fail-open | 11 |
| `tests/test_fcd_fetch.py` | `file-commit-dating-S1, S2, S4, S5, S6, S8, S9, S12, S13, S15` | 12 |
| `tests/test_fcd_config.py` | `file-commit-dating-S7, S22` + inert defaults + `github_graphql` bucket | 20 |
| `tests/test_fcd_pipeline_wiring.py` | `file-commit-dating-S20, S11` | 2 |
| `tests/test_fcd_stage_integration.py` | `file-commit-dating-S3, S10, S21, S23, S24` | 5 |
| `tests/test_fcd_render.py` | `file-commit-dating-S25, S26` | 2 |
| `tests/test_registry_migration.py` | `link-registry-S4, S5` | 2 |
| `tests/test_registry_core.py` | `link-registry-S3, S10, S11` | 3 |
| `tests/test_date_flow.py` | `date-extraction-S7, S9, S12` (+ re-pinned S10, S11) | 4 |
| `tests/test_gt_transport.py` | `gather-transport-S30 … S35` | 6 |
| `tests/test_gate_fixes_f1_f5.py` | pins for D14–D18 | 15 |
| MANUAL | `link-registry-S6` (older build reads a newer database) — approximated by S4/S5, not equivalent | 1 |

Fixtures under `tests/fixtures/file_dating/` (16 files) are live captures with provenance sidecars
naming endpoint, capture date, auth mode and status: four REST `commits?path=` responses, one batched
GraphQL response (`rateLimit.cost = 1`), the `Blob.committedDate` counter-example (`200` carrying
`cannotSpreadFragment` + `undefinedField`), and a raw `200`/`304` pair proving the conditional answer
has an empty body. Synthetic vectors in the test files are labelled as such.

## 5. Live field verification

Method: run roots under `/var/tmp` (never `/tmp`), each arm in its own workspace directory, launched
from a script file with `setsid nohup`, wrapped in `timeout -k 30 600`, invoked as
`main.py -c <cfg> --timeout 300 --stats-interval 30`. Arm configurations were produced by a structured
YAML edit of the live `config.yaml` and **proved** semantically identical except for the intended keys
(`PROOF arm_X identical-except-intended-keys: True`), then passed `main.py --validate`, then were
destroyed inside the script. Arms ran sequentially — they share credentials and baskets. No `pytest`
ran while a gate was active (`AGENTS.md` §2).

### 5.1 First gate — four arms, 15:24–15:52

| Figure | arm A (inert) | arm B (`rest`) | arm B2 (2nd run, same ws) | arm C (`graphql`) |
|---|---|---|---|---|
| exit / status | 0 / Healthy, 0 alerts | 0 / Healthy | 0 / Healthy | 0 / Healthy |
| console | 1 744 967 B / 6 959 ln | 1 120 513 B / 4 518 ln | 2 416 273 B / 9 055 ln | **61 978 B / 421 ln** |
| `Refine:` | gen=21312 adm=144 wire=21168 uniq=144 | identical | identical | **gen=144 adm=36** |
| `Aggregation:` | hits=111 misses=37 entries=37 | identical | identical | **misses=1** |
| `Gather:` | raw req=2544 bytes=80 057 116 defer[budget=1165] | req=869 bytes=20 332 009 | req=2466 bytes=59 279 985 | *(never ran)* |
| `Dating:` | *(absent — correct)* | req=40 filled=40 absent=0 budget_stop=188 fill=1.0000 | req=40 filled=40 budget_stop=192 | **req=0** filled=100 batches=25 cost=25 |
| `links` / `gathered_ok` | 3 071 / 2 323 | 3 071 / 2 382 (B+B2) | — | **0** |
| `fill_file_commit_date` | 0.0000 | **0.0000** ← defect | — | 0.0000 |
| `fill_etag` | 0.7564 | 0.7756 | — | 0.0000 |
| `file_meta` rows (dated) | 0 | 73 (73) | — | 62 (62) |
| gather queue rows / distinct | 9 740 / 3 038 (3.206×) | 21 233 / 3 071 (6.914×) | — | 0 |
| harvest records / unique | 17 760 / 3 429 | 33 447 / 3 431 | — | 100 / 100 |
| banned signatures | none | none | none | none |

The refine invariant `gen=21312 adm=144 refused[wire=21168] uniq_wires=144` reproduced exactly in
arms A, B and B2, i.e. this change does not perturb the R11 guard.

**Five defects found, all fixed (design D14–D18):** dates never reached `links` (path-prefix mismatch);
scope was not enforced at the call site (arm C dated 100 finding-less files under `scope=keyed`);
dating blocked the single search thread (arm C completed one search task in 300 s); the published
request counter ignored the batched transport (`req=0` against 25 POSTs); `gather_304` was published
but never rendered and the behaviour had no rollback flag.

### 5.2 GraphQL cost scaling — the open question of design D3, answered

A dedicated probe issued three batched POSTs spaced 6 s apart over 60 real targets read from the
surviving gather queue:

| Batch size N | results | filled | `rateLimit.cost` values | elapsed |
|---|---|---|---|---|
| 4 | 4 | 4 | `[1]` | 0.8 s |
| 20 | 20 | 20 | `[1]` | 2.3 s |
| 50 | 50 | 50 | `[1]` | 5.0 s |

Client-side counters after the probe: `batches=3`, `file_dating_graphql_cost=3`. **Cost is one point
per batch and does not scale with N up to 50**, so the ceiling `graphql_batch_max = 50` is safe and
larger batches are strictly cheaper per file. Every date matched the REST form byte-for-byte
(`2021-05-29T14:24:14Z`, `2018-06-04T12:59:03Z`, `2011-06-30T19:18:15Z`, `2021-07-09T18:30:09Z`).

### 5.3 Confirmation gate — three arms, 17:01–17:22

| Figure | arm D (`rest`, run 1) | arm D2 (2nd run, same ws) | arm E (`graphql`, basket 0.5) |
|---|---|---|---|
| exit / status | 0 / Healthy, 0 alerts | 0 / Healthy | 0 / Healthy |
| console | 1 445 730 B / 5 842 ln | 1 817 272 B / 7 054 ln | **2 010 000 B / 7 686 ln** |
| `Refine:` | gen=21312 adm=144 wire=21168 | identical | **gen=21312 adm=144 wire=21168** ← starvation gone |
| `Gather:` | req=2194 bytes=68 466 765 **304=213** | req=2342 bytes=**34 134 871** **304=1475** | req=2273 bytes=70 985 562 **304=256** |
| `Dating:` | req=40 filled=40 budget_stop=52 cached=0 fill=1.0000 | req=38 filled=38 budget_stop=0 | **req=40** filled=40 **batches=40 cost=40** budget_stop=51 cached=1 |
| `fill_file_commit_date` | **0.0252** ← propagation fixed | (same db) | **0.0130** |
| `fill_etag` | 0.9211 | (same db) | 0.6571 |
| `file_meta` rows (dated) | 78 (78) | (same db) | 40 (40) |
| `links` / `gathered_ok` | 3 091 / 2 847 | — | 3 068 / 2 016 |
| harvest records / unique | 34 689 / 3 493 | — | 17 428 / 3 366 |
| refusals / secondary limits | all 0 | all 0 | all 0 |
| banned signatures | none | none | none |

Reading of the two headline results:

- **Conditional re-gather works and pays immediately.** In the second run over the same workspace
  **1 475 of 2 342 requests (63.0 %) were answered `304` with an empty body**, and transferred bytes
  fell from 68.5 MB to 34.1 MB *while the request count rose* — mean payload per request dropped from
  31.2 KB to 14.6 KB (**−53.2 %**). Notably 213 short-circuits already occur in a *first* run, because
  intra-run URL duplication is ~3.2×: the first fetch stores the token and later duplicates reuse it.
- **Dating fills the column it exists for.** `links.file_commit_date` went from 0.0000 in every
  historical run to 0.0252 (arm D) and 0.0130 (arm E) within a single 300 s run bounded by
  `max_requests_per_run: 40`; `fill=1.0000` means every request that was issued produced a date.
  Extrapolated: the ceiling, not the mechanism, is what bounds coverage — 40 requests dated 40 files
  out of ~3 000 discovered, and `budget_stop=52` records the demand that was turned away.

Search-side neutrality holds: `Refine`, `Aggregation` and `Skip` figures in arms D/D2/E match the
inert arm A within run-to-run noise, and no arm recorded a rate-limit, quota, auth or transient
refusal, a secondary-limit incident, or a degraded run.

### 5.4 Registry migration on a real version-4 database

Task 13.6 named the run-№4 registry, but that run root had already been cleaned up, so a stronger
substitute was used: a database **created by the previous release itself**. A detached worktree at
HEAD (`8c5cd60`, `SCHEMA_VERSION = 4`) built a registry with 40 links, 25 `gathered_ok` and two dated
rows; the shipped build then opened the same file.

| | HEAD build (before) | shipped build (after) |
|---|---|---|
| `PRAGMA user_version` | 4 | **5** |
| `links` columns | 16 | **18** (`etag`, `etag_transport` appended) |
| tables | keys, link_coverage, links, meta, repos, runs | **+ file_meta** |
| links / gathered_ok | 40 / 25 | **40 / 25** (unchanged) |
| dated row values | `1757593673.2609053`, `1446553673.2609053` | **byte-identical** |
| reopen ×2 | — | `user_version` 5, 40 links, 18 columns — idempotent |

## 6. Deviations and amendments

1. **D14–D18** (see `design.md`): the five gate-found defects and their fixes, recorded as numbered
   amendments rather than silent edits.
2. **Probe budget exceeded by two requests.** Task 2.1 capped the staging probe at 14 requests; 16 were
   issued because the raw `200`/`304` pair needed two anonymous requests that do not touch API quota.
   No secondary limit was provoked (`secondary_limit_incidents=0` in all seven runs).
3. **RED reconstructed, not observed inline** for two of the four implementation passes (§2). The
   worktree method is sound but the ordering deviates from strict TDD; the fix is procedural and is
   recorded here rather than papered over.
4. **Task 13.6's named artefact no longer existed**; a HEAD-built version-4 database was used instead
   (§5.4), which is stricter evidence than the original plan.
5. **Arm workspaces are shared between an arm and its repeat run** (B/B2 and D/D2), so all database
   figures for those pairs are cumulative; only console-derived figures are per run. The analyzer
   initially looked for a non-existent `arm_B2/` workspace and reported empty harvest data for it —
   an analysis-script bug, not a product bug, corrected before the confirmation gate.
6. **Batching is currently defeated by the call pattern.** The gather hook dates one URL at a time, so
   arm E issued `batches=40 cost=40` for 40 files — one point per file instead of one point per four.
   The capability is correct and measured (§5.2); accumulating a pending set and dating it in batches
   is a follow-up worth roughly 4× on the GraphQL bill. Recorded as a deferral, not hidden.
7. **`gather_304` counting nuance:** a `304` is counted as a successful gather and advances recency,
   but produces no shard record and no key rows, exactly as spec S31 requires; arms D/D2 confirm the
   harvest totals stay consistent with that (unique values 3 493 across two runs).

## 7. Hygiene and security

- Run roots: `/var/tmp/opencode-fcd-gate_20261016T151851` (49 M) and `…_confirm` (47 M), each holding
  distilled evidence (`00-live-config-md5`, `10-arm_*-status`, `20-extract.json`, `30-providers-before-delete`,
  `40-graphql-cost`, `90-hygiene`, `91-hygiene-post`, `df_tmp_before`). `data/providers/**` was deleted
  **only after** distilling sizes and counts, per `AGENTS.md` §1.3.
- Credential scans were performed **by value** (all eight live tokens/sessions compared against every
  file in both roots), not by filename glob — the lesson of И8, where a `.bak-<ts>` copy survived a
  `*.yaml` check. Post-destruction scan: **VALUE-hits NONE, FORM-hits NONE, config-shaped files NONE**
  in both roots. The in-script scan intentionally runs before the `rm`, so its output lists the arm
  configs as present; `91-hygiene-post.txt` is the authoritative one.
- Arm configs were created with `chmod 600` inside `chmod 700` run roots (containment layer L1) and
  destroyed by the script; both scripts verified destruction explicitly (`D=gone E=gone`).
- `/tmp` ended at 44 M / 2 %, below its pre-task level. No credential was ever printed; the fixtures
  contain response bodies and non-secret response headers only, never request headers or cookies.
- Repository state after all work: `logs/` and `data/` contain 0 files; `git status` shows only the
  intended modifications plus the pre-existing untracked `examples/harvester.service` (plan item II.11.1,
  deliberately excluded from this change).

## 8. Deliberately not done

- `enrichment.enabled` stays **false**. Repository-level `pushed_at` remains a separate subsystem; the
  measured B−C gaps (11 / 35 / 311 / 4 561 days) show it cannot answer "new leak or old leak".
- `skip.skip_known` stays **shadow**; promoting it is a separate operator decision so the two effects
  are not mixed in one measurement.
- `keys.source_url_hash` still collapses "the same key found in two files" into one ledger row. Real
  defect, adjacent scope, filed in `plan.md` — not fixed here.
- No change to query splitting or refine depth for either transport.
- Scope widening beyond `keyed` is priced but not enabled: at ~9 req/min through a shared basket,
  dating all 3 029 discovered files would take ~5.6 h, which is why the per-run ceiling exists.
