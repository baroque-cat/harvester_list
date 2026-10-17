"""P15 — aggregation KPI published as an explicit derived key, PAIRED with the
distinct-work measure, plus the honest verdict on ``coverage_estimate``.

Traceability: plan.md II.4 (R3 reduced to a KPI), II.19 P15, lessons of II.16.2
and the R11 gate (ratio 49.9 for the wasteful arm vs 3.0 for the enforcing one;
307.6 vs 6.4 in the depth A/B): **an aggregation ratio published without a
measure of distinguishable work rewards waste**.  The remedy shipped here:

1. ``AggregationMetrics.to_dict()`` derives ``aggregation_ratio =
   (hits + misses + joins) / misses`` — pure arithmetic over counters already
   published (no new collection), ``0.0`` when ``misses == 0`` (no real HTTP
   yet — a legitimate zero, not an error).
2. The ``Aggregation:`` status line renders ``ratio=N.NN`` **on the same line
   as ``misses=``** (its denominator — the count of real HTTP requests), and
   the adjacent ``Refine:`` line carries ``uniq_wires=`` (distinct admitted
   wire queries).  The pair is the contract; ratio alone is not.
3. ``coverage_estimate`` keeps its pinned honest arithmetic but gains an
   explicit ``(unfit-denominator)`` marker in the render: per II.5.2 neither
   numerator nor denominator relates to the key population (the denominator is
   inflated by the degenerate literal wire query), so the value must never be
   read as coverage.  It is retained, not removed, because spec scenarios and
   ``test_coverage_estimate_is_honest_arithmetic`` pin the arithmetic.

RED at plan time (additive change — reproduces on HEAD, no worktree needed):
- K1/K2/K3/K10 fail: ``to_dict()`` has no ``aggregation_ratio`` key.
- K4/K5/K7 fail: rendered ``Aggregation:`` line has no ``ratio=`` token.
- K6 fails: rendered ``Refine:`` line has no ``(unfit-denominator)`` marker.
- K8/K9 PASS PREMATURELY and are kept as regression guards (R16 precedent:
  determinism and empty-surface behaviour must survive the addition).

Harness mirrors ``tests/test_tdm_render.py`` (RO-S1..S3 style): the section
formatter RETURNS lines, so contracts are asserted on return values.
"""

import math

import pytest

from core.metrics import PipelineStatus, StageMetrics
from search.aggregation import AggregationMetrics
from state.display import StatusDisplayEngine, get_display_config
from state.enums import DisplayMode, StatusContext
from state.models import SystemStatus

AGG = {
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
    "refused_wire_collapse": 16,
    "truncated_to_cap": 16,
    "distinct_wire_admitted": 144,
    "parents_refined": 4,
    "parents_at_depth_cap_paginated": 0,
    "budget_remaining": 9872,
    "coverage_estimate_min": 0.0027,
    "coverage_estimate_avg": 0.0746,
}


def _status(**surfaces):
    surfaces.setdefault("stages", {"gather": StageMetrics(name="gather", workers=8)})
    return SystemStatus(pipeline=PipelineStatus(**surfaces))


def _config(mode=DisplayMode.DETAILED):
    return get_display_config(StatusContext.APPLICATION, mode)


def _section(status, mode=DisplayMode.DETAILED):
    return StatusDisplayEngine()._format_pipeline_section(status, _config(mode))


def _line(lines, prefix):
    return [ln for ln in lines if ln.startswith(prefix)]


def _metrics(hits=0, misses=0, joins=0):
    m = AggregationMetrics()
    m.hits, m.misses, m.joins = hits, misses, joins
    return m


# ---------------------------------------------------------------------------
# K1 — derived key exists and is the documented arithmetic
# ---------------------------------------------------------------------------
def test_k1_to_dict_exposes_aggregation_ratio():
    """Live-gate figures (P25 run1): hits=111 misses=37 joins=0 -> ratio 148/37."""
    d = _metrics(hits=111, misses=37, joins=0).to_dict()
    assert "aggregation_ratio" in d
    assert d["aggregation_ratio"] == pytest.approx(148 / 37)


# ---------------------------------------------------------------------------
# K2 — legitimate zero: misses == 0 must not raise and yields 0.0
# ---------------------------------------------------------------------------
def test_k2_zero_misses_yields_zero_ratio():
    d = _metrics(hits=7, misses=0, joins=3).to_dict()
    assert d["aggregation_ratio"] == 0.0


