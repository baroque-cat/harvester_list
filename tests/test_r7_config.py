"""R7 — gather.share_fetch config surface pins (D9).

Traceability: /tmp/opencode/r7-plan.md §3 D9.

Three-position rollout flag: off | shadow | on, default off.  Byte-identical
message in both GatherConfig.__post_init__ and ConfigValidator._validate_gather_config.

House conventions: real Config objects, no mocks; mirror test_gt_config.py /
test_fcd_config.py patterns.
"""

import pytest

from config.schemas import Config, GatherConfig
from config.validator import ConfigValidator
from config.loader import ConfigLoader


EXPECTED_ERROR_MSG = "gather.share_fetch must be one of: off, shadow, on"


# ---------------------------------------------------------------------------
# C1 — default is "off" on a bare GatherConfig()
# ---------------------------------------------------------------------------
def test_c1_default_is_off():
    cfg = GatherConfig()
    assert cfg.share_fetch == "off"


# ---------------------------------------------------------------------------
# C2 — "shadow" and "on" accepted; case/whitespace normalised
# ---------------------------------------------------------------------------
def test_c2_valid_modes_accepted_and_normalised():
    assert GatherConfig(share_fetch="shadow").share_fetch == "shadow"
    assert GatherConfig(share_fetch="on").share_fetch == "on"
    assert GatherConfig(share_fetch=" ON ").share_fetch == "on"
    assert GatherConfig(share_fetch="  Shadow  ").share_fetch == "shadow"
    assert GatherConfig(share_fetch="OFF").share_fetch == "off"


# ---------------------------------------------------------------------------
# C3 — non-member string raises ValueError with exact message from BOTH sites
# ---------------------------------------------------------------------------
def test_c3_schema_rejects_invalid_mode():
    with pytest.raises(ValueError) as exc:
        GatherConfig(share_fetch="yes")
    assert str(exc.value) == EXPECTED_ERROR_MSG


def test_c3_validator_rejects_invalid_mode():
    cfg = Config()
    # Bypass __post_init__ by mutating after construction
    object.__setattr__(cfg.gather, "share_fetch", "yes")

    validator = ConfigValidator()
    try:
        validator.validate(cfg)
    except ValueError:
        pass

    assert EXPECTED_ERROR_MSG in validator.errors


# ---------------------------------------------------------------------------
# C4 — loader: explicit "shadow" parses, absent key yields "off"
# ---------------------------------------------------------------------------
def test_c4_loader_parses_share_fetch():
    loader = ConfigLoader()

    data_shadow = {"transport": "raw", "share_fetch": "shadow"}
    cfg_shadow = loader._parse_gather_config(data_shadow)
    assert cfg_shadow.share_fetch == "shadow"

    data_absent = {"transport": "raw"}
    cfg_absent = loader._parse_gather_config(data_absent)
    assert cfg_absent.share_fetch == "off"


# ---------------------------------------------------------------------------
# C5 — byte-identical message pin
# ---------------------------------------------------------------------------
def test_c5_byte_identical_message():
    # Schema path
    try:
        GatherConfig(share_fetch="bogus")
        assert False, "should have raised"
    except ValueError as exc:
        schema_msg = str(exc)

    # Validator path
    cfg = Config()
    object.__setattr__(cfg.gather, "share_fetch", "bogus")
    validator = ConfigValidator()
    try:
        validator.validate(cfg)
    except ValueError:
        pass

    assert schema_msg == EXPECTED_ERROR_MSG
    assert EXPECTED_ERROR_MSG in validator.errors
    assert schema_msg == EXPECTED_ERROR_MSG
