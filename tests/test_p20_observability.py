"""P20 observability: the dedup counters must survive the process that earned them.

Traceability: /tmp/opencode/p20-plan.md D7 (loud once, counted always) plus the
gap the P20 field gate exposed -- ``suppressed_puts`` lives in RAM, so a run
whose console carried a single loud-once WARNING could not say HOW MANY
redundant rows the store absorbed (measured indirectly as +50 rows instead of
+9 686).  Two publishers close that gap:

* the engine logs the 1st suppression and then every ``_DEDUP_LOG_EVERY``-th, so
  a long soak shows a running total without one line per event;
* ``QueueManager._report_dedup_stats`` emits one INFO line per stage whenever the
  totals change, which also lands the final total because ``Pipeline._on_stop``
  saves once more before stopping (durable-task-queue-S19 ordering).

Contract exercised here:
    SqliteTaskQueue(..., durable_dedup=True).dedup_stats()
        -> {"suppressed_puts": int, "startup_collapsed": int}
    QueueManager.save_all_queues(stages) reports changed totals only
"""

import json
import logging
from types import SimpleNamespace

import pytest

from manager.queue import QueueManager
from storage.task_queue import _DEDUP_LOG_EVERY, SqliteTaskQueue

TOKEN = "suppressed duplicate pending put"
REPORT_TOKEN = "Durable dedup for"


def _q(tmp_path, name="obs", **kw):
    return SqliteTaskQueue(
        str(tmp_path / f"{name}_queue.sqlite"),
        serializer=json.dumps,
        deserializer=json.loads,
        name=name,
        **kw,
    )


def _suppress(q, dedup_id, n):
    """Put one real row, then ``n`` fresh duplicates that the probe suppresses."""
    assert q.put({"url": dedup_id}, dedup_id=dedup_id) is True
    for i in range(n):
        assert q.put({"url": dedup_id, "copy": i}, dedup_id=dedup_id) is False


# ---------------------------------------------------------------------------
# O1/O2 - engine: bounded logging, unbounded counting
# ---------------------------------------------------------------------------
def test_o1_suppression_logs_first_and_every_nth(tmp_path, caplog):
    q = _q(tmp_path)
    try:
        with caplog.at_level(logging.WARNING):
            _suppress(q, "gather:p:one", 2 * _DEDUP_LOG_EVERY)

        lines = [r.getMessage() for r in caplog.records if TOKEN in r.getMessage()]
        # Logged at 1, 1000 and 2000 suppressions -- never once per event.
        assert len(lines) == 3, lines
        assert "suppressed_puts now 1)" in lines[0]
        assert f"suppressed_puts now {_DEDUP_LOG_EVERY})" in lines[1]
        assert f"suppressed_puts now {2 * _DEDUP_LOG_EVERY})" in lines[2]
    finally:
        q.close()


def test_o2_counter_is_exact_while_logging_stays_bounded(tmp_path):
    q = _q(tmp_path)
    try:
        _suppress(q, "gather:p:two", 1234)
        stats = q.dedup_stats()
        assert stats["suppressed_puts"] == 1234
        assert stats["startup_collapsed"] == 0
        # The store still holds exactly one row: counting is not inserting.
        assert q.qsize() == 1
        assert q.counts() == {"pending": 1, "claimed": 0}
    finally:
        q.close()


# ---------------------------------------------------------------------------
# O3/O4/O5 - manager: report on change only, duck-typed, fail-open
# ---------------------------------------------------------------------------
def _manager_with_durable_stage(tmp_path, stage_name="gather"):
    qm = QueueManager(workspace=str(tmp_path / "ws"), save_interval=60, backend="sqlite")
    q = _q(tmp_path, name=stage_name)
    return qm, {stage_name: SimpleNamespace(queue=q)}, q


def test_o3_manager_reports_changed_totals_once(tmp_path, caplog):
    qm, stages, q = _manager_with_durable_stage(tmp_path)
    try:
        _suppress(q, "gather:p:three", 5)

        with caplog.at_level(logging.INFO):
            qm.save_all_queues(stages)
        first = [r.getMessage() for r in caplog.records if REPORT_TOKEN in r.getMessage()]
        assert len(first) == 1, first
        assert "suppressed_puts=5" in first[0]
        assert "startup_collapsed=0" in first[0]
        assert "gather" in first[0]

        # Unchanged totals are not repeated: a quiet run stays quiet even though
        # maintenance passes every save interval.
        caplog.clear()
        with caplog.at_level(logging.INFO):
            qm.save_all_queues(stages)
        assert [r for r in caplog.records if REPORT_TOKEN in r.getMessage()] == []

        # A new suppression publishes the new total.
        _suppress(q, "gather:p:four", 1)
        caplog.clear()
        with caplog.at_level(logging.INFO):
            qm.save_all_queues(stages)
        second = [r.getMessage() for r in caplog.records if REPORT_TOKEN in r.getMessage()]
        assert len(second) == 1, second
        assert "suppressed_puts=6" in second[0]
    finally:
        q.close()


def test_o4_zero_totals_are_not_reported(tmp_path, caplog):
    qm, stages, q = _manager_with_durable_stage(tmp_path)
    try:
        with caplog.at_level(logging.INFO):
            qm.save_all_queues(stages)
        assert [r for r in caplog.records if REPORT_TOKEN in r.getMessage()] == []
    finally:
        q.close()


def test_o5_queue_without_the_accessor_reports_nothing_and_never_raises(tmp_path, caplog):
    """Memory-backend shape (no ``dedup_stats``) and a raising accessor both pass."""
    qm = QueueManager(workspace=str(tmp_path / "ws2"), save_interval=60, backend="sqlite")

    class Legacy:
        def counts(self):
            return {"pending": 0, "claimed": 0}

        def sweep_expired_claims(self):
            return 0

        def checkpoint(self):
            return None

    class Raising(Legacy):
        def dedup_stats(self):
            raise RuntimeError("boom")

    with caplog.at_level(logging.INFO):
        qm.save_all_queues({"gather": SimpleNamespace(queue=Legacy())})
        qm.save_all_queues({"gather": SimpleNamespace(queue=Raising())})

    assert [r for r in caplog.records if REPORT_TOKEN in r.getMessage()] == []
    # Maintenance itself must not have been reported as failed for the legacy shape.
    assert [r for r in caplog.records if "maintenance failed" in r.getMessage()] == []


# ---------------------------------------------------------------------------
# O6 - neither publisher may carry payload content
# ---------------------------------------------------------------------------
def test_o6_no_payload_secret_in_either_log_line(tmp_path, caplog):
    qm, stages, q = _manager_with_durable_stage(tmp_path)
    try:
        secret = "SUPER_SECRET_VALUE"
        assert q.put({"token": secret}, dedup_id="gather:p:six") is True
        assert q.put({"token": secret}, dedup_id="gather:p:six") is False

        with caplog.at_level(logging.INFO):
            qm.save_all_queues(stages)

        messages = [r.getMessage() for r in caplog.records]
        assert any(TOKEN in m for m in messages)
        assert any(REPORT_TOKEN in m for m in messages)
        for m in messages:
            assert secret not in m
    finally:
        q.close()
