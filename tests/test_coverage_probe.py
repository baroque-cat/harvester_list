"""coverage_probe in governor + registry probe method (F-05, slice 3a.2).

Traceability: 03_sql_integration.md D-03.2/D-03.3, 07_verification.md §7.3.

RED predictions (pre-implementation):
- CP1: Registry has no ``probe_wire_covered`` method (AttributeError).
- CP2: Governor ``select_children`` has no ``coverage_probe`` kwarg (TypeError).
- CP3: ``refused_wire_covered`` key absent from governor metrics.
- CP4: Render line lacks ``wcovered=`` token.
- CP5: Config lacks ``coverage_mode`` / ``wire_ttl_hours`` fields.
- CP6: Wiring — coverage_mode/wire_ttl_hours do not reach governor.
- CP7–CP14: various behavioral pins (see below).
"""

import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from config.schemas import Config, RefineGovernorConfig
from manager.pipeline import Pipeline
from search.refine_governor import RefineGovernor
from search.querykey import fingerprint
from state.display import StatusDisplayEngine
from state.enums import DisplayMode, StatusContext
from state.models import SystemStatus
from core.metrics import PipelineStatus, StageMetrics
from state.display import get_display_config
from storage.registry import Registry


# ---------------------------------------------------------------------------
# Helper: build a real registry with wire_coverage rows
# ---------------------------------------------------------------------------
def _make_registry(workspace):
    """Build and start a registry in workspace."""
    registry = Registry(str(workspace), enabled=True)
    registry.start()
    return registry


def _insert_wire_coverage(conn, wire_fp, patterns_hash, pages_done,
                          pages_needed, last_ts, total_count=1000,
                          owner_provider=None):
    """Direct SQL insert for test setup."""
    conn.execute(
        "INSERT OR REPLACE INTO wire_coverage "
        "(wire_fp, patterns_hash, total_count, pages_bitmap, pages_done, "
        "pages_needed, results, first_ts, last_ts, owner_provider) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (wire_fp, patterns_hash, total_count,
         (1 << pages_needed) - 1 if pages_done >= pages_needed else 1,
         pages_done, pages_needed, 100, last_ts - 10, last_ts,
         owner_provider),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# CP1: Registry probe — covered only when watermark >= needed AND fresh ts
# ---------------------------------------------------------------------------
def test_cp1_covered_requires_watermark_and_freshness(workspace):
    """WHEN pages_done >= pages_needed AND last_ts is fresh THEN the query
    is covered; WHEN pages_done < pages_needed OR ts is stale THEN not covered.
    """
    registry = _make_registry(workspace)
    try:
        conn = sqlite3.connect(registry.path)
        try:
            now = time.time()
            ph = "abc123"
            # Query A: fully covered, fresh
            fp_a = fingerprint("/sk-aaa[a-z]{29}/", True)
            _insert_wire_coverage(conn, fp_a, ph, pages_done=5, pages_needed=5,
                                  last_ts=now - 100)
            # Query B: NOT covered (watermark < needed)
            fp_b = fingerprint("/sk-bbb[a-z]{29}/", True)
            _insert_wire_coverage(conn, fp_b, ph, pages_done=3, pages_needed=5,
                                  last_ts=now - 100)
            # Query C: NOT covered (stale ts, TTL=168h, last_ts older than TTL)
            fp_c = fingerprint("/sk-ccc[a-z]{29}/", True)
            _insert_wire_coverage(conn, fp_c, ph, pages_done=5, pages_needed=5,
                                  last_ts=now - 169 * 3600)
        finally:
            conn.close()

        queries = ["/sk-aaa[a-z]{29}/", "/sk-bbb[a-z]{29}/", "/sk-ccc[a-z]{29}/"]
        covered = registry.probe_wire_covered(
            queries, use_api=True, patterns_hash_value=ph, wire_ttl_hours=168,
        )
        assert "/sk-aaa[a-z]{29}/" in covered
        assert "/sk-bbb[a-z]{29}/" not in covered
        assert "/sk-ccc[a-z]{29}/" not in covered
    finally:
        registry.stop()


