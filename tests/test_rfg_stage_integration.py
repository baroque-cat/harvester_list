"""SearchStage integration for the refine fan-out governor (add-refine-fanout-governor).

Traceability: refine-fanout-governor-S1, S2, S3, S5 (spy), S8, S10 (stage),
S11 (stage), S14, S16, S17 (both scenarios).

Harness: real SearchStage + real RefineEngine (deterministic regex-split for
the incident seed), mocked ``search_with_count`` returning canned totals per
wire query; governor injected via the new ``StageResources.refine_governor``
field. No network.

RED state notes (expected collection/attribute failures pre-implementation):
- ``from search.refine_governor import RefineGovernor`` - module absent;
- ``from config.schemas import RefineGovernorConfig`` - class absent;
- ``StageResources(..., refine_governor=...)`` - unexpected keyword;
- ``SearchTask(..., refine_depth=1)`` - unexpected keyword;
- ``PipelineStatus().refine_metrics`` - attribute absent.
"""

import math
from types import SimpleNamespace

import pytest

from config.schemas import Config, RefineGovernorConfig, StageConfig, TaskConfig  # RED: RefineGovernorConfig
from constant.search import API_MAX_PAGES
from core.exceptions import TransientFetchError
from core.metrics import PipelineStatus
from core.models import Patterns, SearchTask
from search import client as search_client
from search.github.refine.engine import RefineEngine
from search.refine_governor import RefineGovernor  # RED driver: module absent
from stage.base import StageResources
from stage.definition import SearchStage

PROVIDER = "deepseek"
KEY_PATTERN = r"sk-[A-Za-z0-9]{16}"
RAW_SEED = "/sk-[a-zA-Z0-9]{32}/"          # incident seed; splittable regex form
ROOT_WIRE = '"sk-"'                          # its API wire form (clean_regex)
API_LIMIT = 1000
API_PER_PAGE = 100


class FakeAuth:
    def get_session(self):
        return "session-token"

    def get_token(self):
        return "api-token"

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


def _governor(mode="on", depth=1, partitions=3, budget=50):
    return RefineGovernor(
        mode=mode,
        max_refine_depth=depth,
        max_partitions_per_refine=partitions,
        max_search_tasks_per_run=budget,
    )


def _stage(gov, early_stop=None, failure_mode="legacy"):
    cfg = Config()
    cfg.pipeline.failure_handling = failure_mode
    # The config mirror is descriptive only -- the stage reads the *injected*
    # governor.  Keep it valid: configuration rejects non-positive caps (spec
    # requirement 5) while ``RefineGovernor`` itself tolerates an already
    # exhausted 0 budget, which is exactly the state S8 constructs.
    cfg.refine_governor = RefineGovernorConfig(
        mode=gov.mode if gov else "off",
        max_refine_depth=gov.max_refine_depth if gov else 2,
        max_partitions_per_refine=gov.max_partitions_per_refine if gov else 128,
        max_search_tasks_per_run=max(gov.max_search_tasks_per_run, 1) if gov else 10000,
    )
    resources = StageResources(
        limiter=None,
        providers={},
        config=cfg,
        task_configs={PROVIDER: _task_config()},
        auth=FakeAuth(),
        registry=None,
        early_stop=early_stop,
        refine_governor=gov,  # RED: unexpected keyword pre-implementation
    )
    outputs = []
    stage = SearchStage(resources, outputs.append, thread_count=1)
    return stage, outputs


def _mock_search(monkeypatch, totals_by_wire, spy=None):
    """Canned (urls, total, '') per wire query; default total for children."""

    def fake(**kwargs):
        wire = kwargs["query"]
        total = totals_by_wire.get(wire, totals_by_wire.get("__default__", 0))
        if spy is not None:
            spy.append((wire, kwargs.get("page", 1)))
        n = min(total, API_PER_PAGE) if total else 0
        urls = [f"https://github.com/o/r/blob/main/f{i}.py" for i in range(n)]
        return urls, total, ""

    monkeypatch.setattr(search_client, "search_with_count", fake)


def _root_task():
    return SearchTask(provider=PROVIDER, query=RAW_SEED, regex=KEY_PATTERN, page=1, use_api=True)


def _search_children(output):
    return [t for t, stage_name in output.new_tasks
            if stage_name == "search" and t.query != RAW_SEED]


def _page_children(output):
    return [t for t, stage_name in output.new_tasks
            if stage_name == "search" and t.query == RAW_SEED and t.page > 1]


