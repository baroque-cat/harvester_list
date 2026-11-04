"""DEF-1 — recovery reader must use the same patterns_hash domain as writers.

Defect (found live at gate 4.G, 2026-10-23; chronic since R7): two
patterns_hash domains coexist.  Durable WRITERS (_record_gathered /
_record_shared_coverage via _digest_for(task)) key link_coverage by the
AcquisitionTask pattern fields, which the runtime fan-out builds from
``resources.task_configs[B].patterns`` (EMPTY in the combat config ⇒
H_empty domain).  The recovery READER (manager/recovery.py) computed its
digest from ``provider.get_patterns()`` = conditions[0].patterns (real
key_pattern ⇒ H_cfg domain).  H_cfg != H_empty ⇒ covered=∅ ⇒
skip-suppressed=0 at EVERY restart (measured live: W1/W2 arms, gate 4.G;
full backlog re-enqueue, fetch mitigated only by etag 304s).

DEF-2 update (operator directive 2026-11-04): the fan-out now resolves
condition-level patterns when task-level is empty (the combat-shape
default).  Both domains are unified: recovery AND runtime writers now
use H_cfg (condition-level) when task-level patterns are empty.  The
tests below verify the unified domain (H_cfg), not the old H_empty.

Fix (variant A, operator directive 2026-10-27; variant A′ 2026-11-04):
resolve patterns from ``pipeline.task_configs[name].patterns`` when
key_pattern is non-empty; fallback to ``provider.get_patterns()``
(condition-level).  The single resolution point feeds all three
consumers: skip digest, share-claim key, and create_acquisition_task
pattern fields (so recovered tasks write coverage in the SAME domain as
runtime-created ones).

Harness: real Registry + real GatherSkipEngine + real GatherShareLedger
(style of tests/test_p26_recovery_filter.py); fakes only at the
stage/pipeline/provider boundary.

RED predictions on HEAD (verified by running before the fix):
- D1a FAILS: ``assert [] == ['/file0.txt', '/file1.txt']`` shape — both
  URLs enqueued instead of skip-suppressed (foreign digest ⇒ covered=∅).
- D1b PASSES pre-fix (fallback domain == provider domain when the
  pipeline stub has no task_configs) — regression guard.
- D1c FAILS: runtime-twin claim returns False (recovery claimed under
  the foreign digest ⇒ different ledger key ⇒ no suppression).
- D1d FAILS: ``assert 'sk-[A-Za-z0-9]{16}' == ''`` — recovered task
  carries condition patterns instead of task-config patterns.
"""

import logging
import os

import pytest

from core.enums import PipelineStage
from core.models import Patterns
from config.schemas import StageConfig, TaskConfig
from manager.gather_share import GatherShareLedger
from manager.recovery import TaskRecoveryManager
from storage.gather_skip import MODE_OFF, MODE_ON, GatherSkipEngine
from storage.registry import Registry, patterns_hash, url_hash

import manager.recovery as mr

PROV_A = "prov-a"
PROV_B = "prov-b"
# Condition-level pattern (what provider.get_patterns() returns in combat).
COND_PATTERN = r"sk-[A-Za-z0-9]{16}"
# Task-level domain: TaskConfig.patterns is EMPTY in the combat config.
TASK_PATTERNS = Patterns()
COND_PATTERNS = Patterns(key_pattern=COND_PATTERN)
TASK_DIGEST = patterns_hash(
    key_pattern=TASK_PATTERNS.key_pattern,
    address_pattern=TASK_PATTERNS.address_pattern,
    endpoint_pattern=TASK_PATTERNS.endpoint_pattern,
    model_pattern=TASK_PATTERNS.model_pattern,
)
COND_DIGEST = patterns_hash(key_pattern=COND_PATTERN)
assert TASK_DIGEST != COND_DIGEST, "test premise: the two domains differ"

NOW = 1_700_000_000.0
HOUR = 3600.0
DAY = 24 * HOUR
TTL_HOURS = 168.0

BASE = "https://github.com/acme/widgets/blob/main"


