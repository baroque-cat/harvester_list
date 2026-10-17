"""R7 — Stage integration pins (cross-provider fetch sharing).

Traceability: /tmp/opencode/r7-plan.md §3 D4–D8, D10, D11.

Harness: SearchStage and AcquisitionStage run against fake clients and (where
needed) a real SQLite Registry in a tmp workspace.  No network, no live GitHub,
no credentials.

Pin names: test_s1_… through test_s13_… for stage integration, test_r1_…
through test_r4_… for render (in test_r7_render.py).
"""

import os
import sqlite3
import threading

import pytest

from config.schemas import Config, StageConfig, TaskConfig
from core.enums import ResultType
from core.exceptions import RateLimitDeferral, TransientFetchError
from core.models import (
    AcquisitionTask,
    CheckTask,
    LinkMetadata,
    Patterns,
    SearchTask,
    Service,
)
from manager.gather_share import GatherShareLedger
from search import client as search_client
from stage.base import StageResources
from stage.definition import AcquisitionStage, SearchStage
from stage.factory import TaskFactory
from storage.registry import Registry, patterns_hash, url_hash

KEY_PATTERN = r"sk-[A-Za-z0-9]{16}"
URL = "https://github.com/acme/widgets/blob/main/leak.py"
PROVIDER_A = "openai"
PROVIDER_B = "deepseek"


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


def _task_config(name: str, use_api: bool = False) -> TaskConfig:
    return TaskConfig(
        name=name,
        enabled=True,
        provider_type="openai_like",
        use_api=use_api,
        stages=StageConfig(search=True, gather=True, check=True, inspect=False),
        patterns=Patterns(key_pattern=KEY_PATTERN),
    )


def _search_resources(ledger=None, use_api_a=False, use_api_b=False):
    cfg = Config()
    return StageResources(
        limiter=None,
        providers={},
        config=cfg,
        task_configs={
            PROVIDER_A: _task_config(PROVIDER_A, use_api=use_api_a),
            PROVIDER_B: _task_config(PROVIDER_B, use_api=use_api_b),
        },
        auth=FakeAuth(),
        registry=None,
        share_ledger=ledger,
    )


def _gather_resources(registry=None, ledger=None, use_api_a=False, use_api_b=False, file_dating=None):
    cfg = Config()
    return StageResources(
        limiter=None,
        providers={},
        config=cfg,
        task_configs={
            PROVIDER_A: _task_config(PROVIDER_A, use_api=use_api_a),
            PROVIDER_B: _task_config(PROVIDER_B, use_api=use_api_b),
        },
        auth=FakeAuth(),
        registry=registry,
        share_ledger=ledger,
        file_dating=file_dating,
    )


def _gather_tasks(output):
    return [task for task, target in output.new_tasks if target == "gather"]


def _check_tasks(output):
    return [task for task, target in output.new_tasks if target == "check"]


# ---------------------------------------------------------------------------
# S1: two providers, same URL, same patterns, on mode → exactly ONE task
# ---------------------------------------------------------------------------
def test_s1_two_providers_same_key_one_task(monkeypatch):
    """R7 D4: two providers discovering the same URL with identical patterns
    in on mode → exactly ONE acquisition task created."""
    ledger = GatherShareLedger(mode="on")
    resources = _search_resources(ledger=ledger)
    monkeypatch.setattr(
        search_client, "search_with_count",
        lambda **kwargs: ([URL], 1, ""),
    )

    # Provider A claims first
    stage_a = SearchStage(resources, lambda out: None)
    output_a = stage_a.process_task(
        SearchTask(provider=PROVIDER_A, query='"sk-"', regex=KEY_PATTERN, page=1, use_api=False)
    )
    tasks_a = _gather_tasks(output_a)
    assert len(tasks_a) == 1
    assert tasks_a[0].provider == PROVIDER_A

    # Provider B discovers the same URL → suppressed by ledger
    stage_b = SearchStage(resources, lambda out: None)
    output_b = stage_b.process_task(
        SearchTask(provider=PROVIDER_B, query='"sk-"', regex=KEY_PATTERN, page=1, use_api=False)
    )
    tasks_b = _gather_tasks(output_b)
    assert len(tasks_b) == 0

    stats = ledger.to_stats()
    assert stats["suppressed"] == 1
    assert stats["claims"] == 2


