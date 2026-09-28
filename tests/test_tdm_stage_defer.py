"""Stage-level accounting of a gather fetch withheld by the system's own budget.

Traceability: failure-handling-FH3-S5, FH3-S12.

Contract under test: a withholding by the local rate-limit budget is the THIRD
outcome (deferral), not a failure-empty.  It must increment ``tasks_deferred``
only - never ``failure_empties_detected``, ``total_errors``, ``tasks_requeued``
or ``tasks_dropped_max_retries`` - must leave ``attempts``, ``created_at`` and the
dedup identity untouched so the queue's ``max_age_hours`` gate stays anchored to
first creation, and must write no registry outcome at all.

Harness: a real ``AcquisitionStage`` over a real registry in the tmp workspace
(the builders are imported from ``tests/test_fh_gather_fidelity.py``), with the
real ``search.client`` chain running against a stubbed HTTP session and a real
``RateLimiter`` whose token supply is exhausted at the decision seam
(``GitHubClient._limit`` -> False, which in production happens only after the
scheduled wait was slept and a competing worker took the token).  The basket rate
is set high so each deferral's bounded wait is ~10 ms and the test stays fast.

RED at plan time: the withholding branch raises ``TransientFetchError``
(``search/client.py:1319-1322``), so the worker loop takes the failure-empty path
- ``failure_empties_detected`` and ``tasks_requeued`` advance, ``attempts`` is
incremented, and the strict-mode policy eventually drops the task loudly without
it ever having reached the network.
"""

import time

import pytest

from config.schemas import RateLimitConfig, StageConfig, TaskConfig
from constant.system import SERVICE_TYPE_GITHUB_RAW
from core.models import Patterns
from search import client as sc
from stage.base import StageResources
from stage.definition import AcquisitionStage
from tests.test_fh_gather_fidelity import (
    KEY_PATTERN,
    PROVIDER,
    URL,
    FakeAuth,
    _registry,
    _rows,
    _task,
)
from tools.ratelimit import RateLimiter


class _FakeResponse:
    def __init__(self, status=200, text=""):
        self.status_code = status
        self.text = text
        self.content = text.encode("utf-8")
        self.headers = {}
        self.ok = 200 <= status < 300

    def raise_for_status(self):
        if not self.ok:
            import requests

            raise requests.HTTPError(f"{self.status_code}", response=self)

    def iter_content(self, chunk_size=8192):
        data = self.content
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]


class _FakeSession:
    def __init__(self):
        self.calls = []

    def request(self, method=None, url=None, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return _FakeResponse(200, "APP_KEY=sk-abcdefgh01234567")

    def get(self, url, **kwargs):
        return self.request(method="GET", url=url, **kwargs)


def _resources(mode, registry=None):
    from config.schemas import Config

    cfg = Config()
    cfg.pipeline.failure_handling = mode
    cfg.gather.transport = "raw"
    return StageResources(
        limiter=None,
        providers={},
        config=cfg,
        task_configs={
            PROVIDER: TaskConfig(
                name=PROVIDER,
                enabled=True,
                provider_type="openai_like",
                use_api=False,
                stages=StageConfig(search=True, gather=True, check=True, inspect=True),
                patterns=Patterns(key_pattern=KEY_PATTERN),
            )
        },
        auth=FakeAuth(),
        registry=registry,
    )


@pytest.fixture
def starved_gather(monkeypatch):
    """Install a module client whose local budget never has a token."""
    limiter = RateLimiter(
        {
            SERVICE_TYPE_GITHUB_RAW: RateLimitConfig(
                base_rate=100.0, burst_limit=1, adaptive=True
            )
        }
    )
    sess = _FakeSession()
    gh = sc.GitHubClient(limiter=limiter)
    monkeypatch.setattr(sc, "_HTTP_SESSION", sess)
    monkeypatch.setattr(sc, "_github_client", gh)
    monkeypatch.setattr(gh, "_limit", lambda service, credential=None: False)
    sc.reset_gather_transport_stats()
    return gh, sess, limiter


def _run_until_deferred(stage, task, count, deadline=15.0):
    """Start the stage, feed one task, and stop once it deferred ``count`` times."""
    stage.start()
    try:
        assert stage.put_task(task) is True
        end = time.time() + deadline
        while time.time() < end:
            if stage.tasks_deferred >= count:
                return True
            time.sleep(0.02)
        return False
    finally:
        stage.stop(timeout=3.0)


# ---------------------------------------------------------------------------
# failure-handling-FH3-S5
# ---------------------------------------------------------------------------
def test_fh3_s5_own_budget_withholding_is_a_deferral_not_a_failure_empty(
    workspace, starved_gather, monkeypatch
):
    """WHEN a gather fetch is withheld by the local budget and no request reaches
    the network THEN the outcome is a deferral: counted on its own ledger, never
    as a failure-empty or an error, and nothing is written to the registry - so
    the link stays eligible exactly as if it had never been attempted.
    """
    gh, sess, limiter = starved_gather
    registry = _registry(workspace)
    try:
        stage = AcquisitionStage(
            _resources("strict", registry=registry), lambda out: None, thread_count=1, max_retries=3
        )
        task = _task()

        assert _run_until_deferred(stage, task, 3), "the worker loop never reached three deferrals"

        registry.flush(10.0)

        assert stage.tasks_deferred >= 3
        assert stage.failure_empties_detected == 0, "a withholding is not a failure-empty"
        assert stage.total_errors == 0, "a withholding is not an error"
        assert stage.tasks_requeued == 0, "a withholding does not consume the requeue budget"
        assert stage.tasks_dropped_max_retries == 0, "a withholding is never a drop"
        assert stage.total_processed == 0, "a deferral is not a completion"
        assert sess.calls == [], "no request may reach the network while the budget is empty"

        # no registry trace of the withheld attempts: neither a visit_status row
        # nor a coverage row (contrast test_s7, where a FAILED fetch records
        # visit_status='failed')
        assert _rows(workspace, "SELECT visit_status FROM links") == []
        assert _rows(workspace, "SELECT COUNT(*) FROM link_coverage")[0][0] == 0
    finally:
        registry.stop()


# ---------------------------------------------------------------------------
# failure-handling-FH3-S12
# ---------------------------------------------------------------------------
def test_fh3_s12_own_budget_withholding_preserves_identity_and_age(starved_gather):
    """WHEN a gather task is withheld by the system's own budget repeatedly THEN
    its attempt counter, creation timestamp and deduplication identity are
    preserved across every withholding, no error/requeue/processed counter
    advances, and the queue's age gate therefore remains the single ceiling on
    how long it may circulate.
    """
    stage = AcquisitionStage(
        _resources("strict"), lambda out: None, thread_count=1, max_retries=3
    )
    task = _task()

    attempts_before = task.attempts
    created_before = task.created_at
    dedup_before = stage._generate_id(task)

    assert _run_until_deferred(stage, task, 4), "the worker loop never reached four deferrals"

    assert stage.tasks_deferred >= 4
    assert task.attempts == attempts_before == 0, "a withholding must not burn an attempt"
    assert task.created_at == created_before, (
        "created_at must stay anchored to first creation, otherwise the max_age_hours "
        "gate can never purge a task that keeps being withheld"
    )
    assert stage._generate_id(task) == dedup_before
    assert stage.total_errors == 0
    assert stage.tasks_requeued == 0
    assert stage.tasks_dropped_max_retries == 0
    assert stage.total_processed == 0
