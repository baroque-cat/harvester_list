"""Check-stage provider-basket starvation becomes a deferral (FH4-S1..S3), and
one flag governs both halves of the rollback (PRT-S13).

RED at plan time: `_check_worker` sleeps the basket wait **inside the claimed
task** (unclamped - invariant 6 exposure) and then raises `TransientFetchError`,
so the row is requeued and burns an attempt.  The dedicated counter, the
`provider.classify_refusals` flag and the typed deferral do not exist.

Harness: a real `CheckStage` from the registry over a real `RateLimiter` with a
drained per-provider basket, plus a real registry in the tmp workspace so "writes
no outcome" is asserted against the database rather than against a mock.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

import search.client as sc
import stage.definition as definition
from core.exceptions import RateLimitDeferral, TransientFetchError
from core.models import CheckResult, Patterns, ResultStorage, Service
from core.types import IProvider
from stage.definition import CheckStage
from stage.factory import TaskFactory
from tests.test_fh_gather_fidelity import KEY_PATTERN, PROVIDER, _registry, _rows
from tests.test_fh_stage_modes import _resources
from tools.ratelimit import RateLimiter
from tools.utils import get_service_name

RATE_LIMIT_BODY = json.dumps(
    {"error": {"message": "rate limit exceeded, please retry after 30 seconds"}}
)


class _RefusingProvider(IProvider):
    """A provider whose `check` is never reached while the basket is starved.

    Satisfies the real `IProvider` lookup contract so the worker reaches the
    rate-limiting block instead of the unknown-provider configuration path.
    """

    name = PROVIDER
    conditions = []

    @property
    def result(self):
        return ResultStorage()

    def get_patterns(self):
        return Patterns(key_pattern=KEY_PATTERN)

    def check(self, token, address="", endpoint="", model="", **kwargs):  # pragma: no cover
        raise AssertionError("check() must not run while the basket has no token")

    def inspect(self, token, address="", endpoint="", **kwargs):  # pragma: no cover
        return []


def _starved_limiter(service, base_rate=100.0):
    """A real limiter whose basket for `service` holds no token.

    `base_rate` is high so the deferral wait derived from the refill time is
    ~10 ms and the worker loop settles inside the test's deadline.  The capacity
    is pinned to zero (not merely its current fill) so the bucket cannot refill
    between construction and the worker's acquire - otherwise the real token
    bucket would hand out a token after ~10 ms and the starvation precondition
    would race the test harness rather than being deterministic.
    """
    from config.schemas import RateLimitConfig

    limiter = RateLimiter({service: RateLimitConfig(base_rate=base_rate, burst_limit=1, adaptive=True)})
    bucket = limiter._get_bucket(service)
    bucket.tokens = 0.0
    bucket.burst = 0
    return limiter


def _check_stage(limiter, classify=True, registry=None, max_retries=2):
    resources = _resources("strict", limiter=limiter, providers={PROVIDER: _RefusingProvider()}, registry=registry)
    resources.config.provider.classify_refusals = classify
    return CheckStage(resources, lambda out: None, thread_count=1, max_retries=max_retries)


def _check_task():
    return TaskFactory.create_check_task(
        PROVIDER, Service(address="https://api.example", endpoint="/v1", key="k-1", model="m")
    )


def _run_until(stage, task, predicate, deadline=15.0):
    """Start the stage, feed one task, and stop once `predicate(stage)` holds."""
    stage.start()
    try:
        assert stage.put_task(task) is True
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            if predicate(stage):
                return True
            time.sleep(0.01)
        return predicate(stage)
    finally:
        stage.stop(timeout=5.0)


# ---------------------------------------------------------------------------
# FH4-S1
# ---------------------------------------------------------------------------
def test_fh4_s1_starved_check_defers_without_burning_an_attempt(workspace):
    """A starved check defers: no failure-empty, no error, no requeue, no drop,
    the task keeps its identity and age, and nothing is written to the registry."""
    registry = _registry(workspace)
    try:
        service = get_service_name(PROVIDER)
        limiter = _starved_limiter(service)
        stage = _check_stage(limiter, registry=registry)
        task = _check_task()
        task_id, created_at = task.task_id, task.created_at

        assert _run_until(stage, task, lambda s: s.tasks_deferred >= 1), "never reached a deferral"

        stats = stage.get_stats()
        # The task is always-starved, so it is re-queued and re-claimed as soon
        # as its bounded sleep elapses: the exact number of deferrals is a timing
        # artifact, not the contract (the R5.3 gather precedent pins ``>= N``).
        assert stats.tasks_deferred >= 1
        assert stats.failure_empties_detected == 0
        assert stage.total_errors == 0
        assert stats.tasks_requeued == 0
        assert stage.tasks_dropped_max_retries == 0
        assert stage.total_processed == 0

        assert task.task_id == task_id, "deferral must not mint a new identity"
        assert task.created_at == created_at, "deferral must not reset the age gate"
        assert task.attempts == 0, "a deferral is not a retry: no attempt burned"

        registry.flush(10.0)
        assert _rows(workspace, "SELECT visit_status FROM links") == []
    finally:
        registry.stop()


def test_fh4_s1_no_unclamped_sleep_inside_the_claimed_task(monkeypatch):
    """Invariant 6: the worker must not sleep the basket wait inside the claim.

    `process_task` is called directly (no worker loop), so any recorded sleep is
    the in-claim sleep this change removes.
    """
    slept = []
    monkeypatch.setattr(definition.time, "sleep", lambda seconds: slept.append(seconds))

    service = get_service_name(PROVIDER)
    stage = _check_stage(_starved_limiter(service))

    with pytest.raises(RateLimitDeferral) as exc:
        stage.process_task(_check_task())

    assert slept == [], f"the claim slept in-thread: {slept}"
    assert exc.value.wait_s > 0
    assert exc.value.stage_pause is False, "the basket is per provider, so no stage-wide pause"
    assert exc.value.provider == PROVIDER


# ---------------------------------------------------------------------------
# FH4-S2
# ---------------------------------------------------------------------------
def test_fh4_s2_starvation_is_counted_apart_and_reports_nothing_to_the_budget(monkeypatch):
    service = get_service_name(PROVIDER)
    limiter = _starved_limiter(service)

    reported = []
    real_report = limiter.report_result
    monkeypatch.setattr(limiter, "report_result", lambda *a, **k: reported.append((a, k)) or real_report(*a, **k))

    sc.reset_provider_refusal_stats()
    sc.reset_gather_transport_stats()

    stage = _check_stage(limiter)
    with pytest.raises(RateLimitDeferral):
        stage.process_task(_check_task())

    refusals = sc.get_provider_refusal_stats()
    assert refusals["deferred_provider_budget"] == 1
    assert refusals["refusals_rate_limit"] == 0, "own-basket starvation is not a remote refusal"
    assert sc.get_gather_transport_stats()["deferred_rate_limit"] == 0

    assert reported == [], "no request was issued, so nothing may be reported to the adaptive budget"
    bucket = limiter._get_bucket(service)
    assert bucket.consecutive_failures == 0
    assert bucket.rate == pytest.approx(100.0), "the adaptive rate was not decayed"


# ---------------------------------------------------------------------------
# FH4-S3 + PRT-S13 (one flag governs both halves)
# ---------------------------------------------------------------------------
def test_fh4_s3_legacy_starvation_returns_with_the_rollback_flag():
    """`provider.classify_refusals: false` restores the pre-change classification
    byte-for-byte: `TransientFetchError`, a requeue and a burned attempt."""
    service = get_service_name(PROVIDER)
    limiter = _starved_limiter(service)
    stage = _check_stage(limiter, classify=False)
    task = _check_task()

    sc.reset_provider_refusal_stats()

    with pytest.raises(TransientFetchError, match="provider limiter starved"):
        stage.process_task(task)

    assert _run_until(stage, _check_task(), lambda s: s.tasks_requeued >= 1), "legacy path must requeue"
    assert stage.get_stats().tasks_deferred == 0
    assert sc.get_provider_refusal_stats()["deferred_provider_budget"] == 0


def test_prt_s13_flag_off_restores_the_legacy_transport_classification(wire):
    """The same flag also governs `http_get`: with it off, a published-wait 429
    stays the legacy retryable transient and no refusal counter moves."""
    url = wire(429, {"Retry-After": "30"}, RATE_LIMIT_BODY)

    sc.reset_provider_refusal_stats()

    with pytest.raises(ConnectionError) as exc:
        sc.http_get(url=url, retries=3, interval=0.1, timeout=5)

    assert not isinstance(exc.value, RateLimitDeferral)
    assert _ScriptedHandler.hits == 3, "legacy behavior: blindly retried"
    stats = sc.get_provider_refusal_stats()
    assert stats["refusals_rate_limit"] == 0
    assert stats["deferred_provider_budget"] == 0


# ---------------------------------------------------------------------------
# loopback harness for PRT-S13
# ---------------------------------------------------------------------------
class _ScriptedHandler(BaseHTTPRequestHandler):
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
    """Loopback server + isolated session, with the rollback flag switched off."""
    monkeypatch.setattr(sc, "_configured_classify_refusals", lambda: False)

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
