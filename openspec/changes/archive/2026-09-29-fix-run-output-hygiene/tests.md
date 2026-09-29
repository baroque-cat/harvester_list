# Test Plan

Derived mechanically from the delta specs under `specs/`. One scenario = one
automated test, or one Manual entry with a reason. Scenario IDs are stable and
append-only.

Observed RED run (2026-09-29, before any implementation):
`12 failed, 3 passed` across the four new files. The three passes are declared
below as **preservation pins** — they are expected to hold both before and after
the change, and exist so the fix cannot be bought by moving a different ceiling or
by breaking flush ordering.

## Traceability

| Scenario ID | Spec | Requirement | Scenario | Test file | Status |
|-------------|------|-------------|----------|-----------|--------|
| `ROI-S1` | `specs/run-output-integrity/spec.md` | Every operator-visible sink redacts credential material | a credential logged to the console never reaches it | `tests/test_roi_redaction.py` | RED |
| `ROI-S2` | `specs/run-output-integrity/spec.md` | Every operator-visible sink redacts credential material | a credential inside an attached traceback is redacted too | `tests/test_roi_redaction.py` | RED |
| `ROI-S3` | `specs/run-output-integrity/spec.md` | Every operator-visible sink redacts credential material | a redaction failure publishes a marker, not the payload | `tests/test_roi_redaction.py` | RED |
| `ROI-S4` | `specs/run-output-integrity/spec.md` | Every operator-visible sink redacts credential material | no sink in the logging layer can ship without redaction | `tests/test_roi_redaction.py` | RED |
| `ROI-S5` | `specs/run-output-integrity/spec.md` | Redaction is attached where records actually pass | a filter on the ancestor logger never sees a pipeline record | `tests/test_roi_redaction.py` | RED |
| `ROI-S6` | `specs/run-output-integrity/spec.md` | Redaction is attached where records actually pass | a failed request reports header names and no header value | `tests/test_roi_redaction.py` | RED |
| `ROI-S7` | `specs/run-output-integrity/spec.md` | Nothing is written to a sink after the run has begun closing it | a record emitted after finalization began produces no write | `tests/test_roi_shutdown.py` | RED |
| `ROI-S8` | `specs/run-output-integrity/spec.md` | Nothing is written to a sink after the run has begun closing it | output produced before finalization is still flushed | `tests/test_roi_shutdown.py` | GREEN (preservation pin) |
| `ROI-S9` | `specs/run-output-integrity/spec.md` | Nothing is written to a sink after the run has begun closing it | a surviving worker does not turn a clean stop into a failure status | — | MANUAL |
| `ROI-S10` | `specs/run-output-integrity/spec.md` | Work discarded during teardown is loud once and counted always | the first teardown discard is reported and later ones are counted | `tests/test_roi_shutdown.py` | RED |
| `ROI-S11` | `specs/run-output-integrity/spec.md` | Work discarded during teardown is loud once and counted always | the discard total is reported with the stop-completion record | `tests/test_roi_shutdown.py` | RED |
| `ROI-S12` | `specs/run-output-integrity/spec.md` | A test run writes to its own sink and collects only its own tests | a supplied sink location survives logging setup and is used | `tests/test_roi_logsink.py` | RED |
| `ROI-S13` | `specs/run-output-integrity/spec.md` | A test run writes to its own sink and collects only its own tests | a test run leaves no log file in the repository | `tests/test_roi_logsink.py` | RED |
| `ROI-S14` | `specs/run-output-integrity/spec.md` | A test run writes to its own sink and collects only its own tests | collection from the repository root does not include the root package module | — | MANUAL |
| `PRT-S26` | `specs/provider-refusal-taxonomy/spec.md` | A published wait is handed to the stage, never slept in the transport | a provider refusal is bounded by the provider ceiling, not by another surface's | `tests/test_prt_cap_surface.py` | RED |
| `PRT-S27` | `specs/provider-refusal-taxonomy/spec.md` | A published wait is handed to the stage, never slept in the transport | a code-hosting-surface refusal keeps its own ceiling | `tests/test_prt_cap_surface.py` | GREEN (preservation pin) |
| `PRT-S28` | `specs/provider-refusal-taxonomy/spec.md` | A published wait is handed to the stage, never slept in the transport | the applied wait is unchanged while the two ceilings are equal | `tests/test_prt_cap_surface.py` | GREEN (preservation pin) |

Scenarios `PRT-S6`, `PRT-S7`, `PRT-S8` and `PRT-S17` of the modified requirement
are preserved unchanged and stay covered by `tests/test_pfc_defer.py`,
`tests/test_pfc_config.py` and `tests/test_prc_transports.py`. No existing test is
made obsolete by this change, so none is deleted.

## Automated

### File: `tests/test_roi_redaction.py`

Describe: run-output-integrity — credential material never reaches a sink

