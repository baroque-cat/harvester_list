"""Delivery of date metadata through StageOutput into the registry.

Covers ``date-extraction-S6..S9`` from the add-date-extraction test plan.
Uses the registry tmp-workspace harness from ``conftest.py`` and synthetic
stage outputs; no network is touched.
"""

import os

import pytest

from config.schemas import Config, StageConfig, TaskConfig
from core.metrics import DateFillMetrics, PipelineStatus
from core.models import (
    AcquisitionTask,
    Condition,
    LinkMetadata,
    Patterns,
    ResultStorage,
    SearchTask,
)
from search import client as search_client
from stage.base import StageResources
from stage.definition import AcquisitionStage, SearchStage
from storage.registry import Registry, canonical_url

SEARCH_URLS = [
    "https://github.com/acme/widgets/blob/main/config.py",
    "https://github.com/acme/widgets/blob/main/.env",
]
GATHER_TEMPLATE = "https://github.com/acme/widgets/blob/main/f{i}.py"
KEY_PATTERN = r"sk-[A-Za-z0-9]{16}"


class FakeAuth:
    def get_session(self):
        return "session-token"

    def get_token(self):
        return "api-token"

    def get_user_agent(self):
        return "test-agent"


class FakeProvider:
    def __init__(self, name="openai"):
        self._name = name
        self._conditions = [Condition(query="sk-", patterns=Patterns(key_pattern=KEY_PATTERN))]
        self._result = ResultStorage(folder=name, filenames={})

    @property
    def name(self):
        return self._name

    @property
    def conditions(self):
        return self._conditions

    @property
    def result(self):
        return self._result

    def get_patterns(self):
        return self._conditions[0].patterns


def _task_config(use_api: bool) -> TaskConfig:
    return TaskConfig(
        name="openai",
        enabled=True,
        provider_type="openai",
        use_api=use_api,
        stages=StageConfig(search=True, gather=True, check=False, inspect=False),
        patterns=Patterns(key_pattern=KEY_PATTERN),
    )


class _FastRegistryConfig:
    """Registry config that flushes synchronously-ish (batch_size=1)."""

    enabled = True
    batch_size = 1
    flush_interval = 0.05
    queue_size = 1000
    path = ""


def _registry(workspace: str) -> Registry:
    return Registry(workspace, config=_FastRegistryConfig(), enabled=True)


def _resources(use_api: bool, registry=None, date_metrics=None) -> StageResources:
    return StageResources(
        limiter=None,
        providers={"openai": FakeProvider()},
        config=Config(),
        task_configs={"openai": _task_config(use_api)},
        auth=FakeAuth(),
        registry=registry,
        date_metrics=date_metrics,
    )


class _NoopResultManager:
    def add_result(self, *args, **kwargs):
        pass

    def add_links(self, *args, **kwargs):
        pass

    def add_models(self, *args, **kwargs):
        pass


class _PipelineStub:
    """Minimal host exposing the fields ``Pipeline._handle_stage_output`` touches."""

    def __init__(self, registry):
        self.link_registry = registry
        self.result_manager = _NoopResultManager()
        self.task_configs = {}
        self.stages = {}


def _forward(output, registry):
    from manager.pipeline import Pipeline

    Pipeline._handle_stage_output(_PipelineStub(registry), output)


# ---------------------------------------------------------------------------
# date-extraction-S6
# ---------------------------------------------------------------------------
def test_s6_web_search_produces_no_dates(workspace, db_scalar, monkeypatch):
    """S6: web transport yields no search-stage metadata; rows stay NULL."""
    registry = _registry(workspace)
    assert registry.start() is True
    try:
        resources = _resources(use_api=False, registry=registry)
        monkeypatch.setattr(
            search_client,
            "search_with_count",
            lambda **kwargs: (list(SEARCH_URLS), len(SEARCH_URLS), ""),
        )

        output = SearchStage(resources, lambda out: None).process_task(
            SearchTask(provider="openai", query='"sk-"', regex=KEY_PATTERN, page=1, use_api=False)
        )

        assert output is not None
        assert output.link_metadata == []
        _forward(output, registry)
        registry.flush(2.0)

        stored = db_scalar(workspace, "SELECT repo_pushed_at FROM links WHERE url = ?", (canonical_url(SEARCH_URLS[0]),))
        assert stored is None
    finally:
        registry.stop()


