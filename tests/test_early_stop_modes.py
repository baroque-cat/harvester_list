"""Transport exclusion, trust gate, kill-switch and shadow instrumentation.

Traceability: search-early-stop-S5, S7..S10.

Harness: the SearchStage pagination entry is invoked with a mocked search client
returning canned pages per transport; the registry is manipulated (row counts,
degraded flag, meta marker); the shared JSONL decision log is asserted.  No
network, no live GitHub.  The clock is frozen.
"""

import json
import os
import sqlite3

from config.schemas import Config, StageConfig, TaskConfig
from core.models import Patterns, SearchTask
from search import client as search_client
from stage.base import StageResources
from stage.definition import SearchStage
from storage.early_stop import MODE_ON, MODE_SHADOW, EarlyStopEngine
from storage.registry import Registry, patterns_hash

PROVIDER = "openai"
KEY_PATTERN = r"sk-[A-Za-z0-9]{16}"
PATTERNS = patterns_hash(key_pattern=KEY_PATTERN)

NOW = 1_700_000_000.0
HOUR = 3600.0
DAY = 24 * HOUR
TTL_HOURS = 168.0
BASE = "https://github.com/acme/widgets/blob/main"


class FakeAuth:
    def get_session(self):
        return "session-token"

    def get_token(self):
        return "api-token"

    def get_user_agent(self):
        return "test-agent"


class _FastRegistryConfig:
    enabled = True
    batch_size = 50
    flush_interval = 0.05
    queue_size = 100000
    path = ""


def _registry(workspace: str) -> Registry:
    registry = Registry(workspace, config=_FastRegistryConfig(), enabled=True)
    assert registry.start() is True
    return registry


def _seed_many(registry: Registry, urls, *, gathered_ts: float = NOW - DAY) -> None:
    for url in urls:
        registry.record_link(url, transport="api", provider=PROVIDER, ts=min(gathered_ts, NOW - DAY) - 10)
        registry.record_gather(url, provider=PROVIDER, patterns_hash=PATTERNS, success=True, ts=gathered_ts)
    registry.flush(10.0)


def _mark_migration(workspace: str) -> None:
    conn = sqlite3.connect(os.path.join(workspace, "registry.sqlite"))
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('migration_complete', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(NOW),),
        )
        conn.commit()
    finally:
        conn.close()


def _engine(workspace: str, mode: str = MODE_SHADOW, **kwargs) -> EarlyStopEngine:
    degraded_probe = kwargs.pop("degraded_probe", None)
    return EarlyStopEngine(
        workspace=workspace,
        mode=mode,
        window=kwargs.pop("window", 10),
        theta=kwargs.pop("theta", 0.9),
        min_pages=kwargs.pop("min_pages", 2),
        min_trust=kwargs.pop("min_trust", 0),
        ttl_hours=TTL_HOURS,
        registry_path=os.path.join(workspace, "registry.sqlite"),
        run_id="run-early-stop-modes",
        clock=lambda: NOW,
        degraded_probe=degraded_probe,
        **kwargs,
    )


def _task_config(use_api: bool) -> TaskConfig:
    return TaskConfig(
        name=PROVIDER,
        enabled=True,
        provider_type="openai",
        use_api=use_api,
        stages=StageConfig(search=True, gather=True, check=False, inspect=False),
        patterns=Patterns(key_pattern=KEY_PATTERN),
    )


def _resources(early_stop, *, use_api: bool, registry=None) -> StageResources:
    return StageResources(
        limiter=None,
        providers={},
        config=Config(),
        task_configs={PROVIDER: _task_config(use_api)},
        auth=FakeAuth(),
        registry=registry,
        gather_skip=None,
        early_stop=early_stop,
    )


def _search_tasks(output):
    return [
        (task.page, task.query, task.use_api)
        for task, target in output.new_tasks
        if target == "search" and isinstance(task, SearchTask)
    ]


