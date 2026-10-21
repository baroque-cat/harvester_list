"""NEW-7 micro-set + C10 dead-surface retirement (slice 2.8).

Traceability: 02_critical_fixes.md NEW-7 (LOW), C10 (NOTE).

Pins:
    P1 — shadow_jaccard window is bounded (deque maxlen).
    P2 — min/p95 are exact for series <= maxlen (known values).
    P3 — dropped counter has a lock (_dropped_lock attribute exists).
    P3b — deterministic dropped accounting: filled queue + sequential _enqueue.
    P4 — bulk_insert legacy rows carry dedup_id='' and survive reconcile.
    P5 — GracefulShutdown removed from manager.queue and manager.__all__.
    P6 — get_total_num removed from search.client and search.__all__.
    P7 — build_all_snapshots_all removed from MultiResultManager;
         build_all_snapshots (alive) still exists.
"""

import json
import os
import threading

import pytest

from search.aggregation import AggregationMetrics


# ---------------------------------------------------------------------------
# P1: shadow_jaccard window bounded
# ---------------------------------------------------------------------------
def test_p1_shadow_jaccard_window_bounded():
    """record_shadow_jaccard beyond maxlen must not grow unboundedly."""
    m = AggregationMetrics()
    maxlen = getattr(m.shadow_jaccard, "maxlen", None)
    assert maxlen is not None, "shadow_jaccard must be a deque with maxlen"
    assert maxlen > 0

    # Push well beyond maxlen.
    for i in range(maxlen + 500):
        m.record_shadow_jaccard(i / (maxlen + 500))

    assert len(m.shadow_jaccard) == maxlen
    # Oldest values evicted: first value (0.0) must be gone.
    assert 0.0 not in list(m.shadow_jaccard)


# ---------------------------------------------------------------------------
# P2: min/p95 exact for known series <= maxlen
# ---------------------------------------------------------------------------
def test_p2_shadow_jaccard_min_p95_exact():
    """For a known series within the window, min and p95 are deterministic."""
    m = AggregationMetrics()
    values = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    for v in values:
        m.record_shadow_jaccard(v)

    assert m.shadow_jaccard_min == pytest.approx(0.1)

    # p95: sorted = [0.1..1.0], len=10, index = ceil(0.95*10)-1 = 10-1 = 9
    # ordered[9] = 1.0
    assert m.shadow_jaccard_p95 == pytest.approx(1.0)

    # Empty case
    m2 = AggregationMetrics()
    assert m2.shadow_jaccard_min == 1.0
    assert m2.shadow_jaccard_p95 == 1.0


# ---------------------------------------------------------------------------
# P3: dropped lock exists
# ---------------------------------------------------------------------------
def test_p3_dropped_lock_exists(workspace):
    """Registry must have a _dropped_lock for the dropped counter."""
    from storage.registry import Registry

    registry = Registry(workspace, enabled=True)
    registry.start()
    try:
        assert hasattr(registry, "_dropped_lock"), (
            "Registry must have _dropped_lock attribute"
        )
        assert isinstance(registry._dropped_lock, type(threading.Lock()))
    finally:
        registry.stop()


# ---------------------------------------------------------------------------
# P3b: deterministic dropped accounting
# ---------------------------------------------------------------------------
def test_p3b_dropped_deterministic_sequential(workspace):
    """Fill the queue then _enqueue sequentially; dropped must be exact."""
    from storage.registry import Registry

    # Use a tiny config to get a small queue.
    class _Cfg:
        enabled = True
        batch_size = 50
        flush_interval = 5
        queue_size = 2
        path = ""

    registry = Registry(workspace, config=_Cfg())
    registry.start()
    try:
        # Stop the writer thread so it doesn't consume items.
        registry._stop_event.set()
        if registry._writer is not None:
            registry._writer.join(timeout=2.0)
        
        # Fill the queue to capacity.
        while registry._queue.qsize() < 2:
            registry._enqueue("noop", {"k": "fill"})

        # Now 100 sequential enqueues should all drop.
        for _ in range(100):
            registry._enqueue("noop", {"k": "drop"})

        assert registry.dropped == 100
    finally:
        registry._stop_event.clear()
        registry.stop()


# ---------------------------------------------------------------------------
# P4: bulk_insert legacy invariant
# ---------------------------------------------------------------------------
def test_p4_bulk_insert_legacy_dedup_id_empty(tmp_path):
    """bulk_insert writes dedup_id=''; two identical legacy rows survive reopen."""
    from storage.task_queue import SqliteTaskQueue

    path = str(tmp_path / "queue.sqlite")
    q = SqliteTaskQueue(
        path,
        serializer=json.dumps,
        deserializer=json.loads,
        name="test",
        visibility_timeout_s=300.0,
        max_age_hours=24.0,
    )
    try:
        # Two identical payloads via bulk_insert.
        objs = [{"url": "same"}, {"url": "same"}]
        inserted = q.bulk_insert(objs)
        assert inserted == 2

        # Both rows have dedup_id=''.
        import sqlite3

        conn = sqlite3.connect(path)
        try:
            rows = conn.execute(
                "SELECT dedup_id FROM tasks ORDER BY seq"
            ).fetchall()
            assert len(rows) == 2
            assert all(r[0] == "" for r in rows)
        finally:
            conn.close()

        # Reopen: both rows survive (reconcile does not collapse dedup_id='').
        q.close()
        q2 = SqliteTaskQueue(
            path,
            serializer=json.dumps,
            deserializer=json.loads,
            name="test",
            visibility_timeout_s=300.0,
            max_age_hours=24.0,
        )
        try:
            assert q2.qsize() == 2
        finally:
            q2.close()
    finally:
        try:
            q.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# P5: GracefulShutdown removed
# ---------------------------------------------------------------------------
def test_p5_graceful_shutdown_removed():
    """GracefulShutdown must not exist on manager.queue or manager.__all__."""
    import manager
    import manager.queue as mq

    assert not hasattr(mq, "GracefulShutdown"), (
        "GracefulShutdown class must be removed from manager.queue"
    )
    assert "GracefulShutdown" not in manager.__all__, (
        "GracefulShutdown must be removed from manager.__all__"
    )


# ---------------------------------------------------------------------------
# P6: get_total_num removed
# ---------------------------------------------------------------------------
def test_p6_get_total_num_removed():
    """get_total_num must not exist on search.client or search.__all__."""
    import search
    import search.client as sc

    assert not hasattr(sc, "get_total_num"), (
        "get_total_num must be removed from search.client"
    )
    assert "get_total_num" not in search.__all__, (
        "get_total_num must be removed from search.__all__"
    )


# ---------------------------------------------------------------------------
# P7: build_all_snapshots_all removed, build_all_snapshots alive
# ---------------------------------------------------------------------------
def test_p7_build_all_snapshots_all_removed():
    """build_all_snapshots_all dead; build_all_snapshots (alive) untouched."""
    from storage.persistence import MultiResultManager, ResultManager

    assert not hasattr(MultiResultManager, "build_all_snapshots_all"), (
        "build_all_snapshots_all must be removed from MultiResultManager"
    )
    assert hasattr(ResultManager, "build_all_snapshots"), (
        "build_all_snapshots (alive, per-provider ResultManager) must NOT be removed"
    )
