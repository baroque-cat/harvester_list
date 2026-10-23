"""Restore CLI tests (slice 4.3, wave 4).

Traceability: 05_purge_cli.md §5.4 (restore canon), 00_INDEX.md line 72 (wave 4).

Design:
- ``tools/restore.py`` implements the restore logic.
- ``main.py`` adds a ``restore`` subparser with dest="command".
- Minimum for registry: restore NDJSON dumps from purge backup.
- Interlocks: lock (symmetry, holder="restore"), wal-freshness, ambiguity refuse.
- N-11: restore documents that registry tables have no dedup_id; queue restore = manual byte-copy.

RED predictions (all pins must FAIL before product code):
- P1: ``tools.restore`` module does not exist → ImportError.
- P2: ``restore`` subparser not in main.py → subprocess exits with usage error.
- P3-P12: same ImportError/AttributeError cascade or missing functionality.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _lock_path(workspace: str) -> str:
    return os.path.join(workspace, ".harvester.lock")


def _write_lock(workspace: str, pid: int, holder: str, boot_ts: float = 0.0) -> str:
    """Write a lock file manually (for test setup)."""
    path = _lock_path(workspace)
    data = {"pid": pid, "boot_ts": boot_ts or time.time(), "holder": holder}
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, json.dumps(data).encode())
    finally:
        os.close(fd)
    return path


def _spawn_live_process():
    """Spawn a short-lived subprocess and return its Popen object."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.3)
    assert proc.poll() is None, "spawned process died immediately"
    return proc


def _minimal_config_yaml(workspace: str, with_credentials: bool = True) -> str:
    """Generate a minimal config YAML pointing workspace at tmp dir."""
    import textwrap
    if with_credentials:
        return textwrap.dedent(f"""\
            global:
              workspace: "{workspace}"
              github_credentials:
                sessions:
                  - "test_session_abc123def456"
            persistence:
              shutdown_timeout: 5
            registry:
              enabled: true
            tasks:
              - name: "test_provider"
                enabled: true
                provider_type: "openai_like"
                use_api: false
                stages:
                  search: true
                  gather: true
                  check: true
                  inspect: true
                extras: {{}}
                api:
                  base_url: "https://api.example.com"
                  completion_path: "/v1/chat/completions"
                  model_path: "/v1/models"
                  default_model: "test"
                  auth_key: "Authorization"
                  extra_headers: {{}}
                  api_version: ""
                  timeout: 30
                  retries: 3
                patterns:
                  key_pattern: "sk-[a-zA-Z0-9]{{20}}"
                  address_pattern: ""
                  endpoint_pattern: ""
                  model_pattern: ""
                conditions:
                  - query: '"test"'
                rate_limit:
                  base_rate: 1.0
                  burst_limit: 3
                  adaptive: true
                storage:
                  directory: ""
                  plan: ""
        """)
    else:
        return textwrap.dedent(f"""\
            global:
              workspace: "{workspace}"
            persistence:
              shutdown_timeout: 5
            registry:
              enabled: true
            tasks:
              - name: "test_provider"
                enabled: true
                provider_type: "openai_like"
                use_api: false
                stages:
                  search: true
                  gather: true
                  check: true
                  inspect: true
                extras: {{}}
                api:
                  base_url: "https://api.example.com"
                  completion_path: "/v1/chat/completions"
                  model_path: "/v1/models"
                  default_model: "test"
                  auth_key: "Authorization"
                  extra_headers: {{}}
                  api_version: ""
                  timeout: 30
                  retries: 3
                patterns:
                  key_pattern: "sk-[a-zA-Z0-9]{{20}}"
                  address_pattern: ""
                  endpoint_pattern: ""
                  model_pattern: ""
                conditions:
                  - query: '"test"'
                rate_limit:
                  base_rate: 1.0
                  burst_limit: 3
                  adaptive: true
                storage:
                  directory: ""
                  plan: ""
        """)


