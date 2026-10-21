"""F4: per-child budget-refusal WARNING collapsed to one aggregated line per parent.

Traceability: refine-fanout-governor S7 (budget exhaustion), spec requirement
"Per-run search-task budget with loud deterministic admission" (:44).

RED state note: ``_log_budget_refusals`` currently emits one WARNING per refused
child (``for query in refused: logger.warning(... child={query} ...)``).  At
clamp 46 656 a single starving root produces 46 k WARNING lines (measured
incident: 56 048 of 70 059 WARNING lines in the measured arm).  The fix
collapses to ONE aggregated WARNING per call with stable token
``budget_refusal:``, mirroring design D7 (``_log_wire_collapse``).

Pins:
- P1: large refusal batch -> exactly ONE warning with token ``budget_refusal:``
  and ``refused=<N>``; ZERO lines with ``child=``.
- P2: per-child counter ``refused_budget`` stays exact (metrics property).
- P3: two different parents in one run -> exactly two aggregated lines, each
  with its own ``query=``.
- P4: ``first=``/``last=`` match the actual first/last refused child in sorted
  (fingerprint) order.
- P5: shadow path (:265 would_refuse) also aggregated (one line, no per-child).
- P6: empty refusal -> zero lines (early exit).
"""

import logging

from search.querykey import fingerprint
from search.refine_governor import RefineGovernor


def _gov(mode="on", partitions=128, budget=10, depth=2):
    return RefineGovernor(
        mode=mode,
        max_refine_depth=depth,
        max_partitions_per_refine=partitions,
        max_search_tasks_per_run=budget,
    )


# Build a large set of children that will all be past the budget cutoff.
# We need >= 100 refused children for P1.
def _make_children(n=150):
    """Generate n distinct child queries with distinct wire fingerprints."""
    # Use patterns that produce distinct wire queries (different fixed literals).
    return [f"/sk-{i:04d}[a-z]{{28}}/" for i in range(n)]


CHILDREN = _make_children(150)


def _refusal_lines(caplog):
    return [r.getMessage() for r in caplog.records
            if "budget_refusal:" in r.getMessage()
            or "budget refusal" in r.getMessage()]


def _child_lines(caplog):
    return [r.getMessage() for r in caplog.records
            if "child=" in r.getMessage()]


# ---------------------------------------------------------------------------
# P1: large batch -> exactly ONE aggregated warning, ZERO per-child
# ---------------------------------------------------------------------------
def test_p1_large_batch_one_aggregated_warning(caplog):
    """With >=100 refused children, exactly ONE warning line with token
    budget_refusal: and refused=<N>; ZERO lines with child=."""
    gov = _gov(budget=5)  # only 5 admitted out of 128 (cap)

    with caplog.at_level(logging.WARNING):
        admitted = gov.select_children("prov", "/sk-parent/", list(CHILDREN), 1000, 100_000)

    # We should have some refusals
    m = gov.metrics
    assert m["refused_budget"] >= 100, f"expected >=100 refusals, got {m['refused_budget']}"

    refusal_lines = _refusal_lines(caplog)
    child_lines = _child_lines(caplog)

    assert len(refusal_lines) == 1, (
        f"expected exactly 1 aggregated warning, got {len(refusal_lines)}: {refusal_lines[:3]}"
    )
    assert len(child_lines) == 0, (
        f"expected 0 per-child lines, got {len(child_lines)}"
    )
    line = refusal_lines[0]
    assert "budget_refusal:" in line
    assert f"refused={m['refused_budget']}" in line


# ---------------------------------------------------------------------------
# P2: per-child counter stays exact
# ---------------------------------------------------------------------------
def test_p2_counter_per_child_exact(caplog):
    """metrics['refused_budget'] == actual number of refused children."""
    gov = _gov(budget=3)

    with caplog.at_level(logging.WARNING):
        admitted = gov.select_children("prov", "/sk-parent/", list(CHILDREN[:20]), 1000, 50_000)

    m = gov.metrics
    # admitted + refused should equal min(cap, len(children))
    total_after_cap = min(128, 20)  # cap=128, 20 children
    assert m["children_admitted"] + m["refused_budget"] == total_after_cap
    assert m["refused_budget"] == total_after_cap - m["children_admitted"]


