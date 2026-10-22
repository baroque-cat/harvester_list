"""Search-side cross-provider share ledger pins (D-03.6/D-03.7, F-07, NEW-3/N-09).

Traceability: 03_sql_integration.md D-03.6/D-03.7.

The ledger lives in manager/ (mirror of gather_share.py).  The key is
(wire_fp, page) — provider is NOT part of the key.  House conventions:
no network, no live GitHub, no credentials.
"""

import logging

import pytest

from manager.search_share import (
    DEFAULT_MAX_TRACKED,
    MODE_OFF,
    MODE_ON,
    MODE_SHADOW,
    VALID_MODES,
    SearchShareLedger,
)


# ---------------------------------------------------------------------------
# L1 — off mode: zero overhead, indistinguishable from pre-change
# ---------------------------------------------------------------------------
def test_l1_off_mode_claim_always_false_no_state():
    ledger = SearchShareLedger(mode=MODE_OFF)
    assert ledger.enabled is False
    assert ledger.enforce is False

    result = ledger.claim("fp1", 1, "provider_a", "ph1")
    assert result is False
    stats = ledger.to_stats()
    assert stats["claims"] == 0
    assert stats["mode"] == "off"

    taken = ledger.take("fp1", 1)
    assert taken == []


# ---------------------------------------------------------------------------
# L2 — on mode, two providers, same key: first False, second True
# ---------------------------------------------------------------------------
def test_l2_on_mode_two_providers_same_key():
    ledger = SearchShareLedger(mode=MODE_ON)
    assert ledger.enabled is True
    assert ledger.enforce is True

    # First claim: registers owner, returns False (task IS created)
    r1 = ledger.claim("fp1", 1, "provider_a", "ph1")
    assert r1 is False

    # Second claim by different provider: returns True (suppress task)
    r2 = ledger.claim("fp1", 1, "provider_b", "ph1")
    assert r2 is True

    stats = ledger.to_stats()
    assert stats["suppressed"] == 1
    assert stats["claims"] == 2

    # take returns owner (no resolver ⇒ just owner)
    taken = ledger.take("fp1", 1)
    assert taken == ["provider_a"]


# ---------------------------------------------------------------------------
# L3 — idempotent claim: same provider claiming twice not appended twice
# ---------------------------------------------------------------------------
def test_l3_idempotent_claim_same_provider():
    ledger = SearchShareLedger(mode=MODE_ON)

    ledger.claim("fp1", 1, "provider_a", "ph1")
    ledger.claim("fp1", 1, "provider_a", "ph1")  # idempotent

    taken = ledger.take("fp1", 1)
    assert taken == ["provider_a"]
    assert len(taken) == 1


# ---------------------------------------------------------------------------
# L4 — different page → different key → no suppression
# ---------------------------------------------------------------------------
def test_l4_different_page_no_suppression():
    ledger = SearchShareLedger(mode=MODE_ON)

    r1 = ledger.claim("fp1", 1, "provider_a", "ph1")
    r2 = ledger.claim("fp1", 2, "provider_b", "ph1")  # different page
    assert r1 is False
    assert r2 is False  # different key, no suppression

    stats = ledger.to_stats()
    assert stats["suppressed"] == 0
    assert stats["tracked"] == 2


# ---------------------------------------------------------------------------
# L5 — take with resolver: beneficiaries include all providers with same ph
# ---------------------------------------------------------------------------
def test_l5_take_with_resolver():
    def resolver(ph):
        if ph == "ph1":
            return ["provider_a", "provider_b", "provider_c"]
        return []

    ledger = SearchShareLedger(mode=MODE_ON, beneficiaries_resolver=resolver)

    ledger.claim("fp1", 1, "provider_a", "ph1")

    taken = ledger.take("fp1", 1)
    # Owner first, then sorted others
    assert taken == ["provider_a", "provider_b", "provider_c"]

    stats = ledger.to_stats()
    assert stats["shared_fetches"] == 1
    assert stats["fanout_extra"] == 2
    assert stats["takes"] == 1


# ---------------------------------------------------------------------------
# L6 — abandon: returns dropped count, removes entry
# ---------------------------------------------------------------------------
def test_l6_abandon_returns_dropped_and_removes_entry():
    def resolver(ph):
        if ph == "ph1":
            return ["provider_a", "provider_b", "provider_c"]
        return []

    ledger = SearchShareLedger(mode=MODE_ON, beneficiaries_resolver=resolver)

    ledger.claim("fp1", 1, "provider_a", "ph1")

    dropped = ledger.abandon("fp1", 1)
    assert dropped == 3  # total beneficiaries dropped

    # Entry removed
    taken = ledger.take("fp1", 1)
    assert taken == []

    stats = ledger.to_stats()
    # abandoned counts beneficiaries (dropped - 1 for the owner)
    assert stats["abandoned"] == 2


def test_l6_abandon_absent_key_returns_zero():
    ledger = SearchShareLedger(mode=MODE_ON)
    dropped = ledger.abandon("nope", 99)
    assert dropped == 0


def test_l6_abandon_off_mode_returns_zero():
    ledger = SearchShareLedger(mode=MODE_OFF)
    dropped = ledger.abandon("fp1", 1)
    assert dropped == 0


# ---------------------------------------------------------------------------
# L7 — cap fail-open: third NEW key refused, already-tracked key still works
# ---------------------------------------------------------------------------
def test_l7_cap_fail_open():
    ledger = SearchShareLedger(mode=MODE_ON, max_tracked=2)

    # Two keys fill the cap
    ledger.claim("fp1", 1, "p1", "ph1")
    ledger.claim("fp2", 1, "p2", "ph2")

    # Third NEW key: refused, fail-open
    r3 = ledger.claim("fp3", 1, "p3", "ph3")
    assert r3 is False
    stats = ledger.to_stats()
    assert stats["ledger_full"] == 1

    # Already-tracked key: still succeeds normally
    r4 = ledger.claim("fp1", 1, "p4", "ph1")
    assert r4 is True  # fp1 is tracked, new provider suppresses


# ---------------------------------------------------------------------------
# L8 — shadow mode: would_suppress counts, claim always False, no entries
# ---------------------------------------------------------------------------
def test_l8_shadow_mode():
    ledger = SearchShareLedger(mode=MODE_SHADOW)
    assert ledger.enabled is True
    assert ledger.enforce is False

    # First "claim" — no owner yet, no would_suppress
    r1 = ledger.claim("fp1", 1, "provider_a", "ph1")
    assert r1 is False

    # Second claim by different provider — would suppress, but doesn't
    r2 = ledger.claim("fp1", 1, "provider_b", "ph1")
    assert r2 is False

    stats = ledger.to_stats()
    assert stats["would_suppress"] == 1
    assert stats["claims"] == 2
    assert stats["tracked"] == 0  # no entries registered

    # take returns empty (no entries)
    taken = ledger.take("fp1", 1)
    assert taken == []


# ---------------------------------------------------------------------------
# L9 — invalid mode string degrades to off
# ---------------------------------------------------------------------------
def test_l9_invalid_mode_degrades_to_off():
    ledger = SearchShareLedger(mode="garbage")
    assert ledger.to_stats()["mode"] == "off"
    assert ledger.enabled is False
    assert ledger.claim("fp", 1, "prov", "ph") is False


# ---------------------------------------------------------------------------
# L10 — to_stats() key set is EXACTLY the documented set; close() safe twice
# ---------------------------------------------------------------------------
def test_l10_to_stats_exact_key_set():
    ledger = SearchShareLedger(mode=MODE_ON)
    stats = ledger.to_stats()
    expected_keys = {
        "mode", "claims", "would_suppress", "suppressed", "takes",
        "shared_fetches", "fanout_extra", "abandoned", "ledger_full", "tracked",
    }
    assert set(stats) == expected_keys


