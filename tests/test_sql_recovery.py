"""SQL-recovery — acquisition-task recovery from registry instead of shards.

Traceability: D-03.4, F-14, F-17 (ред. Д14), OPEN-14, slice 3a.3.

The feature: when ``recovery.source`` is ``registry`` or ``auto`` (with a live
registry), acquisition URLs are streamed from the link registry via
``Registry.fetch_recoverable_urls`` instead of aggregated from shards.  The
stream is freshest-first (ORDER BY last_seen_ts DESC), chunked (≤1000 per
fetch), keyset-paginated (tie-breaker on url_hash), and supports a per-boot
cap with continuation marker in the ``meta`` table.

Fail-CLOSED (Д14): when registry is enabled but dead, RegistryUnavailableError
propagates — no shard-fallback.  When registry is legitimately disabled, auto
falls back to shards (neutral default).

P26 stack (skip decide → share claim) is preserved on top of the registry
URL list — the same ``_recover_acquisition_tasks`` method processes URLs from
either source.
"""

import logging
import os
import sqlite3

import pytest

from core.enums import PipelineStage
from core.exceptions import RegistryUnavailableError
from core.models import Patterns
from manager.gather_share import GatherShareLedger
from manager.recovery import TaskRecoveryManager
from storage.gather_skip import MODE_OFF, MODE_ON, GatherSkipEngine
from storage.registry import Registry, patterns_hash, url_hash

import manager.recovery as mr

PROV_A = "prov-a"
PROV_B = "prov-b"
KEY_PATTERN = r"sk-[A-Za-z0-9]{16}"
DIGEST = patterns_hash(key_pattern=KEY_PATTERN)

NOW = 1_700_000_000.0
HOUR = 3600.0
DAY = 24 * HOUR

BASE = "https://github.com/acme/widgets/blob/main"


def _url(i: int) -> str:
    return f"{BASE}/file{i}.txt"


class _FastRegistryConfig:
    enabled = True
    batch_size = 1
    flush_interval = 0.05
    queue_size = 100000
    path = ""


class _RecoveryCfg:
    def __init__(self, source="shards", per_boot_cap=0):
        self.source = source
        self.per_boot_cap = per_boot_cap


class _GatherCfg:
    transport = "raw"


class _PipelineCfg:
    def __init__(self, recovery=None):
        self.gather = _GatherCfg()
        self.recovery = recovery or _RecoveryCfg()


class FakeStage:
    def __init__(self):
        self.tasks = []

    def put_task(self, task):
        self.tasks.append(task)
        return True

    @property
    def urls(self):
        return [t.url for t in self.tasks]


class FakeProvider:
    def __init__(self, key_pattern=KEY_PATTERN):
        self._patterns = Patterns(key_pattern=key_pattern)

    def get_patterns(self):
        return self._patterns


class FakePipeline:
    def __init__(self, stage, registry=None, recovery_cfg=None, gather_skip=None, share_ledger=None):
        self._stage = stage
        # D1 fix: use link_registry to match real Pipeline attribute name.
        self.link_registry = registry
        self.config = _PipelineCfg(recovery=recovery_cfg)
        self.gather_skip = gather_skip
        self.share_ledger = share_ledger

    def get_stage(self, name):
        return self._stage if name == PipelineStage.GATHER.value else None


class FakeTaskConfig:
    def __init__(self, name):
        self.name = name
        self.stages = FakeStageConfig()


class FakeStageConfig:
    search = True
    gather = True
    check = True
    inspect = True


@pytest.fixture()
def registry(tmp_path):
    reg = Registry(str(tmp_path), config=_FastRegistryConfig(), enabled=True)
    assert reg.start() is True
    yield reg
    try:
        reg.stop()
    except Exception:
        pass


