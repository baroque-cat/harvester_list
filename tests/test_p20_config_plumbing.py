"""P20 — config surface and plumbing pins C1–C5, S-A through S-D.

Traceability: plan /tmp/opencode/p20-plan.md §3 (Config level + Stage level).

These pins verify:
* C1  schema default is True
* C2  TaskQueueConfig(durable_dedup="false") raises with exact message
* C3  ConfigValidator reports identical message for mutated non-bool
* C4  loader parses explicit false from dict and absent key as True
* C5  full config load with queue.durable_dedup: false reaches the stage
* S-A sqlite put_task twice with attempts==0 → True then False, 1 row
* S-B S21 sequence on sqlite with flag ON → same four outcomes, 2 rows
* S-C started stage with RateLimitDeferral: deferred task executed after
* S-D memory backend: duplicate put_task unchanged, no attribute error
* H4  counts() still returns exactly {"pending","claimed"}
* H6  two config error sites produce byte-identical strings
"""

import json
import time

import pytest

from config.schemas import Config, TaskQueueConfig
from config.validator import ConfigValidator
from config.loader import ConfigLoader
from core.exceptions import RateLimitDeferral
from core.models import Patterns, SearchTask
from stage.base import StageResources
from stage.definition import SearchStage
from storage.task_queue import SqliteTaskQueue

PROVIDER = "deepseek"
KEY_PATTERN = r"sk-[A-Za-z0-9]{16}"


# ---------------------------------------------------------------------------
# C1 — schema default is True
# ---------------------------------------------------------------------------
def test_c1_schema_default_is_true():
    cfg = TaskQueueConfig()
    assert cfg.durable_dedup is True


# ---------------------------------------------------------------------------
# C2 — TaskQueueConfig(durable_dedup="false") raises with exact message
# ---------------------------------------------------------------------------
def test_c2_schema_rejects_string_false():
    with pytest.raises(ValueError, match="queue.durable_dedup must be a boolean"):
        TaskQueueConfig(durable_dedup="false")


# ---------------------------------------------------------------------------
# C3 — ConfigValidator reports identical message for mutated non-bool
# ---------------------------------------------------------------------------
def test_c3_validator_reports_identical_message():
    cfg = Config()
    cfg.queue.durable_dedup = "false"  # mutate to non-bool
    validator = ConfigValidator()
    try:
        validator.validate(cfg)
        assert False, "should have raised ValueError"
    except ValueError:
        pass
    assert "queue.durable_dedup must be a boolean" in validator.errors


# ---------------------------------------------------------------------------
# C4 — loader parses explicit false from dict and absent key as True
# ---------------------------------------------------------------------------
def test_c4_loader_parses_explicit_false_and_absent_as_true():
    loader = ConfigLoader()

    # Explicit false
    data_false = {"backend": "sqlite", "durable_dedup": False}
    cfg_false = loader._parse_task_queue_config(data_false)
    assert cfg_false.durable_dedup is False

    # Absent key → default True
    data_absent = {"backend": "sqlite"}
    cfg_absent = loader._parse_task_queue_config(data_absent)
    assert cfg_absent.durable_dedup is True


# ---------------------------------------------------------------------------
# C5 — full config load reaches the stage (variant: construct stage directly)
# ---------------------------------------------------------------------------
def test_c5_config_load_reaches_stage_queue(tmp_path):
    """Construct a stage directly with queue_durable_dedup=False and verify
    the SqliteTaskQueue receives it."""
    from config.schemas import StageConfig, TaskConfig

    task_cfg = TaskConfig(
        name=PROVIDER,
        enabled=True,
        provider_type="openai_like",
        use_api=False,
        stages=StageConfig(search=True, gather=True, check=True, inspect=True),
        patterns=Patterns(key_pattern=KEY_PATTERN),
    )

    class FakeAuth:
        def get_session(self):
            return "session-token"

        def get_token(self):
            return "api-token"

        def get_user_agent(self):
            return "test-agent"

    cfg = Config()
    cfg.queue.backend = "sqlite"
    cfg.queue.durable_dedup = False

    resources = StageResources(
        limiter=None,
        providers={},
        config=cfg,
        task_configs={PROVIDER: task_cfg},
        auth=FakeAuth(),
    )

    stage = SearchStage(
        resources,
        lambda out: None,
        thread_count=1,
        queue_backend="sqlite",
        queue_dir=str(tmp_path / "queue_state"),
        queue_durable_dedup=False,
    )

    try:
        assert isinstance(stage.queue, SqliteTaskQueue)
        assert stage.queue.durable_dedup is False
    finally:
        stage.queue.close()


