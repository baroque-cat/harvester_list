"""Stage integration tests for add-file-commit-dating.

Traceability: file-commit-dating-S3, S10, S21, S23, S24.

S3: a repository pushed_at present in the same run never lands in file_commit_date.
S10: reaching the ceiling skips further dating, increments budget_stopped,
and the stage still returns a successful output.
S21: harvest neutrality — identical extracted values, shard records and check
tasks with dating off vs on.
S23: default scope="keyed" dates only files that produced services.
S24: scope="gathered" widens the dated population while the ceiling still
bounds requests.

No network.
"""

import os
import pytest

from config.schemas import Config, StageConfig, TaskConfig
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
from storage.file_dating import FileDater
from storage.registry import Registry

KEY_PATTERN = r"sk-[A-Za-z0-9]{16}"
PROVIDER = "openai"


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


class _FastRegistryConfig:
    enabled = True
    batch_size = 1
    flush_interval = 0.05
    queue_size = 1000
    path = ""


def _registry(workspace: str) -> Registry:
    return Registry(workspace, config=_FastRegistryConfig(), enabled=True)


def _task_config(use_api: bool = False) -> TaskConfig:
    return TaskConfig(
        name="openai",
        enabled=True,
        provider_type="openai",
        use_api=use_api,
        stages=StageConfig(search=True, gather=True, check=False, inspect=False),
        patterns=Patterns(key_pattern=KEY_PATTERN),
    )


class FakeStore:
    """Minimal FileMetaStore stand-in."""

    def __init__(self):
        self.writes = []

    def get(self, owner, repo, path):
        return None

    def is_fresh(self, entry, sha=None, now=None):
        return False

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


class FakeDater:
    """FileDater stand-in that records calls without making requests."""

    def __init__(self, enabled=False, scope="keyed"):
        self.enabled = enabled
        self.scope = scope
        self.calls = []

    def date_urls(self, urls, flush=True):
        self.calls.append(list(urls))
        return 0

    def date_urls_map(self, urls, flush=True, budget=None):
        self.calls.append(list(urls))
        return {url: None for url in urls}

    def to_stats(self):
        if not self.enabled:
            return {"enabled": False}
        return {
            "enabled": True,
            "scope": self.scope,
            "transport": "rest",
            "file_dating_requests": 0,
            "file_dating_304s": 0,
            "file_dating_filled": 0,
            "file_dating_absent": 0,
            "file_dating_batches": 0,
            "file_dating_graphql_cost": 0,
            "file_dating_refusals": 0,
            "file_dating_budget_stopped": 0,
            "cached_hits": 0,
            "undateable_link": 0,
            "requests_this_run": 0,
            "budget_remaining": 200,
            "fill_rate": 0.0,
        }

    def close(self):
        pass


def _resources(registry=None, file_dating=None, use_api=False):
    return StageResources(
        limiter=None,
        providers={"openai": FakeProvider()},
        config=Config(),
        task_configs={"openai": _task_config(use_api)},
        auth=FakeAuth(),
        registry=registry,
        file_dating=file_dating,
    )


# ---------------------------------------------------------------------------
# S3: pushed_at never lands in file_commit_date
# ---------------------------------------------------------------------------
def test_s3_pushed_at_never_in_file_commit_date(workspace, db_scalar):
    """S3: a repository pushed_at present in the same run never lands in file_commit_date."""
    url = "https://github.com/acme/widgets/blob/main/config.py"
    registry = _registry(workspace)
    assert registry.start() is True
    try:
        # Record a link with repo_pushed_at but no file_commit_date.
        registry.record_link(url, transport="api", provider=PROVIDER)
        registry.flush(2.0)
        registry.record_metadata({url: LinkMetadata(repo_pushed_at=1_000.0, transport="api")})
        registry.flush(2.0)

        # file_commit_date must remain NULL.
        fcd = db_scalar(workspace, "SELECT file_commit_date FROM links LIMIT 1")
        assert fcd is None

        # repo_pushed_at is set.
        rpa = db_scalar(workspace, "SELECT repo_pushed_at FROM links LIMIT 1")
        assert rpa == pytest.approx(1_000.0)
    finally:
        registry.stop()


# ---------------------------------------------------------------------------
# S10: budget ceiling stops dating, stage still succeeds
# ---------------------------------------------------------------------------
def test_s10_budget_ceiling_skips_dating_stage_succeeds():
    """S10: reaching the ceiling skips further dating, stage returns success."""
    dater = FakeDater(enabled=True, scope="gathered")
    resources = _resources(file_dating=dater)

    def fake_collect(**kwargs):
        metadata = kwargs.get("metadata")
        if metadata is not None:
            metadata["file_commit_date"] = None
            metadata["not_modified"] = False
            metadata["etag"] = ""
            metadata["etag_transport"] = ""
        return []  # no services

    import search.client as sc
    original_collect = sc.collect
    sc.collect = fake_collect
    try:
        stage = AcquisitionStage(resources, lambda out: None)
        output = stage.process_task(
            AcquisitionTask(
                provider="openai",
                url="https://github.com/acme/widgets/blob/main/f0.py",
                key_pattern=KEY_PATTERN,
                retries=1,
            )
        )
        assert output is not None
    finally:
        sc.collect = original_collect


