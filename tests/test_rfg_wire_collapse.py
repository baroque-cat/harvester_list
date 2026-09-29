"""RefineGovernor wire-collapse guard (fix-refine-fanout-wire-collapse).

Traceability: refine-fanout-governor-S19..S28 and S30 (see the change's
``tests.md``).  S29 is the live two-arm gate; S31 lives in
``tests/test_rfg_pipeline_wiring.py``; S32/S33 in ``tests/test_tdm_render.py``.

RED state note: ``search.refine_governor`` exists, but the guard, the
``drop_wire_indistinguishable`` keyword and the two new metric keys
(``refused_wire_collapse``, ``distinct_wire_admitted``) do not.  Tests that need
the flag off pass it explicitly and therefore fail with ``TypeError``; every
other test relies on the default and fails on an assertion or a ``KeyError``,
which is the intended RED signal.

Fixtures were probed against the shipped ``search.querykey.wire_query`` before
being written (repository rule: probe before spec).  The wire function is never
stubbed here: S25 asserts web-transport inertness, and a stub could be written
to make that property hold trivially.

The aggregated guard log line is identified by the stable token
``wire_collapse``.  Only the *number* of lines per parent is asserted, never the
line's exact wording -- the property under test is "aggregated, not per child".
"""

import logging
import random

from search.querykey import fingerprint, wire_query
from search.refine_governor import RefineGovernor

GUARD_TOKEN = "wire_collapse"

# --- fixtures: every wire value below was probed, not assumed -----------------
# Parent whose wire is "sk-": children keep a fixed literal after it, so they do
# not collapse onto the parent but can collapse onto each other.
PARENT_BROAD = "/sk-[a-z]{2}[0-9]{30}/"
# Parent whose wire is "sk-a": every child below also reduces to "sk-a".
PARENT_NARROW = "/sk-a[a-z0-9]{31}/"

SIBLINGS = [                      # wires: "sk-a", "sk-a", "sk-b"  -> 2 distinct
    "/sk-a[0-9]b[a-z]{28}/",
    "/sk-a[0-9]c[a-z]{28}/",
    "/sk-b[0-9]z[a-z]{28}/",
]
SELF_COLLAPSING = [               # all reduce to the parent's wire "sk-a"
    "/sk-a[0-9][a-z]{29}/",
    "/sk-a[b-d][a-z]{29}/",
    "/sk-a[0-9]e[a-z]{28}/",
]
FIVE_DISTINCT = [                 # the pre-existing fixture: 5 distinct wires
    "/sk-c[d-e][a-z]{30}/",
    "/sk-a[b-c][a-z]{30}/",
    "/sk-b[c-d][a-z]{30}/",
    "/sk-d[e-f][a-z]{30}/",
    "/sk-e[f-g][a-z]{30}/",
]
NO_FIXED_LITERAL_PARENT = "/[a-z]{32}/"
NO_FIXED_LITERAL_CHILDREN = [     # wire falls back to the raw query for each
    "/[a-z]{31}[0-9]/",
    "/[a-z]{31}[a-f]/",
    "/[a-z]{30}[0-9]{2}/",
]


def _gov(mode="on", partitions=128, budget=1000, depth=2, guard=None):
    """Build a governor; the guard keyword is passed only when a test needs it,
    so unrelated tests fail on behaviour rather than on the signature."""
    kwargs = dict(
        mode=mode,
        max_refine_depth=depth,
        max_partitions_per_refine=partitions,
        max_search_tasks_per_run=budget,
    )
    if guard is not None:
        kwargs["drop_wire_indistinguishable"] = guard
    return RefineGovernor(**kwargs)


def _guard_lines(caplog):
    return [r.getMessage() for r in caplog.records if GUARD_TOKEN in r.getMessage()]


