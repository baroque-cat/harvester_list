"""C3: SQLite bind-variable chunking for gather-skip and key-ledger lookups.

Traceability: refactor wave-1, defect C3 (BLOCKER).

The SQLite bind-variable limit (upstream 32 766; varies by build) means that
passing >~900 parameters in a single ``WHERE x IN (?)`` clause risks an
``OperationalError`` on some platforms.  Recovery and large batches feed
thousands of URLs/digests into ``decide``/``classify``; the lookup paths must
chunk internally so every executed SELECT carries ≤900 bind parameters.

Pins:
  1. gather_skip.decide with >900 URLs: every executed SELECT has ≤900 binds.
  2. Equivalence: decisions on a large list match chunked-reference decisions.
  3. WARNING emitted when chunking actually splits (>1 chunk).
  4. key_ledger.decide with >900 digests: same contract.
  5. gather_skip.classify is also protected.

No network, no live GitHub.
"""

import os
import sqlite3

from storage.gather_skip import (
    MODE_ON,
    GatherSkipEngine,
)
from storage.key_ledger import (
    KeyCheckRequest,
    KeyLedger,
    key_hash,
)
from storage.registry import Registry, patterns_hash, url_hash

PROVIDER = "openai"
KEY_PATTERN = r"sk-[A-Za-z0-9]{16}"
PATTERNS = patterns_hash(key_pattern=KEY_PATTERN)
NOW = 1_700_000_000.0
HOUR = 3600.0
DAY = 86400.0
TTL_HOURS = 168.0

BASE = "https://github.com/acme/widgets/blob/main"


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


def _gs_engine(workspace: str, mode: str = MODE_ON, **kwargs) -> GatherSkipEngine:
    return GatherSkipEngine(
        workspace=workspace,
        mode=mode,
        ttl_hours=TTL_HOURS,
        registry_path=os.path.join(workspace, "registry.sqlite"),
        run_id="run-c3-test",
        clock=lambda: NOW,
        **kwargs,
    )


def _kl_engine(workspace: str, mode: str = "on", **kwargs) -> KeyLedger:
    return KeyLedger(
        workspace=workspace,
        mode=mode,
        registry_path=os.path.join(workspace, "registry.sqlite"),
        run_id="run-c3-kl-test",
        clock=lambda: NOW,
        **kwargs,
    )


def _seed_links(registry: Registry, urls, *, gathered_ts=NOW - DAY, provider=PROVIDER, patterns=PATTERNS):
    """Seed a batch of URLs as fully-known skippable links."""
    for url in urls:
        registry.record_link(url, transport="web", provider=provider, ts=gathered_ts - 10)
        registry.record_gather(url, provider=provider, patterns_hash=patterns, success=True, ts=gathered_ts)
    registry.flush(10.0)


def _seed_keys(workspace: str, digests, *, status="valid", last_recheck_ts=NOW - HOUR):
    """Directly insert key rows for chunked-ledger tests."""
    conn = sqlite3.connect(os.path.join(workspace, "registry.sqlite"), timeout=5.0)
    try:
        for digest in digests:
            conn.execute(
                "INSERT OR REPLACE INTO keys "
                "(key_hash, provider, key_ref_masked, address, endpoint, status, "
                "first_seen_ts, last_recheck_ts, last_seen_ts, source_url_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (digest, PROVIDER, "…", "", "", status, NOW - DAY, last_recheck_ts, NOW - DAY, ""),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Pin 1: gather_skip.decide — every SELECT carries ≤900 bind parameters
# ---------------------------------------------------------------------------
def test_gather_skip_decide_chunks_binds(workspace):
    """With >900 URLs every executed SELECT must have ≤900 bind parameters."""
    urls = [f"{BASE}/chunk{i}.py" for i in range(1050)]
    registry = _registry(workspace)
    try:
        _seed_links(registry, urls)
    finally:
        registry.stop()

    engine = _gs_engine(workspace)
    # Spy on the connection's execute to count bind params.
    original_connect = engine._connect
    bind_sizes = []

    class _SpyConn:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, params=None):
            if params is not None:
                bind_sizes.append(len(params))
            return self._real.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._real, name)

    engine._connect = lambda: _SpyConn(original_connect())
    decisions = engine.decide(urls, provider=PROVIDER, patterns_hash=PATTERNS)

    assert len(decisions) == len(urls)
    assert bind_sizes, "expected at least one execute call"
    for size in bind_sizes:
        assert size <= 900, f"execute had {size} bind params (limit 900)"


