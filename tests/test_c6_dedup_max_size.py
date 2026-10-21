"""C6/F5 — per-stage dedup_max_size plumbing through config.

Traceability: 02_critical_fixes.md §2.2 C6/F5; 07_verification.md §slice 2.2.

Defect: ``dedup_max_size`` (default 100k) is NEVER passed from config to stages.
The ctor parameter exists (``stage/base.py:167``), the clamp exists (:197),
the FIFO eviction exists (:443-451), but ``manager/pipeline.py:381-392`` only
plumbs ``thread_count`` and ``queue_size`` — not ``dedup_max_size``.  At 10⁶
tasks the gate forgets everything beyond the last 100k.

Fix: plumb per-stage through config by mirror of ``threads``/``queue_sizes``
pattern — a ``dedup_sizes: Dict[str, int]`` field on ``PipelineConfig``.

Pins:
* P1  loader parses ``pipeline.dedup_sizes`` → schema attribute
* P2  absent key ⇒ default 100_000 per stage (neutrality — byte-identical
      behavior without the key)
* P3  pipeline wiring: configured value reaches stage instance
      (``stage.dedup_max_size == configured``)
* P4  eviction behavior at a low cap: cap+X unique task_ids ⇒
      ``len(processed) <= cap``, oldest evicted FIRST (FIFO), no exceptions
* P5  clamp floor: value <1000 ⇒ 1000 (existing ``base.py:197`` mechanism)
* P6  validator rejects non-positive value with mirror message
"""

from __future__ import annotations

from config.loader import ConfigLoader
from config.schemas import Config, PipelineConfig
from config.validator import ConfigValidator
from core.models import Patterns, SearchTask
from stage.base import StageResources
from stage.definition import SearchStage

PROVIDER = "deepseek"
KEY_PATTERN = r"sk-[A-Za-z0-9]{16}"


class FakeAuth:
    def get_session(self):
        return "session-token"

    def get_token(self):
        return "api-token"

    def get_user_agent(self):
        return "test-agent"


def _task_config():
    from config.schemas import StageConfig, TaskConfig
    return TaskConfig(
        name=PROVIDER,
        enabled=True,
        provider_type="openai_like",
        use_api=False,
        stages=StageConfig(search=True, gather=True, check=True, inspect=True),
        patterns=Patterns(key_pattern=KEY_PATTERN),
    )


def _resources(cfg=None):
    if cfg is None:
        cfg = Config()
    return StageResources(
        limiter=None,
        providers={},
        config=cfg,
        task_configs={PROVIDER: _task_config()},
        auth=FakeAuth(),
    )


# ---------------------------------------------------------------------------
# P1 — loader parses pipeline.dedup_sizes → schema attribute
# ---------------------------------------------------------------------------
def test_p1_loader_parses_dedup_sizes():
    """WHEN config data contains pipeline.dedup_sizes
    THEN the loader populates PipelineConfig.dedup_sizes with the dict."""
    loader = ConfigLoader()
    data = {
        "threads": {"search": 1, "gather": 8, "check": 4, "inspect": 2},
        "queue_sizes": {"search": 100000, "gather": 200000, "check": 500000, "inspect": 1000000},
        "dedup_sizes": {"search": 50000, "gather": 500000, "check": 200000, "inspect": 100000},
    }
    cfg = loader._parse_pipeline_config(data)
    assert hasattr(cfg, "dedup_sizes")
    assert cfg.dedup_sizes == {"search": 50000, "gather": 500000, "check": 200000, "inspect": 100000}


# ---------------------------------------------------------------------------
# P2 — absent key ⇒ default 100_000 per stage (neutrality)
# ---------------------------------------------------------------------------
def test_p2_absent_dedup_sizes_defaults_to_100k():
    """WHEN pipeline.dedup_sizes is absent from config
    THEN PipelineConfig.dedup_sizes defaults to 100_000 per stage
    (byte-identical behavior to the pre-fix state)."""
    loader = ConfigLoader()
    data = {
        "threads": {"search": 1, "gather": 8, "check": 4, "inspect": 2},
        "queue_sizes": {"search": 100000, "gather": 200000, "check": 500000, "inspect": 1000000},
    }
    cfg = loader._parse_pipeline_config(data)
    assert hasattr(cfg, "dedup_sizes")
    # Every standard stage defaults to 100_000
    for stage in ("search", "gather", "check", "inspect"):
        assert cfg.dedup_sizes[stage] == 100_000


