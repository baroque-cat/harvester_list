"""Gather-outcome fidelity (fix-silent-losses).

Traceability: failure-handling-S7, S8.

Harness: AcquisitionStage runs against a real SQLite Registry in the tmp
workspace (conftest builders); ``search.client.http_get`` is monkeypatched to
simulate fetch outcomes. No network.

RED state notes:
- S7 drives the fix through the real call chain: pre-fix ``collect()``'s
  @handle_exceptions swallows the fetch error into ``[]`` and the stage records
  success=True + coverage row -> assertions fail (RED). Post-fix the typed
  failure escapes collect, the stage records success=False (no coverage) and,
  under strict mode, propagates for bounded requeue.
- S8 is a regression guard expected GREEN already and MUST stay green:
  "fetched successfully, zero keys" remains gathered_ok with its coverage row.
"""

import os
import sqlite3

import pytest

from config.schemas import Config, StageConfig, TaskConfig
from core.exceptions import TransientFetchError  # RED driver: absent pre-fix
from core.models import Patterns
from search import client as search_client
from stage.base import StageResources
from stage.definition import AcquisitionStage
from stage.factory import TaskFactory
from storage.registry import Registry, patterns_hash, url_hash

PROVIDER = "deepseek"
KEY_PATTERN = r"sk-[A-Za-z0-9]{16}"
URL = "https://github.com/acme/widgets/blob/main/leak.py"


class FakeAuth:
    def get_session(self):
        return "session-token"

    def get_token(self):
        return "api-token"

    def get_user_agent(self):
        return "test-agent"


class _FastRegistryConfig:
    enabled = True
    batch_size = 1
    flush_interval = 0.05
    queue_size = 100000
    path = ""


def _registry(workspace) -> Registry:
    registry = Registry(str(workspace), config=_FastRegistryConfig(), enabled=True)
    assert registry.start() is True
    return registry


def _rows(workspace, sql, params=()):
    conn = sqlite3.connect(os.path.join(str(workspace), "registry.sqlite"))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, tuple(params)).fetchall()
    finally:
        conn.close()


def _stage(workspace, registry, mode="strict"):
    cfg = Config()
    cfg.pipeline.failure_handling = mode
    resources = StageResources(
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
        registry=registry,
    )
    return AcquisitionStage(resources, lambda out: None, thread_count=1, max_retries=2)


def _task():
    return TaskFactory.create_acquisition_task(PROVIDER, URL, Patterns(key_pattern=KEY_PATTERN))


# ---------------------------------------------------------------------------
# failure-handling-S7
# ---------------------------------------------------------------------------
def test_s7_fetch_failure_never_produces_gathered_ok(workspace, monkeypatch):
    def dead_network(url="", retries=3, interval=1.0, **kwargs):
        raise ConnectionError("HTTP 503 error: upstream gone")

    monkeypatch.setattr(search_client, "http_get", dead_network)
    registry = _registry(workspace)
    try:
        stage = _stage(workspace, registry, mode="strict")

        # Contract: the typed transient failure escapes for bounded requeue...
        with pytest.raises(TransientFetchError):
            stage.process_task(_task())

        registry.flush(10.0)
        digest = patterns_hash(key_pattern=KEY_PATTERN)

        # ...and the registry tells the truth: failed visit, no coverage row.
        status = _rows(workspace, "SELECT visit_status FROM links WHERE url_hash=?", (url_hash(URL),))
        assert [r["visit_status"] for r in status] == ["failed"]
        coverage = _rows(
            workspace,
            "SELECT COUNT(*) FROM link_coverage WHERE url_hash=? AND provider=? AND patterns_hash=?",
            (url_hash(URL), PROVIDER, digest),
        )
        assert coverage[0][0] == 0
    finally:
        registry.stop()


# ---------------------------------------------------------------------------
# failure-handling-S8 (regression guard, expected GREEN before and after)
# ---------------------------------------------------------------------------
def test_s8_zero_key_success_keeps_coverage_semantics(workspace, monkeypatch):
    monkeypatch.setattr(
        search_client, "http_get",
        lambda url="", retries=3, interval=1.0, **kwargs: "<html>no secrets here</html>",
    )
    registry = _registry(workspace)
    try:
        stage = _stage(workspace, registry, mode="strict")

        stage.process_task(_task())  # succeeds with zero extracted services
        registry.flush(10.0)
        digest = patterns_hash(key_pattern=KEY_PATTERN)

        status = _rows(workspace, "SELECT visit_status FROM links WHERE url_hash=?", (url_hash(URL),))
        assert [r["visit_status"] for r in status] == ["gathered_ok"]
        coverage = _rows(
            workspace,
            "SELECT COUNT(*) FROM link_coverage WHERE url_hash=? AND provider=? AND patterns_hash=?",
            (url_hash(URL), PROVIDER, digest),
        )
        assert coverage[0][0] == 1  # "researched, nothing found" stays covered
    finally:
        registry.stop()
