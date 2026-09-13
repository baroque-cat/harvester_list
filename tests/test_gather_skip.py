"""Decision-engine tests for gather-skip (add-gather-skip).

Traceability: gather-skip-S1..S5, S8, S10..S12.

Harness: a tmp-workspace registry is pre-populated through the change-1 writer
API, then the decision engine is invoked directly with synthetic page URL
lists.  No network, no live GitHub.  The clock is frozen so TTL/push
comparisons are deterministic.
"""

import json
import os
import sqlite3

import pytest

from core.models import LinkMetadata
from storage.gather_skip import (
    MODE_ON,
    REASON_CHANGED,
    REASON_COVERAGE_GAP,
    REASON_FAILED_RETRY,
    REASON_TTL_EXPIRED,
    GatherSkipEngine,
)
from storage.registry import Registry, patterns_hash, url_hash

PROVIDER = "openai"
OTHER_PROVIDER = "anthropic"
PATTERNS = patterns_hash(key_pattern=r"sk-[A-Za-z0-9]{16}")
OTHER_PATTERNS = patterns_hash(key_pattern=r"sk-legacy-[A-Za-z0-9]{16}")
KEY_PATTERN = r"sk-[A-Za-z0-9]{16}"

NOW = 1_700_000_000.0
HOUR = 3600.0
DAY = 24 * HOUR
TTL_HOURS = 168.0

BASE = "https://github.com/acme/widgets/blob/main"


class _FastRegistryConfig:
    """Registry config that flushes synchronously-ish (batch_size=1)."""

    enabled = True
    batch_size = 1
    flush_interval = 0.05
    queue_size = 100000
    path = ""


def _registry(workspace: str) -> Registry:
    registry = Registry(workspace, config=_FastRegistryConfig(), enabled=True)
    assert registry.start() is True
    return registry


def _engine(workspace: str, mode: str = MODE_ON, **kwargs) -> GatherSkipEngine:
    return GatherSkipEngine(
        workspace=workspace,
        mode=mode,
        ttl_hours=TTL_HOURS,
        registry_path=os.path.join(workspace, "registry.sqlite"),
        run_id="run-engine-test",
        clock=lambda: NOW,
        **kwargs,
    )


def _seed(
    registry: Registry,
    workspace: str,
    url: str,
    *,
    status: str = "gathered_ok",
    gathered_ts: float = NOW - DAY,
    provider: str = PROVIDER,
    patterns: str = PATTERNS,
    pushed=None,
) -> None:
    """Populate one link through the change-1 writer API."""
    registry.record_link(url, transport="web", provider=provider, ts=min(gathered_ts, NOW - DAY) - 10)
    if status == "gathered_ok":
        registry.record_gather(url, provider=provider, patterns_hash=patterns, success=True, ts=gathered_ts)
        if pushed is not None:
            registry.record_metadata({url: LinkMetadata(repo_pushed_at=pushed)})
    elif status == "failed":
        registry.record_gather(url, provider=provider, patterns_hash=patterns, success=False, ts=gathered_ts)
    # "discovered" -> record_link only
    registry.flush(10.0)

    if status == "failed_fresh":
        # The writer never keeps gathered_ts on a failed row (a success is
        # never downgraded), so the adversarial "failed but recently touched"
        # state is constructed directly to pin the never-skip-failures rule.
        _force_failed(workspace, url, gathered_ts)


def _force_failed(workspace: str, url: str, ts: float) -> None:
    conn = sqlite3.connect(os.path.join(workspace, "registry.sqlite"), timeout=5.0)
    try:
        conn.execute(
            "UPDATE links SET visit_status = 'failed', gathered_ts = ? WHERE url_hash = ?",
            (ts, url_hash(url)),
        )
        conn.commit()
    finally:
        conn.close()


def _read_log(workspace: str):
    path = os.path.join(workspace, "registry_decisions.jsonl")
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


