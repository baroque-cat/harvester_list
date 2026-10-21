"""W1-W5 handle-coherence advisories (slice 2.7).

Traceability: 02_critical_fixes.md:48 (W1-W5 line), 02_critical_fixes.md:98 (F8: W4 WARNING not error),
08_decisions.md Д3 (19 days / 10^6 wires for W3), AGENTS.md §0.1 (46656 = 36^3 envelope).

House style: real Config objects, no mocks; warnings checked via validator.warnings list
(mirrors test_new1_shadow_poisoning.py, test_cql_lint.py, test_fcd_config.py).
No network.

RED state notes (pre-implementation):
- All tests FAIL because _validate_handle_coherence does not exist.
- W1 trigger: budget < N_enabled × clamp.
- W2 trigger: share_ledger_max_tracked < proxy (refine_governor.max_search_tasks_per_run).
- W3 trigger: max_age_hours < 456 (19 days × 24).
- W4 trigger: max_partitions_per_refine > 46656.
- W5 trigger: ≥2 distinct key_pattern sets among enabled tasks.

Neutrality (P-neutral): combat config form (clamp=128, budget=10000, age=720,
ledger-default 50000, 4 tasks with identical patterns) must NOT trigger any W1-W5.
"""

import contextlib

from config.schemas import (
    Config,
    CredentialsConfig,
    LoadBalanceStrategy,
    StageConfig,
    TaskConfig,
)
from config.validator import ConfigValidator
from core.models import Condition, Patterns


def _valid_base_config():
    """A Config that passes global/task validation."""
    cfg = Config()
    cfg.global_config.github_credentials = CredentialsConfig(
        sessions=["session"], tokens=["token"], strategy=LoadBalanceStrategy.ROUND_ROBIN
    )
    return cfg


def _warnings_for_config(cfg):
    """Return validator warnings for the given config."""
    validator = ConfigValidator()
    with contextlib.suppress(ValueError):
        validator.validate(cfg)
    return list(validator.warnings)


# ---------------------------------------------------------------------------
# W1: budget < N_enabled_tasks × clamp
# ---------------------------------------------------------------------------
def test_w1_trigger_budget_starvation():
    """W1 fires when budget < N_enabled × clamp (budget starves some roots)."""
    cfg = _valid_base_config()
    cfg.refine_governor.max_search_tasks_per_run = 100  # budget
    cfg.refine_governor.max_partitions_per_refine = 128  # clamp
    cfg.tasks = [
        TaskConfig(name="t1", enabled=True, patterns=Patterns(key_pattern="sk-x")),
        TaskConfig(name="t2", enabled=True, patterns=Patterns(key_pattern="sk-x")),
    ]
    # 2 enabled × 128 clamp = 256 > 100 budget → W1 fires
    warnings = _warnings_for_config(cfg)
    hits = [w for w in warnings if w.startswith("W1:")]
    assert len(hits) == 1, f"expected exactly one W1 warning, got: {hits}"
    assert "100" in hits[0] and "256" in hits[0], f"W1 must name the numbers: {hits[0]}"


def test_w1_neutral_single_task():
    """W1 does NOT fire when only one task is enabled (no starvation)."""
    cfg = _valid_base_config()
    cfg.refine_governor.max_search_tasks_per_run = 100
    cfg.refine_governor.max_partitions_per_refine = 128
    cfg.tasks = [
        TaskConfig(name="t1", enabled=True, patterns=Patterns(key_pattern="sk-x")),
    ]
    # 1 enabled × 128 clamp = 128 > 100 budget, but only one task → no W1
    warnings = _warnings_for_config(cfg)
    hits = [w for w in warnings if w.startswith("W1:")]
    assert len(hits) == 0, f"W1 should not fire for single task: {hits}"


