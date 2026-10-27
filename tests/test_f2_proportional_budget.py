"""F2 proportional per-provider budget (wave 5, slice 5.1a).

Traceability: 02_critical_fixes.md:88 F2, 07_verification.md:128 (fairness ±10%).

The refine governor's global FCFS budget allows the first provider to monopolize
the entire budget when it submits large batches first (measured incident 2026-09-29).
F2 fixes this with a proportional per-provider budget: each provider gets a fair
share (ceil(budget / n_providers)), and can't exceed it until all providers have
had a chance to claim their share. Work-conserving: once all providers have been
seen, the remaining budget is FCFS.

RED state: n_providers kwarg doesn't exist yet ⇒ TypeError on construction.
Fairness pin: without the fix, provider A monopolizes (A=100, B=0 in the scenario).
"""

import pytest

from search.refine_governor import RefineGovernor


# Generate enough queries for large-batch tests.
QUERIES = [f"/sk-{i:03d}[a-z]{{30}}/" for i in range(150)]


def test_n_providers_kwarg_accepted():
    """F2-1: n_providers trailing kwarg is accepted (RED: TypeError pre-fix)."""
    gov = RefineGovernor(
        mode="on",
        max_refine_depth=2,
        max_partitions_per_refine=128,
        max_search_tasks_per_run=100,
        n_providers=2,
    )
    assert gov.n_providers == 2


def test_n_providers_default_is_one():
    """F2-2: n_providers=1 (default) ⇒ byte-identical to pre-change behavior."""
    gov = RefineGovernor(mode="on", max_search_tasks_per_run=100)
    assert gov.n_providers == 1
    
    # With n=1, fair_share = budget, so behavior is unchanged.
    admitted = gov.select_children("prov", "/sk-x/", QUERIES[:10], 1000, 50_000)
    assert len(admitted) == 10  # all admitted (budget=100, demand=10)
    assert gov.metrics["children_admitted"] == 10
    assert gov.metrics["budget_remaining"] == 90


def test_fairness_symmetric_demand():
    """F2-3: fairness pin — symmetric demand ⇒ ±10% (RED: A monopolizes pre-fix).
    
    Scenario: budget=100, n=2, fair_share=50. A wants 100+, B wants 50+.
    Pre-fix: A calls first, takes 100 (FCFS). B gets 0. Monopoly.
    Post-fix: A takes 50 (fair_share cap). B takes 50. Fair.
    """
    gov = RefineGovernor(
        mode="on",
        max_partitions_per_refine=100,
        max_search_tasks_per_run=100,
        n_providers=2,
    )
    
    # A calls first with 100 candidates.
    a1 = gov.select_children("A", "/sk-p1/", QUERIES[:100], 1000, 50_000)
    # A calls again (still before B has called).
    a2 = gov.select_children("A", "/sk-p2/", QUERIES[:100], 1000, 50_000)
    # B calls with 50 candidates.
    b1 = gov.select_children("B", "/sk-p3/", QUERIES[:50], 1000, 50_000)
    
    total_a = len(a1) + len(a2)
    total_b = len(b1)
    
    # Fairness: A and B should be within ±10% of each other.
    # With budget=100, n=2, fair_share=50: A≈50, B≈50.
    assert total_a == 50, f"A should get ~50, got {total_a}"
    assert total_b == 50, f"B should get ~50, got {total_b}"
    
    # ±10% tolerance.
    assert abs(total_a - total_b) <= 10, f"Fairness violation: A={total_a}, B={total_b}"


