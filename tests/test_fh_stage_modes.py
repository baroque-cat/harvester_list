"""Stage-mode contract for failure handling (fix-silent-losses).

Traceability: failure-handling-S4, S5, S6, S9, S10, S11, S12, S13, S14.

Harness: SearchStage/CheckStage are invoked with a mocked search client that
raises the typed transient failure; the tri-mode flag is set on
``config.pipeline.failure_handling`` (design D3). No network.

RED state notes:
- ``TransientFetchError`` import fails pre-fix (expected RED for the file);
- S5 and S10 encode behavior that already exists (dedup gate admits bounded
  requeues; legacy == pre-change) - they are regression guards expected GREEN
  once the module imports, and MUST stay green after the fix;
- counters (``failure_empties_detected``, ``tasks_requeued``,
  ``tasks_dropped_max_retries``) do not exist pre-fix -> AttributeError RED.
"""

import logging
import time

import pytest

from config.schemas import Config, StageConfig, TaskConfig
from core.exceptions import TransientFetchError  # RED driver: absent pre-fix
from core.models import Patterns, SearchTask, Service
from search import client as search_client
from stage.base import StageResources
from stage.definition import CheckStage, SearchStage
from stage.factory import TaskFactory
from tools.state import GithubCredentialLimited

PROVIDER = "deepseek"
KEY_PATTERN = r"sk-[A-Za-z0-9]{16}"


class FakeAuth:
    def get_session(self):
        return "session-token"

    def get_token(self):
        return "api-token"

    def get_user_agent(self):
        return "test-agent"


def _task_config(use_api=False):
    return TaskConfig(
        name=PROVIDER,
        enabled=True,
        provider_type="openai_like",
        use_api=use_api,
        stages=StageConfig(search=True, gather=True, check=True, inspect=True),
        patterns=Patterns(key_pattern=KEY_PATTERN),
    )


def _cfg(mode):
    cfg = Config()
    # The fix formalizes this field (default "shadow"); setattr keeps the test
    # independent of constructor plumbing.
    cfg.pipeline.failure_handling = mode
    return cfg


def _resources(mode, limiter=None, providers=None, registry=None):
    return StageResources(
        limiter=limiter,
        providers=providers or {},
        config=_cfg(mode),
        task_configs={PROVIDER: _task_config()},
        auth=FakeAuth(),
        registry=registry,
    )


def _search_stage(mode, max_retries=0, **res_kwargs):
    return SearchStage(_resources(mode, **res_kwargs), lambda out: None,
                       thread_count=1, max_retries=max_retries)


def _search_task():
    return SearchTask(provider=PROVIDER, query='"sk-"', regex=KEY_PATTERN, page=1, use_api=False)


def _boom(monkeypatch, calls=None):
    def failing(**kwargs):
        if calls is not None:
            calls.append(1)
        raise TransientFetchError("network down")

    monkeypatch.setattr(search_client, "search_with_count", failing)


def _wait_drain(stage, deadline=6.0):
    end = time.time() + deadline
    while time.time() < end:
        if stage.queue.empty() and not stage._has_active_workers():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# failure-handling-S10 (regression guard: legacy == pre-change)
# ---------------------------------------------------------------------------
def test_s10_legacy_mode_matches_pre_change_behavior(monkeypatch):
    calls = []
    _boom(monkeypatch, calls)
    stage = _search_stage("legacy")

    out = stage.process_task(_search_task())

    assert out is None                 # swallowed completion, exactly as pre-change
    assert len(calls) == 1             # no requeue happened
    assert stage.total_errors == 0     # nothing propagated to the loop
    assert getattr(stage, "failure_empties_detected", 0) == 0  # counters inert in legacy


# ---------------------------------------------------------------------------
# failure-handling-S11
# ---------------------------------------------------------------------------
def test_s11_shadow_counts_and_logs_without_enforcing(monkeypatch, caplog):
    calls = []
    _boom(monkeypatch, calls)
    stage = _search_stage("shadow")

    with caplog.at_level(logging.WARNING):
        out = stage.process_task(_search_task())

    assert out is None                          # harvest behavior identical to legacy
    assert len(calls) == 1                      # no enforcement yet
    assert stage.failure_empties_detected == 1  # RED pre-fix: attribute missing
    assert any(PROVIDER in rec.getMessage() for rec in caplog.records)


# ---------------------------------------------------------------------------
# failure-handling-S12
# ---------------------------------------------------------------------------
def test_s12_strict_propagates_typed_failure(monkeypatch):
    _boom(monkeypatch)
    stage = _search_stage("strict")

    with pytest.raises(TransientFetchError):
        stage.process_task(_search_task())