- [ ] `ROI-S1` — it("a credential logged to the console never reaches it") <!-- WHEN a record containing a harvested credential is emitted through a category logger while the real console sink is captured THEN the captured text holds no form of the credential and the rest of the line still renders -->
- [ ] `ROI-S2` — it("a credential inside an attached traceback is redacted too") <!-- WHEN a record is emitted with exception information whose traceback contains a credential THEN the traceback survives with the credential redacted — the case a record-level filter cannot cover -->
- [ ] `ROI-S3` — it("a redaction failure publishes a marker, not the payload") <!-- WHEN redaction of a rendered line raises THEN the sink receives a marker naming level, origin logger and source location, and no part of the unredacted line -->
- [ ] `ROI-S4` — it("no sink in the logging layer can ship without redaction") <!-- WHEN every renderer defined by the logging layer is enumerated and given a line containing a credential THEN each redacts it, so a renderer added later without redaction is a test failure -->
- [ ] `ROI-S5` — it("a filter on the ancestor logger never sees a pipeline record") <!-- WHEN a spy filter sits on the top-level logger and a category logger emits THEN the spy saw nothing, and the logging layer's source no longer parks a filter there -->
- [ ] `ROI-S6` — it("a failed request reports header names and no header value") <!-- WHEN an outbound request fails against a loopback server and the failure is logged THEN the record names the headers sent and carries no header value -->

### File: `tests/test_roi_shutdown.py`

Describe: run-output-integrity — the finalization boundary and teardown noise

- [ ] `ROI-S7` — it("a record emitted after finalization began produces no write") <!-- WHEN finalization has begun and another thread emits to a previously open sink THEN the sink receives nothing further and nothing is raised into the emitting thread -->
- [ ] `ROI-S8` — it("output produced before finalization is still flushed") <!-- WHEN records are emitted before finalization and finalization then runs THEN every one of them is present in its file sink. Preservation pin: passes before the change and must keep passing after it, so suppression cannot cost the run its final report -->
- [ ] `ROI-S10` — it("the first teardown discard is reported and later ones are counted") <!-- WHEN a stopped stage is handed five tasks THEN exactly one report names the condition and says further discards are counted, and the counter reads five -->
- [ ] `ROI-S11` — it("the discard total is reported with the stop-completion record") <!-- WHEN a stage finishes stopping after discarding three tasks THEN its completion record carries the total and reports no survivors -->

### File: `tests/test_roi_logsink.py`

Describe: run-output-integrity — the log sink belongs to the run

Each probe runs in a **subprocess** on purpose: it needs pristine module-level
logging state, and mutating `Logger._file_handler` / `_logs_dir` /
`_module_handlers` inside the suite would leak into every other test that
captures a module logger. The probe forces the file-handler setup to run *after*
the preset, because importing the package already ran it once and its early
return would otherwise hide the behaviour under test.

- [ ] `ROI-S12` — it("a supplied sink location survives logging setup and is used") <!-- WHEN a sink location is supplied before logging setup and a record is emitted THEN the file appears under the supplied location and not under a working-directory-relative one, and an unsupplied location still resolves to the shipped default -->
- [ ] `ROI-S13` — it("a test run leaves no log file in the repository") <!-- WHEN records are emitted during a suite run THEN the repository's log directory gained no file -->

### File: `tests/test_prt_cap_surface.py`

Describe: provider-refusal-taxonomy Requirement 2 — the ceiling that governs

- [ ] `PRT-S26` — it("a provider refusal is bounded by the provider ceiling, not by another surface's") <!-- WHEN a provider-facing stage defers on a published wait above the provider ceiling while the code-hosting ceiling is lower THEN the wait applied is the provider ceiling and the deferral record names it as the bound that governed -->
- [ ] `PRT-S27` — it("a code-hosting-surface refusal keeps its own ceiling") <!-- WHEN a search or gather stage defers above its own ceiling THEN that ceiling still governs. Preservation pin: GREEN before the change by design -->
- [ ] `PRT-S28` — it("the applied wait is unchanged while the two ceilings are equal") <!-- WHEN both ceilings hold the shipped default THEN the applied wait is identical to today's and stays strictly below the queue visibility window. Preservation pin: GREEN before the change by design -->

## Manual

- `ROI-S9` — a surviving worker does not turn a clean stop into a failure status.
  The claim is about a real interpreter-finalization race and a **process exit
  code** (observed as `Fatal Python error: _enter_buffered_busy` with exit 134
  after `kill -9`). An in-process pin would have to win a timing race to be
  meaningful and would be flaky, and a subprocess pin would assert on a race it
  cannot reliably provoke. The **mechanism** that makes the exit code honest is
  automated by `ROI-S7`; the exit code itself is verified in the live gate's
  `kill -9` + recovery drill, which already measures it (precedent: gate R8 row
  B6, gate R9 row B6).

- `ROI-S14` — collection from the repository root does not include the root
  package module. The property is about how pytest resolves collection scope from
  an arbitrary working tree; pinning it in-suite would require a nested pytest
  subprocess collecting the whole suite from inside a test, which is slower than
  the property is worth and would itself be affected by the outer run's
  configuration. Verified manually by performing the previously blocked
  reproduction: collect at a parent commit inside a separate `git worktree`,
  which today dies with `ImportError: attempted relative import with no known
  parent package` at `__init__.py:97` (`plan_races.md` И4).