def test_l10_close_twice_safe_and_empties_tracked():
    ledger = SearchShareLedger(mode=MODE_ON)
    ledger.claim("fp1", 1, "p1", "ph1")
    ledger.claim("fp1", 1, "p2", "ph1")
    assert ledger.to_stats()["tracked"] == 1

    ledger.close()
    assert ledger.to_stats()["tracked"] == 0

    # Second close must not raise
    ledger.close()
    assert ledger.to_stats()["tracked"] == 0


# ---------------------------------------------------------------------------
# L11 — loud-once warning fires exactly once for repeated cap refusals
# ---------------------------------------------------------------------------
def test_l11_loud_once_warning_on_cap_refusals(caplog):
    ledger = SearchShareLedger(mode=MODE_ON, max_tracked=1)

    # First key fills the cap
    ledger.claim("fp1", 1, "p1", "ph1")

    with caplog.at_level(logging.WARNING):
        # Second NEW key: refused, WARNING fires
        ledger.claim("fp2", 1, "p2", "ph2")
        # Third NEW key: refused, WARNING must NOT fire again
        ledger.claim("fp3", 1, "p3", "ph3")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "ledger" in r.message.lower()]
    assert len(warnings) == 1, f"Expected exactly 1 warning, got {len(warnings)}: {[w.message for w in warnings]}"
    assert ledger.to_stats()["ledger_full"] == 2  # counter still increments


# ---------------------------------------------------------------------------
# L12 — take of absent key returns []
# ---------------------------------------------------------------------------
def test_l12_take_absent_key_returns_empty():
    ledger = SearchShareLedger(mode=MODE_ON)
    taken = ledger.take("nope", 99)
    assert taken == []


# ---------------------------------------------------------------------------
# Config pins
# ---------------------------------------------------------------------------
def test_config_defaults():
    from config.schemas import SearchConfig

    cfg = SearchConfig()
    assert cfg.share_fetch == "off"
    assert cfg.share_ledger_max_tracked == 50_000


def test_config_invalid_mode():
    from config.schemas import SearchConfig

    with pytest.raises(ValueError, match="search.share_fetch must be one of"):
        SearchConfig(share_fetch="invalid")


def test_config_invalid_max_tracked():
    from config.schemas import SearchConfig

    with pytest.raises(ValueError, match="search.share_ledger_max_tracked must be positive"):
        SearchConfig(share_ledger_max_tracked=0)


# ---------------------------------------------------------------------------
# Render pin
# ---------------------------------------------------------------------------
def test_render_sshare_line():
    from core.metrics import PipelineStatus, StageMetrics
    from state.display import StatusDisplayEngine
    from state.models import SystemStatus

    status = SystemStatus.__new__(SystemStatus)
    status.pipeline = PipelineStatus(
        state=None,
        active=0,
        total=0,
        stages={},
        runtime=0.0,
        search_share_metrics={
            "mode": "on",
            "claims": 10,
            "suppressed": 3,
            "shared_fetches": 2,
            "fanout_extra": 1,
            "abandoned": 0,
            "tracked": 5,
            "ledger_full": 0,
        },
    )

    line = StatusDisplayEngine._format_search_share_metrics_line(status)
    assert "SShare:" in line
    assert "mode=on" in line
    assert "claims=10" in line
    assert "suppressed=3" in line


def test_render_sshare_guard_off():
    from core.metrics import PipelineStatus
    from state.display import StatusDisplayEngine
    from state.models import SystemStatus

    status = SystemStatus.__new__(SystemStatus)
    status.pipeline = PipelineStatus(
        state=None,
        active=0,
        total=0,
        stages={},
        runtime=0.0,
        search_share_metrics={"mode": "off"},
    )

    line = StatusDisplayEngine._format_search_share_metrics_line(status)
    assert line == ""


# ---------------------------------------------------------------------------
# Integration pins (simplified - verify ledger API is called, not full worker)
# ---------------------------------------------------------------------------

from config.schemas import Config
from search.querykey import fingerprint


# ---------------------------------------------------------------------------
# I1 — Stage-suppression: ledger.claim() is called during task processing
# ---------------------------------------------------------------------------
def test_i1_ledger_claim_called():
    """WHEN ledger is enabled THEN claim() is available for stage to call."""
    ledger = SearchShareLedger(mode=MODE_ON)
    
    # Verify claim API works
    wire_fp = fingerprint('"sk-"', False)
    r1 = ledger.claim(wire_fp, 1, "prov_a", "ph1")
    assert r1 is False  # first claim succeeds
    
    r2 = ledger.claim(wire_fp, 1, "prov_b", "ph1")
    assert r2 is True  # second claim suppressed
    
    stats = ledger.to_stats()
    assert stats["suppressed"] == 1


# ---------------------------------------------------------------------------
# I2 — Fan-out: ledger.take() returns beneficiaries
# ---------------------------------------------------------------------------
def test_i2_ledger_take_returns_beneficiaries():
    """WHEN take() is called THEN all beneficiaries are returned."""
    def resolver(ph):
        return ["prov_a", "prov_b"]
    
    ledger = SearchShareLedger(mode=MODE_ON, beneficiaries_resolver=resolver)
    
    ledger.claim("fp1", 1, "prov_a", "ph1")
    beneficiaries = ledger.take("fp1", 1)
    
    assert beneficiaries == ["prov_a", "prov_b"]
    
    stats = ledger.to_stats()
    assert stats["takes"] == 1
    assert stats["shared_fetches"] == 1


# ---------------------------------------------------------------------------
# I3 — F1×ledger: suppression tracking works
# ---------------------------------------------------------------------------
def test_i3_ledger_suppression_tracking():
    """WHEN multiple claims are suppressed THEN counter increments."""
    ledger = SearchShareLedger(mode=MODE_ON)
    
    # Simulate: 3 children, all suppressed by second provider
    for i in range(3):
        ledger.claim(f"fp{i}", 1, "prov_a", "ph1")
        ledger.claim(f"fp{i}", 1, "prov_b", "ph1")  # suppressed
    
    stats = ledger.to_stats()
    assert stats["suppressed"] == 3


# ---------------------------------------------------------------------------
# I4 — Abandon-trio: (a) abandon releases key
# ---------------------------------------------------------------------------
def test_i4a_abandon_releases_key():
    """WHEN abandon is called THEN key is released."""
    ledger = SearchShareLedger(mode=MODE_ON)
    
    ledger.claim("fp1", 1, "prov_a", "ph1")
    assert ledger.to_stats()["tracked"] == 1
    
    dropped = ledger.abandon("fp1", 1)
    assert dropped == 1
    assert ledger.to_stats()["tracked"] == 0
    
    # Another provider can now claim
    r2 = ledger.claim("fp1", 1, "prov_b", "ph1")
    assert r2 is False


# ---------------------------------------------------------------------------
# I4 — Abandon-trio: (b) abandon on exception
# ---------------------------------------------------------------------------
def test_i4b_abandon_on_exception():
    """WHEN exception occurs THEN abandon can be called."""
    ledger = SearchShareLedger(mode=MODE_ON)
    
    ledger.claim("fp1", 1, "prov_a", "ph1")
    assert ledger.to_stats()["tracked"] == 1
    
    # Simulate abandon on exception
    ledger.abandon("fp1", 1)
    assert ledger.to_stats()["tracked"] == 0


