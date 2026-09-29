# Tasks

TDD ordering: group 1 is the RED baseline, groups 2–7 drive every automated
scenario to green, groups 8–11 are regression, documentation, live gate and
archive.

**Wire-format rule, considered explicitly.** This change touches no external wire
format: no provider dialect, no HTTP contract, no response-body assumption. No live
staging probe is therefore required in groups 1–2. The credential material cited as
motivation comes from captures already taken (gate R8 row B5, 2026-09-28; gate R9
both runs, 2026-09-29) and is referenced, not re-captured. Every credential used by
the new fixtures is **synthetic** and labeled as such in the test module docstrings;
no live-captured secret is used as a fixture.

## 1. RED baseline

- [x] 1.1 Run `tests/test_roi_redaction.py`, `tests/test_roi_shutdown.py`, `tests/test_roi_logsink.py`, `tests/test_prt_cap_surface.py` and confirm `12 failed, 3 passed`, each failure naming the cause recorded in `tests.md` (done 2026-09-29)
- [x] 1.2 Confirm the three passes are exactly the declared preservation pins — `ROI-S8`, `PRT-S27`, `PRT-S28` — and not accidental coverage of an unimplemented behaviour
- [x] 1.3 Clean the probe files the RED run leaves in the repository's `logs/` (they are the very pollution `ROI-S13` pins against), using a guarded glob — an unmatched glob aborts the whole command under `zsh`
- [x] 1.4 Record that `ROI-S12` initially passed for a **false** reason and was corrected: importing the package runs the file-handler setup once, whose early return hides the behaviour under test, so the probe now forces `_file_handler = None` before presetting the location

## 2. Redaction at every sink (ROI-S1, ROI-S2, ROI-S3, ROI-S4)

- [x] 2.1 Add one module-level helper in `tools/logger.py` that returns `redact_api_keys_in_text(text)` and, on any exception, a fail-closed marker naming level, origin logger and source location without the payload (D2); the helper must not log, to avoid recursion inside a formatter
- [x] 2.2 Apply the helper as the last step of `ColoredFormatter.format` (`tools/logger.py:69`), the console renderer (ROI-S1, ROI-S2)
- [x] 2.3 Apply the helper as the last step of `JSONFormatter.format` (`:128`), including its plain-text fallback branch (ROI-S4)
- [x] 2.4 Apply the helper in `FileFormatterWithRedaction._redact_api_keys_in_message` (`:395`), replacing the fail-open `return message` — which publishes the secret exactly when redaction breaks — with the fail-closed marker (ROI-S3, ROI-S4)
- [x] 2.5 Route `APIKeyRedactionFormatter` (`:104`) through the same helper so its marker text and failure semantics match every other sink instead of being a fourth variant (ROI-S4)
- [x] 2.6 Verify ROI-S2 on its own terms: the credential sits in traceback text that the **renderer** adds, which is why redaction is applied to the rendered line and not to `record.msg`
- [x] 2.7 Confirm ROI-S4 enumerates at least the four shipped renderers by inspecting the module, so a fifth renderer added later without redaction fails the suite rather than shipping silently

## 3. Attached where records pass, and not emitted at the source (ROI-S5, ROI-S6)

- [x] 3.1 Remove the `RedactionFilter` attachment on the root logger (`tools/logger.py:870`) and leave a comment giving both reasons it can never fire: `setup_logger` sets `propagate = False`, and `Logger.callHandlers` walks ancestors for **handlers** only, never for filters (D4)
- [x] 3.2 Delete the `RedactionFilter` class (`:159-190`); grep shows its only reference is the attachment removed in 3.1. If any importer turns up, stop and record it as a new decision instead of deleting
- [x] 3.3 Confirm no `getLogger().addFilter` remains anywhere in the module, which is what the ROI-S5 source inspection asserts
- [x] 3.4 `search/client.py:1862` — stop interpolating header values into the failure message: report the sorted header **names** and keep URL, status code and message (D5). "Was an authorization header even sent?" stays answerable; its value is not needed
- [x] 3.5 Check the sibling debug branch that passes `traceback.format_exc()` as the message carries no header values, and leave it unchanged if clean
- [x] 3.6 Grep the repository for other log statements interpolating a headers dict or an authorization value, and fix any found; if none, record that the census was taken

## 4. The finalization boundary (ROI-S7, ROI-S8)

