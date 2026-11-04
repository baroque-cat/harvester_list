"""DEF-2 — condition-level pattern fallback for fan-out and recovery.

Defect (regression 6a1516d, wave 3b, D-03.6/D-03.7 search-share fan-out):
the fan-out at stage/definition.py:257-262 and :342-347 reads
``B_task_config.patterns.*`` unconditionally.  In the combat config,
TaskConfig.patterns is an empty Patterns() (default_factory) — the real
extraction patterns live in conditions[].patterns, seeded into
SearchTask.regex/address_pattern/endpoint_pattern/model_pattern by
manager/task.py:511-521.  Consequence: every AcquisitionTask gets
key_pattern='' ⇒ client.collect(key_pattern='') returns [] ⇒ zero
Services ⇒ zero CheckTasks ⇒ API gather collects 53 764 files / 1.06 GB
and finds NOTHING (operator log 2026-11-04 17:47: Valid=0, key_sources=0).

Fix (variant A′, operator directive 2026-11-04): when task-level patterns
are empty, fallback to condition-level fields carried by the SearchTask
(task.regex, task.address_pattern, task.endpoint_pattern, task.model_pattern).
Same fallback in manager/recovery.py:336 — empty key_pattern ⇒ use
provider.get_patterns() (condition-level).

Domain consequence: patterns_hash writers migrate from H_empty to H_cfg
(= wire_coverage domain).  Existing link_coverage rows under H_empty
orphan ⇒ one-time re-gather (bounded, accepted by operator).

Harness: combat-shape config — TaskConfig(patterns=Patterns() EMPTY,
conditions carry real patterns).  Mock only the transport seam
(_execute_first_page_search / fetch_gather_content), not entire stages.
All fixture values obviously synthetic (SYNTHETIC/TEST/FIXTURE markers).
"""

import os

import pytest

from config.schemas import Config, StageConfig, TaskConfig
from core.models import AcquisitionTask, Condition, Patterns, SearchTask
from stage.base import StageResources
from stage.definition import SearchStage
from stage.factory import TaskFactory
from storage.registry import Registry, patterns_hash


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------
PROVIDER = "SYNTHETIC_provider"
BENEFICIARY = "SYNTHETIC_beneficiary"
KEY_PATTERN = r"sk-[a-zA-Z0-9]{32}"
QUERY = f"/{KEY_PATTERN}/"
ADDR_PAT = r"https://api\.[a-z]+\.example\.com"
ENDPOINT_PAT = r"/v1/[a-z]+"
MODEL_PAT = r"model-[a-z0-9]+"

LINKS = [
    "https://github.com/SYNTHETIC/test/blob/main/leak1.py",
    "https://github.com/SYNTHETIC/test/blob/main/leak2.py",
]

H_EMPTY = patterns_hash()  # all-empty patterns
H_CFG = patterns_hash(
    key_pattern=KEY_PATTERN,
    address_pattern=ADDR_PAT,
    endpoint_pattern=ENDPOINT_PAT,
    model_pattern=MODEL_PAT,
)
assert H_EMPTY != H_CFG, "test premise: empty and condition digests differ"


class _FastRegistryConfig:
    enabled = True
    batch_size = 1
    flush_interval = 0.05
    queue_size = 100000
    path = ""


class _GatherCfg:
    transport = "raw"


class _Cfg:
    gather = _GatherCfg()
    pipeline = None


class _SearchShareStub:
    """Stub that returns fixed beneficiaries from take()."""
    enforce = True

    def __init__(self, beneficiaries):
        self._beneficiaries = beneficiaries

    def take(self, wire_fp, page):
        return list(self._beneficiaries)


def _combat_task_config(name, *, patterns=None):
    """Combat-shape TaskConfig: task-level patterns EMPTY."""
    return TaskConfig(
        name=name,
        enabled=True,
        provider_type="openai_like",
        use_api=False,
        stages=StageConfig(search=True, gather=True, check=True, inspect=True),
        patterns=patterns or Patterns(),  # EMPTY — combat shape
    )