def test_w1_neutral_sufficient_budget():
    """W1 does NOT fire when budget >= N_enabled × clamp."""
    cfg = _valid_base_config()
    cfg.refine_governor.max_search_tasks_per_run = 10000
    cfg.refine_governor.max_partitions_per_refine = 128
    cfg.tasks = [
        TaskConfig(name="t1", enabled=True, patterns=Patterns(key_pattern="sk-x")),
        TaskConfig(name="t2", enabled=True, patterns=Patterns(key_pattern="sk-x")),
    ]
    # 2 enabled × 128 clamp = 256 <= 10000 budget → no W1
    warnings = _warnings_for_config(cfg)
    hits = [w for w in warnings if w.startswith("W1:")]
    assert len(hits) == 0, f"W1 should not fire with sufficient budget: {hits}"


# ---------------------------------------------------------------------------
# W2: share_ledger_max_tracked < proxy
# ---------------------------------------------------------------------------
def test_w2_trigger_ledger_undersized():
    """W2 fires when share_ledger_max_tracked < proxy (max_search_tasks_per_run)."""
    cfg = _valid_base_config()
    cfg.gather.share_ledger_max_tracked = 5000  # ledger cap
    cfg.refine_governor.max_search_tasks_per_run = 10000  # proxy
    warnings = _warnings_for_config(cfg)
    hits = [w for w in warnings if w.startswith("W2:")]
    assert len(hits) == 1, f"expected exactly one W2 warning, got: {hits}"
    assert "5000" in hits[0] and "10000" in hits[0], f"W2 must name the numbers: {hits[0]}"


def test_w2_neutral_sufficient_ledger():
    """W2 does NOT fire when share_ledger_max_tracked >= proxy."""
    cfg = _valid_base_config()
    cfg.gather.share_ledger_max_tracked = 50000  # default
    cfg.refine_governor.max_search_tasks_per_run = 10000  # default
    # 50000 >= 10000 → no W2
    warnings = _warnings_for_config(cfg)
    hits = [w for w in warnings if w.startswith("W2:")]
    assert len(hits) == 0, f"W2 should not fire with sufficient ledger: {hits}"


# ---------------------------------------------------------------------------
# W3: max_age_hours < HORIZON_HOURS (456 = 19 days × 24)
# ---------------------------------------------------------------------------
def test_w3_trigger_narrow_age_gate():
    """W3 fires when max_age_hours < 456 (19 days × 24, Д3)."""
    cfg = _valid_base_config()
    cfg.queue.max_age_hours = 24.0  # schema default
    # 24 < 456 → W3 fires
    warnings = _warnings_for_config(cfg)
    hits = [w for w in warnings if w.startswith("W3:")]
    assert len(hits) == 1, f"expected exactly one W3 warning, got: {hits}"
    assert "24" in hits[0] and "456" in hits[0], f"W3 must name the numbers: {hits[0]}"


def test_w3_neutral_wide_age_gate():
    """W3 does NOT fire when max_age_hours >= 456."""
    cfg = _valid_base_config()
    cfg.queue.max_age_hours = 720.0  # combat config
    # 720 >= 456 → no W3
    warnings = _warnings_for_config(cfg)
    hits = [w for w in warnings if w.startswith("W3:")]
    assert len(hits) == 0, f"W3 should not fire with wide age gate: {hits}"


# ---------------------------------------------------------------------------
# W4: max_partitions_per_refine > 46656 (36^3 envelope)
# ---------------------------------------------------------------------------
def test_w4_trigger_fat_finger():
    """W4 fires when max_partitions_per_refine > 46656 (fat-finger guard)."""
    cfg = _valid_base_config()
    cfg.refine_governor.max_partitions_per_refine = 50000
    # 50000 > 46656 → W4 fires
    warnings = _warnings_for_config(cfg)
    hits = [w for w in warnings if w.startswith("W4:")]
    assert len(hits) == 1, f"expected exactly one W4 warning, got: {hits}"
    assert "50000" in hits[0] and "46656" in hits[0], f"W4 must name the numbers: {hits[0]}"


def test_w4_neutral_within_envelope():
    """W4 does NOT fire when max_partitions_per_refine <= 46656."""
    cfg = _valid_base_config()
    cfg.refine_governor.max_partitions_per_refine = 128  # combat config
    # 128 <= 46656 → no W4
    warnings = _warnings_for_config(cfg)
    hits = [w for w in warnings if w.startswith("W4:")]
    assert len(hits) == 0, f"W4 should not fire within envelope: {hits}"


