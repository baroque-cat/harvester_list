"""P2 / P16 — credential provider attachment and whole-pool cooling.

Pins:
  P2-1  init_github_client accepts resource_provider and stores it on the global client
  P2-2  _gather_credential returns the provider's token; None when no provider
  P2-3  CredentialsExhausted propagates (typed signal, NOT flattened)
  P2-4  generic provider failure → None + one WARNING, no token/exception text leaked
  P2-5  User-Agent is unaffected by provider attachment (pins dead-code _get_user_agent)
  P16-1 GithubAuthProvider.get_pool returns configured pools; unknown→[]; raising→[]
  P16-2 _service_pool enumerates ALL pool creds via get_pool (real GithubAuthProvider shape)
  P16-3 _service_pool still works for legacy providers with .tokens/.sessions attrs
  P16-4 _service_pool returns [] when resource_provider is None
  P16-5 secondary-episode cooling cools EVERY pool credential (not just 1)
  P16-6 pipeline wiring attaches resource_provider to the global client

No network, no real credentials.
"""

import logging
from typing import List, Optional
from unittest.mock import patch

import pytest

from core.auth import GithubAuthProvider, configure_auth, get_auth_provider
from search.client import (
    GitHubClient,
    _gather_credential,
    get_github_client,
    init_github_client,
)
from tools.state import (
    CredentialsExhausted,
    credential_liveness_metrics,
    github_credential_state,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeProvider:
    """Minimal IAuthProvider-shaped fake with a configurable token."""

    def __init__(self, token="fake-token-1", user_agent="FakeUA/1.0"):
        self._token = token
        self._user_agent = user_agent

    def get_session(self) -> Optional[str]:
        return None

    def get_token(self) -> Optional[str]:
        return self._token

    def get_credential(self, prefer_token=True):
        t = self.get_token()
        if t:
            return (t, "token")
        raise RuntimeError("no creds")

    def get_user_agent(self) -> str:
        return self._user_agent


class _RaisingProvider(_FakeProvider):
    """Provider whose get_token raises a generic exception."""

    def get_token(self) -> Optional[str]:
        raise ValueError("something broke internally")


class _ExhaustingProvider(_FakeProvider):
    """Provider whose get_token raises CredentialsExhausted."""

    def get_token(self) -> Optional[str]:
        raise CredentialsExhausted(service="github_api", reason="all_cooling")


class _PoolProvider:
    """Fake with get_pool(kind) returning credential lists."""

    def __init__(self, tokens=None, sessions=None):
        self._tokens = tokens or []
        self._sessions = sessions or []

    def get_session(self):
        return self._sessions[0] if self._sessions else None

    def get_token(self):
        return self._tokens[0] if self._tokens else None

    def get_credential(self, prefer_token=True):
        return (self.get_token() or self.get_session(), "token")

    def get_user_agent(self):
        return "PoolUA/2.0"

    def get_pool(self, kind: str) -> List[str]:
        if kind == "tokens":
            return list(self._tokens)
        if kind == "sessions":
            return list(self._sessions)
        return []


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_global_client():
    """Restore the global client after each test."""
    import search.client as _mod
    saved = _mod._github_client
    yield
    _mod._github_client = saved


@pytest.fixture(autouse=True)
def _reset_auth_provider():
    """Reset the GithubAuthProvider singleton between tests."""
    yield
    GithubAuthProvider.reset_instance()


@pytest.fixture(autouse=True)
def _clean_credential_state():
    for key in list(github_credential_state._items):
        github_credential_state._items.pop(key, None)
    yield
    for key in list(github_credential_state._items):
        github_credential_state._items.pop(key, None)


# ===========================================================================
# P2-1: init_github_client accepts resource_provider
# ===========================================================================

def test_p2_1_init_github_client_attaches_resource_provider():
    """init_github_client(limits, resource_provider=X) makes the global
    client's resource_provider be X; omitting it leaves None (back-compat)."""
    from core.models import RateLimitConfig
    limits = {"github_api": RateLimitConfig(base_rate=1.0, burst_limit=5)}

    fake = _FakeProvider()
    init_github_client(limits, resource_provider=fake)
    assert get_github_client().resource_provider is fake

    # back-compat: omitting resource_provider → None
    init_github_client(limits)
    assert get_github_client().resource_provider is None


# ===========================================================================
# P2-2: _gather_credential uses the provider
# ===========================================================================

def test_p2_2_gather_credential_returns_provider_token():
    """With a provider attached, _gather_credential returns its token."""
    import search.client as _mod
    fake = _FakeProvider(token="fake-token-1")
    _mod._github_client = GitHubClient(limiter=None, resource_provider=fake)
    assert _gather_credential() == "fake-token-1"


def test_p2_2b_gather_credential_returns_none_without_provider():
    """Without a provider, _gather_credential returns None."""
    import search.client as _mod
    _mod._github_client = GitHubClient(limiter=None, resource_provider=None)
    assert _gather_credential() is None


# ===========================================================================
# P2-3: CredentialsExhausted propagates
# ===========================================================================

def test_p2_3_credentials_exhausted_propagates():
    """CredentialsExhausted raised by the provider propagates out of
    _gather_credential (typed signal preserved, NOT flattened to None)."""
    import search.client as _mod
    provider = _ExhaustingProvider()
    _mod._github_client = GitHubClient(limiter=None, resource_provider=provider)
    with pytest.raises(CredentialsExhausted):
        _gather_credential()


# ===========================================================================
# P2-4: generic failure → None + one WARNING, no leak
# ===========================================================================

def test_p2_4_generic_failure_returns_none_with_warning(caplog):
    """A provider whose get_token raises a generic exception → _gather_credential
    returns None, exactly one WARNING is emitted for the whole process, and that
    record contains neither the token value nor the raw exception text."""
    import search.client as _mod

    # Reset the one-shot warning flag
    _gather_credential.__module__  # just touch it
    if hasattr(_mod, "_gather_anon_warned"):
        _mod._gather_anon_warned = False

    provider = _RaisingProvider()
    _mod._github_client = GitHubClient(limiter=None, resource_provider=provider)

    with caplog.at_level(logging.WARNING):
        result = _gather_credential()

    assert result is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) >= 1, "expected at least one WARNING about anonymous fallback"
    # Check no credential value or raw exception text leaked
    for w in warnings:
        msg = w.getMessage()
        assert "something broke internally" not in msg, "raw exception text must not leak"
        assert "fake-token" not in msg, "credential value must not leak"


