"""wire_coverage table (schema v8) + record_wire_page + page seams (F-01, slice 3a.1).

Traceability: 03_sql_integration.md D-03.1, F-01 (gap-safe bitmap+watermark).

RED predictions (pre-implementation):
- W1: SCHEMA_VERSION is 6, not 7; wire_coverage table absent; user_version==6.
- W2: migration from v6 does not create wire_coverage; user_version stays 6.
- W3: record_wire_page method absent (AttributeError).
- W4: out-of-order pages — method absent.
- W5: duplicate page — method absent.
- W6: hole not covered — method absent.
- W7: bit clamp — method absent.
- W8: disabled no-op — method absent or table absent.
- W9: seam integration — no wire_coverage row after page-1 processing.
- W10: seam integration page-N — no wire_coverage row after page-2 processing.
"""

import math
import sqlite3
import time

import pytest

from storage.registry import Registry, SCHEMA_VERSION


# ---------------------------------------------------------------------------
# W1: contract — fresh registry has wire_coverage with exact schema
# ---------------------------------------------------------------------------
def test_w1_contract_fresh_registry(workspace, db_rows, db_scalar, registry_path):
    """W1: fresh bootstrap creates wire_coverage with exact DDL, PK, indexes,
    user_version==8, SCHEMA_VERSION==8, and idx_links_visit_status."""
    registry = Registry(workspace, enabled=True)
    registry.start()
    registry.stop()

    # Schema version bumped.
    assert SCHEMA_VERSION == 8
    assert db_scalar(workspace, "PRAGMA user_version") == 8

    # wire_coverage table exists with exact column set/order.
    columns = db_rows(workspace, "PRAGMA table_info(wire_coverage)")
    col_names = [row["name"] for row in columns]
    assert col_names == [
        "wire_fp", "patterns_hash", "total_count", "pages_bitmap",
        "pages_done", "pages_needed", "results", "first_ts", "last_ts",
        "owner_provider",
    ]

    # PK is (wire_fp, patterns_hash).
    pk_cols = [row["name"] for row in columns if row["pk"] > 0]
    assert pk_cols == ["wire_fp", "patterns_hash"]

    # idx_wire_coverage_ts exists.
    indexes = {row["name"] for row in db_rows(workspace, "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_wire_coverage_ts" in indexes

    # idx_links_visit_status exists (pre-registered for 3a.3).
    assert "idx_links_visit_status" in indexes


# ---------------------------------------------------------------------------
# W2: migration v6 -> v7
# ---------------------------------------------------------------------------
def test_w2_migration_v6_to_v7(workspace, db_rows, db_scalar, registry_path):
    """W2: a v6 database migrates to v8 with wire_coverage present and
    pre-existing links rows untouched."""
    path = registry_path(workspace)
    conn = sqlite3.connect(path)

    # Build a v6-shaped database (current schema minus wire_coverage and
    # idx_links_visit_status).
    conn.executescript("""
        CREATE TABLE links(
          url_hash TEXT PRIMARY KEY,
          url TEXT NOT NULL,
          owner TEXT,
          repo TEXT,
          path TEXT,
          first_seen_ts REAL,
          last_seen_ts REAL,
          gathered_ts REAL,
          visit_status TEXT NOT NULL DEFAULT 'discovered',
          transport TEXT,
          query_origin TEXT,
          provider TEXT,
          repo_pushed_at REAL,
          repo_size_kb INTEGER,
          file_commit_date REAL,
          priority REAL,
          etag TEXT,
          etag_transport TEXT
        );
        CREATE TABLE link_coverage(
          url_hash TEXT NOT NULL,
          provider TEXT NOT NULL,
          patterns_hash TEXT NOT NULL,
          gathered_ts REAL NOT NULL,
          PRIMARY KEY(url_hash, provider, patterns_hash)
        );
        CREATE TABLE keys(
          key_hash TEXT PRIMARY KEY,
          provider TEXT,
          key_ref_masked TEXT,
          address TEXT,
          endpoint TEXT,
          status TEXT,
          first_seen_ts REAL,
          last_recheck_ts REAL,
          last_seen_ts REAL,
          source_url_hash TEXT
        );
        CREATE TABLE runs(
          run_id TEXT PRIMARY KEY,
          started_at REAL,
          finished_at REAL,
          config_digest TEXT,
          degraded INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE repos(
          owner TEXT NOT NULL,
          repo TEXT NOT NULL,
          pushed_at REAL,
          size_kb INTEGER,
          default_branch TEXT,
          etag TEXT,
          fetched_at REAL NOT NULL,
          gone INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(owner, repo)
        );
        CREATE TABLE meta(
          key TEXT PRIMARY KEY,
          value TEXT
        );
        CREATE TABLE file_meta(
          owner TEXT NOT NULL,
          repo TEXT NOT NULL,
          path TEXT NOT NULL,
          resolved_sha TEXT,
          committed_date REAL,
          etag TEXT,
          fetched_at REAL NOT NULL,
          gone INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(owner, repo, path)
        );
        CREATE TABLE key_sources(
          key_hash TEXT NOT NULL,
          url_hash TEXT NOT NULL,
          first_seen_ts REAL,
          last_seen_ts REAL,
          PRIMARY KEY(key_hash, url_hash)
        );
        CREATE INDEX idx_links_repo ON links(owner, repo);
        CREATE INDEX idx_links_last_seen ON links(last_seen_ts);
        CREATE INDEX idx_links_provider ON links(provider);
        CREATE INDEX idx_keys_recheck ON keys(status, last_recheck_ts);
        CREATE INDEX idx_file_meta_fetched ON file_meta(fetched_at);
        CREATE INDEX idx_key_sources_url ON key_sources(url_hash);
    """)

    # Insert a known link row.
    from storage.registry import canonical_url, url_hash as compute_hash
    url = "https://github.com/o/r/blob/main/a.py"
    canon = canonical_url(url)
    digest = compute_hash(url)
    conn.execute(
        "INSERT INTO links (url_hash, url, owner, repo, path, first_seen_ts, last_seen_ts, visit_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (digest, canon, "o", "r", "a.py", 100.0, 200.0, "discovered"),
    )
    conn.execute("PRAGMA user_version = 6")
    conn.commit()
    conn.close()

    # Open through Registry: migration must run.
    registry = Registry(workspace, enabled=True)
    registry.start()
    registry.stop()

    # user_version is now 8.
    assert db_scalar(workspace, "PRAGMA user_version") == 8

    # wire_coverage table exists.
    tables = {row["name"] for row in db_rows(workspace, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "wire_coverage" in tables

    # idx_links_visit_status exists.
    indexes = {row["name"] for row in db_rows(workspace, "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_links_visit_status" in indexes

    # Pre-existing links row untouched.
    assert db_scalar(workspace, "SELECT COUNT(*) FROM links") == 1
    row = db_rows(workspace, "SELECT url, owner, repo, visit_status FROM links WHERE url_hash = ?", [digest])[0]
    assert row["url"] == canon
    assert row["visit_status"] == "discovered"


# ---------------------------------------------------------------------------
# W3: page-1 seam semantics
# ---------------------------------------------------------------------------
def test_w3_page1_seam_semantics(workspace, db_rows, db_scalar):
    """W3: record_wire_page(page=1, total=950, page_size=100, n_results=100)
    writes a row with total_count=950, pages_needed=10, bitmap bit0, pages_done=1,
    results=100, first_ts and last_ts set."""
    from search.querykey import fingerprint
    from storage.registry import patterns_hash

    registry = Registry(workspace, enabled=True)
    registry.start()

    query = "/sk-[a-zA-Z0-9]{32}/"
    use_api = True
    ph = patterns_hash(key_pattern="sk-[A-Za-z0-9]{16}")
    pages_needed = 10  # ceil(950/100)

    registry.record_wire_page(
        query=query,
        use_api=use_api,
        patterns_hash_value=ph,
        page=1,
        page_size=100,
        total=950,
        n_results=100,
        pages_needed=pages_needed,
    )
    registry.stop()

    wire_fp = fingerprint(query, use_api)
    rows = db_rows(
        workspace,
        "SELECT * FROM wire_coverage WHERE wire_fp = ? AND patterns_hash = ?",
        [wire_fp, ph],
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["total_count"] == 950
    assert row["pages_needed"] == 10
    assert row["pages_bitmap"] == 1  # bit0
    assert row["pages_done"] == 1
    assert row["results"] == 100
    assert row["first_ts"] is not None
    assert row["last_ts"] is not None
    assert row["first_ts"] <= row["last_ts"]


# ---------------------------------------------------------------------------
# W4: F-01(i) out-of-order pages
# ---------------------------------------------------------------------------
def test_w4_out_of_order_pages(workspace, db_rows):
    """W4: pages 1 then 5 -> watermark=1; then 2,3,4 -> watermark=5."""
    from search.querykey import fingerprint
    from storage.registry import patterns_hash

    registry = Registry(workspace, enabled=True)
    registry.start()

    query = "/sk-[a-zA-Z0-9]{32}/"
    use_api = True
    ph = patterns_hash(key_pattern="sk-[A-Za-z0-9]{16}")

    # Page 1.
    registry.record_wire_page(
        query=query, use_api=use_api, patterns_hash_value=ph,
        page=1, page_size=100, total=1000, n_results=100, pages_needed=10,
    )
    # Page 5 (out of order).
    registry.record_wire_page(
        query=query, use_api=use_api, patterns_hash_value=ph,
        page=5, page_size=100, total=None, n_results=100, pages_needed=None,
    )
    registry.stop()

    wire_fp = fingerprint(query, use_api)
    row = db_rows(
        workspace,
        "SELECT * FROM wire_coverage WHERE wire_fp = ? AND patterns_hash = ?",
        [wire_fp, ph],
    )[0]
    # Bitmap has bits 0 and 4 (pages 1 and 5).
    assert row["pages_bitmap"] == (1 | 16)  # bit0 + bit4
    # Watermark: only page 1 is contiguous from bit0.
    assert row["pages_done"] == 1

    # Now record pages 2, 3, 4.
    registry2 = Registry(workspace, enabled=True)
    registry2.start()
    for p in [2, 3, 4]:
        registry2.record_wire_page(
            query=query, use_api=use_api, patterns_hash_value=ph,
            page=p, page_size=100, total=None, n_results=100, pages_needed=None,
        )
    registry2.stop()

    row = db_rows(
        workspace,
        "SELECT * FROM wire_coverage WHERE wire_fp = ? AND patterns_hash = ?",
        [wire_fp, ph],
    )[0]
    # Bitmap has bits 0,1,2,3,4 (pages 1-5).
    assert row["pages_bitmap"] == 0b11111
    # Watermark: pages 1-5 contiguous.
    assert row["pages_done"] == 5


# ---------------------------------------------------------------------------
# W5: F-01(ii) duplicate page idempotence
# ---------------------------------------------------------------------------
def test_w5_duplicate_page_idempotent(workspace, db_rows):
    """W5: recording page 3 twice does not move watermark, results counted ONCE."""
    from search.querykey import fingerprint
    from storage.registry import patterns_hash

    registry = Registry(workspace, enabled=True)
    registry.start()

    query = "/sk-[a-zA-Z0-9]{32}/"
    use_api = True
    ph = patterns_hash(key_pattern="sk-[A-Za-z0-9]{16}")

    # Record pages 1, 2 first so watermark is 2.
    registry.record_wire_page(
        query=query, use_api=use_api, patterns_hash_value=ph,
        page=1, page_size=100, total=500, n_results=100, pages_needed=5,
    )
    registry.record_wire_page(
        query=query, use_api=use_api, patterns_hash_value=ph,
        page=2, page_size=100, total=None, n_results=100, pages_needed=None,
    )
    # Record page 3 first time.
    registry.record_wire_page(
        query=query, use_api=use_api, patterns_hash_value=ph,
        page=3, page_size=100, total=None, n_results=100, pages_needed=None,
    )
    registry.stop()

    wire_fp = fingerprint(query, use_api)
    row = db_rows(
        workspace,
        "SELECT * FROM wire_coverage WHERE wire_fp = ? AND patterns_hash = ?",
        [wire_fp, ph],
    )[0]
    assert row["pages_done"] == 3
    assert row["results"] == 300  # 3 pages * 100

    # Record page 3 again (duplicate).
    registry2 = Registry(workspace, enabled=True)
    registry2.start()
    registry2.record_wire_page(
        query=query, use_api=use_api, patterns_hash_value=ph,
        page=3, page_size=100, total=None, n_results=100, pages_needed=None,
    )
    registry2.stop()

    row = db_rows(
        workspace,
        "SELECT * FROM wire_coverage WHERE wire_fp = ? AND patterns_hash = ?",
        [wire_fp, ph],
    )[0]
    # Watermark unchanged.
    assert row["pages_done"] == 3
    # Results counted only once for the duplicate.
    assert row["results"] == 300


# ---------------------------------------------------------------------------
# W6: hole not covered (unit analogue of F-01(iii))
# ---------------------------------------------------------------------------
def test_w6_hole_not_covered(workspace, db_rows):
    """W6: pages 1-4 and 6 of 6 needed -> pages_done==4 < pages_needed==6."""
    from search.querykey import fingerprint
    from storage.registry import patterns_hash

    registry = Registry(workspace, enabled=True)
    registry.start()

    query = "/sk-[a-zA-Z0-9]{32}/"
    use_api = True
    ph = patterns_hash(key_pattern="sk-[A-Za-z0-9]{16}")

    registry.record_wire_page(
        query=query, use_api=use_api, patterns_hash_value=ph,
        page=1, page_size=100, total=600, n_results=100, pages_needed=6,
    )
    for p in [2, 3, 4, 6]:
        registry.record_wire_page(
            query=query, use_api=use_api, patterns_hash_value=ph,
            page=p, page_size=100, total=None, n_results=100, pages_needed=None,
        )
    registry.stop()

    wire_fp = fingerprint(query, use_api)
    row = db_rows(
        workspace,
        "SELECT * FROM wire_coverage WHERE wire_fp = ? AND patterns_hash = ?",
        [wire_fp, ph],
    )[0]
    # Watermark stops at 4 (page 5 missing).
    assert row["pages_done"] == 4
    assert row["pages_needed"] == 6
    assert row["pages_done"] < row["pages_needed"]


# ---------------------------------------------------------------------------
# W7: bit clamp insurance (page > 64)
# ---------------------------------------------------------------------------
def test_w7_bit_clamp_insurance(workspace, db_rows):
    """W7: page=65 does not crash, bit63 is set (clamp), watermark sane."""
    from search.querykey import fingerprint
    from storage.registry import patterns_hash

    registry = Registry(workspace, enabled=True)
    registry.start()

    query = "/sk-[a-zA-Z0-9]{32}/"
    use_api = True
    ph = patterns_hash(key_pattern="sk-[A-Za-z0-9]{16}")

    # page=65 should clamp to bit 62.
    registry.record_wire_page(
        query=query, use_api=use_api, patterns_hash_value=ph,
        page=65, page_size=100, total=10000, n_results=50, pages_needed=100,
    )
    registry.stop()

    wire_fp = fingerprint(query, use_api)
    row = db_rows(
        workspace,
        "SELECT * FROM wire_coverage WHERE wire_fp = ? AND patterns_hash = ?",
        [wire_fp, ph],
    )[0]
    # Bit 62 is set (the clamp).
    assert row["pages_bitmap"] & (1 << 62) != 0
    # Watermark: bit0 not set, so pages_done=0.
    assert row["pages_done"] == 0


# ---------------------------------------------------------------------------
# W8: disabled no-op
# ---------------------------------------------------------------------------
def test_w8_disabled_noop(workspace, db_scalar, registry_path):
    """W8: enabled=False -> record_wire_page does nothing, no row written."""
    from storage.registry import patterns_hash

    registry = Registry(workspace, enabled=False)
    registry.start()  # returns False

    # Should not raise.
    registry.record_wire_page(
        query="/sk-[a-zA-Z0-9]{32}/",
        use_api=True,
        patterns_hash_value=patterns_hash(key_pattern="sk-[A-Za-z0-9]{16}"),
        page=1,
        page_size=100,
        total=500,
        n_results=100,
        pages_needed=5,
    )

    # No database file created.
    import os
    assert not os.path.exists(registry_path(workspace))


# ---------------------------------------------------------------------------
# W9: seam integration — page-1 seam writes wire_coverage row
# ---------------------------------------------------------------------------
def test_w9_seam_integration_page1(monkeypatch, workspace, db_rows, db_scalar):
    """W9: driving the real page-1 seam through SearchStage writes a
    wire_coverage row with page 1 recorded."""
    from config.schemas import Config, RefineGovernorConfig, StageConfig, TaskConfig
    from core.models import Patterns, SearchTask
    from search import client as search_client
    from search.refine_governor import RefineGovernor
    from stage.base import StageResources
    from stage.definition import SearchStage
    from search.querykey import fingerprint

    PROVIDER = "deepseek"
    KEY_PATTERN = r"sk-[A-Za-z0-9]{16}"
    RAW_SEED = "/sk-[a-zA-Z0-9]{32}/"

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
        mode="off", max_refine_depth=2, max_partitions_per_refine=128,
        max_search_tasks_per_run=10000, drop_wire_indistinguishable=False,
    )
    cfg = Config()
    cfg.pipeline.failure_handling = "legacy"
    cfg.refine_governor = RefineGovernorConfig(
        mode="off", max_refine_depth=2, max_partitions_per_refine=128,
        max_search_tasks_per_run=10000,
    )

    # Start a real registry.
    registry = Registry(workspace, enabled=True)
    registry.start()

    resources = StageResources(
        limiter=None, providers={}, config=cfg,
        task_configs={PROVIDER: task_config},
        auth=FakeAuth(), registry=registry,
        refine_governor=gov,
    )
    outputs = []
    stage = SearchStage(resources, outputs.append, thread_count=1)

    # Mock search to return 950 results (total=950, 100 on page 1).
    def fake_search(**kwargs):
        total = 950
        n = min(total, 100)
        urls = [f"https://github.com/o/r/blob/main/f{i}.py" for i in range(n)]
        return urls, total, ""

    monkeypatch.setattr(search_client, "search_with_count", fake_search)

    task = SearchTask(
        provider=PROVIDER, query=RAW_SEED, regex=KEY_PATTERN,
        page=1, use_api=True,
    )
    out = stage.process_task(task)
    assert out is not None

    # Drain registry.
    registry.stop()

    # A wire_coverage row must exist with page 1 recorded.
    rows = db_rows(workspace, "SELECT * FROM wire_coverage")
    assert len(rows) >= 1
    row = rows[0]
    assert row["pages_bitmap"] & 1 == 1  # bit0 set
    assert row["pages_done"] == 1
    assert row["total_count"] == 950


# ---------------------------------------------------------------------------
# W10: seam integration — page-N seam writes wire_coverage row
# ---------------------------------------------------------------------------
def test_w10_seam_integration_pageN(monkeypatch, workspace, db_rows, db_scalar):
    """W10: driving a page-2 task through SearchStage writes a wire_coverage row
    with bit1 set, total_count preserved from page-1, results accumulated."""
    from config.schemas import Config, RefineGovernorConfig, StageConfig, TaskConfig
    from core.models import Patterns, SearchTask
    from search import client as search_client
    from search.refine_governor import RefineGovernor
    from stage.base import StageResources
    from stage.definition import SearchStage
    from search.querykey import fingerprint
    from storage.registry import patterns_hash

    PROVIDER = "deepseek"
    KEY_PATTERN = r"sk-[A-Za-z0-9]{16}"
    RAW_SEED = "/sk-[a-zA-Z0-9]{32}/"

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
        mode="off", max_refine_depth=2, max_partitions_per_refine=128,
        max_search_tasks_per_run=10000, drop_wire_indistinguishable=False,
    )
    cfg = Config()
    cfg.pipeline.failure_handling = "legacy"
    cfg.refine_governor = RefineGovernorConfig(
        mode="off", max_refine_depth=2, max_partitions_per_refine=128,
        max_search_tasks_per_run=10000,
    )

    registry = Registry(workspace, enabled=True)
    registry.start()

    # Pre-seed page-1 data so page-2 can accumulate.
    ph = patterns_hash(key_pattern=KEY_PATTERN)
    wire_fp = fingerprint(RAW_SEED, True)
    registry.record_wire_page(
        query=RAW_SEED, use_api=True, patterns_hash_value=ph,
        page=1, page_size=100, total=950, n_results=100, pages_needed=10,
    )

    resources = StageResources(
        limiter=None, providers={}, config=cfg,
        task_configs={PROVIDER: task_config},
        auth=FakeAuth(), registry=registry,
        refine_governor=gov,
    )
    outputs = []
    stage = SearchStage(resources, outputs.append, thread_count=1)

    # Mock search for page 2 (page-N uses client.search_code, not search_with_count).
    def fake_search_code(**kwargs):
        n = 100
        urls = [f"https://github.com/o/r/blob/main/f{i}.py" for i in range(n)]
        return urls, ""

    monkeypatch.setattr(search_client, "search_code", fake_search_code)

    task = SearchTask(
        provider=PROVIDER, query=RAW_SEED, regex=KEY_PATTERN,
        page=2, use_api=True,
    )
    out = stage.process_task(task)
    assert out is not None

    registry.stop()

    # wire_coverage row must have bit1 set, total_count preserved.
    rows = db_rows(
        workspace,
        "SELECT * FROM wire_coverage WHERE wire_fp = ? AND patterns_hash = ?",
        [wire_fp, ph],
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["pages_bitmap"] & 2 == 2  # bit1 set (page 2)
    assert row["pages_bitmap"] & 1 == 1  # bit0 still set (from page 1)
    assert row["total_count"] == 950  # preserved from page 1 via COALESCE
    assert row["results"] == 200  # 100 + 100