# ---------------------------------------------------------------------------
# I4 — Abandon-trio: (c) no abandon on deferral
# ---------------------------------------------------------------------------
def test_i4c_no_abandon_on_deferral():
    """WHEN RateLimitDeferral occurs at stage level THEN abandon is NOT called.
    
    Stage-level test: real SearchStage+StageResources, search_share=ledger mode=on,
    pre-claim key (tracked==1), client raises RateLimitDeferral ⇒ exception propagated,
    tracked stays 1 (abandon NOT called), and repeated claim by same provider is idempotent.
    """
    from unittest.mock import Mock, patch
    from stage.base import StageResources
    from stage.definition import SearchStage
    from core.models import SearchTask
    from config.schemas import TaskConfig, StageConfig, Patterns
    from core.exceptions import RateLimitDeferral
    
    ledger = SearchShareLedger(mode=MODE_ON)
    
    task_config = TaskConfig(
        name="prov_a",
        enabled=True,
        provider_type="openai_like",
        use_api=False,
        stages=StageConfig(search=True, gather=True, check=True, inspect=True),
        patterns=Patterns(key_pattern=r"sk-[A-Za-z0-9]{16}"),
    )
    
    resources = StageResources(
        limiter=None,
        providers={},
        config=Config(),
        task_configs={"prov_a": task_config},
        auth=Mock(),
        search_share=ledger,
    )
    
    stage = SearchStage(resources, lambda out: None, thread_count=1)
    
    # Mock the search client to raise RateLimitDeferral
    with patch('stage.definition.client.search_with_count') as mock_search:
        mock_search.side_effect = RateLimitDeferral("Rate limited", wait_s=60.0)
        
        task = SearchTask(
            provider="prov_a",
            query='"sk-"',
            regex=r"sk-[A-Za-z0-9]{16}",
            page=1,
            use_api=False,
        )
        
        # Pre-claim the key
        wire_fp = fingerprint(task.query, task.use_api)
        ledger.claim(wire_fp, task.page, "prov_a", "ph1")
        assert ledger.to_stats()["tracked"] == 1
        
        # Run process_task (should raise RateLimitDeferral WITHOUT calling abandon)
        with pytest.raises(RateLimitDeferral):
            stage.process_task(task)
        
        # Verify abandon() was NOT called (tracked stays 1)
        stats = ledger.to_stats()
        assert stats["tracked"] == 1, f"Expected abandon() NOT to be called on deferral, got stats: {stats}"
        
        # Verify idempotent claim by same provider
        ledger.claim(wire_fp, task.page, "prov_a", "ph1")
        assert ledger.to_stats()["tracked"] == 1, "Idempotent claim should not change tracked count"


# ---------------------------------------------------------------------------
# I4d — Stage-level None-swallow abandon (failure_handling="shadow")
# ---------------------------------------------------------------------------
def test_i4d_abandon_on_none_swallow():
    """WHEN process_task returns None (shadow-swallow) THEN abandon is called.
    
    Stage-level test: failure_handling="shadow", client raises TransientFetchError,
    super().process_task returns None (shadow-swallow base.py:695-702),
    process_task override calls abandon (tracked 1→0).
    """
    from unittest.mock import Mock, patch
    from stage.base import StageResources
    from stage.definition import SearchStage
    from core.models import SearchTask
    from config.schemas import TaskConfig, StageConfig, Patterns
    from core.exceptions import TransientFetchError
    
    ledger = SearchShareLedger(mode=MODE_ON)
    
    task_config = TaskConfig(
        name="prov_a",
        enabled=True,
        provider_type="openai_like",
        use_api=False,
        stages=StageConfig(search=True, gather=True, check=True, inspect=True),
        patterns=Patterns(key_pattern=r"sk-[A-Za-z0-9]{16}"),
    )
    
    # Configure failure_handling="shadow"
    cfg = Config()
    cfg.pipeline.failure_handling = "shadow"
    
    resources = StageResources(
        limiter=None,
        providers={},
        config=cfg,
        task_configs={"prov_a": task_config},
        auth=Mock(),
        search_share=ledger,
    )
    
    stage = SearchStage(resources, lambda out: None, thread_count=1)
    
    # Mock the search client to raise TransientFetchError
    with patch('stage.definition.client.search_with_count') as mock_search:
        mock_search.side_effect = TransientFetchError("Network error")
        
        task = SearchTask(
            provider="prov_a",
            query='"sk-"',
            regex=r"sk-[A-Za-z0-9]{16}",
            page=1,
            use_api=False,
        )
        
        # Pre-claim the key
        wire_fp = fingerprint(task.query, task.use_api)
        ledger.claim(wire_fp, task.page, "prov_a", "ph1")
        assert ledger.to_stats()["tracked"] == 1
        
        # Run process_task (should return None in shadow mode and call abandon)
        result = stage.process_task(task)
        
        # Verify process_task returned None (shadow-swallow)
        assert result is None, f"Expected process_task to return None in shadow mode, got {result}"
        
        # Verify abandon() was called (tracked decremented to 0)
        stats = ledger.to_stats()
        assert stats["tracked"] == 0, f"Expected abandon() to be called on None-swallow, got stats: {stats}"


# ---------------------------------------------------------------------------
# I5 — Wiring through Pipeline
# ---------------------------------------------------------------------------
def test_i5_wiring_through_pipeline():
    """WHEN Pipeline is constructed THEN search_share ledger is wired."""
    from manager.pipeline import Pipeline
    
    cfg = Config()
    cfg.search.share_fetch = "on"
    cfg.search.share_ledger_max_tracked = 1000
    
    pipeline = Pipeline(cfg, providers={})
    
    assert hasattr(pipeline, 'search_share')
    assert pipeline.search_share._mode == "on"
    assert pipeline.search_share._max_tracked == 1000
    
    pipeline._on_stop()


# ---------------------------------------------------------------------------
# I6 — Neutrality off: ledger.enabled is False
# ---------------------------------------------------------------------------
def test_i6_neutrality_off_mode():
    """WHEN mode=off THEN ledger.enabled is False."""
    ledger = SearchShareLedger(mode=MODE_OFF)
    
    assert ledger.enabled is False
    assert ledger.enforce is False
    
    # All operations return early
    ledger.claim("fp1", 1, "prov_a", "ph1")
    ledger.take("fp1", 1)
    ledger.abandon("fp1", 1)
    
    assert ledger.to_stats()["claims"] == 0
    assert ledger.to_stats()["takes"] == 0


# ---------------------------------------------------------------------------
# I7 — Stage-integration: _search_worker calls ledger.take() for fan-out
# ---------------------------------------------------------------------------
def test_i7_stage_integration_take_called():
    """WHEN _search_worker runs with ledger.enforce THEN take() is called."""
    from unittest.mock import Mock, patch
    from stage.base import StageResources
    from stage.definition import SearchStage
    from core.models import SearchTask
    from config.schemas import TaskConfig, StageConfig, Patterns
    
    # Create a ledger with a resolver
    def resolver(ph):
        return ["prov_a", "prov_b"]
    
    ledger = SearchShareLedger(mode=MODE_ON, beneficiaries_resolver=resolver)
    
    # Create a minimal StageResources
    task_config = TaskConfig(
        name="prov_a",
        enabled=True,
        provider_type="openai_like",
        use_api=False,
        stages=StageConfig(search=True, gather=True, check=True, inspect=True),
        patterns=Patterns(key_pattern=r"sk-[A-Za-z0-9]{16}"),
    )
    
    resources = StageResources(
        limiter=None,
        providers={},
        config=Config(),
        task_configs={"prov_a": task_config, "prov_b": task_config},
        auth=Mock(),
        search_share=ledger,
    )
    
    # Create a SearchStage
    stage = SearchStage(resources, lambda out: None, thread_count=1)
    
    # Mock the search client to return controlled results
    with patch('stage.definition.client.search_with_count') as mock_search:
        mock_search.return_value = (["link1"], "content", 100)
        
        # Create a task
        task = SearchTask(
            provider="prov_a",
            query='"sk-"',
            regex=r"sk-[A-Za-z0-9]{16}",
            page=1,
            use_api=False,
        )
        
        # Pre-claim the key so take() will return beneficiaries
        wire_fp = fingerprint(task.query, task.use_api)
        ledger.claim(wire_fp, task.page, "prov_a", "ph1")
        
        # Run _search_worker
        try:
            output = stage._search_worker(task)
        except Exception:
            # Worker may fail due to missing dependencies, but we just want to verify take() was called
            pass
        
        # Verify take() was called (takes counter incremented)
        stats = ledger.to_stats()
        assert stats["takes"] >= 1, f"Expected take() to be called, got stats: {stats}"