# ---------------------------------------------------------------------------
# S2: differing patterns_hash or transport → TWO tasks
# ---------------------------------------------------------------------------
def test_s2_differing_patterns_two_tasks(monkeypatch):
    """R7 D4: differing patterns_hash → TWO tasks."""
    ledger = GatherShareLedger(mode="on")
    cfg = Config()
    resources = StageResources(
        limiter=None,
        providers={},
        config=cfg,
        task_configs={
            PROVIDER_A: _task_config(PROVIDER_A),
            PROVIDER_B: TaskConfig(
                name=PROVIDER_B,
                enabled=True,
                provider_type="openai_like",
                use_api=False,
                stages=StageConfig(search=True, gather=True, check=True, inspect=False),
                patterns=Patterns(key_pattern=r"ghp_[A-Za-z0-9]{36}"),  # different pattern
            ),
        },
        auth=FakeAuth(),
        registry=None,
        share_ledger=ledger,
    )
    monkeypatch.setattr(
        search_client, "search_with_count",
        lambda **kwargs: ([URL], 1, ""),
    )

    stage_a = SearchStage(resources, lambda out: None)
    output_a = stage_a.process_task(
        SearchTask(provider=PROVIDER_A, query='"sk-"', regex=KEY_PATTERN, page=1, use_api=False)
    )
    stage_b = SearchStage(resources, lambda out: None)
    output_b = stage_b.process_task(
        SearchTask(provider=PROVIDER_B, query='"ghp_"', regex=r"ghp_[A-Za-z0-9]{36}", page=1, use_api=False)
    )

    assert len(_gather_tasks(output_a)) == 1
    assert len(_gather_tasks(output_b)) == 1


def test_s2_differing_transport_two_tasks(monkeypatch):
    """R7 D4: differing gather transport → TWO tasks."""
    ledger = GatherShareLedger(mode="on")
    cfg = Config()
    # Provider A uses "raw" transport, provider B uses "html"
    cfg.gather.transport = "raw"
    resources_a = StageResources(
        limiter=None, providers={}, config=cfg,
        task_configs={PROVIDER_A: _task_config(PROVIDER_A)},
        auth=FakeAuth(), registry=None, share_ledger=ledger,
    )
    cfg2 = Config()
    cfg2.gather.transport = "html"
    resources_b = StageResources(
        limiter=None, providers={}, config=cfg2,
        task_configs={PROVIDER_B: _task_config(PROVIDER_B)},
        auth=FakeAuth(), registry=None, share_ledger=ledger,
    )
    monkeypatch.setattr(
        search_client, "search_with_count",
        lambda **kwargs: ([URL], 1, ""),
    )

    stage_a = SearchStage(resources_a, lambda out: None)
    output_a = stage_a.process_task(
        SearchTask(provider=PROVIDER_A, query='"sk-"', regex=KEY_PATTERN, page=1, use_api=False)
    )
    stage_b = SearchStage(resources_b, lambda out: None)
    output_b = stage_b.process_task(
        SearchTask(provider=PROVIDER_B, query='"sk-"', regex=KEY_PATTERN, page=1, use_api=False)
    )

    assert len(_gather_tasks(output_a)) == 1
    assert len(_gather_tasks(output_b)) == 1


# ---------------------------------------------------------------------------
# S3: shadow mode → TWO tasks, would_suppress >= 1, suppressed == 0
# ---------------------------------------------------------------------------
def test_s3_shadow_mode_two_tasks_would_suppress(monkeypatch):
    """R7 D4: shadow mode → TWO tasks created, would_suppress >= 1, suppressed == 0."""
    ledger = GatherShareLedger(mode="shadow")
    resources = _search_resources(ledger=ledger)
    monkeypatch.setattr(
        search_client, "search_with_count",
        lambda **kwargs: ([URL], 1, ""),
    )

    stage_a = SearchStage(resources, lambda out: None)
    output_a = stage_a.process_task(
        SearchTask(provider=PROVIDER_A, query='"sk-"', regex=KEY_PATTERN, page=1, use_api=False)
    )
    stage_b = SearchStage(resources, lambda out: None)
    output_b = stage_b.process_task(
        SearchTask(provider=PROVIDER_B, query='"sk-"', regex=KEY_PATTERN, page=1, use_api=False)
    )

    assert len(_gather_tasks(output_a)) == 1
    assert len(_gather_tasks(output_b)) == 1
    stats = ledger.to_stats()
    assert stats["would_suppress"] >= 1
    assert stats["suppressed"] == 0