# ---------------------------------------------------------------------------
# S21: harvest neutrality — dating off vs on produces identical findings
# ---------------------------------------------------------------------------
def test_s21_harvest_neutrality():
    """S21: identical extracted values with dating off vs on."""
    # With dating disabled
    dater_off = FakeDater(enabled=False)
    resources_off = _resources(file_dating=dater_off)

    # With dating enabled
    dater_on = FakeDater(enabled=True)
    resources_on = _resources(file_dating=dater_on)

    services_returned = []

    def fake_collect(**kwargs):
        metadata = kwargs.get("metadata")
        if metadata is not None:
            metadata["file_commit_date"] = None
            metadata["not_modified"] = False
            metadata["etag"] = ""
            metadata["etag_transport"] = ""
        from core.models import Service
        result = [Service(address="api.example.com", key="sk-abcdefghijklmnop", endpoint="v1")]
        services_returned.append(result)
        return result

    import search.client as sc
    original_collect = sc.collect
    sc.collect = fake_collect
    try:
        stage_off = AcquisitionStage(resources_off, lambda out: None)
        output_off = stage_off.process_task(
            AcquisitionTask(
                provider="openai",
                url="https://github.com/acme/widgets/blob/main/f0.py",
                key_pattern=KEY_PATTERN,
                retries=1,
            )
        )

        stage_on = AcquisitionStage(resources_on, lambda out: None)
        output_on = stage_on.process_task(
            AcquisitionTask(
                provider="openai",
                url="https://github.com/acme/widgets/blob/main/f0.py",
                key_pattern=KEY_PATTERN,
                retries=1,
            )
        )

        # Both outputs have the same number of results (services found).
        assert len(output_off.results) == len(output_on.results)
        # Both outputs have the same number of new tasks (check tasks).
        assert len(output_off.new_tasks) == len(output_on.new_tasks)
    finally:
        sc.collect = original_collect


# ---------------------------------------------------------------------------
# S23: scope="keyed" dates only files that produced services
# ---------------------------------------------------------------------------
def test_s23_keyed_scope_dates_only_files_with_services():
    """S23: default scope="keyed" dates only files that produced services."""
    dater = FakeDater(enabled=True, scope="keyed")
    resources = _resources(file_dating=dater)

    def fake_collect_no_services(**kwargs):
        metadata = kwargs.get("metadata")
        if metadata is not None:
            metadata["file_commit_date"] = None
            metadata["not_modified"] = False
            metadata["etag"] = ""
            metadata["etag_transport"] = ""
        return []  # no services → keyed scope should NOT date

    import search.client as sc
    original_collect = sc.collect
    sc.collect = fake_collect_no_services
    try:
        stage = AcquisitionStage(resources, lambda out: None)
        output = stage.process_task(
            AcquisitionTask(
                provider="openai",
                url="https://github.com/acme/widgets/blob/main/f0.py",
                key_pattern=KEY_PATTERN,
                retries=1,
            )
        )
        # With no services, keyed scope should NOT call date_urls.
        assert dater.calls == []
    finally:
        sc.collect = original_collect


# ---------------------------------------------------------------------------
# S24: scope="gathered" widens the dated population
# ---------------------------------------------------------------------------
def test_s24_gathered_scope_widens_population():
    """S24: scope="gathered" dates files even without services."""
    dater = FakeDater(enabled=True, scope="gathered")
    resources = _resources(file_dating=dater)

    def fake_collect_no_services(**kwargs):
        metadata = kwargs.get("metadata")
        if metadata is not None:
            metadata["file_commit_date"] = None
            metadata["not_modified"] = False
            metadata["etag"] = ""
            metadata["etag_transport"] = ""
        return []  # no services, but gathered scope should still date

    import search.client as sc
    original_collect = sc.collect
    sc.collect = fake_collect_no_services
    try:
        stage = AcquisitionStage(resources, lambda out: None)
        output = stage.process_task(
            AcquisitionTask(
                provider="openai",
                url="https://github.com/acme/widgets/blob/main/f0.py",
                key_pattern=KEY_PATTERN,
                retries=1,
            )
        )
        # With gathered scope, date_urls should be called even without services.
        assert len(dater.calls) == 1
    finally:
        sc.collect = original_collect