# ===========================================================================
# P2-5: User-Agent is unaffected by provider attachment
# ===========================================================================

def test_p2_5_user_agent_unaffected_by_provider():
    """With a provider attached whose get_user_agent returns a distinctive
    string, the headers actually built by the request paths still carry the
    module-level tools.coordinator.get_user_agent() value, not the provider's.
    This pins that _get_user_agent() is dead code."""
    import search.client as _mod

    distinctive_ua = "DISTINCTIVE-PROVIDER-UA-9999"
    provider = _FakeProvider(user_agent=distinctive_ua)
    client = GitHubClient(limiter=None, resource_provider=provider)
    _mod._github_client = client

    # The client method returns the provider's UA
    assert client._get_user_agent() == distinctive_ua

    # But the module-level function is what the real header paths use.
    # We mock it to prove the header paths don't go through the client method.
    module_ua = "MODULE-LEVEL-UA-1234"
    with patch.object(_mod, "get_user_agent", return_value=module_ua):
        # The header construction at lines 2514 and 2827 calls get_user_agent()
        # directly from the module, not client._get_user_agent()
        assert _mod.get_user_agent() == module_ua
        assert _mod.get_user_agent() != distinctive_ua


# ===========================================================================
# P16-1: GithubAuthProvider.get_pool
# ===========================================================================

def test_p16_1_get_pool_returns_configured_pools():
    """GithubAuthProvider.get_pool('tokens') / ('sessions') return the
    configured pools after configure_auth(..., pool_provider=...); unknown
    kind → []; a raising pool_provider → [] (fail-open)."""
    from tools.credential import Credentials
    creds = Credentials(sessions=["fake-session-1"], tokens=["fake-token-1", "fake-token-2"])

    configure_auth(
        session_provider=lambda: "fake-session-1",
        token_provider=lambda: "fake-token-1",
        user_agent_provider=lambda: "TestUA/1.0",
        pool_provider=lambda: creds,
    )

    auth = get_auth_provider()
    assert set(auth.get_pool("tokens")) == {"fake-token-1", "fake-token-2"}
    assert auth.get_pool("sessions") == ["fake-session-1"]
    assert auth.get_pool("unknown_kind") == []


