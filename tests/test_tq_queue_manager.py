"""QueueManager facade: legacy importer, startup maintenance, shutdown durability.

Traceability: durable-task-queue-S16 .. S19.

RED state notes (tests.md group header): ``storage.task_queue`` import fails
pre-fix (whole-file RED); ``QueueManager(backend=...)`` kwarg and
``attach_stages`` do not exist pre-fix (TypeError/AttributeError RED);
``Pipeline._on_stop`` performs no final ``save_all_queues`` pre-fix, so S19's
call-order assertion is a genuine behavioral RED beyond the import failure.

Legacy snapshots for S16 are produced by the PRODUCTION memory-backend
``QueueManager.save_queue_state`` (dogfoods the captured wire format documented
in design.md Context instead of synthesizing one); S17/S18 handcraft the
legacy-fallback envelope (numeric ``saved_at``, bare ``tasks``) and the
corrupt/aged files per that same captured format.
"""

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from config.schemas import Config, StageConfig, TaskConfig
from core.models import Patterns
from manager.pipeline import Pipeline
from manager.queue import QueueManager
from stage.base import StageResources
from stage.definition import AcquisitionStage, SearchStage
from stage.factory import TaskFactory
from storage.task_queue import SqliteTaskQueue  # RED driver: absent pre-fix

PROVIDER = "deepseek"
KEY_PATTERN = r"sk-[A-Za-z0-9]{16}"


class FakeAuth:
    def get_session(self):
        return "session-token"

    def get_token(self):
        return "api-token"

    def get_user_agent(self):
        return "test-agent"


def _resources():
    cfg = Config()
    cfg.pipeline.failure_handling = "legacy"
    return StageResources(
        limiter=None,
        providers={},
        config=cfg,
        task_configs={
            PROVIDER: TaskConfig(
                name=PROVIDER,
                enabled=True,
                provider_type="openai_like",
                use_api=False,
                stages=StageConfig(search=True, gather=True, check=True, inspect=True),
                patterns=Patterns(key_pattern=KEY_PATTERN),
            )
        },
        auth=FakeAuth(),
    )


def _sqlite_stage(cls, queue_dir):
    return cls(
        _resources(),
        lambda out: None,
        thread_count=1,
        queue_backend="sqlite",
        queue_dir=str(queue_dir),
    )


def _task(query="/a/", page=1):
    return TaskFactory.create_search_task(PROVIDER, query, page=page)


# ---------------------------------------------------------------------------
# durable-task-queue-S16
# ---------------------------------------------------------------------------
def test_s16_current_format_snapshot_imports_once_and_is_neutralized(tmp_path, caplog):
    ws = tmp_path
    qdir = ws / "queue_state"

    # 1) produce a REAL legacy snapshot with the production memory-backend writer
    mem_qm = QueueManager(workspace=str(ws), save_interval=60)
    t1, t2 = _task("/a/"), _task("/b/", page=2)
    mem_qm.save_queue_state("search", [t1, t2])
    json_path = qdir / "search_queue.json"
    assert json_path.exists()

    # 2) fresh run on the sqlite backend finds and imports it
    stage = _sqlite_stage(SearchStage, qdir)
    try:
        sqm = QueueManager(workspace=str(ws), save_interval=60, backend="sqlite")
        sqm.attach_stages({"search": stage})

        with caplog.at_level(logging.INFO):
            restored = sqm.load_all_queues()

        assert not restored.get("search"), "durable stages recover in-place, not via re-put lists"
        assert stage.queue.qsize() == 2

        # one-shot: file neutralized by rename
        assert not json_path.exists()
        imported = list(qdir.glob("search_queue.json.imported-*"))
        assert len(imported) == 1

        # idempotent: second startup changes nothing
        sqm.load_all_queues()
        assert stage.queue.qsize() == 2

        # payloads intact, FIFO order = snapshot order
        got1 = stage.queue.get(timeout=1.0)
        stage.queue.task_done()
        got2 = stage.queue.get(timeout=1.0)
        stage.queue.task_done()
        assert got1.to_dict() == t1.to_dict()
        assert got2.to_dict() == t2.to_dict()
    finally:
        stage.queue.close()


# ---------------------------------------------------------------------------
# durable-task-queue-S17
# ---------------------------------------------------------------------------
def test_s17_legacy_format_and_corrupt_files_degrade_safely(tmp_path, caplog):
    ws = tmp_path
    qdir = ws / "queue_state"
    qdir.mkdir(parents=True, exist_ok=True)

    # legacy fallback envelope: numeric saved_at + bare tasks list
    legacy_task = _task("/legacy/")
    legacy_doc = {"saved_at": time.time() - 100, "tasks": [legacy_task.to_dict()]}
    (qdir / "search_queue.json").write_text(json.dumps(legacy_doc), encoding="utf-8")
    # corrupt file for the gather stage
    (qdir / "gather_queue.json").write_text("{oops not json", encoding="utf-8")

    search_stage = _sqlite_stage(SearchStage, qdir)
    gather_stage = _sqlite_stage(AcquisitionStage, qdir)
    try:
        sqm = QueueManager(workspace=str(ws), save_interval=60, backend="sqlite")
        sqm.attach_stages({"search": search_stage, "gather": gather_stage})

        with caplog.at_level(logging.WARNING):
            sqm.load_all_queues()  # must not raise

        assert search_stage.queue.qsize() == 1     # legacy shape imported
        got = search_stage.queue.get(timeout=1.0)
        search_stage.queue.task_done()
        assert got.to_dict() == legacy_task.to_dict()

        assert gather_stage.queue.qsize() == 0     # corrupt file skipped
        assert (qdir / "gather_queue.json").exists(), "corrupt file left for operator inspection"
        assert any(
            rec.levelno >= logging.WARNING and "gather" in rec.getMessage().lower()
            for rec in caplog.records
        ), "corrupt snapshot must be skipped loudly"
    finally:
        search_stage.queue.close()
        gather_stage.queue.close()