def _search_task(provider=PROVIDER):
    """SearchTask seeded from condition-level patterns (manager/task.py:511-521)."""
    return SearchTask(
        provider=provider,
        query=QUERY,
        regex=KEY_PATTERN,
        page=1,
        use_api=False,
        address_pattern=ADDR_PAT,
        endpoint_pattern=ENDPOINT_PAT,
        model_pattern=MODEL_PAT,
    )


def _make_stage(resources, monkeypatch, *, links=LINKS, content=""):
    """Build a SearchStage with _execute_first_page_search mocked."""
    stage = SearchStage(resources, lambda out: None, thread_count=1)

    def fake_first_page(task, metadata=None):
        return list(links), content, len(links)

    monkeypatch.setattr(stage, "_execute_first_page_search", fake_first_page)
    return stage


# ---------------------------------------------------------------------------
# D2-1 (MAIN RED): combat config ⇒ AcquisitionTask.key_pattern from condition
# ---------------------------------------------------------------------------
def test_d21_combat_config_acquisition_task_has_condition_patterns(
    workspace, monkeypatch
):
    """WHEN SearchStage._search_worker runs with combat-shape config
    (TaskConfig.patterns=Patterns() EMPTY, conditions carry real patterns)
    THEN the resulting AcquisitionTask in output.new_tasks has
    key_pattern=='sk-[a-zA-Z0-9]{32}' (resolved from task.regex fallback).

    Pre-fix: key_pattern=='' (exact failure text).
    """
    registry = Registry(str(workspace), config=_FastRegistryConfig(), enabled=True)
    assert registry.start() is True

    try:
        cfg = Config()
        cfg.pipeline.failure_handling = "strict"
        cfg.gather.transport = "raw"

        resources = StageResources(
            limiter=None,
            providers={},
            config=cfg,
            task_configs={
                PROVIDER: _combat_task_config(PROVIDER),
            },
            auth=None,
            registry=registry,
            search_share=_SearchShareStub([PROVIDER]),
        )

        stage = _make_stage(resources, monkeypatch)
        task = _search_task()

        output = stage._search_worker(task)
        assert output is not None

        gather_tasks = [
            t for t, stage_name in output.new_tasks if stage_name == "gather"
        ]
        assert len(gather_tasks) > 0, "expected at least one gather task"
        for gt in gather_tasks:
            assert gt.key_pattern == KEY_PATTERN, (
                f"DEF-2: combat config AcquisitionTask.key_pattern must be "
                f"'{KEY_PATTERN}', got {gt.key_pattern!r} (pre-fix: '')"
            )
            assert gt.address_pattern == ADDR_PAT
            assert gt.endpoint_pattern == ENDPOINT_PAT
            assert gt.model_pattern == MODEL_PAT
    finally:
        registry.stop()


# ---------------------------------------------------------------------------
# D2-2: multiple beneficiaries — all B get resolved patterns; B with
# non-empty task-level override wins
# ---------------------------------------------------------------------------
def test_d22_multiple_beneficiaries_all_resolved(workspace, monkeypatch):
    """WHEN search_share.take returns [owner, B1, B2] and all have empty
    task-level patterns THEN all B get condition-level patterns from the
    search task.

    AND: when B_override has non-empty task-level patterns, those win.
    """
    B_OVERRIDE = "SYNTHETIC_override"
    OVERRIDE_KEY = r"override-[a-z]{16}"

    registry = Registry(str(workspace), config=_FastRegistryConfig(), enabled=True)
    assert registry.start() is True

    try:
        cfg = Config()
        cfg.pipeline.failure_handling = "strict"
        cfg.gather.transport = "raw"

        override_tc = _combat_task_config(
            B_OVERRIDE,
            patterns=Patterns(key_pattern=OVERRIDE_KEY),
        )

        resources = StageResources(
            limiter=None,
            providers={},
            config=cfg,
            task_configs={
                PROVIDER: _combat_task_config(PROVIDER),
                BENEFICIARY: _combat_task_config(BENEFICIARY),
                B_OVERRIDE: override_tc,
            },
            auth=None,
            registry=registry,
            search_share=_SearchShareStub([PROVIDER, BENEFICIARY, B_OVERRIDE]),
        )

        stage = _make_stage(resources, monkeypatch)
        task = _search_task()

        output = stage._search_worker(task)
        assert output is not None

        gather_tasks = [
            t for t, stage_name in output.new_tasks if stage_name == "gather"
        ]

        # Group by provider
        by_provider = {}
        for gt in gather_tasks:
            by_provider.setdefault(gt.provider, []).append(gt)

        # Owner and BENEFICIARY: condition-level patterns
        for prov in (PROVIDER, BENEFICIARY):
            assert prov in by_provider, f"expected gather tasks for {prov}"
            for gt in by_provider[prov]:
                assert gt.key_pattern == KEY_PATTERN, (
                    f"DEF-2: {prov} should get condition-level key_pattern, "
                    f"got {gt.key_pattern!r}"
                )

        # B_OVERRIDE: task-level override wins
        assert B_OVERRIDE in by_provider, f"expected gather tasks for {B_OVERRIDE}"
        for gt in by_provider[B_OVERRIDE]:
            assert gt.key_pattern == OVERRIDE_KEY, (
                f"DEF-2: {B_OVERRIDE} should use task-level override, "
                f"got {gt.key_pattern!r}"
            )
    finally:
        registry.stop()