def _url(i: int) -> str:
    return f"{BASE}/file{i}.txt"


class _FastRegistryConfig:
    enabled = True
    batch_size = 1
    flush_interval = 0.05
    queue_size = 100000
    path = ""


@pytest.fixture()
def registry(tmp_path):
    reg = Registry(str(tmp_path), config=_FastRegistryConfig(), enabled=True)
    assert reg.start() is True
    yield reg
    stop = getattr(reg, "stop", None)
    if callable(stop):
        try:
            stop()
        except Exception:
            pass


def _seed(registry, url, *, provider=PROV_A, digest):
    """One gathered_ok link fresh inside the TTL, coverage under ``digest``."""
    gathered_ts = NOW - DAY
    registry.record_link(url, transport="web", provider=provider, ts=gathered_ts - 10)
    registry.record_gather(
        url, provider=provider, patterns_hash=digest, success=True, ts=gathered_ts
    )
    registry.flush(10.0)


def _engine(tmp_path, mode):
    return GatherSkipEngine(
        workspace=str(tmp_path),
        mode=mode,
        ttl_hours=TTL_HOURS,
        registry_path=os.path.join(str(tmp_path), "registry.sqlite"),
        run_id="def1-test",
        clock=lambda: NOW,
    )


class FakeStage:
    def __init__(self):
        self.tasks = []

    def put_task(self, task):
        self.tasks.append(task)
        return True

    @property
    def urls(self):
        return [t.url for t in self.tasks]


class _GatherCfg:
    transport = "raw"


class _Cfg:
    gather = _GatherCfg()


class Def1Pipeline:
    """Pipeline stub WITH task_configs (combat shape: empty patterns)."""

    def __init__(self, stage, gather_skip=None, share_ledger=None, task_configs=None):
        self._stage = stage
        self.gather_skip = gather_skip
        self.share_ledger = share_ledger
        self.config = _Cfg()
        if task_configs is not None:
            self.task_configs = task_configs

    def get_stage(self, name):
        return self._stage if name == PipelineStage.GATHER.value else None


class LegacyPipeline:
    """Pipeline stub WITHOUT task_configs (pre-DEF-1 harness shape)."""

    def __init__(self, stage, gather_skip=None, share_ledger=None):
        self._stage = stage
        self.gather_skip = gather_skip
        self.share_ledger = share_ledger
        self.config = _Cfg()

    def get_stage(self, name):
        return self._stage if name == PipelineStage.GATHER.value else None


class FakeProvider:
    def __init__(self, patterns=COND_PATTERNS):
        self._patterns = patterns

    def get_patterns(self):
        return self._patterns


def _combat_task_config():
    return TaskConfig(
        name=PROV_A,
        enabled=True,
        provider_type="openai_like",
        use_api=False,
        stages=StageConfig(search=True, gather=True, check=True, inspect=True),
        patterns=Patterns(),  # EMPTY — the combat shape (kp_len=0)
    )


def _mgr(pipeline, providers=(PROV_A,)):
    return TaskRecoveryManager(pipeline, {p: FakeProvider() for p in providers})


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def lines(self):
        return [r.getMessage() for r in self.records]


@pytest.fixture()
def capture():
    handler = _Capture()
    mr.logger.addHandler(handler)
    try:
        yield handler
    finally:
        mr.logger.removeHandler(handler)


# ---------------------------------------------------------------------------
# D1a: cross-restart skip suppression — reader must hit the WRITER domain.
# DEF-2 update: both recovery and runtime writers now use the condition-level
# domain (H_cfg) when task-level patterns are empty (the combat shape).
# Coverage rows are seeded under COND_DIGEST; recovery must suppress them.
# ---------------------------------------------------------------------------
def test_d1a_recovery_suppresses_task_domain_coverage(tmp_path, registry, capture):
    stage = FakeStage()
    engine = _engine(tmp_path, MODE_ON)
    pipeline = Def1Pipeline(
        stage, gather_skip=engine, task_configs={PROV_A: _combat_task_config()}
    )
    urls = [_url(0), _url(1)]
    for u in urls:
        _seed(registry, u, digest=COND_DIGEST)

    _mgr(pipeline)._recover_acquisition_tasks(PROV_A, urls)

    assert stage.tasks == [], (
        "DEF-1/DEF-2: coverage written under the unified condition-level "
        f"digest must suppress re-enqueue at restart, got enqueued: {stage.urls}"
    )
    lines = [l for l in capture.lines() if "Recovered" in l and PROV_A in l]
    assert lines and "skip-suppressed 2" in lines[0], (
        f"expected 'skip-suppressed 2' in the recovery line, got: {lines}"
    )


