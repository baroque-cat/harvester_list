"""Basket/attribution invariance and shutdown draining (add-search-aggregation).

Traceability: search-aggregation-S20, S22.

Harness: real SearchStage instances (pattern of tests/test_gather_skip_modes.py)
with a real SQLite Registry in the tmp workspace; the web transport function
and total estimator are monkeypatched (no network); aggregation is configured
through the planned ``search/aggregation`` hooks. Import failure IS the
expected RED state.
"""

import time

import pytest

from config.schemas import Config, StageConfig, TaskConfig
from core.models import Patterns, SearchTask
from search import client as search_client
from search.aggregation import SearchAggregator, configure_aggregator, reset_aggregator  # RED driver
from stage.base import StageResources
from stage.definition import SearchStage
from storage.registry import Registry, url_hash

PROV_A = "prov-a"
PROV_B = "prov-b"
KEY_A = r"sk-[a-z]{4}"       # matches the planted key in content
KEY_B = r"tk-[a-z]{4}"       # deliberately does NOT match
QUERY = '/sk-[a-z]{4}/ AND content:"llm"'
URL = "https://github.com/o/r/blob/main/a.py"
CONTENT = f'<a href="/o/r/blob/main/a.py#L10">x</a> leaked sk-abcd here'


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


def _task_config(name, key_pattern):
    return TaskConfig(
        name=name, enabled=True, provider_type="openai_like", use_api=False,
        stages=StageConfig(search=True, gather=True, check=True, inspect=True),
        patterns=Patterns(key_pattern=key_pattern),
    )


def _stage(registry):
    resources = StageResources(
        limiter=None, providers={}, config=Config(),
        task_configs={PROV_A: _task_config(PROV_A, KEY_A), PROV_B: _task_config(PROV_B, KEY_B)},
        auth=FakeAuth(), registry=registry,
    )
    return SearchStage(resources, lambda out: None, thread_count=1)


def _task(provider, regex):
    return SearchTask(provider=provider, query=QUERY, regex=regex, page=1, use_api=False)


def _patch_web(monkeypatch, delay=0.0):
    calls = []

    def fake_web(query, session, page):
        calls.append((query, page))
        if delay:
            time.sleep(delay)
        return CONTENT

    monkeypatch.setattr(search_client, "search_github_web", fake_web)
    monkeypatch.setattr(search_client, "estimate_web_total", lambda q, s, c=None: 1)
    return calls


def _gather_tasks(output):
    return [t for t, target in output.new_tasks if target == "gather"]


def _check_tasks(output):
    return [t for t, target in output.new_tasks if target == "check"]


# ---------------------------------------------------------------------------
# search-aggregation-S20
# ---------------------------------------------------------------------------
def test_s20_shared_response_feeds_two_baskets_correctly(workspace, monkeypatch):
    calls = _patch_web(monkeypatch)
    registry = Registry(str(workspace), config=_FastRegistryConfig(), enabled=True)
    assert registry.start() is True
    configure_aggregator(SearchAggregator(
        mode="on", ttl_web_s=120, ttl_api_s=300,
        max_bytes=64 * 1024 * 1024, join_timeout_s=5, workspace=str(workspace),
    ))
    try:
        stage = _stage(registry)

        out_a = stage.process_task(_task(PROV_A, KEY_A))
        out_b = stage.process_task(_task(PROV_B, KEY_B))

        # ONE real HTTP fetch served both providers
        assert len(calls) == 1

        # identical link sets land in both baskets
        assert out_a.links == [(PROV_A, [URL])]
        assert out_b.links == [(PROV_B, [URL])]

        # downstream tasks carry each provider's OWN patterns
        ga, gb = _gather_tasks(out_a), _gather_tasks(out_b)
        assert [(t.provider, t.url, t.key_pattern) for t in ga] == [(PROV_A, URL, KEY_A)]
        assert [(t.provider, t.url, t.key_pattern) for t in gb] == [(PROV_B, URL, KEY_B)]

        # per-provider extraction from the shared content: A finds its key, B does not
        assert len(_check_tasks(out_a)) == 1
        assert len(_check_tasks(out_b)) == 0

        # registry hooks fired per provider: single row, first discoverer preserved
        registry.flush(10.0)
        import sqlite3, os
        conn = sqlite3.connect(os.path.join(str(workspace), "registry.sqlite"))
        rows = conn.execute("SELECT provider FROM links WHERE url_hash=?", (url_hash(URL),)).fetchall()
        conn.close()
        assert [r[0] for r in rows] == [PROV_A]
    finally:
        reset_aggregator()
        registry.stop()


# ---------------------------------------------------------------------------
# search-aggregation-S22
# ---------------------------------------------------------------------------
def test_s22_shutdown_drain_not_blocked_by_joiners(workspace, monkeypatch):
    _patch_web(monkeypatch, delay=0.6)           # leader in flight longer than join timeout
    registry = Registry(str(workspace), config=_FastRegistryConfig(), enabled=True)
    assert registry.start() is True
    configure_aggregator(SearchAggregator(
        mode="on", ttl_web_s=120, ttl_api_s=300,
        max_bytes=64 * 1024 * 1024, join_timeout_s=0.2, workspace=str(workspace),
    ))
    try:
        stage = _stage(registry)
        stage.thread_count = 2
        stage.start()
        # re-spawn workers to honor thread_count=2 if start() cached the old count
        stage.adjust_workers(2)
        try:
            stage.put_task(_task(PROV_A, KEY_A))
            stage.put_task(_task(PROV_B, KEY_B))
            time.sleep(0.1)                       # let both workers pick tasks up

            began = time.monotonic()
            stage.stop(timeout=3.0)               # graceful shutdown / drain
            elapsed = time.monotonic() - began

            # bounded by join timeout + fetch latency, never a hang
            assert elapsed < 3.0
            assert stage.get_zombie_count() == 0
        finally:
            if stage.running:
                stage.stop(timeout=1.0)
    finally:
        reset_aggregator()
        registry.stop()