# ---------------------------------------------------------------------------
# S19 -- a child collapsing onto its parent's wire query is withheld
# ---------------------------------------------------------------------------
def test_s19_child_collapsing_onto_parent_wire_is_withheld():
    """WHEN every candidate of a parent shares the parent's wire query THEN
    nothing is admitted, the collapse counter equals the candidate count, and
    neither the partition cap nor the run budget is touched by the batch."""
    assert wire_query(PARENT_NARROW, True) == wire_query(SELF_COLLAPSING[0], True)

    gov = _gov(partitions=10, budget=100)
    admitted = gov.select_children("prov", PARENT_NARROW, list(SELF_COLLAPSING), 1000, 9000)

    assert admitted == []
    m = gov.metrics
    assert m["children_generated"] == len(SELF_COLLAPSING)
    assert m["refused_wire_collapse"] == len(SELF_COLLAPSING)
    assert m["children_admitted"] == 0
    assert m["distinct_wire_admitted"] == 0
    assert m["truncated_to_cap"] == 0
    assert m["refused_budget"] == 0
    assert m["budget_remaining"] == 100          # withheld children consume nothing


# ---------------------------------------------------------------------------
# S20 -- siblings sharing one wire query admit exactly one survivor
# ---------------------------------------------------------------------------
def test_s20_siblings_sharing_one_wire_admit_exactly_one_survivor():
    """WHEN candidates collapse onto fewer distinct wire queries than they have
    members THEN exactly one child per distinct wire is admitted, the survivor is
    a verbatim candidate, and the withheld count is reported."""
    assert wire_query(PARENT_BROAD, True) not in {wire_query(q, True) for q in SIBLINGS}
    assert len({wire_query(q, True) for q in SIBLINGS}) == 2

    gov = _gov()
    admitted = gov.select_children("prov", PARENT_BROAD, list(SIBLINGS), 1000, 9000)

    wires = [wire_query(q, True) for q in admitted]
    assert len(admitted) == 2
    assert len(set(wires)) == 2
    assert set(wires) == {wire_query("/sk-a[0-9]b[a-z]{28}/", True),
                          wire_query("/sk-b[0-9]z[a-z]{28}/", True)}
    assert all(q in SIBLINGS for q in admitted)          # selected, never rewritten
    assert gov.metrics["refused_wire_collapse"] == 1


# ---------------------------------------------------------------------------
# S21 -- enforced admission admits one child per distinct wire query
# ---------------------------------------------------------------------------
def test_s21_enforced_admission_admits_one_child_per_distinct_wire():
    """WHEN several parents refine under enforcement THEN the published figures
    satisfy admitted == distinct-wire, readable from the status line alone."""
    gov = _gov()
    gov.select_children("p1", PARENT_BROAD, list(SIBLINGS), 1000, 9000)
    gov.select_children("p2", PARENT_NARROW, list(SELF_COLLAPSING), 1000, 9000)
    gov.select_children("p3", "/sk-x/", list(FIVE_DISTINCT), 1000, 9000)

    m = gov.metrics
    assert m["children_admitted"] > 0
    assert m["children_admitted"] == m["distinct_wire_admitted"]
    # 2 (siblings) + 0 (all collapse onto parent) + 5 (already distinct)
    assert m["children_admitted"] == 7


# ---------------------------------------------------------------------------
# S22 -- withheld children consume neither cap nor budget
# ---------------------------------------------------------------------------
def test_s22_withheld_children_consume_neither_cap_nor_budget():
    """WHEN one batch loses candidates to collapse, to the cap and to the budget
    THEN the four counters account for disjoint children and the budget is drawn
    down only by admitted children."""
    batch = [
        "/sk-a[0-9]b[a-z]{28}/",   # "sk-a"
        "/sk-a[0-9]c[a-z]{28}/",   # "sk-a"  collapse
        "/sk-a[0-9]d[a-z]{28}/",   # "sk-a"  collapse
        "/sk-b[0-9]z[a-z]{28}/",   # "sk-b"
        "/sk-c[0-9]z[a-z]{28}/",   # "sk-c"
        "/sk-d[0-9]z[a-z]{28}/",   # "sk-d"
    ]
    assert len({wire_query(q, True) for q in batch}) == 4

    gov = _gov(partitions=2, budget=1)
    admitted = gov.select_children("prov", PARENT_BROAD, list(batch), 1000, 9000)

    m = gov.metrics
    assert m["children_generated"] == 6
    assert m["refused_wire_collapse"] == 2
    assert m["truncated_to_cap"] == 2          # post-guard population 4, cap 2
    assert m["refused_budget"] == 1
    assert len(admitted) == m["children_admitted"] == 1
    assert m["distinct_wire_admitted"] == 1
    assert m["budget_remaining"] == 0
    assert m["children_generated"] == (
        m["refused_wire_collapse"] + m["truncated_to_cap"]
        + m["refused_budget"] + m["children_admitted"]
    )


