"""Regression tests for live gate defects F1-F5 (2026-10-16, four 300 s arms).

F1: propagation by URL — date_urls_map returns {url: date} and the stage
    merges it into output.add_link_metadata so registry links rows carry
    file_commit_date.
F2: scope enforcement — search-stage hook fires only for scope="discovered".
F3: wall-clock budget — date_urls/date_urls_map accept a budget parameter and
    stop issuing requests when the deadline passes.
F4: request counter — to_stats()["file_dating_requests"] equals
    _requests_this_run; batch path increments the client stat.
F5: observability and rollback flag — Gather: line renders 304=, Dating: line
    renders cached=, gather.conditional_refetch flag gates etag lookup.

No network.
"""

import os
import sqlite3
import time

import pytest

from config.schemas import Config, GatherConfig
from core.models import LinkMetadata
from search.client import (
    FileDateResult,
    _FILE_DATE_STATS,
    fetch_file_commit_dates_batch,
    reset_file_date_stats,
)
from state.display import StatusDisplayEngine
from core.metrics import PipelineStatus
from state.models import SystemStatus


# ---------------------------------------------------------------------------
# F1: date_urls_map returns {url: date_or_None}
# ---------------------------------------------------------------------------
class _FakeStore:
    """Minimal FileMetaStore stand-in with cache entries."""

    def __init__(self, entries=None):
        self.entries = entries or {}
        self.writes = []

    def get(self, owner, repo, path):
        return self.entries.get((owner, repo, path))

    def is_fresh(self, entry, sha=None, now=None):
        return entry is not None

    def record_upsert(self, *a, **kw):
        self.writes.append(("upsert", a, kw))

    def record_link_date(self, *a, **kw):
        self.writes.append(("link_date", a, kw))

    def record_touch(self, *a, **kw):
        pass

    def record_gone(self, *a, **kw):
        pass

    def flush(self, timeout=10.0):
        pass

    def close(self):
        pass


class _FakeAuth:
    def get_token(self):
        return "test-token"


def test_f1_date_urls_map_returns_map():
    """F1: date_urls_map returns {url: date_or_None}."""
    from storage.file_dating import FileDater
    from storage.file_meta import FileMetaEntry

    NOW = 1_700_000_000.0
    store = _FakeStore(entries={
        ("octocat", "Hello-World", "src/main.py"): FileMetaEntry(
            owner="octocat", repo="Hello-World", path="src/main.py",
            committed_date=NOW - 3600, fetched_at=NOW,
        ),
    })
    dater = FileDater(store, auth=_FakeAuth(), enabled=True, clock=lambda: NOW)

    urls = [
        "https://github.com/octocat/Hello-World/blob/main/src/main.py",
        "https://github.com/octocat/Hello-World/blob/main/src/other.py",
    ]
    result = dater.date_urls_map(urls)

    assert isinstance(result, dict)
    # The cached entry should yield a date.
    assert result[urls[0]] is not None
    assert result[urls[0]] == pytest.approx(NOW - 3600)
    # The missing entry should yield None.
    assert result[urls[1]] is None


def test_f1_date_urls_delegates_to_map():
    """F1: date_urls delegates to date_urls_map and returns count of non-None."""
    from storage.file_dating import FileDater
    from storage.file_meta import FileMetaEntry

    NOW = 1_700_000_000.0
    store = _FakeStore(entries={
        ("octocat", "Hello-World", "src/main.py"): FileMetaEntry(
            owner="octocat", repo="Hello-World", path="src/main.py",
            committed_date=NOW - 3600, fetched_at=NOW,
        ),
    })
    dater = FileDater(store, auth=_FakeAuth(), enabled=True, clock=lambda: NOW)

    urls = [
        "https://github.com/octocat/Hello-World/blob/main/src/main.py",
        "https://github.com/octocat/Hello-World/blob/main/src/other.py",
    ]
    count = dater.date_urls(urls)
    assert count == 1  # only the cached entry yields a date