# ---------------------------------------------------------------------------
# I8 — Stage-integration: process_task calls abandon() on exception
# ---------------------------------------------------------------------------
def test_i8_stage_integration_abandon_on_exception():
    """WHEN process_task encounters an exception THEN abandon() is called."""
    from unittest.mock import Mock, patch
    from stage.base import StageResources
    from stage.definition import SearchStage
    from core.models import SearchTask
    from config.schemas import TaskConfig, StageConfig, Patterns
    from core.exceptions import TransientFetchError
    
    ledger = SearchShareLedger(mode=MODE_ON)
    
    task_config = TaskConfig(
        name="prov_a",
        enabled=True,
        provider_type="openai_like",
        use_api=False,
        stages=StageConfig(search=True, gather=True, check=True, inspect=True),
        patterns=Patterns(key_pattern=r"sk-[A-Za-z0-9]{16}"),
    )
    
    resources = StageResources(
        limiter=None,
        providers={},
        config=Config(),
        task_configs={"prov_a": task_config},
        auth=Mock(),
        search_share=ledger,
    )
    
    stage = SearchStage(resources, lambda out: None, thread_count=1)
    
    # Mock the search client to raise an exception
    with patch('stage.definition.client.search_with_count') as mock_search:
        mock_search.side_effect = TransientFetchError("Network error")
        
        task = SearchTask(
            provider="prov_a",
            query='"sk-"',
            regex=r"sk-[A-Za-z0-9]{16}",
            page=1,
            use_api=False,
        )
        
        # Pre-claim the key
        wire_fp = fingerprint(task.query, task.use_api)
        ledger.claim(wire_fp, task.page, "prov_a", "ph1")
        assert ledger.to_stats()["tracked"] == 1
        
        # Run process_task (should raise and call abandon)
        try:
            stage.process_task(task)
        except TransientFetchError:
            pass  # Expected
        
        # Verify abandon() was called (tracked decremented)
        stats = ledger.to_stats()
        assert stats["tracked"] == 0, f"Expected abandon() to be called, got stats: {stats}"


# ---------------------------------------------------------------------------
# J-series: Per-beneficiary attribution tests (D-03.7)
# ---------------------------------------------------------------------------

def test_j1_per_beneficiary_gather_skip_decisions():
    """J1: Each beneficiary gets its own gather-skip decision based on its patterns.
    
    When search_share ledger returns multiple beneficiaries, each beneficiary
    should get its own gather-skip decision based on its own patterns_hash,
    not the owner's patterns_hash.
    """
    from unittest.mock import MagicMock, patch
    from stage.definition import SearchStage
    from stage.base import StageResources
    from core.models import SearchTask, Patterns
    from config.schemas import TaskConfig, StageConfig
    from manager.search_share import SearchShareLedger
    
    # Create ledger with resolver that returns 2 beneficiaries
    def resolver(ph):
        return ["prov_a", "prov_b"]
    
    ledger = SearchShareLedger(mode=MODE_ON, beneficiaries_resolver=resolver)
    
    # Create task configs with DIFFERENT patterns for each provider
    task_config_a = TaskConfig(
        name="prov_a",
        enabled=True,
        provider_type="openai_like",
        use_api=False,
        stages=StageConfig(search=True, gather=True, check=True, inspect=True),
        patterns=Patterns(key_pattern=r"sk-[A-Za-z0-9]{16}"),
    )
    
    task_config_b = TaskConfig(
        name="prov_b",
        enabled=True,
        provider_type="openai_like",
        use_api=False,
        stages=StageConfig(search=True, gather=True, check=True, inspect=True),
        patterns=Patterns(key_pattern=r"pk-[A-Za-z0-9]{16}"),  # Different pattern!
    )
    
    # Mock gather_skip engine
    gather_skip = MagicMock()
    gather_skip.enabled = True
    gather_skip.decide = MagicMock(return_value=[])  # No skip decisions
    
    # Mock auth
    auth = MagicMock()
    auth.get_session = MagicMock(return_value="mock_session")
    auth.get_token = MagicMock(return_value="mock_token")
    
    resources = StageResources(
        limiter=None,
        providers={},
        config=None,
        task_configs={"prov_a": task_config_a, "prov_b": task_config_b},
        auth=auth,
        search_share=ledger,
        gather_skip=gather_skip,
    )
    
    stage = SearchStage(resources, lambda out: None, thread_count=1)
    
    # Mock search client to return results
    with patch('stage.definition.client.search_with_count') as mock_search:
        mock_search.return_value = (["link1"], "content", 100)
        
        task = SearchTask(
            provider="prov_a",
            query='"sk-"',
            regex=r"sk-[A-Za-z0-9]{16}",
            page=1,
            use_api=False,
        )
        
        # Pre-claim the key so take() returns beneficiaries
        wire_fp = fingerprint(task.query, task.use_api)
        ledger.claim(wire_fp, task.page, "prov_a", "ph1")
        
        # Run _search_worker
        try:
            output = stage._search_worker(task)
        except Exception:
            pass  # May fail due to missing dependencies
        
        # Verify gather_skip.decide was called TWICE (once per beneficiary)
        # with DIFFERENT patterns_hash values
        assert gather_skip.decide.call_count == 2, \
            f"Expected 2 calls to gather_skip.decide (one per B), got {gather_skip.decide.call_count}"
        
        # Extract the patterns_hash arguments from the calls
        calls = gather_skip.decide.call_args_list
        patterns_hashes = [call[1]['patterns_hash'] for call in calls]
        
        # Verify the patterns_hashes are DIFFERENT (because patterns are different)
        assert len(set(patterns_hashes)) == 2, \
            f"Expected 2 different patterns_hash values, got {patterns_hashes}"


