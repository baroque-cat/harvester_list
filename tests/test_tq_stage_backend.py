"""Stage & config integration for the durable task queue backend.

Traceability: durable-task-queue-S1 .. S6, S20, S21, S22.

Harness mirrors tests/test_fh_stage_modes.py (FakeAuth / _task_config /
_resources / real SearchStage / monkeypatched search_client) per house style.
RED state notes (tests.md group header):
- ``storage.task_queue`` import fails pre-fix (whole-file RED);
- ``TaskQueueConfig`` import from config.schemas fails pre-fix;
- stage ctor kwargs ``queue_backend``/``queue_dir`` do not exist pre-fix
  (TypeError RED);
- ``StageMetrics.tasks_dropped_backend_errors`` does not exist pre-fix.

S21/S22 re-pin the failure-handling contracts (fh-S5 and fh-S4/S6) across
BOTH backends instead of editing the originals - the fh suite must stay
green unmodified (design D8 parity promise).
"""

import json
import logging
import queue as queue_mod
import sqlite3
import time
from types import SimpleNamespace

import pytest

from config.schemas import Config, StageConfig, TaskConfig, TaskQueueConfig  # RED: TaskQueueConfig absent
from config.validator import ConfigValidator
from core.exceptions import TransientFetchError
from core.models import Patterns, SearchTask
from search import client as search_client
from stage.base import StageResources
from stage.definition import SearchStage
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


def _task_config():
    return TaskConfig(
        name=PROVIDER,
        enabled=True,
        provider_type="openai_like",
        use_api=False,
        stages=StageConfig(search=True, gather=True, check=True, inspect=True),
        patterns=Patterns(key_pattern=KEY_PATTERN),
    )


def _cfg(mode="legacy"):
    cfg = Config()
    cfg.pipeline.failure_handling = mode
    return cfg


def _resources(mode="legacy"):
    return StageResources(
        limiter=None,
        providers={},
        config=_cfg(mode),
        task_configs={PROVIDER: _task_config()},
        auth=FakeAuth(),
    )


def _stage(tmp_path, backend="memory", mode="legacy", max_retries=0, queue_size=1000):
    """Real SearchStage on the requested backend (kwargs absent pre-fix -> RED)."""
    return SearchStage(
        _resources(mode),
        lambda out: None,
        thread_count=1,
        max_retries=max_retries,
        queue_size=queue_size,
        queue_backend=backend,
        queue_dir=str(tmp_path / "queue_state"),
    )


def _search_task(query='"sk-"', page=1):
    return SearchTask(provider=PROVIDER, query=query, regex=KEY_PATTERN, page=page, use_api=False)


def _boom(monkeypatch, calls=None):
    def failing(**kwargs):
        if calls is not None:
            calls.append(1)
        raise TransientFetchError("network down")

    monkeypatch.setattr(search_client, "search_with_count", failing)


def _wait_drain(stage, deadline=10.0):
    end = time.time() + deadline
    while time.time() < end:
        if stage.queue.empty() and not stage._has_active_workers():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# durable-task-queue-S1
# ---------------------------------------------------------------------------
def test_s1_absent_config_selects_memory_backend_with_unchanged_behavior(tmp_path):
    cfg = Config()
    assert cfg.queue.backend == "memory"          # RED pre-fix: no queue section
    assert cfg.queue.visibility_timeout_s == 300
    assert cfg.queue.max_age_hours == 24

    # stage built exactly as before (no new kwargs) -> plain in-memory FIFO
    stage = SearchStage(_resources(), lambda out: None, thread_count=1)
    assert isinstance(stage.queue, queue_mod.Queue)
    assert getattr(stage, "queue_degraded", False) is False

    t1, t2 = _search_task(query="/a/"), _search_task(query="/b/")
    assert stage.put_task(t1) is True
    assert stage.put_task(t2) is True
    assert stage.queue.qsize() == 2
    assert stage.queue.get_nowait() is t1          # FIFO, unchanged semantics
    assert stage.queue.get_nowait() is t2


# ---------------------------------------------------------------------------
# durable-task-queue-S2
# ---------------------------------------------------------------------------
def test_s2_sqlite_backend_creates_per_stage_wal_store(tmp_path):
    stage = _stage(tmp_path, backend="sqlite")
    db_path = tmp_path / "queue_state" / "search_queue.sqlite"
    assert db_path.exists(), "per-stage durable store must be created at construction"
    assert stage.queue.journal_mode == "wal"
    assert isinstance(stage.queue, SqliteTaskQueue)
    stage.queue.close()


# ---------------------------------------------------------------------------
# durable-task-queue-S3
# ---------------------------------------------------------------------------
def test_s3_invalid_backend_name_fails_validation_loudly():
    with pytest.raises(ValueError):
        TaskQueueConfig(backend="redis")

    cfg = Config()
    cfg.queue.backend = "redis"  # mutation bypasses __post_init__: validator is layer 2
    with pytest.raises(ValueError) as excinfo:
        ConfigValidator().validate(cfg)
    assert "backend" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# durable-task-queue-S4