def _run_web(monkeypatch, early_stop, pages, total):
    monkeypatch.setattr(
        search_client,
        "search_with_count",
        lambda **kwargs: (list(pages.get(kwargs.get("page", 1), [])), total, ""),
    )
    stage = SearchStage(_resources(early_stop, use_api=False), lambda out: None)
    return stage.process_task(
        SearchTask(provider=PROVIDER, query='"sk-"', regex=KEY_PATTERN, page=1, use_api=False)
    )


def _run_api_chain(monkeypatch, early_stop, pages, total):
    """Process the API page chain, returning the page numbers actually fetched."""
    fetched = []

    def fake_with_count(**kwargs):
        page = kwargs.get("page", 1)
        fetched.append(page)
        return list(pages.get(page, [])), total, ""

    def fake_search_code(**kwargs):
        page = kwargs.get("page", 1)
        fetched.append(page)
        return list(pages.get(page, [])), ""

    monkeypatch.setattr(search_client, "search_with_count", fake_with_count)
    monkeypatch.setattr(search_client, "search_code", fake_search_code)

    stage = SearchStage(_resources(early_stop, use_api=True), lambda out: None)
    initial = SearchTask(provider=PROVIDER, query='"sk-"', regex=KEY_PATTERN, page=1, use_api=True)

    queue = [initial]
    guard = 0
    while queue and guard < 50:
        guard += 1
        task = queue.pop(0)
        output = stage.process_task(task)
        if output is None:
            continue
        for candidate, target in output.new_tasks:
            if target == "search" and isinstance(candidate, SearchTask):
                queue.append(candidate)

    return fetched


def _run_api_first_page(monkeypatch, early_stop, pages, total):
    monkeypatch.setattr(
        search_client,
        "search_with_count",
        lambda **kwargs: (list(pages.get(kwargs.get("page", 1), [])), total, ""),
    )
    stage = SearchStage(_resources(early_stop, use_api=True), lambda out: None)
    return stage.process_task(
        SearchTask(provider=PROVIDER, query='"sk-"', regex=KEY_PATTERN, page=1, use_api=True)
    )


def _read_log(workspace: str):
    path = os.path.join(workspace, "registry_decisions.jsonl")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


# ---------------------------------------------------------------------------
# search-early-stop-S5
# ---------------------------------------------------------------------------
def test_s5_web_run_with_flag_on_paginates_unchanged(workspace, monkeypatch):
    pages = {1: [f"{BASE}/w{i}.py" for i in range(3)]}
    baseline = _run_web(monkeypatch, None, pages, 50)

    _mark_migration(workspace)
    engine = _engine(workspace, mode=MODE_ON, window=10, theta=0.9, min_pages=1)
    with_engine = _run_web(monkeypatch, engine, pages, 50)

    assert _search_tasks(baseline) == _search_tasks(with_engine)
    assert _search_tasks(baseline) == [(2, '"sk-"', False), (3, '"sk-"', False)]
    assert engine.to_stats()["evaluations"] == 0


# ---------------------------------------------------------------------------
# Regression: off mode is byte-for-byte the pre-change bulk pagination
# ---------------------------------------------------------------------------
def test_off_mode_api_pagination_is_unchanged(workspace, monkeypatch):
    pages = {1: [f"{BASE}/w{i}.py" for i in range(3)]}
    baseline = _run_api_first_page(monkeypatch, None, pages, 250)

    engine = _engine(workspace, mode="off")
    off = _run_api_first_page(monkeypatch, engine, pages, 250)

    assert _search_tasks(baseline) == _search_tasks(off)
    assert [page for page, _, _ in _search_tasks(off)] == [2, 3]
    assert engine.to_stats()["evaluations"] == 0


