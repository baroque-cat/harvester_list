"""Stage-level deferral on credential exhaustion (credential-liveness S7, S8,
S10, S15-integration, S23).

RED at plan time: ``CredentialsExhausted`` / ``credential_liveness_metrics``
imports fail pre-fix; the search rotation loops (stage/definition.py:245-315)
have no exhaustion branch (S7/S8 fail with an unhandled exception type);
``PipelineStatus.credential_metrics`` does not exist (S23 AttributeError).

Harness: the SearchStage/durable-sqlite patterns of tests/test_fh_stage_modes.py
and tests/test_gt_stage_integration.py. No network: auth is fake and the search
entry point is sentinel-patched.
"""

import json
import time

import pytest

from config.schemas import Config, StageConfig, TaskConfig
from core.exceptions import RateLimitDeferral, TransientFetchError
from core.metrics import PipelineStatus
from core.models import Patterns, SearchTask
from search import client as search_client
from search.client import GitHubClient
from stage.base import StageResources
from stage.definition import SearchStage
from tools import credential as credential_module
from tools.credential import Credentials
from tools.state import (  # RED driver: absent pre-fix
    CredentialsExhausted,
    GithubCredentialLimited,
    credential_liveness_metrics,
    github_credential_state,
)

PROVIDER = "deepseek"
KEY_PATTERN = r"sk-[A-Za-z0-9]{16}"


class _Settings:
    def __init__(self, wait_mode="bounded", max_wait_s=60.0, early_release=True, emergency_threshold=3):
        self.wait_mode = wait_mode
        self.max_wait_s = max_wait_s
        self.early_release = early_release
        self.emergency_threshold = emergency_threshold


class ExhaustedAuth:
    """Auth whose every credential request ends in typed exhaustion."""

    def __init__(self, service="github_api", wait=900.0):
        self._exc = CredentialsExhausted(service=service, reason="budget_spent", wait_estimate_s=wait)

    def get_token(self):
        raise self._exc

    def get_session(self):
        raise self._exc

    def get_user_agent(self):
        return "test-agent"


def _task_config():
    return TaskConfig(
        name=PROVIDER,
        enabled=True,
        provider_type="openai_like",
        use_api=True,
        stages=StageConfig(search=True, gather=True, check=True, inspect=True),
        patterns=Patterns(key_pattern=KEY_PATTERN),
    )


def _stage(auth, tmp_path=None, **queue_kwargs):
    cfg = Config()
    cfg.pipeline.failure_handling = "strict"
    resources = StageResources(
        limiter=None,
        providers={},
        config=cfg,
        task_configs={PROVIDER: _task_config()},
        auth=auth,
        registry=None,
    )
    kwargs = dict(queue_kwargs)
    if tmp_path is not None:
        kwargs.update(
            queue_backend="sqlite",
            queue_dir=str(tmp_path / "queue_state"),
            queue_visibility_timeout_s=300,
            queue_max_age_hours=24,
        )
    return SearchStage(resources, lambda out: None, thread_count=1, max_retries=0, **kwargs)


def _search_task(page=1):
    return SearchTask(provider=PROVIDER, query='"sk-"', regex=KEY_PATTERN, page=page, use_api=True)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _boom(**kwargs):
        pytest.fail("no search HTTP request may be sent when credentials are exhausted")

    monkeypatch.setattr(search_client, "search_with_count", _boom)
    monkeypatch.setattr(search_client, "search_code", _boom)
    yield


@pytest.fixture(autouse=True)
def _clean_state():
    for key in list(github_credential_state._items):
        github_credential_state._items.pop(key, None)
    yield
    for key in list(github_credential_state._items):
        github_credential_state._items.pop(key, None)


# ---------------------------------------------------------------------------
# credential-liveness-S7
# ---------------------------------------------------------------------------
def test_s7_search_task_defers_on_credential_exhaustion(tmp_path):
    """WHEN the rotation ends in typed exhaustion THEN process_task raises the
    R5.1 deferral signal (worker loop DEFER branch re-queues it), and
    defer_task accounting works on the durable backend: tasks_deferred
    increments, the row returns to pending with attempts untouched."""
    stage = _stage(ExhaustedAuth(), tmp_path=tmp_path)
    task = _search_task()

    with pytest.raises(RateLimitDeferral) as exc:
        stage.process_task(task)
    assert "exhaust" in str(exc.value).lower()
    assert not isinstance(exc.value, TransientFetchError), \
        "a deferral is not a task fault and must never enter the requeue ladder"

    assert stage.defer_task(task) is True
    assert stage.tasks_deferred == 1
    counts = stage.queue.counts()
    assert counts["pending"] == 1, "deferred task is back in the queue, never dropped"
    row = stage.queue._conn.execute(
        "SELECT attempts FROM tasks ORDER BY seq"
    ).fetchone()
    assert row[0] == 0, "deferral does not burn the retry budget"
    stage.queue.close()