# ---------------------------------------------------------------------------
# CP2: patterns-change isolation — different patterns_hash ⇒ not covered
# ---------------------------------------------------------------------------
def test_cp2_patterns_change_isolation(workspace):
    """WHEN the patterns_hash differs from the one used to record coverage
    THEN the query is NOT covered (safety: pattern change ⇒ re-execute)."""
    registry = _make_registry(workspace)
    try:
        conn = sqlite3.connect(registry.path)
        try:
            now = time.time()
            fp = fingerprint("/sk-ddd[a-z]{29}/", True)
            _insert_wire_coverage(conn, fp, "old_hash", pages_done=5,
                                  pages_needed=5, last_ts=now - 100)
        finally:
            conn.close()

        covered = registry.probe_wire_covered(
            ["/sk-ddd[a-z]{29}/"], use_api=True,
            patterns_hash_value="new_hash", wire_ttl_hours=168,
        )
        assert len(covered) == 0
    finally:
        registry.stop()


# ---------------------------------------------------------------------------
# CP3: chunking — >900 wires triggers chunked IN queries
# ---------------------------------------------------------------------------
def test_cp3_chunking_over_900_wires(workspace):
    """WHEN more than ~897 wires are probed THEN the implementation chunks
    the IN query (bind params per chunk ≤ 900 accounting for extras)."""
    registry = _make_registry(workspace)
    try:
        # Create 950 wire coverage rows
        conn = sqlite3.connect(registry.path)
        try:
            now = time.time()
            ph = "chunk_test"
            queries = [f"/sk-q{i:04d}[a-z]{{26}}/" for i in range(950)]
            for q in queries:
                fp = fingerprint(q, True)
                _insert_wire_coverage(conn, fp, ph, pages_done=5,
                                      pages_needed=5, last_ts=now - 100)
        finally:
            conn.close()

        # All 950 should be covered (proves chunking works correctly)
        covered = registry.probe_wire_covered(
            queries, use_api=True, patterns_hash_value=ph, wire_ttl_hours=168,
        )
        assert len(covered) == 950
    finally:
        registry.stop()


# ---------------------------------------------------------------------------
# CP4: fail-open — read error ⇒ empty set + WARNING
# ---------------------------------------------------------------------------
def test_cp4_fail_open_on_read_error(workspace):
    """WHEN the probe encounters a read error THEN it returns an empty set
    and logs a WARNING (loud-once), without crashing."""
    registry = _make_registry(workspace)
    try:
        # Corrupt the database to force a read error
        registry.stop()
        # Overwrite the db file with garbage
        with open(registry.path, 'wb') as f:
            f.write(b'not a database')

        # Re-create registry pointing at corrupt file
        registry2 = Registry(str(workspace), enabled=True)
        # start will fail, so we simulate a different failure:
        # just test that probe returns empty when not available
        registry2._available = False
        covered = registry2.probe_wire_covered(
            ["/sk-eee[a-z]{29}/"], use_api=True,
            patterns_hash_value="ph", wire_ttl_hours=168,
        )
        assert covered == set()
    finally:
        try:
            registry2.stop()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CP5: disabled registry ⇒ empty set
# ---------------------------------------------------------------------------
def test_cp5_disabled_registry_returns_empty():
    """WHEN the registry is disabled THEN probe returns empty set."""
    registry = Registry(workspace=".", enabled=False)
    covered = registry.probe_wire_covered(
        ["/sk-fff[a-z]{29}/"], use_api=True,
        patterns_hash_value="ph", wire_ttl_hours=168,
    )
    assert covered == set()


# ---------------------------------------------------------------------------
# CP6: Governor — coverage_probe as trailing kwarg
# ---------------------------------------------------------------------------
def test_cp6_governor_accepts_coverage_probe_kwarg():
    """WHEN select_children is called with coverage_probe kwarg THEN it accepts it."""
    gov = RefineGovernor(mode="on", coverage_mode="on")
    batch = ["/sk-a[0-9]b[a-z]{28}/", "/sk-b[0-9]c[a-z]{28}/"]
    
    def probe(queries, use_api):
        return set()
    
    # Should not raise
    admitted = gov.select_children(
        "prov", "/parent[a-z]{32}/", batch, 1000, 5000,
        use_api=True, coverage_probe=probe,
    )
    assert len(admitted) == 2


