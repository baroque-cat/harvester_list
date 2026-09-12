"""Offline integration checks for registry on/off equivalence and migration.

`link-registry-S8` requires that registry-on and registry-off runs produce the
same shards. Live GitHub traffic is not needed to prove that property at the
integration level, and live results are not guaranteed byte-stable between two
searches. This module therefore drives the **real** `SearchStage` and
`AcquisitionStage` hooks plus the **real** shard persistence stack, mocking only
the network client (`search.client`). It also migrates a workspace that was
produced by that real persistence stack, covering the intent of task 5.4.
"""

import os
import sqlite3

from config.schemas import Config, StageConfig, TaskConfig
from core.models import AcquisitionTask, Condition, Patterns, ResultStorage, SearchTask, Service
from search import client as search_client
from stage.base import StageResources
from stage.definition import AcquisitionStage, SearchStage
from storage.persistence import MultiResultManager
from storage.registry import Registry
from tools.registry_migrate import migrate_workspace

SEARCH_URLS = [
    "https://github.com/acme/widgets/blob/main/config.py#L12",
    "https://github.com/acme/widgets/blob/main/.env",
]
GATHER_URLS = [
    "https://github.com/acme/widgets/blob/main/config.py",
    "https://github.com/acme/widgets/blob/main/broken.py",
]
KEY_PATTERN = r"sk-[A-Za-z0-9]{16}"
# Distinct canonical URLs actually persisted to the links shards:
# config.py (from search + successful gather) and .env. The failed gather never
# produces a shard record.
EXPECTED_SHARD_DISTINCT_LINKS = 2
# Registry rows also include the gather-failed link (recorded by the hook even
# though it never reaches the shards).
EXPECTED_REGISTRY_LINKS = 3


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
        self._result = ResultStorage(
            folder=name,
            filenames={
                "valid": "valid-keys.txt",
                "invalid": "invalid-keys.txt",
                "no_quota": "no-quota-keys.txt",
                "wait_check": "wait-check-keys.txt",
                "material": "material.txt",
                "links": "links.txt",
                "summary": "summary.json",
            },
        )

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

    def check(self, *args, **kwargs):  # pragma: no cover - not exercised
        raise NotImplementedError

    def inspect(self, *args, **kwargs):  # pragma: no cover - not exercised
        raise NotImplementedError


def _task_config():
    return TaskConfig(
        name="openai",
        enabled=True,
        provider_type="openai",
        use_api=False,
        stages=StageConfig(search=True, gather=True, check=False, inspect=False),
        patterns=Patterns(key_pattern=KEY_PATTERN),
    )


def _fake_collect(url):
    if url.endswith("broken.py"):
        raise RuntimeError("simulated fetch failure")
    return [Service(address="https://api.example.com", endpoint="", key="sk-abcdef1234567890", model="gpt-4o")]


def _run_stages_and_persist(workspace, registry, monkeypatch):
    """Run real stage hooks + real shard persistence against a mocked network."""
    provider = FakeProvider()
    resources = StageResources(
        limiter=None,
        providers={"openai": provider},
        config=Config(),
        task_configs={"openai": _task_config()},
        auth=FakeAuth(),
        registry=registry,
    )

    outputs = []
    handler = outputs.append

    monkeypatch.setattr(
        search_client,
        "search_with_count",
        lambda **kwargs: (list(SEARCH_URLS), len(SEARCH_URLS), ""),
    )
    monkeypatch.setattr(search_client, "collect", lambda **kwargs: _fake_collect(kwargs.get("url", "")))

    # `process_task` returns the StageOutput; the handler is only invoked by the
    # worker loop, which we bypass to keep the test deterministic.
    search_output = SearchStage(resources, handler).process_task(
        SearchTask(provider="openai", query='"sk-"', regex=KEY_PATTERN, page=1, use_api=False)
    )
    if search_output:
        outputs.append(search_output)

    acquisition = AcquisitionStage(resources, handler)
    for url in GATHER_URLS:
        output = acquisition.process_task(
            AcquisitionTask(provider="openai", url=url, key_pattern=KEY_PATTERN, retries=1)
        )
        if output:
            outputs.append(output)

    manager = MultiResultManager(
        workspace=workspace,
        providers={"openai": provider},
        batch_size=100,
        save_interval=100,
        simple=False,
        shutdown_timeout=2,
    )
    for output in outputs:
        for provider_name, links in output.links:
            manager.add_links(provider_name, links)
        for provider_name, result_type, data in output.results:
            manager.add_result(provider_name, result_type, data)
    manager.stop_all()
    return outputs


def _shard_records(workspace):
    """Collect NDJSON records per result type, ignoring shard filenames/timestamps."""
    records = {}
    root = os.path.join(workspace, "providers", "openai", "shards")
    if not os.path.isdir(root):
        return records
    for result_type in sorted(os.listdir(root)):
        directory = os.path.join(root, result_type)
        if not os.path.isdir(directory):
            continue
        items = []
        for name in sorted(os.listdir(directory)):
            if not name.endswith(".ndjson"):
                continue
            with open(os.path.join(directory, name), encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        items.append(line)
        records[result_type] = sorted(items)
    return records


def _count(workspace, sql):
    conn = sqlite3.connect(os.path.join(workspace, "registry.sqlite"))
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def test_s8_registry_on_off_produce_identical_shards(tmp_path, monkeypatch):
    """link-registry-S8: flag on/off must not change produced shard records."""
    off_workspace = str(tmp_path / "off")
    on_workspace = str(tmp_path / "on")
    os.makedirs(off_workspace)
    os.makedirs(on_workspace)

    off_registry = Registry(off_workspace, enabled=False)
    on_registry = Registry(on_workspace, enabled=True)
    off_registry.start()
    assert on_registry.start() is True
    try:
        _run_stages_and_persist(off_workspace, off_registry, monkeypatch)
        _run_stages_and_persist(on_workspace, on_registry, monkeypatch)
    finally:
        off_registry.stop()
        on_registry.stop()

    off_records = _shard_records(off_workspace)
    on_records = _shard_records(on_workspace)
    assert off_records == on_records
    assert "links" in off_records and "material" in off_records

    # Flag off created no registry file; flag on recorded the expected rows.
    assert not os.path.exists(os.path.join(off_workspace, "registry.sqlite"))
    assert _count(on_workspace, "SELECT COUNT(*) FROM links") == EXPECTED_REGISTRY_LINKS
    assert _count(on_workspace, "SELECT COUNT(*) FROM link_coverage") == 1


def test_migration_over_pipeline_produced_workspace(tmp_path, monkeypatch):
    """Task 5.4 equivalent: migrate a workspace emitted by the real stack."""
    workspace = str(tmp_path / "pipeline-workspace")
    os.makedirs(workspace)

    registry = Registry(workspace, enabled=False)
    registry.start()
    _run_stages_and_persist(workspace, registry, monkeypatch)
    registry.stop()

    first = migrate_workspace(workspace)
    second = migrate_workspace(workspace)

    assert first["links"] == EXPECTED_SHARD_DISTINCT_LINKS
    assert first["keys"] == 1
    assert first == second  # idempotent
    assert _count(workspace, "SELECT COUNT(*) FROM links") == EXPECTED_SHARD_DISTINCT_LINKS
    assert _count(workspace, "SELECT COUNT(*) FROM keys") == 1
    # Migrated links are conservative.
    status, gathered = sqlite3.connect(os.path.join(workspace, "registry.sqlite")).execute(
        "SELECT visit_status, gathered_ts FROM links LIMIT 1"
    ).fetchone()
    assert status == "discovered"
    assert gathered is None
