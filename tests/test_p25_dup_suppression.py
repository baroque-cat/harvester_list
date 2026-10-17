"""P25 — observability of the silent processed-id gate in ``put_task``.

Traceability: plan.md II.19 P25; lesson Н9 (plan_races.md:353).

The in-RAM dedup gate (``stage/base.py``) refuses a FRESH task
(``attempts == 0``) whose id was already accepted by this process with a bare
``return False`` — no counter, no log — while its sibling branch
(``attempts > max_retries``) is counted and loud.  The live R17 gate attributed
1 544–2 524 created gather tasks per 300 s run to this silent refusal, and the
first gate report explained the divergence with a fabricated cause because
nothing published the real one (lesson Н9: a silent refusal without a counter
makes any divergence arithmetic useless and provokes false attribution).

Contract pinned here (observability only — refusal behavior is UNCHANGED):

* every fresh-duplicate refusal increments ``tasks_suppressed_duplicate``;
* the first refusal and every ``_DUP_SUPPRESS_LOG_EVERY``-th are reported with
  the running total (R16 publisher cadence);
* the stop-completion record carries the EXACT final total on both the
  graceful and the survivor path (D7: the number survives the run even when
  the last event missed the cadence boundary — lesson Н9.4);
* the counter is published in ``StageMetrics`` via ``get_stats()``;
* the exhausted-retry branch, bounded requeues (S21) and deferrals keep their
  own ledgers — the branches are mutually exclusive;
* the gate is identical on both queue backends, and on the durable backend it
  fires BEFORE the insert-time probe (in-run duplicates never reach it).

Expected RED at creation, and why:

* ``BasePipelineStage.tasks_suppressed_duplicate`` does not exist -> AttributeError;
* ``StageMetrics.tasks_suppressed_duplicate`` does not exist -> AttributeError;
* ``stage.base._DUP_SUPPRESS_LOG_EVERY`` does not exist -> monkeypatch AttributeError;
* no report names a suppressed duplicate -> captures stay empty;
* the stop-completion record carries no ``duplicate suppressions`` total.

House style: harness mirrors tests/test_tq_stage_backend.py (shared
``_resources`` from tests/test_fh_stage_modes.py per tests/test_roi_shutdown.py);
log capture mirrors tests/test_roi_shutdown.py (_Capture on the stage.base
module logger).
"""

from __future__ import annotations

import logging
import threading

import pytest

import stage.base as sb
from core.metrics import StageMetrics
from core.models import SearchTask
from stage.definition import SearchStage
from tests.test_fh_stage_modes import _resources

PROVIDER = "deepseek"
KEY_PATTERN = r"sk-[A-Za-z0-9]{16}"


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []
        self.levels: list[int] = []

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - trivial
        self.messages.append(record.getMessage())
        self.levels.append(record.levelno)


def _stage(tmp_path, backend="memory", max_retries=0):
    """Real SearchStage on the requested backend (mirrors test_tq_stage_backend)."""
    return SearchStage(
        _resources("legacy"),
        lambda out: None,
        thread_count=1,
        max_retries=max_retries,
        queue_size=1000,
        queue_backend=backend,
        queue_dir=str(tmp_path / "queue_state"),
    )


def _search_task(query='"sk-"', page=1):
    return SearchTask(provider=PROVIDER, query=query, regex=KEY_PATTERN, page=page, use_api=False)


def _reports(capture: _Capture) -> list[str]:
    return [m for m in capture.messages if "suppressed duplicate" in m]


# ---------------------------------------------------------------------------
# T1 — the refusal is counted on its own ledger
# ---------------------------------------------------------------------------
def test_t1_fresh_duplicate_is_refused_and_counted(tmp_path):
    """WHEN a fresh task repeats an id this process already accepted
    THEN the refusal returns False exactly as before AND increments the new
    counter, leaving the drop ledger untouched."""
    stage = _stage(tmp_path)
    assert stage.put_task(_search_task()) is True
    assert stage.tasks_suppressed_duplicate == 0        # RED pre-fix: attribute missing

    assert stage.put_task(_search_task()) is False      # behavior unchanged
    assert stage.tasks_suppressed_duplicate == 1        # ...but no longer silent
    assert stage.tasks_dropped_max_retries == 0         # own ledger, not the drop ledger
    assert stage.tasks_dropped_backend_errors == 0