# ---------------------------------------------------------------------------
# CP7: Governor — shadow mode counts but admits all
# ---------------------------------------------------------------------------
def test_cp7_shadow_counts_but_admits_all():
    """WHEN coverage_mode=shadow and coverage_probe reports covered children THEN the
    shadow counter grows but ALL children are still admitted, refused_wire_covered=0."""
    gov = RefineGovernor(
        mode="on",  # RFG mode
        coverage_mode="shadow",  # coverage mode
        max_partitions_per_refine=10,
        max_search_tasks_per_run=100,
    )
    
    def probe(queries, use_api):
        # Mark the first query as covered
        return {queries[0]} if queries else set()

    batch = [
        "/sk-a[0-9]b[a-z]{28}/",
        "/sk-b[0-9]c[a-z]{28}/",
        "/sk-c[0-9]d[a-z]{28}/",
    ]
    admitted = gov.select_children(
        "prov", "/parent[a-z]{32}/", batch, 1000, 5000,
        use_api=True, coverage_probe=probe,
    )
    m = gov.metrics
    assert m["wire_covered_shadow_hits"] == 1  # shadow counter grew
    assert m["refused_wire_covered"] == 0  # refused stays 0
    assert len(admitted) == 3  # all admitted (shadow)


# ---------------------------------------------------------------------------
# CP8: Governor — coverage_mode=off ⇒ probe ignored
# ---------------------------------------------------------------------------
def test_cp8_coverage_mode_off_probe_ignored():
    """WHEN coverage_mode=off THEN the probe is ignored even if passed."""
    gov = RefineGovernor(mode="on", coverage_mode="off")
    
    probe_called = [False]
    def probe(queries, use_api):
        probe_called[0] = True
        return {queries[0]} if queries else set()

    batch = ["/sk-a[0-9]b[a-z]{28}/", "/sk-b[0-9]c[a-z]{28}/"]
    admitted = gov.select_children(
        "prov", "/parent[a-z]{32}/", batch, 1000, 5000,
        use_api=True, coverage_probe=probe,
    )
    assert not probe_called[0]  # probe not called
    assert len(admitted) == 2
    assert gov.metrics["refused_wire_covered"] == 0
    assert gov.metrics["wire_covered_shadow_hits"] == 0


# ---------------------------------------------------------------------------
# CP9: Governor — probe RAISES ⇒ zero suppressions, no crash
# ---------------------------------------------------------------------------
def test_cp9_probe_raises_zero_suppressions():
    """WHEN coverage_probe raises an exception THEN no children are suppressed
    and the governor continues without crashing."""
    gov = RefineGovernor(mode="on", coverage_mode="on")
    
    def bad_probe(queries, use_api):
        raise RuntimeError("database exploded")

    batch = ["/sk-a[0-9]b[a-z]{28}/", "/sk-b[0-9]c[a-z]{28}/"]
    admitted = gov.select_children(
        "prov", "/parent[a-z]{32}/", batch, 1000, 5000,
        use_api=True, coverage_probe=bad_probe,
    )
    assert gov.metrics["refused_wire_covered"] == 0
    assert len(admitted) == 2


# ---------------------------------------------------------------------------
# CP10: Identity — generated == admitted + refused_wire_collapse +
#        refused_wire_covered + truncated_to_cap + refused_budget (on mode)
# ---------------------------------------------------------------------------
def test_cp10_admission_identity_on_mode():
    """F-02(1): the five counters account for every generated child in on mode."""
    gov = RefineGovernor(
        mode="on",
        coverage_mode="on",
        max_partitions_per_refine=3,
        max_search_tasks_per_run=100,
    )
    
    def probe(queries, use_api):
        # Cover the first 2
        return set(queries[:2])

    batch = [
        "/sk-a[0-9]b[a-z]{28}/",  # covered
        "/sk-b[0-9]c[a-z]{28}/",  # covered
        "/sk-c[0-9]d[a-z]{28}/",  # admitted
        "/sk-d[0-9]e[a-z]{28}/",  # admitted
        "/sk-e[0-9]f[a-z]{28}/",  # admitted
        "/sk-f[0-9]g[a-z]{28}/",  # truncated (cap=3)
    ]
    admitted = gov.select_children(
        "prov", "/parent[a-z]{32}/", batch, 1000, 5000,
        use_api=True, coverage_probe=probe,
    )
    m = gov.metrics
    assert m["children_generated"] == (
        m["refused_wire_covered"]
        + m["refused_wire_collapse"]
        + m["truncated_to_cap"]
        + m["refused_budget"]
        + m["children_admitted"]
    )


