"""NEW-1: validator WARNING on shadow + registry/skip-on coupling.

Traceability: NEW-1 (02_critical_fixes.md), failure-handling spec §1.4 delta.

The shadow failure-handling mode combined with registry-write (skip_known != off)
creates a latent poisoning hazard: swallowed TransientFetchErrors write sticky
gathered_ok status that suppresses re-gathering for 168 hours.  The validator
MUST warn about this coupling without blocking config load.

House pattern: real Config objects, no mocks; warnings checked via
validator.warnings list (mirrors test_cql_lint.py, test_fcd_config.py).
No network.
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
    """A Config that passes the pre-existing global/task validation."""
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


def _warnings_for(failure_handling, skip_known):
    """Return validator warnings for the given mode coupling."""
    cfg = _valid_base_config()
    cfg.pipeline.failure_handling = failure_handling
    cfg.skip.skip_known = skip_known
    cfg.registry.enabled = True  # registry is the poisoning surface

    validator = ConfigValidator()
    with contextlib.suppress(ValueError):
        validator.validate(cfg)
    return list(validator.warnings)


# ---------------------------------------------------------------------------
# NEW-1-W1: shadow + skip_known=on MUST warn
# ---------------------------------------------------------------------------
def test_new1_w1_shadow_plus_skip_on_warns():
    """WHEN failure_handling=shadow AND skip.skip_known=on (registry-write
    enabled) THEN the validator emits a WARNING about the shadow poisoning
    hazard.  The config still loads (WARNING, not error)."""
    warnings = _warnings_for("shadow", "on")

    hits = [w for w in warnings if "shadow" in w.lower() and "skip" in w.lower()]
    assert hits, (
        f"expected a shadow+skip_on coupling warning, got: {warnings}"
    )
    # Warning names the hazard (sticky gathered_ok poisoning).
    assert any("gathered_ok" in w or "poison" in w.lower() or "sticky" in w.lower()
               for w in hits), f"warning must name the hazard: {hits}"


# ---------------------------------------------------------------------------
# NEW-1-W2: shadow + skip_known=shadow MUST warn (same hazard surface)
# ---------------------------------------------------------------------------
def test_new1_w2_shadow_plus_skip_shadow_warns():
    """WHEN failure_handling=shadow AND skip.skip_known=shadow THEN the same
    coupling hazard exists (registry writes are active in shadow mode too)."""
    warnings = _warnings_for("shadow", "shadow")

    hits = [w for w in warnings if "shadow" in w.lower() and "skip" in w.lower()]
    assert hits, (
        f"expected a shadow+skip_shadow coupling warning, got: {warnings}"
    )


# ---------------------------------------------------------------------------
# NEW-1-W3: strict + skip_known=on MUST NOT warn (strict writes failed, not gathered_ok)
# ---------------------------------------------------------------------------
def test_new1_w3_strict_plus_skip_on_no_warning():
    """WHEN failure_handling=strict AND skip.skip_known=on THEN no coupling
    warning: strict mode records visit_status='failed' on transient errors,
    so the sticky gathered_ok poisoning cannot occur."""
    warnings = _warnings_for("strict", "on")

    hits = [w for w in warnings if "shadow" in w.lower() and "skip" in w.lower()]
    assert not hits, (
        f"strict mode must NOT trigger the shadow+skip warning, got: {hits}"
    )


# ---------------------------------------------------------------------------
# NEW-1-W4: shadow + skip_known=off MUST NOT warn (no registry-write surface)
# ---------------------------------------------------------------------------
def test_new1_w4_shadow_plus_skip_off_no_warning():
    """WHEN failure_handling=shadow AND skip.skip_known=off THEN no coupling
    warning: with skip disabled, the sticky gathered_ok has no downstream
    consumer that would suppress re-gathering."""
    warnings = _warnings_for("shadow", "off")

    hits = [w for w in warnings if "shadow" in w.lower() and "skip" in w.lower()]
    assert not hits, (
        f"shadow+skip_off must NOT trigger the coupling warning, got: {hits}"
    )


# ---------------------------------------------------------------------------
# NEW-1-W5: legacy + skip_known=on MUST NOT warn (legacy is explicit rollback)
# ---------------------------------------------------------------------------
def test_new1_w5_legacy_plus_skip_on_no_warning():
    """WHEN failure_handling=legacy AND skip.skip_known=on THEN no coupling
    warning: legacy is the explicit pre-change rollback state, and the
    operator has acknowledged the risk by choosing it."""
    warnings = _warnings_for("legacy", "on")

    hits = [w for w in warnings if "shadow" in w.lower() and "skip" in w.lower()]
    assert not hits, (
        f"legacy+skip_on must NOT trigger the shadow+skip warning, got: {hits}"
    )