# ---------------------------------------------------------------------------
# S1 + S4 + S5(spy) + S16: governed root refinement
# ---------------------------------------------------------------------------
def test_s1_root_refines_capped_children_with_depth_and_attribution(monkeypatch):
    gov = _governor("on", depth=1, partitions=3, budget=50)
    stage, outputs = _stage(gov)
    _mock_search(monkeypatch, {ROOT_WIRE: 46_000_000})

    # Spy on the engine call to prove the clamp reaches the generator (S5).
    engine = RefineEngine.get_instance()
    seen_partitions = []
    original = engine.generate_queries

    def spying(query, partitions):
        seen_partitions.append(partitions)
        return original(query=query, partitions=partitions)

    monkeypatch.setattr(engine, "generate_queries", spying)

    out = stage.process_task(_root_task())

    assert out is not None
    children = _search_children(out)
    assert 0 < len(children) <= 3                       # cap enforced (S4)
    assert seen_partitions and max(seen_partitions) <= 3  # generator got clamped value (S5)
    for child in children:
        assert child.refine_depth == 1                  # S1: depth inherited
        assert child.provider == PROVIDER               # S16: attribution kept
        assert child.regex == KEY_PATTERN
        assert child.use_api is True
        assert child.page == 1
    m = gov.metrics
    assert m["children_generated"] >= m["children_admitted"] == len(children)
    assert m["parents_refined"] == 1


# ---------------------------------------------------------------------------
# S2: depth cap falls through to pagination, counted
# ---------------------------------------------------------------------------
def test_s2_depth_cap_paginates_instead_of_refining(monkeypatch):
    gov = _governor("on", depth=1, partitions=3, budget=50)
    stage, _ = _stage(gov)
    _mock_search(monkeypatch, {"__default__": 2_000_000})  # child total >> limit

    child = SearchTask(  # RED: refine_depth kwarg absent pre-fix
        provider=PROVIDER, query="/sk-aaa[a-zA-Z0-9]{29}/", regex=KEY_PATTERN,
        page=1, use_api=True, refine_depth=1,
    )
    out = stage.process_task(child)

    refined = [t for t, s in out.new_tasks if s == "search" and t.query != child.query]
    pages = [t for t, s in out.new_tasks if s == "search" and t.query == child.query]
    assert refined == []                                 # no grandchildren
    # Ordinary pagination, capped at the transport page cap (API_MAX_PAGES).
    assert len(pages) == min(math.ceil(2_000_000 / API_PER_PAGE), API_MAX_PAGES) - 1
    assert all(t.refine_depth == 1 for t in pages)       # pagination inherits depth
    assert gov.metrics["parents_at_depth_cap_paginated"] == 1


# ---------------------------------------------------------------------------
# S3: additive serialization both directions
# ---------------------------------------------------------------------------
def test_s3_depth_serializes_and_legacy_defaults_to_zero():
    from stage.factory import TaskFactory

    task = SearchTask(provider=PROVIDER, query="q", regex=KEY_PATTERN, page=1,
                      use_api=True, refine_depth=2)
    roundtrip = TaskFactory.from_dict(task.to_dict())
    assert roundtrip.refine_depth == 2

    legacy = task.to_dict()
    legacy.pop("refine_depth", None)          # pre-change queue JSON shape
    assert TaskFactory.from_dict(legacy).refine_depth == 0


# ---------------------------------------------------------------------------
# S8: page tasks and roots are budget-exempt; exhausted budget refuses children
# ---------------------------------------------------------------------------
def test_s8_pagination_runs_with_exhausted_budget(monkeypatch):
    gov = _governor("on", depth=1, partitions=3, budget=0)
    stage, _ = _stage(gov)
    _mock_search(monkeypatch, {"__default__": 500})  # 500 < limit -> pagination path

    paginating = SearchTask(provider=PROVIDER, query="/sk-bbb[a-zA-Z0-9]{29}/",
                            regex=KEY_PATTERN, page=1, use_api=True, refine_depth=1)
    out = stage.process_task(paginating)
    pages = [t for t, s in out.new_tasks if s == "search" and t.page > 1]
    assert len(pages) == 4                     # pages 2..5 despite zero budget
    assert gov.metrics["refused_budget"] == 0  # pages never touch the budget

    # Root still executes under an exhausted budget; its children are refused loudly.
    _mock_search(monkeypatch, {ROOT_WIRE: 46_000_000})
    out_root = stage.process_task(_root_task())
    assert out_root is not None
    assert _search_children(out_root) == []
    assert gov.metrics["refused_budget"] > 0