# ---------------------------------------------------------------------------
# P3 — pipeline wiring: configured value reaches stage instance
# ---------------------------------------------------------------------------
def test_p3_configured_dedup_size_reaches_stage(tmp_path):
    """WHEN a Pipeline is constructed with configured dedup_sizes
    THEN each stage.dedup_max_size equals the configured value.

    This is the ACTUAL defect: manager/pipeline.py must pass the config value
    to the stage ctor. We construct Pipeline (not SearchStage directly) to
    prove the wiring works end-to-end."""
    from config.schemas import Config, StageConfig, TaskConfig
    from manager.pipeline import Pipeline

    cfg = Config()
    cfg.global_config.workspace = str(tmp_path)
    cfg.persistence.simple = True  # avoid snapshot threads

    # Configure per-stage dedup_sizes
    cfg.pipeline.dedup_sizes = {
        "search": 250_000,
        "gather": 500_000,
        "check": 200_000,
        "inspect": 100_000,
    }

    # Add a task that requests all stages
    task_cfg = TaskConfig(
        name=PROVIDER,
        enabled=True,
        provider_type="openai_like",
        use_api=False,
        stages=StageConfig(search=True, gather=True, check=True, inspect=True),
        patterns=Patterns(key_pattern=KEY_PATTERN),
    )
    cfg.tasks = [task_cfg]

    # Construct Pipeline — this calls _create_stages() which must plumb dedup_max_size
    pipeline = Pipeline(cfg, providers={})

    try:
        # Verify each stage received the configured dedup_max_size
        assert pipeline.stages["search"].dedup_max_size == 250_000
        assert pipeline.stages["gather"].dedup_max_size == 500_000
        assert pipeline.stages["check"].dedup_max_size == 200_000
        assert pipeline.stages["inspect"].dedup_max_size == 100_000
    finally:
        # Clean up sqlite queues if created
        for stage in pipeline.stages.values():
            if hasattr(stage.queue, "close"):
                stage.queue.close()


# ---------------------------------------------------------------------------
# P4 — eviction behavior at low cap: FIFO, oldest first, no exceptions
# ---------------------------------------------------------------------------
def test_p4_eviction_at_low_cap_fifo_oldest_first(tmp_path):
    """WHEN cap+X unique task_ids are submitted with a low dedup_max_size
    THEN len(processed) <= cap, the oldest is evicted FIRST (FIFO),
    and no exceptions are raised.

    Note: we set dedup_max_size directly after construction to bypass the
    ctor clamp (max(1000, ...)) — the clamp is tested in P5."""
    cfg = Config()
    resources = _resources(cfg)

    cap = 5
    stage = SearchStage(
        resources,
        lambda out: None,
        thread_count=1,
        dedup_max_size=1000,  # pass clamp, then override below
    )
    # Override to test eviction behavior at low cap
    stage.dedup_max_size = cap

    # Submit cap+3 unique tasks
    for i in range(cap + 3):
        task = SearchTask(
            provider=PROVIDER,
            query=f'"sk-{i:04d}"',
            regex=KEY_PATTERN,
            page=1,
            use_api=False,
        )
        assert stage.put_task(task) is True

    # Bounded by cap
    assert len(stage.processed) <= cap

    # The oldest 3 were evicted (FIFO): task_ids for i=0,1,2 are gone
    # Collect actual task_ids to check eviction
    remaining = set(stage.processed)
    # Extract the numeric suffix from task_ids to determine which survived
    def _extract_idx(tid: str) -> int:
        # task_id format: search:deepseek:"sk-NNNN":1:sk-[A-Za-z0-9]{16}
        import re
        m = re.search(r'sk-(\d+)', tid)
        return int(m.group(1))

    surviving_indices = sorted(_extract_idx(tid) for tid in remaining)
    # With cap=5 and 8 submitted (0..7), the 3 oldest (0,1,2) should be evicted
    assert surviving_indices == [3, 4, 5, 6, 7], f"expected [3,4,5,6,7], got {surviving_indices}"


# ---------------------------------------------------------------------------
# P5 — clamp floor: value <1000 ⇒ 1000
# ---------------------------------------------------------------------------
def test_p5_clamp_floor_value_below_1000_becomes_1000():
    """WHEN dedup_max_size < 1000 is passed to the stage ctor
    THEN the stage clamps it to 1000 (existing base.py:197 mechanism)."""
    cfg = Config()
    resources = _resources(cfg)

    stage = SearchStage(
        resources,
        lambda out: None,
        thread_count=1,
        dedup_max_size=100,  # below floor
    )

    assert stage.dedup_max_size == 1000  # clamped to floor


# ---------------------------------------------------------------------------
# P6 — validator rejects non-positive value with mirror message
# ---------------------------------------------------------------------------
def test_p6_validator_rejects_non_positive_dedup_size():
    """WHEN pipeline.dedup_sizes contains a non-positive value for a required stage
    THEN the validator reports an error naming the field and stage,
    mirroring the queue_sizes validation pattern."""
    cfg = Config()
    cfg.pipeline.dedup_sizes = {"search": 0, "gather": 100000, "check": 100000, "inspect": 100000}

    validator = ConfigValidator()
    try:
        validator.validate(cfg)
    except ValueError:
        pass  # other errors (credentials etc.) also fire

    # Should have an error about dedup_sizes for search
    dedup_errors = [e for e in validator.errors if "dedup" in e.lower() and "search" in e.lower()]
    assert len(dedup_errors) >= 1, f"expected dedup_sizes validation error, got: {validator.errors}"
    # Message should mirror queue_sizes pattern: "Dedup size for {stage} must be positive"
    assert any("must be positive" in e for e in dedup_errors)


def test_p6b_validator_rejects_negative_dedup_size():
    """WHEN pipeline.dedup_sizes contains a negative value
    THEN the validator reports an error."""
    cfg = Config()
    cfg.pipeline.dedup_sizes = {"search": -1, "gather": 100000, "check": 100000, "inspect": 100000}

    validator = ConfigValidator()
    try:
        validator.validate(cfg)
    except ValueError:
        pass

    dedup_errors = [e for e in validator.errors if "dedup" in e.lower() and "search" in e.lower()]
    assert len(dedup_errors) >= 1