def _seed(registry, url, *, status="discovered", ts=None, provider=PROV_A):
    """Seed a link row with a specific last_seen_ts and visit_status."""
    ts = NOW if ts is None else ts
    registry.record_link(url, transport="web", provider=provider, ts=ts)
    if status == "failed":
        registry.record_gather(url, provider=provider, patterns_hash=DIGEST, success=False, ts=ts + 1)
    registry.flush(10.0)


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)

    @property
    def messages(self):
        return [r.getMessage() for r in self.records]


@pytest.fixture()
def cap():
    handler = _Capture()
    mr.logger.addHandler(handler)
    old_level = mr.logger.level
    mr.logger.setLevel(logging.INFO)
    yield handler
    mr.logger.removeHandler(handler)
    mr.logger.setLevel(old_level)


def _mgr(pipeline, providers=(PROV_A,)):
    return TaskRecoveryManager(pipeline, {p: FakeProvider() for p in providers})


# ---------------------------------------------------------------------------
# SR1 — Streaming: >1000 rows ⇒ multiple fetch chunks ≤1000
# ---------------------------------------------------------------------------
def test_sr1_streaming_chunks_at_1000(registry):
    """More than 1000 recoverable rows triggers multiple fetch chunks."""
    n = 2500
    for i in range(n):
        _seed(registry, _url(i), ts=NOW - i)

    urls = list(registry.fetch_recoverable_urls(chunk_size=1000))
    assert len(urls) == n
    # Verify ordering is DESC by ts.
    timestamps = [ts for _, _, ts in urls]
    assert timestamps == sorted(timestamps, reverse=True)


# ---------------------------------------------------------------------------
# SR2 — Freshest-first: ORDER BY last_seen_ts DESC strict
# ---------------------------------------------------------------------------
def test_sr2_freshest_first_ordering(registry):
    """Rows with different last_seen_ts are returned in strict DESC order."""
    _seed(registry, _url(1), ts=NOW - 100)
    _seed(registry, _url(2), ts=NOW - 10)
    _seed(registry, _url(3), ts=NOW - 50)

    urls = list(registry.fetch_recoverable_urls())
    assert len(urls) == 3
    # Freshest first: url(2) at NOW-10, url(3) at NOW-50, url(1) at NOW-100.
    assert urls[0][0] == _url(2)
    assert urls[1][0] == _url(3)
    assert urls[2][0] == _url(1)


# ---------------------------------------------------------------------------
# SR3 — Cap+marker: cap=K, N>K ⇒ K freshest taken, composite marker written
# ---------------------------------------------------------------------------
def test_sr3_cap_and_marker(registry):
    """Per-boot cap truncates to K freshest; composite marker is written."""
    n = 10
    for i in range(n):
        _seed(registry, _url(i), ts=NOW - i)

    # Cap at 3 — should take the 3 freshest.
    urls = list(registry.fetch_recoverable_urls(limit=3))
    assert len(urls) == 3
    # Freshest first.
    assert urls[0][0] == _url(0)
    assert urls[1][0] == _url(1)
    assert urls[2][0] == _url(2)

    # Write a composite marker manually (simulating what the recovery mgr does).
    last_ts = urls[-1][2]
    last_hash = urls[-1][1]
    registry.set_meta("recovery_cursor:prov-a", f"{last_ts}|{last_hash}")

    # Verify marker is readable.
    marker = registry.get_meta("recovery_cursor:prov-a")
    assert marker is not None
    assert "|" in marker
    parts = marker.split("|", 1)
    assert float(parts[0]) == last_ts
    assert parts[1] == last_hash


# ---------------------------------------------------------------------------
# SR4 — Continuation: second boot continues STRICTLY below composite cursor
# ---------------------------------------------------------------------------
def test_sr4_continuation_resumes_below_cursor(registry):
    """Second boot with composite marker continues strictly below the cursor."""
    n = 10
    for i in range(n):
        _seed(registry, _url(i), ts=NOW - i)

    # First boot: take 3.
    first_batch = list(registry.fetch_recoverable_urls(limit=3))
    assert len(first_batch) == 3
    cursor_ts = first_batch[-1][2]
    cursor_hash = first_batch[-1][1]

    # Second boot: continue from composite cursor.
    second_batch = list(registry.fetch_recoverable_urls(
        before_ts=cursor_ts, before_hash=cursor_hash
    ))
    assert len(second_batch) == 7  # remaining 7

    # No overlap: second batch starts below cursor.
    first_urls = {u[0] for u in first_batch}
    second_urls = {u[0] for u in second_batch}
    assert first_urls.isdisjoint(second_urls)

    # Combined = all 10.
    combined = first_urls | second_urls
    assert len(combined) == n