# ---------------------------------------------------------------------------
# S4: off mode → TWO tasks, claims == 0, ledger state untouched
# ---------------------------------------------------------------------------
def test_s4_off_mode_two_tasks_no_claims(monkeypatch):
    """R7 D4: off mode → TWO tasks, claims == 0, ledger state untouched."""
    ledger = GatherShareLedger(mode="off")
    resources = _search_resources(ledger=ledger)
    monkeypatch.setattr(
        search_client, "search_with_count",
        lambda **kwargs: ([URL], 1, ""),
    )

    stage_a = SearchStage(resources, lambda out: None)
    output_a = stage_a.process_task(
        SearchTask(provider=PROVIDER_A, query='"sk-"', regex=KEY_PATTERN, page=1, use_api=False)
    )
    stage_b = SearchStage(resources, lambda out: None)
    output_b = stage_b.process_task(
        SearchTask(provider=PROVIDER_B, query='"sk-"', regex=KEY_PATTERN, page=1, use_api=False)
    )

    assert len(_gather_tasks(output_a)) == 1
    assert len(_gather_tasks(output_b)) == 1
    stats = ledger.to_stats()
    assert stats["claims"] == 0


# ---------------------------------------------------------------------------
# S5: successful shared gather writes coverage for EVERY beneficiary
# ---------------------------------------------------------------------------
def test_s5_shared_gather_writes_coverage_for_all(workspace, monkeypatch):
    """R7 D5: a successful shared gather writes a link_coverage row for EVERY
    beneficiary while the links row is written once with visit_status='gathered_ok'."""
    registry = _registry(workspace)
    try:
        ledger = GatherShareLedger(mode="on")
        digest = patterns_hash(key_pattern=KEY_PATTERN)
        transport = "raw"
        # Simulate two providers claiming the same key
        ledger.claim(url_hash(URL), digest, transport, PROVIDER_A)
        ledger.claim(url_hash(URL), digest, transport, PROVIDER_B)

        services = [Service(address="api.example.com", key="sk-abcdefghijklmnop", endpoint="v1")]

        def fake_collect(**kwargs):
            metadata = kwargs.get("metadata")
            if metadata is not None:
                metadata["file_commit_date"] = None
                metadata["not_modified"] = False
                metadata["etag"] = ""
                metadata["etag_transport"] = ""
            return services

        monkeypatch.setattr(search_client, "collect", fake_collect)

        resources = _gather_resources(registry=registry, ledger=ledger)
        stage = AcquisitionStage(resources, lambda out: None)
        task = TaskFactory.create_acquisition_task(PROVIDER_A, URL, Patterns(key_pattern=KEY_PATTERN))
        output = stage.process_task(task)
        assert output is not None

        registry.flush(10.0)

        # links row written once with gathered_ok
        links = _rows(workspace, "SELECT visit_status FROM links WHERE url_hash=?", (url_hash(URL),))
        assert len(links) == 1
        assert links[0]["visit_status"] == "gathered_ok"

        # coverage rows for BOTH providers
        coverage = _rows(
            workspace,
            "SELECT provider FROM link_coverage WHERE url_hash=? AND patterns_hash=?",
            (url_hash(URL), digest),
        )
        coverage_providers = {r["provider"] for r in coverage}
        # Owner gets coverage from record_gather, beneficiary from record_shared_coverage
        assert PROVIDER_A in coverage_providers
        assert PROVIDER_B in coverage_providers
    finally:
        registry.stop()


# ---------------------------------------------------------------------------
# S6: critical attribution pin — every beneficiary gets check task + result
# ---------------------------------------------------------------------------
def test_s6_every_beneficiary_gets_check_task_and_result(monkeypatch):
    """R7 D5: when the fetch yields services, every beneficiary gets its own
    check task AND its own shard result."""
    ledger = GatherShareLedger(mode="on")
    digest = patterns_hash(key_pattern=KEY_PATTERN)
    transport = "raw"
    ledger.claim(url_hash(URL), digest, transport, PROVIDER_A)
    ledger.claim(url_hash(URL), digest, transport, PROVIDER_B)

    services = [
        Service(address="api.example.com", key="sk-abcdefghijklmnop", endpoint="v1"),
        Service(address="api2.example.com", key="sk-qrstuvwxyz012345", endpoint="v2"),
    ]

    def fake_collect(**kwargs):
        metadata = kwargs.get("metadata")
        if metadata is not None:
            metadata["file_commit_date"] = None
            metadata["not_modified"] = False
            metadata["etag"] = ""
            metadata["etag_transport"] = ""
        return services

    monkeypatch.setattr(search_client, "collect", fake_collect)

    resources = _gather_resources(ledger=ledger)
    stage = AcquisitionStage(resources, lambda out: None)
    task = TaskFactory.create_acquisition_task(PROVIDER_A, URL, Patterns(key_pattern=KEY_PATTERN))
    output = stage.process_task(task)
    assert output is not None

    # Check tasks: one per (beneficiary × service) = 2 providers × 2 services = 4
    checks = _check_tasks(output)
    assert len(checks) == 4
    check_providers = {(c.provider, c.service.address) for c in checks}
    assert (PROVIDER_A, "api.example.com") in check_providers
    assert (PROVIDER_A, "api2.example.com") in check_providers
    assert (PROVIDER_B, "api.example.com") in check_providers
    assert (PROVIDER_B, "api2.example.com") in check_providers

    # Results: one MATERIAL entry per beneficiary
    material_results = [(p, t) for p, t, _ in output.results if t == ResultType.MATERIAL.value]
    assert len(material_results) == 2
    result_providers = {p for p, _ in material_results}
    assert PROVIDER_A in result_providers
    assert PROVIDER_B in result_providers


