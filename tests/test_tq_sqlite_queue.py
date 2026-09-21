"""SqliteTaskQueue engine contract (fix-queue-persistence-under-load).

Traceability: durable-task-queue-S7 .. S15.

Module under test ``storage/task_queue.py`` does NOT exist yet - the
ModuleNotFoundError on import is the expected RED state for this whole file
(tests.md group header).  Engine-level tests drive the queue with plain dict
payloads (serializer=json.dumps / deserializer=json.loads); S12 wires the
stage-equivalent pair (ProviderTask.to_dict / TaskFactory.from_dict) and so
also pins the captured snapshot payload format through the durable store.

Contract exercised here (design D1/D2/D3/D5):
    SqliteTaskQueue(path, *, serializer, deserializer, name="",
                    visibility_timeout_s=300.0, max_age_hours=24.0)
    put(obj, timeout=None, *, dedup_id="", created_at=None)  # never queue.Full
    get(timeout=None) -> obj                 # claims; raises queue.Empty
    task_done()                              # acks (deletes) this thread's claim
    qsize()/empty()/counts()/snapshot_pending()/sweep_expired_claims()/
    bulk_insert(objs)/checkpoint()/close()/journal_mode
"""

import gc
import json
import queue as queue_mod
import threading
import time

import pytest

from storage.task_queue import SqliteTaskQueue  # RED driver: absent pre-fix


def _q(tmp_path, name="test", **kwargs):
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
# durable-task-queue-S7
# ---------------------------------------------------------------------------
def test_s7_dequeue_order_equals_insertion_order(tmp_path):
    q = _q(tmp_path)
    try:
        payloads = [{"n": i} for i in range(9)]
        for p in payloads:
            q.put(p)

        got = []
        for _ in payloads:
            got.append(q.get(timeout=1.0))
            q.task_done()

        assert got == payloads  # strict FIFO by monotonic seq
        assert q.empty()
    finally:
        q.close()


# ---------------------------------------------------------------------------
# durable-task-queue-S8
# ---------------------------------------------------------------------------
def test_s8_claim_is_exclusive_until_acknowledged(tmp_path):
    q = _q(tmp_path)
    try:
        q.put({"n": 1})
        q.put({"n": 2})

        first = q.get(timeout=1.0)
        assert first == {"n": 1}
        # claimed row is invisible to other consumers and excluded from pending
        assert q.qsize() == 1
        assert q.counts() == {"pending": 1, "claimed": 1}

        second = q.get(timeout=1.0)
        assert second == {"n": 2}
        # both rows claimed now -> store looks empty but nothing is acked yet
        assert q.empty()
        assert q.counts() == {"pending": 0, "claimed": 2}
        with pytest.raises(queue_mod.Empty):
            q.get(timeout=0.05)  # exclusive claims: no third dequeue possible

        q.task_done()  # ack first claim (thread-local stack: oldest first)
        q.task_done()
        assert q.counts() == {"pending": 0, "claimed": 0}
    finally:
        q.close()


# ---------------------------------------------------------------------------
# durable-task-queue-S9
# ---------------------------------------------------------------------------
def test_s9_empty_store_dequeue_honors_timeout_and_raises_empty(tmp_path):
    q = _q(tmp_path)
    try:
        start = time.time()
        with pytest.raises(queue_mod.Empty):
            q.get(timeout=0.2)
        elapsed = time.time() - start
        assert elapsed >= 0.15, f"get() returned too early: {elapsed:.3f}s"
        # get_nowait on empty store raises immediately
        with pytest.raises(queue_mod.Empty):
            q.get_nowait()
    finally:
        q.close()


