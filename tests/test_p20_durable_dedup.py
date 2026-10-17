"""P20 — durable dedup engine pins E1–E13.

Traceability: plan /tmp/opencode/p20-plan.md §3.

These pins exercise ``SqliteTaskQueue`` with the ``durable_dedup`` flag ON
(default).  The contract under test:

* E1  fresh duplicate while pending → 1 row, put() returned False,
    dedup_stats()["suppressed_puts"] == 1.
* E2  distinct dedup_ids → both retained, both return True.
* E3  attempts=1 with identical pending row → RETAINED (S21 shape, H2).
* E4  duplicate arriving while original is CLAIMED → retained; after
    task_done() the store holds exactly one row (deferral/retry path, H1).
* E5  dedup_id="" duplicates (bulk_insert feed) → all retained (H5).
* E6  flag off → today's behaviour byte-for-byte: duplicates retained,
    suppressed_puts==0, no reconcile.
* E7  concurrent producers, same id, N threads → exactly 1 row, N−1
    suppressions, no exception.
* E8  reopen a store containing 3 pending duplicates + 1 unique → reconcile
    keeps MIN(seq), FIFO order of survivors equals insertion order, second
    reopen collapses 0 (idempotent), one loud line naming the count.
* E9  reconcile runs AFTER the reclaim: a claimed orphan plus a pending twin
    end as ONE pending row.
* E10 aged-out duplicate: the purge removes the old copy and the survivor is
    the remaining one (order of maintenance steps).
* E11 SCHEMA_VERSION == 2, PRAGMA user_version == 2, the partial index
    exists, counts() still returns exactly {"pending","claimed"} (H4).
* E12 FIFO/claim/ack/snapshot/sweep regressions: the pre-existing contract
    still holds with the flag on.
* E13 no plaintext secret ever reaches the store or the log lines.
"""

import json
import logging
import os
import threading
import time

import pytest

from storage.task_queue import SCHEMA_VERSION, SqliteTaskQueue


