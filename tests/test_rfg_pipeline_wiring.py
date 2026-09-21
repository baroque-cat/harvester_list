"""Pipeline wiring for the search-work fan-out governor (aux S14, design D1/D9).

Covers what the stage-level integration file cannot: ``Pipeline`` builds a
governor from ``Config.refine_governor``, exposes its metrics on the status
surface, and -- per design D1 -- falls back to the built-in safe defaults in
``on`` mode when config-derived construction fails.  It must never fall back to
the unbounded state: that state is the recorded 2026-09-21 OOM incident.

Both methods under test touch nothing but ``config.refine_governor`` and
``self.refine_governor``, so they are exercised unbound against a lightweight
stand-in; no storage, worker or network dependencies are constructed.
"""

from types import SimpleNamespace

import pytest

from config.schemas import Config, RefineGovernorConfig
from manager.pipeline import Pipeline
from search import refine_governor as rfg_module

# The ten metric keys design D9 promises on the status surface.
D9_KEYS = (
    "mode",
    "children_generated",
    "children_admitted",
    "refused_depth",
    "refused_budget",
    "truncated_to_cap",
    "parents_refined",
    "parents_at_depth_cap_paginated",
    "budget_remaining",
    "coverage_estimate_min",
    "coverage_estimate_avg",
)


def _configure(config):
    """Run ``Pipeline._configure_refine_governor`` on a bare stand-in."""
    stub = SimpleNamespace()
    Pipeline._configure_refine_governor(stub, config)
    return stub


def test_configured_values_reach_the_governor():
    cfg = Config(
        refine_governor=RefineGovernorConfig(
            mode="shadow",
            max_refine_depth=3,
            max_partitions_per_refine=7,
            max_search_tasks_per_run=42,
        )
    )

    governor = _configure(cfg).refine_governor

    assert governor.mode == "shadow"
    assert governor.max_refine_depth == 3
    assert governor.max_partitions_per_refine == 7
    assert governor.max_search_tasks_per_run == 42


def test_absent_config_section_gets_governed_defaults():
    governor = _configure(Config()).refine_governor

    assert governor.mode == "on"  # default is enforced, not measuring
    assert governor.max_refine_depth == 2
    assert governor.max_partitions_per_refine == 128
    assert governor.max_search_tasks_per_run == 10000


def test_construction_failure_falls_back_to_safe_defaults(monkeypatch, caplog):
    """D1 fail-safe: a broken config yields governed defaults, never `off`."""
    real = rfg_module.RefineGovernor

    class Exploding(real):
        def __init__(self, *args, **kwargs):
            if args or kwargs:
                raise ValueError("boom: config-derived construction failed")
            super().__init__()  # built-in defaults still work

    monkeypatch.setattr(rfg_module, "RefineGovernor", Exploding)
    # An operator asking for `off` must NOT get `off` when wiring blows up.
    cfg = Config(refine_governor=RefineGovernorConfig(mode="off"))

    governor = _configure(cfg).refine_governor

    assert governor.mode == "on"
    assert governor.max_refine_depth == 2
    assert governor.max_partitions_per_refine == 128
    assert governor.max_search_tasks_per_run == 10000
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any("safe defaults" in message for message in errors)


def test_metrics_reach_the_status_surface():
    stub = _configure(Config())
    stub.refine_governor.record_depth_cap_pagination()

    metrics = Pipeline.get_refine_metrics(stub)

    for key in D9_KEYS:
        assert key in metrics, f"missing refine metric: {key}"
    assert metrics["mode"] == "on"
    assert metrics["parents_at_depth_cap_paginated"] == 1
    assert metrics["budget_remaining"] == 10000


def test_metrics_without_a_governor_are_an_empty_dict():
    """Pre-wiring / legacy managers report nothing instead of raising."""
    assert Pipeline.get_refine_metrics(SimpleNamespace()) == {}


def test_zero_budget_is_rejected_at_config_level():
    """Spec requirement 5: non-positive caps fail validation loudly.

    ``RefineGovernor`` itself stays tolerant of 0 (tests construct an exhausted
    budget directly), but configuration may not express it.
    """
    with pytest.raises(ValueError):
        RefineGovernorConfig(max_search_tasks_per_run=0)
    with pytest.raises(ValueError):
        RefineGovernorConfig(max_partitions_per_refine=0)
    with pytest.raises(ValueError):
        RefineGovernorConfig(mode="enforce")
