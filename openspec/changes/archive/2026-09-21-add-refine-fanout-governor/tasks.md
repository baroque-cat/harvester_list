# Tasks: add-refine-fanout-governor

## 1. RED baseline & early live probes

- [x] 1.1 Run `tests/test_rfg_governor.py` and `tests/test_rfg_stage_integration.py`; confirm both fail collection with the expected ImportErrors (`search.refine_governor`, `RefineGovernorConfig`) exactly as recorded in tests.md; run the pre-existing suite (`python3 -m pytest tests/ --ignore=tests/test_rfg_governor.py --ignore=tests/test_rfg_stage_integration.py`) and confirm 172 passed (staleness audit: nothing to delete or repair — recorded 2026-09-21)
- [x] 1.2 Guardrails before any tooling (plan_agr.md §3.4): snapshot `logs/`; credentials ONLY via env `GITHUB_SESSIONS`/`GITHUB_TOKENS` (support at `config/loader.py:174-182`); never write secrets into files; tools override logging to their own directory
- [x] 1.3 Early live probes (wire-format rule; pilot data of 2026-09-21 exists — formalize): against real GitHub (api.github.com/search/code, Bearer): root total for `/sk-[a-zA-Z0-9]{32}/` plus totals for ≥5 refined children spanning the alphabet (include a hot prefix like `"sk-000"` — measured 75 136 — and cold ones like `"sk-i00"` — 24); save raw responses as fixtures under `tests/fixtures/live_2026_09_rfg_*.json` WITH provenance headers (endpoint, capture date, auth mode); re-run the offline generator probe (partitions 64/1000/46 007 → children counts, wall time, peak RSS) and record numbers in verification notes

## 2. Model and configuration surface

- [x] 2.1 Add additive `refine_depth: int = 0` to `SearchTask` (`core/models.py:104-140`): include in `_serialize_data`, read via `data.get("refine_depth", 0)` in `_deserialize_data` (old queue JSON loads as roots); optional kwarg in `TaskFactory.create_search_task` (`stage/factory.py:27-48`)
- [x] 2.2 Add `RefineGovernorConfig` dataclass to `config/schemas.py` following the `AggregationConfig` precedent (`:511-527`): `mode: str = "on"`, `max_refine_depth: int = 2`, `max_partitions_per_refine: int = 128`, `max_search_tasks_per_run: int = 10000`; add `refine_governor` field to `Config` (`:679` neighborhood)
- [x] 2.3 Parse the `refine_governor:` section in `config/loader.py` (precedent `:146-148`, `:426+`); validate loudly in `config/validator.py` (precedent `:455-472`): unknown mode → ERROR, non-positive caps → ERROR, `max_refine_depth > 5` → ERROR (sanity)
- [x] 2.4 Add `refine_metrics: Dict[str, Any]` to `PipelineStatus` (`core/metrics.py:210` neighborhood, beside `aggregation_metrics`)
- [x] 2.5 Drive `test_s3_depth_serializes_and_legacy_defaults_to_zero` GREEN (round-trip + legacy fallback)

## 3. RefineGovernor core (new module `search/refine_governor.py`)

- [x] 3.1 Implement the contract pinned by `tests/test_rfg_governor.py` docstring: `RefineGovernor(mode, max_refine_depth, max_partitions_per_refine, max_search_tasks_per_run)`; `may_refine(depth)`; `clamp_partitions(raw)`; `select_children(provider, parent_query, queries, transport_limit, total)`; `record_depth_cap_pagination()`; `metrics` snapshot — single `threading.Lock`, WorkerManager-safe (design D1/D4)
- [x] 3.2 Deterministic admission (design D3/D5): sort candidates by `search/querykey.fingerprint` order (stable, provider-independent), truncate to the clamped cap, admit within remaining budget; compute per-parent `coverage_estimate = min(1.0, admitted×limit/max(total,1))` and aggregate min/avg (design D8)
- [x] 3.3 Mode semantics (design D6): `off` — passthrough preserving input order, counters inert; `shadow` — passthrough enqueue while every would-be clamp/depth/budget refusal is computed, counted and logged with identical deterministic order; `on` — enforce; refusals always log WARNING naming provider, parent query and reason (never silent)
- [x] 3.4 Drive `tests/test_rfg_governor.py` fully GREEN (S6, S7, S9, S12, S13, S15 + aux gates)

