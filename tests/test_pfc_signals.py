"""Wire-signal classification on the LLM-provider surface (PRT-S1..PRT-S5).

RED at plan time: `http_get` raises `NetworkError("Authentication failed (HTTP
403)")` for a 403 that carries rate-limit text, never reads `Retry-After`, and
blindly retries a 429 three times before the fail-open wrapper turns it into
`[]`.  The typed deferral signal and the shared predicate do not exist yet.

House conventions: real `http.server` on the loopback interface and the shipped
`search.client.http_get`, no mocks; assertions are on the exception **type**, the
wait it carries and its reason class - never on a message substring alone.
No third-party provider dialect is assumed (design D11).
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

import search.client as sc
from core.enums import ErrorReason
from core.exceptions import NetworkError, RateLimitDeferral

RATE_LIMIT_BODY = json.dumps(
    {"error": {"message": "rate limit exceeded, please retry after 30 seconds"}}
)
AUTH_BODY = json.dumps({"error": {"message": "invalid api key"}})


class _ScriptedHandler(BaseHTTPRequestHandler):
    """Returns one scripted response and counts the hits."""

    script = (200, {}, "{}")
    hits = 0
    lock = threading.Lock()

    def do_GET(self):  # noqa: N802 - http.server contract
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
    """A loopback server plus an isolated session, so no proxy/env can interfere.

    Yields a callable `serve(status, headers, body)` that scripts the next
    response and returns its URL.  `_ScriptedHandler.hits` counts how many times
    the transport actually reached the wire, which is what separates "deferred"
    from "blindly retried".
    """
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


# ---------------------------------------------------------------------------
# PRT-S1: a published wait becomes a bounded deferral instead of hammering
# ---------------------------------------------------------------------------
def test_s1_published_wait_defers_instead_of_blind_retry(served):
    url = served(429, {"Retry-After": "30"}, RATE_LIMIT_BODY)

    with pytest.raises(RateLimitDeferral) as excinfo:
        sc.http_get(url=url, retries=3, interval=0.1, timeout=5)

    signal = excinfo.value
    assert signal.wait_s == pytest.approx(30.0), "the published wait must travel on the signal"
    assert signal.reason == ErrorReason.RATE_LIMITED.name
    assert _ScriptedHandler.hits == 1, "a published wait must not be retried in-thread"


# ---------------------------------------------------------------------------
# PRT-S2: a 403 carrying a limit marker is capacity, not authentication
# ---------------------------------------------------------------------------
def test_s2_forbidden_with_limit_marker_is_capacity_not_auth(served):
    url = served(403, {"Retry-After": "12"}, RATE_LIMIT_BODY)

    with pytest.raises(RateLimitDeferral) as excinfo:
        sc.http_get(url=url, retries=3, interval=0.1, timeout=5)

    assert excinfo.value.wait_s == pytest.approx(12.0)
    assert excinfo.value.reason == ErrorReason.RATE_LIMITED.name
    assert _ScriptedHandler.hits == 1


# ---------------------------------------------------------------------------
# PRT-S3: a 403 without any limit marker stays an authentication failure
# ---------------------------------------------------------------------------
def test_s3_forbidden_without_marker_stays_an_auth_failure(served):
    url = served(403, {}, AUTH_BODY)

    with pytest.raises(NetworkError) as excinfo:
        sc.http_get(url=url, retries=3, interval=0.1, timeout=5)

    assert not isinstance(excinfo.value, RateLimitDeferral), "an auth failure must not defer"
    assert "403" in str(excinfo.value)
    assert _ScriptedHandler.hits == 1, "an auth failure is not retried (unchanged)"


# ---------------------------------------------------------------------------
# PRT-S4: a 429 with no published wait keeps the legacy retryable transient
# ---------------------------------------------------------------------------
def test_s4_rate_limit_without_published_wait_keeps_legacy_transient(served):
    url = served(429, {}, json.dumps({"error": {"message": "too many requests"}}))

    with pytest.raises(ConnectionError) as excinfo:
        sc.http_get(url=url, retries=3, interval=0.1, timeout=5)

    assert not isinstance(excinfo.value, RateLimitDeferral), (
        "without a published wait there is nothing to defer on (design D4)"
    )
    assert _ScriptedHandler.hits == 3, "the legacy bounded retry is preserved verbatim"


# ---------------------------------------------------------------------------
# PRT-S5: one wire signal, one verdict on both surfaces
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "status,body,expected",
    [
        (429, RATE_LIMIT_BODY, True),
        (403, RATE_LIMIT_BODY, True),
        (403, AUTH_BODY, False),
        (401, AUTH_BODY, False),
        (503, RATE_LIMIT_BODY, False),
    ],
)
def test_s5_both_surfaces_classify_the_same_response_identically(status, body, expected):
    from tools.http_signals import is_capacity_refusal, wait_from_content, wait_from_headers

    assert is_capacity_refusal(status, body) is expected
    # The gather surface keeps its existing behavior, now sourced from the shared
    # predicate rather than from its own private copy (design D1).
    assert bool(sc._gather_is_rate_limit_signal(status, body)) is expected

    if status in (429, 403):
        assert wait_from_headers({"Retry-After": "30"}) == pytest.approx(30.0)
        assert wait_from_content(RATE_LIMIT_BODY) == pytest.approx(30.0)
        assert wait_from_headers({}) is None
        assert wait_from_content(AUTH_BODY) is None
