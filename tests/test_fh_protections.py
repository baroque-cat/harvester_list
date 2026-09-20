"""Preserved protections - regression guards (fix-silent-losses).

Traceability: failure-handling-S16 (full-pool cooldown waits, never drops)
plus an auxiliary guard for the backoff schedule supporting S15 (canonical
S15 test lives in tests/test_fh_stage_modes.py). S17 restart-recovery is
Manual: it needs a full application boot/shutdown cycle - see tests.md.

These tests encode behavior that EXISTS today and must survive the fix
unchanged. They are expected GREEN both before and after implementation;
any RED here means the fix broke a protected mechanism.

No network: credential state and sleeps are monkeypatched.
"""

import time

from tools.credential import Credentials
from tools.state import GithubCredentialLimited, github_credential_state


# ---------------------------------------------------------------------------
# failure-handling-S16 (all-credentials-cooling blocks and waits, never drops)
# ---------------------------------------------------------------------------
def test_s16_full_pool_cooldown_waits_never_drops(monkeypatch):
    """WHEN the only session is cooling THEN _get_available blocks until release
    and returns it - the task waits, it is never dropped."""
    creds = Credentials(sessions=["s1"], tokens=[], strategy="round_robin")

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
    assert slept and slept[0] == 0.05  # blocked on the cooldown timer, did not give up


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
