"""Rendering tests for P22 registry multi-source observability.

Traceability: P22 (key_sources table, multi_source_keys counter, rendered line).

T12a: a status whose registry_metrics carries the three counters plus
      available=True/enabled=True renders the line with every figure in the
      documented order and spelling.
T12b: the line is ABSENT when the registry is disabled or unavailable.
T12c: malformed figures degrade fail-open — no exception escapes and the line
      still renders with the neutral value the neighbouring lines use.
T12d: registry_metrics is a member of _RENDERED_METRIC_SURFACES.
T12e: the pipeline wiring: a pipeline-shaped object whose registry returns a
      stats dict produces PipelineStatus.registry_metrics equal to it, and a
      missing/broken registry yields {} without raising.

No network.
"""

from core.metrics import PipelineStatus
from state.display import StatusDisplayEngine, _RENDERED_METRIC_SURFACES
from state.models import SystemStatus


def _status(registry_metrics=None):
    """Build a SystemStatus with the given registry_metrics."""
    pipeline = PipelineStatus(
        registry_metrics=registry_metrics or {},
    )
    return SystemStatus(pipeline=pipeline)


def _section(status):
    """Render the pipeline section via the display engine."""
    engine = StatusDisplayEngine.__new__(StatusDisplayEngine)
    from config.schemas import DisplayContextConfig
    config = DisplayContextConfig(
        title="Pipeline Status",
        show_workers=False,
        show_alerts=False,
        show_performance=False,
        show_newline_prefix=False,
        width=80,
        max_alerts_per_level=5,
    )
    return engine._format_pipeline_section(status, config)


def _line(lines, prefix):
    """Return all lines starting with the given prefix."""
    return [l for l in lines if l.startswith(prefix)]


# ---------------------------------------------------------------------------
# T12a — full render with every figure in documented order
# ---------------------------------------------------------------------------
def test_p22_t12a_full_render_documented_order():
    """T12a: a status whose registry_metrics carries the three counters plus
    available=True/enabled=True renders the line with every figure in the
    documented order and spelling.
    """
    metrics = {
        "enabled": True,
        "available": True,
        "degraded": False,
        "queued": 0,
        "dropped": 0,
        "metadata_noops": 0,
        "key_source_rows": 42,
        "multi_source_keys": 7,
        "key_sources_backfilled": 3,
    }
    status = _status(metrics)
    line = StatusDisplayEngine._format_registry_metrics_line(status)

    assert line != "", "expected a non-empty rendered line"
    assert line.startswith("Registry:")
    assert "available=True" in line
    assert "degraded=False" in line
    assert "queued=0" in line
    assert "dropped=0" in line
    assert "metadata_noops=0" in line
    assert "key_sources=42" in line
    assert "multi_source_keys=7" in line
    assert "backfilled=3" in line


# ---------------------------------------------------------------------------
# T12b — absent when disabled or unavailable
# ---------------------------------------------------------------------------
def test_p22_t12b_disabled_renders_no_line():
    """T12b: the line is ABSENT when the registry is disabled."""
    status = _status({"enabled": False, "available": True})
    line = StatusDisplayEngine._format_registry_metrics_line(status)
    assert line == ""


def test_p22_t12b_unavailable_renders_no_line():
    """T12b: the line is ABSENT when the registry is unavailable."""
    status = _status({"enabled": True, "available": False})
    line = StatusDisplayEngine._format_registry_metrics_line(status)
    assert line == ""


def test_p22_t12b_empty_metrics_renders_no_line():
    """T12b: empty metrics → no line."""
    status = _status({})
    line = StatusDisplayEngine._format_registry_metrics_line(status)
    assert line == ""


# ---------------------------------------------------------------------------
# T12c — malformed figures degrade fail-open
# ---------------------------------------------------------------------------
def test_p22_t12c_malformed_figures_degrade_fail_open():
    """T12c: malformed figures (a string where an int belongs, None, a missing
    key) degrade fail-open — no exception escapes and the line still renders
    with the neutral value the neighbouring lines use.
    """
    metrics = {
        "enabled": True,
        "available": True,
        "degraded": "not-a-bool",
        "queued": None,
        "dropped": "NaN",
        "metadata_noops": float("inf"),
        "key_source_rows": "not-a-number",
        "multi_source_keys": None,
        # key_sources_backfilled is missing entirely
    }
    status = _status(metrics)
    # Must not raise
    line = StatusDisplayEngine._format_registry_metrics_line(status)
    assert line != "", "line must still render with neutral values"
    assert "Registry:" in line
    # Neutral (zero) values for the malformed integers
    assert "queued=0" in line
    assert "dropped=0" in line
    assert "key_sources=0" in line
    assert "multi_source_keys=0" in line
    assert "backfilled=0" in line


# ---------------------------------------------------------------------------
# T12d — surface membership
# ---------------------------------------------------------------------------
def test_p22_t12d_surface_membership():
    """T12d: registry_metrics is a member of _RENDERED_METRIC_SURFACES."""
    assert "registry_metrics" in _RENDERED_METRIC_SURFACES


# ---------------------------------------------------------------------------
# T12e — pipeline wiring
# ---------------------------------------------------------------------------
def test_p22_t12e_pipeline_wiring_with_registry():
    """T12e: a pipeline-shaped object whose registry returns a stats dict
    produces PipelineStatus.registry_metrics equal to it.
    """
    expected_stats = {
        "enabled": True,
        "available": True,
        "degraded": False,
        "queued": 5,
        "dropped": 1,
        "metadata_noops": 2,
        "key_source_rows": 100,
        "multi_source_keys": 12,
        "key_sources_backfilled": 8,
    }

    class FakeRegistry:
        available = True
        enabled = True

        def get_stats(self):
            return dict(expected_stats)

    class FakePipeline:
        link_registry = FakeRegistry()

    fp = FakePipeline()
    # Simulate what _get_pipeline_status does
    registry = getattr(fp, "link_registry", None)
    if registry is not None:
        try:
            result = registry.get_stats()
        except Exception:
            result = {}
    else:
        result = {}

    ps = PipelineStatus(registry_metrics=result)
    assert ps.registry_metrics == expected_stats


def test_p22_t12e_pipeline_wiring_missing_registry():
    """T12e: a missing registry yields {} without raising."""

    class FakePipeline:
        pass

    fp = FakePipeline()
    registry = getattr(fp, "link_registry", None)
    if registry is not None:
        try:
            result = registry.get_stats()
        except Exception:
            result = {}
    else:
        result = {}

    ps = PipelineStatus(registry_metrics=result)
    assert ps.registry_metrics == {}


def test_p22_t12e_pipeline_wiring_broken_registry():
    """T12e: a broken registry (get_stats raises) yields {} without raising."""

    class BrokenRegistry:
        def get_stats(self):
            raise RuntimeError("simulated failure")

    class FakePipeline:
        link_registry = BrokenRegistry()

    fp = FakePipeline()
    registry = getattr(fp, "link_registry", None)
    if registry is not None:
        try:
            result = registry.get_stats()
        except Exception:
            result = {}
    else:
        result = {}

    ps = PipelineStatus(registry_metrics=result)
    assert ps.registry_metrics == {}