# ---------------------------------------------------------------------------
# T2 — a bounded requeue is not a duplicate (S21 preserved)
# ---------------------------------------------------------------------------
def test_t2_bounded_requeue_is_not_a_duplicate(tmp_path):
    """WHEN the same task is re-put with 1 <= attempts <= max_retries
    THEN it is admitted and the duplicate counter does not move."""
    stage = _stage(tmp_path, max_retries=2)
    first = _search_task()
    assert stage.put_task(first) is True
    first.attempts = 1
    assert stage.put_task(first) is True                # S21: bounded retry admitted
    assert stage.tasks_suppressed_duplicate == 0


# ---------------------------------------------------------------------------
# T3 — the exhausted branch keeps its own ledger (mutual exclusion)
# ---------------------------------------------------------------------------
def test_t3_exhausted_retry_branch_keeps_its_own_ledger(tmp_path):
    """WHEN a task beyond the retry bound is refused
    THEN only tasks_dropped_max_retries grows and only the max-retries report
    is emitted — the branches never double-count."""
    stage = _stage(tmp_path, max_retries=0)
    first = _search_task()
    assert stage.put_task(first) is True
    first.attempts = 1                                  # > max_retries

    capture = _Capture()
    sb.logger.addHandler(capture)
    try:
        assert stage.put_task(first) is False
    finally:
        sb.logger.removeHandler(capture)

    assert stage.tasks_dropped_max_retries == 1
    assert stage.tasks_suppressed_duplicate == 0
    assert not _reports(capture)
    assert any("max retries" in m for m in capture.messages)


# ---------------------------------------------------------------------------
# T4 — loud once at the default cadence, counted always
# ---------------------------------------------------------------------------
def test_t4_first_suppression_is_loud_and_further_ones_are_counted(tmp_path):
    """WHEN several fresh duplicates arrive under the default cadence
    THEN exactly one report names the condition and the stage, every refusal
    is counted, and the report carries the running total."""
    stage = _stage(tmp_path)
    assert stage.put_task(_search_task()) is True

    capture = _Capture()
    sb.logger.addHandler(capture)
    try:
        for _ in range(3):
            assert stage.put_task(_search_task()) is False
    finally:
        sb.logger.removeHandler(capture)

    reports = _reports(capture)
    assert len(reports) == 1, f"expected one report, got {len(reports)}: {reports}"
    assert stage.tasks_suppressed_duplicate == 3        # counted, not only logged
    assert "suppressed_duplicate now 1" in reports[0]   # running total is parseable
    assert "search" in reports[0]                       # names the stage
    first = capture.messages.index(reports[0])
    assert capture.levels[first] == logging.WARNING     # loud, at the R16 level


# ---------------------------------------------------------------------------
# T5 — cadence: first + every Nth with running totals
# ---------------------------------------------------------------------------
def test_t5_cadence_reports_first_and_every_nth(tmp_path, monkeypatch):
    """WHEN the cadence constant is small
    THEN reports land on totals 1 and N with the running total in each."""
    monkeypatch.setattr(sb, "_DUP_SUPPRESS_LOG_EVERY", 5)   # RED pre-fix: constant missing
    stage = _stage(tmp_path)
    assert stage.put_task(_search_task()) is True

    capture = _Capture()
    sb.logger.addHandler(capture)
    try:
        for _ in range(7):
            assert stage.put_task(_search_task()) is False
    finally:
        sb.logger.removeHandler(capture)

    reports = _reports(capture)
    assert len(reports) == 2, f"expected reports at 1 and 5, got: {reports}"
    assert "suppressed_duplicate now 1" in reports[0]
    assert "suppressed_duplicate now 5" in reports[1]
    assert stage.tasks_suppressed_duplicate == 7


# ---------------------------------------------------------------------------
# T6 — publication through get_stats()
# ---------------------------------------------------------------------------
def test_t6_counter_is_published_in_stage_metrics(tmp_path):
    """THEN the counter travels with StageMetrics so status consumers and gate
    extraction read it programmatically."""
    stage = _stage(tmp_path)
    assert stage.put_task(_search_task()) is True
    assert stage.put_task(_search_task()) is False

    stats = stage.get_stats()
    assert isinstance(stats, StageMetrics)
    assert stats.tasks_suppressed_duplicate == 1        # RED pre-fix: field missing