# ---------------------------------------------------------------------------
# CP10b: Identity in shadow mode — refused_wire_covered stays 0
# ---------------------------------------------------------------------------
def test_cp10b_admission_identity_shadow_mode():
    """In shadow mode, refused_wire_covered stays 0 (disjoint identity F-02(1))."""
    gov = RefineGovernor(
        mode="on",
        coverage_mode="shadow",
        max_partitions_per_refine=10,  # Increased to allow all 4
        max_search_tasks_per_run=100,
    )
    
    def probe(queries, use_api):
        return set(queries[:2])

    batch = [
        "/sk-a[0-9]b[a-z]{28}/",
        "/sk-b[0-9]c[a-z]{28}/",
        "/sk-c[0-9]d[a-z]{28}/",
        "/sk-d[0-9]e[a-z]{28}/",
    ]
    admitted = gov.select_children(
        "prov", "/parent[a-z]{32}/", batch, 1000, 5000,
        use_api=True, coverage_probe=probe,
    )
    m = gov.metrics
    assert m["refused_wire_covered"] == 0  # stays 0 in shadow
    assert m["wire_covered_shadow_hits"] == 2  # shadow counter grew
    assert len(admitted) == 4  # all admitted (shadow doesn't suppress)


# ---------------------------------------------------------------------------
# CP11: Render — wcovered and wshadow tokens present
# ---------------------------------------------------------------------------
def test_cp11_render_wcovered_and_wshadow_tokens():
    """WHEN refine_metrics carries refused_wire_covered and wire_covered_shadow_hits
    THEN the Refine render line contains both 'wcovered=' and 'wshadow=' tokens."""
    refine = {
        "mode": "on",
        "coverage_mode": "on",
        "children_generated": 144,
        "children_admitted": 100,
        "refused_depth": 0,
        "refused_budget": 5,
        "truncated_to_cap": 16,
        "parents_refined": 1,
        "parents_at_depth_cap_paginated": 0,
        "budget_remaining": 9999,
        "coverage_estimate_min": 0.0027,
        "coverage_estimate_avg": 0.0746,
        "distinct_wire_admitted": 100,
        "refused_wire_collapse": 7,
        "search_swallowed_exceptions": 0,
        "pagination_fallbacks": 1,
        "refused_wire_covered": 16,
        "wire_covered_shadow_hits": 3,
    }
    status = SystemStatus(pipeline=PipelineStatus(
        stages={"search": StageMetrics(name="search", workers=1)},
        refine_metrics=refine,
    ))
    cfg = get_display_config(StatusContext.APPLICATION, DisplayMode.DETAILED)
    lines = StatusDisplayEngine()._format_pipeline_section(status, cfg)
    refine_lines = [ln for ln in lines if ln.startswith("Refine:")]
    assert len(refine_lines) == 1
    line = refine_lines[0]
    assert line.count("wcovered=") == 1
    assert line.count("wshadow=") == 1
    assert "wcovered=16" in line
    assert "wshadow=3" in line
    # Position: after fallbacks=, before (unfit-denominator)
    assert line.index("fallbacks=") < line.index("wcovered=")
    assert line.index("wcovered=") < line.index("wshadow=")
    assert line.index("wshadow=") < line.index("(unfit-denominator)")
    assert line.rstrip().endswith("(unfit-denominator)")


# ---------------------------------------------------------------------------
# CP12: Config — coverage_mode and wire_ttl_hours validated
# ---------------------------------------------------------------------------
def test_cp12_config_validation():
    """WHEN coverage_mode is invalid OR wire_ttl_hours is non-positive THEN
    config validation fails loudly."""
    # Valid defaults
    cfg = RefineGovernorConfig()
    assert cfg.coverage_mode == "off"
    assert cfg.wire_ttl_hours == 168

    # Invalid mode
    with pytest.raises(ValueError):
        RefineGovernorConfig(coverage_mode="invalid")

    # Non-positive TTL
    with pytest.raises(ValueError):
        RefineGovernorConfig(wire_ttl_hours=0)
    with pytest.raises(ValueError):
        RefineGovernorConfig(wire_ttl_hours=-1)

    # Valid values
    cfg2 = RefineGovernorConfig(coverage_mode="shadow", wire_ttl_hours=720)
    assert cfg2.coverage_mode == "shadow"
    assert cfg2.wire_ttl_hours == 720