# ---------------------------------------------------------------------------
# P3: two different parents -> two aggregated lines
# ---------------------------------------------------------------------------
def test_p3_two_parents_two_lines(caplog):
    """Two different parents in one run -> exactly two aggregated lines,
    each with its own query=."""
    gov = _gov(budget=3)

    with caplog.at_level(logging.WARNING):
        gov.select_children("prov", "/sk-parent-A/", list(CHILDREN[:20]), 1000, 50_000)
        gov.select_children("prov", "/sk-parent-B/", list(CHILDREN[20:40]), 1000, 50_000)

    refusal_lines = _refusal_lines(caplog)
    assert len(refusal_lines) == 2, (
        f"expected 2 aggregated lines (one per parent), got {len(refusal_lines)}"
    )
    # Each line mentions its parent
    assert any("query=/sk-parent-A/" in l for l in refusal_lines)
    assert any("query=/sk-parent-B/" in l for l in refusal_lines)


# ---------------------------------------------------------------------------
# P4: first=/last= match sorted order
# ---------------------------------------------------------------------------
def test_p4_first_last_match_sorted_order(caplog):
    """first= and last= in the aggregated line match the actual first and last
    refused child in fingerprint-sorted order."""
    gov = _gov(budget=2, partitions=10)

    small = CHILDREN[:10]
    with caplog.at_level(logging.WARNING):
        admitted = gov.select_children("prov", "/sk-parent/", list(small), 1000, 50_000)

    # Compute expected refused in sorted order
    ordered = sorted(small, key=lambda q: (fingerprint(q, True), q))
    expected_refused = ordered[2:]  # budget=2, so first 2 admitted

    refusal_lines = _refusal_lines(caplog)
    assert len(refusal_lines) == 1
    line = refusal_lines[0]

    assert f"first={expected_refused[0]}" in line, (
        f"expected first={expected_refused[0]} in line: {line}"
    )
    assert f"last={expected_refused[-1]}" in line, (
        f"expected last={expected_refused[-1]} in line: {line}"
    )


# ---------------------------------------------------------------------------
# P5: shadow path also aggregated
# ---------------------------------------------------------------------------
def test_p5_shadow_path_aggregated(caplog):
    """Shadow path (:265 would_refuse) also produces one aggregated line,
    no per-child lines."""
    gov = _gov(mode="shadow", budget=3)

    with caplog.at_level(logging.WARNING):
        gov.select_children("prov", "/sk-parent/", list(CHILDREN[:20]), 1000, 50_000)

    refusal_lines = _refusal_lines(caplog)
    child_lines = _child_lines(caplog)

    # Shadow path should also have exactly 1 aggregated line
    assert len(refusal_lines) == 1, (
        f"shadow: expected 1 aggregated warning, got {len(refusal_lines)}"
    )
    assert len(child_lines) == 0, (
        f"shadow: expected 0 per-child lines, got {len(child_lines)}"
    )


# ---------------------------------------------------------------------------
# P6: empty refusal -> zero lines (early exit)
# ---------------------------------------------------------------------------
def test_p6_empty_refusal_zero_lines(caplog):
    """When no children are refused, zero warning lines emitted."""
    gov = _gov(budget=1000)  # large budget, nothing refused

    with caplog.at_level(logging.WARNING):
        gov.select_children("prov", "/sk-parent/", list(CHILDREN[:5]), 1000, 50_000)

    refusal_lines = _refusal_lines(caplog)
    assert len(refusal_lines) == 0, (
        f"expected 0 refusal lines when nothing refused, got {len(refusal_lines)}"
    )