# ---------------------------------------------------------------------------
# SR5 — Composite cursor at equal-ts boundary: zero loss, zero dupes (D2 fix)
# ---------------------------------------------------------------------------
def test_sr5_composite_cursor_at_equal_ts_boundary(registry):
    """D2 fix: composite cursor at equal-ts boundary loses nothing, duplicates nothing."""
    same_ts = NOW - 42
    n = 5
    for i in range(n):
        _seed(registry, _url(i), ts=same_ts)

    # Take 2 (mid-group truncation).
    first = list(registry.fetch_recoverable_urls(limit=2))
    assert len(first) == 2
    cursor_ts = first[-1][2]
    cursor_hash = first[-1][1]

    # Continue with composite cursor.
    second = list(registry.fetch_recoverable_urls(
        before_ts=cursor_ts, before_hash=cursor_hash
    ))
    # Should get the remaining 3 (zero loss).
    assert len(second) == 3

    # No overlap (zero dupes).
    first_hashes = {u[1] for u in first}
    second_hashes = {u[1] for u in second}
    assert first_hashes.isdisjoint(second_hashes)

    # Combined = all 5.
    combined = first_hashes | second_hashes
    assert len(combined) == n


# ---------------------------------------------------------------------------
# SR6 — Seen-set dedup by url_hash
# ---------------------------------------------------------------------------
def test_sr6_seen_set_dedup(registry):
    """Duplicate url_hash rows do not create two tasks."""
    # Seed the same URL twice (second seed updates last_seen_ts).
    _seed(registry, _url(1), ts=NOW - 100)
    _seed(registry, _url(1), ts=NOW - 50)  # update

    urls = list(registry.fetch_recoverable_urls())
    # Only one row per url_hash.
    hashes = [uh for _, uh, _ in urls]
    assert len(hashes) == len(set(hashes))
    assert len(urls) == 1


# ---------------------------------------------------------------------------
# SR7 — Tri-mode: shards (default) ⇒ reader NOT called
# ---------------------------------------------------------------------------
def test_sr7_shards_default_no_reader_call(registry, cap, monkeypatch):
    """source=shards (default) does not call the registry reader."""
    _seed(registry, _url(1))
    stage = FakeStage()
    pipe = FakePipeline(stage, registry=registry, recovery_cfg=_RecoveryCfg(source="shards"))

    # Spy on fetch_recoverable_urls.
    called = []
    monkeypatch.setattr(registry, "fetch_recoverable_urls", lambda **kw: called.append(1) or iter([]))

    mgr = _mgr(pipe)
    mgr._recover_acquisition_tasks(PROV_A, [_url(1)])

    assert called == [], "reader must not be called in shards mode"
    assert stage.urls == [_url(1)]


# ---------------------------------------------------------------------------
# SR8 — Tri-mode: registry ⇒ SQL path + recovered_from_registry token
# ---------------------------------------------------------------------------
def test_sr8_registry_source_uses_sql(registry, cap):
    """source=registry uses the SQL reader and publishes recovered_from_registry."""
    _seed(registry, _url(1), ts=NOW - 10, status="discovered")
    _seed(registry, _url(2), ts=NOW - 20, status="failed")

    stage = FakeStage()
    pipe = FakePipeline(stage, registry=registry, recovery_cfg=_RecoveryCfg(source="registry"))
    pipe.task_configs = {PROV_A: FakeTaskConfig(PROV_A)}

    mgr = _mgr(pipe)
    # Simulate the full recovery path.
    from core.models import RecoveredTasks
    tasks = RecoveredTasks()
    tasks.acquisition = []  # empty shard list — registry provides URLs
    mgr._recover_provider_tasks(PROV_A, tasks)

    # URLs came from registry.
    assert len(stage.urls) == 2
    # recovered_from_registry token published.
    assert any("recovered_from_registry=2" in m for m in cap.messages)