# ---------------------------------------------------------------------------
# Task 4.3: an enforced stop emits no further page tasks
# ---------------------------------------------------------------------------
def test_on_mode_stopped_partition_emits_no_further_pages(workspace, monkeypatch):
    known = [f"{BASE}/k{i}.py" for i in range(9)]

    registry = _registry(workspace)
    try:
        _seed_many(registry, known)
    finally:
        registry.stop()
    _mark_migration(workspace)

    engine = _engine(workspace, mode=MODE_ON, window=10, theta=0.9, min_pages=2)
    pages = {
        1: [f"{BASE}/n1-{i}.py" for i in range(10)],
        2: known + [f"{BASE}/n2.py"],
        3: [f"{BASE}/n3-{i}.py" for i in range(10)],
    }

    fetched = _run_api_chain(monkeypatch, engine, pages, total=250)

    assert fetched == [1, 2]
    assert engine.to_stats()["early_stop_fired"] == 1


# ---------------------------------------------------------------------------
# On mode without saturation chains to the cap (R1/S3 at stage level)
# ---------------------------------------------------------------------------
def test_on_mode_unsaturated_stream_chains_to_the_cap(workspace, monkeypatch):
    # Full schema + trust marker but zero links: the trust gate passes while
    # every result stays novel, so the ratio gate alone drives pagination.
    registry = _registry(workspace)
    registry.stop()
    _mark_migration(workspace)

    engine = _engine(workspace, mode=MODE_ON, window=10, theta=0.9, min_pages=2)
    pages = {page: [f"{BASE}/u{page}-{i}.py" for i in range(10)] for page in (1, 2, 3)}

    fetched = _run_api_chain(monkeypatch, engine, pages, total=250)

    assert fetched == [1, 2, 3]  # ceil(250/100) = 3 = chained cap, no stop
    stats = engine.to_stats()
    assert stats["evaluations"] == 3  # detector evaluated every boundary
    assert stats["early_stop_would_fire"] == 0
    assert stats["early_stop_fired"] == 0


# ---------------------------------------------------------------------------
# search-early-stop-S7
# ---------------------------------------------------------------------------
def test_s7_fresh_registry_forces_full_passes(workspace):
    known = [f"{BASE}/k{i}.py" for i in range(5)]

    # (a) row count below min_trust
    registry = _registry(workspace)
    try:
        _seed_many(registry, known)
    finally:
        registry.stop()
    _mark_migration(workspace)

    engine = _engine(workspace, mode=MODE_ON, window=5, theta=0.9, min_pages=1, min_trust=1000)
    result = engine.observe(
        provider=PROVIDER, query='"sk-"', page=1, links=known, patterns_hash=PATTERNS, max_pages=3
    )
    assert result.ratio == 1.0
    assert result.trust_ok is False
    assert result.would_stop is False
    assert result.stopped is False

    # (b) migration marker missing
    other = os.path.join(workspace, "other")
    os.makedirs(other, exist_ok=True)
    registry = _registry(other)
    try:
        _seed_many(registry, known)
    finally:
        registry.stop()

    engine2 = _engine(other, mode=MODE_ON, window=5, theta=0.9, min_pages=1, min_trust=1)
    result2 = engine2.observe(
        provider=PROVIDER, query='"sk-"', page=1, links=known, patterns_hash=PATTERNS, max_pages=3
    )
    assert result2.trust_ok is False
    assert result2.would_stop is False
    assert result2.stopped is False


# ---------------------------------------------------------------------------
# search-early-stop-S8
# ---------------------------------------------------------------------------
def test_s8_mid_run_error_mutes_stopping(workspace):
    known = [f"{BASE}/k{i}.py" for i in range(5)]

    registry = _registry(workspace)
    try:
        _seed_many(registry, known)
    finally:
        registry.stop()
    _mark_migration(workspace)

    state = {"degraded": False}
    engine = _engine(
        workspace,
        mode=MODE_ON,
        window=5,
        theta=0.9,
        min_pages=1,
        min_trust=0,
        degraded_probe=lambda: state["degraded"],
    )

    first = engine.observe(
        provider=PROVIDER, query='"sk-a"', page=1, links=known, patterns_hash=PATTERNS, max_pages=3
    )
    assert first.stopped is True

    state["degraded"] = True
    second = engine.observe(
        provider=PROVIDER, query='"sk-b"', page=1, links=known, patterns_hash=PATTERNS, max_pages=3
    )
    assert second.trust_ok is False
    assert second.would_stop is False
    assert second.stopped is False


