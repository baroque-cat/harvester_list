"""Repository priority scoring tests for add-target-prioritization.

Traceability: target-prioritization-S1..S6, S10.

Harness: a fast tmp registry seeded through the change-1 writer API (links with
controlled freshness/size plus keys carrying statuses and a source-URL
attribution); the scorer is exercised directly, through the dirty-set writer
path, and through the run-finish sweep.  No network, no live GitHub.
"""

import os
import sqlite3

import pytest

from storage.priority import (
    PriorityWeights,
    compute_score,
    score_repo,
    sweep,
)
from storage.registry import url_hash

DAY = 86400.0
NOW = 1_700_000_000.0
WEIGHTS = PriorityWeights()


def _registry_file(workspace: str) -> str:
    return os.path.join(str(workspace), "registry.sqlite")


def _link(registry, owner, repo, path="a.py", pushed_at=None, size_kb=None, ts=NOW):
    """Create a repo link (and optional repo metadata) through the writer API."""
    url = f"https://github.com/{owner}/{repo}/blob/main/{path}"
    registry.record_link(url, ts=ts)
    if pushed_at is not None or size_kb is not None:
        registry.record_repo_link_metadata(owner, repo, pushed_at=pushed_at, size_kb=size_kb)
    registry.flush(10.0)
    return url


def _key(registry, url, status, key_hash="key-1", ts=NOW):
    """Upsert a ledger row attributed to ``url`` via its source hash."""
    registry.record_key(
        key_hash=key_hash,
        provider="openai",
        key_ref_masked="sk-***",
        address="https://api.example.com",
        endpoint="https://api.example.com/v1",
        status=status,
        source_url_hash=url_hash(url),
        ts=ts,
    )
    registry.flush(10.0)