# ---------------------------------------------------------------------------
# SR9 — Tri-mode: auto+enabled-live ⇒ SQL path
# ---------------------------------------------------------------------------
def test_sr9_auto_enabled_live_uses_sql(registry, cap):
    """source=auto with enabled+available registry uses SQL path."""
    _seed(registry, _url(1), ts=NOW - 10)

    stage = FakeStage()
    pipe = FakePipeline(stage, registry=registry, recovery_cfg=_RecoveryCfg(source="auto"))
    pipe.task_configs = {PROV_A: FakeTaskConfig(PROV_A)}

    mgr = _mgr(pipe)
    from core.models import RecoveredTasks
    tasks = RecoveredTasks()
    tasks.acquisition = []
    mgr._recover_provider_tasks(PROV_A, tasks)

    assert len(stage.urls) == 1
    assert any("recovered_from_registry=1" in m for m in cap.messages)


# ---------------------------------------------------------------------------
# SR10 — Tri-mode: auto+disabled ⇒ shards path
# ---------------------------------------------------------------------------
def test_sr10_auto_disabled_falls_back_to_shards(cap):
    """source=auto with registry disabled falls back to shards."""
    stage = FakeStage()

    # Create a disabled registry.
    disabled_reg = Registry(".", config=_FastRegistryConfig(), enabled=False)

    pipe = FakePipeline(stage, registry=disabled_reg, recovery_cfg=_RecoveryCfg(source="auto"))
    mgr = _mgr(pipe)
    # Pass source_label="shard" to match the legacy path.
    mgr._recover_acquisition_tasks(PROV_A, [_url(10)], source_label="shard")

    assert stage.urls == [_url(10)]
    # No recovered_from_registry token.
    assert not any("recovered_from_registry" in m for m in cap.messages)


# ---------------------------------------------------------------------------
# SR11 — Tri-mode: registry+dead DB ⇒ RegistryUnavailableError propagates
# ---------------------------------------------------------------------------
def test_sr11_registry_dead_db_crashes(registry, monkeypatch):
    """source=registry with dead DB propagates RegistryUnavailableError."""
    stage = FakeStage()
    pipe = FakePipeline(stage, registry=registry, recovery_cfg=_RecoveryCfg(source="registry"))
    pipe.task_configs = {PROV_A: FakeTaskConfig(PROV_A)}

    # Simulate a fatal DB error.
    registry._fatal = sqlite3.DatabaseError("corrupt")

    mgr = _mgr(pipe)
    from core.models import RecoveredTasks
    tasks = RecoveredTasks()
    tasks.acquisition = []

    with pytest.raises(RegistryUnavailableError):
        mgr._recover_provider_tasks(PROV_A, tasks)


# ---------------------------------------------------------------------------
# SR12 — Config validation: invalid source raises ValueError
# ---------------------------------------------------------------------------
def test_sr12_config_invalid_source():
    """Invalid recovery.source raises ValueError."""
    from config.schemas import RecoveryConfig
    with pytest.raises(ValueError, match="recovery.source must be one of"):
        RecoveryConfig(source="invalid")


# ---------------------------------------------------------------------------
# SR13 — Config validation: negative cap raises ValueError
# ---------------------------------------------------------------------------
def test_sr13_config_negative_cap():
    """Negative per_boot_cap raises ValueError."""
    from config.schemas import RecoveryConfig
    with pytest.raises(ValueError, match="recovery.per_boot_cap must be a non-negative"):
        RecoveryConfig(per_boot_cap=-1)


