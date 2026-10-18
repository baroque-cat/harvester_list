"""P26 — shard recovery must honour the creation-time policy stack.

Traceability: plan.md II.19 P26 (found by the P17 A/B gate, I.23), design
D1–D8 in /tmp/opencode/p26-plan.md.

The defect: ``manager/recovery.py::_recover_acquisition_tasks`` replayed EVERY
unique shard URL straight into ``put_task``, bypassing ``gather_skip.decide()``
and ``share_ledger.claim()``.  Shards are append-only and cumulative, so each
restart re-enqueued the entire link history × providers (measured 12 268 tasks
on one restart; unbounded growth) — under ``skip_known: on`` this dominated
queue growth (12 268 recovered vs 1 390 search-created).

The fix mirrors the search-side stack EXACTLY (stage/definition.py:171-200):
gather-skip decision first (suppression only under ``enforce``), then the
cross-provider share claim (fail-open), then create+put.  Both layers ride
their EXISTING flags (``skip.skip_known``, ``gather.share_fetch``); with both
off the behaviour is byte-identical to the pre-P26 replay.  No repo-meta
refresh and no dating hooks run at startup (D4 — stored registry state only).

Harness: real ``Registry`` + real ``GatherSkipEngine`` (frozen clock, style of
tests/test_gather_skip.py) + real ``GatherShareLedger``; fakes only at the
stage/pipeline/provider boundary.  Log contract asserted on captured records
of ``manager.recovery.logger`` (style of tests/test_roi_shutdown.py).

RED at plan time (additive change — reproduces on HEAD, no worktree needed).
Line-by-line prediction, verified by running (R11 lesson: reasons, not counts):
- FAIL on HEAD (9): T1/T13/T14/T15 (line-format tokens absent, suppression
  absent), T2 (engine never consulted — skipped_known stays 0), T4 (WARNING
  never logged — decide unreachable), T5 (both providers enqueue; take() empty
  — claim never called), T6 (would_suppress stays 0), T9 (line token absent).
- PASS PREMATURELY on HEAD, kept as regression guards (6): T3 (off = zero
  interaction — same before/after), T7 (share off = both enqueue), T8 (claim
  unreachable on HEAD so the boom never fires), T10/T11/T12 (re-gather reasons
  enqueue on HEAD too — they guard AGAINST over-suppression after the fix).
"""

import logging
import os

import pytest

from core.enums import PipelineStage
from core.models import Patterns
from manager.gather_share import GatherShareLedger
from manager.recovery import TaskRecoveryManager
from storage.gather_skip import MODE_OFF, MODE_ON, MODE_SHADOW, GatherSkipEngine
from storage.registry import Registry, patterns_hash, url_hash

import manager.recovery as mr

PROV_A = "prov-a"
PROV_B = "prov-b"
KEY_PATTERN = r"sk-[A-Za-z0-9]{16}"
OTHER_PATTERN = r"sk-legacy-[A-Za-z0-9]{16}"
DIGEST = patterns_hash(key_pattern=KEY_PATTERN)
OTHER_DIGEST = patterns_hash(key_pattern=OTHER_PATTERN)

NOW = 1_700_000_000.0
HOUR = 3600.0
DAY = 24 * HOUR
TTL_HOURS = 168.0

BASE = "https://github.com/acme/widgets/blob/main"


def _url(i: int) -> str:
    return f"{BASE}/file{i}.txt"


class _FastRegistryConfig:
    """Registry config that flushes synchronously-ish (batch_size=1)."""

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


def _seed(registry, url, *, status="gathered_ok", gathered_ts=None, provider=PROV_A, digest=None):
    """Populate one link through the registry writer API (test_gather_skip style)."""
    gathered_ts = NOW - DAY if gathered_ts is None else gathered_ts
    digest = DIGEST if digest is None else digest
    registry.record_link(url, transport="web", provider=provider, ts=gathered_ts - 10)
    if status == "gathered_ok":
        registry.record_gather(url, provider=provider, patterns_hash=digest, success=True, ts=gathered_ts)
    elif status == "failed":
        registry.record_gather(url, provider=provider, patterns_hash=digest, success=False, ts=gathered_ts)
    # "discovered" -> record_link only
    registry.flush(10.0)


