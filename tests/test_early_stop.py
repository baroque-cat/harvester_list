"""Window-tracker and stop-rule tests for early-stop (add-search-early-stop).

Traceability: search-early-stop-S1..S4, S6, S11.

Harness: a tmp-workspace registry is pre-populated through the change-1 writer
API; the early-stop tracker is driven directly with canned page-result
sequences.  No network, no live GitHub.  The clock is frozen so TTL/ratio
comparisons are deterministic.
"""

import os
import sqlite3

from constant.search import API_MAX_PAGES
from storage.early_stop import MODE_ON, EarlyStopEngine
from storage.registry import Registry, patterns_hash

PROVIDER = "openai"
OTHER_PROVIDER = "anthropic"
KEY_PATTERN = r"sk-[A-Za-z0-9]{16}"
PATTERNS = patterns_hash(key_pattern=KEY_PATTERN)
OTHER_PATTERNS = patterns_hash(key_pattern=r"sk-legacy-[A-Za-z0-9]{16}")

NOW = 1_700_000_000.0
HOUR = 3600.0
DAY = 24 * HOUR
TTL_HOURS = 168.0
BASE = "https://github.com/acme/widgets/blob/main"


class _FastRegistryConfig:
    """Registry config that batches writes so seeding stays fast."""

    enabled = True
    batch_size = 50
    flush_interval = 0.05
    queue_size = 100000
    path = ""


def _registry(workspace: str) -> Registry:
    registry = Registry(workspace, config=_FastRegistryConfig(), enabled=True)
    assert registry.start() is True
    return registry


def _seed_many(
    registry: Registry,
    urls,
    *,
    status: str = "gathered_ok",
    gathered_ts: float = NOW - DAY,
    provider: str = PROVIDER,
    patterns: str = PATTERNS,
) -> None:
    """Populate many links through the change-1 writer API in one flush."""
    for url in urls:
        registry.record_link(url, transport="api", provider=provider, ts=min(gathered_ts, NOW - DAY) - 10)
        if status == "gathered_ok":
            registry.record_gather(url, provider=provider, patterns_hash=patterns, success=True, ts=gathered_ts)
    registry.flush(10.0)


def _mark_migration(workspace: str) -> None:
    """Insert the one-time migration marker the trust gate requires."""
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


