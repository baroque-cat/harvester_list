"""RefineGovernor unit contract (add-refine-fanout-governor).

Traceability: refine-fanout-governor-S4 (partial: clamp math), S6, S7, S9,
S10 (unit level), S11 (unit level), S12, S13, S15 (arithmetic), plus the
depth-gate unit half of S1/S2 (may_refine).

RED state note: ``search.refine_governor`` does not exist yet - the import
failure IS the expected RED state. The driven API contract (design D1-D9):

    RefineGovernor(mode, max_refine_depth, max_partitions_per_refine,
                   max_search_tasks_per_run)
    .may_refine(depth) -> bool          # False only in 'on' at depth cap
    .clamp_partitions(raw) -> int       # identity in 'off'
    .select_children(provider, parent_query, queries, transport_limit, total,
                     use_api=True) -> List[str]   # admitted children (below)
    .record_depth_cap_pagination()      # counter for the fallthrough path
    .metrics -> Dict[str, Any]          # snapshot incl. coverage aggregates

Mode semantics: off = passthrough, input order preserved, counters inert;
shadow = passthrough enqueue BUT every would-be refusal counted/logged with
the same deterministic order 'on' would apply; on = enforce.
"""

import logging

import pytest

from search.querykey import fingerprint
from search.refine_governor import RefineGovernor  # RED driver: module absent


def _gov(mode="on", depth=2, partitions=3, budget=100):
    return RefineGovernor(
        mode=mode,
        max_refine_depth=depth,
        max_partitions_per_refine=partitions,
        max_search_tasks_per_run=budget,
    )


QUERIES = ["/sk-c[d-e][a-z]{30}/", "/sk-a[b-c][a-z]{30}/", "/sk-b[c-d][a-z]{30}/",
           "/sk-d[e-f][a-z]{30}/", "/sk-e[f-g][a-z]{30}/"]

# Admission order is ascending wire fingerprint (design D5 / spec requirement 2),
# NOT lexicographic: fingerprint order spreads the truncation gaps over the query
# space instead of permanently sacrificing one fixed region (lexicographic always
# drops the alphabet tail).  The transport is part of the hash, so API and web
# twins of the same query legitimately order differently.
ADMITTED_API = sorted(QUERIES, key=lambda q: fingerprint(q, True))[:3]
ADMITTED_WEB = sorted(QUERIES, key=lambda q: fingerprint(q, False))[:3]


# ---------------------------------------------------------------------------
# S12/S4: on-mode clamps and sorts
# ---------------------------------------------------------------------------
def test_on_select_children_caps_and_sorts():
    gov = _gov("on", partitions=3)
    admitted = gov.select_children("prov", "/sk-x/", list(QUERIES), 1000, 46_000_000)

    assert admitted == ADMITTED_API             # wire-fingerprint order (D5)
    assert admitted != sorted(QUERIES)[:3]      # ... which is not lexicographic
    web = _gov("on", partitions=3).select_children(
        "prov", "/sk-x/", list(QUERIES), 1000, 46_000_000, use_api=False
    )
    assert web == ADMITTED_WEB                  # transport is part of the hash
    m = gov.metrics
    assert m["children_generated"] == 5
    assert m["children_admitted"] == 3
    assert m["truncated_to_cap"] == 2


def test_clamp_partitions_math():
    gov = _gov("on", partitions=128)
    assert gov.clamp_partitions(46_007) == 128
    assert gov.clamp_partitions(5) == 5
    assert _gov("off").clamp_partitions(46_007) == 46_007  # off: identity


# ---------------------------------------------------------------------------
# S6: determinism regardless of generator ordering
# ---------------------------------------------------------------------------
def test_identical_inputs_admit_identical_children():
    import random

    a = _gov("on", partitions=3)
    b = _gov("on", partitions=3)
    shuffled = list(QUERIES)
    random.Random(42).shuffle(shuffled)

    assert a.select_children("p", "/sk-x/", list(QUERIES), 1000, 9_000) == \
           b.select_children("p", "/sk-x/", shuffled, 1000, 9_000)


# ---------------------------------------------------------------------------
# S7: budget exhaustion refuses loudly and deterministically
# ---------------------------------------------------------------------------
def test_budget_exhaustion_refuses_loudly(caplog):
    gov = _gov("on", partitions=10, budget=4)

    with caplog.at_level(logging.WARNING):
        first = gov.select_children("prov-a", "/sk-p1/", QUERIES[:3], 1000, 5_000)
        second = gov.select_children("prov-a", "/sk-p2/", QUERIES[3:] + QUERIES[:1], 1000, 5_000)

    assert len(first) == 3                      # budget 4 -> 3 admitted, 1 left
    assert len(second) == 1                     # only the remaining slot
    m = gov.metrics
    assert m["children_admitted"] == 4
    assert m["refused_budget"] == 2             # 2 of the second batch refused
    assert m["budget_remaining"] == 0
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("prov-a" in w and "budget" in w.lower() for w in warnings)


# ---------------------------------------------------------------------------
# S9: fresh instance resets the budget
# ---------------------------------------------------------------------------
def test_fresh_instance_resets_budget():
    spent = _gov("on", partitions=10, budget=2)
    spent.select_children("p", "/q/", QUERIES, 1000, 9_000)
    assert spent.metrics["budget_remaining"] == 0

    fresh = _gov("on", partitions=10, budget=2)
    assert fresh.metrics["budget_remaining"] == 2