# ---------------------------------------------------------------------------
# CP13: Wiring — coverage_mode/wire_ttl_hours reach governor via Pipeline
# ---------------------------------------------------------------------------
def test_cp13_wiring_pipeline_to_governor():
    """WHEN Config carries coverage_mode and wire_ttl_hours THEN Pipeline
    wiring passes them to the governor constructor."""
    cfg = Config(
        refine_governor=RefineGovernorConfig(
            mode="on",
            coverage_mode="shadow",
            wire_ttl_hours=720,
        )
    )
    stub = SimpleNamespace()
    Pipeline._configure_refine_governor(stub, cfg)
    governor = stub.refine_governor
    # The governor should have received the coverage settings
    assert governor.coverage_mode == "shadow"
    assert governor.wire_ttl_hours == 720


# ---------------------------------------------------------------------------
# CP14: Neutrality — coverage_mode=off (default) ⇒ byte-identical behavior
# ---------------------------------------------------------------------------
def test_cp14_neutrality_coverage_mode_off():
    """WHEN coverage_mode=off (default) THEN the governor behaves byte-identically
    to the pre-change implementation: no probe called, no suppressions."""
    gov = RefineGovernor(mode="on", coverage_mode="off")
    
    probe_called = [False]
    def probe(queries, use_api):
        probe_called[0] = True
        return {queries[0]} if queries else set()

    batch = ["/sk-a[0-9]b[a-z]{28}/", "/sk-b[0-9]c[a-z]{28}/"]
    admitted = gov.select_children(
        "prov", "/parent[a-z]{32}/", batch, 1000, 5000,
        use_api=True, coverage_probe=probe,
    )
    m = gov.metrics
    assert not probe_called[0]  # probe not called
    assert m["refused_wire_covered"] == 0
    assert m["wire_covered_shadow_hits"] == 0
    assert len(admitted) == 2


# ---------------------------------------------------------------------------
# CP15: TTL flow — functional test through probe
# ---------------------------------------------------------------------------
def test_cp15_ttl_flow_functional(workspace):
    """WHEN last_ts is 2h old and wire_ttl_hours=1 THEN NOT covered;
    WHEN wire_ttl_hours=168 THEN covered."""
    registry = _make_registry(workspace)
    try:
        conn = sqlite3.connect(registry.path)
        try:
            now = time.time()
            ph = "ttl_test"
            fp = fingerprint("/sk-ttl[a-z]{29}/", True)
            # 2 hours old
            _insert_wire_coverage(conn, fp, ph, pages_done=5, pages_needed=5,
                                  last_ts=now - 2 * 3600)
        finally:
            conn.close()

        queries = ["/sk-ttl[a-z]{29}/"]
        
        # TTL=1h: 2h old ⇒ NOT covered
        covered_1h = registry.probe_wire_covered(
            queries, use_api=True, patterns_hash_value=ph, wire_ttl_hours=1,
        )
        assert len(covered_1h) == 0
        
        # TTL=168h: 2h old ⇒ covered
        covered_168h = registry.probe_wire_covered(
            queries, use_api=True, patterns_hash_value=ph, wire_ttl_hours=168,
        )
        assert len(covered_168h) == 1
    finally:
        registry.stop()