## 4. Stage integration and pipeline wiring

- [x] 4.1 Add optional `refine_governor: Any = None` field to `StageResources` (`stage/base.py:48-53` pattern)
- [x] 4.2 Consult the governor in `_handle_first_page_results` (`stage/definition.py:333-401`): depth gate BEFORE refining — at cap fall through to the existing pagination branch and call `record_depth_cap_pagination()`; pass `clamp_partitions(ceil(total/limit))` into `generate_queries` (bounds engine materialization at the source); run the returned list through `select_children`; create admitted children with `refine_depth=task.refine_depth+1` and parent attribution verbatim (provider/patterns/use_api); page tasks inherit depth unchanged; missing governor (None) behaves as `off` (fail-open compatibility for stage-level unit harnesses)
- [x] 4.3 Wire `Pipeline._configure_refine_governor(config)` following `_configure_aggregation` (`manager/pipeline.py:190-217`) with the D1 fail-safe deviation: construction error → governor with built-in defaults in `on` mode + loud error log (never fall back to unbounded)
- [x] 4.4 Expose metrics via a `get_refine_metrics()` accessor mirroring `get_aggregation_metrics` (`manager/pipeline.py:18`) into `PipelineStatus.refine_metrics`
- [x] 4.5 Drive `tests/test_rfg_stage_integration.py` fully GREEN (S1, S2, S4, S5, S8, S10, S11, S14, S16, S17, S18)

## 5. Regression

- [x] 5.1 Both new test files fully GREEN
- [x] 5.2 Full suite GREEN (`python3 -m pytest tests/`): ≥172 pre-existing + all new; explicitly verify search-aggregation golden vectors (`test_sa_fingerprint.py`), query-refinement pins (`test_qr_clean_regex.py`), failure-handling modes (`test_fh_*`), early-stop modes (`test_early_stop_modes.py`) — none pin fan-out volume, so no edits expected; if any assertion breaks, treat it as a defect of this change, not of the test
- [x] 5.3 Re-confirm the staleness audit: no test deleted or weakened by this change (operator instruction: outdated tests would be fixed/removed — none were found; keep it that way)

## 6. Live verification that the governor works

- [x] 6.1 Offline mode-matrix E2E (mocked transport, temp workspace, own logging dir): `off`/`shadow`/`on` × {root with total≈46M, depth-cap child with total≈2M} — assert child counts (unclamped vs capped), counter deltas, queue sizes bounded, shadow enqueue-parity with off
- [x] 6.2 Short REAL-GitHub harvester run (env credentials from gitignored `.secrets`; current production `config.yaml` shape: 4 API providers, incident seed) under `mode: on`: search queue stays ≤ budget + roots + pages (no march toward millions), process RSS flat (watch via /proc or systemd status; no GB-scale growth), `refine_metrics` shows generated/admitted/refused + coverage_estimate, harvest non-zero (links discovered, gather/check flowing), NO `lost task during persistence` storm; capture before/after status snapshots
- [x] 6.3 Optional operator gate: one `shadow` cycle to measure the would-be coverage price on live totals before relying on `on` defaults; record the decision either way — **decision recorded: NOT executed** (rationale in `verification.md` §6.3: a live shadow cycle would re-create the 46 656-child inflation into the 4M-slot queue to measure a price already quantified offline; left as an explicit operator gate before any cap tightening)
- [x] 6.4 Write `verification.md` in the change directory: probe fixtures provenance, offline matrix results, live-run metrics/RSS evidence, residuals (honest gaps, e.g. totals drift between runs)

## 7. Docs & rollout