# ---------------------------------------------------------------------------
# Kill-switch: a registry read error muting stopping (spec R5/S8)
# ---------------------------------------------------------------------------
def test_read_error_kill_switch_mutes_stopping(workspace, monkeypatch):
    known = [f"{BASE}/k{i}.py" for i in range(5)]

    registry = _registry(workspace)
    try:
        _seed_many(registry, known)
    finally:
        registry.stop()
    _mark_migration(workspace)

    engine = _engine(workspace, mode=MODE_ON, window=5, theta=0.9, min_pages=1, min_trust=0)
    real_lookup = engine._skip._lookup

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    # A registry read error occurs first...
    monkeypatch.setattr(engine._skip, "_lookup", boom)
    engine.observe(
        provider=PROVIDER, query='"sk-a"', page=1, links=known, patterns_hash=PATTERNS, max_pages=3
    )
    assert engine._skip.has_read_errors() is True

    # ...and keeps stopping muted even after reads recover.
    monkeypatch.setattr(engine._skip, "_lookup", real_lookup)
    result = engine.observe(
        provider=PROVIDER, query='"sk-b"', page=1, links=known, patterns_hash=PATTERNS, max_pages=3
    )
    assert result.ratio == 1.0
    assert result.trust_ok is False
    assert result.would_stop is False
    assert result.stopped is False


# ---------------------------------------------------------------------------
# search-early-stop-S9
# ---------------------------------------------------------------------------
def test_s9_shadow_logs_but_never_acts(workspace, monkeypatch):
    known = [f"{BASE}/k{i}.py" for i in range(9)]

    registry = _registry(workspace)
    try:
        _seed_many(registry, known)
    finally:
        registry.stop()
    _mark_migration(workspace)

    engine = _engine(workspace, mode=MODE_SHADOW, window=10, theta=0.9, min_pages=2)
    pages = {
        1: [f"{BASE}/n1-{i}.py" for i in range(10)],
        2: known + [f"{BASE}/n2.py"],
        3: [f"{BASE}/n3-{i}.py" for i in range(10)],
    }

    fetched = _run_api_chain(monkeypatch, engine, pages, total=250)
    assert set(fetched) >= {1, 2, 3}

    entries = [entry for entry in _read_log(workspace) if entry.get("type") == "early_stop"]
    record = next(entry for entry in entries if entry.get("page") == 2)
    assert record["query"] == '"sk-"'
    assert record["decision"] == "would_stop"

    stats = engine.to_stats()
    assert stats["early_stop_would_fire"] == 1
    assert stats["early_stop_fired"] == 0


# ---------------------------------------------------------------------------
# search-early-stop-S10
# ---------------------------------------------------------------------------
def test_s10_false_stop_price_is_measured(workspace, monkeypatch):
    page2_known = [f"{BASE}/k{i}.py" for i in range(9)]
    page3_known = [f"{BASE}/j{i}.py" for i in range(6)]

    registry = _registry(workspace)
    try:
        _seed_many(registry, page2_known + page3_known)
    finally:
        registry.stop()
    _mark_migration(workspace)

    engine = _engine(workspace, mode=MODE_SHADOW, window=10, theta=0.9, min_pages=2)
    pages = {
        1: [f"{BASE}/n1-{i}.py" for i in range(10)],
        2: page2_known + [f"{BASE}/extra.py"],
        3: page3_known + [f"{BASE}/new{i}.py" for i in range(4)],
    }

    _run_api_chain(monkeypatch, engine, pages, total=250)

    assert engine.to_stats()["novel_after_stop"] == 4
