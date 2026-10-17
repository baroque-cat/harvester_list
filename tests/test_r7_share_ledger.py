"""R7 — GatherShareLedger pins (cross-provider fetch sharing).

Traceability: /tmp/opencode/r7-plan.md §3 D1, D2, D3.

The ledger lives in manager/ (not storage/ — no persistence; not stage/ — used
by two stages).  A single threading.Lock guards all operations.  The key is
(url_hash, patterns_hash, transport) — provider is NOT part of the key, which
is the whole point of R7.

House conventions: no network, no live GitHub, no credentials.  Pin names use
the test_l<N>_... pattern.
"""

import threading

import pytest

from manager.gather_share import (
    DEFAULT_MAX_TRACKED_KEYS,
    MODE_OFF,
    MODE_ON,
    MODE_SHADOW,
    VALID_MODES,
    GatherShareLedger,
)


# ---------------------------------------------------------------------------
# L1 — off mode: zero overhead, indistinguishable from pre-change
# ---------------------------------------------------------------------------
def test_l1_off_mode_claim_always_false_no_state():
    ledger = GatherShareLedger(mode=MODE_OFF)
    assert ledger.enabled is False
    assert ledger.enforce is False

    result = ledger.claim("url1", "pat1", "raw", "provider_a")
    assert result is False
    stats = ledger.to_stats()
    assert stats["claims"] == 0
    assert stats["mode"] == "off"

    taken = ledger.take("url1", "pat1", "raw")
    assert taken == []


# ---------------------------------------------------------------------------
# L2 — on mode, two providers, same key: first False, second True
# ---------------------------------------------------------------------------
def test_l2_on_mode_two_providers_same_key():
    ledger = GatherShareLedger(mode=MODE_ON)
    assert ledger.enabled is True
    assert ledger.enforce is True

    # First claim: registers owner, returns False (task IS created)
    r1 = ledger.claim("url1", "pat1", "raw", "provider_a")
    assert r1 is False

    # Second claim by different provider: returns True (suppress task)
    r2 = ledger.claim("url1", "pat1", "raw", "provider_b")
    assert r2 is True

    stats = ledger.to_stats()
    assert stats["suppressed"] == 1
    assert stats["claims"] == 2

    # take returns both providers, owner first
    taken = ledger.take("url1", "pat1", "raw")
    assert taken == ["provider_a", "provider_b"]


# ---------------------------------------------------------------------------
# L3 — idempotent claim: same provider claiming twice not appended twice
# ---------------------------------------------------------------------------
def test_l3_idempotent_claim_same_provider():
    ledger = GatherShareLedger(mode=MODE_ON)

    ledger.claim("url1", "pat1", "raw", "provider_a")
    ledger.claim("url1", "pat1", "raw", "provider_a")  # idempotent

    taken = ledger.take("url1", "pat1", "raw")
    assert taken == ["provider_a"]
    assert len(taken) == 1


# ---------------------------------------------------------------------------
# L4 — different patterns_hash or transport → different key → no suppression
# ---------------------------------------------------------------------------
def test_l4_different_patterns_hash_no_suppression():
    ledger = GatherShareLedger(mode=MODE_ON)

    r1 = ledger.claim("url1", "pat1", "raw", "provider_a")
    r2 = ledger.claim("url1", "pat2", "raw", "provider_b")  # different patterns
    assert r1 is False
    assert r2 is False  # different key, no suppression

    stats = ledger.to_stats()
    assert stats["suppressed"] == 0
    assert stats["tracked"] == 2


def test_l4_different_transport_no_suppression():
    ledger = GatherShareLedger(mode=MODE_ON)

    r1 = ledger.claim("url1", "pat1", "raw", "provider_a")
    r2 = ledger.claim("url1", "pat1", "html", "provider_b")  # different transport
    assert r1 is False
    assert r2 is False  # different key, no suppression

    stats = ledger.to_stats()
    assert stats["suppressed"] == 0
    assert stats["tracked"] == 2