# ---------------------------------------------------------------------------
# failure-handling-S4 + S6 (end-to-end through the worker loop)
# ---------------------------------------------------------------------------
def test_s4_s6_strict_requeues_within_bound_then_drops_loudly(monkeypatch, caplog):
    calls = []
    _boom(monkeypatch, calls)
    stage = _search_stage("strict", max_retries=2)
    monkeypatch.setattr(stage.retry_policy, "get_delay", lambda attempts: 0.0)
    task = _search_task()

    stage.start()
    try:
        assert stage.put_task(task) is True
        drained = _wait_drain(stage)
    finally:
        stage.stop(timeout=2.0)

    assert drained, "worker loop did not settle within the deadline"
    assert len(calls) == 3                  # initial attempt + 2 bounded retries
    assert task.attempts == 3               # grew past the bound -> final requeue rejected
    assert stage.total_errors == 3          # failures accounted as errors, never successes
    assert stage.tasks_dropped_max_retries == 1  # RED pre-fix: attribute missing
    assert any(
        "discarded" in rec.getMessage().lower() or "max retries" in rec.getMessage().lower()
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# failure-handling-S5 (regression guard: dedup gate already admits retries)
# ---------------------------------------------------------------------------
def test_s5_dedup_gate_admits_bounded_requeue():
    stage = _search_stage("legacy", max_retries=2)

    first = _search_task()
    assert stage.put_task(first) is True      # first enqueue marks id processed

    duplicate = _search_task()
    assert stage.put_task(duplicate) is False  # attempts==0 duplicate rejected

    first.attempts = 1
    assert stage.put_task(first) is True       # bounded retry admitted despite processed mark

    first.attempts = 3
    assert stage.put_task(first) is False      # beyond bound rejected


# ---------------------------------------------------------------------------
# failure-handling-S9
# ---------------------------------------------------------------------------
class _StarvingLimiter:
    def acquire(self, service):
        return False

    def wait_time(self, service):
        return 0.01

    def report_result(self, *args, **kwargs):
        pass

    def _get_bucket(self, service):
        return None


def test_s9_starved_check_requeues_instead_of_dropping(monkeypatch):
    from core.types import IProvider
    from core.models import CheckResult, ResultStorage

    class FakeProvider(IProvider):
        @property
        def name(self):
            return PROVIDER

        @property
        def conditions(self):
            return []

        @property
        def result(self):
            return ResultStorage()

        def get_patterns(self):
            return Patterns(key_pattern=KEY_PATTERN)

        def check(self, token, address="", endpoint="", model="", **kwargs):
            return CheckResult(available=False)

        def inspect(self, token, address="", endpoint="", **kwargs):
            return []

    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    stage = CheckStage(
        _resources("strict", limiter=_StarvingLimiter(), providers={PROVIDER: FakeProvider()}),
        lambda out: None,
        thread_count=1,
        max_retries=2,
    )
    task = TaskFactory.create_check_task(
        PROVIDER, Service(address="https://api.x", endpoint="/v1", key="k-1", model="m")
    )

    # Pre-fix: returns None (silent drop). Contract: typed failure escapes so the
    # worker loop requeues it within bounds.
    with pytest.raises(TransientFetchError):
        stage.process_task(task)


# ---------------------------------------------------------------------------
# failure-handling-S13
# ---------------------------------------------------------------------------
def test_s13_flag_flip_rolls_back_without_code_change(monkeypatch):
    _boom(monkeypatch)

    strict = _search_stage("strict")
    with pytest.raises(TransientFetchError):
        strict.process_task(_search_task())

    legacy = _search_stage("legacy")  # same code, flipped configuration
    assert legacy.process_task(_search_task()) is None


# ---------------------------------------------------------------------------
# failure-handling-S14
# ---------------------------------------------------------------------------
def test_s14_counters_exposed_in_stage_stats(monkeypatch):
    _boom(monkeypatch)
    stage = _search_stage("shadow")
    stage.process_task(_search_task())

    stats = stage.get_stats()
    assert stats.failure_empties_detected == 1
    assert hasattr(stats, "tasks_requeued")
    assert hasattr(stats, "tasks_dropped_max_retries")


# ---------------------------------------------------------------------------
# Cooldown channel must not be double-handled as failure-empty (supports S15)
# ---------------------------------------------------------------------------
def test_credential_rotation_is_not_a_failure_empty(monkeypatch):
    calls = {"n": 0}

    def rotating(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise GithubCredentialLimited(service="github_web", credential="s1", wait=0.0)
        return (["https://github.com/o/r/blob/main/a.py"], 1, "")

    monkeypatch.setattr(search_client, "search_with_count", rotating)
    stage = _search_stage("shadow")

    out = stage.process_task(_search_task())

    assert out is not None
    assert calls["n"] == 2                                   # rotation happened as before
    assert getattr(stage, "failure_empties_detected", 0) == 0  # cooldown is its own channel


# ---------------------------------------------------------------------------
# failure-handling-S19 (fix-gather-transport: DEFER does not consume the budget)
# ---------------------------------------------------------------------------
def test_s19_deferral_does_not_consume_the_retry_budget(monkeypatch):
    """WHEN a started strict-mode stage defers a task on a published rate limit
    and the same task later succeeds within the run THEN attempts is unchanged,
    no drop/requeue/error counter moves, and only the success is accounted.

    RED at plan time: cfg.gather is absent, RateLimitDeferral is absent, the
    worker loop has no deferral branch and tasks_deferred does not exist
    (design D4; spec failure-handling "Bounded retry and honest accounting").
    """
    import json  # noqa: F401  (kept local: only the new scenarios need it)

    from core.exceptions import RateLimitDeferral
    from core.models import AcquisitionTask
    from stage.definition import AcquisitionStage

    cfg_holder = {}

    def flaky_fetch(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            raise RateLimitDeferral("rate limit exceeded", wait_s=0.05)
        return "token=sk-abcdefgh01234567"

    calls = []
    monkeypatch.setattr(search_client, "fetch_gather_content", flaky_fetch)

    resources = _resources("strict")
    resources.config.gather.transport = "raw"  # RED driver: no gather section yet
    cfg_holder["cfg"] = resources.config
    stage = AcquisitionStage(resources, lambda out: None, thread_count=1, max_retries=3)
    task = TaskFactory.create_acquisition_task(
        PROVIDER, "https://github.com/acme/widgets/blob/main/leak.py",
        Patterns(key_pattern=KEY_PATTERN),
    )

    stage.start()
    try:
        assert stage.put_task(task) is True
        drained = _wait_drain(stage, deadline=10.0)
    finally:
        stage.stop(timeout=2.0)

    assert drained, "worker loop did not settle within the deadline"
    assert len(calls) == 2                       # deferred once, then succeeded
    assert task.attempts == 0                    # deferral never touches attempts
    assert stage.tasks_dropped_max_retries == 0  # not a drop...
    assert stage.tasks_requeued == 0             # ...not a requeue...
    assert stage.total_errors == 0               # ...not an error
    assert stage.tasks_deferred == 1             # counted on its own ledger
    assert stage.total_processed == 1            # only the successful completion


# ---------------------------------------------------------------------------
# failure-handling-S20 (fix-gather-transport: deferral circulates bounded)
# ---------------------------------------------------------------------------
def test_s20_deferral_cannot_circulate_forever(tmp_path, caplog):
    """GIVEN a real SqliteTaskQueue with an age gate WHEN a task is deferred
    repeatedly and its ORIGINAL created_at passes the gate THEN the existing
    startup maintenance purges it loudly - the ceiling is max_age_hours, not
    the retry budget (design D4; mirrors tests/test_tq_sqlite_queue.py S13).

    RED at plan time: stage.defer_task does not exist.
    """
    import json

    from core.models import AcquisitionTask  # noqa: F401
    from stage.definition import AcquisitionStage
    from storage.task_queue import SqliteTaskQueue

    stage = AcquisitionStage(
        _resources("strict"),
        lambda out: None,
        thread_count=1,
        max_retries=3,
        queue_backend="sqlite",
        queue_dir=str(tmp_path / "qs"),
        queue_max_age_hours=24.0,
    )
    try:
        task = TaskFactory.create_acquisition_task(
            PROVIDER, "https://github.com/acme/widgets/blob/main/leak.py",
            Patterns(key_pattern=KEY_PATTERN),
        )
        task.created_at = time.time() - 25 * 3600  # born aged-out; deferral keeps this
        assert stage.put_task(task) is True

        got = stage.queue.get(timeout=2.0)
        assert stage.defer_task(got) is True       # repeated deferral, attempts untouched
        stage.queue.task_done()
        assert stage.queue.qsize() == 1            # still circulating as pending
        db_path = str(tmp_path / "qs" / "gather_queue.sqlite")
    finally:
        stage.queue.close()

    with caplog.at_level(logging.WARNING):
        q2 = SqliteTaskQueue(db_path, serializer=json.dumps, deserializer=json.loads,
                             name="acquisition", max_age_hours=24.0)
    try:
        assert q2.qsize() == 0                     # purged by the age gate, not claimable
        assert any("purged" in rec.getMessage().lower() for rec in caplog.records), (
            "the age-gate purge that bounds deferral must be loud"
        )
    finally:
        q2.close()