# ---------------------------------------------------------------------------
# durable-task-queue-S18
# ---------------------------------------------------------------------------
def test_s18_aged_out_snapshot_is_parked_not_imported(tmp_path, caplog):
    ws = tmp_path
    qdir = ws / "queue_state"
    qdir.mkdir(parents=True, exist_ok=True)

    aged_doc = {
        "stage": "search",
        "provider": "multi",
        "task_count": 1,
        "saved_at": (datetime.now() - timedelta(hours=30)).isoformat(),
        "status": "active",
        "tasks": [_task("/old/").to_dict()],
    }
    json_path = qdir / "search_queue.json"
    json_path.write_text(json.dumps(aged_doc), encoding="utf-8")

    stage = _sqlite_stage(SearchStage, qdir)
    try:
        sqm = QueueManager(workspace=str(ws), save_interval=60, backend="sqlite", max_age_hours=24)
        sqm.attach_stages({"search": stage})

        with caplog.at_level(logging.WARNING):
            sqm.load_all_queues()

        assert stage.queue.qsize() == 0            # nothing imported
        assert not json_path.exists()
        expired = list(qdir.glob("search_queue.json.expired-*"))
        assert len(expired) == 1                   # parked, not deleted
        assert any(
            rec.levelno >= logging.WARNING
            and any(w in rec.getMessage().lower() for w in ("age", "expired", "old", "hour"))
            for rec in caplog.records
        ), "age decision must be loud"
    finally:
        stage.queue.close()


# ---------------------------------------------------------------------------
# durable-task-queue-S19
# ---------------------------------------------------------------------------
def _stub_pipeline_ns(stages, qm, events):
    """SimpleNamespace satisfying Pipeline._on_stop's attribute surface.

    The queue manager keeps its REAL save_all_queues (wrapped in an ordering
    spy); stop() is a pure recorder (the periodic thread was never started).
    """
    noop = lambda *a, **k: None  # noqa: E731
    real_save = qm.save_all_queues  # capture BEFORE shadowing

    def save_spy(stgs):
        events.append("save_all_queues")
        real_save(stgs)

    qm.save_all_queues = save_spy
    qm.stop = lambda *a, **k: events.append("queue_manager_stop")
    return SimpleNamespace(
        stages=stages,
        get_order=lambda: list(stages.keys()),
        recheck=SimpleNamespace(stop=noop, close=noop),
        link_registry=SimpleNamespace(stop=noop),
        gather_skip=SimpleNamespace(close=noop),
        enrichment=SimpleNamespace(close=noop),
        early_stop=SimpleNamespace(close=noop),
        key_ledger=SimpleNamespace(close=noop),
        queue_manager=qm,
        result_manager=SimpleNamespace(stop_all=lambda: events.append("result_stop")),
    )


def test_s19_graceful_stop_persists_final_state(tmp_path):
    # --- phase A: memory backend -> final JSON snapshot written at stop -----
    ws_a = tmp_path / "ws_memory"
    stage_mem = SearchStage(_resources(), lambda out: None, thread_count=1)
    t1, t2 = _task("/m1/"), _task("/m2/")
    assert stage_mem.put_task(t1) and stage_mem.put_task(t2)

    qm_mem = QueueManager(workspace=str(ws_a), save_interval=60)
    events_a = []
    ns_a = _stub_pipeline_ns({"search": stage_mem}, qm_mem, events_a)

    Pipeline._on_stop(ns_a)

    assert "save_all_queues" in events_a, "pre-fix RED: _on_stop never saves queues"
    assert events_a.index("save_all_queues") < events_a.index("queue_manager_stop"), (
        "final save must run before the queue manager stops"
    )
    snap_path = ws_a / "queue_state" / "search_queue.json"
    assert snap_path.exists(), "clean shutdown must leave a fresh snapshot"
    doc = json.loads(snap_path.read_text(encoding="utf-8"))
    assert doc["task_count"] == 2
    assert {t["data"]["query"] for t in doc["tasks"]} == {"/m1/", "/m2/"}

    # --- phase B: sqlite backend -> rows intact, checkpoint not drain -------
    ws_b = tmp_path / "ws_sqlite"
    qdir_b = ws_b / "queue_state"
    stage_sq = _sqlite_stage(SearchStage, qdir_b)
    try:
        assert stage_sq.put_task(_task("/s1/")) and stage_sq.put_task(_task("/s2/"))
        qm_sq = QueueManager(workspace=str(ws_b), save_interval=60, backend="sqlite")
        qm_sq.attach_stages({"search": stage_sq})
        events_b = []
        ns_b = _stub_pipeline_ns({"search": stage_sq}, qm_sq, events_b)

        Pipeline._on_stop(ns_b)

        assert events_b.index("save_all_queues") < events_b.index("queue_manager_stop")
        counts = stage_sq.queue.counts()
        assert counts["pending"] == 2, "stop-time maintenance must not consume or drop rows"
        assert counts["claimed"] == 0
        # and the work is genuinely recoverable by the next run
        got = stage_sq.queue.get(timeout=1.0)
        stage_sq.queue.task_done()
        assert got.to_dict()["data"]["query"] == "/s1/"
    finally:
        stage_sq.queue.close()
