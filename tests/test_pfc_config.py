"""Configuration surface for the provider-refusal policy (PRT-S7 config halves,
PRT-S14).

RED at plan time: there is no `provider:` section, no `ProviderConfig`, and the
validator knows nothing about a refusal-wait cap - so every assertion below fails
on a missing attribute rather than on a wrong value.

House conventions (R0 doctrine, `tests/test_gt_config.py`): policy is visible in
config and never implied by a default; a non-boolean or non-positive value is
rejected **loudly** in both `__post_init__` and `ConfigValidator`; rollback is a
flag flip with no code change and no data migration.
"""

import pytest

from config.schemas import (
    Config,
    CredentialsConfig,
    LoadBalanceStrategy,
    ProviderConfig,
    StageConfig,
    TaskConfig,
)
from config.validator import ConfigValidator
from core.models import Condition, Patterns


def _valid_base_config():
    """A Config that passes the pre-existing global/task validation.

    A bare ``Config()`` always fails unrelated checks (no credentials, no tasks),
    which would mask the provider assertions below.
    """
    cfg = Config()
    cfg.global_config.github_credentials = CredentialsConfig(
        sessions=["session"], tokens=[], strategy=LoadBalanceStrategy.ROUND_ROBIN
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
# PRT-S7 - the durability invariant WAIT_CAP < visibility_timeout_s
# ---------------------------------------------------------------------------
def test_s7_defaults_are_the_fixed_behavior_and_the_cap_is_bounded():
    cfg = Config()
    assert cfg.provider.classify_refusals is True, "default must be the fixed behavior"
    assert cfg.provider.max_refusal_wait_s == pytest.approx(60.0)
    assert cfg.provider.max_refusal_wait_s > 0
    # The invariant holds against the shipped queue defaults without any tuning.
    assert cfg.provider.max_refusal_wait_s < cfg.queue.visibility_timeout_s


def test_s7_cap_not_below_the_visibility_window_is_rejected_loudly():
    """Under `queue.backend: sqlite` a cap that reaches the visibility window
    would let a claimed row be re-delivered mid-sleep (no claim-renewal API), so
    the pair is rejected at load time rather than discovered during a run."""
    cfg = _valid_base_config()
    cfg.queue.backend = "sqlite"
    cfg.queue.visibility_timeout_s = 300
    cfg.provider.max_refusal_wait_s = 300.0

    with pytest.raises(ValueError) as exc:
        ConfigValidator().validate(cfg)

    message = str(exc.value)
    assert "provider.max_refusal_wait_s" in message
    assert "visibility_timeout_s" in message

    # Strictly below is accepted.
    cfg.provider.max_refusal_wait_s = 299.0
    ConfigValidator().validate(cfg)  # must not raise


@pytest.mark.parametrize("value", [0, -1, -0.5])
def test_s7_non_positive_cap_fails_in_both_guards(value):
    with pytest.raises(ValueError, match="max_refusal_wait_s"):
        ProviderConfig(max_refusal_wait_s=value)

    cfg = _valid_base_config()
    cfg.provider.max_refusal_wait_s = value  # bypass __post_init__ to reach the validator
    with pytest.raises(ValueError) as exc:
        ConfigValidator().validate(cfg)
    assert "max_refusal_wait_s" in str(exc.value)


# ---------------------------------------------------------------------------
# PRT-S14
# ---------------------------------------------------------------------------
def test_s14_non_boolean_flag_is_rejected_loudly_without_coercion():
    """A truthy string must not silently select a behavioral contract."""
    with pytest.raises(ValueError, match="classify_refusals"):
        ProviderConfig(classify_refusals="yes")

    cfg = _valid_base_config()
    cfg.provider.classify_refusals = "yes"
    with pytest.raises(ValueError) as exc:
        ConfigValidator().validate(cfg)

    message = str(exc.value)
    assert "classify_refusals" in message
    assert "yes" in message, "the offending value is named, not just the key"


def test_s14_flag_is_a_real_configuration_surface(tmp_path):
    """Parsed from the `provider:` section as a genuine boolean, and `false` is a
    supported rollback position rather than a hazard - so it earns no warning."""
    from config.loader import ConfigLoader

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        # A loader-valid configuration: ``load()`` runs the full validator, which
        # (independently of provider) requires a credential, a user agent and an
        # enabled task.
        "global:\n"
        "  workspace: ./data\n"
        "  github_credentials:\n"
        "    sessions: ['s']\n"
        "    tokens: []\n"
        "    strategy: round_robin\n"
        "  user_agents: ['Mozilla/5.0']\n"
        "provider:\n"
        "  classify_refusals: false\n"
        "  max_refusal_wait_s: 45.0\n"
        "tasks:\n"
        "  - name: probe\n"
        "    enabled: true\n"
        "    provider_type: openai_like\n"
        "    use_api: false\n"
        "    stages:\n"
        "      search: true\n"
        "      gather: true\n"
        "      check: true\n"
        "      inspect: true\n"
        "    patterns:\n"
        "      key_pattern: 'sk-x'\n"
        "    conditions:\n"
        "      - query: '/sk-x/'\n",
        encoding="utf-8",
    )

    cfg = ConfigLoader(str(cfg_path)).load()
    assert cfg.provider.classify_refusals is False
    assert cfg.provider.max_refusal_wait_s == pytest.approx(45.0)