def _engine(workspace: str, mode: str = MODE_ON, **kwargs) -> EarlyStopEngine:
    skip_engine = kwargs.pop("skip_engine", None)
    degraded_probe = kwargs.pop("degraded_probe", None)
    return EarlyStopEngine(
        workspace=workspace,
        mode=mode,
        window=kwargs.pop("window", 100),
        theta=kwargs.pop("theta", 0.9),
        min_pages=kwargs.pop("min_pages", 2),
        min_trust=kwargs.pop("min_trust", 0),
        ttl_hours=TTL_HOURS,
        registry_path=os.path.join(workspace, "registry.sqlite"),
        run_id="run-early-stop",
        clock=lambda: NOW,
        skip_engine=skip_engine,
        degraded_probe=degraded_probe,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# search-early-stop-S1
# ---------------------------------------------------------------------------
def test_s1_saturated_window_stops_pagination(workspace):
    known = [f"{BASE}/k{i}.py" for i in range(97)]
    novel = [f"{BASE}/n{i}.py" for i in range(3)]

    registry = _registry(workspace)
    try:
        _seed_many(registry, known)
    finally:
        registry.stop()
    _mark_migration(workspace)

    engine = _engine(workspace, window=100, theta=0.9, min_pages=2)

    first = engine.observe(
        provider=PROVIDER,
        query='"sk-"',
        page=1,
        links=known[:50] + novel[:1],
        patterns_hash=PATTERNS,
        max_pages=5,
    )
    assert first.would_stop is False
    assert first.stopped is False

    second = engine.observe(
        provider=PROVIDER,
        query='"sk-"',
        page=2,
        links=known[50:] + novel[1:],
        patterns_hash=PATTERNS,
    )
    assert second.window_size == 100
    assert second.known_count == 97
    assert second.ratio >= 0.9
    assert second.would_stop is True
    assert second.stopped is True
    assert engine.to_stats()["early_stop_fired"] == 1


# ---------------------------------------------------------------------------
# search-early-stop-S2
# ---------------------------------------------------------------------------
def test_s2_never_stops_on_the_first_page(workspace):
    known = [f"{BASE}/k{i}.py" for i in range(100)]

    registry = _registry(workspace)
    try:
        _seed_many(registry, known)
    finally:
        registry.stop()
    _mark_migration(workspace)

    engine = _engine(workspace, window=100, theta=0.9, min_pages=2)
    result = engine.observe(
        provider=PROVIDER,
        query='"sk-"',
        page=1,
        links=known,
        patterns_hash=PATTERNS,
        max_pages=5,
    )

    assert result.ratio == 1.0
    assert result.would_stop is False
    assert result.stopped is False
    assert result.first_stop_page is None


# ---------------------------------------------------------------------------
# search-early-stop-S3
# ---------------------------------------------------------------------------
def test_s3_unsaturated_window_continues_to_the_cap(workspace):
    registry = _registry(workspace)
    registry.stop()
    _mark_migration(workspace)

    engine = _engine(workspace, window=100, theta=0.9, min_pages=2)
    for page in range(1, API_MAX_PAGES + 1):
        links = [f"{BASE}/p{page}-{i}.py" for i in range(3)]
        result = engine.observe(
            provider=PROVIDER,
            query='"sk-"',
            page=page,
            links=links,
            patterns_hash=PATTERNS,
            max_pages=API_MAX_PAGES,
        )
        assert result.would_stop is False
        assert result.stopped is False

    assert engine.to_stats()["early_stop_fired"] == 0


# ---------------------------------------------------------------------------
# search-early-stop-S4
# ---------------------------------------------------------------------------
def test_s4_discovered_only_links_count_as_novel(workspace):
    known = [f"{BASE}/k{i}.py" for i in range(8)]
    discovered = f"{BASE}/disc.py"
    foreign = f"{BASE}/foreign.py"

    registry = _registry(workspace)
    try:
        _seed_many(registry, known)
        _seed_many(registry, [discovered], status="discovered")
        _seed_many(registry, [foreign], provider=OTHER_PROVIDER, patterns=OTHER_PATTERNS)
    finally:
        registry.stop()
    _mark_migration(workspace)

    engine = _engine(workspace, window=10, theta=0.9, min_pages=1)
    result = engine.observe(
        provider=PROVIDER,
        query='"sk-"',
        page=1,
        links=known + [discovered, foreign],
        patterns_hash=PATTERNS,
        max_pages=3,
    )

    assert result.known_count == 8
    assert result.ratio == 0.8
    assert result.would_stop is False


# ---------------------------------------------------------------------------
# search-early-stop-S6
# ---------------------------------------------------------------------------
def test_s6_mixed_partitions_act_independently(workspace):
    known = [f"{BASE}/k{i}.py" for i in range(10)]
    novel = [f"{BASE}/n{i}.py" for i in range(10)]

    registry = _registry(workspace)
    try:
        _seed_many(registry, known)
    finally:
        registry.stop()
    _mark_migration(workspace)

    engine = _engine(workspace, window=10, theta=0.9, min_pages=2)

    a1 = engine.observe(
        provider=PROVIDER, query='"sk-a"', page=1, links=known, patterns_hash=PATTERNS, max_pages=5
    )
    b1 = engine.observe(
        provider=PROVIDER, query='"sk-b"', page=1, links=novel, patterns_hash=PATTERNS, max_pages=5
    )
    assert a1.stopped is False
    assert b1.stopped is False

    a2 = engine.observe(provider=PROVIDER, query='"sk-a"', page=2, links=known, patterns_hash=PATTERNS)
    b2 = engine.observe(provider=PROVIDER, query='"sk-b"', page=2, links=novel, patterns_hash=PATTERNS)

    assert a2.stopped is True
    assert a2.known_count == 10
    assert b2.stopped is False
    assert b2.known_count == 0
    assert engine.to_stats()["early_stop_fired"] == 1


# ---------------------------------------------------------------------------
# search-early-stop-S11
# ---------------------------------------------------------------------------
def test_s11_ttl_expired_links_count_as_novel(workspace):
    fresh = [f"{BASE}/fresh{i}.py" for i in range(8)]
    stale = [f"{BASE}/stale{i}.py" for i in range(2)]

    registry = _registry(workspace)
    try:
        _seed_many(registry, fresh, gathered_ts=NOW - HOUR)
        _seed_many(registry, stale, gathered_ts=NOW - (TTL_HOURS + 1) * HOUR)
    finally:
        registry.stop()
    _mark_migration(workspace)

    engine = _engine(workspace, window=10, theta=0.9, min_pages=1)
    result = engine.observe(
        provider=PROVIDER,
        query='"sk-"',
        page=1,
        links=fresh + stale,
        patterns_hash=PATTERNS,
        max_pages=3,
    )

    assert result.known_count == 8
    assert result.ratio == 0.8
    assert result.would_stop is False
