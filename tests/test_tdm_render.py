"""Operator rendering of collected run-metric surfaces.

Traceability: run-observability-RO-S1 ... RO-S8, RO-S11.

The three surfaces this change renders - ``gather_transport_metrics``,
``aggregation_metrics`` and ``refine_metrics`` - are already collected and
published on ``PipelineStatus`` (``manager/pipeline.py:490-495``) but have no
consumer anywhere in the codebase, which is why the live gate had to record
acceptance rows A1/A2 as NOT MEASURED and why shipped requirement
gather-transport S21 ("status reporting exposes ...") was unmet for operators.

Harness: ``StatusDisplayEngine._format_pipeline_section`` RETURNS its lines, so
the contract is asserted on return values rather than on captured logs; only the
compact-mode pin needs ``caplog``.  Rendered labels are part of the contract
because acceptance rows are read off them:

    Gather: raw req=12 bytes=3456 p50=250ms p99=750ms defer[rl=0,sec=0,cred=0,budget=3] ...
    Aggregation: hits=5 misses=2 joins=1 ...
    Refine: mode=on gen=144 adm=128 refused[depth=0,budget=0] ...

RED at plan time: ``state/display.py`` has no formatter for any of the three
surfaces and declares no rendered-surface registry, so the attribute lookups
raise ``AttributeError`` and the section contains no such line.  There are
currently no display tests in the suite; this file is the first.
"""

import dataclasses
import logging
import re

import pytest

from core.metrics import PipelineStatus, StageMetrics
from state.display import StatusDisplayEngine, get_display_config
from state.enums import DisplayMode, StatusContext
from state.models import SystemStatus

GATHER = {
    "requests_raw": 12,
    "bytes_raw": 3456,
    "latency_samples_raw": 12,
    "latency_p50_ms_raw": 250,
    "latency_p99_ms_raw": 750,
    "requests_html": 0,
    "bytes_html": 0,
    "deferred_rate_limit": 1,
    "deferred_secondary": 0,
    "deferred_credentials": 0,
    "deferred_local_budget": 3,
    "dropped_auth": 0,
    "dropped_not_found": 2,
    "head_fallback": 1,
    "truncated": 0,
    "unparseable_link": 0,
}

AGGREGATION = {
    "hits": 5,
    "misses": 2,
    "joins": 1,
    "join_timeouts": 0,
    "evictions": 0,
    "entries": 6,
    "bytes": 1234,
    "poisoned_rejected": 0,
    "shadow_comparisons": 0,
    "shadow_warnings": 0,
    "shadow_jaccard_min": 0.0,
    "shadow_jaccard_p95": 0.0,
    "aggregatable_pairs": 4,
}

REFINE = {
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
}


def _status(**surfaces):
    """A realistic run status: one stage row plus the metric surfaces under test.

    The stage row matters - ``_format_pipeline_section`` takes an early-return
    branch when ``pipeline.stages`` is empty, and a real run always has stages.
    """
    surfaces.setdefault("stages", {"gather": StageMetrics(name="gather", workers=8)})
    return SystemStatus(pipeline=PipelineStatus(**surfaces))


def _config(mode=DisplayMode.DETAILED):
    return get_display_config(StatusContext.APPLICATION, mode)


def _section(status, mode=DisplayMode.DETAILED):
    return StatusDisplayEngine()._format_pipeline_section(status, _config(mode))


def _line(lines, prefix):
    return [ln for ln in lines if ln.startswith(prefix)]


# ---------------------------------------------------------------------------
# run-observability-RO-S1
# ---------------------------------------------------------------------------
def test_ro_s1_collected_surfaces_are_rendered():
    """WHEN a run finishes having collected transport, aggregation and refinement
    figures THEN detailed status output contains one line per non-empty surface
    naming its principal figures.
    """
    lines = _section(
        _status(
            gather_transport_metrics=dict(GATHER),
            aggregation_metrics=dict(AGGREGATION),
            refine_metrics=dict(REFINE),
        )
    )

    gather = _line(lines, "Gather:")
    aggregation = _line(lines, "Aggregation:")
    refine = _line(lines, "Refine:")
    assert len(gather) == 1 and len(aggregation) == 1 and len(refine) == 1, lines

    g = gather[0]
    for fragment in ("req=12", "bytes=3456", "p50=250ms", "p99=750ms", "budget=3", "rl=1", "404=2", "head=1"):
        assert fragment in g, f"{fragment!r} missing from {g!r}"

    a = aggregation[0]
    for fragment in ("hits=5", "misses=2", "joins=1", "entries=6", "bytes=1234", "pairs=4"):
        assert fragment in a, f"{fragment!r} missing from {a!r}"

    r = refine[0]
    for fragment in ("mode=on", "gen=144", "adm=128", "depth=0", "budget=0", "trunc=16", "cov["):
        assert fragment in r, f"{fragment!r} missing from {r!r}"