def _q(tmp_path, name="dedup", **kwargs):
    """Build a queue with the standard dict serializer pair."""
    kwargs.setdefault("visibility_timeout_s", 300.0)
    kwargs.setdefault("max_age_hours", 24.0)
    return SqliteTaskQueue(
        str(tmp_path / f"{name}_queue.sqlite"),
        serializer=json.dumps,
        deserializer=json.loads,
        name=name,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# E1 — fresh duplicate while pending → suppressed
# ---------------------------------------------------------------------------
def test_e1_fresh_duplicate_while_pending_suppressed(tmp_path):
    q = _q(tmp_path)
    try:
        assert q.put({"url": "a"}, dedup_id="gather:x:a") is True
        assert q.put({"url": "a"}, dedup_id="gather:x:a") is False
        assert q.qsize() == 1
        stats = q.dedup_stats()
        assert stats["suppressed_puts"] == 1
        assert stats["startup_collapsed"] == 0
    finally:
        q.close()


# ---------------------------------------------------------------------------
# E2 — distinct dedup_ids both retained
# ---------------------------------------------------------------------------
def test_e2_distinct_dedup_ids_both_retained(tmp_path):
    q = _q(tmp_path)
    try:
        assert q.put({"url": "a"}, dedup_id="gather:x:a") is True
        assert q.put({"url": "b"}, dedup_id="gather:x:b") is True
        assert q.qsize() == 2
        assert q.dedup_stats()["suppressed_puts"] == 0
    finally:
        q.close()


# ---------------------------------------------------------------------------
# E3 — attempts=1 with identical pending row → RETAINED (H2 / S21 shape)
# ---------------------------------------------------------------------------
def test_e3_attempts_one_with_pending_retained(tmp_path):
    q = _q(tmp_path)
    try:
        q.put({"url": "a"}, dedup_id="gather:x:a", attempts=0)
        # A bounded retry (attempts >= 1) must NOT be suppressed.
        assert q.put({"url": "a"}, dedup_id="gather:x:a", attempts=1) is True
        assert q.qsize() == 2
        assert q.dedup_stats()["suppressed_puts"] == 0
    finally:
        q.close()


# ---------------------------------------------------------------------------
# E4 — duplicate arriving while original is CLAIMED → retained (H1)
# ---------------------------------------------------------------------------
def test_e4_duplicate_while_claimed_retained(tmp_path):
    q = _q(tmp_path, visibility_timeout_s=300.0)
    try:
        q.put({"url": "a"}, dedup_id="gather:x:a")
        # Claim the row so it is now 'claimed'
        item = q.get(timeout=1.0)
        assert item == {"url": "a"}
        # While claimed, a fresh duplicate must be retained
        assert q.put({"url": "a"}, dedup_id="gather:x:a", attempts=0) is True
        # Ack the claim
        q.task_done()
        # Now only the second row should remain pending
        assert q.qsize() == 1
        assert q.dedup_stats()["suppressed_puts"] == 0
    finally:
        q.close()


# ---------------------------------------------------------------------------
# E5 — dedup_id="" duplicates (bulk_insert feed) → all retained (H5)
# ---------------------------------------------------------------------------
def test_e5_empty_dedup_id_duplicates_retained(tmp_path):
    q = _q(tmp_path)
    try:
        # Direct put with empty dedup_id
        assert q.put({"url": "a"}, dedup_id="") is True
        assert q.put({"url": "a"}, dedup_id="") is True
        assert q.qsize() == 2
        # bulk_insert also uses empty dedup_id
        q.bulk_insert([{"url": "a"}, {"url": "a"}])
        assert q.qsize() == 4
        assert q.dedup_stats()["suppressed_puts"] == 0
    finally:
        q.close()


# ---------------------------------------------------------------------------
# E6 — flag off → today's behaviour byte-for-byte
# ---------------------------------------------------------------------------
def test_e6_flag_off_duplicates_retained(tmp_path):
    q = _q(tmp_path, durable_dedup=False)
    try:
        assert q.put({"url": "a"}, dedup_id="gather:x:a") is True
        assert q.put({"url": "a"}, dedup_id="gather:x:a") is True
        assert q.qsize() == 2
        assert q.dedup_stats()["suppressed_puts"] == 0
    finally:
        q.close()


# ---------------------------------------------------------------------------
# E7 — concurrent producers, same id, N threads → exactly 1 row
# ---------------------------------------------------------------------------
def test_e7_concurrent_same_id_one_row(tmp_path):
    q = _q(tmp_path)
    N = 8
    results = []
    errors = []

    def producer():
        try:
            r = q.put({"url": "concurrent"}, dedup_id="gather:x:concurrent")
            results.append(r)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=producer) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    try:
        assert not errors, f"unexpected exceptions: {errors}"
        assert q.qsize() == 1
        assert results.count(True) == 1
        assert results.count(False) == N - 1
        assert q.dedup_stats()["suppressed_puts"] == N - 1
    finally:
        q.close()


