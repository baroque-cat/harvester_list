"""Pagination fallback when refine admission is empty (F1, BLOCKER).

Traceability: 02_critical_fixes.md F1.

The refine branch (``if refine: …``) and the pagination branch
(``elif total > per_page: …``) in ``_handle_first_page_results`` are mutually
exclusive.  When the governor refuses ALL children (``enqueued == 0``), the
refine branch runs but produces nothing, and the pagination branch is
unreachable — the parent's pages 2..N are lost (up to 900 links per parent).

Fix:
(a) Extract the pagination body into ``_paginate_first_page``.
(b) After the admission loop in the refine branch, if ``enqueued == 0`` and
    ``total > per_page``, call ``_paginate_first_page`` (fallback).
(c) Counter ``pagination_fallbacks`` on the governor (mirror of
    ``record_depth_cap_pagination``).
(d) Render token ``fallbacks=<n>`` at the END of the Refine line.

RED predictions (pre-fix):
- Pin 1: assertion — no page tasks created (fallback not implemented).
- Pin 2: KeyError — ``pagination_fallbacks`` absent from governor metrics.
- Pin 3: assertion — render lacks ``fallbacks=`` token.
- Pin 4: KeyError — ``pagination_fallbacks`` absent (non-regression).
- Pin 5: KeyError — ``pagination_fallbacks`` absent (non-regression).
"""

import math
from types import SimpleNamespace

import pytest

from config.schemas import Config, RefineGovernorConfig, StageConfig, TaskConfig
from core.metrics import PipelineStatus, StageMetrics
from core.models import Patterns, SearchTask
from search import client as search_client
from search.refine_governor import RefineGovernor
from stage.base import StageResources
from stage.definition import SearchStage
from state.display import StatusDisplayEngine, get_display_config
from state.enums import DisplayMode, StatusContext
from state.models import SystemStatus

PROVIDER = "deepseek"
KEY_PATTERN = r"sk-[A-Za-z0-9]{16}"
RAW_SEED = "/sk-[a-zA-Z0-9]{32}/"
ROOT_WIRE = '"sk-"'
# A depth-1 parent query: its children will all wire-collapse onto it because
# clean_regex extracts "sk-0" (4 chars fixed) then the next char class breaks
# the run, yielding wire "sk-0" — identical to the parent's wire.
DEPTH1_QUERY = "/sk-0[a-zA-Z0-9]{30}/"
DEPTH1_WIRE = '"sk-0"'
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


def _governor(mode="on", depth=2, partitions=128, budget=10000,
              drop_wire=True):
    return RefineGovernor(
        mode=mode,
        max_refine_depth=depth,
        max_partitions_per_refine=partitions,
        max_search_tasks_per_run=budget,
        drop_wire_indistinguishable=drop_wire,
    )


def _stage(gov):
    cfg = Config()
    cfg.pipeline.failure_handling = "legacy"
    cfg.refine_governor = RefineGovernorConfig(
        mode=gov.mode,
        max_refine_depth=gov.max_refine_depth,
        max_partitions_per_refine=gov.max_partitions_per_refine,
        max_search_tasks_per_run=max(gov.max_search_tasks_per_run, 1),
    )
    resources = StageResources(
        limiter=None,
        providers={},
        config=cfg,
        task_configs={PROVIDER: _task_config()},
        auth=FakeAuth(),
        registry=None,
        refine_governor=gov,
    )
    outputs = []
    stage = SearchStage(resources, outputs.append, thread_count=1)
    return stage, outputs


def _mock_search(monkeypatch, totals_by_wire, spy=None):
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


def _depth1_task():
    """A depth-1 parent whose children all wire-collapse onto it."""
    return SearchTask(
        provider=PROVIDER, query=DEPTH1_QUERY, regex=KEY_PATTERN,
        page=1, use_api=True, refine_depth=1,
    )


def _page_children(output, query):
    return [t for t, stage_name in output.new_tasks
            if stage_name == "search" and t.query == query and t.page > 1]


def _search_children(output, parent_query):
    return [t for t, stage_name in output.new_tasks
            if stage_name == "search" and t.query != parent_query]


# ---------------------------------------------------------------------------
# F1 pin 1: all children refused + total > per_page ⇒ pagination fallback
# ---------------------------------------------------------------------------
def test_pagination_fallback_when_all_children_refused(monkeypatch):
    """WHEN refine produces children but the governor refuses them ALL (wire
    collapse or budget) AND total > per_page THEN the parent falls back to
    pagination and pages 2..N are created.
    """
    gov = _governor("on", depth=2, partitions=128, budget=10000)
    stage, outputs = _stage(gov)
    # Mock select_children to return empty list (simulating all refused)
    original_select = gov.select_children
    def refusing_select(*args, **kwargs):
        # Call original to update counters, but return empty
        original_select(*args, **kwargs)
        return []
    monkeypatch.setattr(gov, "select_children", refusing_select)
    
    _mock_search(monkeypatch, {ROOT_WIRE: 5000})

    out = stage.process_task(_root_task())

    assert out is not None
    # No children were admitted (all refused)
    children = _search_children(out, RAW_SEED)
    assert len(children) == 0, f"expected 0 children, got {len(children)}"
    # Fallback: pages 2..N must exist
    pages = _page_children(out, RAW_SEED)
    expected_pages = min(math.ceil(5000 / API_PER_PAGE), 10) - 1  # API_MAX_PAGES=10
    assert len(pages) == expected_pages, (
        f"expected {expected_pages} page tasks, got {len(pages)}"
    )


