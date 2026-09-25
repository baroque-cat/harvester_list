"""Early cooldown release + escalation ladder (credential-liveness S11, S12, S13).

RED at plan time: ``tools.state.CredentialsExhausted`` /
``credential_liveness_metrics`` do not exist and ``mark_success`` pops only
expired entries - the import failure below IS the expected RED state (design D4).

No network, no real sleeping: the cooldown clock is a monkeypatched
``time.time``.
"""

import time

import pytest

from tools import state as state_module
from tools.state import (  # RED driver: absent pre-fix
    credential_liveness_metrics,
    github_credential_state,
)

SERVICE = "github_api"


@pytest.fixture(autouse=True)
def _clean_state():
    for key in list(github_credential_state._items):
        github_credential_state._items.pop(key, None)
    yield
    for key in list(github_credential_state._items):
        github_credential_state._items.pop(key, None)


class _Clock:
    """Frozen wall clock; advance() moves time without sleeping."""

    def __init__(self, monkeypatch, start=1_000_000.0):
        self.now = start
        monkeypatch.setattr(time, "time", lambda: self.now)

    def advance(self, seconds):
        self.now += seconds


# ---------------------------------------------------------------------------
# credential-liveness-S11
# ---------------------------------------------------------------------------
def test_s11_successful_request_frees_a_benched_credential(monkeypatch):
    """WHEN a credential under an UNEXPIRED cooldown succeeds and early_release
    is on THEN it is immediately selectable again and the release is counted."""
    _Clock(monkeypatch)
    monkeypatch.setattr(state_module, "_early_release_enabled", lambda: True)

    github_credential_state.mark_limited(SERVICE, "t1", wait=600)
    assert github_credential_state.is_cooling(SERVICE, "t1")

    before = credential_liveness_metrics()["early_releases"]
    github_credential_state.mark_success(SERVICE, "t1")

    assert not github_credential_state.is_cooling(SERVICE, "t1")  # freed at once
    assert credential_liveness_metrics()["early_releases"] == before + 1


# ---------------------------------------------------------------------------
# credential-liveness-S12
# ---------------------------------------------------------------------------
def test_s12_rollback_flag_restores_expired_only_release(monkeypatch):
    """WHEN early_release is off THEN a success does NOT clear an unexpired
    cooldown (byte-for-byte pre-change behavior), but an expired one still
    clears."""
    clock = _Clock(monkeypatch)
    monkeypatch.setattr(state_module, "_early_release_enabled", lambda: False)

    github_credential_state.mark_limited(SERVICE, "t1", wait=60)
    github_credential_state.mark_success(SERVICE, "t1")
    assert github_credential_state.is_cooling(SERVICE, "t1"), "unexpired cooldown must survive a success in rollback mode"

    clock.advance(61)
    github_credential_state.mark_success(SERVICE, "t1")
    assert not github_credential_state.is_cooling(SERVICE, "t1"), "expired cooldown still clears, as before"


# ---------------------------------------------------------------------------
# credential-liveness-S13
# ---------------------------------------------------------------------------
def test_s13_repeated_limits_still_escalate_and_release_restarts_the_ladder(monkeypatch):
    """WHEN a credential is limited again WITHOUT an intervening success THEN
    the ladder escalates 60->120->240 (unchanged schedule); a success-release
    in between restarts the ladder from the minimum."""
    _Clock(monkeypatch)
    monkeypatch.setattr(state_module, "_early_release_enabled", lambda: True)

    assert github_credential_state.mark_limited(SERVICE, "t1") == 60.0
    assert github_credential_state.mark_limited(SERVICE, "t1") == 120.0
    assert github_credential_state.mark_limited(SERVICE, "t1") == 240.0

    github_credential_state.mark_success(SERVICE, "t1")  # proven working -> released
    assert not github_credential_state.is_cooling(SERVICE, "t1")

    assert github_credential_state.mark_limited(SERVICE, "t1") == 60.0, \
        "a fresh entry after early release restarts at the minimum, not at the old escalation"
