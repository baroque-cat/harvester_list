"""Slice 6.5 — Flip-hygiene pins (transport in search task identity).

Traceability: 02_critical_fixes.md §2.4 line 101 «Flip-гигиена»;
07_verification.md §7.8.

Defect: ``SearchStage._generate_id`` (stage/definition.py:113-118) produced
``search:{provider}:{query}:{page}:{regex}`` — WITHOUT transport.  A
``use_api`` flip (api↔web) while pending rows lived in the durable queue
caused ``SqliteTaskQueue.put`` (storage/task_queue.py:285-298) to suppress
the new-transport twin as a duplicate (same dedup_id), silently losing
tasks of the other transport.

Fix: append a transport segment (``"api"``/``"web"``) to the search
identity so that identical (provider, query, page, regex) with different
``use_api`` produce distinct dedup_ids and both survive insertion.

Contract pinned here:

* FH1: two SearchTasks identical except use_api ⇒ DIFFERENT ids; both
  contain the transport segment.
* FH2: two identical api-transport tasks ⇒ SAME id (same-transport twin
  suppression preserved).
* FH3 (functional, MAIN): real SqliteTaskQueue + real SearchStage._generate_id:
  put(api-twin) then put(web-twin) ⇒ BOTH rows in queue (rows==2);
  control: re-put same api ⇒ suppressed (rows==2, not 3).
* FH4: id stability through defer-recompute (two _generate_id calls equal;
  attempts/created_at mutations do not affect id).
* FH5: gather-share key NOT affected — claim/take with same
  (url_hash, patterns_hash, transport) work byte-as-before (insurance
  against accidental gather_share.py edits — file NOT to be changed).
* FH6: other stages not affected — Acquisition/Check/Inspect _generate_id
  byte-as-before (insurance against drift).

Expected RED at creation, and why:

* FH1: ids equal (both lack transport segment) ⇒ assertion fails.
* FH3: rows==1 instead of 2 (second put suppressed as dup).
* FH2/FH4/FH5/FH6: insurance pins — PASS before and after fix.

House style: no network, no live GitHub, no credentials.
"""

from __future__ import annotations

import json

import pytest

from core.models import SearchTask
from manager.gather_share import GatherShareLedger, MODE_ON
from stage.definition import AcquisitionStage, CheckStage, InspectStage, SearchStage
from storage.task_queue import SqliteTaskQueue


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _search_task(**overrides):
    """Build a SearchTask with sensible defaults; overrides win."""
    defaults = dict(
        provider="deepseek",
        query='"sk-"',
        regex=r"sk-[A-Za-z0-9]{16}",
        page=1,
        use_api=False,
    )
    defaults.update(overrides)
    return SearchTask(**defaults)


def _search_stage():
    """Minimal SearchStage for id generation (no worker, no resources)."""
    # We only need _generate_id — bypass __init__ requirements.
    stage = SearchStage.__new__(SearchStage)
    return stage