# ---------------------------------------------------------------------------
# D1b: fallback guard — without task_configs the legacy provider-patterns
# domain is used byte-identically (harness compatibility, pre/post both PASS).
# ---------------------------------------------------------------------------
def test_d1b_fallback_provider_domain_when_no_task_configs(tmp_path, registry, capture):
    stage = FakeStage()
    engine = _engine(tmp_path, MODE_ON)
    pipeline = LegacyPipeline(stage, gather_skip=engine)
    urls = [_url(0)]
    _seed(registry, urls[0], digest=COND_DIGEST)

    _mgr(pipeline)._recover_acquisition_tasks(PROV_A, urls)

    assert stage.tasks == [], (
        "legacy fallback: provider-patterns digest must still suppress, "
        f"got enqueued: {stage.urls}"
    )


# ---------------------------------------------------------------------------
# D1c: share-claim key alignment — recovery's claim must suppress the
# runtime twin claim keyed by the unified condition-level digest (single
# boot, R17).  DEF-2 update: both recovery and runtime fan-out now use
# COND_DIGEST when task-level patterns are empty.
# ---------------------------------------------------------------------------
def test_d1c_share_claim_key_aligned_with_runtime_domain(tmp_path, registry):
    stage = FakeStage()
    ledger = GatherShareLedger(mode="on")
    pipeline = Def1Pipeline(
        stage, share_ledger=ledger, task_configs={PROV_A: _combat_task_config()}
    )
    urls = [_url(0)]

    _mgr(pipeline)._recover_acquisition_tasks(PROV_A, urls)
    assert stage.urls == urls, "owner (first claimant) must enqueue its own task"

    # Runtime twin: search-side fan-out claims under the unified condition-level
    # digest (DEF-2: B_patterns resolved from condition when task-level empty).
    twin_suppressed = ledger.claim(url_hash(urls[0]), COND_DIGEST, "raw", PROV_B)
    assert twin_suppressed is True, (
        "DEF-1/DEF-2: recovery claim must share the unified condition-level "
        "ledger key domain; twin claim was NOT suppressed "
        "(foreign digest claimed a different key)"
    )


# ---------------------------------------------------------------------------
# D1d: creation-domain alignment — recovered AcquisitionTasks carry the
# unified condition-level patterns (DEF-2: when task-level is empty,
# fallback to provider.get_patterns()), so their downstream
# _digest_for/_record_gathered writes land in the SAME domain.
# ---------------------------------------------------------------------------
def test_d1d_recovered_task_carries_task_config_patterns(tmp_path, registry):
    stage = FakeStage()
    pipeline = Def1Pipeline(stage, task_configs={PROV_A: _combat_task_config()})
    urls = [_url(0)]

    _mgr(pipeline)._recover_acquisition_tasks(PROV_A, urls)

    assert len(stage.tasks) == 1
    task = stage.tasks[0]
    # DEF-2 update: task-level is empty ⇒ condition-level patterns used.
    assert task.key_pattern == COND_PATTERNS.key_pattern, (
        "DEF-1/DEF-2: recovered task must carry condition-level patterns "
        f"(task-level empty in combat), got key_pattern={task.key_pattern!r}"
    )
    assert task.address_pattern == COND_PATTERNS.address_pattern
    assert task.endpoint_pattern == COND_PATTERNS.endpoint_pattern
    assert task.model_pattern == COND_PATTERNS.model_pattern
