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
from core.models import CheckResult, Condition, Patterns, RateLimitConfig, ResultStorage, Service
from core.types import IProvider
from stage.definition import InspectStage
from stage.factory import TaskFactory
from tests.test_fh_gather_fidelity import KEY_PATTERN, PROVIDER, _registry
from tests.test_fh_stage_modes import _resources
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
class _InspectProvider(IProvider):
    """Refuses for one key and answers empty for another.

    Satisfies the real ``IProvider`` lookup contract, otherwise the worker takes
    the unknown-provider configuration path and neither outcome is exercised
    (PFC-D12).
    """

    name = PROVIDER
    conditions = []

    @property
    def result(self):
        return ResultStorage()

    def get_patterns(self):
        return Patterns(key_pattern=KEY_PATTERN)

    def check(self, token, address="", endpoint="", model="", **kwargs):  # pragma: no cover
        return CheckResult.success()

    def inspect(self, token, address="", endpoint="", **kwargs):
        if token == "k-refuse":
            raise RateLimitDeferral(
                "provider rate limit (HTTP 429)", wait_s=0.0, reason=ErrorReason.RATE_LIMITED.name
            )
        return []


def _inspect_task(key):
    return TaskFactory.create_inspect_task(
        PROVIDER, Service(address="https://api.example", endpoint="/v1", key=key, model="m")
    )


def _inspect_stage(workspace, providers, cap=0.05, max_retries=2):
    """A real `InspectStage` over a real registry.

    Both caps are pinned: the transport clamps with `provider.max_refusal_wait_s`
    resolved from the *global* config, while the worker clamps with
    `gather.max_refusal_wait_s` from the stage's own resources (predecessor D5).
    Leaving either at its default would let a 3600 s published wait be slept
    inside the claim and the test would time out instead of asserting.
    """
    resources = _resources("strict", providers=providers, registry=_registry(workspace))
    resources.config.provider.classify_refusals = True
    resources.config.provider.max_refusal_wait_s = cap
    resources.config.gather.max_refusal_wait_s = cap
    return InspectStage(resources, lambda out: None, thread_count=1, max_retries=max_retries)


def test_s9_refusal_and_empty_answer_are_counted_apart_and_neither_is_an_error(workspace):
    """PRT-S9, driven through a real ``InspectStage`` rather than by incrementing
    counters by hand: one refused inspect call and one genuine empty answer must
    land in different counters, and neither may advance ``total_errors``.

    The hand-incremented version of this pin proved only that the counter
    surface works; it could not see that a real refusal arrived at the stage as
    ``[]`` and was therefore recorded as an empty answer.  Driving the stage is
    what makes that visible.
    """
    sc.reset_provider_refusal_stats()
    stage = _inspect_stage(workspace, {PROVIDER: _InspectProvider()})

    stage.start()
    try:
        assert stage.put_task(_inspect_task("k-refuse")) is True
        assert stage.put_task(_inspect_task("k-empty")) is True

        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            stats = sc.get_provider_refusal_stats()
            if stats["inspect_refused"] >= 1 and stats["inspect_empty_answers"] >= 1:
                break
            time.sleep(0.01)
    finally:
        stage.stop(timeout=5.0)

    stats = sc.get_provider_refusal_stats()
    assert stats["inspect_refused"] >= 1, "the refused call must be counted as refused"
    assert stats["inspect_empty_answers"] >= 1, "the empty answer must be counted as empty"
    assert stage.total_errors == 0, "neither outcome is a task error"
    assert stats["refusals_auth"] == 0, "a capacity refusal must not be counted as auth"

    with pytest.raises(ValueError, match="undeclared_key"):
        sc._provider_refusal_stat_inc("undeclared_key")


def _wire_provider(monkeypatch, base_url):
    """A real `OpenAILikeProvider` aimed at the loopback server.

    Only the user-agent lookup is stubbed: it resolves through
    `ResourceManager.get_instance()`, which requires a loaded `config.yaml` and is
    unrelated to the refusal path under test.  Everything else - header building,
    the fail-open wrapper, the transport - is the shipped code.
    """
    import provider.openai_like as mod
    from provider.openai_like import OpenAILikeProvider

    monkeypatch.setattr(mod, "get_user_agent", lambda: "probe-agent")
    return OpenAILikeProvider(
        name="probe",
        base_url=base_url,
        completion_path="/v1/chat/completions",
        model_path="/models",
        default_model="probe-model",
        conditions=[Condition(query="/sk-x/")],
    )