# ---------------------------------------------------------------------------
# S23 -- the survivor is deterministic under generator reordering
# ---------------------------------------------------------------------------
def test_s23_survivor_is_deterministic_under_generator_reordering():
    """WHEN the same batch is admitted repeatedly with the candidate order
    shuffled THEN every run admits the same list in the same order, including
    which raw query survived each wire group."""
    batch = [
        "/sk-a[0-9]b[a-z]{28}/",
        "/sk-a[0-9]c[a-z]{28}/",
        "/sk-a[0-9]d[a-z]{28}/",
        "/sk-b[0-9]z[a-z]{28}/",
        "/sk-c[0-9]z[a-z]{28}/",
    ]
    runs = []
    for seed in range(8):
        shuffled = list(batch)
        random.Random(seed).shuffle(shuffled)
        gov = _gov()
        runs.append(gov.select_children("p", PARENT_BROAD, shuffled, 1000, 9000))

    assert all(r == runs[0] for r in runs), runs
    assert len(runs[0]) == 3
    # the survivor of a wire group is its lexicographically smallest raw query
    assert "/sk-a[0-9]b[a-z]{28}/" in runs[0]
    assert "/sk-a[0-9]c[a-z]{28}/" not in runs[0]
    assert "/sk-a[0-9]d[a-z]{28}/" not in runs[0]


# ---------------------------------------------------------------------------
# S24 -- no fixed literal: wire falls back to raw, nothing collapses
# ---------------------------------------------------------------------------
def test_s24_child_without_fixed_literal_is_not_spuriously_collapsed():
    """WHEN parent and children all reduce to no fixed literal, so the wire value
    falls back to each raw query THEN distinct children are all admitted -- the
    comparison is on the transport's wire value, not on literal extraction."""
    for q in [NO_FIXED_LITERAL_PARENT] + NO_FIXED_LITERAL_CHILDREN:
        assert wire_query(q, True) == q, q

    gov = _gov()
    admitted = gov.select_children("p", NO_FIXED_LITERAL_PARENT,
                                   list(NO_FIXED_LITERAL_CHILDREN), 1000, 9000)

    assert sorted(admitted) == sorted(NO_FIXED_LITERAL_CHILDREN)
    assert gov.metrics["refused_wire_collapse"] == 0
    assert gov.metrics["children_admitted"] == len(NO_FIXED_LITERAL_CHILDREN)


# ---------------------------------------------------------------------------
# S25 -- the web transport withholds nothing
# ---------------------------------------------------------------------------
def test_s25_web_transport_withholds_nothing():
    """WHEN use_api is false and candidates are distinct raw strings THEN every
    candidate distinct from the parent is admitted and nothing is counted as
    collapsed, because on web the wire value is the raw query verbatim."""
    collapsing = SELF_COLLAPSING + SIBLINGS
    assert len({wire_query(q, False) for q in collapsing}) == len(collapsing)

    gov = _gov()
    admitted = gov.select_children("p", PARENT_NARROW, list(collapsing), 1000, 9000,
                                   use_api=False)

    assert sorted(admitted) == sorted(collapsing)
    m = gov.metrics
    assert m["refused_wire_collapse"] == 0
    assert m["children_admitted"] == len(collapsing)
    assert m["distinct_wire_admitted"] == len(collapsing)