# ---------------------------------------------------------------------------
# S10 (stage level): off reproduces the unclamped pre-change child set
# ---------------------------------------------------------------------------
def test_s10_off_matches_unclamped_generator_output(monkeypatch):
    gov = _governor("off", depth=1, partitions=2, budget=1)
    stage, _ = _stage(gov)
    total = 5_000                              # partitions = ceil(5000/1000) = 5
    _mock_search(monkeypatch, {ROOT_WIRE: total})

    out = stage.process_task(_root_task())
    children = {t.query for t in _search_children(out)}

    expected = {q for q in RefineEngine.get_instance().generate_queries(query=RAW_SEED, partitions=5)
                if q and q != RAW_SEED}
    assert children == expected                # nothing withheld, nothing reordered away
    assert gov.metrics["children_admitted"] == 0
    assert gov.metrics["truncated_to_cap"] == 0


# ---------------------------------------------------------------------------
# S11 (stage level): shadow enqueues everything, counts would-be refusals
# ---------------------------------------------------------------------------
def test_s11_shadow_enqueues_everything_but_counts(monkeypatch):
    shadow = _governor("shadow", depth=1, partitions=2, budget=100)
    stage, _ = _stage(shadow)
    total = 5_000
    _mock_search(monkeypatch, {ROOT_WIRE: total})

    out = stage.process_task(_root_task())
    children = {t.query for t in _search_children(out)}

    expected = {q for q in RefineEngine.get_instance().generate_queries(query=RAW_SEED, partitions=5)
                if q and q != RAW_SEED}
    assert children == expected                        # enforcement: none
    m = shadow.metrics
    assert m["children_generated"] == len(expected)
    assert m["truncated_to_cap"] >= 1                  # would-be truncation measured


# ---------------------------------------------------------------------------
# S14: metrics surface
# ---------------------------------------------------------------------------
def test_s14_metrics_reach_status_surface(monkeypatch):
    gov = _governor("on", depth=1, partitions=3, budget=50)
    stage, _ = _stage(gov)
    _mock_search(monkeypatch, {ROOT_WIRE: 46_000_000})
    stage.process_task(_root_task())

    m = gov.metrics
    for key in ("children_generated", "children_admitted", "refused_depth",
                "refused_budget", "truncated_to_cap", "parents_refined",
                "parents_at_depth_cap_paginated", "budget_remaining",
                "coverage_estimate_min", "coverage_estimate_avg"):
        assert key in m, f"missing metric {key}"

    status = PipelineStatus()
    assert isinstance(status.refine_metrics, dict)     # RED pre-fix: field absent


# ---------------------------------------------------------------------------
# S17: strict failure contract applies to governed children unchanged
# ---------------------------------------------------------------------------
def test_s17_strict_failure_propagates_for_governed_child(monkeypatch):
    gov = _governor("on")
    stage, _ = _stage(gov, failure_mode="strict")

    def failing(**kwargs):
        raise TransientFetchError("network down")

    monkeypatch.setattr(search_client, "search_with_count", failing)
    child = SearchTask(provider=PROVIDER, query="/sk-ccc[a-zA-Z0-9]{29}/",
                       regex=KEY_PATTERN, page=1, use_api=True, refine_depth=1)

    with pytest.raises(TransientFetchError):
        stage.process_task(child)


# ---------------------------------------------------------------------------
# S18: early-stop observes executed governed pages per (provider, query)
# ---------------------------------------------------------------------------
def test_s18_early_stop_observes_governed_pages(monkeypatch):
    observed = []

    class FakeEngine:
        enabled = True

        def observe(self, **kwargs):
            observed.append((kwargs["provider"], kwargs["query"], kwargs["page"]))
            return SimpleNamespace(stopped=False)

        def max_pages(self, provider, query):
            return 10

    gov = _governor("on")
    stage, _ = _stage(gov, early_stop=FakeEngine())
    child_query = "/sk-ddd[a-zA-Z0-9]{29}/"
    child_wire = RefineEngine.get_instance().clean_regex(query=child_query) or child_query
    _mock_search(monkeypatch, {child_wire: 500})

    child = SearchTask(provider=PROVIDER, query=child_query, regex=KEY_PATTERN,
                       page=1, use_api=True, refine_depth=1)
    stage.process_task(child)

    assert (PROVIDER, child_query, 1) in observed