- [x] 4.1 Add a module-level finalization flag in `tools/logger.py` and set it inside `shutdown_logging()` **after** `flush_all_handlers()` and **before** any handler is closed — that ordering is what keeps ROI-S8 green (D6)
- [x] 4.2 Add a `logging.Filter` that returns `False` once the flag is set, and attach it to every handler `setup_logger` creates: console, per-module file, and main file (`tools/logger.py` ~`:598-608`)
- [x] 4.3 Note in a comment why this filter is attached to **handlers** while the one removed in 3.1 was attached to a logger: `Handler.handle` calls `self.filter(record)`, so a handler filter does run, whereas an ancestor logger's filter never does
- [x] 4.4 Verify ROI-S7 raises nothing into the emitting thread — a survivor thread calling `logger.warning` must neither write nor propagate an exception
- [x] 4.5 Leave `daemon=True` (`stage/base.py:280`, `:759`), the bounded join and the zombie WARNING (`:314-316`) untouched; they already detect and report survivors honestly (D6, rejected alternatives)

## 5. Teardown noise: loud once, counted always (ROI-S10, ROI-S11)

- [x] 5.1 Add `tasks_discarded_after_shutdown` to `BasePipelineStage.__init__`, initialised to zero and updated under `stats_lock` like the neighbouring counters
- [x] 5.2 `put_task` (`stage/base.py:321-325`): increment the counter on every discard, and emit the WARNING only on the **first** one, naming the condition and stating that further discards are counted rather than logged (D7)
- [x] 5.3 Report the total in the stop-completion branch of `stop()` (`:313-319`), on both the graceful path and the survivor path, so the number survives the run
- [x] 5.4 Deliberately do **not** add a new metric surface for the counter: any `*_metrics` field on `PipelineStatus` must have a renderer (`_RENDERED_METRIC_SURFACES`, main spec `run-observability` RO-S4), and a renderer is out of scope here. The total lives on the stop-completion record

## 6. The log sink belongs to the run (ROI-S12, ROI-S13, ROI-S14)

- [x] 6.1 `tools/logger.py::_setup_file_handlers` — assign `Logger._logs_dir = Path("logs")` only when it is `None` (D9). The class attribute already defaults to `None`, so the production path and the CWD-relative behaviour `main.py` and AGENTS.md §1.2 rely on are unchanged
- [x] 6.2 Confirm `_get_or_create_module_handler` and `_ensure_logs_directory` read `_logs_dir` at creation time, so the preset governs `main.log` and the per-module files alike
- [x] 6.3 Add `pytest.ini` declaring `testpaths = tests` (D10); confirm rootdir resolution is unchanged and that the root package module is no longer collected
- [x] 6.4 Add a `pytest_configure` hook in `tests/conftest.py` that redirects the sink **before** collection imports product code: preset `Logger._logs_dir` to a session directory, force `_file_handler = None`, clear `_module_handlers` / `_loggers` / `_directory_initialized`, and strip already-attached file handlers from the loggers in `logging.Logger.manager.loggerDict`
- [x] 6.5 Justify 6.4's shape in a comment: importing `tools.logger` executes `tools/__init__.py`, whose module-level `get_logger(...)` calls resolve the sink location at import time — before any fixture could run — so the redirect has to happen in a configure hook, not in a fixture, and has to undo handlers that import already created
- [x] 6.6 Verify ROI-S12 including its second half: with nothing supplied, the location still resolves to the shipped working-directory-relative `logs`
- [x] 6.7 Verify ROI-S13 across a full suite run, not only the pin itself: the repository's `logs/` gains no file
- [x] 6.8 Manual (`ROI-S14`): create a `git worktree` at `HEAD`, run collection inside it and confirm it no longer dies with `ImportError: attempted relative import with no known parent package` at `__init__.py:97`; then remove the worktree, run `git worktree prune`, and confirm no credential-bearing file was copied into it (`plan_races.md` И4 records the previous failure and the hygiene rules)

## 7. The ceiling that governs a refusal (PRT-S26, PRT-S27, PRT-S28)

- [x] 7.1 Add `_refusal_wait_surface = "gather"` to `BasePipelineStage` and make `_defer_wait_cap()` (`stage/base.py:435-444`) read that section of the config, keeping the `inf` fallback when the config is unreachable (D8)
- [x] 7.2 Override the attribute to `"provider"` in `CheckStage` (`stage/definition.py:677`) and `InspectStage` (`:941`)
- [x] 7.3 Confirm both call sites inherit the change: the clamp in `_effective_defer_wait` (`:446-448`) and the ceiling printed by `defer_task` (`:422-431`), so the number an operator reads is the number that governed
- [x] 7.4 Name the surface in the deferral record, so a printed ceiling cannot be mistaken for the other section's
- [x] 7.5 Confirm `SearchStage` and `AcquisitionStage` are untouched (PRT-S27) and that with both ceilings at the shipped default the applied wait is byte-identical to today's (PRT-S28)
- [x] 7.6 Re-check `config/validator.py:568-578` and `:602-612`: both ceilings remain validated as strictly below `queue.visibility_timeout_s`, so invariant 6 holds whichever one governs. No validator change is expected; if one becomes necessary, record it as a new decision
- [x] 7.7 Do not touch the transport-side clamp in `search/client.py` nor the R9 precedence order (D2 of `fix-provider-refusal-counters`); this task group changes only which ceiling the **stage** applies

