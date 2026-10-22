"""Tests for the one-time idempotent migration tool.

Traceability: link-registry-S20, link-registry-S21, link-registry-S22.
"""

import os

from storage.registry import Registry
from tools.registry_migrate import migrate_workspace

URL_A = "https://github.com/o/r/blob/main/a.py"
URL_B = "https://github.com/o/r/blob/main/b.py"
URL_C = "https://github.com/o/r/blob/main/c.py"


def test_s20_migration_imports_legacy_links_conservatively(workspace, db_rows, db_scalar, write_links_shard):
    """link-registry-S20: one conservative row per distinct canonical URL."""
    write_links_shard(
        workspace, "openai", [f"{URL_A}#L10", URL_B], first_ts=1000.0, last_ts=1100.0, name="links_a.ndjson"
    )
    write_links_shard(
        workspace, "openai", [f"{URL_A}#L99", URL_C], first_ts=1200.0, last_ts=1300.0, name="links_b.ndjson"
    )

    stats = migrate_workspace(workspace)

    assert stats["links"] == 3
    assert db_scalar(workspace, "SELECT COUNT(*) FROM links") == 3

    row = db_rows(workspace, "SELECT * FROM links WHERE url = ?", [URL_A])[0]
    assert row["visit_status"] == "discovered"
    assert row["gathered_ts"] is None
    assert row["provider"] == "openai"


def test_s21_migration_is_idempotent(workspace, db_scalar, write_links_shard, write_result_shard):
    """link-registry-S21: a second pass changes no row counts."""
    write_links_shard(workspace, "openai", [URL_A, URL_B], first_ts=1000.0, last_ts=1100.0)
    write_result_shard(
        workspace,
        "openai",
        "material",
        [
            {"address": "https://api.example.com", "endpoint": "", "key": "sk-abcdef1234567890", "model": "gpt-4o"},
            {"address": "", "endpoint": "https://api.example.com/v1", "key": "sk-zzzzzz1234567890", "model": ""},
        ],
    )
    write_result_shard(
        workspace,
        "openai",
        "valid",
        [{"address": "", "endpoint": "", "key": "sk-validkey1234567890", "model": ""}],
    )

    migrate_workspace(workspace)
    links_first = db_scalar(workspace, "SELECT COUNT(*) FROM links")
    keys_first = db_scalar(workspace, "SELECT COUNT(*) FROM keys")

    migrate_workspace(workspace)
    assert db_scalar(workspace, "SELECT COUNT(*) FROM links") == links_first
    assert db_scalar(workspace, "SELECT COUNT(*) FROM keys") == keys_first
    assert keys_first == 3


def test_s22_migration_never_regresses_live_data(workspace, db_rows, db_scalar, write_links_shard):
    """link-registry-S22: fresher in-place gathered_ok data wins over shards."""
    registry = Registry(workspace, enabled=True)
    registry.start()
    registry.record_link(URL_A, provider="openai", ts=5000.0)
    registry.record_gather(URL_A, provider="openai", patterns_hash="ph", success=True, ts=5001.0)
    registry.stop()

    # An older shard mentions the same URL.
    write_links_shard(workspace, "openai", [URL_A], first_ts=1000.0, last_ts=1100.0)

    migrate_workspace(workspace)

    row = db_rows(workspace, "SELECT visit_status, gathered_ts FROM links WHERE url = ?", [URL_A])[0]
    assert row["visit_status"] == "gathered_ok"
    assert row["gathered_ts"] == 5001.0
    assert db_scalar(workspace, "SELECT COUNT(*) FROM links") == 1


def test_migration_writes_early_stop_trust_marker(workspace, db_scalar, write_links_shard):
    """Task 3.1: a successful migration marks it complete for the trust gate."""
    write_links_shard(workspace, "openai", [URL_A], first_ts=1000.0, last_ts=1100.0)

    migrate_workspace(workspace)

    assert db_scalar(workspace, "SELECT COUNT(*) FROM meta WHERE key = 'migration_complete'") == 1
    assert db_scalar(workspace, "SELECT value FROM meta WHERE key = 'migration_complete'") is not None


def test_migration_dry_run_writes_nothing(workspace, registry_path, write_links_shard):
    """Supporting check for --dry-run: no registry file is created."""
    write_links_shard(workspace, "openai", [URL_A], first_ts=1000.0, last_ts=1100.0)

    stats = migrate_workspace(workspace, dry_run=True)

    assert stats["links"] == 1
    assert not os.path.exists(registry_path(workspace))


# ---------------------------------------------------------------------------
# link-registry-S4: schema v4 migrates to v5
# ---------------------------------------------------------------------------
def test_s4_schema_v4_migrates_to_v5(workspace, db_rows, db_scalar, registry_path):
    """link-registry-S4: a v4 database migrates in place with all rows intact."""
    import sqlite3

    path = registry_path(workspace)
    conn = sqlite3.connect(path)

    # Build a v4-shaped database: same DDL as the shipped code but WITHOUT
    # the new columns (etag, etag_transport) and the file_meta table.
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
          priority REAL
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
        CREATE INDEX idx_links_repo ON links(owner, repo);
        CREATE INDEX idx_links_last_seen ON links(last_seen_ts);
        CREATE INDEX idx_links_provider ON links(provider);
        CREATE INDEX idx_keys_recheck ON keys(status, last_recheck_ts);
    """)

    # Insert a known row with known values.
    from storage.registry import canonical_url, url_hash as compute_hash

    canon = canonical_url(URL_A)
    digest = compute_hash(URL_A)
    conn.execute(
        "INSERT INTO links (url_hash, url, owner, repo, path, first_seen_ts, last_seen_ts, visit_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (digest, canon, "o", "r", "a.py", 100.0, 200.0, "discovered"),
    )
    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    conn.close()

    # Open through the shipped Registry path: migration must run.
    registry = Registry(workspace, enabled=True)
    registry.start()
    registry.stop()

    # Schema version is now 7.
    assert db_scalar(workspace, "PRAGMA user_version") == 8

    # New columns exist.
    columns = {row["name"] for row in db_rows(workspace, "PRAGMA table_info(links)")}
    assert "etag" in columns
    assert "etag_transport" in columns

    # file_meta table exists.
    tables = {row["name"] for row in db_rows(workspace, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "file_meta" in tables
    assert "key_sources" in tables

    # Pre-existing row kept its values.
    row = db_rows(workspace, "SELECT url, owner, repo, path, first_seen_ts, last_seen_ts, visit_status FROM links WHERE url_hash = ?", [digest])[0]
    assert row["url"] == canon
    assert row["owner"] == "o"
    assert row["repo"] == "r"
    assert row["path"] == "a.py"
    assert row["first_seen_ts"] == 100.0
    assert row["last_seen_ts"] == 200.0
    assert row["visit_status"] == "discovered"


# ---------------------------------------------------------------------------
# link-registry-S5: migration is idempotent
# ---------------------------------------------------------------------------
def test_s5_migration_is_idempotent_at_v5(workspace, db_scalar):
    """link-registry-S5: opening an already-migrated database raises nothing."""
    import sqlite3

    # First open: bootstraps at v7.
    registry = Registry(workspace, enabled=True)
    registry.start()
    registry.stop()
    assert db_scalar(workspace, "PRAGMA user_version") == 8

    # Second open: migration guard runs again, must be a no-op.
    registry2 = Registry(workspace, enabled=True)
    registry2.start()
    registry2.stop()
    assert db_scalar(workspace, "PRAGMA user_version") == 8
