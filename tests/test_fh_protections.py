"""Preserved protections - regression guards (fix-silent-losses, amended by
fix-credential-liveness).

Traceability: failure-handling-S16 was SUPERSEDED by fix-credential-liveness
FH-S2: the delta spec rewrites the full-pool-cooldown scenario from "blocks
until the earliest cooldown release" to "waits at most the configured budget,
then DEFERS" - the obsolete pin
``test_s16_full_pool_cooldown_waits_never_drops`` encoded the pre-change
unbounded-blocking contract and is deliberately REPLACED here (never weakened):
part 1 keeps the waits-within-budget guarantee, part 2 pins the typed
exhaustion beyond it (stage-side deferral accounting lives in
tests/test_cl_stage_defer.py S7/S8). Plus the auxiliary guard for the backoff
schedule supporting S15 (canonical S15 test lives in
tests/test_fh_stage_modes.py); the escalation schedule itself is UNCHANGED by
fix-credential-liveness, so that guard survives as-is. S17 restart-recovery
is Manual: it needs a full application boot/shutdown cycle - see tests.md.

No network: credential state and sleeps are monkeypatched.
"""

import time

import pytest

from tools import credential as credential_module
from tools.credential import Credentials
from tools.state import (  # RED driver: CredentialsExhausted absent pre-fix
    CredentialsExhausted,
    GithubCredentialLimited,
    github_credential_state,
)


class _Settings:
    """Stand-in for the resolved CredentialLivenessConfig (design D2 seam)."""

    def __init__(self, wait_mode="bounded", max_wait_s=60.0, early_release=True, emergency_threshold=3):
        self.wait_mode = wait_mode
        self.max_wait_s = max_wait_s
        self.early_release = early_release
        self.emergency_threshold = emergency_threshold


# ---------------------------------------------------------------------------
# failure-handling FH-S2 (full-pool cooldown defers, never drops)
# ---------------------------------------------------------------------------
def test_fh_s2_full_pool_cooldown_defers_never_drops(monkeypatch):
    """Part 1: release within budget -> the selector waits it out and proceeds
    (the never-drops guarantee is preserved). Part 2: release beyond budget ->
    typed exhaustion instead of an unbounded in-claim sleep; the caller defers
    (pinned in test_cl_stage_defer.py), so the task is never dropped and never
    completed-empty by this mechanism."""
    creds = Credentials(sessions=["s1"], tokens=[], strategy="round_robin")

    # Part 1 - within budget: waits out the cooldown and proceeds.
    monkeypatch.setattr(credential_module, "_liveness_settings", lambda: _Settings(max_wait_s=60.0))
    cooling_calls = {"n": 0}

    def fake_is_cooling(service, credential):
        cooling_calls["n"] += 1
        return cooling_calls["n"] <= 1  # cooling on first probe, released afterwards

    slept = []
    monkeypatch.setattr(github_credential_state, "is_cooling", fake_is_cooling)
    monkeypatch.setattr(github_credential_state, "next_wait", lambda service, items: 0.05)
    monkeypatch.setattr(time, "sleep", lambda seconds: slept.append(seconds))

    got = creds.get_session()

    assert got == "s1"
    assert slept and slept[0] == 0.05  # waited on the cooldown timer, did not give up

    # Part 2 - beyond budget: typed exhaustion, never an unbounded block.
    monkeypatch.setattr(credential_module, "_liveness_settings", lambda: _Settings(max_wait_s=0.0))
    monkeypatch.setattr(github_credential_state, "is_cooling", lambda service, credential: True)
    monkeypatch.setattr(github_credential_state, "next_wait", lambda service, items: 900.0)

    with pytest.raises(CredentialsExhausted):
        creds.get_session()
    assert sum(slept) <= 0.05 + 1e-6, "no additional sleep was spent chasing the 900s cooldown"


# ---------------------------------------------------------------------------
# Auxiliary guard supporting failure-handling-S15 (backoff schedule untouched)
# ---------------------------------------------------------------------------
def test_aux_backoff_schedule_unchanged():
    """WHEN a credential hits the rate limit THEN mark_limited escalates the
    backoff (60 -> 120 -> ... -> 900 clamp) exactly as before the change, and
    GithubCredentialLimited remains the dedicated rotation signal."""
    service, cred = "github_web", "sess-x"

    # Clean any state from other tests.
    github_credential_state._items.pop((service, cred), None)

    w1 = github_credential_state.mark_limited(service, cred)
    w2 = github_credential_state.mark_limited(service, cred)
    w3 = github_credential_state.mark_limited(service, cred)

    assert w1 == 60.0
    assert w2 == 120.0
    assert w3 == 240.0

    # Escalation clamps at 900s.
    for _ in range(10):
        w = github_credential_state.mark_limited(service, cred)
    assert w == 900.0

    # The typed rotation signal carries the wait for the stage loop.
    exc = GithubCredentialLimited(service=service, credential=cred, wait=w)
    assert exc.wait == 900.0

    github_credential_state._items.pop((service, cred), None)
