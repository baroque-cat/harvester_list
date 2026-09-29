"""Every declared refusal class has a producer (PRT-S15, S18..S23).

RED at plan time: `refusals_quota`, `refusals_auth` and `refusals_transient` are
declared in `_PROVIDER_REFUSAL_STAT_KEYS`, rendered and registered - and never
incremented anywhere.  Measured against commit `698bbf5` (the baseline table
below was produced by `/tmp/opencode/prc_baseline.py`, not remembered): a 401, a
bare 403, a 403 carrying `exceeded_current_quota_error`, a 429 carrying
`insufficient_quota` and a 503 all report `counters={}`, so `_fetch_models`
returns `[]` and the stage records `inspect_empty_answers`.  A refusal is
indistinguishable from "this provider has no models" - the exact defect the
capability exists to remove.

The invariant these pins defend is that counting is **observational** (design
D1): `test_s22` asserts the exception type, the message and the wire-hit count of
every row and is expected GREEN both before and after the producers land.  It is
a regression guard, not a RED driver - recorded as such in `tests.md`.

Fixture provenance (house rule): no live quota or capacity refusal was ever
captured - the PFC-D11 probe of all four production providers returned HTTP 401
`invalid_api_key` with no `Retry-After` and no `X-RateLimit-*` (dashscope,
dashscope-intl, maas.qwencloudapi, api.deepseek; 2026-09-28; auth mode:
deliberately invalid key).  The quota bodies below are therefore **synthetic
supplementary unit vectors**, transcribed from marker strings already shipped in
`provider/openai_like.py:123,127`, `provider/anthropic.py:151` and
`provider/gemini.py:72`.  Bodies are ASCII-only on the wire path so no charset
detection sits between the marker and the assertion; the CJK markers are covered
at unit level in `tests/test_prc_vocabulary.py`.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

import search.client as sc
from core.exceptions import NetworkError, RateLimitDeferral

BODY_LIMIT = json.dumps({"error": {"message": "You triggered the rate limit policy"}})
BODY_LIMIT_WAIT = json.dumps({"error": {"message": "rate limit exceeded, retry after 30 seconds"}})
BODY_AUTH = json.dumps({"error": {"message": "invalid api key"}})
BODY_QUOTA_403 = json.dumps({"error": {"message": "exceeded_current_quota_error", "code": "exceeded_current_quota_error"}})
BODY_QUOTA_429 = json.dumps({"error": {"message": "insufficient_quota: billing_not_active", "code": "insufficient_quota"}})
BODY_SERVER = json.dumps({"error": {"message": "upstream unavailable"}})
BODY_NOT_FOUND = json.dumps({"error": {"message": "no such file"}})
BODY_BAD_REQUEST = json.dumps({"error": {"message": "bad request"}})

REFUSAL_KEYS = ("refusals_rate_limit", "refusals_quota", "refusals_auth", "refusals_transient")


class _ScriptedHandler(BaseHTTPRequestHandler):
    """Returns one scripted response and counts the hits."""

    script = (200, {}, "{}")
    hits = 0
    lock = threading.Lock()

    def do_GET(self):  # noqa: N802 - http.server contract
        self._respond()

    def do_POST(self):  # noqa: N802 - the check surface (`chat`) posts
        self._respond()

    def _respond(self):
        status, headers, body = self.script
        payload = body.encode("utf-8")
        with self.lock:
            type(self).hits += 1
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, str(value))
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # silence the request log
        pass


@pytest.fixture
def served(monkeypatch):
    """A loopback server plus an isolated session, so no proxy/env can interfere."""
    isolated = requests.Session()
    isolated.trust_env = False
    monkeypatch.setattr(sc, "_HTTP_SESSION", isolated)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _ScriptedHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]

    def serve(status, headers=None, body="{}"):
        _ScriptedHandler.script = (status, headers or {}, body)
        _ScriptedHandler.hits = 0
        return f"http://127.0.0.1:{port}/models"

    try:
        yield serve
    finally:
        httpd.shutdown()
        httpd.server_close()
        isolated.close()


@pytest.fixture(autouse=True)
def _clean_counters():
    sc.reset_provider_refusal_stats()
    yield
    sc.reset_provider_refusal_stats()


def _call(url):
    """One transport call; returns (exception or None, wire hits)."""
    try:
        sc.http_get(url=url, retries=3, interval=0.05, timeout=5)
    except Exception as exc:  # noqa: BLE001 - the pin is on the type itself
        return exc, _ScriptedHandler.hits
    return None, _ScriptedHandler.hits


# ---------------------------------------------------------------------------
# The measured baseline.  Column 5 is the ONLY column this change is allowed to
# touch: type, message and hit count are frozen at their `698bbf5` values.
# ---------------------------------------------------------------------------
TRANSPORT_TABLE = [
    # name,                        status, headers,              body,           type,                message,                                                                hits, counter
    ("429 published header wait", 429, {"Retry-After": "30"}, BODY_LIMIT_WAIT, RateLimitDeferral, "provider rate limit (HTTP 429) for URL: {url}", 1, "refusals_rate_limit"),
    ("429 wait expressed in body", 429, {}, BODY_LIMIT_WAIT, RateLimitDeferral, "provider rate limit (HTTP 429) for URL: {url}", 1, "refusals_rate_limit"),
    ("429 no wait", 429, {}, BODY_LIMIT, ConnectionError, "Rate limit exceeded (HTTP 429)", 3, "refusals_transient"),
    ("429 quota no wait", 429, {}, BODY_QUOTA_429, ConnectionError, "Rate limit exceeded (HTTP 429)", 3, "refusals_quota"),
    ("403 marker + header wait", 403, {"Retry-After": "12"}, BODY_LIMIT, RateLimitDeferral, "provider rate limit (HTTP 403) for URL: {url}", 1, "refusals_rate_limit"),
    ("403 marker no wait", 403, {}, BODY_LIMIT, ConnectionError, "Rate limit exceeded (HTTP 403)", 3, "refusals_transient"),
    ("403 no marker", 403, {}, BODY_AUTH, NetworkError, "Authentication failed (HTTP 403)", 1, "refusals_auth"),
    ("403 quota", 403, {}, BODY_QUOTA_403, NetworkError, "Authentication failed (HTTP 403)", 1, "refusals_quota"),
    ("401 no marker", 401, {}, BODY_AUTH, NetworkError, "Authentication failed (HTTP 401)", 1, "refusals_auth"),
    ("503", 503, {}, BODY_SERVER, ConnectionError, f"Server error (HTTP 503): {BODY_SERVER}", 3, "refusals_transient"),
    ("500", 500, {}, BODY_SERVER, ConnectionError, f"Server error (HTTP 500): {BODY_SERVER}", 3, "refusals_transient"),
    ("404", 404, {}, BODY_NOT_FOUND, FileNotFoundError, "File not found (HTTP 404), url: {url}", 1, None),
    ("400", 400, {}, BODY_BAD_REQUEST, NetworkError, f"HTTP 400 error: {BODY_BAD_REQUEST}", 1, None),
]


@pytest.mark.parametrize("row", TRANSPORT_TABLE, ids=[r[0] for r in TRANSPORT_TABLE])
def test_s22_counting_never_re_classifies_a_legacy_path(served, row):
    """PRT-S22: the exception type, its message and the number of wire hits are
    identical to the values measured on the shipped pre-change transport.  Only
    the counters may differ - counting is observational (design D1)."""
    name, status, headers, body, expected_type, expected_message, expected_hits, _ = row
    url = served(status, headers, body)

    exc, hits = _call(url)

    assert exc is not None, f"{name}: the transport must raise"
    assert type(exc) is expected_type, f"{name}: exception type changed"
    assert str(exc) == expected_message.replace("{url}", url), f"{name}: message changed"
    assert hits == expected_hits, f"{name}: retry count changed"


# ---------------------------------------------------------------------------
# PRT-S15: the previously unpinned 403 + marker + no published wait path
# ---------------------------------------------------------------------------
def test_s15_a_marked_403_with_no_published_wait_keeps_the_legacy_transient_exception(served):
    """A 403 carrying a throttling marker but publishing no wait is a capacity
    refusal that cannot be deferred: it keeps the legacy retryable transient
    error (never "Authentication failed", PRT-S2) and is now counted.

    This row changed behaviour in the predecessor change and was pinned by
    nothing - PRT-S2 used a 403 *with* `Retry-After`, PRT-S3 a 403 *without* a
    marker.  Recorded as PFC-D17.
    """
    url = served(403, {}, BODY_LIMIT)

    exc, hits = _call(url)

    assert type(exc) is ConnectionError
    assert str(exc) == "Rate limit exceeded (HTTP 403)"
    assert hits == 3, "the legacy transient retry count must be preserved"

    stats = sc.get_provider_refusal_stats()
    assert stats["refusals_transient"] == 1
    assert stats["refusals_auth"] == 0, "a marked 403 is capacity, never authentication"
    assert sum(stats[key] for key in REFUSAL_KEYS) == 1, "exactly one counted class"


# ---------------------------------------------------------------------------
# PRT-S18: quota
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "status,body,expected_type,expected_message",
    [
        (403, BODY_QUOTA_403, NetworkError, "Authentication failed (HTTP 403)"),
        (429, BODY_QUOTA_429, ConnectionError, "Rate limit exceeded (HTTP 429)"),
    ],
    ids=["403 exhausted quota", "429 insufficient quota"],
)
def test_s18_a_quota_refusal_is_counted_as_quota_and_keeps_its_legacy_exception(
    served, status, body, expected_type, expected_message
):
    url = served(status, {}, body)

    exc, _hits = _call(url)

    assert type(exc) is expected_type
    assert str(exc) == expected_message

    stats = sc.get_provider_refusal_stats()
    assert stats["refusals_quota"] == 1
    assert stats["inspect_empty_answers"] == 0, "a refusal is not 'the provider has no models'"
    assert stats["refusals_auth"] == 0
    assert sum(stats[key] for key in REFUSAL_KEYS) == 1, "exactly one counted class"


# ---------------------------------------------------------------------------
# PRT-S19: authentication
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "status,expected_message",
    [(401, "Authentication failed (HTTP 401)"), (403, "Authentication failed (HTTP 403)")],
    ids=["401", "bare 403"],
)
def test_s19_an_auth_refusal_is_counted_as_auth_and_keeps_its_legacy_exception(
    served, status, expected_message
):
    """PRT-S3's "exactly as before" constrains the exception, not the counter:
    the verdict stays an authentication failure and no deferral is raised, while
    the class becomes visible instead of collapsing into an empty model list."""
    url = served(status, {}, BODY_AUTH)

    exc, hits = _call(url)

    assert type(exc) is NetworkError, "no deferral and no subclass: the legacy auth verdict"
    assert str(exc) == expected_message
    assert hits == 1, "an authentication failure is not retried"

    stats = sc.get_provider_refusal_stats()
    assert stats["refusals_auth"] == 1
    assert stats["inspect_empty_answers"] == 0
    assert sum(stats[key] for key in REFUSAL_KEYS) == 1, "exactly one counted class"


# ---------------------------------------------------------------------------
# PRT-S20: transient
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "status,headers,body,expected_message",
    [
        (429, {}, BODY_LIMIT, "Rate limit exceeded (HTTP 429)"),
        (503, {}, BODY_SERVER, f"Server error (HTTP 503): {BODY_SERVER}"),
    ],
    ids=["429 without published wait", "503"],
)
def test_s20_a_transient_refusal_is_counted_as_transient(served, status, headers, body, expected_message):
    url = served(status, headers, body)

    exc, _hits = _call(url)

    assert type(exc) is ConnectionError
    assert str(exc) == expected_message

    stats = sc.get_provider_refusal_stats()
    assert stats["refusals_transient"] == 1
    assert stats["inspect_empty_answers"] == 0
    assert sum(stats[key] for key in REFUSAL_KEYS) == 1, "exactly one counted class"


# ---------------------------------------------------------------------------
# PRT-S21: exactly one class, and a published wait outranks a quota marker
# ---------------------------------------------------------------------------
def test_s21_a_published_wait_outranks_a_quota_marker_and_still_defers(served):
    """Row 1 of the precedence table (design D2): honouring a published wait is
    the capability's purpose, so the counter must agree with the action taken
    rather than report the more specific label."""
    body = json.dumps(
        {"error": {"message": "insufficient_quota", "code": "insufficient_quota", "retry_after": 30}}
    )
    url = served(429, {"Retry-After": "30"}, body)

    exc, hits = _call(url)

    assert type(exc) is RateLimitDeferral
    assert exc.wait_s == pytest.approx(30.0)
    assert hits == 1

    stats = sc.get_provider_refusal_stats()
    assert stats["refusals_rate_limit"] == 1
    assert stats["refusals_quota"] == 0, "a deferral is not reported as a quota refusal"
    assert sum(stats[key] for key in REFUSAL_KEYS) == 1, "exactly one counted class"


@pytest.mark.parametrize(
    "status,body,expected_type",
    [(404, BODY_NOT_FOUND, FileNotFoundError), (400, BODY_BAD_REQUEST, NetworkError)],
    ids=["404", "400"],
)
def test_s21_a_non_refusal_status_counts_no_refusal_class(served, status, body, expected_type):
    """A missing resource and a malformed request are not refusals: counting them
    would inflate the surface an operator reads for provider health."""
    url = served(status, {}, body)

    exc, _hits = _call(url)

    assert type(exc) is expected_type
    stats = sc.get_provider_refusal_stats()
    assert sum(stats[key] for key in REFUSAL_KEYS) == 0, "not a refusal, so not counted"


def test_s21_no_outcome_advances_more_than_one_refusal_counter(served):
    url = served(403, {}, BODY_QUOTA_403)
    _call(url)

    advanced = [key for key in REFUSAL_KEYS if sc.get_provider_refusal_stats()[key]]
    assert advanced == ["refusals_quota"], f"exactly one class advanced, got {advanced}"


# ---------------------------------------------------------------------------
# PRT-S23: the rollback flag silences the counters too
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "status,headers,body,expected_type,expected_message",
    [
        (401, {}, BODY_AUTH, NetworkError, "Authentication failed (HTTP 401)"),
        (403, {}, BODY_QUOTA_403, NetworkError, "Authentication failed (HTTP 403)"),
        (503, {}, BODY_SERVER, ConnectionError, f"Server error (HTTP 503): {BODY_SERVER}"),
        (403, {}, BODY_LIMIT, NetworkError, "Authentication failed (HTTP 403)"),
    ],
    ids=["auth", "quota", "transient", "marked 403"],
)
def test_s23_with_the_flag_off_no_refusal_class_is_counted(
    served, monkeypatch, status, headers, body, expected_type, expected_message
):
    """A half-rollback must not keep the new half (PRT-S13): with the flag false
    the transport takes the legacy branches and increments nothing at all.

    The last row is the sharpest case - a marked 403 returns to the legacy
    authentication verdict, because without the flag there is no capacity
    classification to prefer.
    """
    monkeypatch.setattr(sc, "_configured_classify_refusals", lambda: False)
    url = served(status, headers, body)

    exc, _hits = _call(url)

    assert type(exc) is expected_type
    assert str(exc) == expected_message
    assert sum(sc.get_provider_refusal_stats().values()) == 0, "rollback silences every counter"


# ---------------------------------------------------------------------------
# PRT-S25: the boundary of the refusal surface
# ---------------------------------------------------------------------------
def test_s25_a_harvested_credential_failure_is_not_a_provider_refusal(served):
    """The refusal surface describes **our** provider credentials being turned
    away, not the credentials this harvester collects.

    Testing a harvested key goes through the check surface (`chat`), which
    already classifies the outcome by reason (`INVALID_KEY`, `NO_QUOTA`, …) and
    feeds the credential-liveness metrics and the adaptive budget.  Counting it
    here as well would make `refusals_auth` a measure of how many leaked keys are
    dead — in a normal run, nearly all of them — and drown the signal an operator
    reads the `ProviderRefusals` line for.  This pin holds that boundary.

    The key below is synthetic; no credential material is used or logged.
    """
    url = served(401, {}, BODY_AUTH)
    sc.reset_provider_refusal_stats()

    code, _message = sc.chat(
        url=url,
        headers={"authorization": "Bearer sk-synthetic-probe-key-000"},
        model="probe-model",
        retries=1,
        timeout=5,
    )

    assert code == 401, "the check surface still classifies the outcome itself"
    stats = sc.get_provider_refusal_stats()
    assert sum(stats[key] for key in REFUSAL_KEYS) == 0, (
        "a harvested credential's rejection is not our provider refusing us"
    )