# ---------------------------------------------------------------------------
# SR14 — Neutrality: default config ⇒ byte-identical shard path
# ---------------------------------------------------------------------------
def test_sr14_neutrality_default_config(registry, cap):
    """Default config (source=shards) produces byte-identical shard path."""
    stage = FakeStage()
    pipe = FakePipeline(stage, registry=registry)  # no recovery_cfg = default
    mgr = _mgr(pipe)
    mgr._recover_acquisition_tasks(PROV_A, [_url(1), _url(2)])

    assert stage.urls == [_url(1), _url(2)]
    # Legacy log line intact.
    hits = [m for m in cap.messages if "acquisition tasks for" in m]
    assert len(hits) == 1
    assert "shard urls 2" in hits[0]
    assert "Recovered 2 acquisition tasks for prov-a" in hits[0]


# ---------------------------------------------------------------------------
# SR15 — P26 stack preserved on registry source (code path invoked)
# ---------------------------------------------------------------------------
def test_sr15_p26_stack_preserved_on_registry(tmp_path, registry, cap):
    """P26 skip/share code path is invoked for registry-sourced URLs."""
    # Seed with "discovered" status (returned by reader).
    _seed(registry, _url(1), ts=NOW - 10, status="discovered")

    stage = FakeStage()
    engine = GatherSkipEngine(
        workspace=str(tmp_path),
        mode=MODE_ON,
        ttl_hours=168.0,
        registry_path=os.path.join(str(tmp_path), "registry.sqlite"),
        run_id="sr15-test",
        clock=lambda: NOW,
    )
    pipe = FakePipeline(
        stage, registry=registry,
        recovery_cfg=_RecoveryCfg(source="registry"),
        gather_skip=engine,
    )
    pipe.task_configs = {PROV_A: FakeTaskConfig(PROV_A)}

    mgr = _mgr(pipe)
    from core.models import RecoveredTasks
    tasks = RecoveredTasks()
    tasks.acquisition = []
    mgr._recover_provider_tasks(PROV_A, tasks)

    # discovered status ⇒ not suppressed (not gathered_ok).
    assert len(stage.urls) == 1
    line = [m for m in cap.messages if "acquisition tasks for" in m]
    assert len(line) == 1
    assert "registry urls 1" in line[0]
    # P26 code path was invoked (skip-suppressed 0 because URL is discovered, not gathered_ok).
    assert "skip-suppressed 0" in line[0]


# ---------------------------------------------------------------------------
# SR16 — Wiring pin: config value reaches recovery manager via Pipeline
# ---------------------------------------------------------------------------
def test_sr16_wiring_config_reaches_recovery(registry, cap, monkeypatch):
    """Config value reaches the recovery manager (behavioral wiring pin)."""
    _seed(registry, _url(1), ts=NOW - 10)
    _seed(registry, _url(2), ts=NOW - 20)
    _seed(registry, _url(3), ts=NOW - 30)

    stage = FakeStage()
    pipe = FakePipeline(
        stage, registry=registry,
        recovery_cfg=_RecoveryCfg(source="registry", per_boot_cap=2),
    )
    pipe.task_configs = {PROV_A: FakeTaskConfig(PROV_A)}

    mgr = _mgr(pipe)
    from core.models import RecoveredTasks
    tasks = RecoveredTasks()
    tasks.acquisition = []
    mgr._recover_provider_tasks(PROV_A, tasks)

    # Cap=2 ⇒ only 2 URLs taken.
    assert len(stage.urls) == 2
    # Truncation logged.
    assert any("recovery_truncated" in m for m in cap.messages)
    # Per-provider composite marker written (D3 fix).
    marker = registry.get_meta("recovery_cursor:prov-a")
    assert marker is not None
    assert "|" in marker  # composite format


