"""Primary/secondary limit reactions + narrowed content detectors
(credential-liveness S14, S15-client-half lives in test_cl_stage_defer.py,
S16, S17, S18, S19, S20).

RED at plan time: pre-fix ``is_rate_limited_content`` still matches the broad
fragments ``please wait`` / ``try again later`` (S18 fails), the whole-pool
secondary reaction and limit kinds do not exist (S14 kind attr / S16 fail),
and the ``credential_liveness_metrics`` import fails (file-level RED driver).

Wire-format honesty (house rule): secondary-marker phrases pinned here are the
ones the code ALREADY matches; no live-captured secondary-limit body exists in
repo logs (grep 2026-09-25 finds test-run lines only), and the runbook's
organic-observation step captures real bodies if they occur. No new phrase is
invented.

No network: only pure classifiers and the cooldown state singleton.
"""

import logging
import time

import pytest

from search.client import GitHubClient
from tools.credential import Credentials
from tools.state import (  # RED driver: credential_liveness_metrics absent pre-fix
    GithubCredentialLimited,
    credential_liveness_metrics,
    github_credential_state,
)

API = "github_api"
WEB = "github_web"


@pytest.fixture(autouse=True)
def _clean_state():
    for key in list(github_credential_state._items):
        github_credential_state._items.pop(key, None)
    yield
    for key in list(github_credential_state._items):
        github_credential_state._items.pop(key, None)


def _client(tokens=("t1", "t2", "t3")):
    provider = Credentials(sessions=[], tokens=list(tokens), strategy="round_robin")
    return GitHubClient(limiter=None, resource_provider=provider, limits=None)


# ---------------------------------------------------------------------------
# credential-liveness-S14
# ---------------------------------------------------------------------------
def test_s14_primary_quota_exhaustion_cools_one_credential_until_reset(caplog):
    """WHEN the response carries remaining=0 + a near-future reset THEN only
    the credential in use is cooled (until reset, clamped), the pool mates stay
    selectable, the entry is kind=primary, and no secondary incident is
    counted."""
    client = _client()
    reset = int(time.time()) + 120
    headers = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)}

    before_secondary = credential_liveness_metrics()["secondary_limit_incidents"]
    with caplog.at_level(logging.ERROR):
        with pytest.raises(GithubCredentialLimited):
            client.mark_credential_limited(API, "t1", headers=headers, content="", reason="")

    assert github_credential_state.is_cooling(API, "t1")
    wait = github_credential_state.wait_time(API, "t1")
    assert 60 <= wait <= 125, f"cooled until reset (clamped to the cooldown bounds), got {wait}"
    assert not github_credential_state.is_cooling(API, "t2")
    assert not github_credential_state.is_cooling(API, "t3")
    assert github_credential_state._items[(API, "t1")].kind == "primary"
    assert credential_liveness_metrics()["secondary_limit_incidents"] == before_secondary
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], \
        "a primary quota event is routine, not an emergency"


# ---------------------------------------------------------------------------
# credential-liveness-S16
# ---------------------------------------------------------------------------
def test_s16_secondary_marker_cools_whole_service_pool_and_escalates_loudly(caplog):
    """WHEN the body matches a secondary-limit marker THEN every credential of
    THAT service is cooled (subject-scoped limit), exactly one ERROR is logged,
    the secondary counter increments; other services' pools are untouched."""
    client = _client()
    github_credential_state.mark_limited(WEB, "s-web", wait=60)  # an unrelated service pool member

    body = '{"message":"You have exceeded a secondary rate limit. Please wait a few minutes before you try again."}'
    before = credential_liveness_metrics()["secondary_limit_incidents"]

    with caplog.at_level(logging.ERROR):
        with pytest.raises(GithubCredentialLimited):
            client.mark_credential_limited(API, "t1", headers={}, content=body, reason="secondary rate limit")

    for tok in ("t1", "t2", "t3"):
        assert github_credential_state.is_cooling(API, tok), f"whole {API} pool must cool on a subject-scoped limit"
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1, "one loud ERROR per secondary episode"
    assert credential_liveness_metrics()["secondary_limit_incidents"] == before + 1
    # separate budgets: the web pool member's state was not extended by the API incident
    assert github_credential_state.wait_time(WEB, "s-web") <= 60


# ---------------------------------------------------------------------------
# credential-liveness-S17
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "code,message,expected",
    [
        (403, "please wait a few minutes", False),       # polite fragment alone: transient, NOT a limit
        (403, "try again later", False),
        (403, "rate limit exceeded", True),               # verifiable marker
        (403, "abuse detection triggered", True),
        (429, "", True),                                  # 429 is always a limit
        (404, "please wait", False),
    ],
)
def test_s17_ambiguous_refusal_never_benches_a_credential(code, message, expected):
    """WHEN a 403 arrives without usable limit markers THEN the HTTP classifier
    says 'not a limit' - the caller routes it to the transient-failure path
    (bounded requeue, counted) and mark_credential_limited is never reached, so
    no credential is benched on a guess."""
    client = _client()
    assert client._is_http_rate_limited(code, message) is expected
    if not expected:
        assert not github_credential_state._items  # classifier is pure: nothing benched


# ---------------------------------------------------------------------------
# credential-liveness-S18
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "content",
    [
        '{"message":"please wait"}',
        '{"message":"Something failed. Please try again later."}',
        '{"message":"try again later"}',
    ],
)
def test_s18_polite_fragment_alone_is_not_a_limit(content):
    """WHEN an API-branch body carries only polite fragments THEN content
    detection must NOT classify it as rate-limited (pre-fix: the broad
    patterns match - this is the narrowing pin)."""
    client = _client()
    assert client.is_rate_limited_content(API, content) is False


# ---------------------------------------------------------------------------
# credential-liveness-S19 (regression guard: expected GREEN pre-fix too)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "content",
    [
        '{"message":"You have exceeded a secondary rate limit"}',
        '{"message":"abuse detection flag raised"}',
        '{"message":"API rate limit exceeded for user"}',
    ],
)
def test_s19_verifiable_markers_still_detected(content):
    """WHEN a body contains a verifiable GitHub marker THEN it is still
    classified as rate-limited - narrowing removes guesses, not detection."""
    client = _client()
    assert client.is_rate_limited_content(API, content) is True


# ---------------------------------------------------------------------------
# credential-liveness-S20 (regression guard: expected GREEN pre-fix too)
# ---------------------------------------------------------------------------
def test_s20_web_search_soft_block_string_preserved():
    """WHEN the web surface returns its exact full-sentence soft block THEN it
    is still classified as rate-limited exactly as before this change."""
    client = _client()
    assert client.is_rate_limited_content(WEB, "Search failed. Please try again later.") is True
