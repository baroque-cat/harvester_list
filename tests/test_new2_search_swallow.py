"""Search-stage catch-all must not silently swallow exceptions (NEW-2).

Traceability: 02_critical_fixes.md NEW-2, failure-handling spec.

The catch-all in ``_search_worker`` (stage/definition.py:257-259) previously
returned None on ANY exception, which the worker framework counted as a
completed task.  Refine subtree failures (including RefineEngine.generate_queries
at :422) disappeared silently with no counter.

Fix: the catch-all increments ``search_swallowed_exceptions`` and rewraps the
exception as ``TransientFetchError`` so the existing failure_handling policy
applies (strict → requeue, shadow → detect path, legacy → pre-change).
"""

import logging

import pytest

from config.schemas import Config, StageConfig, TaskConfig
from core.exceptions import TransientFetchError
from core.models import Patterns, SearchTask
from search import client as search_client
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


def _boom_generic(monkeypatch, calls=None):
    """Make the search client raise a generic (non-transient) exception."""
    def failing(**kwargs):
        if calls is not None:
            calls.append(1)
        raise RuntimeError("refine engine exploded")

    monkeypatch.setattr(search_client, "search_with_count", failing)


# ---------------------------------------------------------------------------
# NEW-2 pin 1: generic exception in search-worker increments the counter
# ---------------------------------------------------------------------------
def test_search_swallowed_exceptions_counter_incremented(monkeypatch):
    """WHEN _search_worker catches a non-transient exception THEN
    search_swallowed_exceptions is incremented (was 0 before the fix).
    """
    calls = []
    _boom_generic(monkeypatch, calls)
    stage = _search_stage("shadow")

    # process_task will see the TransientFetchError rewrap; in shadow mode it
    # returns None (same outcome as legacy) but the counter must have moved.
    stage.process_task(_search_task())

    assert stage.search_swallowed_exceptions == 1


# ---------------------------------------------------------------------------
# NEW-2 pin 2: strict mode requeues instead of silently completing
# ---------------------------------------------------------------------------
def test_strict_mode_requeues_on_generic_exception(monkeypatch):
    """WHEN failure_handling is strict AND _search_worker catches a generic
    exception THEN TransientFetchError propagates (the worker loop will
    requeue), the task is NOT silently marked processed.
    """
    _boom_generic(monkeypatch)
    stage = _search_stage("strict")

    with pytest.raises(TransientFetchError):
        stage.process_task(_search_task())

    # The counter still increments (the exception WAS swallowed at the inner
    # level before rewrapping).
    assert stage.search_swallowed_exceptions == 1


# ---------------------------------------------------------------------------
# NEW-2 pin 3: shadow mode goes through the detect path
# ---------------------------------------------------------------------------
def test_shadow_mode_goes_through_detect_path(monkeypatch, caplog):
    """WHEN failure_handling is shadow AND _search_worker catches a generic
    exception THEN _detect_failure_empty fires (failure_empties_detected
    increments) AND search_swallowed_exceptions increments.
    """
    _boom_generic(monkeypatch)
    stage = _search_stage("shadow")

    with caplog.at_level(logging.WARNING):
        out = stage.process_task(_search_task())

    assert out is None  # shadow preserves harvest behavior
    assert stage.search_swallowed_exceptions == 1
    assert stage.failure_empties_detected == 1
    assert any(PROVIDER in rec.getMessage() for rec in caplog.records)


# ---------------------------------------------------------------------------
# NEW-2 pin 4: render contains the new token
# ---------------------------------------------------------------------------
def test_render_contains_swallowed_token():
    """WHEN refine_metrics carries search_swallowed_exceptions THEN the
    Refine render line contains a 'swallowed=' token.
    """
    from core.metrics import PipelineStatus, StageMetrics
    from state.display import StatusDisplayEngine, get_display_config
    from state.enums import DisplayMode, StatusContext
    from state.models import SystemStatus

    refine = {
        "mode": "on",
        "children_generated": 144,
        "children_admitted": 128,
        "refused_depth": 0,
        "refused_budget": 0,
        "truncated_to_cap": 16,
        "parents_refined": 4,
        "parents_at_depth_cap_paginated": 0,
        "budget_remaining": 9872,
        "coverage_estimate_min": 0.0027,
        "coverage_estimate_avg": 0.0746,
        "distinct_wire_admitted": 128,
        "refused_wire_collapse": 0,
        "search_swallowed_exceptions": 3,
    }
    status = SystemStatus(pipeline=PipelineStatus(
        stages={"search": StageMetrics(name="search", workers=1)},
        refine_metrics=refine,
    ))
    cfg = get_display_config(StatusContext.APPLICATION, DisplayMode.DETAILED)
    lines = StatusDisplayEngine()._format_pipeline_section(status, cfg)
    refine_lines = [ln for ln in lines if ln.startswith("Refine:")]
    assert len(refine_lines) == 1
    assert "swallowed=3" in refine_lines[0]