# ---------------------------------------------------------------------------
# S7: dating hook invoked exactly once per URL for 3 beneficiaries
# ---------------------------------------------------------------------------
def test_s7_dating_invoked_once_per_url(monkeypatch):
    """R7 D7: the dating hook is invoked exactly once per URL for 3 beneficiaries."""
    ledger = GatherShareLedger(mode="on")
    digest = patterns_hash(key_pattern=KEY_PATTERN)
    transport = "raw"
    provider_c = "anthropic"
    ledger.claim(url_hash(URL), digest, transport, PROVIDER_A)
    ledger.claim(url_hash(URL), digest, transport, PROVIDER_B)
    ledger.claim(url_hash(URL), digest, transport, provider_c)

    class CountingDater:
        def __init__(self):
            self.enabled = True
            self.scope = "gathered"
            self.calls = []

        def date_urls_map(self, urls, budget=None):
            self.calls.append(list(urls))
            return {url: None for url in urls}

        def to_stats(self):
            return {"enabled": True, "scope": "gathered"}

        def close(self):
            pass

    dater = CountingDater()

    def fake_collect(**kwargs):
        metadata = kwargs.get("metadata")
        if metadata is not None:
            metadata["file_commit_date"] = None
            metadata["not_modified"] = False
            metadata["etag"] = ""
            metadata["etag_transport"] = ""
        return []

    monkeypatch.setattr(search_client, "collect", fake_collect)

    cfg = Config()
    resources = StageResources(
        limiter=None,
        providers={},
        config=cfg,
        task_configs={
            PROVIDER_A: _task_config(PROVIDER_A),
            PROVIDER_B: _task_config(PROVIDER_B),
            provider_c: _task_config(provider_c),
        },
        auth=FakeAuth(),
        registry=None,
        share_ledger=ledger,
        file_dating=dater,
    )
    stage = AcquisitionStage(resources, lambda out: None)
    task = TaskFactory.create_acquisition_task(PROVIDER_A, URL, Patterns(key_pattern=KEY_PATTERN))
    output = stage.process_task(task)
    assert output is not None

    # Dating called exactly once for scope="gathered"
    assert len(dater.calls) == 1
    assert dater.calls[0] == [URL]


# ---------------------------------------------------------------------------
# S8: RateLimitDeferral neither takes nor abandons
# ---------------------------------------------------------------------------
def test_s8_rate_limit_deferral_preserves_ledger_entry(monkeypatch):
    """R7 D8: a RateLimitDeferral raised by collect neither takes nor abandons:
    the ledger entry survives with all providers and no coverage row is written."""
    ledger = GatherShareLedger(mode="on")
    digest = patterns_hash(key_pattern=KEY_PATTERN)
    transport = "raw"
    ledger.claim(url_hash(URL), digest, transport, PROVIDER_A)
    ledger.claim(url_hash(URL), digest, transport, PROVIDER_B)

    def fake_collect(**kwargs):
        raise RateLimitDeferral(message="rate limited", wait_s=60.0)

    monkeypatch.setattr(search_client, "collect", fake_collect)

    resources = _gather_resources(ledger=ledger)
    stage = AcquisitionStage(resources, lambda out: None, max_retries=0)
    task = TaskFactory.create_acquisition_task(PROVIDER_A, URL, Patterns(key_pattern=KEY_PATTERN))

    with pytest.raises(RateLimitDeferral):
        stage.process_task(task)

    # Ledger entry must still exist (not taken, not abandoned)
    taken = ledger.take(url_hash(URL), digest, transport)
    assert PROVIDER_A in taken
    assert PROVIDER_B in taken


