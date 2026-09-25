"""Bounded credential selector (credential-liveness S1, S2, S3, S4, S6-selector, S9).

RED at plan time: ``tools.state.CredentialsExhausted`` and the
``credential_liveness_metrics`` collector do not exist, and
``tools.credential._liveness_settings`` (the config seam, design D2) is absent
- the ImportError below IS the expected RED state; the monkeypatched seam then
fails with AttributeError until the selector rewrite lands.

No network and no real sleeping: ``time.sleep``/``time.monotonic``/``time.time``
are replaced by a fake clock that advances ONLY through recorded sleeps, so an
unbounded wait loop trips the sentinel instead of hanging the suite.
"""

import logging
import time

import pytest

from tools import credential as credential_module
from tools.credential import Credentials
from tools.state import (  # RED driver: CredentialsExhausted absent pre-fix
    CredentialsExhausted,
    credential_liveness_metrics,
    github_credential_state,
)

SERVICE_WEB = "github_web"


class _Settings:
    """Stand-in for the resolved CredentialLivenessConfig (design D2 seam)."""

    def __init__(self, wait_mode="bounded", max_wait_s=60.0, early_release=True, emergency_threshold=3):
        self.wait_mode = wait_mode
        self.max_wait_s = max_wait_s
        self.early_release = early_release
        self.emergency_threshold = emergency_threshold


class _Clock:
    def __init__(self):
        self.now = 1_000_000.0
        self.sleeps = []

    def install(self, monkeypatch):
        def fake_sleep(seconds):
            self.sleeps.append(seconds)
            self.now += seconds
            if len(self.sleeps) > 8:
                pytest.fail("selector entered an unbounded wait loop")

        monkeypatch.setattr(time, "sleep", fake_sleep)
        monkeypatch.setattr(time, "monotonic", lambda: self.now)
        monkeypatch.setattr(time, "time", lambda: self.now)
        return self.sleeps


@pytest.fixture(autouse=True)
def _clean_state():
    for key in list(github_credential_state._items):
        github_credential_state._items.pop(key, None)
    yield
    for key in list(github_credential_state._items):
        github_credential_state._items.pop(key, None)


def _creds(sessions=("s1",), tokens=()):
    return Credentials(sessions=list(sessions), tokens=list(tokens), strategy="round_robin")


# ---------------------------------------------------------------------------
# credential-liveness-S1 (preserved-behavior regression guard)
# ---------------------------------------------------------------------------
def test_s1_available_credential_returns_immediately(monkeypatch):
    """WHEN a pooled credential is not cooling THEN it is returned without any
    sleep and without touching cooldown state."""
    clock = _Clock().install(monkeypatch)
    monkeypatch.setattr(credential_module, "_liveness_settings", lambda: _Settings())

    assert _creds().get_session() == "s1"
    assert clock == []  # no sleep at all
    assert not github_credential_state._items


# ---------------------------------------------------------------------------
# credential-liveness-S2
# ---------------------------------------------------------------------------
def test_s2_short_cooldown_is_waited_out_within_budget(monkeypatch, caplog):
    """WHEN the only session is cooling but releases within the budget THEN the
    selector sleeps once, returns it, and logs the episode exactly once."""
    clock = _Clock().install(monkeypatch)
    monkeypatch.setattr(credential_module, "_liveness_settings", lambda: _Settings(max_wait_s=60.0))

    probes = {"n": 0}

    def fake_is_cooling(service, credential):
        probes["n"] += 1
        return probes["n"] <= 1  # cooling on first probe, released afterwards

    monkeypatch.setattr(github_credential_state, "is_cooling", fake_is_cooling)
    monkeypatch.setattr(github_credential_state, "next_wait", lambda service, items: 0.05)

    with caplog.at_level(logging.WARNING):
        assert _creds().get_session() == "s1"

    assert clock == [0.05]  # single bounded sleep on the cooldown timer
    episode_logs = [r for r in caplog.records if "cooling down" in r.getMessage()]
    assert len(episode_logs) == 1  # one WARNING per episode, not per iteration