# ---------------------------------------------------------------------------
# SR17 — Full pass removes per-provider marker
# ---------------------------------------------------------------------------
def test_sr17_full_pass_removes_marker(registry):
    """When all rows are consumed (no truncation), per-provider marker is removed."""
    _seed(registry, _url(1), ts=NOW - 10)
    _seed(registry, _url(2), ts=NOW - 20)

    # Pre-set a stale per-provider marker.
    registry.set_meta("recovery_cursor:prov-a", f"{NOW}|abc123")

    stage = FakeStage()
    pipe = FakePipeline(
        stage, registry=registry,
        recovery_cfg=_RecoveryCfg(source="registry", per_boot_cap=0),  # unlimited
    )
    pipe.task_configs = {PROV_A: FakeTaskConfig(PROV_A)}

    mgr = _mgr(pipe)
    from core.models import RecoveredTasks
    tasks = RecoveredTasks()
    tasks.acquisition = []
    mgr._recover_provider_tasks(PROV_A, tasks)

    # Full pass ⇒ per-provider marker removed.
    marker = registry.get_meta("recovery_cursor:prov-a")
    assert marker is None


# ---------------------------------------------------------------------------
# SR18 — Per-provider isolation: provider A's marker doesn't affect B (D3 fix)
# ---------------------------------------------------------------------------
def test_sr18_per_provider_marker_isolation(registry, cap):
    """D3 fix: each provider has its own cursor; A's truncation doesn't affect B."""
    # Seed URLs for both providers.
    for i in range(5):
        _seed(registry, _url(i), ts=NOW - i, provider=PROV_A)
    for i in range(5, 10):
        _seed(registry, _url(i), ts=NOW - i, provider=PROV_B)

    stage = FakeStage()
    pipe = FakePipeline(
        stage, registry=registry,
        recovery_cfg=_RecoveryCfg(source="registry", per_boot_cap=2),
    )
    pipe.task_configs = {
        PROV_A: FakeTaskConfig(PROV_A),
        PROV_B: FakeTaskConfig(PROV_B),
    }

    mgr = _mgr(pipe, providers=(PROV_A, PROV_B))

    # Recover provider A (truncates at 2).
    from core.models import RecoveredTasks
    tasks_a = RecoveredTasks()
    tasks_a.acquisition = []
    mgr._recover_provider_tasks(PROV_A, tasks_a)

    # Provider A has its own marker.
    marker_a = registry.get_meta("recovery_cursor:prov-a")
    assert marker_a is not None

    # Provider B has NO marker yet.
    marker_b = registry.get_meta("recovery_cursor:prov-b")
    assert marker_b is None

    # Recover provider B (also truncates at 2, but independently).
    tasks_b = RecoveredTasks()
    tasks_b.acquisition = []
    mgr._recover_provider_tasks(PROV_B, tasks_b)

    # Now provider B has its own marker.
    marker_b = registry.get_meta("recovery_cursor:prov-b")
    assert marker_b is not None

    # Independence (C-3aG-3 adaptation): with the GLOBAL discovered-set both
    # providers share one ordering space, so marker values may coincide; the
    # guarantee is that A's next boot advances ONLY A's cursor, never B's.
    tasks_a2 = RecoveredTasks()
    tasks_a2.acquisition = []
    mgr._recover_provider_tasks(PROV_A, tasks_a2)
    marker_a2 = registry.get_meta("recovery_cursor:prov-a")
    assert marker_a2 is not None
    assert marker_a2 != marker_a, "A's second boot must advance A's cursor"
    assert registry.get_meta("recovery_cursor:prov-b") == marker_b, (
        "A's second boot must not touch B's cursor"
    )