# ---------------------------------------------------------------------------
# durable-task-queue-S10
# ---------------------------------------------------------------------------
def test_s10_expired_claims_return_to_pending_on_sweep(tmp_path):
    q = _q(tmp_path, visibility_timeout_s=0.05)
    try:
        q.put({"n": 42})
        claimed = q.get(timeout=1.0)
        assert claimed == {"n": 42}
        assert q.counts()["claimed"] == 1
        # not expired yet -> sweep finds nothing
        assert q.sweep_expired_claims() == 0

        time.sleep(0.1)
        assert q.sweep_expired_claims() == 1
        assert q.qsize() == 1

        again = q.get(timeout=1.0)  # re-claimable exactly once
        assert again == {"n": 42}
        q.task_done()
        assert q.empty()
    finally:
        q.close()


# ---------------------------------------------------------------------------
# durable-task-queue-S11
# ---------------------------------------------------------------------------
def test_s11_unacknowledged_claims_survive_reopen_as_pending(tmp_path):
    path = str(tmp_path / "crash_queue.sqlite")
    q1 = SqliteTaskQueue(
        path, serializer=json.dumps, deserializer=json.loads, name="crash"
    )
    q1.put({"payload": "precious"})
    claimed = q1.get(timeout=1.0)
    assert claimed == {"payload": "precious"}
    # simulate SIGKILL: abandon the instance mid-claim, no ack, no close
    del q1
    gc.collect()

    q2 = SqliteTaskQueue(
        path, serializer=json.dumps, deserializer=json.loads, name="crash"
    )
    try:
        assert q2.counts()["pending"] == 1  # orphan reclaimed at open
        assert q2.counts()["claimed"] == 0
        recovered = q2.get(timeout=1.0)
        assert recovered == {"payload": "precious"}  # payload intact
        q2.task_done()
        assert q2.empty()
    finally:
        q2.close()


# ---------------------------------------------------------------------------
# durable-task-queue-S12
# ---------------------------------------------------------------------------
def test_s12_all_four_task_types_round_trip_through_the_durable_store(tmp_path):
    from core.models import Service
    from stage.factory import TaskFactory

    originals = [
        TaskFactory.create_search_task(
            "deepseek", "/sk-[a-z]{8}/", regex="sk-[a-z]{8}", page=3,
            use_api=True, refine_depth=2,
        ),
        TaskFactory.create_acquisition_task(
            "deepseek", "https://github.com/o/r/blob/main/x.py",
            {"key_pattern": "sk-[a-z]{8}"},
        ),
        TaskFactory.create_check_task(
            "deepseek",
            Service(address="https://api.example.com", endpoint="/v1",
                    key="sk-testkey1", model="m1"),
        ),
        TaskFactory.create_inspect_task(
            "deepseek",
            Service(address="https://api.example.org", endpoint="/v2",
                    key="sk-testkey2", model="m2"),
        ),
    ]

    path = str(tmp_path / "tasks_queue.sqlite")
    ser = lambda t: json.dumps(t.to_dict(), ensure_ascii=False)  # noqa: E731
    q1 = SqliteTaskQueue(path, serializer=ser,
                         deserializer=TaskFactory.from_dict, name="tasks")
    for t in originals:
        q1.put(t, dedup_id=t.task_id, attempts=t.attempts)
    q1.close()

    q2 = SqliteTaskQueue(path, serializer=ser,
                         deserializer=TaskFactory.from_dict, name="tasks")
    try:
        assert q2.qsize() == 4
        for original in originals:  # FIFO order preserved across reopen
            got = q2.get(timeout=1.0)
            assert got.to_dict() == original.to_dict()
            q2.task_done()
        assert q2.empty()
    finally:
        q2.close()