# ---------------------------------------------------------------------------
# T7 — the exact final total rides the stop-completion record (both paths)
# ---------------------------------------------------------------------------
def test_t7a_graceful_stop_record_carries_the_exact_total(tmp_path):
    """WHEN the last suppression missed the cadence boundary
    THEN the graceful stop-completion record still carries the exact total —
    the Н9.4 guarantee: gate arithmetic converges from published numbers."""
    stage = _stage(tmp_path)
    assert stage.put_task(_search_task()) is True
    for _ in range(3):
        assert stage.put_task(_search_task()) is False  # total 3; cadence logged only #1

    capture = _Capture()
    sb.logger.addHandler(capture)
    try:
        stage.running = True                            # stop() returns early otherwise
        stage.stop(timeout=0.5)
    finally:
        sb.logger.removeHandler(capture)

    completion = [m for m in capture.messages if "stopped gracefully" in m]
    assert completion, f"no stop-completion record: {capture.messages}"
    assert "duplicate suppressions: 3" in completion[-1]    # RED pre-fix: token absent
    assert "tasks discarded after shutdown: 0" in completion[-1]  # D7 total preserved


def test_t7b_survivor_stop_record_also_carries_the_total(tmp_path):
    """THEN the survivor (zombie) path of the stop-completion record carries
    the same exact total."""
    stage = _stage(tmp_path)
    assert stage.put_task(_search_task()) is True
    for _ in range(2):
        assert stage.put_task(_search_task()) is False

    release = threading.Event()
    survivor = threading.Thread(target=release.wait, args=(10.0,), daemon=True)
    survivor.start()
    stage.workers.append(survivor)

    capture = _Capture()
    sb.logger.addHandler(capture)
    try:
        stage.running = True
        stage.stop(timeout=0.2)                         # join budget << event wait
    finally:
        sb.logger.removeHandler(capture)
        release.set()
        survivor.join(2.0)

    survivors = [m for m in capture.messages if "did not stop gracefully" in m]
    assert survivors, f"no survivor stop record: {capture.messages}"
    assert "duplicate suppressions: 2" in survivors[-1]


# ---------------------------------------------------------------------------
# T8 — concurrency: no lost increment
# ---------------------------------------------------------------------------
def test_t8_concurrent_duplicates_are_counted_exactly(tmp_path):
    """WHEN many threads race fresh duplicates of one accepted id
    THEN the counter equals the exact number of refusals (lock discipline)."""
    stage = _stage(tmp_path)
    assert stage.put_task(_search_task()) is True

    results: list[bool] = []
    results_lock = threading.Lock()

    def hammer() -> None:
        local = [stage.put_task(_search_task()) for _ in range(50)]
        with results_lock:
            results.extend(local)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10.0)

    assert results.count(False) == 400
    assert stage.tasks_suppressed_duplicate == 400


# ---------------------------------------------------------------------------
# T9 — backend parity, and layering against the durable probe (R16)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_t9_gate_is_identical_on_both_backends(tmp_path, backend):
    """THEN the gate behaves the same on both backends, and on sqlite the
    stage-level gate fires BEFORE the durable insert-time probe: an in-run
    duplicate never reaches the storage layer (the two counters measure
    different layers and must not be conflated)."""
    stage = _stage(tmp_path, backend=backend)
    try:
        assert stage.put_task(_search_task()) is True
        assert stage.put_task(_search_task()) is False
        assert stage.tasks_suppressed_duplicate == 1
        assert stage.get_stats().tasks_suppressed_duplicate == 1
        if backend == "sqlite":
            assert stage.queue.dedup_stats()["suppressed_puts"] == 0
    finally:
        if backend == "sqlite":
            stage.queue.close()


# ---------------------------------------------------------------------------
# T10 — a clean run publishes zero and stays silent
# ---------------------------------------------------------------------------
def test_t10_clean_run_publishes_zero_and_stays_silent(tmp_path):
    """WHEN no duplicate ever arrives
    THEN the counter is zero, no suppression report is emitted, and the
    stop-completion record still carries the structural zero token."""
    stage = _stage(tmp_path)
    assert stage.put_task(_search_task()) is True

    capture = _Capture()
    sb.logger.addHandler(capture)
    try:
        stage.running = True
        stage.stop(timeout=0.5)
    finally:
        sb.logger.removeHandler(capture)

    assert stage.tasks_suppressed_duplicate == 0
    assert not _reports(capture)
    completion = [m for m in capture.messages if "stopped gracefully" in m]
    assert completion and "duplicate suppressions: 0" in completion[-1]


# ---------------------------------------------------------------------------
# T12 — a deferral is not a duplicate
# ---------------------------------------------------------------------------
def test_t12_deferral_bypasses_the_gate_entirely(tmp_path):
    """WHEN a rate-limit-deferred task is returned to pending via defer_task
    THEN the dedup gate is bypassed (design D4 seam) and the duplicate
    counter does not move."""
    stage = _stage(tmp_path)
    task = _search_task()
    assert stage.put_task(task) is True
    assert stage.defer_task(task) is True
    assert stage.tasks_suppressed_duplicate == 0
    assert stage.tasks_deferred == 1