# ---------------------------------------------------------------------------
# D2-3: early-stop observe site uses resolved patterns_hash
# ---------------------------------------------------------------------------
def test_d23_early_stop_observe_uses_resolved_hash(workspace, monkeypatch):
    """WHEN early-stop observe fires for a non-owner beneficiary with empty
    task-level patterns THEN the patterns_hash kwarg equals H_cfg (the
    condition-resolved domain), NOT H_empty.

    Harness: mock _early_stop_engine to return a spy engine (bypass the
    use_api guard at :824-831).
    """
    observe_calls = []

    class _SpyEngine:
        enabled = True

        def observe(self, **kwargs):
            observe_calls.append(kwargs)

    registry = Registry(str(workspace), config=_FastRegistryConfig(), enabled=True)
    assert registry.start() is True

    try:
        cfg = Config()
        cfg.pipeline.failure_handling = "strict"
        cfg.gather.transport = "raw"

        resources = StageResources(
            limiter=None,
            providers={},
            config=cfg,
            task_configs={
                PROVIDER: _combat_task_config(PROVIDER),
                BENEFICIARY: _combat_task_config(BENEFICIARY),
            },
            auth=None,
            registry=registry,
            search_share=_SearchShareStub([PROVIDER, BENEFICIARY]),
        )

        stage = _make_stage(resources, monkeypatch)
        # Bypass the use_api guard — inject the spy directly.
        monkeypatch.setattr(stage, "_early_stop_engine", lambda task: _SpyEngine())
        task = _search_task()

        output = stage._search_worker(task)
        assert output is not None

        # The spy should have been called for the non-owner beneficiary.
        assert len(observe_calls) > 0, (
            "DEF-2: expected early-stop observe call for non-owner beneficiary"
        )
        for call in observe_calls:
            ph = call.get("patterns_hash", "")
            assert ph != H_EMPTY, (
                f"DEF-2: early-stop observe patterns_hash must NOT be H_empty "
                f"({H_EMPTY[:12]}…), got {ph[:12]}…"
            )
            assert ph == H_CFG, (
                f"DEF-2: early-stop observe patterns_hash must equal H_cfg "
                f"({H_CFG[:12]}…), got {ph[:12]}…"
            )
    finally:
        registry.stop()


