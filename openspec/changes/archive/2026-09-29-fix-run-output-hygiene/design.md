## Context

See `proposal.md` — Why. This section records only the current state that shapes the approach.

**Four formatters, two of them redact.** `tools/logger.py` ships `ColoredFormatter` (`:69`), `APIKeyRedactionFormatter` (`:104`), `JSONFormatter` (`:128`) and `FileFormatterWithRedaction` (`:381`). Coverage today:

| formatter | reaches | redacts | on redaction failure |
|---|---|---|---|
| `ColoredFormatter` (`FORMATTER`, `:194`) | every console handler (`setup_logger`) | **no** | — |
| `JSONFormatter` (`FILE_FORMATTER_JSON`, `:196`) | file handlers when `_FILE_LOG_FORMAT == "json"` | **no** | — |
| `FileFormatterWithRedaction` (`FILE_FORMATTER`, `:404`) | file handlers in text mode | yes | returns the **original text** — leaks |
| `APIKeyRedactionFormatter` | only the `uvicorn.access` handler (`:980`) | yes | returns `[LOG_REDACTION_ERROR]` |

So the two sinks that carry production output (console, and file logs in JSON mode) are uncovered, and the one file formatter that does redact fails **open**.

**Correction (found during implementation).** The module defines a **fifth** `logging.Formatter`
subclass that the table above omits: `FileFormatter` (`:217`). It is dead code — no shipped call site
constructs it, and `FILE_FORMATTER` is built from `FileFormatterWithRedaction` — but the `ROI-S4` pin
enumerates every `logging.Formatter` subclass *defined* in the module, so it is in scope: leaving it
unredacted fails the suite the moment the pin runs. It is redacted like the other four (`:222`). The
"four formatters" framing above describes the **reachable** sinks; the pin deliberately counts the
**defined** ones, which is exactly what makes a fifth renderer added later a test failure rather than
a silent gap (D3). Deleting the dead class is a separate question and out of scope here.

**The `RedactionFilter` attachment cannot fire.** It is attached once, to the root logger (`:870`). `Logger.setup_logger` sets `logger_instance.propagate = False`, and `logging.Logger.callHandlers` walks the ancestor chain collecting **handlers** only — an ancestor's `filters` are never applied to a child's record. The root logger has no handlers. Every logger in the pipeline comes from the module-level `get_logger(category)` (`:834`) → `Logger.setup_logger`. The class therefore has exactly one reference and zero effect.

**JSON file mode is not reachable from a shipped entry point.** `set_file_log_format` is called from `init_logging(file_format=...)` (`:858`) and from `configure_logging_from_env()` (`:752`, reading `LOG_FILE_FORMAT`). `main.py:480` calls `init_logging(args.log_level)` — level only, so `file_format` stays `"text"` — and `configure_logging_from_env()` **has no callers at all**. The JSON leak is latent, not active: it activates the first time anyone wires up the env switch.

**The fatal-error mechanism.** Worker threads are `daemon=True` (`stage/base.py:280` in `start()`, `:759` in the scale-up path). `stop()` joins with a per-worker budget and already detects survivors — it stores them in `self.zombie_threads` and logs `N workers did not stop gracefully` (`:314-316`). A survivor keeps running during interpreter finalization; `put_task` (`:321-325`) logs `[{stage}] not accepting tasks, discard: {task}` **per task**, and that stdout write against a busy/closing buffer produced `Fatal Python error: _enter_buffered_busy` and exit **134** with queue state byte-identical and reclaim complete. `shutdown_logging()` flushes and closes every handler and clears `Logger._loggers` / `_module_handlers`; it is registered via `atexit` in `_setup_exit_handlers()`.

**The logs directory is not injectable.** `Logger._logs_dir` defaults to `None`, but `_setup_file_handlers()` assigns `Logger._logs_dir = Path("logs")` **unconditionally** — CWD-relative. `_ensure_logs_directory()` (`:427-442`) then creates it, guarded by a `_directory_initialized` attribute. A fixture that presets `_logs_dir` is therefore overwritten by the first logger creation, which is why every suite run leaves `logs/main.log` in the repository root.

**Two caps, one governs.** `_defer_wait_cap()` (`stage/base.py:435-444`) returns `config.gather.max_refusal_wait_s` for every stage; `_effective_defer_wait` (`:446-448`) clamps with it, and `defer_task` prints it as `(cap {cap:.1f}s)` (`:422-431`). The transport has already clamped with `provider.max_refusal_wait_s`, so the effective bound for a provider refusal is `min(published, provider_cap, gather_cap)`. Both default to `60.0` (`config/schemas.py:646`, `:691`) and both are validated strictly below `queue.visibility_timeout_s` (`config/validator.py:568-578`, `:602-612`).