# ---------------------------------------------------------------------------
# L5 — fan-out accounting: take of 3 providers
# ---------------------------------------------------------------------------
def test_l5_fanout_accounting():
    ledger = GatherShareLedger(mode=MODE_ON)

    ledger.claim("url1", "pat1", "raw", "provider_a")
    ledger.claim("url1", "pat1", "raw", "provider_b")
    ledger.claim("url1", "pat1", "raw", "provider_c")

    taken = ledger.take("url1", "pat1", "raw")
    assert len(taken) == 3

    stats = ledger.to_stats()
    assert stats["shared_fetches"] == 1
    assert stats["fanout_extra"] == 2
    assert stats["takes"] == 1


# ---------------------------------------------------------------------------
# L6 — abandon: returns dropped count, removes entry
# ---------------------------------------------------------------------------
def test_l6_abandon_returns_dropped_and_removes_entry():
    ledger = GatherShareLedger(mode=MODE_ON)

    ledger.claim("url1", "pat1", "raw", "provider_a")
    ledger.claim("url1", "pat1", "raw", "provider_b")
    ledger.claim("url1", "pat1", "raw", "provider_c")

    dropped = ledger.abandon("url1", "pat1", "raw")
    assert dropped == 3  # total providers dropped

    # Entry removed
    taken = ledger.take("url1", "pat1", "raw")
    assert taken == []

    stats = ledger.to_stats()
    # abandoned counts beneficiaries (dropped - 1 for the owner)
    assert stats["abandoned"] == 2


def test_l6_abandon_absent_key_returns_zero():
    ledger = GatherShareLedger(mode=MODE_ON)
    dropped = ledger.abandon("nope", "nope", "raw")
    assert dropped == 0


def test_l6_abandon_off_mode_returns_zero():
    ledger = GatherShareLedger(mode=MODE_OFF)
    dropped = ledger.abandon("url1", "pat1", "raw")
    assert dropped == 0


# ---------------------------------------------------------------------------
# L7 — cap fail-open: third NEW key refused, already-tracked key still works
# ---------------------------------------------------------------------------
def test_l7_cap_fail_open():
    ledger = GatherShareLedger(mode=MODE_ON, max_tracked=2)

    # Two keys fill the cap
    ledger.claim("url1", "pat1", "raw", "p1")
    ledger.claim("url2", "pat1", "raw", "p2")

    # Third NEW key: refused, fail-open
    r3 = ledger.claim("url3", "pat1", "raw", "p3")
    assert r3 is False
    stats = ledger.to_stats()
    assert stats["ledger_full"] == 1

    # Already-tracked key: still succeeds normally
    r4 = ledger.claim("url1", "pat1", "raw", "p4")
    assert r4 is True  # url1 is tracked, new provider appended


# ---------------------------------------------------------------------------
# L8 — shadow mode: would_suppress counts, claim always False, no entries
# ---------------------------------------------------------------------------
def test_l8_shadow_mode():
    ledger = GatherShareLedger(mode=MODE_SHADOW)
    assert ledger.enabled is True
    assert ledger.enforce is False

    # First "claim" — no owner yet, no would_suppress
    r1 = ledger.claim("url1", "pat1", "raw", "provider_a")
    assert r1 is False

    # Second claim by different provider — would suppress, but doesn't
    r2 = ledger.claim("url1", "pat1", "raw", "provider_b")
    assert r2 is False

    stats = ledger.to_stats()
    assert stats["would_suppress"] == 1
    assert stats["claims"] == 2
    assert stats["tracked"] == 0  # no entries registered

    # take returns empty (no entries)
    taken = ledger.take("url1", "pat1", "raw")
    assert taken == []