# ---------------------------------------------------------------------------
# S10: off is passthrough with input order and inert counters
# ---------------------------------------------------------------------------
def test_off_passthrough_preserves_input_order():
    gov = _gov("off", partitions=2, budget=1)
    admitted = gov.select_children("p", "/q/", list(QUERIES), 1000, 46_000_000)

    assert admitted == QUERIES                  # pre-change: generator order kept
    m = gov.metrics
    assert m["children_admitted"] == 0 or m.get("mode") == "off"
    assert m["refused_budget"] == 0 and m["truncated_to_cap"] == 0 and m["refused_depth"] == 0


# ---------------------------------------------------------------------------
# (aux S10) off keeps the stage's own empty/self filters authoritative
# ---------------------------------------------------------------------------
def test_off_leaves_stage_filters_authoritative():
    """`off` must be byte-equivalent in *logs* too, not just in enqueued tasks.

    Pre-change, SearchStage itself skipped empty and self-referential generator
    output with a WARNING.  If `off` pre-filtered them inside the governor those
    warnings would never fire, so rollback would not reproduce the old behavior.
    `shadow`/`on` DO filter, because their counters must describe exactly what
    would be enqueued.
    """
    noisy = ["", "/sk-parent/", "/sk-a[b-c][a-z]{30}/"]

    off = _gov("off", partitions=10).select_children("p", "/sk-parent/", list(noisy), 1000, 9_000)
    on = _gov("on", partitions=10).select_children("p", "/sk-parent/", list(noisy), 1000, 9_000)

    assert off == noisy                        # verbatim: stage filters still fire
    assert on == ["/sk-a[b-c][a-z]{30}/"]      # counters see only real candidates


# ---------------------------------------------------------------------------
# S11: shadow enqueues everything but counts would-be refusals like on
# ---------------------------------------------------------------------------
def test_shadow_counts_without_enforcing():
    shadow = _gov("shadow", partitions=3, budget=100)
    on = _gov("on", partitions=3, budget=100)

    got_shadow = shadow.select_children("p", "/q/", list(QUERIES), 1000, 46_000_000)
    got_on = on.select_children("p", "/q/", list(QUERIES), 1000, 46_000_000)

    assert got_shadow == QUERIES                # nothing withheld
    assert got_on == ADMITTED_API               # same fingerprint order as 'on'
    sm = shadow.metrics
    assert sm["truncated_to_cap"] == on.metrics["truncated_to_cap"] == 2  # same decisions
    assert sm["children_admitted"] == 0         # enforcement counters stay clean


# ---------------------------------------------------------------------------
# S13: rollback flip changes behavior only via mode
# ---------------------------------------------------------------------------
def test_mode_flip_is_the_only_difference():
    enforced = _gov("on", partitions=2).select_children("p", "/q/", QUERIES, 1000, 9_000)
    released = _gov("off", partitions=2).select_children("p", "/q/", QUERIES, 1000, 9_000)

    assert len(enforced) == 2
    assert released == QUERIES


# ---------------------------------------------------------------------------
# Depth gate (unit half of S1/S2)
# ---------------------------------------------------------------------------
def test_may_refine_depth_gate():
    on = _gov("on", depth=2)
    assert on.may_refine(0) is True
    assert on.may_refine(1) is True
    assert on.may_refine(2) is False
    assert on.metrics["refused_depth"] == 1

    shadow = _gov("shadow", depth=2)
    assert shadow.may_refine(2) is True         # measures, does not enforce
    assert shadow.metrics["refused_depth"] == 1  # would-be refusal counted

    off = _gov("off", depth=2)
    assert off.may_refine(5) is True
    assert off.metrics["refused_depth"] == 0


# ---------------------------------------------------------------------------
# S15: coverage estimate arithmetic
# ---------------------------------------------------------------------------
def test_coverage_estimate_is_honest_arithmetic():
    gov = _gov("on", partitions=3)
    gov.select_children("p", "/q/", QUERIES, 1000, 46_000_000)

    m = gov.metrics
    expected = min(1.0, 3 * 1000 / 46_000_000)
    assert m["coverage_estimate_min"] == pytest.approx(expected)
    assert m["coverage_estimate_avg"] == pytest.approx(expected)


# ---------------------------------------------------------------------------
# (aux S11) Shadow must simulate the would-be budget trajectory across batches
# so the measured refusals match what 'on' would enforce.
# ---------------------------------------------------------------------------
def test_shadow_budget_drain_is_measured():
    shadow = _gov("shadow", partitions=10, budget=4)
    on = _gov("on", partitions=10, budget=4)

    s1 = shadow.select_children("p", "/q/", QUERIES[:3], 1000, 5_000)
    o1 = on.select_children("p", "/q/", QUERIES[:3], 1000, 5_000)
    s2 = shadow.select_children("p", "/q/", QUERIES[3:] + QUERIES[:1], 1000, 5_000)
    o2 = on.select_children("p", "/q/", QUERIES[3:] + QUERIES[:1], 1000, 5_000)

    assert s1 == QUERIES[:3] and s2 == QUERIES[3:] + QUERIES[:1]  # nothing withheld
    assert len(o1) == 3 and len(o2) == 1

    sm, om = shadow.metrics, on.metrics
    assert sm["refused_budget"] == om["refused_budget"] == 2   # same would-be decisions
    assert sm["budget_remaining"] == om["budget_remaining"] == 0
    assert sm["parents_refined"] == om["parents_refined"] == 2
    assert sm["children_admitted"] == 0                        # enforcement counters clean
    assert om["children_admitted"] == 4