# ---------------------------------------------------------------------------
# F1 pin 2: counter pagination_fallbacks incremented exactly once
# ---------------------------------------------------------------------------
def test_pagination_fallbacks_counter_incremented(monkeypatch):
    """WHEN the pagination fallback fires THEN the governor's
    ``pagination_fallbacks`` counter increments by exactly 1.
    """
    gov = _governor("on", depth=2, partitions=128, budget=10000)
    stage, _ = _stage(gov)
    # Mock select_children to return empty list (simulating all refused)
    original_select = gov.select_children
    def refusing_select(*args, **kwargs):
        original_select(*args, **kwargs)
        return []
    monkeypatch.setattr(gov, "select_children", refusing_select)
    
    _mock_search(monkeypatch, {ROOT_WIRE: 5000})

    assert gov.metrics.get("pagination_fallbacks", 0) == 0
    stage.process_task(_root_task())
    assert gov.metrics["pagination_fallbacks"] == 1


# ---------------------------------------------------------------------------
# F1 pin 3: render contains fallbacks= token
# ---------------------------------------------------------------------------
def test_render_contains_fallbacks_token():
    """WHEN refine_metrics carries pagination_fallbacks THEN the Refine render
    line contains a 'fallbacks=' token at the end (before the unfit note).
    """
    refine = {
        "mode": "on",
        "children_generated": 144,
        "children_admitted": 0,
        "refused_depth": 0,
        "refused_budget": 0,
        "truncated_to_cap": 0,
        "parents_refined": 1,
        "parents_at_depth_cap_paginated": 0,
        "budget_remaining": 9999,
        "coverage_estimate_min": 0.0027,
        "coverage_estimate_avg": 0.0746,
        "distinct_wire_admitted": 0,
        "refused_wire_collapse": 144,
        "search_swallowed_exceptions": 0,
        "pagination_fallbacks": 1,
    }
    status = SystemStatus(pipeline=PipelineStatus(
        stages={"search": StageMetrics(name="search", workers=1)},
        refine_metrics=refine,
    ))
    cfg = get_display_config(StatusContext.APPLICATION, DisplayMode.DETAILED)
    lines = StatusDisplayEngine()._format_pipeline_section(status, cfg)
    refine_lines = [ln for ln in lines if ln.startswith("Refine:")]
    assert len(refine_lines) == 1
    assert "fallbacks=1" in refine_lines[0]


# ---------------------------------------------------------------------------
# F1 pin 4 (non-regression): enqueued > 0 ⇒ no fallback
# ---------------------------------------------------------------------------
def test_no_fallback_when_children_admitted(monkeypatch):
    """WHEN refine admits at least one child THEN pagination fallback does NOT
    fire and the counter stays at 0.
    """
    # Use wire-collapse guard disabled so children ARE admitted
    gov = _governor("on", depth=2, partitions=3, budget=10000, drop_wire=False)
    stage, _ = _stage(gov)
    _mock_search(monkeypatch, {ROOT_WIRE: 5000})

    out = stage.process_task(_root_task())

    children = _search_children(out, RAW_SEED)
    assert len(children) > 0, "expected at least one admitted child"
    pages = _page_children(out, RAW_SEED)
    assert len(pages) == 0, "no pagination fallback expected when children admitted"
    assert gov.metrics["pagination_fallbacks"] == 0


# ---------------------------------------------------------------------------
# F1 pin 5 (non-regression): total <= per_page ⇒ no fallback
# ---------------------------------------------------------------------------
def test_no_fallback_when_total_le_per_page(monkeypatch):
    """WHEN total <= per_page THEN neither refine nor pagination fires, and
    the fallback counter stays at 0.
    """
    gov = _governor("on", depth=2, partitions=128, budget=10000)
    stage, _ = _stage(gov)
    # total=50 < limit=1000, so refine=False; total=50 < per_page=100, so
    # pagination branch is also skipped.  No fallback possible.
    _mock_search(monkeypatch, {ROOT_WIRE: 50})

    out = stage.process_task(_root_task())

    children = _search_children(out, RAW_SEED)
    pages = _page_children(out, RAW_SEED)
    assert len(children) == 0
    assert len(pages) == 0
    assert gov.metrics["pagination_fallbacks"] == 0
