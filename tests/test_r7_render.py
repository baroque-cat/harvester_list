"""R7 — Render pins (cross-provider fetch sharing display line).

Traceability: /tmp/opencode/r7-plan.md §3 D10.

Pin names: test_r1_… through test_r4_….
"""

from types import SimpleNamespace

from state.display import StatusDisplayEngine, _RENDERED_METRIC_SURFACES


def _status(share_metrics=None):
    """Build a minimal SystemStatus-like object with the given share_metrics."""
    pipeline = SimpleNamespace(share_metrics=share_metrics or {})
    return SimpleNamespace(pipeline=pipeline)


# ---------------------------------------------------------------------------
# R1: exact token string for a populated metrics dict
# ---------------------------------------------------------------------------
def test_r1_populated_metrics_exact_string():
    """R1: exact token string for a populated metrics dict."""
    metrics = {
        "mode": "on",
        "claims": 42,
        "would_suppress": 0,
        "suppressed": 10,
        "takes": 5,
        "shared_fetches": 3,
        "fanout_extra": 7,
        "abandoned": 1,
        "tracked": 12,
        "ledger_full": 0,
    }
    status = _status(share_metrics=metrics)
    line = StatusDisplayEngine._format_share_metrics_line(status)
    expected = (
        "Share: mode=on "
        "claims=42 "
        "suppressed=10 "
        "shared=3 "
        "extra=7 "
        "abandoned=1 "
        "tracked=12 "
        "full=0"
    )
    assert line == expected


# ---------------------------------------------------------------------------
# R2: empty string when mode is off or metrics empty
# ---------------------------------------------------------------------------
def test_r2_empty_when_off_or_empty():
    """R2: empty string when mode is off or metrics empty."""
    # Empty metrics
    status = _status(share_metrics={})
    assert StatusDisplayEngine._format_share_metrics_line(status) == ""

    # Mode off
    status = _status(share_metrics={"mode": "off", "claims": 0})
    assert StatusDisplayEngine._format_share_metrics_line(status) == ""

    # None metrics
    status = _status(share_metrics=None)
    assert StatusDisplayEngine._format_share_metrics_line(status) == ""


# ---------------------------------------------------------------------------
# R3: "share_metrics" present in _RENDERED_METRIC_SURFACES
# ---------------------------------------------------------------------------
def test_r3_surface_registered():
    """R3: "share_metrics" present in _RENDERED_METRIC_SURFACES."""
    assert "share_metrics" in _RENDERED_METRIC_SURFACES


# ---------------------------------------------------------------------------
# R4: all-zero counter dict with mode="on" still renders
# ---------------------------------------------------------------------------
def test_r4_all_zero_counters_with_on_still_renders():
    """R4: an all-zero counter dict with mode="on" still renders (not hidden)."""
    metrics = {
        "mode": "on",
        "claims": 0,
        "would_suppress": 0,
        "suppressed": 0,
        "takes": 0,
        "shared_fetches": 0,
        "fanout_extra": 0,
        "abandoned": 0,
        "tracked": 0,
        "ledger_full": 0,
    }
    status = _status(share_metrics=metrics)
    line = StatusDisplayEngine._format_share_metrics_line(status)
    assert line != ""
    assert "mode=on" in line
    assert "claims=0" in line