def test_w4_no_error_only_warning():
    """W4 is a WARNING, not an ERROR (F8: no hard upper bound)."""
    cfg = _valid_base_config()
    cfg.refine_governor.max_partitions_per_refine = 50000
    validator = ConfigValidator()
    with contextlib.suppress(ValueError):
        validator.validate(cfg)
    # W4 must be in warnings, not errors
    w4_warnings = [w for w in validator.warnings if w.startswith("W4:")]
    w4_errors = [e for e in validator.errors if "46656" in e or "50000" in e]
    assert len(w4_warnings) == 1, f"W4 must be a warning: {w4_warnings}"
    assert len(w4_errors) == 0, f"W4 must NOT be an error: {w4_errors}"


# ---------------------------------------------------------------------------
# W5: ≥2 distinct key_pattern sets among enabled tasks
# ---------------------------------------------------------------------------
def test_w5_trigger_diverse_patterns():
    """W5 fires when enabled tasks have ≥2 distinct key_pattern sets."""
    cfg = _valid_base_config()
    cfg.tasks = [
        TaskConfig(name="t1", enabled=True, patterns=Patterns(key_pattern="sk-x")),
        TaskConfig(name="t2", enabled=True, patterns=Patterns(key_pattern="AKIA[0-9A-Z]{16}")),
    ]
    # 2 distinct patterns → W5 fires
    warnings = _warnings_for_config(cfg)
    hits = [w for w in warnings if w.startswith("W5:")]
    assert len(hits) == 1, f"expected exactly one W5 warning, got: {hits}"
    assert "t1" in hits[0] and "t2" in hits[0], f"W5 must name the tasks: {hits[0]}"
    assert "2" in hits[0], f"W5 must name the count: {hits[0]}"


def test_w5_neutral_identical_patterns():
    """W5 does NOT fire when all enabled tasks have identical key_pattern."""
    cfg = _valid_base_config()
    cfg.tasks = [
        TaskConfig(name="t1", enabled=True, patterns=Patterns(key_pattern="sk-x")),
        TaskConfig(name="t2", enabled=True, patterns=Patterns(key_pattern="sk-x")),
        TaskConfig(name="t3", enabled=True, patterns=Patterns(key_pattern="sk-x")),
        TaskConfig(name="t4", enabled=True, patterns=Patterns(key_pattern="sk-x")),
    ]
    # All identical → no W5
    warnings = _warnings_for_config(cfg)
    hits = [w for w in warnings if w.startswith("W5:")]
    assert len(hits) == 0, f"W5 should not fire with identical patterns: {hits}"


def test_w5_neutral_single_task():
    """W5 does NOT fire when only one task is enabled."""
    cfg = _valid_base_config()
    cfg.tasks = [
        TaskConfig(name="t1", enabled=True, patterns=Patterns(key_pattern="sk-x")),
    ]
    # Only one task → no W5
    warnings = _warnings_for_config(cfg)
    hits = [w for w in warnings if w.startswith("W5:")]
    assert len(hits) == 0, f"W5 should not fire for single task: {hits}"


# ---------------------------------------------------------------------------
# P-neutral: combat config form must NOT trigger any W1-W5
# ---------------------------------------------------------------------------
def test_p_neutral_combat_config_form():
    """Combat config form (clamp=128, budget=10000, age=720, ledger-default 50000,
    4 tasks with identical patterns) must NOT trigger any W1-W5."""
    cfg = _valid_base_config()
    cfg.refine_governor.max_partitions_per_refine = 128
    cfg.refine_governor.max_search_tasks_per_run = 10000
    cfg.queue.max_age_hours = 720.0
    # share_ledger_max_tracked stays at default 50000
    cfg.tasks = [
        TaskConfig(name=f"t{i}", enabled=True, patterns=Patterns(key_pattern="sk-[a-zA-Z0-9]{32}"))
        for i in range(1, 5)
    ]
    warnings = _warnings_for_config(cfg)
    w_hits = [w for w in warnings if any(w.startswith(f"W{i}:") for i in range(1, 6))]
    assert len(w_hits) == 0, f"Combat config form must not trigger W1-W5: {w_hits}"