**Stage surfaces.** `SearchStage` (`stage/definition.py:70`) and `AcquisitionStage` (`:531`) talk to GitHub; `CheckStage` (`:677`) and `InspectStage` (`:941`) talk to LLM providers. All four extend `BasePipelineStage` (`stage/base.py:140`).

## Goals / Non-Goals

**Goals:**
- Make redaction a property of **every** sink, enforced structurally rather than by a checklist, with uniform fail-closed behaviour.
- Make "a write to a sink that the run has begun closing" impossible rather than unlikely, and make the exit status honest.
- Make the log sink location injectable so a test run cannot write into the repository, and make collection scope explicit so a measurement can be reproduced at a parent commit.
- Make the refusal-wait cap that governs be the one the operator set for that surface.

**Non-Goals:**
- **Not** wiring up `configure_logging_from_env()` / `LOG_FILE_FORMAT` / `LOG_COLOR`. That would activate the JSON path and is an operator decision; this change only makes activating it safe. The dead switch is recorded as D11 rather than fixed.
- **Not** widening the redaction dictionary (`tools/patterns.py`). The pattern set is a separate question; D5 explains why source-side minimisation is needed regardless of how wide it is.
- **Not** changing thread daemonisation, the join budget, or the zombie WARNING — those already work and are the honest signal.
- **Not** touching refusal classification, precedence (R9 D2), the retry policy, metric renderers, or anything on the harvested-credential `check` surface.
- **Not** introducing a logging dependency; stdlib only.

## Decisions

### D1 — Redaction belongs in the formatter, applied to the final rendered line

A `logging.Filter` sees `record.getMessage()` (msg + args) and custom extra attributes. It does **not** see what the formatter adds: `fileloc`, colour escapes, and above all `exc_info` traceback text. A credential inside a traceback would pass through untouched. The formatter is the last thing that produces the exact bytes reaching the sink.

*Rejected:* attach `RedactionFilter` to each handler. Handler filters do run (`Handler.handle` → `self.filter`), so it would work for the message — but it is incomplete for tracebacks, and a filter mutates the record object shared by all handlers, making the result order-dependent. Two layers doing the same job, one of them weaker, is worse than one correct layer.

### D2 — Uniform fail-closed, with a marker that keeps the line's identity

`FileFormatterWithRedaction` currently returns the **original text** when redaction raises. For a redaction layer that is the wrong default: the failure mode is "leak the secret". `APIKeyRedactionFormatter` already fails closed with `[LOG_REDACTION_ERROR]`.

All formatters converge on one helper that, on failure, emits a marker preserving level, logger name and `file:line` but not the payload. The record's *existence* stays visible — invariant 3 requires degradation to be loud — while the secret cannot escape. The helper does not log from inside a formatter (recursion risk); the marker is the signal.

*Rejected:* fail-open "for consistency with the repo's fail-open invariant". That invariant is about **requests** ("better to lose one request than hang"), and invariant 3's own wording is loud *but not with a secret*. Losing one message body is strictly better than publishing a third-party live key.

### D3 — One shared helper, and completeness enforced by a pin over the module's formatters

Each formatter's `format()` ends with a call to one module-level `_redact_line(text)`. Four divergent implementations become one.

Completeness is then made **structural**, not enumerative: a pin enumerates the `logging.Formatter` subclasses defined in `tools/logger.py` and asserts each one redacts. A fifth formatter added later fails the test instead of silently shipping an unredacted sink. This is the same device R5.3 used for metric surfaces (`_RENDERED_METRIC_SURFACES` checked against `PipelineStatus`, main spec `run-observability` RO-S4), and the reason O3 could not recur silently.

*Rejected:* a `RedactingFormatterMixin` base class. More elegant, but `ColoredFormatter.format` builds a record copy and sets `fileloc` before delegating, so MRO surgery would be needed for zero behavioural gain — higher regression risk on the one formatter whose output every operator reads.

### D4 — Remove the inert `RedactionFilter`, and pin the semantics that make it inert

The class has exactly one reference (`:870`) and zero effect (Context). Deleting a mechanism that looks like a safeguard is the point: an artefact that claims protection the code does not provide is the same defect class as the unreachable `except` removed in R9 and the false "the signal propagates unaided" claim about `vertex` (lessons Н1 and Н2 in `plan_races.md`).