# ---------------------------------------------------------------------------
def test_s4_non_positive_numeric_queue_fields_fail_validation_loudly():
    with pytest.raises(ValueError):
        TaskQueueConfig(visibility_timeout_s=0)
    with pytest.raises(ValueError):
        TaskQueueConfig(max_age_hours=-1)

    cfg = Config()
    cfg.queue.visibility_timeout_s = -5
    with pytest.raises(ValueError) as excinfo:
        ConfigValidator().validate(cfg)
    assert "visibility_timeout_s" in str(excinfo.value)


# ---------------------------------------------------------------------------
# durable-task-queue-S5
# ---------------------------------------------------------------------------
def test_s5_burst_far_beyond_configured_queue_size_is_fully_retained(tmp_path, caplog):
    burst = 5000
    stage = _stage(tmp_path, backend="sqlite", queue_size=10)  # configured cap ignored
    try:
        with caplog.at_level(logging.WARNING):
            results = [
                stage.put_task(TaskFactory.create_search_task(PROVIDER, f"/q{i}/", page=1))
                for i in range(burst)
            ]
        assert all(results), f"{results.count(False)} enqueues refused"
        assert stage.queue.qsize() == burst
        assert not any("full" in rec.getMessage().lower() for rec in caplog.records)
    finally:
        stage.queue.close()


# ---------------------------------------------------------------------------
# durable-task-queue-S6
# ---------------------------------------------------------------------------
def test_s6_runtime_write_error_is_loud_and_counted(tmp_path, monkeypatch, caplog):
    stage = _stage(tmp_path, backend="sqlite")
    try:
        def broken_put(self, obj, timeout=None, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(SqliteTaskQueue, "put", broken_put)

        with caplog.at_level(logging.WARNING):
            accepted = stage.put_task(_search_task())

        assert accepted is False
        assert stage.tasks_dropped_backend_errors == 1   # RED pre-fix: attribute missing
        assert stage.tasks_dropped_max_retries == 0      # other counters untouched
        assert stage.tasks_requeued == 0
        assert any(rec.levelno >= logging.WARNING for rec in caplog.records)
        stats = stage.get_stats()
        assert stats.tasks_dropped_backend_errors == 1   # exposed in StageMetrics
    finally:
        stage.queue.close()


# ---------------------------------------------------------------------------
# durable-task-queue-S20
# ---------------------------------------------------------------------------
def test_s20_unopenable_store_degrades_to_functional_memory_queue(tmp_path, monkeypatch, caplog):
    def exploding_init(self, *args, **kwargs):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(SqliteTaskQueue, "__init__", exploding_init)

    with caplog.at_level(logging.ERROR):
        stage = _stage(tmp_path, backend="sqlite")

    assert stage.queue_degraded is True
    assert isinstance(stage.queue, queue_mod.Queue)      # functional memory fallback
    assert any(
        rec.levelno >= logging.ERROR and "search" in rec.getMessage()
        for rec in caplog.records
    ), "fallback must be loud and name the stage"

    task = _search_task()
    assert stage.put_task(task) is True                  # stage remains usable
    assert stage.queue.qsize() == 1
    assert stage.queue.get_nowait() is task


# ---------------------------------------------------------------------------
# durable-task-queue-S21 (parity: pinned fh-S5 dedup gate on BOTH backends)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_s21_dedup_gate_contract_is_identical_on_both_backends(tmp_path, backend):
    stage = _stage(tmp_path, backend=backend, max_retries=2)
    try:
        first = _search_task()
        assert stage.put_task(first) is True       # first enqueue marks id processed

        duplicate = _search_task()
        assert stage.put_task(duplicate) is False  # attempts==0 duplicate rejected

        first.attempts = 1
        assert stage.put_task(first) is True       # bounded retry admitted despite mark

        first.attempts = 3
        assert stage.put_task(first) is False      # beyond bound rejected loudly
        assert stage.tasks_dropped_max_retries == 1
    finally:
        if backend == "sqlite":
            stage.queue.close()


# ---------------------------------------------------------------------------
# durable-task-queue-S22 (parity: pinned fh-S4/S6 worker retry flow on BOTH)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_s22_worker_retry_flow_is_identical_on_both_backends(tmp_path, monkeypatch, caplog, backend):
    calls = []
    _boom(monkeypatch, calls)
    stage = _stage(tmp_path, backend=backend, mode="strict", max_retries=2)
    monkeypatch.setattr(stage.retry_policy, "get_delay", lambda attempts: 0.0)
    task = _search_task()

    stage.start()
    try:
        assert stage.put_task(task) is True
        drained = _wait_drain(stage)
    finally:
        stage.stop(timeout=3.0)

    assert drained, "worker loop did not settle within the deadline"
    assert len(calls) == 3                        # initial attempt + 2 bounded retries
    assert task.attempts == 3                     # grew past the bound -> final requeue rejected
    assert stage.total_errors == 3                # failures accounted as errors, never successes
    assert stage.tasks_requeued == 2              # both successful requeues counted
    assert stage.tasks_dropped_max_retries == 1
    assert any(
        "discarded" in rec.getMessage().lower() or "max retries" in rec.getMessage().lower()
        for rec in caplog.records
    )
    if backend == "sqlite":
        # durability corollary: every claim acked, refused requeue never inserted
        assert stage.queue.qsize() == 0
        assert stage.queue.counts() == {"pending": 0, "claimed": 0}
        stage.queue.close()