# ---------------------------------------------------------------------------
# durable-task-queue-S13
# ---------------------------------------------------------------------------
def test_s13_aged_out_rows_are_purged_loudly_at_startup(tmp_path, caplog):
    import logging

    path = str(tmp_path / "aged_queue.sqlite")
    q1 = SqliteTaskQueue(path, serializer=json.dumps,
                         deserializer=json.loads, name="aged")
    stale = time.time() - 25 * 3600
    q1.put({"age": "stale"}, created_at=stale)
    q1.put({"age": "fresh"})
    q1.close()

    with caplog.at_level(logging.INFO):
        q2 = SqliteTaskQueue(path, serializer=json.dumps,
                             deserializer=json.loads, name="aged",
                             max_age_hours=24)
    try:
        assert q2.qsize() == 1
        remaining = q2.get(timeout=1.0)
        assert remaining == {"age": "fresh"}
        q2.task_done()
        assert any("aged" in rec.getMessage().lower() for rec in caplog.records), (
            "startup purge must be loud about aged-out rows"
        )
    finally:
        q2.close()


# ---------------------------------------------------------------------------
# durable-task-queue-S14
# ---------------------------------------------------------------------------
def test_s14_snapshot_reads_leave_the_queue_intact(tmp_path):
    q = _q(tmp_path)
    try:
        for i in range(3):
            q.put({"n": i})

        snap = q.snapshot_pending()
        assert snap == [{"n": 0}, {"n": 1}, {"n": 2}]  # FIFO, complete
        assert q.qsize() == 3  # non-destructive

        got = []
        for _ in range(3):
            got.append(q.get(timeout=1.0))
            q.task_done()
        assert got == [{"n": 0}, {"n": 1}, {"n": 2}]  # still dequeued exactly once
        assert q.empty()
    finally:
        q.close()


# ---------------------------------------------------------------------------
# durable-task-queue-S15
# ---------------------------------------------------------------------------
def test_s15_concurrent_churn_loses_nothing(tmp_path):
    q = _q(tmp_path)
    n_producers, per_producer = 2, 250
    total = n_producers * per_producer
    consumed = []
    consumed_lock = threading.Lock()
    acked = [0]
    snaps = [0]
    done_event = threading.Event()
    errors = []

    def produce(pid):
        try:
            for i in range(per_producer):
                q.put({"id": f"{pid}:{i}"}, dedup_id=f"{pid}:{i}")
        except Exception as exc:  # pragma: no cover - failure evidence
            errors.append(exc)

    def consume():
        try:
            while not done_event.is_set():
                try:
                    item = q.get(timeout=0.2)
                except queue_mod.Empty:
                    with consumed_lock:
                        finished = acked[0] >= total
                    if finished:
                        break
                    continue
                with consumed_lock:
                    consumed.append(item["id"])
                    acked[0] += 1
                    take_snap = acked[0] % 25 == 0
                    if acked[0] >= total:
                        done_event.set()
                if take_snap:  # deterministic interleaving of snapshots with churn
                    q.snapshot_pending()
                    with consumed_lock:
                        snaps[0] += 1
                q.task_done()
        except Exception as exc:  # pragma: no cover - failure evidence
            errors.append(exc)

    try:
        producers = [threading.Thread(target=produce, args=(p,)) for p in range(n_producers)]
        consumers = [threading.Thread(target=consume) for _ in range(2)]
        for t in consumers + producers:
            t.start()

        # best-effort snapshots from the main thread while the churn runs
        deadline = time.time() + 30.0
        while time.time() < deadline and not done_event.is_set():
            q.snapshot_pending()
            with consumed_lock:
                snaps[0] += 1
            done_event.wait(timeout=0.01)

        done_event.wait(timeout=30.0)
        for t in producers + consumers:
            t.join(timeout=10.0)

        assert not errors, f"threads raised: {errors[:3]}"
        assert snaps[0] >= 20, f"snapshots did not interleave with churn: {snaps[0]}"
        produced = {f"{p}:{i}" for p in range(n_producers) for i in range(per_producer)}
        assert sorted(consumed) == sorted(produced)  # zero loss, zero duplicates
        assert len(consumed) == total
        assert q.qsize() == 0
        assert q.counts() == {"pending": 0, "claimed": 0}
    finally:
        q.close()