# ---------------------------------------------------------------------------
# credential-liveness-S3
# ---------------------------------------------------------------------------
def test_s3_long_cooldown_exhausts_the_budget(monkeypatch):
    """WHEN the earliest release lies beyond max_wait_s THEN the typed
    exhaustion signal is raised after sleeping no longer than the budget."""
    clock = _Clock().install(monkeypatch)
    monkeypatch.setattr(credential_module, "_liveness_settings", lambda: _Settings(max_wait_s=0.2))
    monkeypatch.setattr(github_credential_state, "is_cooling", lambda service, credential: True)
    monkeypatch.setattr(github_credential_state, "next_wait", lambda service, items: 1000.0)

    before = credential_liveness_metrics()["exhausted_episodes"]
    with pytest.raises(CredentialsExhausted) as exc:
        _creds().get_session()

    e = exc.value
    assert e.service == SERVICE_WEB
    assert e.reason == "budget_spent"
    assert e.wait_estimate_s == 1000.0  # best-known release time, for the deferral wait
    assert sum(clock) <= 0.2 + 1e-6  # never slept past the budget
    assert credential_liveness_metrics()["exhausted_episodes"] == before + 1


# ---------------------------------------------------------------------------
# credential-liveness-S4
# ---------------------------------------------------------------------------
def test_s4_non_positive_wait_anomaly_fails_fast(monkeypatch):
    """WHEN everything is cooling and the earliest-release probe is
    non-positive THEN exhaustion is raised immediately - no hot spin."""
    clock = _Clock().install(monkeypatch)
    monkeypatch.setattr(credential_module, "_liveness_settings", lambda: _Settings(max_wait_s=60.0))
    monkeypatch.setattr(github_credential_state, "is_cooling", lambda service, credential: True)
    monkeypatch.setattr(github_credential_state, "next_wait", lambda service, items: 0.0)

    with pytest.raises(CredentialsExhausted) as exc:
        _creds().get_session()

    assert exc.value.reason == "spin_anomaly"
    assert clock == []  # the pre-change 0.1s spin loop is gone


# ---------------------------------------------------------------------------
# credential-liveness-S6 (selector half: blocking rollback parity)
# ---------------------------------------------------------------------------
def test_s6_blocking_mode_restores_legacy_unbounded_waiting(monkeypatch):
    """WHEN wait_mode=blocking THEN the selector ignores the budget and waits
    out the cooldown exactly like the pre-change code (config-flip rollback)."""
    clock = _Clock().install(monkeypatch)
    monkeypatch.setattr(
        credential_module, "_liveness_settings", lambda: _Settings(wait_mode="blocking", max_wait_s=0.001)
    )

    probes = {"n": 0}

    def fake_is_cooling(service, credential):
        probes["n"] += 1
        return probes["n"] <= 2  # released only after TWO waits - way over any budget

    monkeypatch.setattr(github_credential_state, "is_cooling", fake_is_cooling)
    monkeypatch.setattr(github_credential_state, "next_wait", lambda service, items: 0.05)

    assert _creds().get_session() == "s1"  # legacy: waits, never raises
    assert clock == [0.05, 0.05]


# ---------------------------------------------------------------------------
# credential-liveness-S9
# ---------------------------------------------------------------------------
def test_s9_consecutive_full_bench_trips_raise_a_bounded_emergency(monkeypatch, caplog):
    """WHEN exhaustion episodes on one service reach emergency_threshold
    consecutively THEN exactly one ERROR per trip is logged, the emergency
    counter increments, and the selector keeps failing fast (no pipeline
    death - fail-open doctrine)."""
    _Clock().install(monkeypatch)
    monkeypatch.setattr(
        credential_module, "_liveness_settings", lambda: _Settings(max_wait_s=0.0, emergency_threshold=2)
    )
    monkeypatch.setattr(github_credential_state, "is_cooling", lambda service, credential: True)
    monkeypatch.setattr(github_credential_state, "next_wait", lambda service, items: 900.0)

    creds = _creds()
    before = credential_liveness_metrics()["emergency_trips"]

    with caplog.at_level(logging.ERROR):
        for _ in range(2):
            with pytest.raises(CredentialsExhausted):
                creds.get_session()

    assert credential_liveness_metrics()["emergency_trips"] == before + 1
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1  # one loud ERROR per trip, not per episode
    assert SERVICE_WEB in errors[0].getMessage()

    # a third episode keeps raising (fail-open: defer, never die)
    with pytest.raises(CredentialsExhausted):
        creds.get_session()
