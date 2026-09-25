"""Credential-liveness config surface (credential-liveness S5, S6-config, S21, S22).

RED at plan time: ``CredentialLivenessConfig`` does not exist in
``config/schemas.py``, ``Config.credential_liveness`` is absent, and the loader
still parses the four dead adaptive coefficients - the ImportError below IS the
expected RED state (design D2/D9).

House conventions: real Config objects, no mocks; loud-rejection pins mirror
``tests/test_gt_config.py::test_s2_*`` (pytest.raises(ValueError) + message
asserts).  No network.
"""

import logging

import pytest

from config.schemas import (
    Config,
    CredentialLivenessConfig,  # RED driver: absent pre-fix
    CredentialsConfig,
    LoadBalanceStrategy,
    StageConfig,
    TaskConfig,
)
from config.validator import ConfigValidator
from core.models import Condition, Patterns


def _valid_base_config():
    """A Config that passes the pre-existing global/task validation (mirrors
    tests/test_gt_config.py::_valid_base_config)."""
    cfg = Config()
    cfg.global_config.github_credentials = CredentialsConfig(
        sessions=["session"], tokens=["token"], strategy=LoadBalanceStrategy.ROUND_ROBIN
    )
    cfg.tasks = [
        TaskConfig(
            name="probe",
            enabled=True,
            provider_type="openai_like",
            use_api=False,
            stages=StageConfig(),
            patterns=Patterns(key_pattern="sk-x"),
            conditions=[Condition(query="/sk-x/", patterns=Patterns(key_pattern="sk-x"))],
        )
    ]
    return cfg


# ---------------------------------------------------------------------------
# credential-liveness-S5
# ---------------------------------------------------------------------------
def test_s5_cross_section_invariant_enforced_at_load_time():
    """WHEN sqlite backend and max_wait_s >= visibility_timeout_s THEN loud
    validation error naming both keys (no in-task sleep may outlive the claim
    window while the queue has no claim-renewal API)."""
    cfg = _valid_base_config()

    # defaults are the new safe behavior (design D2)
    assert cfg.credential_liveness.wait_mode == "bounded"
    assert cfg.credential_liveness.max_wait_s == 60.0
    assert cfg.credential_liveness.early_release is True
    assert cfg.credential_liveness.emergency_threshold == 3

    cfg.queue.backend = "sqlite"
    cfg.queue.visibility_timeout_s = 300.0
    cfg.credential_liveness.max_wait_s = 300.0  # == visibility: duplicate-execution hazard

    with pytest.raises(ValueError) as exc:
        ConfigValidator().validate(cfg)
    msg = str(exc.value)
    assert "max_wait_s" in msg
    assert "visibility_timeout_s" in msg

    # strictly below the window passes
    cfg.credential_liveness.max_wait_s = 60.0
    ConfigValidator().validate(cfg)  # no raise

    # loud dataclass-level rejection of nonsense, GatherConfig precedent
    with pytest.raises(ValueError):
        CredentialLivenessConfig(wait_mode="forever")
    with pytest.raises(ValueError):
        CredentialLivenessConfig(max_wait_s=0)
    with pytest.raises(ValueError):
        CredentialLivenessConfig(emergency_threshold=0)


# ---------------------------------------------------------------------------
# credential-liveness-S6 (config-load half; selector parity lives in
# tests/test_cl_selector.py)
# ---------------------------------------------------------------------------
def test_s6_blocking_rollback_mode_warns_loudly_under_sqlite(caplog):
    """WHEN wait_mode=blocking under the sqlite backend THEN config loads (the
    rollback flip must never crash) but a loud WARNING names the deliberately
    re-accepted duplicate-execution hazard; the bounded-mode invariant does not
    apply to the legacy mode."""
    cfg = _valid_base_config()
    cfg.queue.backend = "sqlite"
    cfg.queue.visibility_timeout_s = 300.0
    cfg.credential_liveness.wait_mode = "blocking"
    cfg.credential_liveness.max_wait_s = 5000.0  # ignored by legacy semantics

    with caplog.at_level(logging.WARNING):
        ConfigValidator().validate(cfg)  # must NOT raise

    assert any(
        r.levelno >= logging.WARNING and "blocking" in r.getMessage().lower()
        for r in caplog.records
    ), "blocking mode under sqlite must warn loudly at load time"