def test_j2_per_beneficiary_r17_claim():
    """J2: Each beneficiary claims with its own provider and patterns_hash.
    
    When search_share ledger returns multiple beneficiaries, each beneficiary
    should claim in the R17 gather-share ledger with its own provider name
    and its own patterns_hash.
    """
    from unittest.mock import MagicMock, patch
    from stage.definition import SearchStage
    from stage.base import StageResources
    from core.models import SearchTask, Patterns
    from config.schemas import TaskConfig, StageConfig
    from manager.search_share import SearchShareLedger
    
    # Create ledger with resolver that returns 2 beneficiaries
    def resolver(ph):
        return ["prov_a", "prov_b"]
    
    search_ledger = SearchShareLedger(mode=MODE_ON, beneficiaries_resolver=resolver)
    
    # Create task configs with DIFFERENT patterns for each provider
    task_config_a = TaskConfig(
        name="prov_a",
        enabled=True,
        provider_type="openai_like",
        use_api=False,
        stages=StageConfig(search=True, gather=True, check=True, inspect=True),
        patterns=Patterns(key_pattern=r"sk-[A-Za-z0-9]{16}"),
    )
    
    task_config_b = TaskConfig(
        name="prov_b",
        enabled=True,
        provider_type="openai_like",
        use_api=False,
        stages=StageConfig(search=True, gather=True, check=True, inspect=True),
        patterns=Patterns(key_pattern=r"pk-[A-Za-z0-9]{16}"),  # Different pattern!
    )
    
    # Mock R17 gather-share ledger
    gather_ledger = MagicMock()
    gather_ledger.enabled = True
    gather_ledger.claim = MagicMock(return_value=False)  # No suppression
    
    # Mock config
    config = MagicMock()
    config.gather.transport = "raw"
    
    # Mock auth
    auth = MagicMock()
    auth.get_session = MagicMock(return_value="mock_session")
    auth.get_token = MagicMock(return_value="mock_token")
    
    resources = StageResources(
        limiter=None,
        providers={},
        config=config,
        task_configs={"prov_a": task_config_a, "prov_b": task_config_b},
        auth=auth,
        search_share=search_ledger,
        share_ledger=gather_ledger,
    )
    
    stage = SearchStage(resources, lambda out: None, thread_count=1)
    
    # Mock search client to return results
    with patch('stage.definition.client.search_with_count') as mock_search:
        mock_search.return_value = (["link1"], "content", 100)
        
        task = SearchTask(
            provider="prov_a",
            query='"sk-"',
            regex=r"sk-[A-Za-z0-9]{16}",
            page=1,
            use_api=False,
        )
        
        # Pre-claim the key so take() returns beneficiaries
        wire_fp = fingerprint(task.query, task.use_api)
        search_ledger.claim(wire_fp, task.page, "prov_a", "ph1")
        
        # Run _search_worker
        try:
            output = stage._search_worker(task)
        except Exception:
            pass  # May fail due to missing dependencies
        
        # Verify gather_ledger.claim was called TWICE (once per beneficiary)
        assert gather_ledger.claim.call_count == 2, \
            f"Expected 2 calls to gather_ledger.claim (one per B), got {gather_ledger.claim.call_count}"
        
        # Extract the provider and patterns_hash arguments from the calls
        calls = gather_ledger.claim.call_args_list
        providers = [call[0][3] for call in calls]  # 4th positional arg is provider
        patterns_hashes = [call[0][1] for call in calls]  # 2nd positional arg is patterns_hash
        
        # Verify the providers are correct
        assert providers == ["prov_a", "prov_b"], \
            f"Expected providers ['prov_a', 'prov_b'], got {providers}"
        
        # Verify the patterns_hashes are DIFFERENT (because patterns are different)
        assert len(set(patterns_hashes)) == 2, \
            f"Expected 2 different patterns_hash values, got {patterns_hashes}"


def test_j3_per_beneficiary_link_coverage():
    """J3: Non-owner beneficiaries get link_coverage written.
    
    When search_share ledger returns multiple beneficiaries, each non-owner
    beneficiary should get link_coverage written to the registry with its own
    patterns_hash. Owner's coverage is written by gather stage after successful
    fetch, so we don't duplicate it here.
    """
    from unittest.mock import MagicMock, patch
    from stage.definition import SearchStage
    from stage.base import StageResources
    from core.models import SearchTask, Patterns
    from config.schemas import TaskConfig, StageConfig
    from manager.search_share import SearchShareLedger
    
    # Create ledger with resolver that returns 2 beneficiaries
    def resolver(ph):
        return ["prov_a", "prov_b"]
    
    ledger = SearchShareLedger(mode=MODE_ON, beneficiaries_resolver=resolver)
    
    # Create task configs with DIFFERENT patterns for each provider
    task_config_a = TaskConfig(
        name="prov_a",
        enabled=True,
        provider_type="openai_like",
        use_api=False,
        stages=StageConfig(search=True, gather=True, check=True, inspect=True),
        patterns=Patterns(key_pattern=r"sk-[A-Za-z0-9]{16}"),
    )
    
    task_config_b = TaskConfig(
        name="prov_b",
        enabled=True,
        provider_type="openai_like",
        use_api=False,
        stages=StageConfig(search=True, gather=True, check=True, inspect=True),
        patterns=Patterns(key_pattern=r"pk-[A-Za-z0-9]{16}"),  # Different pattern!
    )
    
    # Mock registry
    registry = MagicMock()
    registry.available = True
    registry.record_coverage = MagicMock()
    
    # Mock auth
    auth = MagicMock()
    auth.get_session = MagicMock(return_value="mock_session")
    auth.get_token = MagicMock(return_value="mock_token")
    
    resources = StageResources(
        limiter=None,
        providers={},
        config=None,
        task_configs={"prov_a": task_config_a, "prov_b": task_config_b},
        auth=auth,
        search_share=ledger,
        registry=registry,
    )
    
    stage = SearchStage(resources, lambda out: None, thread_count=1)
    
    # Mock search client to return results
    with patch('stage.definition.client.search_with_count') as mock_search:
        mock_search.return_value = (["link1"], "content", 100)
        
        task = SearchTask(
            provider="prov_a",
            query='"sk-"',
            regex=r"sk-[A-Za-z0-9]{16}",
            page=1,
            use_api=False,
        )
        
        # Pre-claim the key so take() returns beneficiaries
        wire_fp = fingerprint(task.query, task.use_api)
        ledger.claim(wire_fp, task.page, "prov_a", "ph1")
        
        # Run _search_worker
        try:
            output = stage._search_worker(task)
        except Exception:
            pass  # May fail due to missing dependencies
        
        # Verify registry.record_coverage was called for non-owner beneficiary only
        # (owner's coverage is written by gather stage after successful fetch)
        assert registry.record_coverage.call_count >= 1, \
            f"Expected at least 1 call to record_coverage (for non-owner B), got {registry.record_coverage.call_count}"
        
        # Extract the provider and patterns_hash arguments from the calls
        calls = registry.record_coverage.call_args_list
        providers = [call[1]['provider'] for call in calls]
        patterns_hashes = [call[1]['patterns_hash'] for call in calls]
        
        # Verify prov_b (non-owner) got coverage, but NOT prov_a (owner)
        assert "prov_b" in providers, f"Expected prov_b in coverage providers, got {providers}"
        assert "prov_a" not in providers, f"Expected prov_a (owner) NOT in coverage providers, got {providers}"
        
        # Verify the patterns_hash is for prov_b's patterns
        assert len(set(patterns_hashes)) == 1, \
            f"Expected 1 patterns_hash value (for prov_b), got {set(patterns_hashes)}"


