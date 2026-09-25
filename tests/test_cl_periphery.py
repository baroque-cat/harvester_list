"""Peripheral credential consumers stay live (credential-liveness S24, S25, S26).

RED at plan time: ``manager.task._probe_github_credentials`` does not exist
(AttributeError), ``storage.repo_meta._next_credential`` swallows
CredentialsExhausted into the tokenless-latch path, and ``_gather_credential``
swallows it via ``except Exception: pass`` so the rest transport proceeds
credential-less - the import failure below is the file-level RED driver.

No network: HTTP layers are sentinel-patched.
"""

import logging
import time

import pytest

from search import client as search_client
from tools.state import CredentialsExhausted, github_credential_state  # RED driver: absent pre-fix

EXC = CredentialsExhausted(service="github_api", reason="budget_spent", wait_estimate_s=900.0)


class _RaisingAuth:
    def get_token(self):
        raise EXC

    def get_session(self):
        raise EXC

    def get_user_agent(self):
        return "test-agent"


class _RaisingProvider:
    """Stands in for the resource provider behind _gather_credential."""

    def get_token(self):
        raise EXC


# ---------------------------------------------------------------------------
# credential-liveness-S24
# ---------------------------------------------------------------------------
def test_s24_startup_probe_with_all_cooling_pool_boots(monkeypatch, caplog):
    """WHEN the process starts while every token is cooling THEN the capability
    probe reports 'no usable credential' with at most one WARNING - it neither
    blocks startup nor raises (pre-fix: the probe calls get_token() directly
    and inherits the selector's unbounded wait)."""
    from manager import task as manager_task

    monkeypatch.setattr(manager_task, "get_token", lambda: (_ for _ in ()).throw(EXC))
    monkeypatch.setattr(manager_task, "get_session", lambda: (_ for _ in ()).throw(EXC))

    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    with caplog.at_level(logging.WARNING):
        has_token, has_session = manager_task._probe_github_credentials()

    assert (has_token, has_session) == (False, False)
    assert slept == [], "startup must not sleep on the credential selector"
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, "at most one WARNING for the degraded probe"


def test_s24b_startup_probe_healthy_pool_reports_capability(monkeypatch):
    from manager import task as manager_task

    monkeypatch.setattr(manager_task, "get_token", lambda: "tok")
    monkeypatch.setattr(manager_task, "get_session", lambda: "ses")
    assert manager_task._probe_github_credentials() == (True, True)


# ---------------------------------------------------------------------------
# credential-liveness-S25
# ---------------------------------------------------------------------------
def test_s25_enrichment_yields_silently_on_exhaustion(tmp_path, monkeypatch):
    """WHEN enrichment needs a token and the pool is exhausted THEN it skips
    with the existing silent-yield semantics: no exception escapes, zero
    requests, and - critically - the permanent tokenless latch is NOT tripped
    (exhaustion is transient; a web-only deployment is not)."""
    from storage.repo_meta import RepoMetaEnricher, RepoMetaStore

    store = RepoMetaStore(str(tmp_path), ttl_hours=24, registry=None, clock=time.time)
    enricher = RepoMetaEnricher(
        store, auth=_RaisingAuth(), client=None, enabled=True, ttl_hours=24, clock=time.time
    )

    latch_tripped = []
    monkeypatch.setattr(enricher, "_disable_tokenless", lambda: latch_tripped.append(True))

    got = enricher._next_credential()  # must not raise

    assert got is None
    assert latch_tripped == [], "exhaustion must never permanently disable enrichment"
    assert enricher._tokenless is False


# ---------------------------------------------------------------------------
# credential-liveness-S26
# ---------------------------------------------------------------------------
def test_s26_rest_transport_gather_defers_instead_of_failing_unauthenticated(monkeypatch):
    """WHEN a rest-transport gather fetch cannot obtain a token due to
    exhaustion THEN it defers through the R5.1 refusal taxonomy and NO
    credential-less REST request is sent (pre-fix: _gather_credential swallows
    everything -> None -> guaranteed-to-fail anonymous api.github.com hit)."""
    from core.exceptions import RateLimitDeferral

    monkeypatch.setattr(search_client._github_client, "resource_provider", _RaisingProvider(), raising=False)

    def _no_http(*args, **kwargs):
        pytest.fail("no credential-less REST request may be sent")

    monkeypatch.setattr(search_client, "request", _no_http)

    url = "https://github.com/owner/repo/blob/main/src/config.json"
    with pytest.raises(RateLimitDeferral):
        search_client.fetch_gather_content(url, transport="rest")


# ---------------------------------------------------------------------------
# credential-liveness-S24/S25/S26 (production chain) - live-verification find
# ---------------------------------------------------------------------------
def test_s24c_exhaustion_propagates_through_the_production_auth_chain(monkeypatch):
    """The real chain is stage -> GithubAuthProvider -> coordinator.get_token ->
    Credentials. BOTH intermediate layers used to swallow every exception into
    ``None`` (design D18, found by the live gate): a typed exhaustion would then
    reach the stage as a falsy token and be completed EMPTY (violates S8), trip
    repo_meta's permanent tokenless latch (violates S25), and downgrade the rest
    gather transport to an anonymous request (violates S26)."""
    from core.auth import GithubAuthProvider
    from tools import coordinator as tools_coordinator

    class _ExhaustedCredentials:
        def get_token(self):
            raise EXC

        def get_session(self):
            raise EXC

    manager = tools_coordinator.ResourceManager.__new__(tools_coordinator.ResourceManager)
    manager._initialized = True
    manager._credentials = _ExhaustedCredentials()
    monkeypatch.setattr(
        tools_coordinator.ResourceManager, "get_instance", classmethod(lambda cls: manager)
    )

    with pytest.raises(CredentialsExhausted):
        tools_coordinator.get_token()
    with pytest.raises(CredentialsExhausted):
        tools_coordinator.get_session()

    GithubAuthProvider.configure(
        session_provider=tools_coordinator.get_session,
        token_provider=tools_coordinator.get_token,
        user_agent_provider=lambda: "test-agent",
    )
    try:
        provider = GithubAuthProvider.get_instance()
        with pytest.raises(CredentialsExhausted):
            provider.get_token()
        with pytest.raises(CredentialsExhausted):
            provider.get_session()
    finally:
        GithubAuthProvider._session_provider = None
        GithubAuthProvider._token_provider = None
        GithubAuthProvider._user_agent_provider = None


def test_s24d_capability_probe_never_waits_out_a_cooldown(monkeypatch):
    """Spec S24: "no startup sleep occurs". Under the production config
    (``max_wait_s: 60``) an all-cooling pool made the startup probe sleep the
    whole budget before degrading - boot must instead fail fast (design D19,
    found by the live gate)."""
    from tools.credential import Credentials, fail_fast_liveness

    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

    creds = Credentials(sessions=[], tokens=["t1", "t2"], strategy="round_robin")
    for tok in ("t1", "t2"):
        github_credential_state.mark_limited("github_api", tok, wait=900)
    try:
        t0 = time.monotonic()
        with fail_fast_liveness():
            with pytest.raises(CredentialsExhausted):
                creds.get_token()
        assert slept == [], f"probe must not sleep, slept={slept}"
        assert time.monotonic() - t0 < 1.0, "fail-fast probe returns immediately"

        # Outside the override the configured budget applies again (no leak).
        from tools import credential as credential_module

        assert credential_module._liveness_settings().max_wait_s != 0.0
    finally:
        for key in list(github_credential_state._items):
            github_credential_state._items.pop(key, None)