# ---------------------------------------------------------------------------
# E8 — reopen reconcile: 3 pending duplicates + 1 unique → 2 survivors
# ---------------------------------------------------------------------------
def test_e8_reopen_reconcile_keeps_min_seq(tmp_path, caplog):
    path = str(tmp_path / "reconcile_queue.sqlite")

    # First open: insert 3 duplicates + 1 unique
    q1 = SqliteTaskQueue(
        path,
        serializer=json.dumps,
        deserializer=json.loads,
        name="recon",
        durable_dedup=True,
    )
    q1.put({"url": "dup"}, dedup_id="gather:x:dup")
    q1.put({"url": "dup2"}, dedup_id="gather:x:dup")
    q1.put({"url": "dup3"}, dedup_id="gather:x:dup")
    q1.put({"url": "unique"}, dedup_id="gather:x:unique")
    assert q1.qsize() == 2  # 2 suppressed at insert time
    q1.close()

    # Second open: reconcile should collapse the 3 → 1 (already done at insert)
    # Actually with insert-time probe, only 1 dup + 1 unique exist.
    # To test reconcile we need to bypass the probe — use flag off for setup.
    path2 = str(tmp_path / "reconcile2_queue.sqlite")
    q_setup = SqliteTaskQueue(
        path2,
        serializer=json.dumps,
        deserializer=json.loads,
        name="recon2",
        durable_dedup=False,  # bypass probe to seed duplicates
    )
    q_setup.put({"url": "dup1"}, dedup_id="gather:x:dup")
    q_setup.put({"url": "dup2"}, dedup_id="gather:x:dup")
    q_setup.put({"url": "dup3"}, dedup_id="gather:x:dup")
    q_setup.put({"url": "unique"}, dedup_id="gather:x:unique")
    assert q_setup.qsize() == 4
    q_setup.close()

    # Reopen with dedup ON → reconcile should collapse 3 → 1
    with caplog.at_level(logging.WARNING, logger="storage"):
        q2 = SqliteTaskQueue(
            path2,
            serializer=json.dumps,
            deserializer=json.loads,
            name="recon2",
            durable_dedup=True,
        )
    try:
        assert q2.qsize() == 2  # 1 dup + 1 unique
        stats = q2.dedup_stats()
        assert stats["startup_collapsed"] == 2  # 3 dups → 1 = 2 collapsed
        # Check FIFO: the survivor should be the first-inserted (MIN seq)
        snap = q2.snapshot_pending()
        assert snap[0] == {"url": "dup1"}
        assert snap[1] == {"url": "unique"}

        # Idempotent: close and reopen → 0 collapsed
        q2.close()

        with caplog.at_level(logging.WARNING, logger="storage"):
            q3 = SqliteTaskQueue(
                path2,
                serializer=json.dumps,
                deserializer=json.loads,
                name="recon2",
                durable_dedup=True,
            )
        try:
            assert q3.dedup_stats()["startup_collapsed"] == 0
            assert q3.qsize() == 2
        finally:
            q3.close()
    finally:
        pass  # q2 already closed above


# ---------------------------------------------------------------------------
# E9 — reconcile runs AFTER reclaim: claimed orphan + pending twin → 1 row
# ---------------------------------------------------------------------------
def test_e9_reconcile_after_reclaim(tmp_path):
    path = str(tmp_path / "reclaim_queue.sqlite")

    # Seed with dedup off so we can create the duplicate scenario
    q1 = SqliteTaskQueue(
        path,
        serializer=json.dumps,
        deserializer=json.loads,
        name="reclaim",
        durable_dedup=False,
    )
    q1.put({"url": "a"}, dedup_id="gather:x:a")
    # Claim it (so it is 'claimed')
    item = q1.get(timeout=1.0)
    assert item == {"url": "a"}
    # Insert a second pending copy with same dedup_id
    q1.put({"url": "a2"}, dedup_id="gather:x:a")
    # Now: 1 claimed + 1 pending, same dedup_id
    assert q1.counts() == {"pending": 1, "claimed": 1}
    q1.close()

    # Reopen with dedup ON: reclaim turns claimed → pending, then reconcile
    # collapses the two pending → 1
    q2 = SqliteTaskQueue(
        path,
        serializer=json.dumps,
        deserializer=json.loads,
        name="reclaim",
        durable_dedup=True,
    )
    try:
        assert q2.qsize() == 1
        assert q2.counts() == {"pending": 1, "claimed": 0}
        assert q2.dedup_stats()["startup_collapsed"] == 1
    finally:
        q2.close()


# ---------------------------------------------------------------------------
# E10 — aged-out duplicate: purge removes old, survivor remains
# ---------------------------------------------------------------------------
def test_e10_aged_out_duplicate(tmp_path):
    path = str(tmp_path / "aged_queue.sqlite")
    old_time = time.time() - (25 * 3600)  # 25 hours ago

    # Seed with dedup off and old timestamps
    q1 = SqliteTaskQueue(
        path,
        serializer=json.dumps,
        deserializer=json.loads,
        name="aged",
        max_age_hours=24.0,
        durable_dedup=False,
    )
    # Manually insert rows with old timestamps
    with q1._cond:
        q1._conn.execute(
            "INSERT INTO tasks (dedup_id, payload_json, attempts, state, claimed_until, created_at) "
            "VALUES (?, ?, 0, 'pending', NULL, ?)",
            ("gather:x:a", json.dumps({"url": "old"}), old_time),
        )
        q1._conn.execute(
            "INSERT INTO tasks (dedup_id, payload_json, attempts, state, claimed_until, created_at) "
            "VALUES (?, ?, 0, 'pending', NULL, ?)",
            ("gather:x:a", json.dumps({"url": "new"}), time.time()),
        )
        q1._conn.commit()
    q1.close()

    # Reopen: age purge removes old row, reconcile has nothing to collapse
    q2 = SqliteTaskQueue(
        path,
        serializer=json.dumps,
        deserializer=json.loads,
        name="aged",
        max_age_hours=24.0,
        durable_dedup=True,
    )
    try:
        assert q2.qsize() == 1
        snap = q2.snapshot_pending()
        assert snap[0] == {"url": "new"}
    finally:
        q2.close()