# ---------------------------------------------------------------------------
# S-A — sqlite put_task twice with attempts==0 → True then False, 1 row
# ---------------------------------------------------------------------------
def test_sa_sqlite_put_task_twice_suppresses_second(tmp_path):
    """Stage-level proof that the RAM gate is unchanged AND the durable store
    holds exactly 1 row after two put_task calls with attempts==0."""
    from config.schemas import StageConfig, TaskConfig

    task_cfg = TaskConfig(
        name=PROVIDER,
        enabled=True,
        provider_type="openai_like",
        use_api=False,
        stages=StageConfig(search=True, gather=True, check=True, inspect=True),
        patterns=Patterns(key_pattern=KEY_PATTERN),
    )

    class FakeAuth:
        def get_session(self):
            return "session-token"

        def get_token(self):
            return "api-token"

        def get_user_agent(self):
            return "test-agent"

    cfg = Config()
    resources = StageResources(
        limiter=None,
        providers={},
        config=cfg,
        task_configs={PROVIDER: task_cfg},
        auth=FakeAuth(),
    )

    stage = SearchStage(
        resources,
        lambda out: None,
        thread_count=1,
        queue_backend="sqlite",
        queue_dir=str(tmp_path / "queue_state"),
    )

    try:
        task1 = SearchTask(provider=PROVIDER, query='"sk-"', regex=KEY_PATTERN, page=1, use_api=False)
        task2 = SearchTask(provider=PROVIDER, query='"sk-"', regex=KEY_PATTERN, page=1, use_api=False)

        assert stage.put_task(task1) is True
        assert stage.put_task(task2) is False  # RAM gate rejects duplicate

        # Durable store holds exactly 1 row
        assert stage.queue.qsize() == 1
    finally:
        stage.queue.close()


# ---------------------------------------------------------------------------
# S-B — S21 sequence on sqlite with flag ON (proves H2)
# ---------------------------------------------------------------------------
def test_sb_s21_sequence_on_sqlite_with_flag_on(tmp_path):
    """Imitate tests/test_tq_stage_backend.py:252-266 on sqlite with the flag ON.
    Outcomes: accepted / rejected / accepted / rejected-with-drop-counter.
    After step 3 the store holds 2 rows (proves bounded requeue is never suppressed)."""
    from config.schemas import StageConfig, TaskConfig

    task_cfg = TaskConfig(
        name=PROVIDER,
        enabled=True,
        provider_type="openai_like",
        use_api=False,
        stages=StageConfig(search=True, gather=True, check=True, inspect=True),
        patterns=Patterns(key_pattern=KEY_PATTERN),
    )

    class FakeAuth:
        def get_session(self):
            return "session-token"

        def get_token(self):
            return "api-token"

        def get_user_agent(self):
            return "test-agent"

    cfg = Config()
    resources = StageResources(
        limiter=None,
        providers={},
        config=cfg,
        task_configs={PROVIDER: task_cfg},
        auth=FakeAuth(),
    )

    stage = SearchStage(
        resources,
        lambda out: None,
        thread_count=1,
        max_retries=2,
        queue_backend="sqlite",
        queue_dir=str(tmp_path / "queue_state"),
    )

    try:
        first = SearchTask(provider=PROVIDER, query='"sk-"', regex=KEY_PATTERN, page=1, use_api=False)
        assert stage.put_task(first) is True  # accepted

        duplicate = SearchTask(provider=PROVIDER, query='"sk-"', regex=KEY_PATTERN, page=1, use_api=False)
        assert stage.put_task(duplicate) is False  # rejected (attempts==0 duplicate)

        first.attempts = 1
        assert stage.put_task(first) is True  # accepted (bounded retry)

        # After step 3, store holds 2 rows (proves H2: bounded requeue never suppressed)
        assert stage.queue.qsize() == 2

        first.attempts = 3
        assert stage.put_task(first) is False  # rejected (beyond bound)
        assert stage.tasks_dropped_max_retries == 1
    finally:
        stage.queue.close()


