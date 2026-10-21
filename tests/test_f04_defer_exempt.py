"""F-04: defer-aware age-purge exemption with queue schema v3.

Traceability: refactor wave-2, defect F-04 (BLOCKER до волны 5);
02_critical_fixes.md:60, 08_decisions.md:78 (OPEN-11 РЕШЁН, вариант A).

The age-purge predicate in ``_recover_on_open`` (``storage/task_queue.py``)
previously deleted every row older than ``max_age_hours`` regardless of state
or provenance.  With the C1 config-only fix (720 h) this wiped millions of
*untouched* pending rows — backlog that had never been attempted, never
deferred, merely queued faster than drain.  A naive exemption
(``pending AND attempts=0``) would immortalise defer-circulating tasks whose
only ceiling is the age gate (S13/S20).  The approved variant A adds two
columns (``defer_count``, ``last_defer_ts``) via an additive v2→v3 migration
and exempts only rows that have *never* been touched: ``pending AND
attempts=0 AND defer_count=0``.

Pins:
  P1 — fresh DB: user_version=3, columns exist, defaults 0/NULL.
  P2 — round-trip v2→v3 migration: hand-built v2 DB opened by product class
       ⇒ data intact, defer_count=0, user_version=3.
  P3 — defer increment: defer_task through stage+durable-queue ⇒ DB row
       defer_count=1, last_defer_ts not NULL; second defer ⇒ 2.
  P4 — exempt pin: pending/attempts=0/defer_count=0 row older than cutoff
       SURVIVES reopen.
  P5 — ceiling pin: same-age row with defer_count>0 is PURGED.
  P6 — attempts>0 old row is PURGED.

RED predictions (before product code):
  P1 — FAIL: SCHEMA_VERSION=2, columns absent.
  P2 — FAIL: migration code absent, user_version stays 2 or columns missing.
  P3 — FAIL: put_defer method absent (AttributeError).
  P4 — PASS (current purge deletes ALL old rows; exempt row would be deleted
       too — so this FAILS because the row does NOT survive).
       Correction: P4 FAILS because the old predicate deletes it.
  P5 — PASS under old code (old row purged regardless).  RED impossible for
       the purge half; the pin validates the *discrimination* (exempt vs
       non-exempt at same age).  Marked RED-impossible/insurance for purge.
  P6 — PASS under old code.  Same as P5: RED impossible for purge half.
"""

import json
import sqlite3
import time

import pytest

from storage.task_queue import SCHEMA_VERSION, SqliteTaskQueue

HOUR = 3600.0