def test_work_conservation_asymmetric_demand():
    """F2-4: work-conserving — when B finishes early, A can use the overflow.
    
    Scenario: budget=100, n=2, fair_share=50. A wants 100+, B wants 20 and done.
    Post-fix: A takes 50 (fair_share). B takes 20. A takes 30 more (overflow).
    Total: A=80, B=20. Budget fully consumed.
    """
    gov = RefineGovernor(
        mode="on",
        max_partitions_per_refine=100,
        max_search_tasks_per_run=100,
        n_providers=2,
    )
    
    # A calls first with 100 candidates.
    a1 = gov.select_children("A", "/sk-p1/", QUERIES[:100], 1000, 50_000)
    # B calls with 20 candidates.
    b1 = gov.select_children("B", "/sk-p2/", QUERIES[:20], 1000, 50_000)
    # A calls again (all providers seen, Phase 2 FCFS).
    a2 = gov.select_children("A", "/sk-p3/", QUERIES[:100], 1000, 50_000)
    
    total_a = len(a1) + len(a2)
    total_b = len(b1)
    
    # Work-conservation: budget fully consumed.
    assert total_a + total_b == 100, f"Budget not fully consumed: {total_a + total_b}"
    # A gets overflow after B finishes.
    assert total_a == 80, f"A should get 80 (50 fair + 30 overflow), got {total_a}"
    assert total_b == 20, f"B should get 20, got {total_b}"


def test_identity_preserved():
    """F2-5: admission identity F-02(1) is preserved.
    
    generated == admitted + refused_wire_collapse + refused_wire_covered + truncated_to_cap + refused_budget
    """
    gov = RefineGovernor(
        mode="on",
        max_partitions_per_refine=3,
        max_search_tasks_per_run=5,
        n_providers=2,
    )
    
    # Generate 10 candidates, cap=3, budget=5.
    gov.select_children("A", "/sk-p1/", QUERIES[:10], 1000, 50_000)
    
    m = gov.metrics
    generated = m["children_generated"]
    admitted = m["children_admitted"]
    refused_wire_collapse = m["refused_wire_collapse"]
    refused_wire_covered = m["refused_wire_covered"]
    truncated = m["truncated_to_cap"]
    refused_budget = m["refused_budget"]
    
    # Identity: generated == admitted + refused_wire_collapse + refused_wire_covered + truncated + refused_budget
    assert generated == admitted + refused_wire_collapse + refused_wire_covered + truncated + refused_budget, \
        f"Identity violated: {generated} != {admitted} + {refused_wire_collapse} + {refused_wire_covered} + {truncated} + {refused_budget}"


def test_shadow_mode_unaffected():
    """F2-6: shadow mode with n_providers=1 is byte-identical to pre-change."""
    gov = RefineGovernor(
        mode="shadow",
        max_partitions_per_refine=3,
        max_search_tasks_per_run=5,
        n_providers=1,  # neutral: byte-pre-change
    )
    
    # Shadow enqueues everything.
    admitted = gov.select_children("A", "/sk-p1/", QUERIES[:10], 1000, 50_000)
    assert len(admitted) == 10  # nothing withheld
    
    # But counters measure would-be decisions.
    m = gov.metrics
    assert m["children_admitted"] == 0  # enforcement counters clean
    assert m["truncated_to_cap"] == 7  # 10 - 3 cap
    # Shadow tracks would-be budget drain. With cap=3, would_admit=3.
    # So budget_used=3, budget_remaining=5-3=2.
    assert m["budget_remaining"] == 2  # 5 - 3 = 2


def test_off_mode_unaffected():
    """F2-7: off mode is byte-identical to pre-change."""
    gov = RefineGovernor(
        mode="off",
        max_partitions_per_refine=3,
        max_search_tasks_per_run=5,
        n_providers=2,
    )
    
    # Off returns input verbatim.
    admitted = gov.select_children("A", "/sk-p1/", QUERIES[:10], 1000, 50_000)
    assert admitted == QUERIES[:10]  # input order preserved
    
    # Counters inert.
    m = gov.metrics
    assert m["children_admitted"] == 0
    assert m["refused_budget"] == 0


def test_three_providers_fairness():
    """F2-8: fairness with 3 providers and symmetric demand.
    
    budget=100, n=3, fair_share=34 (ceil(100/3)).
    A, B, C all want 100+. Each gets ~34 (±10%).
    """
    gov = RefineGovernor(
        mode="on",
        max_partitions_per_refine=100,
        max_search_tasks_per_run=100,
        n_providers=3,
    )
    
    # A, B, C each call once.
    a = gov.select_children("A", "/sk-p1/", QUERIES, 1000, 50_000)
    b = gov.select_children("B", "/sk-p2/", QUERIES, 1000, 50_000)
    c = gov.select_children("C", "/sk-p3/", QUERIES, 1000, 50_000)
    
    # Each gets ~34 (fair_share).
    assert len(a) == 34, f"A should get 34, got {len(a)}"
    assert len(b) == 34, f"B should get 34, got {len(b)}"
    assert len(c) == 32, f"C should get 32 (remaining), got {len(c)}"
    
    # ±10% tolerance.
    assert abs(len(a) - len(b)) <= 3, f"Fairness violation: A={len(a)}, B={len(b)}"
    assert abs(len(b) - len(c)) <= 3, f"Fairness violation: B={len(b)}, C={len(c)}"


