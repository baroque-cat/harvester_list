"""Stage-level gather-transport integration (gather-transport S8, S18, S21).

RED at plan time: ``AcquisitionStage`` has no ``gather_transport`` wiring and
``search.client.fetch_gather_content`` / ``get_gather_transport_stats`` do not
exist, so the imports and ctor kwargs below fail - that IS the expected RED.

Harness mirrors tests/test_tq_stage_backend.py (FakeAuth / _task_config /
_resources / real stage over a real durable SqliteTaskQueue) per house style:
real objects, network stubbed at the client seam only.
"""

import json
import threading
import time

import pytest

from config.schemas import Config, StageConfig, TaskConfig
from core.models import AcquisitionTask, Patterns
from search import client as sc
from stage.base import StageResources
from stage.definition import AcquisitionStage
from storage.task_queue import SqliteTaskQueue

PROVIDER = "deepseek"
KEY_PATTERN = r"sk-[A-Za-z0-9]{16}"
SHA = "fbc1aee59cba49edd8964baab8515ef3009453ab"
BLOB = f"https://github.com/danieltamas/cloak/blob/{SHA}/llms.txt"


class FakeAuth:
    def get_session(self):
        return "session-token"

    def get_token(self):
        return "api-token"

    def get_user_agent(self):
        return "test-agent"


def _resources(cfg=None):
    cfg = cfg or Config()
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
    )


def _stage(tmp_path, transport, *, visibility_timeout_s=300.0, max_age_hours=24.0):
    """Real AcquisitionStage on the durable backend with the requested transport."""
    cfg = Config()
    cfg.gather.transport = transport
    return AcquisitionStage(
        _resources(cfg),
        lambda out: None,
        thread_count=1,
        max_retries=3,
        queue_size=1000,
        queue_backend="sqlite",
        queue_dir=str(tmp_path / "queue_state"),
        queue_visibility_timeout_s=visibility_timeout_s,
        queue_max_age_hours=max_age_hours,
    )


def _task(url=BLOB):
    return AcquisitionTask(provider=PROVIDER, url=url, key_pattern=KEY_PATTERN, retries=3)


# ---------------------------------------------------------------------------
# gather-transport-S8
# ---------------------------------------------------------------------------
def test_s8_persisted_tasks_survive_a_transport_switch(tmp_path, monkeypatch):
    """The wire format is transport-independent: switching must not rewrite rows."""
    qdir = tmp_path / "queue_state"

    # --- phase 1: enqueue under html, then abandon the instance (restart) ---
    stage_html = _stage(tmp_path, "html")
    urls = [BLOB, BLOB.replace("llms.txt", "other.md")]
    for u in urls:
        assert stage_html.put_task(_task(u)) is True
    stored_before = [
        json.loads(row[0])["data"]["url"]
        for row in stage_html.queue._conn.execute("SELECT payload_json FROM tasks ORDER BY seq")
    ]
    db_path = str(qdir / "gather_queue.sqlite")
    stage_html.queue.close()

    # every persisted payload still carries the ORIGINAL discovered link
    assert stored_before == urls
    assert all(u.startswith("https://github.com/") for u in stored_before)

    # --- phase 2: restart with transport=raw over the same file ---
    fetched = []

    def fake_fetch(url, **kwargs):
        fetched.append((url, kwargs.get("transport")))
        return "APP_KEY=sk-abcdefgh01234567\n"

    monkeypatch.setattr(sc, "fetch_gather_content", fake_fetch)

    stage_raw = _stage(tmp_path, "raw")
    try:
        counts = stage_raw.queue.counts()
        assert counts["pending"] == 2  # recovered, nothing lost or rewritten

        stored_after = [
            json.loads(row[0])["data"]["url"]
            for row in stage_raw.queue._conn.execute("SELECT payload_json FROM tasks ORDER BY seq")
        ]
        assert stored_after == urls  # byte-for-byte unchanged by the switch

        # processing under the new transport addresses the plain-content host
        task = stage_raw.queue.get(timeout=2.0)
        assert task.url == urls[0]
        target = sc.gather_target_url(task.url, transport="raw")
        assert target.startswith("https://raw.githubusercontent.com/")
        assert SHA in target
        stage_raw.queue.task_done()
    finally:
        stage_raw.queue.close()

    # and the sqlite file itself was never migrated
    assert SqliteTaskQueue(db_path, serializer=json.dumps, deserializer=json.loads).counts()["pending"] == 1