A comment at the former attachment site states the two reasons (propagate=False; `callHandlers` collects ancestor handlers, never ancestor filters), and a pin asserts that a record produced through `get_logger(...)` is never seen by a filter attached to the root logger. Without the pin, the next reader re-adds it and believes redaction is covered.

*Rejected:* keep the class "for future use". Dead code that reads as a security control is worse than absent code.

### D5 — Minimise at the source as well: `chat()` keeps header names, drops values

`search/client.py:1862` interpolates the whole `headers` dict. It keeps the header **names** — "was an `authorization` header even present?" is a real question when debugging a 401 — and drops the values.

This is not redundant with D1–D3. `tools/patterns.py:13` matches `\bsk-[0-9A-Za-z_-]{20,}`: a bearer value with a different shape passes through any pattern-based layer. Redaction is the last line of defence, not the only one, and the cheapest way to stop leaking a value is not to put it in the message.

*Rejected:* rely on redaction alone. Pattern coverage is finite and provider-specific; the live evidence (7–8 harvested keys per gate run, all from this one line) shows a single emitter is responsible for the whole leak.

### D6 — Suppress writes once finalization has begun, at the handler level

`shutdown_logging()` gains a module-level flag, set **after** `flush_all_handlers()` and **before** handlers are closed, so legitimate final output is flushed and nothing after it is written. A small `logging.Filter` attached to the handlers returns `False` once the flag is set. Handler filters do run (`Handler.handle` → `self.filter(record)`) — the same mechanism the root-logger attachment misused, attached where it actually applies.

This makes exit 134 impossible rather than unlikely: a survivor thread may still call `logger.warning(...)`, but the call produces no write to a closing sink.

*Rejected:* drop `daemon=True`. A wedged worker would then hang the process at exit forever — trading a wrong exit code for no exit code, which invariant 3 forbids.
*Rejected:* lengthen the join budget. The join already exists, already bounds itself per worker, and already reports survivors into `zombie_threads` with a WARNING. The defect is not failure to detect survivors but that they can still write afterwards.

### D7 — Aggregate teardown discards: loud once, counted always

`put_task`'s `not self.accepting` branch logs one WARNING per task. `accepting` is only false once `stop()` has begun, so this is a shutdown-only path, and under load it emits thousands of identical lines — precisely the noise plan II.8 says masks real finalization errors.

Change: increment a per-stage discard counter, emit **one** WARNING naming the condition and stating that further discards are counted rather than logged, and include the final total in the existing stop-completion log line (`stage/base.py:314-319`, which already distinguishes graceful from zombie stops). Loud-once satisfies invariant 3; the total preserves the number an operator needs.

*Rejected:* silence the path entirely. Throwing work away must remain visible; a counter nobody prints is the O3 defect in a new place.

### D8 — Choose the refusal-wait cap by the surface that produced the refusal

`BasePipelineStage` gains a class attribute naming the config section that supplies its cap, defaulting to `"gather"`; `CheckStage` and `InspectStage` override it to `"provider"`. `_defer_wait_cap()` reads that section, keeping the `inf` fallback when the config is unreachable. Both call sites — the clamp (`:448`) and the cap printed in the deferral WARNING (`:422`) — inherit the change, so the number an operator sees becomes the number that governed.

The mapping is not a guess: it follows the producers of `RateLimitDeferral`. GitHub-surface producers are gather's local-budget suppression and the secondary-limit path (search, gather); provider-surface producers are the transport classification in `http_get` and the provider-basket starvation in `_check_worker` (check, inspect).

**Behaviour is identical under shipped defaults** — both caps are `60.0` — and stays within the visibility window in every case, because both keys are independently validated as strictly below `queue.visibility_timeout_s`. Invariant 6 is unaffected.

*Rejected (plan II.14 option 2):* a validator rule `provider.max_refusal_wait_s <= gather.max_refusal_wait_s`. It enshrines a coupling between two unrelated surfaces instead of removing it, and makes the provider key meaningful only by forcing it smaller.
*Rejected (plan II.14 option 3):* document the `min()` semantics. That leaves a configuration key which cannot govern — a violation of invariant 5 ("policy is visible in configuration, not implied").

**Correction to plan II.14.** The plan states the deferral message does not mention the cap. It does: `defer_task` prints `(cap {cap:.1f}s)`. What it does not reveal is the cap's **source**, which is why two equal defaults made the coupling invisible. After this decision the printed value is the governing one and names its surface. `plan.md` II.14 and `plan_races.md` П1 are amended accordingly (append-only).

### D9 — `_logs_dir` respects a preset

