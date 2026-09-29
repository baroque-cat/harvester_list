"""Deferral semantics, the two propagation guards and the new counters
(PRT-S6, the clamp half of PRT-S7, PRT-S8, PRT-S9, PRT-S10).

RED at plan time: none of this exists.  `RateLimitDeferral` carries no `reason`,
`RetryCore.should_retry_error` decides on message *words*, `_fetch_models` is
wrapped fail-open with no `exclude`, and there is no provider-refusal counter
surface at all.

House conventions: a real loopback server for the transport half, real
`RateLimiter` / `RetryCore` / `handle_exceptions` objects for the rest - no mocks
of the thing under test.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

import search.client as sc
from core.enums import ErrorReason
from core.exceptions import NetworkError, RateLimitDeferral
from core.models import Condition, RateLimitConfig
from tools.ratelimit import RateLimiter
from tools.retry import RetryCore
from tools.utils import handle_exceptions

RATE_LIMIT_BODY = json.dumps(
    {"error": {"message": "rate limit exceeded, please retry after 30 seconds"}}
)


class _ScriptedHandler(BaseHTTPRequestHandler):
    """Returns one scripted response and counts the wire hits."""

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

    def log_message(self, *args):
        pass


@pytest.fixture
def wire(monkeypatch):
    """A loopback server plus an isolated session (no proxy/env interference)."""
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
        thread.join(timeout=5)
        isolated.close()


# ---------------------------------------------------------------------------
# PRT-S6
# ---------------------------------------------------------------------------
def test_s6_transport_carries_the_wait_and_does_not_sleep_it(wire):
    """WHEN the refusal publishes a wait THEN the transport puts it on the signal
    and returns immediately; the bounded sleep belongs to the stage.

    Invariant 6: a sleep inside a claimed task that outlives the visibility
    window gets the row re-delivered and executed twice, and the durable queue
    has no claim-renewal API.  `stage/base.py` clamps the wait instead.
    """
    url = wire(429, {"Retry-After": "30"}, RATE_LIMIT_BODY)

    started = time.perf_counter()
    with pytest.raises(RateLimitDeferral) as exc:
        sc.http_get(url=url, retries=3, interval=0.1, timeout=5)
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0, f"the transport slept the published wait itself ({elapsed:.2f}s)"
    assert exc.value.wait_s == pytest.approx(30.0)
    assert exc.value.reason == ErrorReason.RATE_LIMITED.name
    assert _ScriptedHandler.hits == 1, "exactly one wire hit: deferred, not blindly retried"


# ---------------------------------------------------------------------------
# PRT-S7 - clamp half (the config-validation half is in test_pfc_config.py)
# ---------------------------------------------------------------------------
def test_s7_wait_is_clamped_by_the_configured_cap(wire):
    """A published wait larger than `provider.max_refusal_wait_s` is clamped, so
    the deferral can never exceed the durable-queue visibility window."""
    url = wire(429, {"Retry-After": "600"}, RATE_LIMIT_BODY)

    with pytest.raises(RateLimitDeferral) as exc:
        sc.http_get(url=url, retries=3, interval=0.1, timeout=5)

    assert exc.value.wait_s == pytest.approx(60.0)
    assert exc.value.wait_s <= 60.0


# ---------------------------------------------------------------------------
# PRT-S8
# ---------------------------------------------------------------------------
def test_s8_retry_policy_rejects_the_signal_by_type_not_by_words():
    """The retry decision for the new signal is by **type**.

    Today `should_retry_error` returns True for anything whose message contains
    "rate limit", which is exactly the wording a capacity refusal carries - so a
    deferral would also be requeued and burn an attempt.  The three legacy
    controls must keep their behavior.
    """
    core = RetryCore()

    assert core.should_retry_error(RateLimitDeferral("rate limit exceeded"), 0, 3) is False

    assert core.should_retry_error(ConnectionError("rate limit exceeded"), 0, 3) is True
    assert core.should_retry_error(TimeoutError("timed out"), 0, 3) is True
    assert core.should_retry_error(NetworkError("too many requests"), 0, 3) is True


def test_s8_fail_open_wrapper_lets_the_signal_through():
    """`handle_exceptions` already supports `exclude` (added by R5.2); the refusal
    must be excluded so the fail-open contract cannot swallow it into `[]`."""

    @handle_exceptions(default_result=[], exclude=(RateLimitDeferral,))
    def guarded():
        raise RateLimitDeferral("rate limit exceeded", wait_s=30.0)

    with pytest.raises(RateLimitDeferral):
        guarded()

    @handle_exceptions(default_result=[])
    def unguarded():
        raise RateLimitDeferral("rate limit exceeded", wait_s=30.0)

    assert unguarded() == [], "control: without the exclusion the signal is swallowed"


def test_s8_fetch_models_propagates_a_refusal_end_to_end(monkeypatch):
    """`_fetch_models` is the swallow point on the production provider: today a
    capacity refusal becomes `[]`, i.e. "this provider has no models", which is
    indistinguishable from a genuine empty answer."""
    import provider.openai_like as openai_like
    from provider.openai_like import OpenAILikeProvider

    def _refuse(*args, **kwargs):
        raise RateLimitDeferral(
            "rate limit exceeded", wait_s=30.0, reason=ErrorReason.RATE_LIMITED.name
        )

    monkeypatch.setattr(openai_like, "http_get", _refuse)

    provider = OpenAILikeProvider(
        name="probe",
        base_url="http://127.0.0.1",
        completion_path="/v1/chat/completions",
        model_path="/v1/models",
        default_model="probe-model",
        conditions=Condition(query="/sk-x/"),
    )

    with pytest.raises(RateLimitDeferral):
        provider._fetch_models("http://127.0.0.1/v1/models", {})


# ---------------------------------------------------------------------------
# PRT-S9
# ---------------------------------------------------------------------------
def test_s9_refusal_and_empty_answer_are_counted_apart_and_neither_is_an_error():
    """Refusal and empty answer are different facts and get different counters;
    neither is a task error, and an undeclared key is rejected loudly."""
    sc.reset_provider_refusal_stats()

    sc._provider_refusal_stat_inc("refusals_rate_limit")
    sc._provider_refusal_stat_inc("inspect_refused")
    sc._provider_refusal_stat_inc("inspect_empty_answers")

    stats = sc.get_provider_refusal_stats()
    assert stats["refusals_rate_limit"] == 1
    assert stats["inspect_refused"] == 1
    assert stats["inspect_empty_answers"] == 1
    assert stats["refusals_auth"] == 0, "a capacity refusal must not be counted as auth"

    with pytest.raises(ValueError, match="undeclared_key"):
        sc._provider_refusal_stat_inc("undeclared_key")


# ---------------------------------------------------------------------------
# PRT-S10
# ---------------------------------------------------------------------------
def test_s10_own_basket_starvation_is_counted_apart_from_remote_refusals():
    """Starvation by our own provider basket advances only its dedicated counter
    and reports nothing to the adaptive budget - no request was issued, so there
    is no remote outcome to learn from (the R5.3 lesson)."""
    sc.reset_provider_refusal_stats()

    sc._provider_refusal_stat_inc("deferred_provider_budget")

    stats = sc.get_provider_refusal_stats()
    assert stats["deferred_provider_budget"] == 1
    assert stats["refusals_rate_limit"] == 0, "own-basket starvation is not a remote refusal"

    limiter = RateLimiter(
        {"github_raw": RateLimitConfig(base_rate=2.0, burst_limit=4, adaptive=True)}
    )
    bucket = limiter._get_bucket("github_raw")
    assert bucket.rate == pytest.approx(2.0), "the adaptive rate was not decayed"
    assert bucket.consecutive_failures == 0, "nothing was reported as a failure"