# ---------------------------------------------------------------------------
# L9 — thread safety: 8 threads × 200 claims over 50 keys
# ---------------------------------------------------------------------------
def test_l9_thread_safety():
    ledger = GatherShareLedger(mode=MODE_ON)
    n_threads = 8
    claims_per_thread = 200
    n_keys = 50
    barrier = threading.Barrier(n_threads)

    results = [[] for _ in range(n_threads)]

    def worker(thread_id):
        barrier.wait()
        for i in range(claims_per_thread):
            key_idx = i % n_keys
            provider = f"t{thread_id}_p{i}"
            url_hash = f"url_{key_idx}"
            pat_hash = "pat_same"
            transport = "raw"
            r = ledger.claim(url_hash, pat_hash, transport, provider)
            results[thread_id].append((url_hash, provider, r))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # For each key, exactly one claim should return False (the first/owner)
    # and the rest True.
    for key_idx in range(n_keys):
        url_hash = f"url_{key_idx}"
        all_results_for_key = []
        for tid in range(n_threads):
            for (uh, prov, r) in results[tid]:
                if uh == url_hash:
                    all_results_for_key.append((prov, r))

        false_count = sum(1 for _, r in all_results_for_key if r is False)
        true_count = sum(1 for _, r in all_results_for_key if r is True)
        assert false_count == 1, f"key {url_hash}: expected 1 False, got {false_count}"
        assert true_count == len(all_results_for_key) - 1

    # take for each key returns all distinct providers with no duplicates
    for key_idx in range(n_keys):
        url_hash = f"url_{key_idx}"
        taken = ledger.take(url_hash, "pat_same", "raw")
        assert len(taken) == len(set(taken)), f"duplicates for {url_hash}"
        # Each thread contributed claims_per_thread // n_keys = 200/50 = 4 claims
        expected_providers = n_threads * (claims_per_thread // n_keys)
        assert len(taken) == expected_providers, (
            f"key {url_hash}: expected {expected_providers} providers, got {len(taken)}"
        )


# ---------------------------------------------------------------------------
# L10 — invalid mode string degrades to off
# ---------------------------------------------------------------------------
def test_l10_invalid_mode_degrades_to_off():
    ledger = GatherShareLedger(mode="garbage")
    assert ledger.to_stats()["mode"] == "off"
    assert ledger.enabled is False
    assert ledger.claim("u", "p", "raw", "prov") is False


# ---------------------------------------------------------------------------
# L11 — to_stats() key set is EXACTLY the documented set; close() safe twice
# ---------------------------------------------------------------------------
def test_l11_to_stats_exact_key_set():
    ledger = GatherShareLedger(mode=MODE_ON)
    stats = ledger.to_stats()
    expected_keys = {
        "mode", "claims", "would_suppress", "suppressed", "takes",
        "shared_fetches", "fanout_extra", "abandoned", "ledger_full", "tracked",
    }
    assert set(stats) == expected_keys


def test_l11_close_twice_safe_and_empties_tracked():
    ledger = GatherShareLedger(mode=MODE_ON)
    ledger.claim("url1", "pat1", "raw", "p1")
    ledger.claim("url1", "pat1", "raw", "p2")
    assert ledger.to_stats()["tracked"] == 1

    ledger.close()
    assert ledger.to_stats()["tracked"] == 0

    # Second close must not raise
    ledger.close()
    assert ledger.to_stats()["tracked"] == 0


# ---------------------------------------------------------------------------
# L12 — loud-once warning fires exactly once for repeated cap refusals
# ---------------------------------------------------------------------------
def test_l12_loud_once_warning_on_cap_refusals(caplog):
    import logging

    ledger = GatherShareLedger(mode=MODE_ON, max_tracked=1)

    # First key fills the cap
    ledger.claim("url1", "pat1", "raw", "p1")

    with caplog.at_level(logging.WARNING):
        # Second NEW key: refused, WARNING fires
        ledger.claim("url2", "pat1", "raw", "p2")
        # Third NEW key: refused, WARNING must NOT fire again
        ledger.claim("url3", "pat1", "raw", "p3")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "ledger" in r.message.lower()]
    assert len(warnings) == 1, f"Expected exactly 1 warning, got {len(warnings)}: {[w.message for w in warnings]}"
    assert ledger.to_stats()["ledger_full"] == 2  # counter still increments
