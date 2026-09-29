"""ROI-S7, S8, S10, S11 — run-output-integrity: finalization and teardown noise.

Change ``fix-run-output-hygiene``.  ROI-S9 (a surviving worker must not turn a
clean stop into a failure status) is a Manual entry in ``tests.md``: its claim is
about a real interpreter-finalization race and a process exit code, which cannot
be pinned deterministically in-process.  ROI-S7 pins the mechanism that makes it
true — no write reaches a sink once finalization has begun.

Expected RED at creation, and why:

* nothing suppresses a write after ``shutdown_logging()``, so S7 sees the record;
* a discard after shutdown is reported once per task and never totalled, so S10
  counts several reports and finds no counter, and S11 finds no total.
"""

from __future__ import annotations

import io
import logging
import pathlib
import threading

import stage.base as sb
import tools.logger as lg
from stage.definition import SearchStage
from tests.test_fh_stage_modes import _resources, _search_task
from tools.logger import get_logger


def _console_handler(logger: logging.Logger) -> logging.StreamHandler:
    for handler in logger.handlers:
        if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
            return handler
    raise AssertionError("the logging layer attached no console handler")


def _clear_finalization_flag() -> None:
    """Restore the module so later tests in the session are not suppressed."""
    if hasattr(lg, "_LOGGING_SHUTDOWN"):
        lg._LOGGING_SHUTDOWN = False


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - trivial
        self.messages.append(record.getMessage())


# ---------------------------------------------------------------------------
# ROI-S7 / ROI-S8 — the finalization boundary
# ---------------------------------------------------------------------------


def test_roi_s7_a_record_emitted_after_finalization_began_produces_no_write():
    """WHEN finalization has begun and a thread emits a record to a sink that was
    open before it
    THEN the sink receives nothing further and the emission raises nothing into the
    emitting thread."""
    logger = get_logger("roi-s7")
    console = _console_handler(logger)
    original, buffer = console.stream, io.StringIO()
    console.stream = buffer
    raised: list[BaseException] = []
    try:
        logger.warning("BEFORE-FINALIZATION marker")
        lg.shutdown_logging()

        def _emit() -> None:
            try:
                logger.warning("AFTER-FINALIZATION marker")
            except BaseException as exc:  # noqa: BLE001 - the point is that nothing escapes
                raised.append(exc)

        worker = threading.Thread(target=_emit, daemon=True)
        worker.start()
        worker.join(5.0)
        rendered = buffer.getvalue()
    finally:
        console.stream = original
        _clear_finalization_flag()

    assert raised == [], f"a write to a closing sink raised into its thread: {raised!r}"
    assert "BEFORE-FINALIZATION" in rendered, "output from before finalization must still be written"
    assert "AFTER-FINALIZATION" not in rendered, "a record was written after finalization began"


def test_roi_s8_output_produced_before_finalization_is_still_flushed():
    """WHEN records are emitted before finalization begins and finalization then runs
    THEN every one of them is present in its sink."""
    logger = get_logger("roi-s8")
    logger.warning("PREFLUSH marker on the file sink")
    logs_dir = lg.Logger.get_logs_directory()
    try:
        lg.shutdown_logging()
    finally:
        _clear_finalization_flag()

    assert logs_dir is not None, "the logging layer reported no sink directory"
    sink = pathlib.Path(logs_dir) / "roi-s8.log"
    assert sink.exists(), f"no file sink was written at {sink}"
    assert "PREFLUSH" in sink.read_text(encoding="utf-8"), "finalization lost output written before it began"


# ---------------------------------------------------------------------------
# ROI-S10 / ROI-S11 — teardown discards are loud once and counted always
# ---------------------------------------------------------------------------


def _stopped_stage() -> SearchStage:
    """A stage that has been through ``stop()`` and therefore no longer accepts work."""
    stage = SearchStage(_resources("strict"), lambda out: None, thread_count=1, max_retries=0)
    stage.running = True  # stop() returns early otherwise; no worker was ever started
    stage.stop(timeout=0.5)
    assert stage.accepting is False, "precondition: the stage stopped accepting work"
    return stage


def test_roi_s10_the_first_teardown_discard_is_reported_and_later_ones_are_counted():
    """WHEN a stage that has stopped accepting work is handed several tasks in
    succession
    THEN exactly one report names the condition, the rest produce no report, and
    every one of them is counted."""
    stage = _stopped_stage()

    capture = _Capture()
    sb.logger.addHandler(capture)
    try:
        results = [stage.put_task(_search_task()) for _ in range(5)]
    finally:
        sb.logger.removeHandler(capture)

    assert results == [False] * 5, "a discard must not be reported as accepted"
    reports = [m for m in capture.messages if "not accepting" in m]
    assert len(reports) == 1, f"expected one report, got {len(reports)}: {reports}"
    assert "counted" in reports[0], "the single report must say further discards are counted"
    assert getattr(stage, "tasks_discarded_after_shutdown", None) == 5, (
        "every discard must be counted, including the ones that were not logged"
    )


def test_roi_s11_the_discard_total_is_reported_with_the_stop_completion_record():
    """WHEN a stage finishes stopping after having discarded tasks
    THEN its stop-completion record carries the total discarded and distinguishes a
    graceful stop from one that left survivors."""
    stage = SearchStage(_resources("strict"), lambda out: None, thread_count=1, max_retries=0)
    stage.running = True
    stage.accepting = False
    for _ in range(3):
        assert stage.put_task(_search_task()) is False

    capture = _Capture()
    sb.logger.addHandler(capture)
    try:
        stage.running = True
        stage.stop(timeout=0.5)
    finally:
        sb.logger.removeHandler(capture)

    completion = [m for m in capture.messages if "stopped gracefully" in m]
    assert completion, f"no stop-completion record was emitted: {capture.messages}"
    assert "3" in completion[-1], f"the completion record does not carry the discard total: {completion[-1]}"
    assert not stage.zombie_threads, "no worker was started, so none may be reported as a survivor"