# ---------------------------------------------------------------------------
# date-extraction-S7
# ---------------------------------------------------------------------------
def test_s7_known_date_survives_later_null_observation(workspace, db_scalar):
    """S7: a NULL observation never overwrites a known repo_pushed_at."""
    url = "https://github.com/acme/widgets/blob/main/config.py"
    registry = _registry(workspace)
    assert registry.start() is True
    try:
        registry.record_link(url, transport="api", provider="openai")
        registry.flush(2.0)
        registry.record_metadata({url: LinkMetadata(repo_pushed_at=1_000.0, transport="api")})
        registry.flush(2.0)
        registry.record_metadata({url: LinkMetadata(transport="web")})
        registry.flush(2.0)

        assert db_scalar(workspace, "SELECT repo_pushed_at FROM links LIMIT 1") == pytest.approx(1_000.0)
    finally:
        registry.stop()


# ---------------------------------------------------------------------------
# date-extraction-S8
# ---------------------------------------------------------------------------
def test_s8_fresher_date_replaces_older(workspace, db_scalar):
    """S8: a newer non-NULL observation replaces the stored value."""
    url = "https://github.com/acme/widgets/blob/main/config.py"
    registry = _registry(workspace)
    assert registry.start() is True
    try:
        registry.record_link(url, transport="api", provider="openai")
        registry.flush(2.0)
        registry.record_metadata({url: LinkMetadata(repo_pushed_at=1_000.0)})
        registry.flush(2.0)
        registry.record_metadata({url: LinkMetadata(repo_pushed_at=2_000.0)})
        registry.flush(2.0)

        assert db_scalar(workspace, "SELECT repo_pushed_at FROM links LIMIT 1") == pytest.approx(2_000.0)
    finally:
        registry.stop()


# ---------------------------------------------------------------------------
# date-extraction-S9
# ---------------------------------------------------------------------------
def test_s9_rates_computed_over_a_run():
    """S9: 100 API items (95 dated) + 50 gathers (40 dated) -> 0.95 / 0.80."""
    metrics = DateFillMetrics()
    for _ in range(95):
        metrics.record_api(True)
    for _ in range(5):
        metrics.record_api(False)
    for _ in range(40):
        metrics.record_web(True)
    for _ in range(10):
        metrics.record_web(False)

    assert metrics.date_fill_rate_api == pytest.approx(0.95)
    assert metrics.date_fill_rate_web == pytest.approx(0.80)

    # Surfaced in run statistics.
    status = PipelineStatus(date_metrics=metrics.to_stats())
    assert status.date_metrics["date_fill_rate_api"] == pytest.approx(0.95)
    assert status.date_metrics["date_fill_rate_web"] == pytest.approx(0.80)


def test_s9_stage_workers_feed_fill_rate_counters(monkeypatch):
    """S9 (wiring): search/gather stages update the shared fill-rate counters."""
    metrics = DateFillMetrics()
    resources = _resources(use_api=True, date_metrics=metrics)

    def fake_search_with_count(**kwargs):
        metadata = kwargs.get("metadata")
        urls = []
        for index in range(100):
            url = f"https://github.com/acme/widgets/blob/main/f{index}.py"
            urls.append(url)
            if metadata is not None:
                metadata[url] = LinkMetadata(repo_pushed_at=1_000.0 if index < 95 else None, transport="api")
        return urls, len(urls), ""

    def fake_collect(**kwargs):
        metadata = kwargs.get("metadata")
        index = int(kwargs.get("url", "f0.py").rsplit("f", 1)[1].split(".")[0])
        if metadata is not None:
            metadata["file_commit_date"] = 1_000.0 if index < 40 else None
        return []

    monkeypatch.setattr(search_client, "search_with_count", fake_search_with_count)
    monkeypatch.setattr(search_client, "collect", fake_collect)

    SearchStage(resources, lambda out: None).process_task(
        SearchTask(provider="openai", query='"sk-"', regex=KEY_PATTERN, page=1, use_api=True)
    )
    assert metrics.date_fill_rate_api == pytest.approx(0.95)

    acquisition = AcquisitionStage(resources, lambda out: None)
    for index in range(50):
        acquisition.process_task(
            AcquisitionTask(
                provider="openai",
                url=GATHER_TEMPLATE.format(i=index),
                key_pattern=KEY_PATTERN,
                retries=1,
            )
        )
    assert metrics.date_fill_rate_web == pytest.approx(0.80)