# ---------------------------------------------------------------------------
# credential-liveness-S8
# ---------------------------------------------------------------------------
def test_s8_exhaustion_never_produces_an_empty_result():
    """WHEN credentials are exhausted THEN the task yields NO StageOutput (the
    honest-empty outcome stays reserved for genuinely empty fetch results) and
    is not counted as processed."""
    outputs = []
    cfg = Config()
    cfg.pipeline.failure_handling = "strict"
    resources = StageResources(
        limiter=None, providers={}, config=cfg,
        task_configs={PROVIDER: _task_config()},
        auth=ExhaustedAuth(), registry=None,
    )
    stage = SearchStage(resources, outputs.append, thread_count=1, max_retries=0)

    with pytest.raises(RateLimitDeferral):
        stage.process_task(_search_task())

    assert outputs == []
    assert stage.total_processed == 0


# ---------------------------------------------------------------------------
# credential-liveness-S10
# ---------------------------------------------------------------------------
def test_s10_deferral_never_extends_task_lifetime(tmp_path):
    """WHEN an aged task is deferred THEN its original created_at survives the
    re-queue - the existing max_age_hours purge remains the ultimate ceiling
    (loud accounting pinned by test_tq_sqlite_queue.py::test_s13)."""
    stage = _stage(ExhaustedAuth(), tmp_path=tmp_path)
    task = _search_task()
    assert stage.put_task(task) is True

    aged = time.time() - 25 * 3600  # older than max_age_hours=24
    stage.queue._conn.execute("UPDATE tasks SET created_at = ?", (aged,))
    stage.queue._conn.commit()

    claimed = stage.queue.get(timeout=1)
    assert claimed is not None
    assert stage.defer_task(claimed, wait_s=0) is True

    row = stage.queue._conn.execute("SELECT created_at, attempts FROM tasks").fetchone()
    assert abs(row[0] - aged) < 1.0, "defer_task must preserve the ORIGINAL created_at"
    assert row[1] == 0
    stage.queue.close()


# ---------------------------------------------------------------------------
# credential-liveness-S15 (integration: far reset -> deferral, not a long sleep)
# ---------------------------------------------------------------------------
def test_s15_reset_far_in_the_future_defers_instead_of_sleeping(monkeypatch):
    """WHEN the header-indicated reset lies beyond the wait budget THEN the
    credential is cooled for the indicated (clamped) duration but the selector
    exhausts within budget -> the stage defers; nobody sleeps the reset out
    inside a claim."""
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(credential_module, "_liveness_settings", lambda: _Settings(max_wait_s=0.2))

    client = GitHubClient(limiter=None, resource_provider=Credentials(sessions=[], tokens=["t1"], strategy="round_robin"), limits=None)
    far_reset = int(time.time()) + 3600
    with pytest.raises(GithubCredentialLimited):
        client.mark_credential_limited(
            "github_api", "t1",
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(far_reset)},
            content="", reason="",
        )
    assert github_credential_state.is_cooling("github_api", "t1")

    creds = Credentials(sessions=[], tokens=["t1"], strategy="round_robin")
    with pytest.raises(CredentialsExhausted):
        creds.get_token()
    assert sum(slept) <= 0.2 + 1e-6, "total in-task sleep stays within the credential budget"


# ---------------------------------------------------------------------------
# credential-liveness-S23
# ---------------------------------------------------------------------------
def test_s23_counters_readable_from_status(monkeypatch):
    """WHEN liveness events occur THEN the counters are readable from the
    pipeline status surface (credential_metrics block, refine_metrics
    precedent) with stable key names."""
    status = PipelineStatus()
    assert isinstance(status.credential_metrics, dict)

    snap = credential_liveness_metrics()
    for key in (
        "exhausted_episodes",
        "deferred_by_credentials",
        "secondary_limit_incidents",
        "early_releases",
        "emergency_trips",
        "blocking_mode_active",
    ):
        assert key in snap, f"credential_metrics must expose {key}"

    # one real exhaustion episode flows into the collector
    monkeypatch.setattr(credential_module, "_liveness_settings", lambda: _Settings(max_wait_s=0.0))
    monkeypatch.setattr(github_credential_state, "is_cooling", lambda service, credential: True)
    monkeypatch.setattr(github_credential_state, "next_wait", lambda service, items: 900.0)
    before = credential_liveness_metrics()["exhausted_episodes"]
    with pytest.raises(CredentialsExhausted):
        Credentials(sessions=["s1"], tokens=[], strategy="round_robin").get_session()
    after = credential_liveness_metrics()["exhausted_episodes"]
    assert after == before + 1


def test_s23b_credential_metrics_summary_renders_every_counter():
    """Design D20: the same snapshot must be renderable for operators and for the
    live-gate acceptance rows (A9-A13 read ``credential_metrics`` from a real run,
    which is otherwise never printed)."""
    from tools.state import bump_credential_metric, credential_metrics_summary

    line = credential_metrics_summary()
    for key in (
        "exhausted_episodes",
        "deferred_by_credentials",
        "secondary_limit_incidents",
        "early_releases",
        "emergency_trips",
        "blocking_mode_active",
    ):
        assert f"{key}=" in line, f"summary must render {key}: {line!r}"

    before = credential_metrics_summary()
    bump_credential_metric("early_releases")
    assert credential_metrics_summary() != before, "summary must track counter changes"