# ---------------------------------------------------------------------------
# gather-skip-S1
# ---------------------------------------------------------------------------
def test_s1_fully_known_fresh_covered_link_is_skipped(workspace):
    registry = _registry(workspace)
    try:
        _seed(registry, workspace, f"{BASE}/a.py", gathered_ts=NOW - DAY)
    finally:
        registry.stop()

    engine = _engine(workspace)
    [decision] = engine.decide([f"{BASE}/a.py"], provider=PROVIDER, patterns_hash=PATTERNS)

    assert decision.known is True
    assert decision.skip is True
    assert decision.reason == ""
    assert engine.skipped_known == 1
    assert engine.to_stats()["skipped_known"] == 1


# ---------------------------------------------------------------------------
# gather-skip-S2
# ---------------------------------------------------------------------------
def test_s2_never_gathered_link_is_not_skipped(workspace):
    registry = _registry(workspace)
    url = f"{BASE}/b.py"
    try:
        _seed(registry, workspace, url, status="discovered")
    finally:
        registry.stop()

    engine = _engine(workspace)
    [decision] = engine.decide([url], provider=PROVIDER, patterns_hash=PATTERNS)

    assert decision.known is True
    assert decision.skip is False
    assert engine.skipped_known == 0


# ---------------------------------------------------------------------------
# gather-skip-S3
# ---------------------------------------------------------------------------
def test_s3_ttl_expiry_forces_regather(workspace):
    registry = _registry(workspace)
    url = f"{BASE}/c.py"
    try:
        _seed(registry, workspace, url, gathered_ts=NOW - (TTL_HOURS + 1) * HOUR)
    finally:
        registry.stop()

    engine = _engine(workspace)
    [decision] = engine.decide([url], provider=PROVIDER, patterns_hash=PATTERNS)

    assert decision.skip is False
    assert decision.reason == REASON_TTL_EXPIRED
    assert engine.to_stats()["regathered_ttl_expired"] == 1


# ---------------------------------------------------------------------------
# gather-skip-S4
# ---------------------------------------------------------------------------
def test_s4_fresh_push_invalidates_prior_gather(workspace):
    registry = _registry(workspace)
    url = f"{BASE}/d.py"
    gathered_ts = NOW - DAY
    try:
        _seed(registry, workspace, url, gathered_ts=gathered_ts, pushed=gathered_ts + 5 * HOUR)
    finally:
        registry.stop()

    engine = _engine(workspace)
    [decision] = engine.decide([url], provider=PROVIDER, patterns_hash=PATTERNS)

    assert decision.skip is False
    assert decision.reason == REASON_CHANGED
    assert decision.push_evidence is True
    assert engine.to_stats()["regathered_changed"] == 1


# ---------------------------------------------------------------------------
# gather-skip-S5
# ---------------------------------------------------------------------------
def test_s5_provider_or_pattern_switch_forces_regather(workspace):
    provider_switch = f"{BASE}/e.py"
    pattern_switch = f"{BASE}/f.py"
    registry = _registry(workspace)
    try:
        _seed(registry, workspace, provider_switch, gathered_ts=NOW - HOUR, provider=OTHER_PROVIDER)
        _seed(registry, workspace, pattern_switch, gathered_ts=NOW - HOUR, patterns=OTHER_PATTERNS)
    finally:
        registry.stop()

    engine = _engine(workspace)
    decisions = engine.decide(
        [provider_switch, pattern_switch], provider=PROVIDER, patterns_hash=PATTERNS
    )

    assert [d.skip for d in decisions] == [False, False]
    assert {d.reason for d in decisions} == {REASON_COVERAGE_GAP}
    assert engine.to_stats()["regathered_coverage_gap"] == 2


# ---------------------------------------------------------------------------
# gather-skip-S8
# ---------------------------------------------------------------------------
def test_s8_failed_link_regathered_despite_fresh_timestamp(workspace):
    registry = _registry(workspace)
    url = f"{BASE}/g.py"
    try:
        _seed(registry, workspace, url, status="failed_fresh", gathered_ts=NOW - HOUR)
    finally:
        registry.stop()

    engine = _engine(workspace)
    [decision] = engine.decide([url], provider=PROVIDER, patterns_hash=PATTERNS)

    assert decision.skip is False
    assert decision.reason == REASON_FAILED_RETRY
    assert engine.to_stats()["regathered_failed_retry"] == 1