# ---------------------------------------------------------------------------
# credential-liveness-S21
# ---------------------------------------------------------------------------
def test_s21_dead_keys_warn_once_and_are_ignored(caplog, tmp_path):
    """WHEN an operator config still carries the removed adaptive coefficients
    THEN loading succeeds, each present key produces exactly one WARNING naming
    it, and the parsed rate-limit objects no longer expose the dead fields."""
    from config.loader import ConfigLoader
    from core.models import RateLimitConfig

    # schema-level removal (design D9)
    for dead in ("backoff_factor", "recovery_factor", "max_rate_multiplier", "min_rate_multiplier"):
        assert not hasattr(RateLimitConfig(), dead)

    loader = ConfigLoader(str(tmp_path / "unused.yaml"))
    data = {
        "github_api": {
            "base_rate": 0.15,
            "burst_limit": 5,
            "adaptive": True,
            "backoff_factor": 0.2,
            "recovery_factor": 1.5,
            "max_rate_multiplier": 5.0,
            "min_rate_multiplier": 0.01,
        }
    }
    with caplog.at_level(logging.WARNING):
        parsed = loader._parse_rate_limits(data)

    live = parsed["github_api"]
    assert live.base_rate == 0.15 and live.burst_limit == 5 and live.adaptive is True
    for dead in ("backoff_factor", "recovery_factor", "max_rate_multiplier", "min_rate_multiplier"):
        named = [r for r in caplog.records if dead in r.getMessage()]
        assert len(named) == 1, f"{dead}: exactly one loud ignore-warning per load"
        assert named[0].levelno >= logging.WARNING


def test_s21b_dead_keys_warn_once_per_load_across_all_blocks(caplog, tmp_path):
    """Spec S21 promises exactly one WARNING per key **per load**, not per
    occurrence: a stale operator config carries the same dead key in the global
    ``ratelimits`` block and in every task-level ``rate_limit`` block (the pre-fix
    live config had 16 such lines = 4 tasks x 4 keys). Design D21."""
    from config.loader import ConfigLoader

    dead_block = {
        "base_rate": 2.0,
        "burst_limit": 10,
        "adaptive": True,
        "backoff_factor": 0.2,
        "recovery_factor": 1.5,
        "max_rate_multiplier": 5.0,
        "min_rate_multiplier": 0.01,
    }
    dead_keys = ("backoff_factor", "recovery_factor", "max_rate_multiplier", "min_rate_multiplier")

    loader = ConfigLoader(str(tmp_path / "unused.yaml"))
    with caplog.at_level(logging.WARNING):
        # two global blocks + one task-level block, all in the SAME load
        loader._parse_rate_limits({"github_api": dict(dead_block), "github_web": dict(dead_block)})
        loader._warn_dead_rate_limit_keys(dict(dead_block))

    for dead in dead_keys:
        named = [r for r in caplog.records if dead in r.getMessage()]
        assert len(named) == 1, f"{dead}: one WARNING per load, got {len(named)}"

    # The dedupe must not be process-global: a fresh loader warns again.
    caplog.clear()
    loader2 = ConfigLoader(str(tmp_path / "unused2.yaml"))
    with caplog.at_level(logging.WARNING):
        loader2._parse_rate_limits({"github_api": dict(dead_block)})
    assert len([r for r in caplog.records if "backoff_factor" in r.getMessage()]) == 1


# ---------------------------------------------------------------------------
# credential-liveness-S22
# ---------------------------------------------------------------------------
def test_s22_clean_config_loads_silently(caplog, tmp_path):
    """WHEN no removed keys are present THEN no acceptance-period warning."""
    from config.loader import ConfigLoader

    loader = ConfigLoader(str(tmp_path / "unused.yaml"))
    with caplog.at_level(logging.WARNING):
        parsed = loader._parse_rate_limits({"github_api": {"base_rate": 0.15, "burst_limit": 5}})

    assert parsed["github_api"].base_rate == 0.15
    assert not any(
        "ignored" in r.getMessage().lower() for r in caplog.records
    ), "clean configs must not see the acceptance-period warning"