# ---------------------------------------------------------------------------
# S26 -- withholding is reported once per parent
# ---------------------------------------------------------------------------
def test_s26_withholding_is_reported_once_per_parent(caplog):
    """WHEN one parent has a large batch all collapsing onto a single wire query
    THEN exactly one aggregated line is emitted for that parent and the line
    count does not grow with the number of withheld children."""
    batch = [f"/sk-a[{i}][a-z]{{28}}/" for i in range(40)]
    assert len(set(batch)) == 40
    assert len({wire_query(q, True) for q in batch}) == 1

    with caplog.at_level(logging.INFO):
        gov = _gov()
        admitted = gov.select_children("prov", PARENT_NARROW, batch, 1000, 9000)

    lines = _guard_lines(caplog)
    assert admitted == []
    assert gov.metrics["refused_wire_collapse"] == 40
    assert len(lines) == 1, f"expected one aggregated line, got {len(lines)}: {lines}"
    assert "prov" in lines[0] and PARENT_NARROW in lines[0]
    assert "40" in lines[0]


# ---------------------------------------------------------------------------
# S27 -- shadow counts and logs without enforcing
# ---------------------------------------------------------------------------
def test_s27_shadow_counts_without_enforcing(caplog):
    """WHEN mode is shadow on a collapsing batch THEN every candidate is still
    enqueued while the would-be withholding is counted with the same value 'on'
    produces and logged on a line marked as shadow."""
    with caplog.at_level(logging.INFO):
        shadow = _gov(mode="shadow")
        admitted = shadow.select_children("p", PARENT_BROAD, list(SIBLINGS), 1000, 9000)
        enforced = _gov(mode="on")
        enforced.select_children("p", PARENT_BROAD, list(SIBLINGS), 1000, 9000)

    assert sorted(admitted) == sorted(SIBLINGS)                  # nothing withheld
    assert shadow.metrics["refused_wire_collapse"] == 1
    assert shadow.metrics["refused_wire_collapse"] == enforced.metrics["refused_wire_collapse"]
    assert any("shadow" in ln for ln in _guard_lines(caplog))


# ---------------------------------------------------------------------------
# S28 -- the guard is a single boolean rollback independent of mode
# ---------------------------------------------------------------------------
def test_s28_guard_flag_is_a_single_boolean_rollback(caplog):
    """WHEN drop_wire_indistinguishable is false THEN admission in both shadow and
    on equals pre-guard admission, no guard line is emitted, the collapse counter
    is zero, and the distinct-wire figure is still measured."""
    pre_guard = sorted(SIBLINGS, key=lambda q: fingerprint(q, True))

    with caplog.at_level(logging.INFO):
        for mode in ("on", "shadow"):
            gov = _gov(mode=mode, guard=False)
            admitted = gov.select_children("p", PARENT_BROAD, list(SIBLINGS), 1000, 9000)

            assert sorted(admitted) == sorted(pre_guard), mode
            m = gov.metrics
            assert m["refused_wire_collapse"] == 0, mode
            # observation, not decision: still measured so the rollback arm of the
            # live gate is self-evidencing (design D8)
            assert m["distinct_wire_admitted"] == 2, mode

    assert _guard_lines(caplog) == []


# ---------------------------------------------------------------------------
# S30 -- truncation counts cap surplus only
# ---------------------------------------------------------------------------
def test_s30_truncation_counts_cap_surplus_only():
    """WHEN a batch loses candidates both to wire collapse and to the partition cap
    THEN the truncation counter reflects post-guard surplus only and no candidate
    is counted twice."""
    batch = [
        "/sk-a[0-9]b[a-z]{28}/",   # "sk-a"
        "/sk-a[0-9]c[a-z]{28}/",   # "sk-a"  collapse
        "/sk-b[0-9]z[a-z]{28}/",   # "sk-b"
        "/sk-c[0-9]z[a-z]{28}/",   # "sk-c"
        "/sk-d[0-9]z[a-z]{28}/",   # "sk-d"
    ]
    gov = _gov(partitions=2)
    admitted = gov.select_children("p", PARENT_BROAD, list(batch), 1000, 9000)

    m = gov.metrics
    assert m["refused_wire_collapse"] == 1
    assert m["truncated_to_cap"] == 2          # post-guard 4 candidates, cap 2 -> 2
    assert len(admitted) == m["children_admitted"] == 2
    assert m["refused_budget"] == 0
    assert (m["refused_wire_collapse"] + m["truncated_to_cap"]
            + m["refused_budget"] + m["children_admitted"]) == len(batch)