# ---------------------------------------------------------------------------
# gather-skip-S10
# ---------------------------------------------------------------------------
def test_s10_counters_reflect_a_mixed_page_of_links(workspace):
    skippable = f"{BASE}/skip.py"
    ttl = f"{BASE}/ttl.py"
    changed = f"{BASE}/changed.py"
    coverage = f"{BASE}/coverage.py"
    failed = f"{BASE}/failed.py"
    unseen = f"{BASE}/unseen.py"

    registry = _registry(workspace)
    try:
        _seed(registry, workspace, skippable, gathered_ts=NOW - DAY)
        _seed(registry, workspace, ttl, gathered_ts=NOW - (TTL_HOURS + 1) * HOUR)
        _seed(
            registry,
            workspace,
            changed,
            gathered_ts=NOW - DAY,
            pushed=(NOW - DAY) + 5 * HOUR,
        )
        _seed(registry, workspace, coverage, gathered_ts=NOW - HOUR, provider=OTHER_PROVIDER)
        _seed(registry, workspace, failed, status="failed_fresh", gathered_ts=NOW - HOUR)
    finally:
        registry.stop()

    engine = _engine(workspace)
    decisions = engine.decide(
        [skippable, ttl, changed, coverage, failed, unseen],
        provider=PROVIDER,
        patterns_hash=PATTERNS,
    )

    assert sum(1 for d in decisions if d.skip) == 1
    assert sum(1 for d in decisions if not d.skip) == 5

    stats = engine.to_stats()
    assert stats["skipped_known"] == 1
    assert stats["regathered_ttl_expired"] == 1
    assert stats["regathered_changed"] == 1
    assert stats["regathered_coverage_gap"] == 1
    assert stats["regathered_failed_retry"] == 1

    # The unseen link yields a task with no regather attribution.
    unseen_decision = decisions[-1]
    assert unseen_decision.known is False
    assert unseen_decision.reason == ""


# ---------------------------------------------------------------------------
# gather-skip-S11
# ---------------------------------------------------------------------------
def test_s11_null_push_date_does_not_veto_skip(workspace):
    registry = _registry(workspace)
    url = f"{BASE}/nullpush.py"
    try:
        _seed(registry, workspace, url, gathered_ts=NOW - DAY, pushed=None)
    finally:
        registry.stop()

    engine = _engine(workspace, mode=MODE_ON)
    [decision] = engine.decide([url], provider=PROVIDER, patterns_hash=PATTERNS)

    assert decision.skip is True
    assert decision.push_evidence is False
    assert engine.skipped_known == 1

    entry = next(e for e in _read_log(workspace) if e["url_hash"] == url_hash(url))
    assert entry["conditions"]["push_evidence"] is False
    assert entry["conditions"]["push_changed"] is False
    assert entry["conditions"]["ttl_ok"] is True


# ---------------------------------------------------------------------------
# gather-skip-S12
# ---------------------------------------------------------------------------
def test_s12_push_signal_coverage_reported(workspace):
    urls = [f"{BASE}/push{i}.py" for i in range(100)]
    registry = _registry(workspace)
    try:
        for index, url in enumerate(urls):
            # 30 links carry real push evidence (older than the gather: clean).
            pushed = (NOW - 2 * DAY) if index < 30 else None
            _seed(registry, workspace, url, gathered_ts=NOW - DAY, pushed=pushed)
    finally:
        registry.stop()

    engine = _engine(workspace)
    engine.decide(urls, provider=PROVIDER, patterns_hash=PATTERNS)

    stats = engine.to_stats()
    assert stats["evaluated_known"] == 100
    assert stats["push_signal_coverage"] == pytest.approx(0.30)