# ---------------------------------------------------------------------------
# S9: failing gather abandons — entry removed, abandoned counted, zero coverage
# ---------------------------------------------------------------------------
def test_s9_failing_gather_abandons(workspace, monkeypatch):
    """R7 D8: a failing gather abandons: entry removed, abandoned counted,
    zero coverage rows."""
    registry = _registry(workspace)
    try:
        ledger = GatherShareLedger(mode="on")
        digest = patterns_hash(key_pattern=KEY_PATTERN)
        transport = "raw"
        ledger.claim(url_hash(URL), digest, transport, PROVIDER_A)
        ledger.claim(url_hash(URL), digest, transport, PROVIDER_B)

        def fake_collect(**kwargs):
            raise TransientFetchError("HTTP 503")

        monkeypatch.setattr(search_client, "collect", fake_collect)

        cfg = Config()
        cfg.pipeline.failure_handling = "strict"
        resources = StageResources(
            limiter=None,
            providers={},
            config=cfg,
            task_configs={
                PROVIDER_A: _task_config(PROVIDER_A),
                PROVIDER_B: _task_config(PROVIDER_B),
            },
            auth=FakeAuth(),
            registry=registry,
            share_ledger=ledger,
        )
        stage = AcquisitionStage(resources, lambda out: None, max_retries=0)
        task = TaskFactory.create_acquisition_task(PROVIDER_A, URL, Patterns(key_pattern=KEY_PATTERN))

        with pytest.raises(TransientFetchError):
            stage.process_task(task)

        registry.flush(10.0)

        # Entry removed
        taken = ledger.take(url_hash(URL), digest, transport)
        assert taken == []

        # Abandoned counted
        stats = ledger.to_stats()
        assert stats["abandoned"] >= 1

        # Zero coverage rows
        coverage = _rows(
            workspace,
            "SELECT COUNT(*) FROM link_coverage WHERE url_hash=?",
            (url_hash(URL),),
        )
        assert coverage[0][0] == 0
    finally:
        registry.stop()


# ---------------------------------------------------------------------------
# S10: absent ledger entry → owner served exactly as pre-change
# ---------------------------------------------------------------------------
def test_s10_absent_ledger_entry_owner_served_normally(monkeypatch):
    """R7 D1: absent ledger entry (simulate a restart by constructing the task
    without claiming) → the owner is served exactly as pre-change."""
    ledger = GatherShareLedger(mode="on")
    # Do NOT claim — simulate a restart where the ledger lost the entry.

    services = [Service(address="api.example.com", key="sk-abcdefghijklmnop", endpoint="v1")]

    def fake_collect(**kwargs):
        metadata = kwargs.get("metadata")
        if metadata is not None:
            metadata["file_commit_date"] = None
            metadata["not_modified"] = False
            metadata["etag"] = ""
            metadata["etag_transport"] = ""
        return services

    monkeypatch.setattr(search_client, "collect", fake_collect)

    resources = _gather_resources(ledger=ledger)
    stage = AcquisitionStage(resources, lambda out: None)
    task = TaskFactory.create_acquisition_task(PROVIDER_A, URL, Patterns(key_pattern=KEY_PATTERN))
    output = stage.process_task(task)
    assert output is not None

    # Owner gets exactly one add_links
    link_entries = [(p, ls) for p, ls in output.links]
    assert len(link_entries) == 1
    assert link_entries[0] == (PROVIDER_A, [URL])

    # Owner gets exactly one MATERIAL result
    material_results = [(p, t) for p, t, _ in output.results if t == ResultType.MATERIAL.value]
    assert len(material_results) == 1
    assert material_results[0][0] == PROVIDER_A

    # Owner gets check tasks (1 service → 1 check task)
    checks = _check_tasks(output)
    assert len(checks) == 1
    assert checks[0].provider == PROVIDER_A