# SR21 — Global discovered-set: every provider recovers the same URLs (C-3aG-3)
def test_sr21_second_provider_recovers_global_set(registry, cap):
    """links.provider records the FIRST discoverer only (measured live at gate
    3a.G G2, 2026-10-22: all 11784 rows carried the single first-discovering
    provider while per-provider fan-out lives in link_coverage), so filtering
    the recovery SELECT by links.provider starves every other provider.
    Canonical D-03.4 selects the GLOBAL discovered/failed set; each provider
    iterates it and per-provider suppression happens below in the P26 stack
    (skip decide -> share claim)."""
    for i in range(3):
        _seed(registry, _url(i), ts=NOW - i, provider=PROV_A)

    stage = FakeStage()
    pipe = FakePipeline(
        stage, registry=registry,
        recovery_cfg=_RecoveryCfg(source="registry", per_boot_cap=0),
    )
    pipe.task_configs = {
        PROV_A: FakeTaskConfig(PROV_A),
        PROV_B: FakeTaskConfig(PROV_B),
    }
    mgr = _mgr(pipe, providers=(PROV_A, PROV_B))

    urls_b, label_b = mgr._resolve_acquisition_urls(PROV_B, [])
    assert label_b == "registry"
    assert len(urls_b) == 3, (
        f"second provider must see the global set, got {len(urls_b)}"
    )

    urls_a, label_a = mgr._resolve_acquisition_urls(PROV_A, [])
    assert label_a == "registry"
    assert sorted(urls_a) == sorted(urls_b)


# ---------------------------------------------------------------------------
# SR19 — Legacy ts-only marker triggers WARNING (backward compat)
# ---------------------------------------------------------------------------
def test_sr19_legacy_marker_warning(registry, cap):
    """Legacy ts-only marker (no hash) triggers WARNING and continues."""
    _seed(registry, _url(1), ts=NOW - 10)
    _seed(registry, _url(2), ts=NOW - 20)

    # Pre-set a legacy ts-only marker (old format).
    registry.set_meta("recovery_cursor:prov-a", str(NOW - 15))

    stage = FakeStage()
    pipe = FakePipeline(
        stage, registry=registry,
        recovery_cfg=_RecoveryCfg(source="registry"),
    )
    pipe.task_configs = {PROV_A: FakeTaskConfig(PROV_A)}

    mgr = _mgr(pipe)
    from core.models import RecoveredTasks
    tasks = RecoveredTasks()
    tasks.acquisition = []
    mgr._recover_provider_tasks(PROV_A, tasks)

    # WARNING logged about legacy marker.
    assert any("legacy ts-only marker" in m for m in cap.messages)
    # Recovery still proceeds (backward compat).
    assert len(stage.urls) >= 1


# ---------------------------------------------------------------------------
# SR20 — Real Pipeline wiring: link_registry attribute (D4 fix)
# ---------------------------------------------------------------------------
def test_sr20_real_pipeline_wiring(tmp_path, cap):
    """D4 fix: recovery uses real Pipeline.link_registry attribute, not .registry."""
    from config.schemas import Config, RecoveryConfig, RegistryConfig
    from manager.pipeline import Pipeline

    # Create a real config with recovery.source=registry.
    cfg = Config()
    cfg.recovery = RecoveryConfig(source="registry", per_boot_cap=0)
    cfg.registry = RegistryConfig(enabled=True, path=str(tmp_path / "registry.sqlite"))
    cfg.global_config.workspace = str(tmp_path)
    cfg.tasks = []  # no providers

    # Create a real Pipeline (this initializes link_registry).
    pipeline = Pipeline(cfg, providers={})

    # Start the registry.
    pipeline.link_registry.start()

    # Seed some data.
    pipeline.link_registry.record_link(_url(1), provider=PROV_A, ts=NOW - 10)
    pipeline.link_registry.record_link(_url(2), provider=PROV_A, ts=NOW - 20)
    pipeline.link_registry.flush(10.0)

    # Create recovery manager with real pipeline.
    from manager.recovery import TaskRecoveryManager
    mgr = TaskRecoveryManager(pipeline, {})

    # Call _resolve_acquisition_urls — should use pipeline.link_registry.
    urls, source_label = mgr._resolve_acquisition_urls(PROV_A, [])

    # Verify SQL path was used (not shard fallback).
    assert source_label == "registry"
    assert len(urls) == 2

    # Verify publication token.
    assert any("recovered_from_registry=2" in m for m in cap.messages)

    # Cleanup.
    pipeline.link_registry.stop()