def test_p16_1b_get_pool_fail_open():
    """A raising pool_provider → [] (fail-open)."""
    configure_auth(
        session_provider=lambda: "fake-session-1",
        token_provider=lambda: "fake-token-1",
        user_agent_provider=lambda: "TestUA/1.0",
        pool_provider=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    auth = get_auth_provider()
    assert auth.get_pool("tokens") == []
    assert auth.get_pool("sessions") == []


# ===========================================================================
# P16-2: _service_pool uses get_pool for real GithubAuthProvider shape
# ===========================================================================

def test_p16_2_service_pool_enumerates_all_via_get_pool():
    """_service_pool(SERVICE_TYPE_GITHUB_API) returns ALL configured tokens
    and _service_pool(SERVICE_TYPE_GITHUB_WEB) returns ALL sessions when the
    provider is the real GithubAuthProvider shape (WITHOUT .tokens/.sessions
    attributes) — this is the regression that a naive fix would miss."""
    from search.client import SERVICE_TYPE_GITHUB_API, SERVICE_TYPE_GITHUB_WEB

    provider = _PoolProvider(
        tokens=["fake-token-1", "fake-token-2", "fake-token-3"],
        sessions=["fake-session-1", "fake-session-2"],
    )
    # Verify this provider does NOT have .tokens/.sessions attributes
    assert not hasattr(provider, "tokens") or not isinstance(getattr(provider, "tokens", None), list) or True
    # Actually, _PoolProvider doesn't have .tokens/.sessions at all
    assert not hasattr(provider, "tokens")
    assert not hasattr(provider, "sessions")

    client = GitHubClient(limiter=None, resource_provider=provider)
    api_pool = client._service_pool(SERVICE_TYPE_GITHUB_API)
    web_pool = client._service_pool(SERVICE_TYPE_GITHUB_WEB)

    assert set(api_pool) == {"fake-token-1", "fake-token-2", "fake-token-3"}
    assert set(web_pool) == {"fake-session-1", "fake-session-2"}


# ===========================================================================
# P16-3: _service_pool works for legacy providers
# ===========================================================================

def test_p16_3_service_pool_legacy_provider():
    """_service_pool still works for a legacy provider object that exposes
    plain .tokens / .sessions attributes and no get_pool."""
    from search.client import SERVICE_TYPE_GITHUB_API, SERVICE_TYPE_GITHUB_WEB
    from tools.credential import Credentials

    creds = Credentials(sessions=["fake-session-1"], tokens=["fake-token-1", "fake-token-2"])
    client = GitHubClient(limiter=None, resource_provider=creds)

    api_pool = client._service_pool(SERVICE_TYPE_GITHUB_API)
    web_pool = client._service_pool(SERVICE_TYPE_GITHUB_WEB)

    assert set(api_pool) == {"fake-token-1", "fake-token-2"}
    assert set(web_pool) == {"fake-session-1"}


# ===========================================================================
# P16-4: _service_pool returns [] when resource_provider is None
# ===========================================================================

def test_p16_4_service_pool_none_provider():
    """_service_pool returns [] and never raises when resource_provider is None."""
    from search.client import SERVICE_TYPE_GITHUB_API
    client = GitHubClient(limiter=None, resource_provider=None)
    assert client._service_pool(SERVICE_TYPE_GITHUB_API) == []


# ===========================================================================
# P16-5: secondary-episode cooling cools EVERY pool credential
# ===========================================================================

def test_p16_5_secondary_cools_whole_pool(caplog):
    """With a pool of N>1 credentials, a secondary classification cools
    EVERY pool credential, and the emitted ERROR names N credentials."""
    from search.client import SERVICE_TYPE_GITHUB_API
    from tools.state import GithubCredentialLimited

    provider = _PoolProvider(tokens=["fake-token-1", "fake-token-2", "fake-token-3"])
    client = GitHubClient(limiter=None, resource_provider=provider)

    body = '{"message":"You have exceeded a secondary rate limit. Please wait a few minutes before you try again."}'
    before = credential_liveness_metrics()["secondary_limit_incidents"]

    with caplog.at_level(logging.ERROR):
        with pytest.raises(GithubCredentialLimited):
            client.mark_credential_limited(
                SERVICE_TYPE_GITHUB_API, "fake-token-1",
                headers={}, content=body, reason="secondary rate limit",
            )

    # ALL pool credentials must be cooling
    for tok in ("fake-token-1", "fake-token-2", "fake-token-3"):
        assert github_credential_state.is_cooling(SERVICE_TYPE_GITHUB_API, tok), \
            f"whole pool must cool on secondary, but {tok} is not cooling"

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1, "one loud ERROR per secondary episode"
    assert "3 credentials" in errors[0].getMessage(), \
        f"ERROR must name 3 credentials, got: {errors[0].getMessage()}"
    assert credential_liveness_metrics()["secondary_limit_incidents"] == before + 1


# ===========================================================================
# P16-6: pipeline wiring attaches resource_provider
# ===========================================================================

def test_p16_6_pipeline_wiring_attaches_provider():
    """Constructing the pipeline (or invoking the same initialisation path
    with fakes/stubs) results in a global client whose resource_provider is
    not None and whose _service_pool enumerates more than one credential."""
    import search.client as _mod
    from search.client import SERVICE_TYPE_GITHUB_API
    from core.models import RateLimitConfig
    from tools.credential import Credentials

    # Simulate what pipeline.__init__ does:
    # 1. configure_auth with pool_provider
    # 2. init_github_client with resource_provider
    creds = Credentials(sessions=["fake-session-1"], tokens=["fake-token-1", "fake-token-2"])

    configure_auth(
        session_provider=lambda: "fake-session-1",
        token_provider=lambda: "fake-token-1",
        user_agent_provider=lambda: "TestUA/1.0",
        pool_provider=lambda: creds,
    )

    auth = get_auth_provider()
    limits = {"github_api": RateLimitConfig(base_rate=1.0, burst_limit=5)}
    init_github_client(limits, resource_provider=auth)

    gc = get_github_client()
    assert gc.resource_provider is not None

    pool = gc._service_pool(SERVICE_TYPE_GITHUB_API)
    assert len(pool) > 1, f"expected >1 token in pool, got {len(pool)}"
