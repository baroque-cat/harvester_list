"""FileDatingConfig surface (file-commit-dating S7, S22).

RED at plan time: ``FileDatingConfig`` does not exist in ``config/schemas.py``,
``Config.file_dating`` is absent, and the loader has no ``file_dating`` dispatch
- the ImportError below IS the expected RED state (design D3/D4).

House conventions: real Config objects, no mocks; loud-rejection pins mirror
``tests/test_gt_config.py::test_s2_*`` / ``tests/test_cl_config.py``.
No network.
"""

import pytest

from config.schemas import (
    Config,
    CredentialsConfig,
    FileDatingConfig,
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


# ---------------------------------------------------------------------------
# Inert defaults: absent file_dating section yields fully-inert defaults
# ---------------------------------------------------------------------------
def test_absent_section_yields_inert_defaults():
    """WHEN no file_dating section is present THEN all fields resolve to their
    inert schema defaults (design: inert until explicitly enabled)."""
    cfg = Config()
    assert cfg.file_dating.enabled is False
    assert cfg.file_dating.transport == "rest"
    assert cfg.file_dating.scope == "keyed"
    assert cfg.file_dating.ttl_hours == 24.0
    assert cfg.file_dating.max_requests_per_run == 200
    assert cfg.file_dating.graphql_batch_size == 4
    assert cfg.file_dating.graphql_batch_max == 50


# ---------------------------------------------------------------------------
# file-commit-dating-S7: batch ceiling enforced
# ---------------------------------------------------------------------------
def test_s7_batch_size_above_ceiling_rejected_by_dataclass():
    """WHEN graphql_batch_size > graphql_batch_max THEN ValueError naming the
    field (design D3: the ceiling bounds the unmeasured cost)."""
    with pytest.raises(ValueError) as exc:
        FileDatingConfig(graphql_batch_size=100, graphql_batch_max=50)
    assert "graphql_batch_size" in str(exc.value)
    assert "graphql_batch_max" in str(exc.value)


def test_s7_batch_size_zero_rejected():
    """Batch size < 1 is rejected (a zero batch can never make progress)."""
    with pytest.raises(ValueError) as exc:
        FileDatingConfig(graphql_batch_size=0)
    assert "graphql_batch_size" in str(exc.value)


def test_s7_negative_batch_size_rejected():
    with pytest.raises(ValueError) as exc:
        FileDatingConfig(graphql_batch_size=-1)
    assert "graphql_batch_size" in str(exc.value)


def test_s7_valid_pair_loads():
    """A valid (size <= max) pair must construct without error."""
    fdc = FileDatingConfig(graphql_batch_size=4, graphql_batch_max=50)
    assert fdc.graphql_batch_size == 4
    assert fdc.graphql_batch_max == 50


def test_s7_batch_ceiling_enforced_via_loader(tmp_path):
    """The loader+validator path must also reject a batch_size above the ceiling."""
    from config.loader import ConfigLoader

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "global:\n"
        "  workspace: ./data\n"
        "  github_credentials:\n"
        "    sessions: ['s']\n"
        "    tokens: []\n"
        "    strategy: round_robin\n"
        "  user_agents: ['Mozilla/5.0']\n"
        "file_dating:\n"
        "  enabled: true\n"
        "  graphql_batch_size: 100\n"
        "  graphql_batch_max: 50\n"
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

    with pytest.raises(RuntimeError) as exc:
        ConfigLoader(str(cfg_path)).load()
    assert "graphql_batch_size" in str(exc.value)


# ---------------------------------------------------------------------------
# file-commit-dating-S22: invalid configuration fails loudly from BOTH paths
# ---------------------------------------------------------------------------
def test_s22_enabled_string_rejected_no_coercion():
    """``enabled: 'false'`` (a string) must NOT be coerced to True.

    ``bool("false") is True`` in Python, so coercion would silently invert the
    operator's intent.  Both paths must reject it.
    """
    # Direct dataclass construction
    with pytest.raises(ValueError) as exc:
        FileDatingConfig(enabled="false")
    assert "enabled" in str(exc.value)
    assert "boolean" in str(exc.value)

    # Loader+validator path
    cfg = _valid_base_config()
    cfg.file_dating.enabled = "false"  # bypass __post_init__
    with pytest.raises(ValueError) as exc:
        ConfigValidator().validate(cfg)
    assert "file_dating.enabled" in str(exc.value)


def test_s22_enabled_real_bool_accepted():
    """``enabled: yes`` parsed from YAML (a real bool) must be accepted."""
    fdc = FileDatingConfig(enabled=True)
    assert fdc.enabled is True

    fdc2 = FileDatingConfig(enabled=False)
    assert fdc2.enabled is False


def test_s22_transport_soap_rejected():
    with pytest.raises(ValueError) as exc:
        FileDatingConfig(transport="soap")
    assert "transport" in str(exc.value)

    cfg = _valid_base_config()
    cfg.file_dating.transport = "soap"
    with pytest.raises(ValueError) as exc:
        ConfigValidator().validate(cfg)
    assert "file_dating.transport" in str(exc.value)


def test_s22_scope_everything_rejected():
    with pytest.raises(ValueError) as exc:
        FileDatingConfig(scope="everything")
    assert "scope" in str(exc.value)

    cfg = _valid_base_config()
    cfg.file_dating.scope = "everything"
    with pytest.raises(ValueError) as exc:
        ConfigValidator().validate(cfg)
    assert "file_dating.scope" in str(exc.value)


def test_s22_ttl_hours_zero_rejected():
    with pytest.raises(ValueError) as exc:
        FileDatingConfig(ttl_hours=0)
    assert "ttl_hours" in str(exc.value)

    cfg = _valid_base_config()
    cfg.file_dating.ttl_hours = 0
    with pytest.raises(ValueError) as exc:
        ConfigValidator().validate(cfg)
    assert "ttl_hours" in str(exc.value)


def test_s22_max_requests_per_run_negative_rejected():
    with pytest.raises(ValueError) as exc:
        FileDatingConfig(max_requests_per_run=-1)
    assert "max_requests_per_run" in str(exc.value)

    cfg = _valid_base_config()
    cfg.file_dating.max_requests_per_run = -1
    with pytest.raises(ValueError) as exc:
        ConfigValidator().validate(cfg)
    assert "max_requests_per_run" in str(exc.value)


# ---------------------------------------------------------------------------
# S22 via loader: each case loaded from YAML, errors collected
# ---------------------------------------------------------------------------
def test_s22_loader_enabled_string_rejected(tmp_path):
    """YAML ``enabled: 'false'`` (a string) must fail the loader path."""
    from config.loader import ConfigLoader

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "global:\n"
        "  workspace: ./data\n"
        "  github_credentials:\n"
        "    sessions: ['s']\n"
        "    tokens: []\n"
        "    strategy: round_robin\n"
        "  user_agents: ['Mozilla/5.0']\n"
        "file_dating:\n"
        "  enabled: 'false'\n"
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

    with pytest.raises(RuntimeError) as exc:
        ConfigLoader(str(cfg_path)).load()
    assert "file_dating.enabled" in str(exc.value)


def test_s22_loader_enabled_yes_accepted(tmp_path):
    """YAML ``enabled: yes`` (a real bool) must load cleanly."""
    from config.loader import ConfigLoader

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "global:\n"
        "  workspace: ./data\n"
        "  github_credentials:\n"
        "    sessions: ['s']\n"
        "    tokens: []\n"
        "    strategy: round_robin\n"
        "  user_agents: ['Mozilla/5.0']\n"
        "file_dating:\n"
        "  enabled: yes\n"
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
    assert cfg.file_dating.enabled is True


def test_s22_loader_transport_soap_rejected(tmp_path):
    from config.loader import ConfigLoader

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "global:\n"
        "  workspace: ./data\n"
        "  github_credentials:\n"
        "    sessions: ['s']\n"
        "    tokens: []\n"
        "    strategy: round_robin\n"
        "  user_agents: ['Mozilla/5.0']\n"
        "file_dating:\n"
        "  transport: soap\n"
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

    with pytest.raises(RuntimeError) as exc:
        ConfigLoader(str(cfg_path)).load()
    assert "file_dating.transport" in str(exc.value)


def test_s22_loader_scope_everything_rejected(tmp_path):
    from config.loader import ConfigLoader

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "global:\n"
        "  workspace: ./data\n"
        "  github_credentials:\n"
        "    sessions: ['s']\n"
        "    tokens: []\n"
        "    strategy: round_robin\n"
        "  user_agents: ['Mozilla/5.0']\n"
        "file_dating:\n"
        "  scope: everything\n"
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

    with pytest.raises(RuntimeError) as exc:
        ConfigLoader(str(cfg_path)).load()
    assert "file_dating.scope" in str(exc.value)


def test_s22_loader_ttl_hours_zero_rejected(tmp_path):
    from config.loader import ConfigLoader

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "global:\n"
        "  workspace: ./data\n"
        "  github_credentials:\n"
        "    sessions: ['s']\n"
        "    tokens: []\n"
        "    strategy: round_robin\n"
        "  user_agents: ['Mozilla/5.0']\n"
        "file_dating:\n"
        "  ttl_hours: 0\n"
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

    with pytest.raises(RuntimeError) as exc:
        ConfigLoader(str(cfg_path)).load()
    assert "ttl_hours" in str(exc.value)


def test_s22_loader_max_requests_per_run_negative_rejected(tmp_path):
    from config.loader import ConfigLoader

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "global:\n"
        "  workspace: ./data\n"
        "  github_credentials:\n"
        "    sessions: ['s']\n"
        "    tokens: []\n"
        "    strategy: round_robin\n"
        "  user_agents: ['Mozilla/5.0']\n"
        "file_dating:\n"
        "  max_requests_per_run: -1\n"
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

    with pytest.raises(RuntimeError) as exc:
        ConfigLoader(str(cfg_path)).load()
    assert "max_requests_per_run" in str(exc.value)


# ---------------------------------------------------------------------------
# Design D4: github_graphql bucket under ratelimits parses without code change
# ---------------------------------------------------------------------------
def test_d4_github_graphql_ratelimit_bucket_parses():
    """A ``ratelimits`` section containing a ``github_graphql`` bucket must
    parse into a RateLimitConfig with the given base_rate/burst_limit.

    This pins design D4's requirement that the new basket is configurable by
    name through the existing generic ratelimits mechanism.
    """
    from config.loader import ConfigLoader

    loader = ConfigLoader("/dev/null")
    data = {
        "github_graphql": {
            "base_rate": 0.5,
            "burst_limit": 2,
            "adaptive": True,
        }
    }
    parsed = loader._parse_rate_limits(data)
    assert "github_graphql" in parsed
    assert parsed["github_graphql"].base_rate == 0.5
    assert parsed["github_graphql"].burst_limit == 2
    assert parsed["github_graphql"].adaptive is True


# ---------------------------------------------------------------------------
# Cross-section warning: enabled but registry disabled
# ---------------------------------------------------------------------------
def test_warning_when_enabled_but_registry_disabled():
    """WHEN file_dating.enabled is true but registry.enabled is false THEN a
    warning is emitted (dating has no durable cache and would re-fetch)."""
    import logging

    cfg = _valid_base_config()
    cfg.file_dating.enabled = True
    cfg.registry.enabled = False

    validator = ConfigValidator()
    validator.validate(cfg)

    assert any(
        "file_dating" in w and "registry" in w
        for w in validator.warnings
    ), f"Expected a file_dating/registry warning, got: {validator.warnings}"