`_setup_file_handlers()` assigns `Logger._logs_dir = Path("logs")` unconditionally, so any preset is overwritten by the first logger creation. Change it to assign only when `_logs_dir is None`. Production is unaffected: the class attribute defaults to `None`, so the first call still resolves to `Path("logs")`, CWD-relative, exactly as `main.py` and AGENTS.md §1.2 rely on.

This one line is the minimum product change that makes the sink injectable; without it no fixture can redirect logging.

*Rejected:* an environment variable for the logs directory. It adds an operator-facing configuration surface for a test-only need, and this is not policy in the sense invariant 5 covers.
*Rejected:* monkeypatching `Path` in the fixture. Brittle, and it hides the defect rather than fixing it — the hardcoded CWD-relative assignment is the reason a test run cannot be scoped at all.

### D10 — Session-scoped log isolation for tests, plus explicit collection scope

An autouse **session-scoped** fixture in `tests/conftest.py` presets `Logger._logs_dir` to a pytest-provided directory before any handler exists and closes handlers afterwards. Session scope, not function scope: resetting `Logger._loggers`, `_file_handler` and `_module_handlers` per test would rebuild handlers hundreds of times for no benefit, and D9 makes the preset survive `_setup_file_handlers`'s early return and any `shutdown_logging()` in between.

A `pytest.ini` declares `testpaths = tests`. This is what unblocks reproducing a measurement at a parent commit: outside the main tree pytest currently collects the root `__init__.py` as a test module and dies with `ImportError: attempted relative import with no known parent package` (`__init__.py:97`), because there is no `pytest.ini`, `pyproject.toml`, `setup.cfg` or root `conftest.py` (lesson И4 in `plan_races.md`). The same file makes rootdir explicit and stops the suite from writing `logs/` into the repository.

Because adding `pytest.ini` can shift rootdir and conftest discovery, the full suite must be re-run and its pass count compared against the recorded **446**; a change in collected count is treated as a regression to explain, not to accept.

### D11 — Record the dead `LOG_FILE_FORMAT` switch instead of wiring it

`configure_logging_from_env()` documents `LOG_FILE_FORMAT` and `LOG_COLOR` and has no callers. Wiring it up is out of scope (Non-Goals), but `JSONFormatter` is redacted anyway, so activating JSON logging later is safe. Recorded here because a documented switch that does nothing is the same defect class as D4: an artefact claiming something the code does not do.

## Risks / Trade-offs

- **Fail-closed redaction could hide a diagnostic line** → the marker preserves level, logger and `file:line`, so the record's existence and origin stay visible; the branch can only be reached if the regex helper itself raises, which no shipped input has done.
- **Removing `RedactionFilter` breaks an unseen importer** → grep shows exactly one reference in the whole tree (`:870`); the class is not exported by any `__init__` and no test imports it.
- **`pytest.ini` shifts rootdir and changes what is collected** → the suite is run before and after and the counts compared against the recorded 446; collection scope is itself pinned (the root `__init__.py` must not appear among collected items).
- **Cap selection changes behaviour when the two caps differ** → that is the intent, and it is bounded: both keys remain validated strictly below `queue.visibility_timeout_s`, so invariant 6 holds. Identity under shipped defaults is pinned explicitly so the "no behaviour change today" claim is measured, not asserted.
- **The shutdown filter drops legitimate final output** → the flag is set *after* `flush_all_handlers()` and *before* closing handlers, and `shutdown_logging()` runs from `atexit`, i.e. after the pipeline has printed its final status. Order is pinned.
- **Aggregating discards hides a real loss** → the total is printed in the stop-completion line; loudness moves from per-record to once-plus-total, it does not disappear.
- **Four items in one change** → they share a theme but not a code path, so each is a separate commit-sized unit inside the change and each is independently revertible. Tasks are grouped accordingly.

## Migration Plan

No data migration, no schema change, no configuration key added, removed or renamed. Deploy is a normal restart.

Rollback is per item:
1. cap selection — revert the class attribute; identical under defaults, so nothing observable changes unless an operator set the caps apart;
2. discard aggregation — revert to per-task WARNING;
3. shutdown suppression — revert the flag and the handler filter; exit 134 returns as a possibility, data integrity is unaffected either way;
4. redaction — revert the helper and restore the four formatters; **this is the one item that should not be rolled back**, since reverting re-opens a third-party-secret leak into stdout and the journal;
5. test isolation — revert the fixture and delete `pytest.ini`; repository `logs/` pollution returns.

Verification order respects AGENTS.md §2: the offline suite and any live gate are sequenced, never concurrent, and a live run writes only under `/var/tmp`.