# ---------------------------------------------------------------------------
# W1: Wiring pin through REAL Pipeline (lesson 2.2-P3/3a.3-D1/3b.2-R9)
# ---------------------------------------------------------------------------
def test_w1_real_pipeline_wiring_n_providers(tmp_path):
    """W1: Pipeline wiring through REAL Pipeline(cfg, providers={}).
    
    Filter: enabled=True AND stages.search=True.
    Scenario: 3 enabled+search, 1 enabled+no-search, 1 disabled+search → n=3.
    """
    from config.schemas import Config, TaskConfig, StageConfig
    from core.models import Patterns
    from manager.pipeline import Pipeline
    
    cfg = Config()
    cfg.global_config.workspace = str(tmp_path)
    cfg.persistence.simple = True
    
    # 5 tasks: 3 enabled+search, 1 enabled+no-search, 1 disabled+search
    cfg.tasks = [
        TaskConfig(name="prov-a", enabled=True, stages=StageConfig(search=True),
                   patterns=Patterns(key_pattern=r"sk-[a-z]{16}")),
        TaskConfig(name="prov-b", enabled=True, stages=StageConfig(search=True),
                   patterns=Patterns(key_pattern=r"sk-[a-z]{16}")),
        TaskConfig(name="prov-c", enabled=True, stages=StageConfig(search=True),
                   patterns=Patterns(key_pattern=r"sk-[a-z]{16}")),
        TaskConfig(name="prov-d", enabled=True, stages=StageConfig(search=False),
                   patterns=Patterns(key_pattern=r"sk-[a-z]{16}")),
        TaskConfig(name="prov-e", enabled=False, stages=StageConfig(search=True),
                   patterns=Patterns(key_pattern=r"sk-[a-z]{16}")),
    ]
    
    pipeline = Pipeline(cfg, providers={})
    try:
        # (а) Filter: enabled AND search
        assert pipeline.refine_governor.n_providers == 3, \
            f"Expected n_providers=3 (3 enabled+search), got {pipeline.refine_governor.n_providers}"
        
        # (б) Metrics expose n_providers
        assert pipeline.refine_governor.metrics["n_providers"] == 3, \
            f"Expected metrics['n_providers']=3, got {pipeline.refine_governor.metrics['n_providers']}"
    finally:
        if hasattr(pipeline, 'queue_manager') and hasattr(pipeline.queue_manager, 'close'):
            pipeline.queue_manager.close()


def test_w1b_real_pipeline_wiring_neutrality(tmp_path):
    """W1b: Single search task → n_providers=1 (neutrality).
    
    With n=1, fair_share=budget, so behavior is byte-identical to pre-F2 FCFS.
    """
    from config.schemas import Config, TaskConfig, StageConfig
    from core.models import Patterns
    from manager.pipeline import Pipeline
    
    cfg = Config()
    cfg.global_config.workspace = str(tmp_path)
    cfg.persistence.simple = True
    
    # Single enabled+search task
    cfg.tasks = [
        TaskConfig(name="prov-single", enabled=True, stages=StageConfig(search=True),
                   patterns=Patterns(key_pattern=r"sk-[a-z]{16}")),
    ]
    
    pipeline = Pipeline(cfg, providers={})
    try:
        assert pipeline.refine_governor.n_providers == 1, \
            f"Expected n_providers=1 (single search task), got {pipeline.refine_governor.n_providers}"
    finally:
        if hasattr(pipeline, 'queue_manager') and hasattr(pipeline.queue_manager, 'close'):
            pipeline.queue_manager.close()