def _engine(tmp_path, mode):
    return GatherSkipEngine(
        workspace=str(tmp_path),
        mode=mode,
        ttl_hours=TTL_HOURS,
        registry_path=os.path.join(str(tmp_path), "registry.sqlite"),
        run_id="p26-test",
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


class FakePipeline:
    def __init__(self, stage, gather_skip=None, share_ledger=None, with_config=True):
        self._stage = stage
        self.gather_skip = gather_skip
        self.share_ledger = share_ledger
        if with_config:
            self.config = _Cfg()

    def get_stage(self, name):
        return self._stage if name == PipelineStage.GATHER.value else None


class BarePipeline:
    """Pipeline stub WITHOUT gather_skip/share_ledger/config attributes (T15)."""

    def __init__(self, stage):
        self._stage = stage

    def get_stage(self, name):
        return self._stage if name == PipelineStage.GATHER.value else None


class FakeProvider:
    def __init__(self, key_pattern=KEY_PATTERN):
        self._patterns = Patterns(key_pattern=key_pattern)

    def get_patterns(self):
        return self._patterns


def _mgr(pipeline, providers=(PROV_A,)):
    return TaskRecoveryManager(pipeline, {p: FakeProvider() for p in providers})


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


def _recovery_line(cap, provider=PROV_A):
    hits = [m for m in cap.messages if "acquisition tasks for" in m and provider in m]
    assert len(hits) == 1, f"expected exactly one recovery line, got {hits!r}"
    return hits[0]


# ---------------------------------------------------------------------------
# T1 — skip=on + gathered_ok + coverage: NOT enqueued, exact line tokens
# ---------------------------------------------------------------------------
def test_t1_skip_on_suppresses_gathered_link(tmp_path, registry, cap):
    _seed(registry, _url(1))
    stage = FakeStage()
    pipe = FakePipeline(stage, gather_skip=_engine(tmp_path, MODE_ON))
    _mgr(pipe)._recover_acquisition_tasks(PROV_A, [_url(1)])
    assert stage.urls == []
    line = _recovery_line(cap)
    assert "Recovered 0 acquisition tasks for prov-a" in line
    assert "(shard urls 1, skip-suppressed 1, share-suppressed 0)" in line


# ---------------------------------------------------------------------------
# T2 — skip=shadow: engine consulted (counters move) but everything enqueued
# ---------------------------------------------------------------------------
def test_t2_shadow_counts_without_acting(tmp_path, registry, cap):
    _seed(registry, _url(1))
    engine = _engine(tmp_path, MODE_SHADOW)
    stage = FakeStage()
    pipe = FakePipeline(stage, gather_skip=engine)
    _mgr(pipe)._recover_acquisition_tasks(PROV_A, [_url(1)])
    assert stage.urls == [_url(1)]
    assert engine.skipped_known >= 1, "shadow must measure the recovery population"
    line = _recovery_line(cap)
    assert "Recovered 1 acquisition tasks for prov-a" in line
    assert "skip-suppressed 0" in line


# ---------------------------------------------------------------------------
# T3 — skip=off: decide() never called, everything enqueued (zero interaction)
# ---------------------------------------------------------------------------
def test_t3_off_mode_never_consults_engine(tmp_path, registry, cap, monkeypatch):
    _seed(registry, _url(1))
    engine = _engine(tmp_path, MODE_OFF)
    calls = []
    monkeypatch.setattr(engine, "decide", lambda *a, **k: calls.append((a, k)) or [])
    stage = FakeStage()
    pipe = FakePipeline(stage, gather_skip=engine)
    _mgr(pipe)._recover_acquisition_tasks(PROV_A, [_url(1)])
    assert stage.urls == [_url(1)]
    assert calls == [], "off mode must not touch the decision engine"


# ---------------------------------------------------------------------------
# T4 — decide() raises: WARNING + fail-open (everything enqueued)
# ---------------------------------------------------------------------------
def test_t4_decide_failure_fails_open(tmp_path, registry, cap, monkeypatch):
    _seed(registry, _url(1))
    engine = _engine(tmp_path, MODE_ON)

    def boom(*a, **k):
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(engine, "decide", boom)
    stage = FakeStage()
    pipe = FakePipeline(stage, gather_skip=engine)
    _mgr(pipe)._recover_acquisition_tasks(PROV_A, [_url(1)])
    assert stage.urls == [_url(1)], "fail-open direction is enqueue"
    assert any("gather-skip decision failed" in m for m in cap.messages)


# ---------------------------------------------------------------------------
# T5 — share=on, two providers, one URL: second suppressed AND registered as
#      beneficiary (take() fan-out contract of R17 preserved for recovery)
# ---------------------------------------------------------------------------
def test_t5_share_claim_suppresses_second_provider(tmp_path, cap):
    stage = FakeStage()
    ledger = GatherShareLedger(mode="on")
    pipe = FakePipeline(stage, share_ledger=ledger)
    mgr = _mgr(pipe, providers=(PROV_A, PROV_B))
    mgr._recover_acquisition_tasks(PROV_A, [_url(5)])
    mgr._recover_acquisition_tasks(PROV_B, [_url(5)])
    assert stage.urls == [_url(5)], "exactly one fetch task for the shared key"
    assert ledger.take(url_hash(_url(5)), DIGEST, "raw") == [PROV_A, PROV_B]


# ---------------------------------------------------------------------------
# T6 — share=shadow: pure counting, both enqueued
# ---------------------------------------------------------------------------
def test_t6_share_shadow_counts_only(tmp_path, cap):
    stage = FakeStage()
    ledger = GatherShareLedger(mode="shadow")
    pipe = FakePipeline(stage, share_ledger=ledger)
    mgr = _mgr(pipe, providers=(PROV_A, PROV_B))
    mgr._recover_acquisition_tasks(PROV_A, [_url(6)])
    mgr._recover_acquisition_tasks(PROV_B, [_url(6)])
    assert sorted(stage.urls) == [_url(6), _url(6)]
    assert ledger.to_stats()["would_suppress"] == 1


# ---------------------------------------------------------------------------
# T7 — share=off: zero ledger interaction
# ---------------------------------------------------------------------------
def test_t7_share_off_zero_interaction(tmp_path, cap):
    stage = FakeStage()
    ledger = GatherShareLedger(mode="off")
    pipe = FakePipeline(stage, share_ledger=ledger)
    mgr = _mgr(pipe, providers=(PROV_A, PROV_B))
    mgr._recover_acquisition_tasks(PROV_A, [_url(7)])
    mgr._recover_acquisition_tasks(PROV_B, [_url(7)])
    assert sorted(stage.urls) == [_url(7), _url(7)]
    assert ledger.to_stats()["claims"] == 0


# ---------------------------------------------------------------------------
# T8 — claim() raises: fail-open, task enqueued
# ---------------------------------------------------------------------------
def test_t8_claim_failure_fails_open(tmp_path, cap, monkeypatch):
    stage = FakeStage()
    ledger = GatherShareLedger(mode="on")

    def boom(*a, **k):
        raise RuntimeError("ledger exploded")

    monkeypatch.setattr(ledger, "claim", boom)
    pipe = FakePipeline(stage, share_ledger=ledger)
    _mgr(pipe)._recover_acquisition_tasks(PROV_A, [_url(8)])
    assert stage.urls == [_url(8)]


# ---------------------------------------------------------------------------
# T9 — pattern switch: coverage under OTHER digest does not suppress (re-gather)
# ---------------------------------------------------------------------------
def test_t9_pattern_switch_regathers(tmp_path, registry, cap):
    _seed(registry, _url(9), digest=OTHER_DIGEST)
    stage = FakeStage()
    pipe = FakePipeline(stage, gather_skip=_engine(tmp_path, MODE_ON))
    _mgr(pipe)._recover_acquisition_tasks(PROV_A, [_url(9)])
    assert stage.urls == [_url(9)]
    assert "skip-suppressed 0" in _recovery_line(cap)


# ---------------------------------------------------------------------------
# T10 — TTL expired: re-enqueued under skip=on
# ---------------------------------------------------------------------------
def test_t10_ttl_expired_regathers(tmp_path, registry, cap):
    _seed(registry, _url(10), gathered_ts=NOW - 8 * DAY)  # TTL is 168 h = 7 d
    stage = FakeStage()
    pipe = FakePipeline(stage, gather_skip=_engine(tmp_path, MODE_ON))
    _mgr(pipe)._recover_acquisition_tasks(PROV_A, [_url(10)])
    assert stage.urls == [_url(10)]


# ---------------------------------------------------------------------------
# T11 — failed visit_status: never suppressed (REASON_FAILED_RETRY)
# ---------------------------------------------------------------------------
def test_t11_failed_link_regathers(tmp_path, registry, cap):
    _seed(registry, _url(11), status="failed")
    stage = FakeStage()
    pipe = FakePipeline(stage, gather_skip=_engine(tmp_path, MODE_ON))
    _mgr(pipe)._recover_acquisition_tasks(PROV_A, [_url(11)])
    assert stage.urls == [_url(11)]


# ---------------------------------------------------------------------------
# T12 — discovered-only (never gathered): re-enqueued
# ---------------------------------------------------------------------------
def test_t12_discovered_only_regathers(tmp_path, registry, cap):
    _seed(registry, _url(12), status="discovered")
    stage = FakeStage()
    pipe = FakePipeline(stage, gather_skip=_engine(tmp_path, MODE_ON))
    _mgr(pipe)._recover_acquisition_tasks(PROV_A, [_url(12)])
    assert stage.urls == [_url(12)]


# ---------------------------------------------------------------------------
# T13 — line contract: exact tokens, printed even when enqueued == 0,
#       legacy base substring intact
# ---------------------------------------------------------------------------
def test_t13_line_printed_when_everything_suppressed(tmp_path, registry, cap):
    _seed(registry, _url(13))
    _seed(registry, _url(14))
    stage = FakeStage()
    pipe = FakePipeline(stage, gather_skip=_engine(tmp_path, MODE_ON))
    _mgr(pipe)._recover_acquisition_tasks(PROV_A, [_url(13), _url(14)])
    assert stage.urls == []
    line = _recovery_line(cap)
    assert "Recovered 0 acquisition tasks for prov-a (shard urls 2, skip-suppressed 2, share-suppressed 0)" in line


# ---------------------------------------------------------------------------
# T14 — layering: the stage receives EXACTLY the non-suppressed set; both
#       layers compose.  Semantics pinned: a skip-suppressed provider does NOT
#       register share interest, so another provider with a genuine coverage
#       gap legitimately OWNS that fetch (and its R17 fan-out serves whoever
#       claimed); the skip-suppressed provider needs no fan-out — it already
#       has coverage.
# ---------------------------------------------------------------------------
def test_t14_both_layers_compose_exact_put_set(tmp_path, registry, cap):
    _seed(registry, _url(15))  # gathered_ok + coverage for PROV_A only
    stage = FakeStage()
    ledger = GatherShareLedger(mode="on")
    pipe = FakePipeline(stage, gather_skip=_engine(tmp_path, MODE_ON), share_ledger=ledger)
    mgr = _mgr(pipe, providers=(PROV_A, PROV_B))
    mgr._recover_acquisition_tasks(PROV_A, [_url(15), _url(16)])
    mgr._recover_acquisition_tasks(PROV_B, [_url(15), _url(16)])
    # PROV_A: url15 skip-suppressed (own coverage); url16 unknown -> claim -> owner -> enqueued.
    # PROV_B: url15 coverage gap -> passes skip -> claim -> OWNER (A never claimed) -> enqueued;
    #         url16 -> claim -> A owns -> share-suppressed (beneficiary).
    assert stage.urls == [_url(16), _url(15)]
    line_a = _recovery_line(cap, PROV_A)
    assert "Recovered 1 acquisition tasks for prov-a" in line_a
    assert "skip-suppressed 1, share-suppressed 0" in line_a
    line_b = _recovery_line(cap, PROV_B)
    assert "Recovered 1 acquisition tasks for prov-b" in line_b
    assert "skip-suppressed 0, share-suppressed 1" in line_b
    assert ledger.take(url_hash(_url(16)), DIGEST, "raw") == [PROV_A, PROV_B]


# ---------------------------------------------------------------------------
# T15 — duck-typing guard: pipeline WITHOUT the new attributes behaves exactly
#       like the legacy replay (enqueue-half passes on HEAD; the line-format
#       half is RED there and becomes the post-fix regression guard)
# ---------------------------------------------------------------------------
def test_t15_bare_pipeline_legacy_behaviour(tmp_path, cap):
    stage = FakeStage()
    pipe = BarePipeline(stage)
    _mgr(pipe)._recover_acquisition_tasks(PROV_A, [_url(17), _url(18)])
    assert stage.urls == [_url(17), _url(18)]
    line = _recovery_line(cap)
    assert "Recovered 2 acquisition tasks for prov-a" in line
    assert "(shard urls 2, skip-suppressed 0, share-suppressed 0)" in line