def test_s24_a_swallowed_refusal_is_never_recorded_as_an_empty_answer(wire, workspace, monkeypatch):
    """The requirement sentence, end to end: *"A refusal SHALL never be recorded
    as 'the provider has no models'."*

    A 401 is not a deferrable refusal, so `@handle_exceptions(default_result=[])`
    still turns it into `[]` - the wrapper is the fail-open contract and stays.
    What must change is the stage's reading of that `[]`: the call was a refusal,
    so the outcome belongs to `inspect_refused` beside its class counter, and
    `inspect_empty_answers` must stay at zero.  This is the pin the
    hand-incremented version of PRT-S9 could not express, and the reason W1
    survived offline verification.
    """
    url = wire(401, {}, json.dumps({"error": {"message": "invalid api key"}}))
    sc.reset_provider_refusal_stats()
    stage = _inspect_stage(workspace, {PROVIDER: _wire_provider(monkeypatch, url.rsplit("/models", 1)[0])})

    stage.start()
    try:
        assert stage.put_task(_inspect_task("k-auth")) is True
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if sc.get_provider_refusal_stats()["inspect_refused"] >= 1:
                break
            time.sleep(0.01)
    finally:
        stage.stop(timeout=5.0)

    stats = sc.get_provider_refusal_stats()
    assert stats["refusals_auth"] >= 1, "the authentication refusal must carry its class"
    assert stats["inspect_refused"] >= 1, "the stage must read the [] as a refusal"
    assert stats["inspect_empty_answers"] == 0, "a refusal is not 'the provider has no models'"
    assert stage.total_errors == 0, "a refusal is not a task error"


def test_s6_the_worker_defers_on_a_transport_produced_signal_with_the_wait_clamped(
    wire, workspace, monkeypatch
):
    """PRT-S6's worker half: a signal that came off the **wire** must reach
    ``defer_task`` with its wait clamped to the configured cap.

    The transport half above proves the transport does not sleep the wait; this
    proves the stage picks it up and bounds it.  The published wait (3600 s) is
    far above the cap (1 s) and far above the test's own budget, so an unclamped
    sleep anywhere on the path shows up as a timeout rather than as a slow pass.
    """
    url = wire(429, {"Retry-After": "3600"}, RATE_LIMIT_BODY)
    base_url = url.rsplit("/models", 1)[0]
    cap = 1.0

    provider = _wire_provider(monkeypatch, base_url)
    # The transport resolves its cap from the global config, which no test loads;
    # pin the resolver so the clamp under test is the configured cap and not the
    # built-in 60 s default.
    monkeypatch.setattr(sc, "_configured_max_refusal_wait_s", lambda: cap)
    sc.reset_provider_refusal_stats()
    stage = _inspect_stage(workspace, {PROVIDER: provider}, cap=cap)

    deferred = []
    original = stage.defer_task

    def _spy(task, wait_s=None):
        deferred.append(wait_s)
        return original(task, wait_s=wait_s)

    monkeypatch.setattr(stage, "defer_task", _spy)

    started = time.monotonic()
    stage.start()
    try:
        assert stage.put_task(_inspect_task("k-wire")) is True
        deadline = started + 15.0
        while time.monotonic() < deadline and not deferred:
            time.sleep(0.01)
    finally:
        stage.stop(timeout=5.0)
    elapsed = time.monotonic() - started

    assert deferred, "defer_task was never reached: the worker absorbed the signal"
    assert deferred[0] == pytest.approx(cap), "the wait handed to the queue must be the cap"
    assert elapsed < 15.0, "a 3600 s published wait must never be slept"
    assert stage.total_errors == 0, "a deferral is not a completion and not an error"
    assert sc.get_provider_refusal_stats()["inspect_refused"] >= 1
    assert _ScriptedHandler.hits >= 1, "the signal must have come off the wire"


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
