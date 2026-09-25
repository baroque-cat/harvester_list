"""GatherConfig surface and cross-field validation (gather-transport S1-S3, S17).

RED at plan time: ``GatherConfig`` does not exist in ``config/schemas.py``, so the
import below fails - that ImportError IS the expected RED state (design D1).

House conventions: real Config objects, no mocks; loud-rejection pins mirror
``tests/test_tq_stage_backend.py::test_s3_*`` / ``test_s4_*``.
"""

import pytest

from config.schemas import (
    Config,
    CredentialsConfig,
    GatherConfig,
    LoadBalanceStrategy,
    StageConfig,
    TaskConfig,
)
from config.validator import ConfigValidator
from core.models import Condition, Patterns


def _valid_base_config():
    """A Config that passes the pre-existing global/task validation.

    A bare ``Config()`` always fails unrelated checks (no credentials, no tasks),
    which would mask the gather cross-field assertions below.  This supplies the
    minimum the existing validator requires without touching gather.
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
# gather-transport-S1
# ---------------------------------------------------------------------------
def test_s1_absent_section_resolves_to_the_measured_cheapest_transport():
    cfg = Config()

    # Absent section resolves to raw: measured 30.6x / 32.9x / 32.0x less traffic
    # per file than the rendered page, at equal or better latency (design Context).
    assert cfg.gather.transport == "raw"
    assert cfg.gather.max_payload_bytes == 8 * 1024 * 1024
    assert cfg.gather.max_refusal_wait_s > 0

    # Explicit html must still be selectable and normalize case/whitespace,
    # reproducing the pre-change fetching path (rollback = config flip).
    for raw_value, expected in (("html", "html"), ("HTML", "html"), (" raw ", "raw"), ("rest", "rest")):
        assert GatherConfig(transport=raw_value).transport == expected

    # The resolved transport reaches the fetch layer: html addresses the rendered
    # host, raw addresses the plain-content host.
    from search.client import gather_target_url

    blob = "https://github.com/o/r/blob/" + "a" * 40 + "/p/f.txt"
    assert gather_target_url(blob, transport="html").startswith("https://github.com/")
    assert gather_target_url(blob, transport="raw").startswith("https://raw.githubusercontent.com/")
    assert gather_target_url(blob, transport="rest").startswith("https://api.github.com/")


# ---------------------------------------------------------------------------
# gather-transport-S2
# ---------------------------------------------------------------------------
def test_s2_unknown_transport_name_fails_validation_loudly():
    with pytest.raises(ValueError) as exc:
        GatherConfig(transport="ftp")
    assert "ftp" in str(exc.value)
    assert "gather.transport" in str(exc.value)

    cfg = Config()
    cfg.gather.transport = "ftp"  # bypass __post_init__ to exercise the validator
    with pytest.raises(ValueError) as exc:
        ConfigValidator().validate(cfg)
    assert "ftp" in str(exc.value)


# ---------------------------------------------------------------------------
# gather-transport-S3
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "kwargs, field",
    [
        ({"max_payload_bytes": 0}, "max_payload_bytes"),
        ({"max_payload_bytes": -1}, "max_payload_bytes"),
        ({"max_refusal_wait_s": 0}, "max_refusal_wait_s"),
        ({"max_refusal_wait_s": -0.5}, "max_refusal_wait_s"),
    ],
)
def test_s3_non_positive_numeric_gather_fields_fail_validation_loudly(kwargs, field):
    with pytest.raises(ValueError) as exc:
        GatherConfig(**kwargs)
    assert field in str(exc.value)

    cfg = Config()
    for name, value in kwargs.items():
        setattr(cfg.gather, name, value)
    with pytest.raises(ValueError) as exc:
        ConfigValidator().validate(cfg)
    assert field in str(exc.value)


# ---------------------------------------------------------------------------
# gather-transport-S17
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("wait_cap", [300.0, 900.0])
def test_s17_contradictory_wait_configuration_is_rejected(wait_cap):
    """A refusal wait >= the queue visibility timeout guarantees a duplicate.

    ``storage/task_queue.py`` exposes no claim renewal, so a worker sleeping past
    ``claimed_until`` has its row re-delivered to another worker (design D5).
    The production constant that motivated this pin is
    ``GITHUB_CREDENTIAL_COOLDOWN_MAX = 900`` against ``visibility_timeout_s = 300``.
    """
    cfg = Config()
    cfg.queue.backend = "sqlite"
    cfg.queue.visibility_timeout_s = 300.0
    cfg.gather.max_refusal_wait_s = wait_cap

    with pytest.raises(ValueError) as exc:
        ConfigValidator().validate(cfg)
    message = str(exc.value)
    # both values named, per the scenario THEN clause
    assert "300" in message
    assert str(int(wait_cap)) in message
    assert "visibility" in message.lower()


def test_s17_strictly_smaller_wait_cap_validates_cleanly():
    cfg = _valid_base_config()
    cfg.queue.backend = "sqlite"
    cfg.queue.visibility_timeout_s = 300.0
    cfg.gather.max_refusal_wait_s = 60.0

    ConfigValidator().validate(cfg)  # must not raise

    # The invariant is backend-aware: under memory there is no visibility window,
    # so no cross-field constraint applies.
    cfg_mem = _valid_base_config()
    cfg_mem.queue.backend = "memory"
    cfg_mem.gather.max_refusal_wait_s = 900.0
    ConfigValidator().validate(cfg_mem)  # must not raise