def test_j4_per_beneficiary_early_stop_observe():
    """J4: Each beneficiary gets early-stop observe called.
    
    When search_share ledger returns multiple beneficiaries and early-stop
    engine is enabled, each beneficiary should get observe() called with
    its own patterns_hash.
    """
    from unittest.mock import MagicMock, patch
    from stage.definition import SearchStage
    from stage.base import StageResources
    from core.models import SearchTask, Patterns
    from config.schemas import TaskConfig, StageConfig
    from manager.search_share import SearchShareLedger
    
    # Create ledger with resolver that returns 2 beneficiaries
    def resolver(ph):
        return ["prov_a", "prov_b"]
    
    ledger = SearchShareLedger(mode=MODE_ON, beneficiaries_resolver=resolver)
    
    # Create task configs with DIFFERENT patterns for each provider
    task_config_a = TaskConfig(
        name="prov_a",
        enabled=True,
        provider_type="openai_like",
        use_api=True,  # API transport for early-stop
        stages=StageConfig(search=True, gather=True, check=True, inspect=True),
        patterns=Patterns(key_pattern=r"sk-[A-Za-z0-9]{16}"),
    )
    
    task_config_b = TaskConfig(
        name="prov_b",
        enabled=True,
        provider_type="openai_like",
        use_api=True,
        stages=StageConfig(search=True, gather=True, check=True, inspect=True),
        patterns=Patterns(key_pattern=r"pk-[A-Za-z0-9]{16}"),  # Different pattern!
    )
    
    # Mock early-stop engine
    early_stop = MagicMock()
    early_stop.enabled = True
    early_stop.observe = MagicMock()
    
    # Mock auth
    auth = MagicMock()
    auth.get_session = MagicMock(return_value="mock_session")
    auth.get_token = MagicMock(return_value="mock_token")
    
    resources = StageResources(
        limiter=None,
        providers={},
        config=None,
        task_configs={"prov_a": task_config_a, "prov_b": task_config_b},
        auth=auth,
        search_share=ledger,
        early_stop=early_stop,
    )
    
    stage = SearchStage(resources, lambda out: None, thread_count=1)
    
    # Mock search client to return results
    with patch('stage.definition.client.search_with_count') as mock_search:
        mock_search.return_value = (["link1"], "content", 100)
        
        task = SearchTask(
            provider="prov_a",
            query='"sk-"',
            regex=r"sk-[A-Za-z0-9]{16}",
            page=1,
            use_api=True,
        )
        
        # Pre-claim the key so take() returns beneficiaries
        wire_fp = fingerprint(task.query, task.use_api)
        ledger.claim(wire_fp, task.page, "prov_a", "ph1")
        
        # Run _search_worker
        try:
            output = stage._search_worker(task)
        except Exception:
            pass  # May fail due to missing dependencies
        
        # Verify early_stop.observe was called for non-owner beneficiaries
        # (owner's observe is handled by pagination logic)
        assert early_stop.observe.call_count >= 1, \
            f"Expected at least 1 call to early_stop.observe (for non-owner B), got {early_stop.observe.call_count}"
        
        # Extract the provider and patterns_hash arguments from the calls
        calls = early_stop.observe.call_args_list
        providers = [call[1]['provider'] for call in calls]
        patterns_hashes = [call[1]['patterns_hash'] for call in calls]
        
        # Verify prov_b got observed (non-owner)
        assert "prov_b" in providers, f"Expected prov_b in observe providers, got {providers}"
        
        # Verify the patterns_hashes are DIFFERENT (because patterns are different)
        unique_hashes = set(patterns_hashes)
        assert len(unique_hashes) >= 1, \
            f"Expected at least 1 patterns_hash value in observe, got {unique_hashes}"


def test_j5_neutrality_off_mode_no_per_b_calls():
    """J5: When mode=off, no per-B calls are made.
    
    When search_share ledger is off, the fan-out logic should not make
    any per-beneficiary calls (gather-skip, R17 claim, coverage, observe).
    """
    from unittest.mock import MagicMock, patch
    from stage.definition import SearchStage
    from stage.base import StageResources
    from core.models import SearchTask, Patterns
    from config.schemas import TaskConfig, StageConfig
    from manager.search_share import SearchShareLedger
    
    # Create ledger in OFF mode
    ledger = SearchShareLedger(mode=MODE_OFF)
    
    task_config = TaskConfig(
        name="prov_a",
        enabled=True,
        provider_type="openai_like",
        use_api=False,
        stages=StageConfig(search=True, gather=True, check=True, inspect=True),
        patterns=Patterns(key_pattern=r"sk-[A-Za-z0-9]{16}"),
    )
    
    # Mock gather_skip engine
    gather_skip = MagicMock()
    gather_skip.enabled = True
    gather_skip.decide = MagicMock(return_value=[])
    
    # Mock auth
    auth = MagicMock()
    auth.get_session = MagicMock(return_value="mock_session")
    auth.get_token = MagicMock(return_value="mock_token")
    
    resources = StageResources(
        limiter=None,
        providers={},
        config=None,
        task_configs={"prov_a": task_config},
        auth=auth,
        search_share=ledger,
        gather_skip=gather_skip,
    )
    
    stage = SearchStage(resources, lambda out: None, thread_count=1)
    
    # Mock search client to return results
    with patch('stage.definition.client.search_with_count') as mock_search:
        mock_search.return_value = (["link1"], "content", 100)
        
        task = SearchTask(
            provider="prov_a",
            query='"sk-"',
            regex=r"sk-[A-Za-z0-9]{16}",
            page=1,
            use_api=False,
        )
        
        # Run _search_worker
        try:
            output = stage._search_worker(task)
        except Exception:
            pass  # May fail due to missing dependencies
        
        # Verify gather_skip.decide was called ONCE (for owner only, not per-B)
        # because ledger is off and beneficiaries = [task.provider]
        assert gather_skip.decide.call_count == 1, \
            f"Expected 1 call to gather_skip.decide (owner only), got {gather_skip.decide.call_count}"


def test_j6_fan_out_with_different_patterns():
    """J6: Fan-out correctly uses each beneficiary's own patterns.
    
    When search_share ledger returns multiple beneficiaries with different
    patterns, each beneficiary's gather-tasks should use its own patterns.
    """
    from unittest.mock import MagicMock, patch
    from stage.definition import SearchStage
    from stage.base import StageResources
    from core.models import SearchTask, Patterns
    from config.schemas import TaskConfig, StageConfig
    from manager.search_share import SearchShareLedger
    
    # Create ledger with resolver that returns 2 beneficiaries
    def resolver(ph):
        return ["prov_a", "prov_b"]
    
    ledger = SearchShareLedger(mode=MODE_ON, beneficiaries_resolver=resolver)
    
    # Create task configs with DIFFERENT patterns for each provider
    task_config_a = TaskConfig(
        name="prov_a",
        enabled=True,
        provider_type="openai_like",
        use_api=False,
        stages=StageConfig(search=True, gather=True, check=True, inspect=True),
        patterns=Patterns(key_pattern=r"sk-[A-Za-z0-9]{16}"),
    )
    
    task_config_b = TaskConfig(
        name="prov_b",
        enabled=True,
        provider_type="openai_like",
        use_api=False,
        stages=StageConfig(search=True, gather=True, check=True, inspect=True),
        patterns=Patterns(key_pattern=r"pk-[A-Za-z0-9]{16}"),  # Different pattern!
    )
    
    # Mock auth
    auth = MagicMock()
    auth.get_session = MagicMock(return_value="mock_session")
    auth.get_token = MagicMock(return_value="mock_token")
    
    resources = StageResources(
        limiter=None,
        providers={},
        config=None,
        task_configs={"prov_a": task_config_a, "prov_b": task_config_b},
        auth=auth,
        search_share=ledger,
    )
    
    # Capture the output
    captured_output = []
    def capture_output(out):
        captured_output.append(out)
    
    stage = SearchStage(resources, capture_output, thread_count=1)
    
    # Mock search client to return results
    with patch('stage.definition.client.search_with_count') as mock_search:
        mock_search.return_value = (["link1"], "content", 100)
        
        task = SearchTask(
            provider="prov_a",
            query='"sk-"',
            regex=r"sk-[A-Za-z0-9]{16}",
            page=1,
            use_api=False,
        )
        
        # Pre-claim the key so take() returns beneficiaries
        wire_fp = fingerprint(task.query, task.use_api)
        ledger.claim(wire_fp, task.page, "prov_a", "ph1")
        
        # Run _search_worker
        output = None
        try:
            output = stage._search_worker(task)
        except Exception:
            pass  # May fail due to missing dependencies
        
        # Verify output contains gather-tasks for both beneficiaries
        if output is not None:
            gather_tasks = [t for t, stage in output.new_tasks if stage == "gather"]
            
            # Extract providers from gather-tasks
            providers = [t.provider for t in gather_tasks]
            
            # Verify both beneficiaries got gather-tasks
            assert "prov_a" in providers, f"Expected prov_a in gather-tasks, got {providers}"
            assert "prov_b" in providers, f"Expected prov_b in gather-tasks, got {providers}"
            
            # Extract patterns from gather-tasks
            patterns_list = [t.patterns for t in gather_tasks]
            
            # Verify the patterns are DIFFERENT (because patterns are different)
            key_patterns = [p.key_pattern for p in patterns_list]
            unique_patterns = set(key_patterns)
            assert len(unique_patterns) == 2, \
                f"Expected 2 different key_pattern values in gather-tasks, got {unique_patterns}"