def _q(path, name="f04", **kwargs):
    kwargs.setdefault("visibility_timeout_s", 300.0)
    kwargs.setdefault("max_age_hours", 720.0)
    return SqliteTaskQueue(
        str(path),
        serializer=json.dumps,
        deserializer=json.loads,
        name=name,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# P1: fresh DB schema
# ---------------------------------------------------------------------------
def test_p1_fresh_db_schema_version_3(tmp_path):
    """P1: a freshly created queue DB has user_version=3 and the two new
    columns with correct defaults."""
    path = tmp_path / "p1_fresh.sqlite"
    q = _q(path)
    try:
        assert SCHEMA_VERSION == 3, "SCHEMA_VERSION must be 3 for F-04"
        uv = q._conn.execute("PRAGMA user_version").fetchone()[0]
        assert uv == 3, f"user_version must be 3, got {uv}"

        cols = {
            row[1]: row
            for row in q._conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        assert "defer_count" in cols, "defer_count column must exist"
        assert "last_defer_ts" in cols, "last_defer_ts column must exist"

        # Insert a row and check defaults
        q.put({"label": "fresh"})
        row = q._conn.execute(
            "SELECT defer_count, last_defer_ts FROM tasks ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        assert row[0] == 0, "defer_count default must be 0"
        assert row[1] is None, "last_defer_ts default must be NULL"
    finally:
        q.close()


# ---------------------------------------------------------------------------
# P2: v2→v3 migration round-trip
# ---------------------------------------------------------------------------
def test_p2_v2_to_v3_migration(tmp_path):
    """P2: a hand-built v2 database (old DDL, user_version=2, pre-existing
    rows) is migrated in-place when opened by the product class: data intact,
    defer_count=0 on old rows, user_version=3."""
    path = tmp_path / "p2_migrate.sqlite"

    # Phase 1: build a v2-shaped database manually
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE tasks (
            seq           INTEGER PRIMARY KEY AUTOINCREMENT,
            dedup_id      TEXT NOT NULL,
            payload_json  TEXT NOT NULL,
            attempts      INTEGER NOT NULL DEFAULT 0,
            state         TEXT NOT NULL DEFAULT 'pending'
                          CHECK (state IN ('pending', 'claimed')),
            claimed_until REAL,
            created_at    REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_state_seq ON tasks(state, seq);
        CREATE INDEX IF NOT EXISTS idx_tasks_claimed ON tasks(claimed_until) WHERE state = 'claimed';
        CREATE INDEX IF NOT EXISTS idx_tasks_dedup_pending ON tasks(dedup_id, seq) WHERE state = 'pending' AND dedup_id <> '';
    """)
    now = time.time()
    conn.execute(
        "INSERT INTO tasks (dedup_id, payload_json, attempts, state, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("dedup-a", json.dumps({"label": "old_row"}), 0, "pending", now - 100 * HOUR),
    )
    conn.execute(
        "INSERT INTO tasks (dedup_id, payload_json, attempts, state, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("dedup-b", json.dumps({"label": "claimed_row"}), 1, "claimed", now - 50 * HOUR),
    )
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    # Phase 2: open through the product class — migration must run
    q = _q(path)
    try:
        uv = q._conn.execute("PRAGMA user_version").fetchone()[0]
        assert uv == 3, f"user_version must be 3 after migration, got {uv}"

        cols = {
            row[1]
            for row in q._conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        assert "defer_count" in cols
        assert "last_defer_ts" in cols

        # Old rows preserved with defer_count=0
        rows = q._conn.execute(
            "SELECT dedup_id, payload_json, attempts, state, defer_count FROM tasks ORDER BY seq"
        ).fetchall()
        assert len(rows) == 2, "migration must not lose rows"
        assert rows[0][0] == "dedup-a"
        assert json.loads(rows[0][1]) == {"label": "old_row"}
        assert rows[0][4] == 0, "migrated rows get defer_count=0"
        assert rows[1][0] == "dedup-b"
        assert rows[1][4] == 0
    finally:
        q.close()


# ---------------------------------------------------------------------------
# P3: defer increment through stage path
# ---------------------------------------------------------------------------
def test_p3_defer_increment_via_stage(tmp_path):
    """P3: defer_task through a stage with a durable queue increments
    defer_count in the DB row.  First defer ⇒ defer_count=1, second ⇒ 2."""
    from config.schemas import Config, StageConfig, TaskConfig
    from core.models import Patterns, SearchTask
    from search import client as search_client
    from stage.base import StageResources
    from stage.definition import SearchStage
    from tools.state import CredentialsExhausted

    provider = "deepseek"
    key_pattern = r"sk-[A-Za-z0-9]{16}"

    class ExhaustedAuth:
        def get_token(self):
            raise CredentialsExhausted(service="github_api", reason="test", wait_estimate_s=900.0)
        def get_session(self):
            raise CredentialsExhausted(service="github_api", reason="test", wait_estimate_s=900.0)
        def get_user_agent(self):
            return "test-agent"

    cfg = Config()
    cfg.pipeline.failure_handling = "strict"
    task_cfg = TaskConfig(
        name=provider, enabled=True, provider_type="openai_like", use_api=True,
        stages=StageConfig(search=True, gather=True, check=True, inspect=True),
        patterns=Patterns(key_pattern=key_pattern),
    )
    resources = StageResources(
        limiter=None, providers={}, config=cfg,
        task_configs={provider: task_cfg},
        auth=ExhaustedAuth(), registry=None,
    )
    stage = SearchStage(
        resources, lambda out: None, thread_count=1, max_retries=0,
        queue_backend="sqlite",
        queue_dir=str(tmp_path / "queue_state"),
        queue_visibility_timeout_s=300,
        queue_max_age_hours=720,
    )

    task = SearchTask(provider=provider, query='"sk-"', regex=key_pattern, page=1, use_api=True)

    # First put (simulating initial enqueue)
    stage.queue.put(task, dedup_id=stage._generate_id(task),
                    created_at=task.created_at, attempts=task.attempts)

    # Claim the row (simulating worker get)
    claimed = stage.queue.get(timeout=1.0)

    # First defer
    assert stage.defer_task(claimed) is True
    rows = stage.queue._conn.execute(
        "SELECT defer_count, last_defer_ts FROM tasks WHERE state='pending' ORDER BY seq"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 1, f"first defer ⇒ defer_count=1, got {rows[0][0]}"
    assert rows[0][1] is not None, "first defer ⇒ last_defer_ts not NULL"

    # Claim again and defer again
    claimed2 = stage.queue.get(timeout=1.0)
    assert stage.defer_task(claimed2) is True
    rows2 = stage.queue._conn.execute(
        "SELECT defer_count, last_defer_ts FROM tasks WHERE state='pending' ORDER BY seq"
    ).fetchall()
    assert len(rows2) == 1
    assert rows2[0][0] == 2, f"second defer ⇒ defer_count=2, got {rows2[0][0]}"

    stage.queue.close()


# ---------------------------------------------------------------------------
# P4: exempt pin — untouched old row survives reopen
# ---------------------------------------------------------------------------
def test_p4_exempt_pending_row_survives_reopen(tmp_path):
    """P4: a pending row with attempts=0 and defer_count=0 older than
    max_age_hours SURVIVES a reopen (exempt from purge)."""
    path = tmp_path / "p4_exempt.sqlite"

    q1 = _q(path, max_age_hours=720.0)
    ancient = time.time() - 721 * HOUR  # beyond the gate
    q1.put({"label": "untouched_backlog"}, created_at=ancient)
    assert q1.qsize() == 1
    q1.close()

    # Reopen — the untouched row must survive
    q2 = _q(path, max_age_hours=720.0)
    try:
        assert q2.qsize() == 1, (
            "untouched pending row (attempts=0, defer_count=0) must survive purge"
        )
    finally:
        q2.close()


# ---------------------------------------------------------------------------
# P5: ceiling pin — defer-circulating old row is purged
# ---------------------------------------------------------------------------
def test_p5_defer_circulating_old_row_is_purged(tmp_path):
    """P5: a pending row with defer_count>0 older than max_age_hours is
    PURGED on reopen (the natural ceiling of S13/S20 is preserved)."""
    path = tmp_path / "p5_ceiling.sqlite"

    # Create a fresh DB, insert a row, then manually set defer_count>0
    q1 = _q(path, max_age_hours=720.0)
    ancient = time.time() - 721 * HOUR
    q1.put({"label": "circulating"}, created_at=ancient)
    # Simulate that this row has been deferred
    q1._conn.execute(
        "UPDATE tasks SET defer_count = 1, last_defer_ts = ? WHERE seq = (SELECT MIN(seq) FROM tasks)",
        (time.time(),),
    )
    q1._conn.commit()
    assert q1.qsize() == 1
    q1.close()

    # Reopen — the circulating row must be purged
    q2 = _q(path, max_age_hours=720.0)
    try:
        assert q2.qsize() == 0, (
            "defer-circulating row (defer_count>0) older than max_age_hours must be purged"
        )
    finally:
        q2.close()


# ---------------------------------------------------------------------------
# P6: attempts>0 old row is purged
# ---------------------------------------------------------------------------
def test_p6_attempts_old_row_is_purged(tmp_path):
    """P6: a pending row with attempts>0 older than max_age_hours is PURGED
    on reopen (retry-circulating rows still hit the ceiling)."""
    path = tmp_path / "p6_attempts.sqlite"

    q1 = _q(path, max_age_hours=720.0)
    ancient = time.time() - 721 * HOUR
    q1.put({"label": "retried"}, created_at=ancient, attempts=1)
    assert q1.qsize() == 1
    q1.close()

    # Reopen — the retried row must be purged
    q2 = _q(path, max_age_hours=720.0)
    try:
        assert q2.qsize() == 0, (
            "retry-circulating row (attempts>0) older than max_age_hours must be purged"
        )
    finally:
        q2.close()
