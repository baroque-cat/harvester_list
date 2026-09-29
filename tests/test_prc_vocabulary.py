"""One exported marker vocabulary, and the quota vocabulary's provenance (PRT-S16).

RED at plan time: `tools/http_signals.py` keeps its markers in the module-private
`_LIMIT_MARKERS`, while `GitHubClient.is_rate_limited_content`
(`search/client.py:631-636`) carries its own `["rate limit", "secondary rate
limit", "abuse detection"]` list.  PFC-D1 consolidated three copies and missed
this one; duplicated vocabularies drifting apart was the root cause of the
original defect, so the fourth copy goes too.

`is_quota_refusal` does not exist yet either.  Its vocabulary is **transcribed**
from the five already-shipped `_judge` methods rather than assumed (design D3),
and the second half of this module pins the tokens that were deliberately left
out - a bare `quota` or `billing` is safe inside a status-scoped provider branch
and unsafe at transport level, where it would relabel an authentication refusal.

No network: pure classifiers only.
"""

import inspect

import pytest

import search.client as sc
import tools.http_signals as hs
from search.client import GitHubClient
from tools.credential import Credentials

API = "github_api"


def _client():
    provider = Credentials(sessions=[], tokens=["t1"], strategy="round_robin")
    return GitHubClient(limiter=None, resource_provider=provider, limits=None)


# Marker bodies the shipped vocabulary already recognises, and bodies it must
# not.  Plain text on purpose: `is_rate_limited_content` only extracts a JSON
# `message` field, so a plain body is examined identically by both surfaces and
# the comparison is about the vocabulary, not about where each one looks.
MARKER_BODIES = [
    "You have exceeded a secondary rate limit",
    "abuse detection flag raised",
    "API rate limit exceeded for user",
    "RATE LIMIT reached",
]
NON_MARKER_BODIES = [
    "please wait",
    "Something failed. Please try again later.",
    "try again later",
    "invalid api key",
    "",
]


def test_s16_the_vocabulary_is_exported_and_shared_by_identity():
    """One definition, imported by name - not a copy that can drift."""
    assert hasattr(hs, "LIMIT_MARKERS"), "the marker vocabulary must be public"
    assert sc.LIMIT_MARKERS is hs.LIMIT_MARKERS, "the client must use the shared object itself"


@pytest.mark.parametrize("body", MARKER_BODIES + NON_MARKER_BODIES)
def test_s16_both_surfaces_give_the_same_verdict_from_one_vocabulary(body):
    """The credential-cooling detector and the refusal classifier answer from the
    same vocabulary, so a body recognised by one is recognised by the other."""
    expected = bool(hs.LIMIT_MARKERS.search(body)) if body else False

    assert _client().is_rate_limited_content(API, body) is expected
    assert hs.is_capacity_refusal(403, body) is expected


def test_s16_no_module_keeps_a_second_private_copy_of_the_limit_markers():
    source = inspect.getsource(GitHubClient.is_rate_limited_content)
    assert "rate limit" not in source.lower(), (
        "the private pattern list is still there; consume LIMIT_MARKERS instead"
    )


# ---------------------------------------------------------------------------
# The quota vocabulary: taken from shipped code, with the loose tokens excluded
# ---------------------------------------------------------------------------
QUOTA_BODIES = [
    # provider/openai_like.py:123 (403 branch)
    '{"error":{"code":"exceeded_current_quota_error"}}',
    '{"error":{"message":"insufficient_user_quota"}}',
    "账户额度不足，请更换密钥",
    "账户余额过低",
    # provider/openai_like.py:127 (429 branch)
    '{"error":{"code":"insufficient_quota"}}',
    '{"error":{"message":"billing_not_active"}}',
    "账户已欠费",
    "请充值后重试",
    "please recharge your account",
    # provider/anthropic.py:151
    '{"error":{"message":"credit balance is too low"}}',
    # provider/gemini.py:72 (the specific phrase, not the bare code)
    '{"error":{"message":"Quota exceeded for quota metric"}}',
]

NON_QUOTA_BODIES = [
    # design D3 exclusions: bare single words, safe only inside a status-scoped
    # provider branch, unsafe at transport level
    "quota",
    "please contact billing",
    "purchase a subscription",
    # RESOURCE_EXHAUSTED is Google's exhaustion code for rate limit *and* quota;
    # a 429 carrying it is already a capacity refusal (requirement 1), so
    # importing it here would steal rate-limit outcomes into the quota class
    '{"error":{"status":"RESOURCE_EXHAUSTED"}}',
    # ordinary refusals
    "invalid api key",
    "You have exceeded a secondary rate limit",
    '{"error":{"message":"upstream unavailable"}}',
    "",
]


@pytest.mark.parametrize("body", QUOTA_BODIES)
def test_s18_the_quota_vocabulary_recognises_every_shipped_marker(body):
    assert hs.is_quota_refusal(body) is True


@pytest.mark.parametrize("body", NON_QUOTA_BODIES)
def test_s18_the_quota_vocabulary_excludes_the_loose_tokens(body):
    assert hs.is_quota_refusal(body) is False


def test_s18_the_quota_vocabulary_is_disjoint_from_the_limit_vocabulary():
    """A body must not be claimable by both vocabularies, or "exactly one counted
    class" would depend on evaluation order for the wrong reason."""
    for body in MARKER_BODIES:
        assert hs.is_quota_refusal(body) is False, f"{body!r} matched both vocabularies"
    for body in QUOTA_BODIES:
        assert bool(hs.LIMIT_MARKERS.search(body)) is False, f"{body!r} matched both vocabularies"


def test_s18_quota_detection_is_fail_open_and_returns_a_real_bool():
    """House rule: classifiers degrade, they never raise into a worker loop."""
    assert hs.is_quota_refusal(None) is False
    assert hs.is_quota_refusal(12345) is False
    assert isinstance(hs.is_quota_refusal("insufficient_quota"), bool)