# ---------------------------------------------------------------------------
# K3 — honest arithmetic property (mirrors test_coverage_estimate_is_honest_
#      arithmetic): ratio * misses reconstructs the call total exactly
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "hits,misses,joins",
    [(0, 1, 0), (5, 2, 1), (111, 37, 0), (114, 38, 0), (1000, 3, 17), (1, 1, 1)],
)
def test_k3_ratio_times_misses_equals_call_total(hits, misses, joins):
    d = _metrics(hits=hits, misses=misses, joins=joins).to_dict()
    assert d["aggregation_ratio"] * misses == pytest.approx(hits + misses + joins)


# ---------------------------------------------------------------------------
# K4 — renderer publishes the token, legacy fragments untouched
# ---------------------------------------------------------------------------
def test_k4_render_carries_ratio_token():
    agg = dict(AGG)
    agg["aggregation_ratio"] = (5 + 2 + 1) / 2  # 4.0
    lines = _section(_status(aggregation_metrics=agg))
    a = _line(lines, "Aggregation:")
    assert len(a) == 1
    line = a[0]
    assert "ratio=4.00" in line
    for fragment in ("hits=5", "misses=2", "joins=1", "entries=6", "bytes=1234", "pairs=4"):
        assert fragment in line, f"{fragment!r} missing from {line!r}"


# ---------------------------------------------------------------------------
# K5 — renderer fail-open: absent or garbage ratio degrades to 0.00, never raises
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [None, "abc", float("nan"), float("inf"), -2.5])
def test_k5_render_fail_open_on_garbage_ratio(bad):
    agg = dict(AGG)
    if bad is not None:
        agg["aggregation_ratio"] = bad
    lines = _section(_status(aggregation_metrics=agg))
    a = _line(lines, "Aggregation:")
    assert len(a) == 1
    assert "ratio=0.00" in a[0]


# ---------------------------------------------------------------------------
# K6 — coverage_estimate keeps its arithmetic but is explicitly marked unfit
# ---------------------------------------------------------------------------
def test_k6_refine_line_marks_coverage_estimate_unfit():
    lines = _section(_status(refine_metrics=dict(REFINE)))
    r = _line(lines, "Refine:")
    assert len(r) == 1
    line = r[0]
    assert line.count("(unfit-denominator)") == 1
    # legacy tokens preserved byte-stable, marker strictly AFTER the cov block
    assert "cov[" in line
    assert "min=0.0027" in line and "avg=0.0746]" in line
    assert line.index("cov[") < line.index("(unfit-denominator)")
    assert line.rstrip().endswith("(unfit-denominator)")


# ---------------------------------------------------------------------------
# K7 — the pairing contract: ratio on the same line as its denominator, and
#      uniq_wires present in the SAME status block (II.4: ratio alone is harmful)
# ---------------------------------------------------------------------------
def test_k7_ratio_is_published_paired_with_distinct_work():
    agg = dict(AGG)
    agg["aggregation_ratio"] = 4.0
    lines = _section(_status(aggregation_metrics=agg, refine_metrics=dict(REFINE)))
    a = _line(lines, "Aggregation:")
    r = _line(lines, "Refine:")
    assert len(a) == 1 and len(r) == 1
    assert "misses=2" in a[0] and "ratio=4.00" in a[0]
    assert a[0].index("misses=") < a[0].index("ratio="), "denominator must precede the ratio"
    assert "uniq_wires=144" in r[0]


# ---------------------------------------------------------------------------
# K8 — determinism survives the addition (PASSES PREMATURELY — regression guard)
# ---------------------------------------------------------------------------
def test_k8_rendering_deterministic_with_new_tokens():
    agg = dict(AGG)
    agg["aggregation_ratio"] = 4.0
    status = _status(aggregation_metrics=agg, refine_metrics=dict(REFINE))
    first = _section(status)
    second = _section(status)
    assert first == second


# ---------------------------------------------------------------------------
# K9 — empty surfaces still render nothing (PASSES PREMATURELY — guard, RO-S2)
# ---------------------------------------------------------------------------
def test_k9_empty_surfaces_render_nothing():
    empty = _section(_status(aggregation_metrics={}, refine_metrics={}))
    assert _line(empty, "Aggregation:") == []
    assert _line(empty, "Refine:") == []


# ---------------------------------------------------------------------------
# K10 — extension, not weakening: every legacy key survives next to the new one
# ---------------------------------------------------------------------------
def test_k10_legacy_keys_preserved():
    d = _metrics(hits=1, misses=1, joins=0).to_dict()
    for key in AGG:  # the full pre-P15 surface
        assert key in d, f"legacy key {key!r} lost from to_dict()"
    assert "aggregation_ratio" in d
    assert not math.isnan(d["aggregation_ratio"])
