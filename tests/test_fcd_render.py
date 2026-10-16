"""Rendering tests for add-file-commit-dating.

Traceability: file-commit-dating-S25, S26.

S25: all counters and the fill rate appear in the rendered status text.
S26: disabled → no line; enabled with zero evidence → line present with
fill=0.0000 and non-zero req/absent.

No network.
"""

from core.metrics import PipelineStatus
from state.display import StatusDisplayEngine
from state.models import SystemStatus


def _status(file_dating_metrics=None):
    """Build a SystemStatus with the given file_dating_metrics."""
    pipeline = PipelineStatus(
        file_dating_metrics=file_dating_metrics or {},
    )
    return SystemStatus(pipeline=pipeline)


def test_s25_all_counters_appear_in_rendered_text():
    """S25: all counters and the fill rate appear in the rendered status text."""
    metrics = {
        "enabled": True,
        "scope": "keyed",
        "transport": "rest",
        "file_dating_requests": 42,
        "file_dating_304s": 5,
        "file_dating_filled": 30,
        "file_dating_absent": 7,
        "file_dating_batches": 0,
        "file_dating_graphql_cost": 0,
        "file_dating_refusals": 1,
        "file_dating_budget_stopped": 2,
        "cached_hits": 10,
        "undateable_link": 3,
        "requests_this_run": 42,
        "budget_remaining": 158,
        "fill_rate": 0.8108,
    }
    status = _status(metrics)
    line = StatusDisplayEngine._format_file_dating_metrics_line(status)

    assert line != ""
    assert "Dating:" in line
    assert "scope=keyed" in line
    assert "transport=rest" in line
    assert "req=42" in line
    assert "304=5" in line
    assert "filled=30" in line
    assert "absent=7" in line
    assert "batches=0" in line
    assert "cost=0" in line
    assert "refusals=1" in line
    assert "budget_stop=2" in line
    assert "fill=0.8108" in line


def test_s26_disabled_renders_no_line():
    """S26: disabled → no line at all."""
    status = _status({"enabled": False})
    line = StatusDisplayEngine._format_file_dating_metrics_line(status)
    assert line == ""


def test_s26_enabled_zero_evidence_renders_line():
    """S26: enabled with zero evidence → line present with fill=0.0000."""
    metrics = {
        "enabled": True,
        "scope": "keyed",
        "transport": "rest",
        "file_dating_requests": 10,
        "file_dating_304s": 0,
        "file_dating_filled": 0,
        "file_dating_absent": 10,
        "file_dating_batches": 0,
        "file_dating_graphql_cost": 0,
        "file_dating_refusals": 0,
        "file_dating_budget_stopped": 0,
        "cached_hits": 0,
        "undateable_link": 0,
        "requests_this_run": 10,
        "budget_remaining": 190,
        "fill_rate": 0.0,
    }
    status = _status(metrics)
    line = StatusDisplayEngine._format_file_dating_metrics_line(status)

    assert line != ""
    assert "req=10" in line
    assert "absent=10" in line
    assert "fill=0.0000" in line


def test_s26_empty_metrics_renders_no_line():
    """Empty metrics (no file_dating section) → no line."""
    status = _status({})
    line = StatusDisplayEngine._format_file_dating_metrics_line(status)
    assert line == ""
