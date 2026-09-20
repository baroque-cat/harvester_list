"""Initial-task locality grouping (add-search-aggregation).

Traceability: search-aggregation-S4, S5, S6.

Harness: pure-function tests against the planned helpers in ``manager/task.py``
(``order_tasks_for_locality``, ``count_aggregatable_pairs``). Import failure of
the helpers IS the expected RED state.

Contract: stable grouping by (transport, fingerprint); multiset of tasks is
preserved exactly (no merge/drop/mutation - baskets intact); relative order
within a group follows the original creation sequence.
"""

import pytest

from core.models import SearchTask
from manager.task import count_aggregatable_pairs, order_tasks_for_locality  # RED driver

KEY = r"sk-[A-Za-z0-9]{16}"


def _task(provider, query, use_api, page=1, regex=KEY):
    return SearchTask(provider=provider, query=query, regex=regex, page=page, use_api=use_api)


def _identity(task):
    return (task.provider, task.query, task.page, task.use_api, task.regex)


# ---------------------------------------------------------------------------
# search-aggregation-S4
# ---------------------------------------------------------------------------
def test_s4_grouping_preserves_the_task_multiset():
    tasks = [
        _task("prov-a", "/re1/", False),
        _task("prov-a", "/re2/", True),
        _task("prov-b", "/re1/", False),
        _task("prov-c", "/re3/", False),
        _task("prov-b", "/re2/", True),
    ]

    ordered = order_tasks_for_locality(list(tasks))

    assert len(ordered) == len(tasks)
    assert sorted(map(_identity, ordered)) == sorted(map(_identity, tasks))
    # objects are the same instances - no cloning/mutation of fields
    assert all(any(o is t for t in tasks) for o in ordered)


# ---------------------------------------------------------------------------
# search-aggregation-S5
# ---------------------------------------------------------------------------
def test_s5_identical_queries_become_fifo_neighbors():
    tasks = [
        _task("prov-a", "/shared/", False),          # group X
        _task("prov-a", "/solo-a/", False),           # group Y
        _task("prov-b", "/shared/", False),           # group X
        _task("prov-b", "/solo-b/", True),            # group Z (other transport)
        _task("prov-c", "/shared/", False),           # group X
    ]

    ordered = order_tasks_for_locality(list(tasks))

    positions = [i for i, t in enumerate(ordered) if t.query == "/shared/"]
    assert positions == list(range(positions[0], positions[0] + 3)), "group members must be adjacent"

    # stable within the group: original provider order preserved
    shared_providers = [ordered[i].provider for i in positions]
    assert shared_providers == ["prov-a", "prov-b", "prov-c"]

    # transport separation: the API '/solo-b/' never lands inside the web group
    api_positions = [i for i, t in enumerate(ordered) if t.use_api]
    assert not any(p in positions for p in api_positions)


# ---------------------------------------------------------------------------
# search-aggregation-S6
# ---------------------------------------------------------------------------
def test_s6_aggregatable_pairs_metric():
    tasks = [
        _task("prov-a", "/q1/", False),
        _task("prov-b", "/q1/", False),   # dup pair 1 (same transport+wire)
        _task("prov-c", "/q1/", False),   # dup pair 2 (third member adds one more saved chain)
        _task("prov-a", "/q2/", True),
        _task("prov-b", "/q2/", False),   # NOT a dup: different transport
    ]

    # N conditions - M distinct fingerprints = saved HTTP chains
    assert count_aggregatable_pairs(tasks) == 2