def _q(tmp_path, name="fh", **kwargs):
    """Construct a SqliteTaskQueue with sensible test defaults."""
    kwargs.setdefault("visibility_timeout_s", 300.0)
    kwargs.setdefault("max_age_hours", 720.0)
    return SqliteTaskQueue(
        str(tmp_path / f"{name}_queue.sqlite"),
        serializer=json.dumps,
        deserializer=json.loads,
        name=name,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# FH1 — transport segment distinguishes api vs web
# ---------------------------------------------------------------------------
def test_fh1_transport_segment_distinguishes_api_vs_web():
    """FH1: identical (provider, query, page, regex) with use_api=True vs
    False ⇒ DIFFERENT ids; both contain the transport segment."""
    stage = _search_stage()
    api_task = _search_task(use_api=True)
    web_task = _search_task(use_api=False)

    id_api = stage._generate_id(api_task)
    id_web = stage._generate_id(web_task)

    assert id_api != id_web, (
        "api and web transport must produce DIFFERENT dedup_ids"
    )
    assert "api" in id_api, "api id must contain the transport segment 'api'"
    assert "web" in id_web, "web id must contain the transport segment 'web'"


# ---------------------------------------------------------------------------
# FH2 — same-transport twins still collapse (suppression preserved)
# ---------------------------------------------------------------------------
def test_fh2_same_transport_twins_collapse():
    """FH2: two identical api-transport tasks ⇒ SAME id (same-transport twin
    suppression preserved — the fix must not break existing dedup)."""
    stage = _search_stage()
    t1 = _search_task(use_api=True)
    t2 = _search_task(use_api=True)

    assert stage._generate_id(t1) == stage._generate_id(t2), (
        "identical tasks on the same transport must produce the SAME id"
    )


# ---------------------------------------------------------------------------
# FH3 — functional: real queue accepts both transports
# ---------------------------------------------------------------------------
def test_fh3_real_queue_accepts_both_transports(tmp_path):
    """FH3 (functional, MAIN): real SqliteTaskQueue + real
    SearchStage._generate_id: put(api-twin) then put(web-twin) ⇒ BOTH rows
    in queue (rows==2); control: re-put same api ⇒ suppressed (rows==2, not 3).

    Pre-fix: the second put was suppressed (rows==1, suppressed_puts==1)
    because the dedup_id lacked the transport segment.
    """
    stage = _search_stage()
    q = _q(tmp_path)
    try:
        api_task = _search_task(use_api=True)
        web_task = _search_task(use_api=False)

        id_api = stage._generate_id(api_task)
        id_web = stage._generate_id(web_task)

        # Put api twin
        assert q.put({"task": "api"}, dedup_id=id_api) is True
        assert q.qsize() == 1

        # Put web twin — MUST succeed (different transport ⇒ different id)
        assert q.put({"task": "web"}, dedup_id=id_web) is True
        assert q.qsize() == 2, (
            "both api and web twins must coexist in the queue"
        )

        # Control: re-put same api ⇒ suppressed (same-transport dedup works)
        assert q.put({"task": "api_dup"}, dedup_id=id_api) is False
        assert q.qsize() == 2, (
            "same-transport duplicate must be suppressed"
        )
        stats = q.dedup_stats()
        assert stats["suppressed_puts"] == 1
    finally:
        q.close()


# ---------------------------------------------------------------------------
# FH4 — id stability through defer-recompute
# ---------------------------------------------------------------------------
def test_fh4_id_stability_through_defer_recompute():
    """FH4: two _generate_id calls on the same task are equal; mutating
    attempts/created_at does not affect the id (id is computed from task
    fields, not mutable state)."""
    stage = _search_stage()
    task = _search_task(use_api=True)

    id_before = stage._generate_id(task)

    # Simulate defer: attempts and created_at mutate
    task.attempts = 5
    task.created_at = 9999999.0

    id_after = stage._generate_id(task)

    assert id_before == id_after, (
        "id must be stable across defer-recompute (attempts/created_at mutations)"
    )


# ---------------------------------------------------------------------------
# FH5 — gather-share key not affected
# ---------------------------------------------------------------------------
def test_fh5_gather_share_key_not_affected():
    """FH5: gather-share key contains transport ⇒ claim/take with same
    (url_hash, patterns_hash, transport) work byte-as-before.

    Insurance pin: gather_share.py is NOT to be changed in this slice.
    This pin guards against accidental edits.
    """
    ledger = GatherShareLedger(mode=MODE_ON)

    # First claim: registers owner, returns False (task IS created)
    r1 = ledger.claim("url_hash_1", "patterns_hash_1", "api", "provider_a")
    assert r1 is False

    # Second claim by different provider, same key: returns True (suppress)
    r2 = ledger.claim("url_hash_1", "patterns_hash_1", "api", "provider_b")
    assert r2 is True

    # Take returns the beneficiary list
    taken = ledger.take("url_hash_1", "patterns_hash_1", "api")
    assert taken == ["provider_a", "provider_b"]

    # Different transport ⇒ different key ⇒ no suppression
    r3 = ledger.claim("url_hash_1", "patterns_hash_1", "web", "provider_a")
    assert r3 is False, "different transport must not suppress"


# ---------------------------------------------------------------------------
# FH6 — other stages not affected
# ---------------------------------------------------------------------------
def test_fh6_other_stages_not_affected():
    """FH6: Acquisition/Check/Inspect _generate_id byte-as-before.

    Insurance pin: other stages' _generate_id must not be changed in this
    slice (the fix is search-only).
    """
    # Acquisition: GATHER:{provider}:{url}
    acq_stage = AcquisitionStage.__new__(AcquisitionStage)
    from core.models import AcquisitionTask
    acq_task = AcquisitionTask(provider="deepseek", url="https://example.com")
    acq_id = acq_stage._generate_id(acq_task)
    assert acq_id == "gather:deepseek:https://example.com"

    # Check: CHECK:{provider}:{digest} (Service() default is truthy)
    check_stage = CheckStage.__new__(CheckStage)
    from core.models import CheckTask
    check_task = CheckTask(provider="deepseek")
    check_id = check_stage._generate_id(check_task)
    # Service() default has empty fields but is truthy ⇒ hash branch
    assert check_id.startswith("check:deepseek:")
    assert len(check_id) > len("check:deepseek:unknown")

    # Inspect: INSPECT:{provider}:{digest} (Service() default is truthy)
    inspect_stage = InspectStage.__new__(InspectStage)
    from core.models import InspectTask
    inspect_task = InspectTask(provider="deepseek")
    inspect_id = inspect_stage._generate_id(inspect_task)
    # Service() default has empty fields but is truthy ⇒ hash branch
    assert inspect_id.startswith("inspect:deepseek:")
    assert len(inspect_id) > len("inspect:deepseek:unknown")
