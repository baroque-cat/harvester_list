"""Stage-level integration tests for repo-meta enrichment (add-repo-meta-enrichment).

Traceability: repo-meta-enrichment-S6..S11.

Harness: the real ``SearchStage`` / ``AcquisitionStage`` workers are driven with
synthetic tasks against a tmp-workspace registry and a mocked GitHub transport
layer.  Credential cooldown behaviour uses the real
``github_credential_state`` so the rotation rule is genuinely asserted.  No
network.
"""

import datetime
import json
import math
import sqlite3

from config.schemas import Config, StageConfig, TaskConfig
from constant.system import SERVICE_TYPE_GITHUB_API
from core.models import AcquisitionTask, Patterns, SearchTask
from search import client as search_client
from stage.base import StageResources
from stage.definition import AcquisitionStage, SearchStage
from storage.gather_skip import MODE_SHADOW, GatherSkipEngine
from storage.registry import Registry, patterns_hash
from storage.repo_meta import RepoMetaEnricher, RepoMetaStore
from tools.state import GithubCredentialLimited, github_credential_state

PROVIDER = "openai"
KEY_PATTERN = r"sk-[A-Za-z0-9]{16}"
REPO = ("acme", "widgets")

NOW = 1_700_000_000.0
HOUR = 3600.0
DAY = 24 * HOUR
TTL_HOURS = 24.0


class FakeAuth:
    """Round-robin credential provider with an explicit token pool."""

    def __init__(self, tokens=("api-token",)):
        self._tokens = list(tokens)
        self._index = 0

    def get_token(self):
        if not self._tokens:
            return None
        token = self._tokens[self._index % len(self._tokens)]
        self._index += 1
        return token

    def get_session(self):
        return "session-token"

    def get_user_agent(self):
        return "test-agent"


class FakeClient:
    """Transport stand-in; one canned ``(status, body, headers)`` per call."""

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []
        self.credentials = []

    def get_with_status(self, url, headers=None, params=None, retries=3, interval=0, timeout=10, credential=None):
        self.calls.append(url)
        self.credentials.append(credential)
        item = self.responses.pop(0) if self.responses else (500, "", {})
        if isinstance(item, Exception):
            raise item
        return item


class RotatingClient(FakeClient):
    """Marks the first credential limited, then succeeds on the next."""

    def get_with_status(self, url, headers=None, params=None, retries=3, interval=0, timeout=10, credential=None):
        self.calls.append(url)
        self.credentials.append(credential)
        if credential == "tok-1":
            github_credential_state.mark_limited(SERVICE_TYPE_GITHUB_API, credential, 60)
            raise GithubCredentialLimited(SERVICE_TYPE_GITHUB_API, credential, 60, "test rate limit")
        return (200, _body(NOW - HOUR, size=3), {"etag": "E-OK"})


class _FastRegistryConfig:
    enabled = True
    batch_size = 1
    flush_interval = 0.05
    queue_size = 100000
    path = ""