# ---------------------------------------------------------------------------
# gather-transport-S18
# ---------------------------------------------------------------------------
def test_s18_a_deferred_task_is_never_executed_twice(tmp_path, monkeypatch):
    """WAIT_CAP < visibility_timeout_s keeps a sleeping claim from being re-delivered."""
    from core.exceptions import RateLimitDeferral

    executions = []
    lock = threading.Lock()

    def slow_then_ok(url, **kwargs):
        with lock:
            executions.append(url)
        n = sum(1 for u in executions if u == url)
        if url == victim and n <= 2:  # only the victim is repeatedly refused
            raise RateLimitDeferral("rate limit", wait_s=0.4)
        return "APP_KEY=sk-abcdefgh01234567\n"

    monkeypatch.setattr(sc, "fetch_gather_content", slow_then_ok)

    stage = _stage(tmp_path, "raw", visibility_timeout_s=30.0)  # >> any bounded wait
    try:
        victim = BLOB
        others = [BLOB.replace("llms.txt", f"f{i}.md") for i in range(6)]
        assert stage.put_task(_task(victim)) is True
        for u in others:
            assert stage.put_task(_task(u)) is True

        claimed_concurrently = []

        def consumer():
            while True:
                try:
                    t = stage.queue.get(timeout=0.2)
                except Exception:
                    return
                snap = stage.queue._conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE state='claimed'"
                ).fetchone()[0]
                claimed_concurrently.append(snap)
                try:
                    sc.fetch_gather_content(t.url, transport="raw", max_refusal_wait_s=0.5)
                except RateLimitDeferral as d:
                    time.sleep(min(d.wait_s, 0.5))  # bounded sleep INSIDE the claim
                    # DEFER: dedicated seam - put_task's dedup gate rejects an
                    # attempts==0 re-enqueue, and a deferral must not touch attempts.
                    assert stage.defer_task(t) is True
                except Exception:
                    pass
                stage.queue.task_done()

        threads = [threading.Thread(target=consumer, daemon=True) for _ in range(2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=20)

        # no claim expired while its worker slept => the sweep reclaims nothing
        assert stage.queue.sweep_expired_claims() == 0
        # the victim ran more than once (deferred) but was never executed concurrently
        assert sum(1 for u in executions if u == victim) >= 2
        assert max(claimed_concurrently) <= 2  # one claim per worker, never a shared seq
        # every other task executed exactly once
        for u in others:
            assert sum(1 for x in executions if x == u) == 1
    finally:
        stage.queue.close()


# ---------------------------------------------------------------------------
# gather-transport-S21
# ---------------------------------------------------------------------------
def test_s21_transport_economics_are_observable(monkeypatch):
    """Flat Dict[str,int] surface, mirroring the existing get_date_parse_stats()."""
    from core.exceptions import RateLimitDeferral
    from tests.test_gt_transport import BLOB as _B, _FakeResponse, _FakeSession

    sess = _FakeSession(
        [
            _FakeResponse(200, "x" * 100),  # successful raw fetch
            _FakeResponse(429, '{"message":"rate limit"}', {"Retry-After": "3"}),  # deferral
            _FakeResponse(200, "y" * 5000),  # truncation candidate
        ]
    )
    monkeypatch.setattr(sc, "_HTTP_SESSION", sess)
    monkeypatch.setattr(sc, "_github_client", None, raising=False)
    sc.reset_gather_transport_stats()

    sc.fetch_gather_content(_B, transport="raw", max_payload_bytes=1 << 20, max_refusal_wait_s=10)
    with pytest.raises(RateLimitDeferral):
        sc.fetch_gather_content(_B, transport="raw", max_payload_bytes=1 << 20, max_refusal_wait_s=10)
    sc.fetch_gather_content(_B, transport="raw", max_payload_bytes=64, max_refusal_wait_s=10)
    sc.derive_raw_url("https://github.com/o/r/blob/main/f.txt")  # head fallback
    sc.derive_raw_url("not a url")  # unparseable

    stats = sc.get_gather_transport_stats()
    assert isinstance(stats, dict)
    # EXACT key set, not a subset: S21 publishes a stable per-transport surface,
    # so a missing OR an extra/undeclared counter is a contract break.  A subset
    # check would silently tolerate schema drift mid-run.
    expected_keys = {
        "requests_raw",
        "bytes_raw",
        "requests_html",
        "bytes_html",
        "requests_rest",
        "bytes_rest",
        "deferred_rate_limit",
        "deferred_secondary",
        "dropped_auth",
        "dropped_not_found",
        "head_fallback",
        "unparseable_link",
        "truncated",
    }
    assert set(stats) == expected_keys, f"observability key set drifted: {set(stats) ^ expected_keys}"
    for key in expected_keys:
        assert isinstance(stats[key], int), f"{key} must be an integer count"

    # Byte counters exist for every transport even in a raw-only run (zeros,
    # never absent) so consumers see one fixed schema from the first sample.
    assert stats["bytes_html"] == 0 and stats["bytes_rest"] == 0
    assert stats["requests_html"] == 0 and stats["requests_rest"] == 0

    assert stats["requests_raw"] == 3
    assert stats["bytes_raw"] >= 100
    assert stats["deferred_rate_limit"] == 1
    assert stats["truncated"] == 1
    assert stats["head_fallback"] == 1
    assert stats["unparseable_link"] == 1
    # refusals are split by classification, not collapsed into one bucket
    assert stats["dropped_auth"] == 0 and stats["dropped_not_found"] == 0

    # Reset must restore the SAME fixed surface (zeroed), never a narrower or
    # wider one — otherwise the published schema depends on run history.
    sc.reset_gather_transport_stats()
    after_reset = sc.get_gather_transport_stats()
    assert set(after_reset) == expected_keys
    assert all(value == 0 for value in after_reset.values())

    # An undeclared counter can never widen the surface silently.
    sc._gather_stat_inc("bytes_ftp", 5)
    assert set(sc.get_gather_transport_stats()) == expected_keys
    sc.reset_gather_transport_stats()


# ---------------------------------------------------------------------------
# tasks.md 4.1 (verification W2): the deferral WARNING must name the BOUNDED wait
# ---------------------------------------------------------------------------
def test_defer_warning_names_the_bounded_wait(tmp_path, caplog):
    """GIVEN a real stage whose refusal cap is 10s WHEN a task is deferred with a
    published wait far above that cap THEN exactly one WARNING is emitted and it
    names stage, provider, task AND the *effective* (clamped) wait - the one
    number an operator needs during a soak, which is otherwise invisible.

    RED before the fix: the message named stage/provider/task only, so the D5
    clamp could not be observed in production logs.
    """
    import logging

    stage = _stage(tmp_path, "raw")
    try:
        stage.resources.config.gather.max_refusal_wait_s = 10.0

        # The clamp itself (design D5): a published 999s wait becomes 10s.
        assert stage._effective_defer_wait(999.0) == 10.0

        task = _task()
        wait = stage._effective_defer_wait(999.0)
        with caplog.at_level(logging.WARNING):
            assert stage.defer_task(task, wait_s=wait) is True

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, f"expected exactly one WARNING per episode, got {len(warnings)}"
        msg = warnings[0].getMessage()
        assert stage.name in msg                    # stage
        assert PROVIDER in msg                      # provider
        assert "bounded wait: 10.0s" in msg         # the EFFECTIVE wait...
        assert "cap 10.0s" in msg                   # ...and the ceiling it hit
        assert "999" not in msg                     # never the unclamped publish
        assert str(task) in msg                     # task identity
        assert stage.tasks_deferred == 1

        # A caller that only re-queues stays valid (wait_s is optional).
        caplog.clear()
        assert stage.defer_task(_task()) is True
        assert "bounded wait: n/a" in caplog.records[-1].getMessage()
        assert stage.tasks_deferred == 2
    finally:
        stage.queue.close()
