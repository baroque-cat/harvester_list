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