# ---------------------------------------------------------------------------
# run-observability-RO-S2
# ---------------------------------------------------------------------------
def test_ro_s2_empty_or_absent_surface_renders_nothing():
    """WHEN a metric surface is empty, or is not present on the status object at
    all THEN its line is omitted entirely, no placeholder is printed, and
    rendering does not raise.
    """
    empty = _section(_status(gather_transport_metrics={}, aggregation_metrics={}, refine_metrics={}))
    assert _line(empty, "Gather:") == []
    assert _line(empty, "Aggregation:") == []
    assert _line(empty, "Refine:") == []

    class _BarePipeline:
        stages = {}  # and no *_metrics attribute at all

    class _NoSurfaces:
        pipeline = _BarePipeline()

    absent = StatusDisplayEngine()._format_pipeline_section(_NoSurfaces(), _config())
    assert isinstance(absent, list)
    assert _line(absent, "Gather:") == []


# ---------------------------------------------------------------------------
# run-observability-RO-S3
# ---------------------------------------------------------------------------
def test_ro_s3_rendering_is_deterministic():
    """WHEN the same run state is rendered twice THEN both renderings are
    identical in labels, field order and values, so output can be diffed between
    runs.
    """
    status = _status(
        gather_transport_metrics=dict(GATHER),
        aggregation_metrics=dict(AGGREGATION),
        refine_metrics=dict(REFINE),
    )
    first = _section(status)
    second = _section(status)

    # non-vacuous: the surfaces under test are actually rendered
    for prefix in ("Gather:", "Aggregation:", "Refine:"):
        assert len(_line(first, prefix)) == 1, first

    assert first == second
    assert first is not second


# ---------------------------------------------------------------------------
# run-observability-RO-S4
# ---------------------------------------------------------------------------
def test_ro_s4_every_published_surface_has_a_renderer():
    """WHEN the run-metric surfaces published on the status object are enumerated
    THEN every one of them is covered by a rendering path, so a newly published
    surface cannot be silently invisible - the defect this capability exists to
    prevent.
    """
    from state import display as display_module

    published = {
        f.name
        for f in dataclasses.fields(PipelineStatus)
        if f.name.endswith("_metrics") or f.name == "credential_metrics"
    }
    assert published, "expected PipelineStatus to publish metric surfaces"

    rendered = set(display_module._RENDERED_METRIC_SURFACES)
    missing = published - rendered
    assert not missing, f"published metric surfaces with no rendering path: {sorted(missing)}"
    unknown = rendered - published
    assert not unknown, f"registry names surfaces that PipelineStatus does not publish: {sorted(unknown)}"


# ---------------------------------------------------------------------------
# run-observability-RO-S5
# ---------------------------------------------------------------------------
def test_ro_s5_unexpected_key_does_not_reach_output():
    """WHEN a producer adds a key to a rendered surface that the renderer does
    not declare THEN the rendered line is unchanged, so operator output cannot be
    widened silently by an upstream change (design D8: allowlist, never
    dict.items()).
    """
    baseline = _section(_status(gather_transport_metrics=dict(GATHER)))
    assert len(_line(baseline, "Gather:")) == 1, baseline  # non-vacuous: a line exists to compare

    widened = dict(GATHER)
    widened["brand_new_internal_key"] = "SURPRISE"
    widened["another_one"] = 12345
    after = _section(_status(gather_transport_metrics=widened))

    assert _line(after, "Gather:") == _line(baseline, "Gather:")
    assert "SURPRISE" not in "\n".join(after)
    assert "12345" not in "\n".join(after)
    assert "brand_new_internal_key" not in "\n".join(after)