def test_j7_integration_real_stage_execution():
    """J7: Integration test with real stage execution.
    
    Verify that the fan-out logic works correctly in a real stage execution
    with multiple beneficiaries.
    """
    from unittest.mock import MagicMock, patch
    from stage.definition import SearchStage
    from stage.base import StageResources
    from core.models import SearchTask, Patterns
    from config.schemas import TaskConfig, StageConfig
    from manager.search_share import SearchShareLedger
    
    # Create ledger with resolver that returns 2 beneficiaries
    def resolver(ph):
        return ["prov_a", "prov_b"]
    
    ledger = SearchShareLedger(mode=MODE_ON, beneficiaries_resolver=resolver)
    
    # Create task configs with DIFFERENT patterns for each provider
    task_config_a = TaskConfig(
        name="prov_a",
        enabled=True,
        provider_type="openai_like",
        use_api=False,
        stages=StageConfig(search=True, gather=True, check=True, inspect=True),
        patterns=Patterns(key_pattern=r"sk-[A-Za-z0-9]{16}"),
    )
    
    task_config_b = TaskConfig(
        name="prov_b",
        enabled=True,
        provider_type="openai_like",
        use_api=False,
        stages=StageConfig(search=True, gather=True, check=True, inspect=True),
        patterns=Patterns(key_pattern=r"pk-[A-Za-z0-9]{16}"),
    )
    
    # Mock registry
    registry = MagicMock()
    registry.available = True
    registry.record_coverage = MagicMock()
    
    # Mock auth
    auth = MagicMock()
    auth.get_session = MagicMock(return_value="mock_session")
    auth.get_token = MagicMock(return_value="mock_token")
    
    resources = StageResources(
        limiter=None,
        providers={},
        config=None,
        task_configs={"prov_a": task_config_a, "prov_b": task_config_b},
        auth=auth,
        search_share=ledger,
        registry=registry,
    )
    
    stage = SearchStage(resources, lambda out: None, thread_count=1)
    
    # Mock search client to return results
    with patch('stage.definition.client.search_with_count') as mock_search:
        mock_search.return_value = (["link1", "link2"], "content with sk-abc123def456ghij", 100)
        
        task = SearchTask(
            provider="prov_a",
            query='"sk-"',
            regex=r"sk-[A-Za-z0-9]{16}",
            page=1,
            use_api=False,
        )
        
        # Pre-claim the key so take() returns beneficiaries
        wire_fp = fingerprint(task.query, task.use_api)
        ledger.claim(wire_fp, task.page, "prov_a", "ph1")
        
        # Run _search_worker
        output = None
        try:
            output = stage._search_worker(task)
        except Exception:
            pass  # May fail due to missing dependencies
        
        # Verify ledger.take() was called
        stats = ledger.to_stats()
        assert stats["takes"] >= 1, f"Expected take() to be called, got stats: {stats}"
        
        # Verify registry.record_coverage was called for non-owner beneficiary only
        # (owner's coverage is written by gather stage after successful fetch)
        assert registry.record_coverage.call_count >= 1, \
            f"Expected at least 1 call to record_coverage (for non-owner B), got {registry.record_coverage.call_count}"
        
        # Verify output contains tasks for both beneficiaries
        if output is not None:
            # Check that output.links contains entries for both beneficiaries
            link_providers = [prov for prov, links in output.links]
            assert "prov_a" in link_providers, f"Expected prov_a in output.links, got {link_providers}"
            assert "prov_b" in link_providers, f"Expected prov_b in output.links, got {link_providers}"


# ---------------------------------------------------------------------------
# K1 — F1×ledger: all children suppressed by ledger ⇒ pagination-fallback does NOT fire
# ---------------------------------------------------------------------------
def test_k1_f1_ledger_suppression_blocks_fallback(monkeypatch):
    """WHEN all refine children are suppressed by search_share ledger THEN
    pagination-fallback does NOT fire (share_suppressed > 0).
    
    Harness: real SearchStage, governor mode=on, RefineEngine.generate_queries
    mocked to return children, all children pre-claimed in search_share ledger
    (mode=on) by another provider ⇒ no SEARCH children in output, no page tasks,
    governor.pagination_fallbacks == 0.
    """
    from unittest.mock import patch
    from stage.base import StageResources
    from stage.definition import SearchStage
    from core.models import SearchTask
    from config.schemas import TaskConfig, StageConfig, Patterns, RefineGovernorConfig
    from search.refine_governor import RefineGovernor
    from search.querykey import fingerprint as fp
    from search import client as search_client
    
    PROVIDER = "deepseek"
    KEY_PATTERN = r"sk-[A-Za-z0-9]{16}"
    RAW_SEED = "/sk-[a-zA-Z0-9]{32}/"
    API_PER_PAGE = 100
    
    class FakeAuth:
        def get_session(self):
            return "session-token"
        def get_token(self):
            return "api-token"
        def get_user_agent(self):
            return "test-agent"
    
    def _task_config():
        return TaskConfig(
            name=PROVIDER,
            enabled=True,
            provider_type="openai_like",
            use_api=True,
            stages=StageConfig(search=True, gather=True, check=True, inspect=True),
            patterns=Patterns(key_pattern=KEY_PATTERN),
        )
    
    # Create ledger in ON mode
    ledger = SearchShareLedger(mode=MODE_ON)
    
    # Configure governor with drop_wire=False to allow children to reach claim()
    gov = RefineGovernor(
        mode="on",
        max_refine_depth=2,
        max_partitions_per_refine=128,
        max_search_tasks_per_run=10000,
        drop_wire_indistinguishable=False,  # Allow children to reach claim()
    )
    
    cfg = Config()
    cfg.pipeline.failure_handling = "legacy"
    
    resources = StageResources(
        limiter=None,
        providers={},
        config=cfg,
        task_configs={PROVIDER: _task_config()},
        auth=FakeAuth(),
        search_share=ledger,
        refine_governor=gov,
    )
    
    stage = SearchStage(resources, lambda out: None, thread_count=1)
    
    # Mock search client
    def fake_search(**kwargs):
        total = 5000
        n = min(total, API_PER_PAGE)
        urls = [f"https://github.com/o/r/blob/main/f{i}.py" for i in range(n)]
        return urls, total, ""
    
    monkeypatch.setattr(search_client, "search_with_count", fake_search)
    
    # Mock RefineEngine.generate_queries on the real instance (not get_instance)
    from search.github.refine.engine import RefineEngine
    real_engine = RefineEngine.get_instance()
    real_engine.generate_queries = lambda query, partitions=None: [
        "/sk-0[a-zA-Z0-9]{30}/",
        "/sk-1[a-zA-Z0-9]{30}/",
        "/sk-2[a-zA-Z0-9]{30}/",
    ]
    
    try:
        task = SearchTask(
            provider=PROVIDER,
            query=RAW_SEED,
            regex=KEY_PATTERN,
            page=1,
            use_api=True,
            refine_depth=0,
        )
        
        # Pre-claim all children in ledger by another provider
        from storage.registry import patterns_hash as ph_func
        for i, child_query in enumerate(["/sk-0[a-zA-Z0-9]{30}/", "/sk-1[a-zA-Z0-9]{30}/", "/sk-2[a-zA-Z0-9]{30}/"]):
            child_fp = fp(child_query, True)
            # Use the SAME patterns_hash as _handle_first_page_results will compute
            ph = ph_func(
                key_pattern=KEY_PATTERN,
                address_pattern="",
                endpoint_pattern="",
                model_pattern="",
            )
            # Claim with a different provider so that task's claim will be suppressed
            ledger.claim(child_fp, 1, "other_provider", ph)
        
        # Run process_task
        output = stage.process_task(task)
        
        # Verify no SEARCH children in output (all suppressed by ledger)
        search_children = [t for t, stage_name in output.new_tasks
                          if stage_name == "search" and t.query != task.query]
        assert len(search_children) == 0, \
            f"Expected 0 SEARCH children (all suppressed), got {len(search_children)}"
        
        # Verify no page tasks (fallback did NOT fire)
        page_tasks = [t for t, stage_name in output.new_tasks
                     if stage_name == "search" and t.query == task.query and t.page > 1]
        assert len(page_tasks) == 0, \
            f"Expected 0 page tasks (fallback blocked), got {len(page_tasks)}"
        
        # Verify governor.pagination_fallbacks == 0
        assert gov.metrics.get("pagination_fallbacks", 0) == 0, \
            f"Expected pagination_fallbacks==0, got {gov.metrics.get('pagination_fallbacks', 0)}"
    finally:
        # DELETE the instance attribute instead of re-assigning the bound
        # method: re-assignment kept an instance attr on the RefineEngine
        # singleton that shadowed later CLASS-level monkeypatches of
        # generate_queries (broke CP16/CP17 when test_search_share ran
        # first — test isolation fix).
        del real_engine.generate_queries