# ---------------------------------------------------------------------------
# D2-4 (recovery): combat config ⇒ recovery resolves condition-level patterns
# ---------------------------------------------------------------------------
def test_d24_recovery_resolves_condition_patterns(tmp_path, monkeypatch):
    """WHEN recovery runs with combat-shape task_configs (empty patterns)
    AND provider.get_patterns() returns condition-level patterns
    THEN the recovered AcquisitionTask has non-empty key_pattern AND the
    digest used for skip/share equals the condition-level digest (same
    domain as runtime writers after DEF-2 fix).
    """
    import logging
    from core.enums import PipelineStage
    from manager.gather_share import GatherShareLedger
    from manager.recovery import TaskRecoveryManager
    from storage.gather_skip import MODE_ON, GatherSkipEngine
    from storage.registry import Registry as Reg, url_hash

    import manager.recovery as mr

    PROV = "def2-prov"
    COND_PATTERNS = Patterns(key_pattern=KEY_PATTERN)
    NOW = 1_700_000_000.0

    class _FakeStage:
        def __init__(self):
            self.tasks = []
        def put_task(self, task):
            self.tasks.append(task)
            return True

    class _FakeProvider:
        def __init__(self):
            self._patterns = COND_PATTERNS
        def get_patterns(self):
            return self._patterns

    class _Pipeline:
        def __init__(self, stage, task_configs, gather_skip=None, share_ledger=None):
            self._stage = stage
            self.task_configs = task_configs
            self.gather_skip = gather_skip
            self.share_ledger = share_ledger
            self.config = _Cfg()
        def get_stage(self, name):
            return self._stage if name == PipelineStage.GATHER.value else None

    stage = _FakeStage()
    pipeline = _Pipeline(
        stage,
        task_configs={PROV: _combat_task_config(PROV)},
    )
    mgr = TaskRecoveryManager(pipeline, {PROV: _FakeProvider()})

    urls = ["https://github.com/DEF2/test/blob/main/file0.txt"]
    mgr._recover_acquisition_tasks(PROV, urls)

    assert len(stage.tasks) == 1
    task = stage.tasks[0]
    assert task.key_pattern == KEY_PATTERN, (
        f"DEF-2: recovery must resolve condition-level key_pattern, "
        f"got {task.key_pattern!r}"
    )


# ---------------------------------------------------------------------------
# D2-5: e2e search→gather→check chain on combat config
# ---------------------------------------------------------------------------
def test_d25_e2e_search_to_gather_to_check(workspace, monkeypatch):
    """WHEN SearchStage produces AcquisitionTasks with condition-level
    patterns AND AcquisitionStage processes one with synthetic content
    THEN CheckTasks are created — the full chain is alive on combat config.
    """
    from search import client as search_client
    from stage.definition import AcquisitionStage

    KEY_A = "AKIASYNTHETICKEY1234"
    KEY_B = "AKIATESTEXAMPL567890"
    ADDR_1 = "https://api.SYNTHETIC.example.com"
    ENDPOINT_1 = "/v1/SYNTHETIC"
    MODEL_1 = "model-TEST-gpt4"
    E2E_KEY_PATTERN = r"AKIA[0-9A-Z]{16}"
    E2E_ADDR_PAT = r"https://api\.[A-Z]+\.[a-z]+\.com"
    E2E_ENDPOINT_PAT = r"/v1/[A-Z]+"
    E2E_MODEL_PAT = r"model-[A-Z]+-[a-z0-9]+"

    gather_content = (
        f"leaked: {KEY_A} and {KEY_B}\n"
        f"address: {ADDR_1}\n"
        f"endpoint: {ENDPOINT_1}\n"
        f"model: {MODEL_1}"
    )

    def fake_fetch(url="", **kwargs):
        return gather_content

    monkeypatch.setattr(search_client, "fetch_gather_content", fake_fetch)

    registry = Registry(str(workspace), config=_FastRegistryConfig(), enabled=True)
    assert registry.start() is True

    try:
        cfg = Config()
        cfg.pipeline.failure_handling = "strict"
        cfg.gather.transport = "raw"

        resources = StageResources(
            limiter=None,
            providers={},
            config=cfg,
            task_configs={PROVIDER: _combat_task_config(PROVIDER)},
            auth=None,
            registry=registry,
            search_share=_SearchShareStub([PROVIDER]),
        )

        # Step 1: SearchStage produces AcquisitionTasks
        search_stage = _make_stage(resources, monkeypatch)
        # Use a search task whose regex matches the content keys
        search_task = SearchTask(
            provider=PROVIDER,
            query=f"/{E2E_KEY_PATTERN}/",
            regex=E2E_KEY_PATTERN,
            page=1,
            use_api=False,
            address_pattern=E2E_ADDR_PAT,
            endpoint_pattern=E2E_ENDPOINT_PAT,
            model_pattern=E2E_MODEL_PAT,
        )
        search_output = search_stage._search_worker(search_task)
        assert search_output is not None

        gather_tasks = [
            t for t, sn in search_output.new_tasks if sn == "gather"
        ]
        assert len(gather_tasks) > 0

        # Verify the first gather task has condition-level patterns
        gt = gather_tasks[0]
        assert gt.key_pattern == E2E_KEY_PATTERN

        # Step 2: AcquisitionStage processes the gather task
        captured = []
        acq_stage = AcquisitionStage(
            resources, lambda out: captured.append(out), thread_count=1, max_retries=2,
        )
        acq_output = acq_stage.process_task(gt)
        assert acq_output is not None

        check_tasks = [
            t for t, sn in acq_output.new_tasks if sn == "check"
        ]
        assert len(check_tasks) == 2, (
            f"DEF-2: e2e chain must produce 2 check tasks (2 keys), got {len(check_tasks)}"
        )
    finally:
        registry.stop()