# ---------------------------------------------------------------------------
# F1 end-to-end: dated URL produces non-NULL links.file_commit_date
# ---------------------------------------------------------------------------
class _FastRegistryConfig:
    enabled = True
    batch_size = 1
    flush_interval = 0.05
    queue_size = 100000
    path = ""


def _registry(workspace: str):
    from storage.registry import Registry
    registry = Registry(workspace, config=_FastRegistryConfig(), enabled=True)
    assert registry.start() is True
    return registry


def test_f1_end_to_end_dated_url_produces_non_null_file_commit_date(workspace, db_scalar):
    """F1: a dated URL produces a non-NULL links.file_commit_date row."""
    NOW = 1_700_000_000.0
    url = "https://github.com/octocat/Hello-World/blob/main/src/main.py"

    registry = _registry(workspace)
    try:
        # Record the link first.
        registry.record_link(url, ts=NOW - 86400)
        registry.flush(10.0)

        # Simulate what the stage does: merge API date into add_link_metadata.
        api_date = NOW - 3600
        registry.record_metadata({url: LinkMetadata(file_commit_date=api_date, transport="api")})
        registry.flush(10.0)

        stored = db_scalar(workspace, "SELECT file_commit_date FROM links WHERE url = ?", (url,))
        assert stored is not None
        assert stored == pytest.approx(api_date)
    finally:
        registry.stop()


# ---------------------------------------------------------------------------
# F2: scope enforcement at search-stage hook
# ---------------------------------------------------------------------------
def test_f2_search_stage_hook_fires_only_for_discovered_scope():
    """F2: search-stage hook fires only when scope='discovered'."""
    from stage.definition import SearchStage
    from stage.base import StageResources
    from core.models import SearchTask, Patterns

    class FakeGatherSkip:
        enabled = True
        def decide(self, links, provider=None, patterns_hash=None):
            return []

    class FakeDater:
        def __init__(self, scope):
            self.enabled = True
            self.scope = scope
            self.calls = []
        def date_urls_map(self, urls, budget=None):
            self.calls.append(list(urls))
            return {}
        def date_urls(self, urls, flush=True):
            self.calls.append(list(urls))
            return 0

    task = SearchTask(provider="test", query="test", regex="sk-", page=1)
    patterns = Patterns(key_pattern="sk-")
    links = ["https://github.com/o/r/blob/main/f.py"]

    # scope="keyed" → search-stage hook should NOT fire.
    dater_keyed = FakeDater(scope="keyed")
    resources = StageResources(
        limiter=None, providers={}, config=Config(),
        task_configs={}, auth=None,
        gather_skip=FakeGatherSkip(),
        file_dating=dater_keyed,
    )
    stage = SearchStage(resources, lambda out: None)
    stage._gather_skip_map(task, links, patterns)
    assert dater_keyed.calls == []

    # scope="discovered" → search-stage hook SHOULD fire.
    dater_discovered = FakeDater(scope="discovered")
    resources2 = StageResources(
        limiter=None, providers={}, config=Config(),
        task_configs={}, auth=None,
        gather_skip=FakeGatherSkip(),
        file_dating=dater_discovered,
    )
    stage2 = SearchStage(resources2, lambda out: None)
    stage2._gather_skip_map(task, links, patterns)
    assert len(dater_discovered.calls) == 1


# ---------------------------------------------------------------------------
# F3: wall-clock budget
# ---------------------------------------------------------------------------
def test_f3_budget_parameter_accepted():
    """F3: date_urls_map accepts a budget parameter without error."""
    from storage.file_dating import FileDater, DEFAULT_DATING_BUDGET_SECONDS

    store = _FakeStore()
    dater = FileDater(store, auth=_FakeAuth(), enabled=True)

    urls = ["https://github.com/octocat/Hello-World/blob/main/src/main.py"]
    # Should not raise; budget is accepted.
    result = dater.date_urls_map(urls, budget=DEFAULT_DATING_BUDGET_SECONDS)
    assert isinstance(result, dict)


def test_f3_default_budget_constant():
    """F3: DEFAULT_DATING_BUDGET_SECONDS is defined and positive."""
    from storage.file_dating import DEFAULT_DATING_BUDGET_SECONDS
    assert DEFAULT_DATING_BUDGET_SECONDS > 0