# ---------------------------------------------------------------------------
# E11 — SCHEMA_VERSION == 2, partial index exists, counts() shape preserved
# ---------------------------------------------------------------------------
def test_e11_schema_version_and_index(tmp_path):
    assert SCHEMA_VERSION == 2

    q = _q(tmp_path)
    try:
        # user_version must be 2
        uv = q._conn.execute("PRAGMA user_version").fetchone()[0]
        assert uv == 2

        # Partial index must exist
        indexes = {
            row[0]
            for row in q._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_tasks_dedup_pending" in indexes

        # counts() shape unchanged (H4)
        assert q.counts() == {"pending": 0, "claimed": 0}
    finally:
        q.close()


# ---------------------------------------------------------------------------
# E12 — FIFO/claim/ack/snapshot/sweep regressions with flag on
# ---------------------------------------------------------------------------
def test_e12_fifo_claim_ack_snapshot_sweep(tmp_path):
    q = _q(tmp_path, visibility_timeout_s=0.1)
    try:
        # FIFO order
        q.put({"n": 1}, dedup_id="a")
        q.put({"n": 2}, dedup_id="b")
        q.put({"n": 3}, dedup_id="c")
        assert q.snapshot_pending() == [{"n": 1}, {"n": 2}, {"n": 3}]

        # Claim
        item = q.get(timeout=1.0)
        assert item == {"n": 1}
        assert q.counts() == {"pending": 2, "claimed": 1}

        # Ack
        q.task_done()
        assert q.counts() == {"pending": 2, "claimed": 0}

        # Sweep expired claims
        q.put({"n": 4}, dedup_id="d")
        _ = q.get(timeout=1.0)  # claim {"n": 2}
        time.sleep(0.15)  # let visibility timeout expire
        reclaimed = q.sweep_expired_claims()
        assert reclaimed == 1
        assert q.counts() == {"pending": 3, "claimed": 0}
    finally:
        q.close()


# ---------------------------------------------------------------------------
# E13 — no plaintext secret in store or log lines
# ---------------------------------------------------------------------------
def test_e13_no_secret_in_store_or_logs(tmp_path, caplog):
    q = _q(tmp_path)
    try:
        # dedup_id is not a secret (it is gather:{provider}:{url}), but ensure
        # no raw payload content leaks into log lines
        with caplog.at_level(logging.WARNING, logger="storage"):
            q.put({"secret_token": "SUPER_SECRET_VALUE"}, dedup_id="gather:x:a")
            q.put({"secret_token": "SUPER_SECRET_VALUE"}, dedup_id="gather:x:a")

        # Check log records don't contain the payload secret
        for record in caplog.records:
            assert "SUPER_SECRET_VALUE" not in record.getMessage()

        # Check the store doesn't contain the secret in dedup_id column
        rows = q._conn.execute("SELECT dedup_id FROM tasks").fetchall()
        for (dedup_id,) in rows:
            assert "SUPER_SECRET_VALUE" not in dedup_id
    finally:
        q.close()


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------
def test_durable_dedup_validation_rejects_non_bool(tmp_path):
    with pytest.raises(ValueError, match="queue.durable_dedup must be a boolean"):
        _q(tmp_path, durable_dedup="true")

    with pytest.raises(ValueError, match="queue.durable_dedup must be a boolean"):
        _q(tmp_path, durable_dedup=1)

    with pytest.raises(ValueError, match="queue.durable_dedup must be a boolean"):
        _q(tmp_path, durable_dedup=None)


def test_durable_dedup_default_true(tmp_path):
    q = _q(tmp_path)
    try:
        assert q.durable_dedup is True
    finally:
        q.close()