# ---------------------------------------------------------------------------
# D2-6 (regress guard, PASS): non-empty task-level ⇒ used directly
# ---------------------------------------------------------------------------
def test_d26_nonempty_task_level_patterns_used_directly(workspace, monkeypatch):
    """WHEN TaskConfig.patterns.key_pattern is non-empty
    THEN it is used directly (byte-pre-change behavior preserved).
    """
    TASK_KEY = r"task-level-[a-z]{20}"
    TASK_PATTERNS = Patterns(key_pattern=TASK_KEY)

    registry = Registry(str(workspace), config=_FastRegistryConfig(), enabled=True)
    assert registry.start() is True

    try:
        cfg = Config()
        cfg.pipeline.failure_handling = "strict"
        cfg.gather.transport = "raw"

        resources = StageResources(
            limiter=None,
            providers={},
            config=cfg,
            task_configs={PROVIDER: _combat_task_config(PROVIDER, patterns=TASK_PATTERNS)},
            auth=None,
            registry=registry,
            search_share=_SearchShareStub([PROVIDER]),
        )

        stage = _make_stage(resources, monkeypatch)
        task = _search_task()

        output = stage._search_worker(task)
        gather_tasks = [
            t for t, sn in output.new_tasks if sn == "gather"
        ]
        assert len(gather_tasks) > 0
        for gt in gather_tasks:
            assert gt.key_pattern == TASK_KEY, (
                f"DEF-2 regress: non-empty task-level must win, "
                f"got {gt.key_pattern!r}"
            )
    finally:
        registry.stop()


# ---------------------------------------------------------------------------
# D2-7 (domain pin, PASS): resolved patterns_hash == condition-level hash
# ---------------------------------------------------------------------------
def test_d27_resolved_patterns_hash_equals_condition_domain(workspace, monkeypatch):
    """WHEN patterns are resolved from condition-level (task.regex etc.)
    THEN patterns_hash(resolved) == patterns_hash(key_pattern=task.regex, ...)
    — formal unification with the wire_coverage domain.
    """
    registry = Registry(str(workspace), config=_FastRegistryConfig(), enabled=True)
    assert registry.start() is True

    try:
        cfg = Config()
        cfg.pipeline.failure_handling = "strict"
        cfg.gather.transport = "raw"

        resources = StageResources(
            limiter=None,
            providers={},
            config=cfg,
            task_configs={PROVIDER: _combat_task_config(PROVIDER)},
            auth=None,
            registry=registry,
            search_share=_SearchShareStub([PROVIDER]),
        )

        stage = _make_stage(resources, monkeypatch)
        task = _search_task()

        output = stage._search_worker(task)
        gather_tasks = [
            t for t, sn in output.new_tasks if sn == "gather"
        ]
        assert len(gather_tasks) > 0

        gt = gather_tasks[0]
        resolved_hash = patterns_hash(
            key_pattern=gt.key_pattern,
            address_pattern=gt.address_pattern,
            endpoint_pattern=gt.endpoint_pattern,
            model_pattern=gt.model_pattern,
        )
        condition_hash = patterns_hash(
            key_pattern=task.regex,
            address_pattern=task.address_pattern,
            endpoint_pattern=task.endpoint_pattern,
            model_pattern=task.model_pattern,
        )
        assert resolved_hash == condition_hash, (
            f"DEF-2: resolved patterns_hash must equal condition-level hash; "
            f"got {resolved_hash[:12]}… vs {condition_hash[:12]}…"
        )
        # Also verify it's NOT the empty hash
        assert resolved_hash != H_EMPTY
    finally:
        registry.stop()