## Addenda (implementation, append-only)

### D12 — ROI-S6 required the 401 failure to become observable again

D5 says the single known emitter keeps header **names** and drops values. Implementing it exposed a
second, smaller defect on the same line: the `chat()` failure message was emitted **only when
`code != 401`**. A 401 fell through silently, so the header names ROI-S6 asserts on were never
logged for the one status the scenario actually exercises (its loopback server answers 401).

The silence was not intentional redaction — it was an `if code != 401` guard. With the value now
dropped at the source (D5), there is nothing left for a 401 to protect, and the failure is logged
like every other status. This is a behaviour change beyond D5's letter: it makes a previously
unlogged path loggable. It is *not* a new leak (no value is interpolated), it does not touch
classification or counters (PRT-S25 is explicit that a harvested-key 401 stays on the `check`
surface), and `tests/test_prc_classes.py::test_s25` — which calls the same `chat()` with a
synthetic key — pins that the refusal counters stay at zero.

Recorded because it contradicts the design's own "D5 is a message-content change only" reading; the
shipped code logs the failure and the scenario requires it.

### D13 — Tasks 4.5 and 5.3 contradict each other; 5.3 governs the survivor line

Task 4.5 says to leave `daemon=True`, the bounded join **and the zombie WARNING** (`stage/base.py`
`:314-316`) untouched, because they already detect and report survivors honestly (D6, rejected
alternatives). Task 5.3 says to report the discard total in the stop-completion branch on **both** the
graceful path and the survivor path. The two cannot both hold: the survivor path *is* the zombie
WARNING, so reporting a total there edits the line 4.5 forbids touching.

Resolved in favour of 5.3, which is the more specific instruction about the number this change exists
to preserve, and which D7 requires ("the total is printed in the stop-completion line … loudness moves
from per-record to once-plus-total, it does not disappear"). What 4.5 actually protects is preserved
verbatim:

- the original text `[{stage}] {N} workers did not stop gracefully` survives **unchanged as a
  prefix**, so survivor detection and its wording are byte-identical for any reader or grep;
- `daemon=True` (`stage/base.py:280`, `:759`) and the bounded join are **not** modified — verified by
  diff: no `daemon=`, `worker.join` or `is_alive` line changed;
- `zombie_threads` is still populated on the same condition.

So the D6 rejected alternatives (dropping `daemon`, lengthening the join) remain rejected, and the
only edit is the appended total. Recorded because 4.5's literal wording is now false and a future
reader must not "restore" the line and lose the total.

### D14 — D10's mechanism is a `pytest_configure` hook, not an autouse session fixture

D10 specifies "an autouse **session-scoped** fixture in `tests/conftest.py` presets `Logger._logs_dir`
… **before any handler exists** and **closes handlers afterwards**". Implementation shows both halves
are wrong, for a reason recorded independently as lesson И5 in `plan_races.md`:

- **A fixture cannot run early enough.** Importing `tools.logger` executes `tools/__init__.py`, whose
  module imports call `get_logger(...)`; the first such call runs `_setup_file_handlers()` and resolves
  `Logger._logs_dir`. Collection imports product code, so by the time any fixture — session-scoped
  included — gets control, the sink is already fixed and `_file_handler` is already non-`None`, which
  makes `_setup_file_handlers()` early-return and never re-read a preset. The redirect therefore lives
  in `pytest_configure` (`tests/conftest.py:24`), the last hook before collection.
- **Handlers may already exist, so they are stripped, not avoided.** The hook presets `_logs_dir`,
  forces `_file_handler = None`, clears `_module_handlers` / `_loggers` / `_directory_initialized`, and
  removes already-attached `logging.FileHandler`s from every logger in
  `logging.Logger.manager.loggerDict` (`tests/conftest.py:41-52`).
- **No "close handlers afterwards" teardown exists**, and none is needed: `ROI-S13` pins the property
  that matters (the repository's `logs/` gains no file), the session directory is a `tempfile.mkdtemp`
  under the system temp, and adding a teardown would race the `shutdown_logging()` calls that
  `tests/test_roi_shutdown.py` performs deliberately.

The **intent** of D10 is unchanged and is what the pins assert: one session-wide redirect, not a
per-test reset (resetting `_loggers`/`_file_handler`/`_module_handlers` per test would rebuild handlers
hundreds of times for no benefit), plus `pytest.ini` declaring `testpaths = tests`. Only the mechanism
differs. D10's prose above is therefore superseded by this addendum; it is left in place unedited so
the record of what was designed stays readable.