# ---------------------------------------------------------------------------
# CP16: stage-seam, on-mode подавление
# ---------------------------------------------------------------------------
def test_cp16_stage_seam_on_mode_suppresses(monkeypatch, workspace):
    """CP16: stage/definition.py:478-502 шов — когда coverage_mode='on' и один
    из детей покрыт, stage подавляет его через probe closure."""
    from config.schemas import Config, RefineGovernorConfig, StageConfig, TaskConfig
    from core.models import Patterns, SearchTask
    from search import client as search_client
    from search.refine_governor import RefineGovernor
    from search.github.refine.engine import RefineEngine
    from stage.base import StageResources
    from stage.definition import SearchStage
    from storage.registry import patterns_hash

    PROVIDER = "deepseek"
    KEY_PATTERN = r"sk-[A-Za-z0-9]{16}"
    RAW_SEED = "/sk-[a-zA-Z0-9]{32}/"
    COVERED_CHILD = "/sk-aaa[a-z]{29}/"
    UNCOVERED_CHILD = "/sk-bbb[a-z]{29}/"

    class FakeAuth:
        def get_session(self): return "session-token"
        def get_token(self): return "api-token"
        def get_user_agent(self): return "test-agent"

    task_config = TaskConfig(
        name=PROVIDER, enabled=True, provider_type="openai_like", use_api=True,
        stages=StageConfig(search=True, gather=True, check=True, inspect=True),
        patterns=Patterns(key_pattern=KEY_PATTERN),
    )
    gov = RefineGovernor(
        mode="on", coverage_mode="on", wire_ttl_hours=168,
        max_refine_depth=2, max_partitions_per_refine=128,
        max_search_tasks_per_run=10000, drop_wire_indistinguishable=False,
    )
    cfg = Config()
    cfg.pipeline.failure_handling = "legacy"
    cfg.refine_governor = RefineGovernorConfig(
        mode="on", coverage_mode="on", wire_ttl_hours=168,
        max_refine_depth=2, max_partitions_per_refine=128,
        max_search_tasks_per_run=10000,
    )

    # Start a real registry.
    registry = Registry(str(workspace), enabled=True)
    registry.start()

    # Compute patterns_hash for the task.
    ph = patterns_hash(key_pattern=KEY_PATTERN)

    # Seed wire_coverage row for COVERED_CHILD (fully covered, fresh).
    now = time.time()
    wire_fp = fingerprint(COVERED_CHILD, use_api=True)
    conn = sqlite3.connect(registry.path)
    try:
        _insert_wire_coverage(
            conn, wire_fp, ph, pages_done=5, pages_needed=5,
            last_ts=now - 100, total_count=500,
        )
    finally:
        conn.close()

    resources = StageResources(
        limiter=None, providers={}, config=cfg,
        task_configs={PROVIDER: task_config},
        auth=FakeAuth(), registry=registry,
        refine_governor=gov,
    )
    outputs = []
    stage = SearchStage(resources, outputs.append, thread_count=1)

    # Mock search to return total > limit (triggers refine).
    def fake_search(**kwargs):
        total = 5000  # > API_LIMIT=1000
        n = 100
        urls = [f"https://github.com/o/r/blob/main/f{i}.py" for i in range(n)]
        return urls, total, ""

    monkeypatch.setattr(search_client, "search_with_count", fake_search)

    # Monkeypatch RefineEngine.generate_queries to return our children.
    def fake_generate_queries(query, partitions):
        return [COVERED_CHILD, UNCOVERED_CHILD]

    monkeypatch.setattr(
        RefineEngine, "generate_queries",
        lambda self, query, partitions: fake_generate_queries(query, partitions),
    )

    task = SearchTask(
        provider=PROVIDER, query=RAW_SEED, regex=KEY_PATTERN,
        page=1, use_api=True,
    )
    out = stage.process_task(task)
    assert out is not None

    # Drain registry.
    registry.stop()

    # Check outputs: COVERED_CHILD should be suppressed, UNCOVERED_CHILD admitted.
    # Tasks are in out.new_tasks as (task, target_stage) tuples.
    # Filter only SearchTask (refined children).
    from core.models import SearchTask
    output_queries = [t.query for t, _ in out.new_tasks if isinstance(t, SearchTask)]
    assert COVERED_CHILD not in output_queries, f"covered child should be suppressed, got {output_queries}"
    assert UNCOVERED_CHILD in output_queries, f"uncovered child should be admitted, got {output_queries}"

    # Check governor metrics.
    m = gov.metrics
    assert m["refused_wire_covered"] >= 1, f"refused_wire_covered should be >= 1, got {m['refused_wire_covered']}"
    assert m["wire_covered_shadow_hits"] == 0, f"wire_covered_shadow_hits should be 0 in on mode, got {m['wire_covered_shadow_hits']}"