def _create_registry(workspace: str) -> str:
    """Create a minimal registry.sqlite with schema and data."""
    db_path = os.path.join(workspace, "registry.sqlite")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS links(
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
        CREATE TABLE IF NOT EXISTS link_coverage(
          url_hash TEXT NOT NULL,
          provider TEXT NOT NULL,
          patterns_hash TEXT NOT NULL,
          gathered_ts REAL NOT NULL,
          PRIMARY KEY(url_hash, provider, patterns_hash)
        );
        CREATE TABLE IF NOT EXISTS keys(
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
        CREATE TABLE IF NOT EXISTS runs(
          run_id TEXT PRIMARY KEY,
          started_at REAL,
          finished_at REAL,
          config_digest TEXT,
          degraded INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS repos(
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
        CREATE TABLE IF NOT EXISTS meta(
          key TEXT PRIMARY KEY,
          value TEXT
        );
        CREATE TABLE IF NOT EXISTS file_meta(
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
        CREATE TABLE IF NOT EXISTS key_sources(
          key_hash TEXT NOT NULL,
          url_hash TEXT NOT NULL,
          first_seen_ts REAL,
          last_seen_ts REAL,
          PRIMARY KEY(key_hash, url_hash)
        );
        CREATE TABLE IF NOT EXISTS wire_coverage(
          wire_fp      TEXT NOT NULL,
          patterns_hash TEXT NOT NULL,
          total_count  INTEGER,
          pages_bitmap INTEGER DEFAULT 0,
          pages_done   INTEGER DEFAULT 0,
          pages_needed INTEGER,
          results      INTEGER DEFAULT 0,
          first_ts     REAL, last_ts REAL,
          owner_provider TEXT,
          PRIMARY KEY (wire_fp, patterns_hash)
        );
    """)
    # Insert test data
    conn.execute("INSERT INTO links (url_hash, url, visit_status, file_commit_date) VALUES ('h1', 'http://example.com/1', 'gathered', 1234567890.0)")
    conn.execute("INSERT INTO links (url_hash, url, visit_status) VALUES ('h2', 'http://example.com/2', 'discovered')")
    conn.execute("INSERT INTO link_coverage (url_hash, provider, patterns_hash, gathered_ts) VALUES ('h1', 'github', 'ph1', 1000.0)")
    conn.execute("INSERT INTO keys (key_hash, provider, status) VALUES ('k1', 'openai', 'valid')")
    conn.execute("INSERT INTO repos (owner, repo, fetched_at) VALUES ('owner1', 'repo1', 1000.0)")
    conn.execute("INSERT INTO file_meta (owner, repo, path, fetched_at) VALUES ('o1', 'r1', 'p1', 2000.0)")
    conn.execute("INSERT INTO key_sources (key_hash, url_hash, first_seen_ts) VALUES ('k1', 'h1', 500.0)")
    conn.execute("INSERT INTO wire_coverage (wire_fp, patterns_hash, total_count, owner_provider) VALUES ('wf1', 'wp1', 100, 'github')")
    conn.commit()
    conn.close()

    # Make WAL files old (> 120s) to avoid triggering WAL freshness check
    wal_path = db_path + "-wal"
    shm_path = db_path + "-shm"
    if os.path.exists(wal_path):
        old_time = time.time() - 200
        os.utime(wal_path, (old_time, old_time))
    if os.path.exists(shm_path):
        old_time = time.time() - 200
        os.utime(shm_path, (old_time, old_time))

    return db_path


def _backup_registry_tables(workspace: str, backup_dir: str) -> None:
    """Backup registry tables as NDJSON (mirror of purge backup)."""
    db_path = os.path.join(workspace, "registry.sqlite")
    if not os.path.exists(db_path):
        return

    os.makedirs(backup_dir, exist_ok=True)
    tables = ["links", "link_coverage", "file_meta", "repos", "wire_coverage", "keys", "key_sources"]
    conn = sqlite3.connect(db_path)
    for table in tables:
        try:
            cursor = conn.execute(f"SELECT * FROM {table}")
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            if rows:
                backup_path = os.path.join(backup_dir, f"{table}.ndjson")
                with open(backup_path, "w", encoding="utf-8") as f:
                    for row in rows:
                        record = dict(zip(columns, row))
                        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass
    conn.close()


def _empty_registry_tables(workspace: str) -> None:
    """Empty all registry tables (simulate post-purge state)."""
    db_path = os.path.join(workspace, "registry.sqlite")
    conn = sqlite3.connect(db_path)
    tables = ["links", "link_coverage", "file_meta", "repos", "wire_coverage", "keys", "key_sources"]
    for table in tables:
        try:
            conn.execute(f"DELETE FROM {table}")
        except Exception:
            pass
    conn.commit()
    conn.close()


def _setup_workspace_with_backup(tmp_path) -> tuple:
    """Set up a workspace with registry data and backup."""
    workspace = str(tmp_path / "ws")
    os.makedirs(workspace)
    cfg_path = str(tmp_path / "config.yaml")
    with open(cfg_path, "w") as f:
        f.write(_minimal_config_yaml(workspace))
    _create_registry(workspace)
    backup_dir = str(tmp_path / "backup")
    _backup_registry_tables(workspace, backup_dir)
    _empty_registry_tables(workspace)
    return workspace, cfg_path, backup_dir


# ---------------------------------------------------------------------------
# T11-restore: round-trip (dump → restore → counters match)
# ---------------------------------------------------------------------------

class TestRestoreRoundTrip:
    """T11-restore: full round-trip — purge backup → restore → counters match."""

    def test_restore_round_trip_all_tables(self, tmp_path):
        """T11-restore: restore from NDJSON backup recovers all tables."""
        workspace, cfg_path, backup_dir = _setup_workspace_with_backup(tmp_path)

        # Verify tables are empty after purge simulation
        conn = sqlite3.connect(os.path.join(workspace, "registry.sqlite"))
        for table in ["links", "link_coverage", "keys", "repos", "wire_coverage"]:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert count == 0, f"{table} should be empty before restore"
        conn.close()

        # Restore
        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "restore", "--from", backup_dir, "--yes"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"restore failed: {result.stderr}"

        # Verify counters match
        conn = sqlite3.connect(os.path.join(workspace, "registry.sqlite"))
        links_count = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
        link_cov_count = conn.execute("SELECT COUNT(*) FROM link_coverage").fetchone()[0]
        keys_count = conn.execute("SELECT COUNT(*) FROM keys").fetchone()[0]
        repos_count = conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0]
        wire_cov_count = conn.execute("SELECT COUNT(*) FROM wire_coverage").fetchone()[0]
        file_meta_count = conn.execute("SELECT COUNT(*) FROM file_meta").fetchone()[0]
        key_sources_count = conn.execute("SELECT COUNT(*) FROM key_sources").fetchone()[0]
        conn.close()

        assert links_count == 2, f"links count mismatch: {links_count}"
        assert link_cov_count == 1, f"link_coverage count mismatch: {link_cov_count}"
        assert keys_count == 1, f"keys count mismatch: {keys_count}"
        assert repos_count == 1, f"repos count mismatch: {repos_count}"
        assert wire_cov_count == 1, f"wire_coverage count mismatch: {wire_cov_count}"
        assert file_meta_count == 1, f"file_meta count mismatch: {file_meta_count}"
        assert key_sources_count == 1, f"key_sources count mismatch: {key_sources_count}"

    def test_restore_preserves_column_values(self, tmp_path):
        """T11-restore: restore preserves specific column values (MAX-merge, patterns_hash)."""
        workspace, cfg_path, backup_dir = _setup_workspace_with_backup(tmp_path)

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "restore", "--from", backup_dir, "--yes"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"restore failed: {result.stderr}"

        # Verify specific values
        conn = sqlite3.connect(os.path.join(workspace, "registry.sqlite"))
        # links: file_commit_date preserved
        row = conn.execute("SELECT file_commit_date, visit_status FROM links WHERE url_hash='h1'").fetchone()
        assert row is not None, "link h1 not restored"
        assert row[0] == 1234567890.0, f"file_commit_date mismatch: {row[0]}"
        assert row[1] == "gathered", f"visit_status mismatch: {row[1]}"
        # link_coverage: patterns_hash preserved
        row = conn.execute("SELECT patterns_hash FROM link_coverage WHERE url_hash='h1'").fetchone()
        assert row is not None, "link_coverage h1 not restored"
        assert row[0] == "ph1", f"patterns_hash mismatch: {row[0]}"
        # wire_coverage: owner_provider preserved
        row = conn.execute("SELECT owner_provider FROM wire_coverage WHERE wire_fp='wf1'").fetchone()
        assert row is not None, "wire_coverage wf1 not restored"
        assert row[0] == "github", f"owner_provider mismatch: {row[0]}"
        conn.close()


# ---------------------------------------------------------------------------
# Refuse: non-empty target table
# ---------------------------------------------------------------------------

class TestRefuseNonEmptyTable:
    """Restore refuses if target table is non-empty (not a merge tool)."""

    def test_refuse_non_empty_table(self, tmp_path):
        """Restore refuses if any target table has rows."""
        workspace, cfg_path, backup_dir = _setup_workspace_with_backup(tmp_path)
        # Re-insert a row into links (simulate non-empty state)
        db_path = os.path.join(workspace, "registry.sqlite")
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO links (url_hash, url, visit_status) VALUES ('h99', 'http://example.com/99', 'discovered')")
        conn.commit()
        conn.close()

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "restore", "--from", backup_dir, "--yes"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode != 0, "restore should refuse non-empty table"
        combined = result.stderr + result.stdout
        assert "non-empty" in combined.lower() or "refuse" in combined.lower(), \
            f"expected non-empty/refuse message: {combined[:500]}"


# ---------------------------------------------------------------------------
# Refuse: live lock
# ---------------------------------------------------------------------------

class TestRefuseLiveLock:
    """Restore refuses when a live process holds the lock."""

    def test_refuse_live_lock(self, tmp_path):
        """Restore refuses with nonzero exit when live lock exists."""
        workspace, cfg_path, backup_dir = _setup_workspace_with_backup(tmp_path)

        proc = _spawn_live_process()
        try:
            _write_lock(workspace, pid=proc.pid, holder="pipeline")

            result = subprocess.run(
                [sys.executable, os.path.join(REPO_ROOT, "main.py"),
                 "-c", cfg_path, "restore", "--from", backup_dir, "--yes"],
                capture_output=True, text=True, timeout=30,
                cwd=str(tmp_path),
            )
            assert result.returncode != 0, "restore should refuse with live lock"
            combined = result.stderr + result.stdout
            assert "lock" in combined.lower() or "refuse" in combined.lower(), \
                f"expected lock/refuse message: {combined[:500]}"
        finally:
            proc.terminate()
            proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# Stale lock: takeover
# ---------------------------------------------------------------------------

class TestStaleLockTakeover:
    """Stale lock is ignored with loud INFO."""

    def test_stale_lock_takeover(self, tmp_path):
        """Stale lock → INFO message + restore proceeds."""
        workspace, cfg_path, backup_dir = _setup_workspace_with_backup(tmp_path)

        # Spawn then kill a process
        proc = _spawn_live_process()
        dead_pid = proc.pid
        proc.terminate()
        proc.wait(timeout=5)

        _write_lock(workspace, pid=dead_pid, holder="pipeline", boot_ts=1000.0)

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "restore", "--from", backup_dir, "--yes"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"restore should proceed with stale lock: {result.stderr}"
        combined = result.stderr + result.stdout
        assert "stale" in combined.lower(), f"expected 'stale' message: {combined[:500]}"


# ---------------------------------------------------------------------------
# Refuse: --from missing
# ---------------------------------------------------------------------------

class TestRefuseMissingFrom:
    """Restore refuses if --from directory does not exist."""

    def test_refuse_missing_from_dir(self, tmp_path):
        """Restore refuses if --from directory does not exist."""
        workspace = str(tmp_path / "ws")
        os.makedirs(workspace)
        cfg_path = str(tmp_path / "config.yaml")
        with open(cfg_path, "w") as f:
            f.write(_minimal_config_yaml(workspace))
        _create_registry(workspace)
        _empty_registry_tables(workspace)

        nonexistent_dir = str(tmp_path / "nonexistent")
        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "restore", "--from", nonexistent_dir, "--yes"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode != 0, "restore should refuse missing --from"
        combined = result.stderr + result.stdout
        assert "not exist" in combined.lower() or "refuse" in combined.lower(), \
            f"expected missing/refuse message: {combined[:500]}"


# ---------------------------------------------------------------------------
# Refuse: corrupted NDJSON
# ---------------------------------------------------------------------------

class TestRefuseCorruptedNDJSON:
    """Restore refuses if NDJSON is corrupted (no partial insert)."""

    def test_refuse_corrupted_ndjson(self, tmp_path):
        """Restore refuses if NDJSON has bad lines."""
        workspace, cfg_path, backup_dir = _setup_workspace_with_backup(tmp_path)

        # Corrupt one of the NDJSON files
        links_ndjson = os.path.join(backup_dir, "links.ndjson")
        if os.path.exists(links_ndjson):
            with open(links_ndjson, "a") as f:
                f.write("THIS IS NOT JSON\n")

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "restore", "--from", backup_dir, "--yes"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode != 0, "restore should refuse corrupted NDJSON"
        combined = result.stderr + result.stdout
        assert "corrupt" in combined.lower() or "invalid" in combined.lower() or "json" in combined.lower(), \
            f"expected corrupt/json message: {combined[:500]}"

        # Verify no partial insert (links table should remain empty)
        conn = sqlite3.connect(os.path.join(workspace, "registry.sqlite"))
        count = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
        conn.close()
        assert count == 0, f"links should be empty after corrupt NDJSON, got {count}"


# ---------------------------------------------------------------------------
# Partial backup: present tables restored, absent skipped
# ---------------------------------------------------------------------------

class TestPartialBackup:
    """Partial backup: present tables restored, absent skipped with counter."""

    def test_partial_backup_restore(self, tmp_path):
        """Partial backup: only links.ndjson present → restore links, skip others."""
        workspace = str(tmp_path / "ws")
        os.makedirs(workspace)
        cfg_path = str(tmp_path / "config.yaml")
        with open(cfg_path, "w") as f:
            f.write(_minimal_config_yaml(workspace))
        _create_registry(workspace)
        _empty_registry_tables(workspace)

        # Create backup with only links.ndjson
        backup_dir = str(tmp_path / "backup")
        os.makedirs(backup_dir)
        with open(os.path.join(backup_dir, "links.ndjson"), "w") as f:
            f.write('{"url_hash": "h1", "url": "http://example.com/1", "visit_status": "gathered"}\n')

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "restore", "--from", backup_dir, "--yes"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"restore failed: {result.stderr}"

        # Verify links restored
        conn = sqlite3.connect(os.path.join(workspace, "registry.sqlite"))
        links_count = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
        keys_count = conn.execute("SELECT COUNT(*) FROM keys").fetchone()[0]
        conn.close()

        assert links_count == 1, f"links should be restored: {links_count}"
        assert keys_count == 0, f"keys should remain empty: {keys_count}"

        # Verify skip message
        combined = result.stderr + result.stdout
        assert "skip" in combined.lower() or "0" in combined, \
            f"expected skip/counter message: {combined[:500]}"


# ---------------------------------------------------------------------------
# Secret masking: no secret values in output
# ---------------------------------------------------------------------------

class TestSecretMasking:
    """Secret values must not appear in stdout/stderr."""

    def test_no_secret_in_output(self, tmp_path):
        """Secret plant values must not appear in CLI output."""
        workspace = str(tmp_path / "ws")
        os.makedirs(workspace)
        cfg_path = str(tmp_path / "config.yaml")
        with open(cfg_path, "w") as f:
            f.write(_minimal_config_yaml(workspace))
        _create_registry(workspace)
        _empty_registry_tables(workspace)

        # Create backup with secret plant value
        backup_dir = str(tmp_path / "backup")
        os.makedirs(backup_dir)
        with open(os.path.join(backup_dir, "links.ndjson"), "w") as f:
            f.write('{"url_hash": "h1", "url": "http://example.com/1", "visit_status": "gathered", "secret_field": "SECRETVALUE_PLAINTEXT_1"}\n')

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "restore", "--from", backup_dir, "--yes"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        # Even if restore fails due to extra column, secret must not appear
        combined = result.stderr + result.stdout
        assert "SECRETVALUE_PLAINTEXT_1" not in combined, "secret value in CLI output"


# ---------------------------------------------------------------------------
# Parser compatibility: existing forms byte-identical
# ---------------------------------------------------------------------------

class TestParserCompatibility:
    """Existing CLI forms must remain byte-identical."""

    def test_validate_still_works(self, tmp_path):
        """--validate must still work after restore subparser added."""
        workspace = str(tmp_path / "ws")
        os.makedirs(workspace)
        cfg_path = str(tmp_path / "config.yaml")
        with open(cfg_path, "w") as f:
            f.write(_minimal_config_yaml(workspace, with_credentials=True))

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"), "-c", cfg_path, "--validate"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"--validate broken: {result.stderr}"

    def test_restore_requires_from(self, tmp_path):
        """restore without --from should error."""
        workspace = str(tmp_path / "ws")
        os.makedirs(workspace)
        cfg_path = str(tmp_path / "config.yaml")
        with open(cfg_path, "w") as f:
            f.write(_minimal_config_yaml(workspace))

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "restore", "--yes"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode != 0, "restore without --from should error"


# ---------------------------------------------------------------------------
# N-11: documentation pin (registry tables have no dedup_id)
# ---------------------------------------------------------------------------

class TestN11Documentation:
    """N-11: restore documents that registry tables have no dedup_id."""

    def test_n11_no_dedup_id_in_registry(self, tmp_path):
        """N-11: registry tables do not have dedup_id column; restore works without it."""
        workspace, cfg_path, backup_dir = _setup_workspace_with_backup(tmp_path)

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "restore", "--from", backup_dir, "--yes"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"restore failed: {result.stderr}"

        # Verify registry tables do not have dedup_id column
        conn = sqlite3.connect(os.path.join(workspace, "registry.sqlite"))
        cursor = conn.execute("PRAGMA table_info(links)")
        columns = [row[1] for row in cursor.fetchall()]
        conn.close()
        assert "dedup_id" not in columns, "links table should not have dedup_id column"

        # N-11 documentation is present in README (structural doc pin).
        with open(os.path.join(REPO_ROOT, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        assert "N-11 caveat" in readme, "README must carry the N-11 caveat"
        assert "dedup_id" in readme, "README N-11 caveat must name dedup_id"


# ---------------------------------------------------------------------------
# R13 (orchestrator remediation): real purge CLI -> real restore CLI chain.
# Guards against backup-format drift between tools/purge.py and
# tools/restore.py (the round-trip pins above use a test-side mirror writer),
# and proves single-profile undo: after `purge links`, restoring the links
# backup MUST succeed while keys/key_sources remain non-empty (canon 05 §5.4
# "minimum for registry"; fail-CLOSED applies to the tables being restored,
# not to unrelated tables the backup does not touch).
# ---------------------------------------------------------------------------

class TestRealPurgeRestoreChain:
    """R13: chained real-CLI round-trip purge links -> restore."""

    def test_purge_links_then_restore_chain(self, tmp_path):
        workspace = str(tmp_path / "ws")
        os.makedirs(workspace)
        cfg_path = str(tmp_path / "config.yaml")
        with open(cfg_path, "w") as f:
            f.write(_minimal_config_yaml(workspace))
        db_path = _create_registry(workspace)
        backup_dir = str(tmp_path / "real_backup")

        # Real purge CLI: profile links (5 link-family tables), with backup.
        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "purge", "links", "--backup-to", backup_dir, "--yes"],
            capture_output=True, text=True, timeout=60,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"purge links failed: {result.stderr}"
        assert os.path.isfile(os.path.join(backup_dir, "links.ndjson")), \
            "purge must produce links.ndjson backup"

        # Purge touched the DB: age WAL sidecars again for restore's
        # freshness interlock.
        for suffix in ("-wal", "-shm"):
            p = db_path + suffix
            if os.path.exists(p):
                old = time.time() - 200
                os.utime(p, (old, old))

        # Real restore CLI: keys/key_sources are still non-empty and NOT in
        # the backup; restore of the links-family dumps must succeed.
        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "restore", "--from", backup_dir, "--yes"],
            capture_output=True, text=True, timeout=60,
            cwd=str(tmp_path),
        )
        combined = result.stdout + result.stderr
        assert result.returncode == 0, \
            f"restore after purge-links refused (single-profile undo broken): {combined[:600]}"

        conn = sqlite3.connect(db_path)
        try:
            links_n = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
            cov_n = conn.execute("SELECT COUNT(*) FROM link_coverage").fetchone()[0]
            wire_n = conn.execute("SELECT COUNT(*) FROM wire_coverage").fetchone()[0]
            keys_n = conn.execute("SELECT COUNT(*) FROM keys").fetchone()[0]
            ks_n = conn.execute("SELECT COUNT(*) FROM key_sources").fetchone()[0]
            fcd = conn.execute(
                "SELECT file_commit_date FROM links WHERE url_hash='h1'"
            ).fetchone()[0]
            owner = conn.execute(
                "SELECT owner_provider FROM wire_coverage WHERE wire_fp='wf1'"
            ).fetchone()[0]
        finally:
            conn.close()

        assert links_n == 2, f"links rows must be restored, got {links_n}"
        assert cov_n == 1, f"link_coverage rows must be restored, got {cov_n}"
        assert wire_n == 1, f"wire_coverage rows must be restored, got {wire_n}"
        assert keys_n == 1, f"keys must be UNTOUCHED (absent from backup), got {keys_n}"
        assert ks_n == 1, f"key_sources must be UNTOUCHED, got {ks_n}"
        assert fcd == 1234567890.0, f"file_commit_date must survive chain, got {fcd}"
        assert owner == "github", f"owner_provider must survive chain, got {owner}"