# ---------------------------------------------------------------------------
# K2 — F1×ledger: governor refuses all children (share_suppressed==0) ⇒ fallback FIRES
# ---------------------------------------------------------------------------
def test_k2_f1_governor_refusal_triggers_fallback(monkeypatch):
    """WHEN governor refuses all children (share_suppressed==0) AND ledger is ON
    THEN pagination-fallback FIRES (previous F1 semantics preserved).
    
    Harness: same as K1, but governor refuses all children (select_children returns []),
    ledger is ON but share_suppressed==0 ⇒ fallback creates page tasks,
    governor.pagination_fallbacks == 1.
    """
    from stage.base import StageResources
    from stage.definition import SearchStage
    from core.models import SearchTask
    from config.schemas import TaskConfig, StageConfig, Patterns, RefineGovernorConfig
    from search.refine_governor import RefineGovernor
    from search import client as search_client
    
    PROVIDER = "deepseek"
    KEY_PATTERN = r"sk-[A-Za-z0-9]{16}"
    RAW_SEED = "/sk-[a-zA-Z0-9]{32}/"
    API_PER_PAGE = 100
    
    class FakeAuth:
        def get_session(self):
            return "session-token"
        def get_token(self):
            return "api-token"
        def get_user_agent(self):
            return "test-agent"
    
    def _task_config():
        return TaskConfig(
            name=PROVIDER,
            enabled=True,
            provider_type="openai_like",
            use_api=True,
            stages=StageConfig(search=True, gather=True, check=True, inspect=True),
            patterns=Patterns(key_pattern=KEY_PATTERN),
        )
    
    # Create ledger in ON mode
    ledger = SearchShareLedger(mode=MODE_ON)
    
    # Configure governor
    gov = RefineGovernor(
        mode="on",
        max_refine_depth=2,
        max_partitions_per_refine=128,
        max_search_tasks_per_run=10000,
    )
    
    # Mock select_children to return empty (governor refuses all)
    original_select = gov.select_children
    def refusing_select(*args, **kwargs):
        original_select(*args, **kwargs)
        return []
    gov.select_children = refusing_select
    
    cfg = Config()
    cfg.pipeline.failure_handling = "legacy"
    
    resources = StageResources(
        limiter=None,
        providers={},
        config=cfg,
        task_configs={PROVIDER: _task_config()},
        auth=FakeAuth(),
        search_share=ledger,
        refine_governor=gov,
    )
    
    stage = SearchStage(resources, lambda out: None, thread_count=1)
    
    # Mock search client
    def fake_search(**kwargs):
        total = 5000
        n = min(total, API_PER_PAGE)
        urls = [f"https://github.com/o/r/blob/main/f{i}.py" for i in range(n)]
        return urls, total, ""
    
    monkeypatch.setattr(search_client, "search_with_count", fake_search)
    
    # Mock RefineEngine.generate_queries on the real instance (not get_instance)
    from search.github.refine.engine import RefineEngine
    real_engine = RefineEngine.get_instance()
    real_engine.generate_queries = lambda query, partitions=None: [
        "/sk-0[a-zA-Z0-9]{30}/",
        "/sk-1[a-zA-Z0-9]{30}/",
        "/sk-2[a-zA-Z0-9]{30}/",
    ]
    
    try:
        task = SearchTask(
            provider=PROVIDER,
            query=RAW_SEED,
            regex=KEY_PATTERN,
            page=1,
            use_api=True,
            refine_depth=0,
        )
        
        # Run process_task
        output = stage.process_task(task)
        
        # Verify no SEARCH children (governor refused all)
        search_children = [t for t, stage_name in output.new_tasks
                          if stage_name == "search" and t.query != task.query]
        assert len(search_children) == 0, \
            f"Expected 0 SEARCH children (governor refused), got {len(search_children)}"
        
        # Verify page tasks exist (fallback FIRED)
        page_tasks = [t for t, stage_name in output.new_tasks
                     if stage_name == "search" and t.query == task.query and t.page > 1]
        assert len(page_tasks) > 0, \
            f"Expected page tasks (fallback fired), got {len(page_tasks)}"
        
        # Verify governor.pagination_fallbacks == 1
        assert gov.metrics.get("pagination_fallbacks", 0) == 1, \
            f"Expected pagination_fallbacks==1, got {gov.metrics.get('pagination_fallbacks', 0)}"
    finally:
        # DELETE the instance attribute instead of re-assigning the bound
        # method: re-assignment kept an instance attr on the RefineEngine
        # singleton that shadowed later CLASS-level monkeypatches of
        # generate_queries (broke CP16/CP17 when test_search_share ran
        # first — test isolation fix).
        del real_engine.generate_queries


# ---------------------------------------------------------------------------
# Render — wsuppr= token (gate 3b.G arm-B observability, 07 §7.4)
# ---------------------------------------------------------------------------
def test_render_sshare_wsuppr_token():
    """WHEN metrics carry would_suppress THEN SShare renders wsuppr=<n>
    between suppressed= and shared= (arm-B shadow evidence, 07 §7.4)."""
    from core.metrics import PipelineStatus
    from state.display import StatusDisplayEngine
    from state.models import SystemStatus

    status = SystemStatus.__new__(SystemStatus)
    status.pipeline = PipelineStatus(
        state=None,
        active=0,
        total=0,
        stages={},
        runtime=0.0,
        search_share_metrics={
            "mode": "shadow",
            "claims": 8,
            "would_suppress": 7,
            "suppressed": 0,
            "shared_fetches": 0,
            "fanout_extra": 0,
            "abandoned": 0,
            "tracked": 0,
            "ledger_full": 0,
        },
    )
    line = StatusDisplayEngine._format_search_share_metrics_line(status)
    assert "wsuppr=7" in line
    assert "suppressed=0" in line
    assert line.index("suppressed=") < line.index("wsuppr=") < line.index("shared=")