def _iso(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _body(pushed_at: float, size=1, branch="main") -> str:
    return json.dumps({"name": "x", "pushed_at": _iso(pushed_at), "size": size, "default_branch": branch})


def _registry(workspace: str) -> Registry:
    registry = Registry(workspace, config=_FastRegistryConfig(), enabled=True)
    assert registry.start() is True
    return registry


def _task_config() -> TaskConfig:
    return TaskConfig(
        name=PROVIDER,
        enabled=True,
        provider_type="openai",
        use_api=False,
        stages=StageConfig(search=True, gather=True, check=False, inspect=False),
        patterns=Patterns(key_pattern=KEY_PATTERN),
    )


def _resources(*, enrichment=None, gather_skip=None, registry=None, auth=None) -> StageResources:
    return StageResources(
        limiter=None,
        providers={},
        config=Config(),
        task_configs={PROVIDER: _task_config()},
        auth=auth or FakeAuth(),
        registry=registry,
        gather_skip=gather_skip,
        enrichment=enrichment,
    )


def _store(workspace: str, registry: Registry) -> RepoMetaStore:
    return RepoMetaStore(workspace, ttl_hours=TTL_HOURS, registry=registry, clock=lambda: NOW)


def _enricher(workspace, registry, client, *, enabled=True, auth=None) -> RepoMetaEnricher:
    return RepoMetaEnricher(
        _store(workspace, registry),
        auth=auth or FakeAuth(),
        client=client,
        enabled=enabled,
        ttl_hours=TTL_HOURS,
        clock=lambda: NOW,
    )


def _seed_fresh(registry: Registry, owner: str, repo: str, *, ts: float = NOW) -> None:
    registry.record_repo_upsert(
        owner, repo, pushed_at=NOW - HOUR, size_kb=1, default_branch="main", etag="E-FRESH", ts=ts
    )
    registry.flush(10.0)


def _seed_stale(registry: Registry, owner: str, repo: str, *, ts: float = NOW - 3 * DAY) -> None:
    registry.record_repo_upsert(
        owner, repo, pushed_at=NOW - 10 * DAY, size_kb=1, default_branch="main", etag="E-OLD", ts=ts
    )
    registry.flush(10.0)


def _seed_link(registry: Registry, url: str) -> None:
    registry.record_link(url, transport="web", provider=PROVIDER, ts=NOW - DAY)
    registry.flush(10.0)


def _link_urls(owner: str, repo: str, count: int) -> list:
    return [f"https://github.com/{owner}/{repo}/blob/main/f{i}.py" for i in range(count)]


def _repo_row(workspace: str, owner: str, repo: str):
    conn = sqlite3.connect(f"file:{workspace}/registry.sqlite?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM repos WHERE owner = ? AND repo = ?", (owner, repo)).fetchone()
    finally:
        conn.close()


def _acq_task(url: str) -> AcquisitionTask:
    return AcquisitionTask(provider=PROVIDER, url=url, key_pattern=KEY_PATTERN, retries=1)


def _run_search(monkeypatch, urls, resources):
    monkeypatch.setattr(search_client, "search_with_count", lambda **kwargs: (list(urls), len(urls), ""))
    stage = SearchStage(resources, lambda out: None)
    return stage.process_task(
        SearchTask(provider=PROVIDER, query='"sk-"', regex=KEY_PATTERN, page=1, use_api=False)
    )


def _run_gather(monkeypatch, resources):
    monkeypatch.setattr(search_client, "collect", lambda **kwargs: [])
    return AcquisitionStage(resources, lambda out: None)


# ---------------------------------------------------------------------------
# repo-meta-enrichment-S6
# ---------------------------------------------------------------------------
def test_s6_fresh_cache_serves_offline(workspace, monkeypatch):
    registry = _registry(workspace)
    try:
        owner, repo = REPO
        url = _link_urls(owner, repo, 1)[0]
        _seed_link(registry, url)
        _seed_fresh(registry, owner, repo)

        client = FakeClient()
        enricher = _enricher(workspace, registry, client)
        stage = _run_gather(monkeypatch, _resources(enrichment=enricher, registry=registry))
        output = stage.process_task(_acq_task(url))
        registry.flush(10.0)
    finally:
        registry.stop()

    assert output is not None
    assert client.calls == []  # cache-first: zero enrichment HTTP requests


# ---------------------------------------------------------------------------
# repo-meta-enrichment-S7
# ---------------------------------------------------------------------------
def test_s7_stale_cache_at_skip_evaluation_triggers_one_shared_refresh(workspace, monkeypatch):
    registry = _registry(workspace)
    owner, repo = REPO
    urls = _link_urls(owner, repo, 5)
    try:
        for url in urls:
            _seed_link(registry, url)
        _seed_stale(registry, owner, repo)

        engine = GatherSkipEngine(
            workspace=workspace,
            mode=MODE_SHADOW,
            ttl_hours=168.0,
            registry_path=registry.path,
            run_id="run-s7",
            clock=lambda: NOW,
        )
        client = FakeClient([(200, _body(NOW - HOUR, size=2), {"etag": "E-NEW"})])
        enricher = _enricher(workspace, registry, client)
        output = _run_search(
            monkeypatch,
            urls,
            _resources(enrichment=enricher, gather_skip=engine, registry=registry),
        )
        decisions = engine.decide(urls, provider=PROVIDER, patterns_hash=patterns_hash(key_pattern=KEY_PATTERN))
        engine.close()
        registry.flush(10.0)

        # Failure path: a refresh that raises still lets decisions proceed
        # fail-open.  Age the cache again so the refresh is actually attempted.
        _seed_stale(registry, owner, repo)
        failing = FakeClient([RuntimeError("boom")])
        enricher_fail = _enricher(workspace, registry, failing)
        engine_fail = GatherSkipEngine(
            workspace=workspace,
            mode=MODE_SHADOW,
            ttl_hours=168.0,
            registry_path=registry.path,
            run_id="run-s7b",
            clock=lambda: NOW,
        )
        try:
            _run_search(
                monkeypatch,
                urls,
                _resources(enrichment=enricher_fail, gather_skip=engine_fail, registry=registry),
            )
        finally:
            engine_fail.close()
    finally:
        registry.stop()

    # Exactly one shared conditional refresh for the single stale repository.
    assert len(client.calls) == 1
    assert enricher.to_stats()["enrichment_fetches"] == 1
    # Its outcome serves all five decisions; none of them crash the batch.
    assert output is not None
    gather_tasks = [task for task, target in output.new_tasks if target == "gather"]
    assert len(gather_tasks) == 5
    assert len(decisions) == 5

    # The failed refresh degraded fail-open instead of raising or blocking.
    assert enricher_fail.to_stats()["enrichment_failures"] == 1


# ---------------------------------------------------------------------------
# repo-meta-enrichment-S8
# ---------------------------------------------------------------------------
def test_s8_duplicate_encounters_fetch_once(workspace, monkeypatch):
    registry = _registry(workspace)
    owner, repo = REPO
    try:
        client = FakeClient([(200, _body(NOW - HOUR, size=1), {"etag": "E-ONE"})])
        enricher = _enricher(workspace, registry, client)
        stage = _run_gather(monkeypatch, _resources(enrichment=enricher, registry=registry))

        # 30 links of the same novel repository, gathered within a single run.
        for url in _link_urls(owner, repo, 30):
            stage.process_task(_acq_task(url))
        registry.flush(10.0)
    finally:
        registry.stop()

    assert len(client.calls) == 1
    assert enricher.to_stats()["enrichment_fetches"] == 1


# ---------------------------------------------------------------------------
# repo-meta-enrichment-S9
# ---------------------------------------------------------------------------
def test_s9_rate_limited_credential_rotates_like_search(workspace):
    github_credential_state._items.clear()
    registry = _registry(workspace)
    owner, repo = REPO
    try:
        _seed_link(registry, _link_urls(owner, repo, 1)[0])
        client = RotatingClient()
        enricher = _enricher(workspace, registry, client, auth=FakeAuth(tokens=["tok-1", "tok-2"]))
        fetched = enricher.enrich_pairs([(owner, repo)])
        cooling_tok1 = github_credential_state.is_cooling(SERVICE_TYPE_GITHUB_API, "tok-1")
        registry.flush(10.0)
    finally:
        registry.stop()
        github_credential_state._items.clear()

    assert fetched == 1
    assert client.credentials == ["tok-1", "tok-2"]  # rotated after the limit signal
    assert cooling_tok1 is True  # the limited credential inherited cooldown state
    # The retry succeeded and persisted real values.
    assert len(client.calls) == 2


def test_s9_supplement_retries_are_bounded(workspace):
    github_credential_state._items.clear()
    registry = _registry(workspace)
    owner, repo = REPO
    try:
        client = FakeClient([GithubCredentialLimited(SERVICE_TYPE_GITHUB_API, "tok-1", 1.0, "x")] * 10)
        enricher = _enricher(workspace, registry, client, auth=FakeAuth(tokens=["tok-1"]))
        fetched = enricher.enrich_pairs([(owner, repo)])
        registry.flush(10.0)
    finally:
        registry.stop()
        github_credential_state._items.clear()

    assert fetched == 0  # abandoned fail-open, never blocked
    stats = enricher.to_stats()
    assert stats["enrichment_failures"] == 1
    assert len(client.calls) <= 5


# ---------------------------------------------------------------------------
# repo-meta-enrichment-S10
# ---------------------------------------------------------------------------
def test_s10_tokenless_deployment_disables_silently(workspace, monkeypatch):
    registry = _registry(workspace)
    owner, repo = REPO
    try:
        url = _link_urls(owner, repo, 1)[0]
        _seed_link(registry, url)
        client = FakeClient()
        enricher = _enricher(workspace, registry, client, auth=FakeAuth(tokens=[]))
        stage = _run_gather(monkeypatch, _resources(enrichment=enricher, registry=registry, auth=FakeAuth(tokens=[])))
        output = stage.process_task(_acq_task(url))
        output2 = stage.process_task(_acq_task(url))
        registry.flush(10.0)
    finally:
        registry.stop()

    assert output is not None and output2 is not None  # web-only pipeline completes
    assert client.calls == []  # zero requests
    assert _repo_row(workspace, owner, repo) is None
    stats = enricher.to_stats()
    assert stats["disabled_tokenless"] is True
    assert stats["tokenless_logs"] == 1  # at most one informational line


def test_s10_supplement_all_cooling_yields_without_blocking(workspace):
    """R4: all tokens cooling -> transient silent yield, never a blocking wait."""
    registry = _registry(workspace)
    owner, repo = REPO
    try:
        state = {"cooling": True}
        client = FakeClient([(200, _body(NOW - HOUR), {"etag": "E"})])
        enricher = RepoMetaEnricher(
            _store(workspace, registry),
            auth=FakeAuth(),
            client=client,
            enabled=True,
            ttl_hours=TTL_HOURS,
            clock=lambda: NOW,
            cooling_probe=lambda: state["cooling"],
        )
        assert enricher.enrich_pairs([(owner, repo)]) == 0
        assert client.calls == []  # zero requests while every token cools
        stats = enricher.to_stats()
        assert stats["cooling_skips"] == 1
        assert stats["disabled_tokenless"] is False  # transient, not latched

        # Once tokens recover, enrichment resumes within the same run.
        state["cooling"] = False
        assert enricher.enrich_pairs([(owner, repo)]) == 1
        assert len(client.calls) == 1
        registry.flush(10.0)
    finally:
        registry.stop()


def test_supplement_batch_budget_bounds_skip_eval_refresh(workspace):
    """D4(b): the synchronous refresh is bounded by a wall-clock batch budget."""
    registry = _registry(workspace)
    try:
        client = FakeClient([(200, _body(NOW - HOUR), {"etag": "E"})] * 3)
        enricher = _enricher(workspace, registry, client)
        # Zero budget: no fetch is started; work stays stale-or-missing in the
        # ledger for a later trigger (fail-open, nothing lost).
        assert enricher.enrich_pairs([("o1", "r1"), ("o2", "r2")], budget=0.0) == 0
        assert client.calls == []
        # Unbounded budget: every stale repo is fetched.
        assert enricher.enrich_pairs([("o1", "r1"), ("o2", "r2")], budget=math.inf) == 2
        assert len(client.calls) == 2
        registry.flush(10.0)
    finally:
        registry.stop()


# ---------------------------------------------------------------------------
# repo-meta-enrichment-S11
# ---------------------------------------------------------------------------
def test_s11_disabled_flag_means_zero_footprint(workspace, monkeypatch):
    # Default flag is off.
    assert Config().enrichment.enabled is False

    registry = _registry(workspace)
    owner, repo = REPO
    try:
        url = _link_urls(owner, repo, 1)[0]
        _seed_link(registry, url)
        client = FakeClient()
        enricher = _enricher(workspace, registry, client, enabled=False)
        assert enricher.to_stats() == {"enabled": False}  # no enrichment metrics emitted

        writes = []
        for name in ("record_upsert", "record_touch", "record_gone", "record_link_metadata"):
            setattr(
                enricher.store,
                name,
                (lambda method: lambda *a, **k: writes.append((method, a, k)))(name),
            )

        stage = _run_gather(monkeypatch, _resources(enrichment=enricher, registry=registry))
        output = stage.process_task(_acq_task(url))
        # Literal A/B baseline: the identical run with no enricher wired at all
        # (pre-change shape).
        baseline_stage = _run_gather(monkeypatch, _resources(enrichment=None, registry=registry))
        baseline_output = baseline_stage.process_task(_acq_task(url))
        enricher.enrich_pairs([(owner, repo)])
        registry.flush(10.0)
    finally:
        registry.stop()

    assert output is not None
    assert client.calls == []
    assert writes == []  # zero registry writes beyond pre-existing channels
    # Disabled flag is indistinguishable from the pre-change baseline.
    assert output.links == baseline_output.links
    assert [(type(t), t.provider, getattr(t, "url", None)) for t, _ in output.new_tasks] == [
        (type(t), t.provider, getattr(t, "url", None)) for t, _ in baseline_output.new_tasks
    ]
    assert output.results == baseline_output.results