def _read_priority(workspace, owner, repo):
    conn = sqlite3.connect(_registry_file(workspace))
    try:
        row = conn.execute(
            "SELECT priority FROM links WHERE owner = ? AND repo = ? ORDER BY url_hash LIMIT 1",
            (owner, repo),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _raw_update_key_status(workspace, key_hash, status):
    """Mutate the ledger directly, bypassing the writer's dirty-marking."""
    conn = sqlite3.connect(_registry_file(workspace), timeout=5.0)
    try:
        conn.execute("UPDATE keys SET status = ? WHERE key_hash = ?", (status, key_hash))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# target-prioritization-S1
# ---------------------------------------------------------------------------
def test_s1_valid_key_repo_outranks_keyless_repo(workspace, fast_registry):
    registry = fast_registry(workspace)
    try:
        url_a = _link(registry, "acme", "alpha", pushed_at=NOW, size_kb=1000)
        _link(registry, "acme", "bravo", pushed_at=NOW, size_kb=1000)
        _key(registry, url_a, "valid")
        registry.sweep_priorities(now=NOW)
    finally:
        registry.stop()

    priority_a = _read_priority(workspace, "acme", "alpha")
    priority_b = _read_priority(workspace, "acme", "bravo")
    assert priority_a - priority_b >= WEIGHTS.w1 - 1e-6


# ---------------------------------------------------------------------------
# target-prioritization-S2
# ---------------------------------------------------------------------------
def test_s2_fresher_activity_outranks_stale(workspace, fast_registry):
    registry = fast_registry(workspace)
    try:
        _link(registry, "acme", "fresh", pushed_at=NOW, size_kb=1000)
        _link(registry, "acme", "stale", pushed_at=NOW - 60 * DAY, size_kb=1000)
        registry.sweep_priorities(now=NOW)
    finally:
        registry.stop()

    fresh = _read_priority(workspace, "acme", "fresh")
    stale = _read_priority(workspace, "acme", "stale")
    assert fresh > stale


# ---------------------------------------------------------------------------
# target-prioritization-S3
# ---------------------------------------------------------------------------
def test_s3_oversized_repo_is_penalized(workspace, fast_registry):
    registry = fast_registry(workspace)
    try:
        _link(registry, "acme", "small", pushed_at=NOW, size_kb=10_000)
        _link(registry, "acme", "huge", pushed_at=NOW, size_kb=WEIGHTS.ramp_kb)
        registry.sweep_priorities(now=NOW)
    finally:
        registry.stop()

    small = _read_priority(workspace, "acme", "small")
    huge = _read_priority(workspace, "acme", "huge")
    assert small - huge == pytest.approx(WEIGHTS.w5)


# ---------------------------------------------------------------------------
# target-prioritization-S4
# ---------------------------------------------------------------------------
def test_s4_sparse_rows_degrade_neutrally(workspace, fast_registry):
    registry = fast_registry(workspace)
    try:
        _link(registry, "acme", "bare")
        registry.sweep_priorities(now=NOW)
    finally:
        registry.stop()

    assert _read_priority(workspace, "acme", "bare") == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# target-prioritization-S5
# ---------------------------------------------------------------------------
def test_s5_status_transition_rescores_without_sweep(workspace, fast_registry):
    registry = fast_registry(workspace)
    try:
        url_a = _link(registry, "acme", "alpha")
        _key(registry, url_a, "wait_check")

        # Mid-run transition: no sweep, no run finish.
        _key(registry, url_a, "valid")

        priority = _read_priority(workspace, "acme", "alpha")
    finally:
        registry.stop()

    assert priority == pytest.approx(WEIGHTS.w1)


# ---------------------------------------------------------------------------
# target-prioritization-S6
# ---------------------------------------------------------------------------
def test_s6_run_finish_sweep_reconciles_everything(workspace, fast_registry):
    registry = fast_registry(workspace)
    try:
        url_a = _link(registry, "acme", "alpha", pushed_at=NOW - 10 * DAY, size_kb=1000)
        _key(registry, url_a, "wait_check")

        expected_valid = compute_score(True, False, NOW - 10 * DAY, 1000, WEIGHTS, NOW)

        # Bypass the writer entirely (e.g. a migration/manual edit): the stored
        # score is now stale relative to the ledger.
        _raw_update_key_status(workspace, "key-1", "valid")
        assert _read_priority(workspace, "acme", "alpha") != pytest.approx(expected_valid)

        registry.sweep_priorities(now=NOW)
    finally:
        registry.stop()

    assert _read_priority(workspace, "acme", "alpha") == pytest.approx(expected_valid)


def test_s6_finish_run_hook_sweeps_bypassed_mutations(workspace, fast_registry):
    """``finish_run()`` itself performs the sweep (design D3 writer-path hook).

    NULL dates/sizes keep the expected score time-independent: soft evidence
    scores exactly ``w2`` before the bypassed mutation is reconciled and
    exactly ``w1`` after the run-finish sweep.
    """
    registry = fast_registry(workspace)
    try:
        url_a = _link(registry, "acme", "alpha")
        _key(registry, url_a, "wait_check")
        assert _read_priority(workspace, "acme", "alpha") == pytest.approx(WEIGHTS.w2)

        # Bypass the writer entirely (e.g. a migration/manual edit): no
        # dirty-marking, so only the run-finish sweep can reconcile.
        _raw_update_key_status(workspace, "key-1", "valid")
        assert _read_priority(workspace, "acme", "alpha") == pytest.approx(WEIGHTS.w2)

        registry.start_run(run_id="run-s6")
        registry.finish_run()
    finally:
        registry.stop()

    assert _read_priority(workspace, "acme", "alpha") == pytest.approx(WEIGHTS.w1)


# ---------------------------------------------------------------------------
# target-prioritization-S10
# ---------------------------------------------------------------------------
def test_s10_custom_weights_change_ordering(workspace, fast_registry):
    weights = PriorityWeights(w1=0.0)
    registry = fast_registry(workspace)
    try:
        url_valid = _link(registry, "acme", "stale_valid", pushed_at=NOW - 60 * DAY, size_kb=1000)
        _link(registry, "acme", "fresh_keyless", pushed_at=NOW, size_kb=1000)
        _key(registry, url_valid, "valid")
        registry.sweep_priorities(weights, now=NOW)
    finally:
        registry.stop()

    assert _read_priority(workspace, "acme", "fresh_keyless") > _read_priority(workspace, "acme", "stale_valid")


# ---------------------------------------------------------------------------
# Scorer module contract (unit-level guards behind the scenarios above)
# ---------------------------------------------------------------------------
def test_score_repo_reads_attributed_key_and_freshness(workspace, fast_registry):
    registry = fast_registry(workspace)
    try:
        url_a = _link(registry, "acme", "alpha", pushed_at=NOW, size_kb=1000)
        _key(registry, url_a, "valid")
        registry.stop()

        conn = sqlite3.connect(_registry_file(workspace))
        try:
            score = score_repo(conn, "acme", "alpha", WEIGHTS, now=NOW)
        finally:
            conn.close()
    finally:
        if registry.available:
            registry.stop()

    assert score == pytest.approx(WEIGHTS.w1 + WEIGHTS.w4)