# ---------------------------------------------------------------------------
# Pin 2: equivalence — large-list decisions match small-batch decisions
# ---------------------------------------------------------------------------
def test_gather_skip_decide_equivalence(workspace):
    """Decisions on 1050 URLs must match decisions on the same URLs in chunks of 500."""
    urls = [f"{BASE}/equiv{i}.py" for i in range(1050)]
    registry = _registry(workspace)
    try:
        _seed_links(registry, urls)
    finally:
        registry.stop()

    engine_full = _gs_engine(workspace)
    full_decisions = engine_full.decide(urls, provider=PROVIDER, patterns_hash=PATTERNS)

    engine_chunked = _gs_engine(workspace)
    chunked_decisions = []
    chunk_size = 500
    for start in range(0, len(urls), chunk_size):
        chunk = urls[start : start + chunk_size]
        chunked_decisions.extend(
            engine_chunked.decide(chunk, provider=PROVIDER, patterns_hash=PATTERNS)
        )

    assert len(full_decisions) == len(chunked_decisions) == len(urls)
    for full_d, chunk_d in zip(full_decisions, chunked_decisions):
        assert full_d.url == chunk_d.url
        assert full_d.skip == chunk_d.skip
        assert full_d.known == chunk_d.known
        assert full_d.reason == chunk_d.reason


# ---------------------------------------------------------------------------
# Pin 3: WARNING when chunking actually splits (>1 chunk)
# ---------------------------------------------------------------------------
def test_gather_skip_warns_on_chunking(workspace):
    """A WARNING is emitted when the lookup requires more than one chunk."""
    urls = [f"{BASE}/warn{i}.py" for i in range(1050)]
    registry = _registry(workspace)
    try:
        _seed_links(registry, urls)
    finally:
        registry.stop()

    engine = _gs_engine(workspace)
    engine.decide(urls, provider=PROVIDER, patterns_hash=PATTERNS)

    # The engine should have recorded that chunking occurred (via a counter or flag).
    # We check that the chunk-warning counter is > 0.
    assert getattr(engine, "chunk_warnings", 0) >= 1 or getattr(engine, "chunked_lookups", 0) >= 1


# ---------------------------------------------------------------------------
# Pin 4: key_ledger.decide — same chunking contract
# ---------------------------------------------------------------------------
def test_key_ledger_decide_chunks_binds(workspace):
    """With >900 digests every executed SELECT must have ≤900 bind parameters."""
    digests = [key_hash(PROVIDER, f"key-{i}", "", "") for i in range(1050)]
    # Ensure the registry DB exists with the keys table.
    _ensure_registry_with_keys_table(workspace)
    _seed_keys(workspace, digests)

    ledger = _kl_engine(workspace)
    bind_sizes = []
    original_connect = ledger._connect

    class _SpyConn:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, params=None):
            if params is not None:
                bind_sizes.append(len(params))
            return self._real.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._real, name)

    ledger._connect = lambda: _SpyConn(original_connect())
    requests = [KeyCheckRequest(provider=PROVIDER, key=f"key-{i}") for i in range(1050)]
    decisions = ledger.decide(requests)

    assert len(decisions) == len(requests)
    assert bind_sizes, "expected at least one execute call"
    for size in bind_sizes:
        assert size <= 900, f"execute had {size} bind params (limit 900)"


# ---------------------------------------------------------------------------
# Pin 5: gather_skip.classify is also protected
# ---------------------------------------------------------------------------
def test_gather_skip_classify_chunks_binds(workspace):
    """classify with >900 URLs must also chunk to ≤900 binds per SELECT."""
    urls = [f"{BASE}/class{i}.py" for i in range(1050)]
    registry = _registry(workspace)
    try:
        _seed_links(registry, urls)
    finally:
        registry.stop()

    engine = _gs_engine(workspace)
    bind_sizes = []
    original_connect = engine._connect

    class _SpyConn:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, params=None):
            if params is not None:
                bind_sizes.append(len(params))
            return self._real.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._real, name)

    engine._connect = lambda: _SpyConn(original_connect())
    decisions = engine.classify(urls, provider=PROVIDER, patterns_hash=PATTERNS)

    assert len(decisions) == len(urls)
    assert bind_sizes, "expected at least one execute call"
    for size in bind_sizes:
        assert size <= 900, f"execute had {size} bind params (limit 900)"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _ensure_registry_with_keys_table(workspace: str):
    """Create a minimal registry.sqlite with the keys table schema."""
    path = os.path.join(workspace, "registry.sqlite")
    if os.path.exists(path):
        return
    conn = sqlite3.connect(path, timeout=5.0)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS keys ("
            "key_hash TEXT PRIMARY KEY, "
            "provider TEXT, "
            "key_ref_masked TEXT, "
            "address TEXT, "
            "endpoint TEXT, "
            "status TEXT, "
            "first_seen_ts REAL, "
            "last_recheck_ts REAL, "
            "last_seen_ts REAL, "
            "source_url_hash TEXT)"
        )
        conn.commit()
    finally:
        conn.close()