## 8. Regression

- [x] 8.1 Run the four new files: expect 15 passed
- [x] 8.2 Run the full suite: expect **461 passed** (446 + 15), and confirm the collected count moved by exactly 15 — any other delta means `pytest.ini` changed collection and must be explained, not accepted
- [x] 8.3 Confirm `tests/test_cl_detectors.py`, `tests/test_pfc_*.py` and `tests/test_prc_*.py` pass **unmodified**: redaction and ceiling selection must not move R8/R9 behaviour
- [x] 8.4 Confirm no existing test had to be deleted. If one did, delete it rather than weaken it and record which scenario supersedes it (R8 precedent)
- [x] 8.5 Run the suite twice more for flake freedom, and confirm `tests/test_gt_stage_integration.py::test_s18_a_deferred_task_is_never_executed_twice` still passes — R9 fixed a real race there and this change must not reintroduce it
- [x] 8.6 Confirm the repository's `data/` is untouched and `logs/` gained nothing (AGENTS.md §1)

## 9. Documentation and predecessor amendments

- [x] 9.1 `README.md`: state that every sink redacts and fails closed, and that the ceiling governing a deferral is the one belonging to its surface
- [x] 9.2 `examples/config-full.yaml`: annotate `provider.max_refusal_wait_s` and `gather.max_refusal_wait_s` as per-surface ceilings rather than one global bound
- [x] 9.3 `AGENTS.md`: note that a suite run no longer writes `logs/` into the repository and that collection is scoped by `pytest.ini` — which is what makes reproducing a measurement at a parent commit possible
- [x] 9.4 `plan.md`: move II.13, II.8, II.9, II.14 and II.15 to closed, add the round card, and refresh the I.8 registry counts, II.12 table and IV.1/IV.2
- [x] 9.5 Amend `plan.md` II.14 and `plan_races.md` П1 append-only for the D8 correction: `defer_task` **does** print the applied ceiling (`stage/base.py:422-431`); what it never revealed was that ceiling's **source**, which is why two equal defaults hid the coupling
- [x] 9.6 Record in `plan_races.md` the import-time finding that shaped 6.4/6.5: `import tools.logger` executes `tools/__init__.py`, whose module-level `get_logger(...)` calls resolve the sink location before any fixture can run

## 10. Live gate

- [x] 10.1 Write the gate script under `/tmp/opencode/`, with the run root under `/var/tmp`, `cd "$RUN"` before launching, `setsid --wait nohup timeout -k 30 <SOAK+300>`, and guarded globs (AGENTS.md §1)
- [x] 10.2 Reference the repository `config.yaml` by absolute path and never copy it, so no credential-bearing duplicate is created (R9 practice: zero operator credentials in evidence before masking)
- [x] 10.3 Soak: confirm throughput is unchanged against the R9 baseline (8.50 / 8.52 req/s), the `ProviderRefusals:` counters are unchanged, and deferral lines now name the governing ceiling
- [x] 10.4 Secret-scan the console capture and require **zero** harvested `sk-` occurrences in it — R9 found 7–8 there and 2844–2927 in payload. This is the acceptance evidence for ROI-S1 and ROI-S6 under real traffic
- [x] 10.5 Durability drill for ROI-S9: graceful stop, then `kill -9 -PGID` with claims in flight, then restart; require exit **0** with no `Fatal Python error: _enter_buffered_busy`, `claimed=0` on every queue, `integrity_check=ok`, and reclaim equal to in-flight (precedent: gate R8 row B6)
- [x] 10.6 Confirm the capture still ends with the full final status block — suppression must not eat the run's report (ROI-S8 under real traffic)
- [x] 10.7 Confirm a teardown discard, if one occurs, appears exactly once per stage with a total, instead of once per task
- [x] 10.8 Hygiene: distil evidence, mask, delete run data, and confirm `df -h /tmp` returns to its pre-task level
- [x] 10.9 Never run pytest concurrently with the gate (AGENTS.md §2)

## 11. Verification and archive

- [x] 11.1 Write `verification.md` mapping every scenario — including both Manual entries — to its evidence
- [x] 11.2 Record every deviation discovered during implementation as a new numbered decision in `design.md`, append-only; never a silent edit contradicting shipped code
- [x] 11.3 Sync the deltas into `openspec/specs/`: create `run-output-integrity` from the ADDED delta (carrying `## Purpose` across), and replace Requirement 2 inside `provider-refusal-taxonomy` with the MODIFIED block, leaving every other requirement and scenario byte-identical. No blockquotes — no main spec in this repository uses them
- [x] 11.4 `openspec validate --specs --strict` → all specs pass; then `openspec archive fix-run-output-hygiene --skip-specs -y` so the delta cannot be applied twice
- [x] 11.5 Commit, and confirm `git status --short` shows only `?? examples/harvester.service`