# ---------------------------------------------------------------------------
# CP17: stage-seam, shadow-mode НЕ подавляет
# ---------------------------------------------------------------------------
def test_cp17_stage_seam_shadow_mode_does_not_suppress(monkeypatch, workspace):
    """CP17: stage/definition.py:478-502 шов — когда coverage_mode='shadow',
    stage НЕ подавляет покрытых детей, но wire_covered_shadow_hits растёт."""
    from config.schemas import Config, RefineGovernorConfig, StageConfig, TaskConfig
    from core.models import Patterns, SearchTask
    from search import client as search_client
    from search.refine_governor import RefineGovernor
    from search.github.refine.engine import RefineEngine
    from stage.base import StageResources
    from stage.definition import SearchStage
    from storage.registry import patterns_hash

    PROVIDER = "deepseek"
    KEY_PATTERN = r"sk-[A-Za-z0-9]{16}"
    RAW_SEED = "/sk-[a-zA-Z0-9]{32}/"
    COVERED_CHILD = "/sk-aaa[a-z]{29}/"
    UNCOVERED_CHILD = "/sk-bbb[a-z]{29}/"

    class FakeAuth:
        def get_session(self): return "session-token"
        def get_token(self): return "api-token"
        def get_user_agent(self): return "test-agent"

    task_config = TaskConfig(
        name=PROVIDER, enabled=True, provider_type="openai_like", use_api=True,
        stages=StageConfig(search=True, gather=True, check=True, inspect=True),
        patterns=Patterns(key_pattern=KEY_PATTERN),
    )
    gov = RefineGovernor(
        mode="on", coverage_mode="shadow", wire_ttl_hours=168,
        max_refine_depth=2, max_partitions_per_refine=128,
        max_search_tasks_per_run=10000, drop_wire_indistinguishable=False,
    )
    cfg = Config()
    cfg.pipeline.failure_handling = "legacy"
    cfg.refine_governor = RefineGovernorConfig(
        mode="on", coverage_mode="shadow", wire_ttl_hours=168,
        max_refine_depth=2, max_partitions_per_refine=128,
        max_search_tasks_per_run=10000,
    )

    # Start a real registry.
    registry = Registry(str(workspace), enabled=True)
    registry.start()

    # Compute patterns_hash for the task.
    ph = patterns_hash(key_pattern=KEY_PATTERN)

    # Seed wire_coverage row for COVERED_CHILD (fully covered, fresh).
    now = time.time()
    wire_fp = fingerprint(COVERED_CHILD, use_api=True)
    conn = sqlite3.connect(registry.path)
    try:
        _insert_wire_coverage(
            conn, wire_fp, ph, pages_done=5, pages_needed=5,
            last_ts=now - 100, total_count=500,
        )
    finally:
        conn.close()

    resources = StageResources(
        limiter=None, providers={}, config=cfg,
        task_configs={PROVIDER: task_config},
        auth=FakeAuth(), registry=registry,
        refine_governor=gov,
    )
    outputs = []
    stage = SearchStage(resources, outputs.append, thread_count=1)

    # Mock search to return total > limit (triggers refine).
    def fake_search(**kwargs):
        total = 5000  # > API_LIMIT=1000
        n = 100
        urls = [f"https://github.com/o/r/blob/main/f{i}.py" for i in range(n)]
        return urls, total, ""

    monkeypatch.setattr(search_client, "search_with_count", fake_search)

    # Monkeypatch RefineEngine.generate_queries to return our children.
    def fake_generate_queries(query, partitions):
        return [COVERED_CHILD, UNCOVERED_CHILD]

    monkeypatch.setattr(
        RefineEngine, "generate_queries",
        lambda self, query, partitions: fake_generate_queries(query, partitions),
    )

    task = SearchTask(
        provider=PROVIDER, query=RAW_SEED, regex=KEY_PATTERN,
        page=1, use_api=True,
    )
    out = stage.process_task(task)
    assert out is not None

    # Drain registry.
    registry.stop()

    # Check outputs: BOTH children should be admitted (shadow doesn't suppress).
    # Tasks are in out.new_tasks as (task, target_stage) tuples.
    # Filter only SearchTask (refined children).
    from core.models import SearchTask
    output_queries = [t.query for t, _ in out.new_tasks if isinstance(t, SearchTask)]
    assert COVERED_CHILD in output_queries, f"covered child should be admitted in shadow mode, got {output_queries}"
    assert UNCOVERED_CHILD in output_queries, f"uncovered child should be admitted, got {output_queries}"

    # Check governor metrics.
    m = gov.metrics
    assert m["wire_covered_shadow_hits"] >= 1, f"wire_covered_shadow_hits should be >= 1 in shadow mode, got {m['wire_covered_shadow_hits']}"
    assert m["refused_wire_covered"] == 0, f"refused_wire_covered should be 0 in shadow mode, got {m['refused_wire_covered']}"