# ---------------------------------------------------------------------------
# run-observability-RO-S6
# ---------------------------------------------------------------------------
def test_ro_s6_rendered_output_carries_no_credential_material():
    """WHEN any metric line is rendered after a run that used pooled credentials
    THEN the line contains no token, cookie or authorization value in any form,
    and no path or payload fragment - including when such a value is present in
    the surface under any key name.
    """
    # The canary is assembled from pieces so that no token-shaped literal ever
    # lands in tracked source: `git grep` for a real credential prefix must stay
    # empty (plan.md I.7 audit invariant), while the assertions below still prove
    # that neither the value nor its prefix can reach operator output.
    prefix = "gh" + "p_"
    secret = prefix + "SecretValueThatMustNeverBeRendered123456"
    leaky_gather = dict(GATHER)
    leaky_gather.update(
        {
            "credential": secret,
            "token": secret,
            "cookie": "user_session=" + secret,
            "path": "/var/tmp/opencode-cl-gate/soak3/data/providers/x/shard.ndjson",
            "payload": "APP_KEY=" + secret,
        }
    )
    leaky_refine = dict(REFINE)
    leaky_refine["token"] = secret

    lines = _section(
        _status(
            gather_transport_metrics=leaky_gather,
            aggregation_metrics=dict(AGGREGATION),
            refine_metrics=leaky_refine,
        )
    )
    rendered = "\n".join(lines)

    # non-vacuous: the surfaces did render, so their absence below is meaningful
    assert len(_line(lines, "Gather:")) == 1 and len(_line(lines, "Refine:")) == 1, lines

    assert secret not in rendered
    assert prefix not in rendered
    assert "user_session" not in rendered
    assert "/var/tmp" not in rendered
    assert "APP_KEY=" not in rendered


# ---------------------------------------------------------------------------
# run-observability-RO-S7
# ---------------------------------------------------------------------------
def test_ro_s7_malformed_figure_degrades_instead_of_raising():
    """WHEN a rendered surface holds a missing, non-numeric or out-of-range value
    for a declared figure THEN that field degrades (omitted or zero), the rest of
    the line still renders, and no exception escapes into the status or shutdown
    path (fail-open, per the project's parser doctrine).
    """
    broken = dict(GATHER)
    broken.update(
        {
            "requests_raw": None,
            "bytes_raw": "not-a-number",
            "latency_p50_ms_raw": float("nan"),
            "latency_p99_ms_raw": float("inf"),
            "deferred_local_budget": -5,
        }
    )
    del broken["dropped_not_found"]

    incomplete_refine = {"mode": "on"}  # every other figure absent

    lines = _section(
        _status(gather_transport_metrics=broken, refine_metrics=incomplete_refine)
    )

    gather = _line(lines, "Gather:")
    assert len(gather) == 1, lines
    assert "nan" not in gather[0].lower()
    assert "inf" not in gather[0].lower()
    assert "not-a-number" not in gather[0]
    refine = _line(lines, "Refine:")
    assert len(refine) == 1 and "mode=on" in refine[0]


# ---------------------------------------------------------------------------
# run-observability-RO-S8
# ---------------------------------------------------------------------------
def test_ro_s8_compact_output_is_unchanged(caplog):
    """WHEN status is rendered in compact mode rather than detailed THEN the added
    metric lines do not appear, so periodic compact output keeps its existing
    shape and width.
    """
    status = _status(
        gather_transport_metrics=dict(GATHER),
        aggregation_metrics=dict(AGGREGATION),
        refine_metrics=dict(REFINE),
    )
    engine = StatusDisplayEngine()

    with caplog.at_level(logging.INFO, logger="state"):
        engine._render_compact(status, _config(DisplayMode.COMPACT))

    rendered = "\n".join(rec.getMessage() for rec in caplog.records)
    for label in ("Gather:", "Aggregation:", "Refine:"):
        assert label not in rendered, f"{label} leaked into compact output"

    # non-vacuous contrast: the SAME status does render them in detailed mode
    detailed = "\n".join(_section(status))
    for label in ("Gather:", "Aggregation:", "Refine:"):
        assert label in detailed, f"{label} missing from detailed output"


# ---------------------------------------------------------------------------
# run-observability-RO-S11
# ---------------------------------------------------------------------------
def test_ro_s11_mean_bytes_per_file_is_computable():
    """WHEN a run gathered files over one transport THEN the published byte total
    and request count for that transport allow the mean bytes per fetched file to
    be computed from status output alone - no log grepping, no inspection of
    harvested data.  This is acceptance row A1.
    """
    lines = _section(_status(gather_transport_metrics=dict(GATHER)))
    gather = _line(lines, "Gather:")[0]

    match = re.search(r"raw req=(\d+) bytes=(\d+)", gather)
    assert match, f"per-transport request and byte totals must be parseable from {gather!r}"
    requests, delivered = int(match.group(1)), int(match.group(2))

    assert requests == 12 and delivered == 3456
    mean_bytes_per_file = delivered / requests
    assert mean_bytes_per_file == pytest.approx(288.0)
