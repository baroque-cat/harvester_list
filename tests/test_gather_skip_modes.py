"""Flag-mode and fail-open behaviour for gather-skip (add-gather-skip).

Traceability: gather-skip-S6, S7, S9.

Harness: the SearchStage worker is invoked with a mocked search client that
returns canned URL sets; the engine's read path is manipulated to force
errors.  No network, no live GitHub.
"""

import json
import os
import sqlite3

from config.schemas import Config, StageConfig, TaskConfig
from core.models import Patterns, SearchTask
from search import client as search_client
from stage.base import StageResources
from stage.definition import SearchStage
from storage.gather_skip import MODE_OFF, MODE_ON, MODE_SHADOW, GatherSkipEngine
from storage.registry import Registry, patterns_hash, url_hash

PROVIDER = "openai"
KEY_PATTERN = r"sk-[A-Za-z0-9]{16}"
PATTERNS = patterns_hash(key_pattern=KEY_PATTERN)
URLS = [f"https://github.com/acme/widgets/blob/main/f{i}.py" for i in range(3)]

NOW = 1_700_000_000.0
DAY = 86400.0


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


def _registry(workspace: str) -> Registry:
    registry = Registry(workspace, config=_FastRegistryConfig(), enabled=True)
    assert registry.start() is True
    return registry


def _engine(workspace: str, mode: str) -> GatherSkipEngine:
    return GatherSkipEngine(
        workspace=workspace,
        mode=mode,
        ttl_hours=168.0,
        registry_path=os.path.join(workspace, "registry.sqlite"),
        run_id="run-modes-test",
        clock=lambda: NOW,
    )


def _task_config() -> TaskConfig:
    return TaskConfig(
        name=PROVIDER,
        enabled=True,
        provider_type="openai",
        use_api=False,
        stages=StageConfig(search=True, gather=True, check=False, inspect=False),
        patterns=Patterns(key_pattern=KEY_PATTERN),
    )


def _resources(gather_skip) -> StageResources:
    return StageResources(
        limiter=None,
        providers={},
        config=Config(),
        task_configs={PROVIDER: _task_config()},
        auth=FakeAuth(),
        registry=None,
        gather_skip=gather_skip,
    )


def _seed_known(registry: Registry, url: str, gathered_ts: float = NOW - DAY) -> None:
    registry.record_link(url, transport="web", provider=PROVIDER, ts=gathered_ts - 10)
    registry.record_gather(url, provider=PROVIDER, patterns_hash=PATTERNS, success=True, ts=gathered_ts)
    registry.flush(10.0)


def _run_stage(monkeypatch, urls, gather_skip):
    monkeypatch.setattr(
        search_client,
        "search_with_count",
        lambda **kwargs: (list(urls), len(urls), ""),
    )
    stage = SearchStage(_resources(gather_skip), lambda out: None)
    return stage.process_task(
        SearchTask(provider=PROVIDER, query='"sk-"', regex=KEY_PATTERN, page=1, use_api=False)
    )


def _gather_tasks(output):
    return [task for task, target in output.new_tasks if target == "gather"]


def _gather_keys(output):
    return sorted((task.provider, task.url) for task in _gather_tasks(output))


def _read_log(workspace: str):
    path = os.path.join(workspace, "registry_decisions.jsonl")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


# ---------------------------------------------------------------------------
# gather-skip-S6
# ---------------------------------------------------------------------------
def test_s6_off_mode_is_indistinguishable_from_pre_change(workspace, monkeypatch):
    baseline = _run_stage(monkeypatch, URLS, None)

    engine = _engine(workspace, MODE_OFF)
    lookup_calls = []
    monkeypatch.setattr(engine, "_lookup", lambda *args, **kwargs: lookup_calls.append(1))
    off = _run_stage(monkeypatch, URLS, engine)

    # Identical task list and identical links shard payloads, nothing read.
    assert _gather_keys(baseline) == _gather_keys(off)
    assert baseline.links == off.links
    assert engine.registry_reads == 0
    assert lookup_calls == []


# ---------------------------------------------------------------------------
# gather-skip-S7
# ---------------------------------------------------------------------------
def test_s7_shadow_mode_logs_without_acting(workspace, monkeypatch):
    registry = _registry(workspace)
    try:
        for url in URLS:
            _seed_known(registry, url)
    finally:
        registry.stop()

    engine = _engine(workspace, MODE_SHADOW)
    output = _run_stage(monkeypatch, URLS, engine)

    # Would-skip candidates are logged...
    entries = _read_log(workspace)
    logged = {entry["url_hash"] for entry in entries}
    assert {url_hash(url) for url in URLS} <= logged
    assert all("conditions" in entry and "url_hash" in entry for entry in entries)

    # ...but every acquisition task is still created and links still recorded.
    assert len(_gather_tasks(output)) == len(URLS)
    assert output.links == [(PROVIDER, URLS)]
    assert engine.skipped_known == len(URLS)


# ---------------------------------------------------------------------------
# gather-skip-S9
# ---------------------------------------------------------------------------
def test_s9_read_error_produces_tasks_not_skips(workspace, monkeypatch):
    engine = _engine(workspace, MODE_ON)

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(engine, "_lookup", boom)

    first = engine.decide(URLS, provider=PROVIDER, patterns_hash=PATTERNS)
    second = engine.decide(URLS, provider=PROVIDER, patterns_hash=PATTERNS)

    assert all(not decision.skip for decision in first + second)
    assert engine.read_errors == 2
    # A warning is emitted once per error kind, not once per failed lookup.
    assert engine.read_error_warnings == 1

    # The search worker keeps creating tasks and completes normally.
    output = _run_stage(monkeypatch, URLS, engine)
    assert len(_gather_tasks(output)) == len(URLS)