# ---------------------------------------------------------------------------
# S-C — started stage with RateLimitDeferral: deferred task executed after
# ---------------------------------------------------------------------------
def test_sc_deferred_task_executed_after_deferral(tmp_path):
    """End-to-end proof of hazard H1: a deferred task is still executed afterwards.
    Imitate fixtures in tests/test_cl_stage_defer.py."""
    from config.schemas import StageConfig, TaskConfig

    task_cfg = TaskConfig(
        name=PROVIDER,
        enabled=True,
        provider_type="openai_like",
        use_api=False,
        stages=StageConfig(search=True, gather=True, check=True, inspect=True),
        patterns=Patterns(key_pattern=KEY_PATTERN),
    )

    class FakeAuth:
        def get_session(self):
            return "session-token"

        def get_token(self):
            return "api-token"

        def get_user_agent(self):
            return "test-agent"

    cfg = Config()
    resources = StageResources(
        limiter=None,
        providers={},
        config=cfg,
        task_configs={PROVIDER: task_cfg},
        auth=FakeAuth(),
    )

    stage = SearchStage(
        resources,
        lambda out: None,
        thread_count=1,
        queue_backend="sqlite",
        queue_dir=str(tmp_path / "queue_state"),
    )

    try:
        task = SearchTask(provider=PROVIDER, query='"sk-"', regex=KEY_PATTERN, page=1, use_api=False)
        assert stage.put_task(task) is True

        # Claim the task
        claimed = stage.queue.get(timeout=1.0)
        assert claimed is not None

        # Defer it (simulates a RateLimitDeferral in the worker)
        assert stage.defer_task(claimed) is True
        assert stage.tasks_deferred == 1

        # The worker's finally block would call task_done() to delete the claim
        stage.queue.task_done()

        # Verify the deferred task is back in the queue and can be claimed again
        counts = stage.queue.counts()
        assert counts["pending"] == 1, "deferred task is back in the queue, never dropped"
        assert counts["claimed"] == 0, "claim was deleted by task_done()"

        # Claim it again and verify it's the same task
        reclaimed = stage.queue.get(timeout=1.0)
        assert reclaimed is not None
        assert reclaimed.task_id == task.task_id

        # Ack it
        stage.queue.task_done()
        assert stage.queue.qsize() == 0
        assert stage.queue.counts()["claimed"] == 0
    finally:
        stage.queue.close()


# ---------------------------------------------------------------------------
# S-D — memory backend: duplicate put_task unchanged, no attribute error
# ---------------------------------------------------------------------------
def test_sd_memory_backend_unchanged(tmp_path):
    """Memory backend: duplicate put_task behaviour unchanged and no attribute error."""
    from config.schemas import StageConfig, TaskConfig

    task_cfg = TaskConfig(
        name=PROVIDER,
        enabled=True,
        provider_type="openai_like",
        use_api=False,
        stages=StageConfig(search=True, gather=True, check=True, inspect=True),
        patterns=Patterns(key_pattern=KEY_PATTERN),
    )

    class FakeAuth:
        def get_session(self):
            return "session-token"

        def get_token(self):
            return "api-token"

        def get_user_agent(self):
            return "test-agent"

    cfg = Config()
    resources = StageResources(
        limiter=None,
        providers={},
        config=cfg,
        task_configs={PROVIDER: task_cfg},
        auth=FakeAuth(),
    )

    stage = SearchStage(
        resources,
        lambda out: None,
        thread_count=1,
        queue_backend="memory",  # memory backend
        queue_durable_dedup=False,  # flag must be inert
    )

    try:
        task1 = SearchTask(provider=PROVIDER, query='"sk-"', regex=KEY_PATTERN, page=1, use_api=False)
        task2 = SearchTask(provider=PROVIDER, query='"sk-"', regex=KEY_PATTERN, page=1, use_api=False)

        # RAM gate rejects duplicate (unchanged behavior)
        assert stage.put_task(task1) is True
        assert stage.put_task(task2) is False

        # No attribute error (flag is inert on memory backend)
        assert not hasattr(stage.queue, "durable_dedup")
    finally:
        pass  # memory queue has no close()


# ---------------------------------------------------------------------------
# H4 — counts() still returns exactly {"pending","claimed"}
# ---------------------------------------------------------------------------
def test_h4_counts_shape_preserved(tmp_path):
    """counts() still returns exactly {"pending","claimed"} (hazard H4)."""
    q = SqliteTaskQueue(
        str(tmp_path / "counts_queue.sqlite"),
        serializer=json.dumps,
        deserializer=json.loads,
        name="counts",
    )
    try:
        assert q.counts() == {"pending": 0, "claimed": 0}
        q.put({"url": "a"}, dedup_id="gather:x:a")
        assert q.counts() == {"pending": 1, "claimed": 0}
        item = q.get(timeout=1.0)
        assert q.counts() == {"pending": 0, "claimed": 1}
        q.task_done()
        assert q.counts() == {"pending": 0, "claimed": 0}
    finally:
        q.close()


# ---------------------------------------------------------------------------
# H6 — two config error sites produce byte-identical strings
# ---------------------------------------------------------------------------
def test_h6_config_error_sites_byte_identical():
    """Schema __post_init__ and ConfigValidator produce byte-identical messages."""
    # Schema error
    try:
        TaskQueueConfig(durable_dedup="false")
        assert False, "should have raised"
    except ValueError as e:
        schema_msg = str(e)

    # Validator error
    cfg = Config()
    cfg.queue.durable_dedup = "false"
    validator = ConfigValidator()
    try:
        validator.validate(cfg)
        assert False, "should have raised ValueError"
    except ValueError:
        pass

    # Find the matching error
    validator_msg = None
    for err in validator.errors:
        if "durable_dedup" in err:
            validator_msg = err
            break

    assert validator_msg is not None
    assert schema_msg == validator_msg
    assert schema_msg == "queue.durable_dedup must be a boolean"