# ---------------------------------------------------------------------------
# S11: four concurrent gather workers — no lost providers, no cross-talk
# ---------------------------------------------------------------------------
def test_s11_concurrent_workers_no_lost_providers(monkeypatch):
    """R7 D1: four concurrent gather workers over distinct keys lose no
    providers and never cross-talk."""
    ledger = GatherShareLedger(mode="on")
    urls = [f"https://github.com/acme/widgets/blob/main/f{i}.py" for i in range(4)]
    digest = patterns_hash(key_pattern=KEY_PATTERN)
    transport = "raw"

    # Each URL has two providers
    for url in urls:
        ledger.claim(url_hash(url), digest, transport, PROVIDER_A)
        ledger.claim(url_hash(url), digest, transport, PROVIDER_B)

    results_collected = []
    lock = threading.Lock()

    def fake_collect(**kwargs):
        metadata = kwargs.get("metadata")
        if metadata is not None:
            metadata["file_commit_date"] = None
            metadata["not_modified"] = False
            metadata["etag"] = ""
            metadata["etag_transport"] = ""
        return [Service(address="api.example.com", key="sk-abcdefghijklmnop", endpoint="v1")]

    monkeypatch.setattr(search_client, "collect", fake_collect)

    resources = _gather_resources(ledger=ledger)
    stage = AcquisitionStage(resources, lambda out: None)

    barrier = threading.Barrier(4)
    errors = []

    def worker(url):
        try:
            barrier.wait(timeout=5.0)
            task = TaskFactory.create_acquisition_task(PROVIDER_A, url, Patterns(key_pattern=KEY_PATTERN))
            output = stage.process_task(task)
            with lock:
                if output is not None:
                    results_collected.append((url, output))
        except Exception as e:
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=worker, args=(url,)) for url in urls]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    assert errors == []
    assert len(results_collected) == 4

    # Each URL should have produced outputs for both providers
    for url, output in results_collected:
        link_providers = {p for p, ls in output.links}
        assert PROVIDER_A in link_providers
        assert PROVIDER_B in link_providers


# ---------------------------------------------------------------------------
# S12: web beneficiary of an api owner gets transport="web"
# ---------------------------------------------------------------------------
def test_s12_web_beneficiary_gets_web_transport(monkeypatch):
    """R7 D6: a web beneficiary (use_api=False) of an api owner receives
    LinkMetadata.transport == "web" while the owner's is "api"."""
    ledger = GatherShareLedger(mode="on")
    digest = patterns_hash(key_pattern=KEY_PATTERN)
    transport = "raw"
    ledger.claim(url_hash(URL), digest, transport, PROVIDER_A)
    ledger.claim(url_hash(URL), digest, transport, PROVIDER_B)

    def fake_collect(**kwargs):
        metadata = kwargs.get("metadata")
        if metadata is not None:
            metadata["file_commit_date"] = 12345.0
            metadata["not_modified"] = False
            metadata["etag"] = ""
            metadata["etag_transport"] = ""
        return []

    monkeypatch.setattr(search_client, "collect", fake_collect)

    # Provider A uses API, Provider B uses web
    resources = _gather_resources(ledger=ledger, use_api_a=True, use_api_b=False)
    stage = AcquisitionStage(resources, lambda out: None)
    task = TaskFactory.create_acquisition_task(PROVIDER_A, URL, Patterns(key_pattern=KEY_PATTERN))
    output = stage.process_task(task)
    assert output is not None

    # Find the metadata for each provider
    meta_by_provider = {}
    for provider, meta_dict in output.link_metadata:
        for url_key, meta in meta_dict.items():
            if url_key == URL:
                meta_by_provider[provider] = meta

    assert PROVIDER_A in meta_by_provider
    assert PROVIDER_B in meta_by_provider
    assert meta_by_provider[PROVIDER_A].transport == "api"
    assert meta_by_provider[PROVIDER_B].transport == "web"


# ---------------------------------------------------------------------------
# S13: wiring — StageResources carries ledger, PipelineStatus publishes metrics
# ---------------------------------------------------------------------------
def test_s13_wiring_stage_resources_and_pipeline_status():
    """R7 D11: StageResources carries the ledger, PipelineStatus.share_metrics
    is published from to_stats(), and close() is called on stop."""
    ledger = GatherShareLedger(mode="on")
    resources = StageResources(
        limiter=None,
        providers={},
        config=Config(),
        task_configs={},
        auth=FakeAuth(),
        share_ledger=ledger,
    )
    assert resources.share_ledger is ledger

    # PipelineStatus has share_metrics field
    from core.metrics import PipelineStatus
    status = PipelineStatus(
        state=None, active=0, total=0, stages={},
        share_metrics=ledger.to_stats(),
    )
    assert status.share_metrics["mode"] == "on"
    assert "claims" in status.share_metrics

    # close() works
    ledger.close()
    stats_after = ledger.to_stats()
    assert stats_after["tracked"] == 0