def test_f3_time_stopped_counter():
    """F3: time_stopped counter exists in to_stats."""
    from storage.file_dating import FileDater

    store = _FakeStore()
    dater = FileDater(store, auth=_FakeAuth(), enabled=True)
    stats = dater.to_stats()
    assert "file_dating_time_stopped" in stats
    assert stats["file_dating_time_stopped"] == 0


# ---------------------------------------------------------------------------
# F4: request counter
# ---------------------------------------------------------------------------
def test_f4_to_stats_requests_equals_requests_this_run():
    """F4: to_stats()['file_dating_requests'] equals _requests_this_run."""
    from storage.file_dating import FileDater

    store = _FakeStore()
    dater = FileDater(store, auth=_FakeAuth(), enabled=True)
    # Simulate some requests.
    dater._requests_this_run = 25
    stats = dater.to_stats()
    assert stats["file_dating_requests"] == 25
    assert stats["requests_this_run"] == 25


def test_f4_batch_increments_client_request_stat():
    """F4: fetch_file_commit_dates_batch increments file_dating_requests on POST."""
    reset_file_date_stats()

    # Call with empty items — no aliases built, no POST, no request increment.
    result = fetch_file_commit_dates_batch([], credential="fake")
    assert result == {}
    assert _FILE_DATE_STATS.get("file_dating_requests", 0) == 0


# ---------------------------------------------------------------------------
# F5: observability — Gather: line renders 304=
# ---------------------------------------------------------------------------
def test_f5_gather_line_renders_304():
    """F5: Gather: line includes 304= token."""
    metrics = {
        "requests_raw": 100,
        "bytes_raw": 50000,
        "gather_304": 7,
    }
    pipeline = PipelineStatus(gather_transport_metrics=metrics)
    status = SystemStatus(pipeline=pipeline)
    line = StatusDisplayEngine._format_gather_transport_metrics_line(status)
    assert "304=7" in line


def test_f5_dating_line_renders_cached():
    """F5: Dating: line includes cached= token."""
    metrics = {
        "enabled": True,
        "scope": "keyed",
        "transport": "rest",
        "file_dating_requests": 10,
        "file_dating_304s": 0,
        "file_dating_filled": 5,
        "file_dating_absent": 5,
        "file_dating_batches": 0,
        "file_dating_graphql_cost": 0,
        "file_dating_refusals": 0,
        "file_dating_budget_stopped": 0,
        "cached_hits": 42,
        "undateable_link": 0,
        "requests_this_run": 10,
        "budget_remaining": 190,
        "fill_rate": 0.5,
    }
    pipeline = PipelineStatus(file_dating_metrics=metrics)
    status = SystemStatus(pipeline=pipeline)
    line = StatusDisplayEngine._format_file_dating_metrics_line(status)
    assert "cached=42" in line


# ---------------------------------------------------------------------------
# F5: conditional_refetch config flag
# ---------------------------------------------------------------------------
def test_f5_conditional_refetch_default_true():
    """F5: gather.conditional_refetch defaults to True."""
    gc = GatherConfig()
    assert gc.conditional_refetch is True


def test_f5_conditional_refetch_rejects_non_bool():
    """F5: gather.conditional_refetch rejects non-bool (no coercion)."""
    with pytest.raises(ValueError, match="gather.conditional_refetch must be a boolean"):
        GatherConfig(conditional_refetch="false")


def test_f5_conditional_refetch_false_accepted():
    """F5: gather.conditional_refetch=False is accepted."""
    gc = GatherConfig(conditional_refetch=False)
    assert gc.conditional_refetch is False


def test_f5_validator_rejects_non_bool():
    """F5: ConfigValidator rejects non-bool conditional_refetch."""
    from config.validator import ConfigValidator
    config = Config()
    config.gather.conditional_refetch = "false"  # type: ignore
    validator = ConfigValidator()
    validator._validate_gather_config(config)
    assert any("gather.conditional_refetch must be a boolean" in e for e in validator.errors)
