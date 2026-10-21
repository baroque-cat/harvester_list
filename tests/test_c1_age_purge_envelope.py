"""C1: queue age gate raised to 720h — purge envelope at the new boundary.

Traceability: refactor wave-1, defect C1 (BLOCKER, config-only).

The ``max_age_hours`` configuration was raised from 24 to 720 (30 days) so that
restarts no longer wipe the deferred backlog accumulated during a coverage wave
(430–690k rows measured at 24h).  The code in ``storage/task_queue.py`` is
unchanged — only the configured value differs.  These pins lock the new
operational envelope: a pending row at 719h survives a reopen, a row at 721h
does not.

Pins:
  1. A pending row with created_at = now − 719h SURVIVES a reopen with
     max_age_hours=720 (within the 30-day gate).
  2. A pending row with created_at = now − 721h is PURGED on reopen with
     max_age_hours=720 (outside the 30-day gate).

No network, no live GitHub.  RED is impossible for this config-only change —
the pins lock the new operational envelope, mirroring the C10 pattern from
slice 1.4.
"""

import json
import time

from storage.task_queue import SqliteTaskQueue


HOUR = 3600.0


def _q(path, name="c1", **kwargs):
    """Construct a SqliteTaskQueue with sensible test defaults."""
    kwargs.setdefault("visibility_timeout_s", 300.0)
    kwargs.setdefault("max_age_hours", 720.0)
    return SqliteTaskQueue(
        str(path),
        serializer=json.dumps,
        deserializer=json.loads,
        name=name,
        **kwargs,
    )


def test_c1_pin1_row_within_720h_survives_reopen(tmp_path):
    """C1 pin 1: a pending row aged ~719h is preserved when the queue reopens
    with max_age_hours=720 (within the 30-day gate)."""
    path = tmp_path / "c1_survive.sqlite"

    # Phase 1: insert a row with created_at = now − 719h
    q1 = _q(path, max_age_hours=720.0)
    near_boundary = time.time() - 719 * HOUR
    q1.put({"label": "near_boundary"}, created_at=near_boundary)
    q1.put({"label": "fresh"})
    assert q1.qsize() == 2
    q1.close()

    # Phase 2: reopen — the 719h row must survive the purge
    q2 = _q(path, max_age_hours=720.0)
    try:
        assert q2.qsize() == 2, (
            "row at 719h must survive with max_age_hours=720"
        )
    finally:
        q2.close()


def test_c1_pin2_row_beyond_720h_is_purged(tmp_path):
    """C1 pin 2: a pending row aged ~721h is purged when the queue reopens
    with max_age_hours=720 (outside the 30-day gate).

    F-04 adaptation: the row is inserted with attempts=1 so it is a
    *circulating* row (not an untouched backlog entry).  Untouched pending
    rows (attempts=0, defer_count=0) are now exempt from the age gate — the
    natural ceiling of S13/S20 is preserved for retry/defer-circulating
    tasks.  This pin verifies that discrimination.
    """
    path = tmp_path / "c1_purge.sqlite"

    # Phase 1: insert rows — one beyond the gate (circulating), one within
    q1 = _q(path, max_age_hours=720.0)
    beyond_boundary = time.time() - 721 * HOUR
    q1.put({"label": "beyond_boundary"}, created_at=beyond_boundary, attempts=1)
    q1.put({"label": "fresh"})
    assert q1.qsize() == 2
    q1.close()

    # Phase 2: reopen — the 721h circulating row must be purged
    q2 = _q(path, max_age_hours=720.0)
    try:
        assert q2.qsize() == 1, (
            "circulating row (attempts=1) at 721h must be purged with max_age_hours=720"
        )
        remaining = q2.get(timeout=1.0)
        assert remaining == {"label": "fresh"}
        q2.task_done()
    finally:
        q2.close()