- [x] 7.1 README: `refine_governor` section — modes, default caps with the 2026-09-21 empirical basis (46 656 children/0.13 s; child totals 24…75 136; 85 h enumeration math), coverage-tradeoff explanation, `refine_metrics` glossary, rollback = flip to `off`, relationship to env seatbelts `REGEX_MAX_QUERIES`/`REGEX_MAX_DEPTH` (defense in depth, superseded as policy)
- [x] 7.2 README operations guidance: with the governor bounding inflow (budget ≪ queue), `queue_sizes.search` MAY be lowered back to sane values (e.g. 100 000) — but only together with R2 (`fix-queue-persistence-under-load`) until put-on-Full semantics are fixed; cross-reference plan.md roadmap
- [x] 7.3 `examples/config-full.yaml`: commented `refine_governor:` block with all four fields explained
- [x] 7.4 `openspec validate --strict` passes; every automated scenario GREEN and Manual entries executed/documented; change ready for archive

## 8. Verification round (post-implementation audit, operator-ordered fixes)

An independent `/opsx-verify` audit found 0 CRITICAL, 2 WARNING and 7 SUGGESTION items. All were fixed on operator instruction before spec sync and archive.

- [x] 8.1 **W1** — admission order brought to spec: `select_children` sorts by ascending `search/querykey.fingerprint(query, use_api)` (spec requirement 2, design D5) instead of lexicographically. New trailing keyword `use_api: bool = True` keeps the pinned positional signature intact; the stage passes `task.use_api`. Design D3 amended (it contradicted D5). Measured motivation: at cap 128 on the incident seed, lexicographic truncation dropped every `w x y z` child on every run (~11 % permanent blind spot; overlap with fingerprint order 112/128). Unit pins updated (`ADMITTED_API`/`ADMITTED_WEB`, plus explicit assertions that the order is not lexicographic and that the transport participates in the hash)
- [x] 8.2 **W2** — non-positive caps now fail configuration validation loudly (spec requirement 5): `RefineGovernorConfig.__post_init__` and `ConfigValidator._validate_refine_governor_config` require `max_search_tasks_per_run > 0`. `RefineGovernor` itself stays tolerant of 0 so S8 can construct an already-exhausted budget directly; the S8 harness's descriptive config mirror floors it at 1
- [x] 8.3 **S1** — `off` returns the generator list verbatim (no empty/self pre-filter inside the governor) so the stage's pre-existing filters and their WARNINGs still fire: rollback is byte-equivalent in logs as well as in tasks. `shadow`/`on` keep filtering so counters describe real enqueues. Pinned by `test_off_leaves_stage_filters_authoritative`
- [x] 8.4 **S2** — the stage INFO line no longer under-reports: `generated N refined tasks, enqueued M` distinguishes engine output from what was actually admitted and passed the filters
- [x] 8.5 **S3** — new `tests/test_rfg_pipeline_wiring.py` (6 tests) pins the manager half that nothing covered: config → governor propagation, governed defaults for an absent section, the D1 fail-safe fallback on construction failure (a requested `off` must not survive a wiring error), all ten D9 metric keys on the status surface, `{}` without a governor, and config-level rejection of zero/non-positive caps
- [x] 8.6 **S4** — `test_s17_early_stop_observes_governed_pages` renamed to `test_s18_early_stop_observes_governed_pages` (it implements S18); tests.md traceability row updated to match
- [x] 8.7 **S5/S6/S7** — verification.md test totals corrected (191 → 192 → 198 with the reason for each step) and deviations 2–3 re-marked RESOLVED; task 6.3 annotated with its recorded "not executed" decision; two stray blank lines removed at the README `### Failure handling` boundary
- [x] 8.8 Docs realigned with W1/W2: README width/volume bullets now name wire-fingerprint order with the measured blind-spot evidence and show `(> 0)` for the budget; `examples/config-full.yaml` comments likewise
- [x] 8.9 Full suite re-run green after the fixes: **199 passed** in 37.6 s (172 pre-existing + 27 new: 12 governor unit, 9 stage integration, 6 pipeline wiring); golden suites (`test_sa_fingerprint`, `test_qr_clean_regex`, `test_early_stop_modes`, `-k fh_`) unchanged; `git diff --name-only -- tests/` still shows no tracked test file modified; offline mode matrix re-run → `ALL MATRIX ASSERTIONS PASSED` with identical counts, and a cap-128 probe confirms the admitted list now equals fingerprint order (first-chars `w x y z` covered again) with `coverage_estimate_min` unchanged
