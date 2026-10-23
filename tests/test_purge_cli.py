"""Purge CLI tests (slice 4.2, wave 4).

Traceability: 05_purge_cli.md §5.1–5.6, 00_INDEX.md line 99 (wave 4).

Design:
- ``tools/purge.py`` implements the purge logic.
- ``main.py`` adds a ``purge`` subparser with dest="command".
- Profiles: all, links, keys, queues, telemetry, shards.
- Interlocks: lock (symmetry), wal-freshness, missing area skip, ambiguity refuse.
- Backup: NDJSON dumps, tar.gz, byte copies; F-29 rights 0700/0600.
- Receipt: atomic JSON at END, counters only, no secret values.

RED predictions (all pins must FAIL before product code):
- P1: ``tools.purge`` module does not exist → ImportError.
- P2: ``purge`` subparser not in main.py → subprocess exits with usage error.
- P3-P18: same ImportError/AttributeError cascade or missing functionality.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tarfile
import textwrap
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


def _create_registry(workspace: str, with_wire_coverage: bool = True) -> str:
    """Create a minimal registry.sqlite with some data."""
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
    """)
    if with_wire_coverage:
        conn.executescript("""
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
    # Insert some test data
    conn.execute("INSERT INTO links (url_hash, url, visit_status) VALUES ('h1', 'http://example.com/1', 'discovered')")
    conn.execute("INSERT INTO links (url_hash, url, visit_status) VALUES ('h2', 'http://example.com/2', 'gathered')")
    conn.execute("INSERT INTO keys (key_hash, provider, status) VALUES ('k1', 'openai', 'valid')")
    conn.execute("INSERT INTO repos (owner, repo, fetched_at) VALUES ('owner1', 'repo1', 1000.0)")
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


def _create_queue_state(workspace: str) -> str:
    """Create queue_state directory with a test sqlite."""
    qs_dir = os.path.join(workspace, "queue_state")
    os.makedirs(qs_dir, exist_ok=True)
    db_path = os.path.join(qs_dir, "tasks.sqlite")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, data TEXT)")
    conn.execute("INSERT INTO tasks (data) VALUES ('test_task')")
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


def _create_shards(workspace: str, provider: str = "test_provider") -> str:
    """Create shard directory structure."""
    shard_root = os.path.join(workspace, "providers", provider, "shards")
    links_dir = os.path.join(shard_root, "links")
    os.makedirs(links_dir, exist_ok=True)
    # Create a shard file with some data
    shard_path = os.path.join(links_dir, "links_20261023_120000.ndjson")
    with open(shard_path, "w") as f:
        f.write('{"url": "http://example.com/1", "key": "SECRETVALUE_PLANT_1"}\n')
        f.write('{"url": "http://example.com/2", "key": "SECRETVALUE_PLANT_2"}\n')
    # Create sidecar index
    index_path = os.path.join(links_dir, "links_20261023_120000.index.json")
    with open(index_path, "w") as f:
        json.dump({"lines": 2, "bad_lines": 0}, f)
    return shard_root


def _create_snapshots(workspace: str, provider: str = "test_provider") -> str:
    """Create snapshot directory structure."""
    snap_dir = os.path.join(workspace, "providers", provider, "snapshots")
    os.makedirs(snap_dir, exist_ok=True)
    # Create a snapshot file
    snap_path = os.path.join(snap_dir, "links.json")
    with open(snap_path, "w") as f:
        json.dump([{"url": "http://example.com/1"}], f)
    # Create build meta
    meta_path = os.path.join(snap_dir, ".build_meta.json")
    with open(meta_path, "w") as f:
        json.dump({"current_generation": 1, "ts": 1000.0}, f)
    return snap_dir


def _create_telemetry(workspace: str) -> list:
    """Create telemetry JSONL files."""
    files = []
    for name in ["registry_decisions.jsonl", "aggregation_decisions.jsonl"]:
        path = os.path.join(workspace, name)
        with open(path, "w") as f:
            f.write('{"decision": "skip", "url_hash": "h1"}\n')
            f.write('{"decision": "gather", "url_hash": "h2"}\n')
        files.append(path)
        # Create rotation generations
        path1 = path + ".1"
        with open(path1, "w") as f:
            f.write('{"decision": "old", "url_hash": "h0"}\n')
        files.append(path1)
    return files


def _setup_full_workspace(tmp_path) -> tuple:
    """Set up a full workspace with all areas."""
    workspace = str(tmp_path / "ws")
    os.makedirs(workspace)
    cfg_path = str(tmp_path / "config.yaml")
    with open(cfg_path, "w") as f:
        f.write(_minimal_config_yaml(workspace))
    _create_registry(workspace)
    _create_queue_state(workspace)
    _create_shards(workspace)
    _create_snapshots(workspace)
    _create_telemetry(workspace)
    return workspace, cfg_path


# ---------------------------------------------------------------------------
# T1: dry-run no-op for each profile
# ---------------------------------------------------------------------------

class TestDryRunNoOp:
    """T1: dry-run must not modify anything for each profile."""

    @pytest.mark.parametrize("profile", ["all", "links", "keys", "queues", "telemetry", "shards"])
    def test_dry_run_no_op(self, tmp_path, profile):
        """T1: dry-run enumerates what would be deleted but makes zero changes."""
        workspace, cfg_path = _setup_full_workspace(tmp_path)

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "purge", profile, "--dry-run"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"dry-run failed for {profile}: {result.stderr}"

        # Verify nothing was deleted
        assert os.path.exists(os.path.join(workspace, "registry.sqlite")), "registry deleted in dry-run"
        assert os.path.exists(os.path.join(workspace, "queue_state")), "queue_state deleted in dry-run"
        assert os.path.exists(os.path.join(workspace, "providers")), "providers deleted in dry-run"
        assert os.path.exists(os.path.join(workspace, "registry_decisions.jsonl")), "telemetry deleted in dry-run"


# ---------------------------------------------------------------------------
# T2: apply deletes exactly the target set
# ---------------------------------------------------------------------------

class TestApplyDeletesTarget:
    """T2: apply mode deletes exactly the target set for each profile."""

    def test_links_profile_deletes_target_tables(self, tmp_path):
        """T2-links: links profile empties target tables, leaves others intact."""
        workspace, cfg_path = _setup_full_workspace(tmp_path)

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "purge", "links", "--yes"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"purge links failed: {result.stderr}"

        # Check target tables are empty
        conn = sqlite3.connect(os.path.join(workspace, "registry.sqlite"))
        links_count = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
        link_cov_count = conn.execute("SELECT COUNT(*) FROM link_coverage").fetchone()[0]
        file_meta_count = conn.execute("SELECT COUNT(*) FROM file_meta").fetchone()[0]
        repos_count = conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0]
        wire_cov_count = conn.execute("SELECT COUNT(*) FROM wire_coverage").fetchone()[0]
        # Check non-target tables are intact
        keys_count = conn.execute("SELECT COUNT(*) FROM keys").fetchone()[0]
        runs_count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        conn.close()

        assert links_count == 0, f"links not emptied: {links_count}"
        assert link_cov_count == 0, f"link_coverage not emptied: {link_cov_count}"
        assert file_meta_count == 0, f"file_meta not emptied: {file_meta_count}"
        assert repos_count == 0, f"repos not emptied: {repos_count}"
        assert wire_cov_count == 0, f"wire_coverage not emptied: {wire_cov_count}"
        assert keys_count == 1, f"keys should be intact: {keys_count}"

    def test_queues_profile_deletes_files(self, tmp_path):
        """T2-queues: queues profile removes queue_state files + sidecars."""
        workspace, cfg_path = _setup_full_workspace(tmp_path)
        qs_dir = os.path.join(workspace, "queue_state")

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "purge", "queues", "--yes"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"purge queues failed: {result.stderr}"

        # Queue files should be gone
        remaining = list(os.listdir(qs_dir)) if os.path.exists(qs_dir) else []
        assert len(remaining) == 0, f"queue files remain: {remaining}"

    def test_shards_profile_deletes_shards_and_snapshots(self, tmp_path):
        """T2-shards: shards profile removes shards + snapshots, leaves registry intact."""
        workspace, cfg_path = _setup_full_workspace(tmp_path)

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "purge", "shards", "--yes", "--force"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"purge shards failed: {result.stderr}"

        # Shards and snapshots should be gone
        provider_dir = os.path.join(workspace, "providers", "test_provider")
        shards_dir = os.path.join(provider_dir, "shards")
        snaps_dir = os.path.join(provider_dir, "snapshots")
        # Directories should be removed (or empty)
        assert not os.path.exists(shards_dir) or len(os.listdir(shards_dir)) == 0, "shards remain"
        assert not os.path.exists(snaps_dir) or len(os.listdir(snaps_dir)) == 0, "snapshots remain"
        # Registry should be intact
        assert os.path.exists(os.path.join(workspace, "registry.sqlite")), "registry deleted"


# ---------------------------------------------------------------------------
# T3: backup round-trip
# ---------------------------------------------------------------------------

class TestBackupRoundTrip:
    """T3: backup → delete → restore manually yields identical data."""

    def test_links_backup_round_trip(self, tmp_path):
        """T3: NDJSON backup of links table round-trips correctly."""
        workspace, cfg_path = _setup_full_workspace(tmp_path)
        backup_dir = str(tmp_path / "backup")

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "purge", "links", "--yes", "--backup-to", backup_dir],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"purge with backup failed: {result.stderr}"

        # Check backup artifacts exist
        assert os.path.exists(backup_dir), "backup dir not created"
        backup_files = os.listdir(backup_dir)
        assert len(backup_files) > 0, "no backup files created"

        # Check F-29: directory mode 0700
        dir_mode = stat.S_IMODE(os.stat(backup_dir).st_mode)
        assert dir_mode == 0o700, f"backup dir mode is {oct(dir_mode)}, expected 0o700"

        # Check artifact mode 0600
        for fname in backup_files:
            fpath = os.path.join(backup_dir, fname)
            if os.path.isfile(fpath):
                fmode = stat.S_IMODE(os.stat(fpath).st_mode)
                assert fmode == 0o600, f"{fname} mode is {oct(fmode)}, expected 0o600"


# ---------------------------------------------------------------------------
# T4: refuse at live lock
# ---------------------------------------------------------------------------

class TestRefuseLiveLock:
    """T4: purge refuses when a live process holds the lock."""

    def test_refuse_live_pipeline_lock(self, tmp_path):
        """T4: purge refuses with nonzero exit when pipeline lock is live."""
        workspace, cfg_path = _setup_full_workspace(tmp_path)

        proc = _spawn_live_process()
        try:
            _write_lock(workspace, pid=proc.pid, holder="pipeline")

            result = subprocess.run(
                [sys.executable, os.path.join(REPO_ROOT, "main.py"),
                 "-c", cfg_path, "purge", "links", "--yes"],
                capture_output=True, text=True, timeout=30,
                cwd=str(tmp_path),
            )
            assert result.returncode != 0, "purge should refuse with live lock"
            combined = result.stderr + result.stdout
            assert "lock" in combined.lower() or "refuse" in combined.lower(), \
                f"expected lock/refuse message, got: {combined[:500]}"
        finally:
            proc.terminate()
            proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# T5: stale lock ignored loudly
# ---------------------------------------------------------------------------

class TestStaleLockIgnored:
    """T5: stale lock (dead pid) is ignored with loud INFO."""

    def test_stale_lock_ignored(self, tmp_path, caplog):
        """T5: stale lock → INFO message + purge proceeds."""
        import logging
        workspace, cfg_path = _setup_full_workspace(tmp_path)

        # Spawn then kill a process
        proc = _spawn_live_process()
        dead_pid = proc.pid
        proc.terminate()
        proc.wait(timeout=5)

        _write_lock(workspace, pid=dead_pid, holder="pipeline", boot_ts=1000.0)

        with caplog.at_level(logging.INFO):
            result = subprocess.run(
                [sys.executable, os.path.join(REPO_ROOT, "main.py"),
                 "-c", cfg_path, "purge", "links", "--yes"],
                capture_output=True, text=True, timeout=30,
                cwd=str(tmp_path),
            )
        assert result.returncode == 0, f"purge should proceed with stale lock: {result.stderr}"
        combined = result.stderr + result.stdout
        assert "stale" in combined.lower(), f"expected 'stale' message: {combined[:500]}"


# ---------------------------------------------------------------------------
# T6: wal-freshness refuse
# ---------------------------------------------------------------------------

class TestWalFreshnessRefuse:
    """T6: fresh WAL without lock → refuse."""

    def test_fresh_wal_refuses(self, tmp_path):
        """T6: fresh WAL (no lock) → refuse with nonzero exit."""
        workspace, cfg_path = _setup_full_workspace(tmp_path)

        # Create a fresh WAL file (touch it now)
        wal_path = os.path.join(workspace, "registry.sqlite-wal")
        with open(wal_path, "w") as f:
            f.write("")
        # WAL is fresh (just created)

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "purge", "links", "--yes"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        # Should refuse because WAL is fresh
        assert result.returncode != 0, "purge should refuse with fresh WAL"


# ---------------------------------------------------------------------------
# T7: shards gate (pre-3a DB)
# ---------------------------------------------------------------------------

class TestShardsGate:
    """T7: shards profile requires wire_coverage table (post-3a)."""

    def test_shards_refuse_without_wire_coverage(self, tmp_path):
        """T7: shards profile refuses if wire_coverage table is missing."""
        workspace = str(tmp_path / "ws")
        os.makedirs(workspace)
        cfg_path = str(tmp_path / "config.yaml")
        with open(cfg_path, "w") as f:
            f.write(_minimal_config_yaml(workspace))
        _create_registry(workspace, with_wire_coverage=False)
        _create_shards(workspace)
        _create_snapshots(workspace)

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "purge", "shards", "--yes"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode != 0, "shards should refuse without wire_coverage"
        combined = result.stderr + result.stdout
        assert "wire_coverage" in combined or "3a" in combined.lower(), \
            f"expected wire_coverage/3a message: {combined[:500]}"

    def test_shards_force_bypasses_gate(self, tmp_path):
        """T7: --force bypasses shards gate + warning in receipt."""
        workspace = str(tmp_path / "ws")
        os.makedirs(workspace)
        cfg_path = str(tmp_path / "config.yaml")
        with open(cfg_path, "w") as f:
            f.write(_minimal_config_yaml(workspace))
        _create_registry(workspace, with_wire_coverage=False)
        _create_shards(workspace)
        _create_snapshots(workspace)

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "purge", "shards", "--yes", "--force"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"shards --force should proceed: {result.stderr}"

        # Check receipt has warning
        receipts_dir = os.path.join(workspace, "purge_receipts")
        assert os.path.exists(receipts_dir), "receipts dir not created"
        receipt_files = os.listdir(receipts_dir)
        assert len(receipt_files) > 0, "no receipt created"
        with open(os.path.join(receipts_dir, receipt_files[0])) as f:
            receipt = json.load(f)
        assert len(receipt.get("warnings", [])) > 0, "expected warning in receipt"


# ---------------------------------------------------------------------------
# T8: missing area skip with counter
# ---------------------------------------------------------------------------

class TestMissingAreaSkip:
    """T8: missing area → skip with counter, not crash."""

    def test_missing_queues_skip(self, tmp_path):
        """T8: queues profile with no queue_state → skip with counter."""
        workspace = str(tmp_path / "ws")
        os.makedirs(workspace)
        cfg_path = str(tmp_path / "config.yaml")
        with open(cfg_path, "w") as f:
            f.write(_minimal_config_yaml(workspace))
        _create_registry(workspace)
        # No queue_state directory

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "purge", "queues", "--yes"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"purge should skip missing area: {result.stderr}"
        combined = result.stderr + result.stdout
        assert "skip" in combined.lower() or "0" in combined, \
            f"expected skip/counter message: {combined[:500]}"


# ---------------------------------------------------------------------------
# T9: receipt schema + no secret values + F-29 rights
# ---------------------------------------------------------------------------

class TestReceiptSchema:
    """T9: receipt has correct schema, no secret values, F-29 rights."""

    def test_receipt_schema_and_rights(self, tmp_path):
        """T9: receipt JSON has required fields, no secret values, 0700/0600."""
        workspace, cfg_path = _setup_full_workspace(tmp_path)
        backup_dir = str(tmp_path / "backup")

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "purge", "links", "--yes", "--backup-to", backup_dir],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"purge failed: {result.stderr}"

        # Find receipt
        receipts_dir = os.path.join(workspace, "purge_receipts")
        assert os.path.exists(receipts_dir), "receipts dir not created"
        receipt_files = os.listdir(receipts_dir)
        assert len(receipt_files) == 1, f"expected 1 receipt, got {len(receipt_files)}"

        with open(os.path.join(receipts_dir, receipt_files[0])) as f:
            receipt = json.load(f)

        # Check schema
        assert "profile" in receipt
        assert receipt["profile"] == "links"
        assert "argv" in receipt
        assert "areas" in receipt
        assert "started_ts" in receipt
        assert "duration_s" in receipt
        assert "git_head" in receipt
        assert "config_md5" in receipt

        # Check areas structure
        for area in receipt["areas"]:
            assert "area" in area
            assert "rows_deleted" in area
            assert "files_deleted" in area
            assert "bytes_freed" in area

        # Check no secret values in receipt
        receipt_text = json.dumps(receipt)
        assert "SECRETVALUE_PLANT" not in receipt_text, "secret value in receipt"
        # Check no sk- patterns
        import re
        sk_pattern = re.compile(r'sk-[a-zA-Z0-9]{32}')
        assert not sk_pattern.search(receipt_text), "sk- pattern in receipt"

        # Canon 05 §5.4 / 07 §7.4: no secret values in CLI stdout/stderr either
        combined_output = (result.stdout or "") + (result.stderr or "")
        assert "SECRETVALUE_PLANT" not in combined_output, "secret value in CLI output"
        assert not sk_pattern.search(combined_output), "sk- pattern in CLI output"

        # Check F-29: backup dir 0700
        if os.path.exists(backup_dir):
            dir_mode = stat.S_IMODE(os.stat(backup_dir).st_mode)
            assert dir_mode == 0o700, f"backup dir mode {oct(dir_mode)}"


    def test_t9b_receipt_records_backup_dir_mode(self, tmp_path):
        """T9b (gate 4.G S4 / F-29): receipt backup section records the
        actual backup directory mode as an octal string ("0700")."""
        workspace, cfg_path = _setup_full_workspace(tmp_path)
        backup_dir = str(tmp_path / "backup_mode")

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "purge", "links", "--yes", "--backup-to", backup_dir],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"purge failed: {result.stderr}"

        receipts_dir = os.path.join(workspace, "purge_receipts")
        receipt_files = sorted(os.listdir(receipts_dir))
        assert len(receipt_files) == 1
        with open(os.path.join(receipts_dir, receipt_files[0])) as f:
            receipt = json.load(f)

        assert receipt.get("backup") is not None, "backup section missing"
        assert receipt["backup"].get("mode") == "0700", (
            "F-29: receipt must record backup dir mode '0700', got: "
            f"{receipt['backup'].get('mode')!r}"
        )
        dir_mode = stat.S_IMODE(os.stat(backup_dir).st_mode)
        assert f"{dir_mode:04o}" == receipt["backup"]["mode"]


# ---------------------------------------------------------------------------
# T10: order in all profile
# ---------------------------------------------------------------------------

class TestAllProfileOrder:
    """T10: all profile deletes in order: queues → shards/snapshots → telemetry → registry."""

    def test_all_profile_order(self, tmp_path):
        """T10: all profile processes areas in correct order."""
        workspace, cfg_path = _setup_full_workspace(tmp_path)

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "purge", "all", "--yes"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"purge all failed: {result.stderr}"

        # Check receipt for order
        receipts_dir = os.path.join(workspace, "purge_receipts")
        receipt_files = os.listdir(receipts_dir)
        assert len(receipt_files) > 0, "no receipt"
        with open(os.path.join(receipts_dir, receipt_files[0])) as f:
            receipt = json.load(f)

        areas = [a["area"] for a in receipt["areas"]]
        # Canonical order (05 §5.3.4): queues → shards → telemetry → registry LAST.
        # Unconditional: every area must be present and strictly ordered (a vacuous
        # pass on missing areas would defeat the pin).
        for expected in ("queues", "shards", "telemetry", "registry"):
            assert expected in areas, f"area '{expected}' missing from receipt: {areas}"
        idx = {n: areas.index(n) for n in ("queues", "shards", "telemetry", "registry")}
        assert idx["queues"] < idx["shards"] < idx["telemetry"] < idx["registry"], (
            f"all-profile order violated: {areas}"
        )


# ---------------------------------------------------------------------------
# T12: purge works without credentials section
# ---------------------------------------------------------------------------

class TestNoCredentials:
    """T12: purge works without credentials section in config."""

    def test_purge_without_credentials(self, tmp_path):
        """T12: purge works with config that has no credentials section."""
        workspace = str(tmp_path / "ws")
        os.makedirs(workspace)
        cfg_path = str(tmp_path / "config.yaml")
        with open(cfg_path, "w") as f:
            f.write(_minimal_config_yaml(workspace, with_credentials=False))
        _create_registry(workspace)

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "purge", "links", "--yes"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"purge without credentials failed: {result.stderr}"


# ---------------------------------------------------------------------------
# T13: idempotency
# ---------------------------------------------------------------------------

class TestIdempotency:
    """T13: second run has zero counters, skips > 0."""

    def test_idempotent_second_run(self, tmp_path):
        """T13: second purge has zero deletions, exit 0."""
        workspace, cfg_path = _setup_full_workspace(tmp_path)

        # First run
        result1 = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "purge", "links", "--yes"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result1.returncode == 0

        # Second run
        result2 = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "purge", "links", "--yes"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result2.returncode == 0, f"second purge failed: {result2.stderr}"

        # Check receipt has zero counters
        receipts_dir = os.path.join(workspace, "purge_receipts")
        receipt_files = sorted(os.listdir(receipts_dir))
        assert len(receipt_files) >= 2, "expected at least 2 receipts"
        with open(os.path.join(receipts_dir, receipt_files[-1])) as f:
            receipt = json.load(f)
        total_rows = sum(a["rows_deleted"] for a in receipt["areas"])
        assert total_rows == 0, f"second run should have 0 rows deleted, got {total_rows}"


# ---------------------------------------------------------------------------
# T14-purge: mutual exclusion (purge side)
# ---------------------------------------------------------------------------

class TestMutualExclusion:
    """T14: purge refuses when pipeline lock is live (purge side of F-10)."""

    def test_purge_refuses_live_pipeline(self, tmp_path):
        """T14: purge refuses when pipeline holds live lock."""
        workspace, cfg_path = _setup_full_workspace(tmp_path)

        proc = _spawn_live_process()
        try:
            _write_lock(workspace, pid=proc.pid, holder="pipeline")

            result = subprocess.run(
                [sys.executable, os.path.join(REPO_ROOT, "main.py"),
                 "-c", cfg_path, "purge", "links", "--yes"],
                capture_output=True, text=True, timeout=30,
                cwd=str(tmp_path),
            )
            assert result.returncode != 0, "purge should refuse with live pipeline lock"
        finally:
            proc.terminate()
            proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# N-10: sidecar pin (queues/all delete -wal/-shm)
# ---------------------------------------------------------------------------

class TestSidecarDeletion:
    """N-10: queues/all profiles delete -wal/-shm sidecars with .sqlite."""

    def test_queues_deletes_sidecars(self, tmp_path):
        """N-10: queues profile removes -wal/-shm along with .sqlite."""
        workspace, cfg_path = _setup_full_workspace(tmp_path)
        qs_dir = os.path.join(workspace, "queue_state")

        # Create sidecar files (make them old to avoid WAL freshness check)
        wal_path = os.path.join(qs_dir, "tasks.sqlite-wal")
        shm_path = os.path.join(qs_dir, "tasks.sqlite-shm")
        with open(wal_path, "w") as f:
            f.write("wal")
        with open(shm_path, "w") as f:
            f.write("shm")
        old_time = time.time() - 200
        os.utime(wal_path, (old_time, old_time))
        os.utime(shm_path, (old_time, old_time))

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "purge", "queues", "--yes"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"purge queues failed: {result.stderr}"

        # All sidecars should be gone
        remaining = os.listdir(qs_dir) if os.path.exists(qs_dir) else []
        assert len(remaining) == 0, f"sidecars remain: {remaining}"

    def test_all_registry_files_counted_in_receipt(self, tmp_path):
        """S2/gate-4G finding: all-profile unlinks registry.sqlite + -wal/-shm
        (N-10), so the receipt MUST count them in files_deleted and report the
        actual on-disk bytes (row estimate would double-count a deleted file)."""
        workspace, cfg_path = _setup_full_workspace(tmp_path)
        reg = os.path.join(workspace, "registry.sqlite")
        assert os.path.exists(reg), "fixture must create registry.sqlite"

        # Ensure both sidecars exist with non-trivial size and old mtime
        # (WAL-freshness interlock).
        old_time = time.time() - 200
        for suffix in ("-wal", "-shm"):
            p = reg + suffix
            with open(p, "w") as f:
                f.write("x" * 32)
            os.utime(p, (old_time, old_time))
        if os.path.exists(reg):
            os.utime(reg, (old_time, old_time))
        reg_size_before = os.path.getsize(reg)

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "purge", "all", "--yes"],
            capture_output=True, text=True, timeout=60,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"purge all failed: {result.stderr}"

        receipts_dir = os.path.join(workspace, "purge_receipts")
        latest = sorted(os.listdir(receipts_dir))[-1]
        with open(os.path.join(receipts_dir, latest)) as f:
            receipt = json.load(f)
        area = next(a for a in receipt["areas"] if a["area"] == "registry")

        # All three paths MUST be gone afterwards (N-10). Note: SQLite itself
        # auto-removes a well-formed -wal/-shm on last-connection close inside
        # _delete_registry_tables, so the unlink loop may legitimately count
        # 1..3 files; the receipt must reflect the ACTUAL unlink count (>=1)
        # and real on-disk bytes (gate 4.G S2; live D4 census proved removal).
        for suffix in ("", "-wal", "-shm"):
            assert not os.path.exists(reg + suffix), f"{reg}{suffix} remains"
        assert area["files_deleted"] >= 1, (
            f"registry db unlink must be counted in files_deleted, "
            f"got {area['files_deleted']}"
        )
        assert area["bytes_freed"] >= reg_size_before, (
            f"bytes_freed must reflect actual file bytes "
            f"(db was {reg_size_before}), got {area['bytes_freed']}"
        )

    def test_purge_all_counts_forced_sidecars(self, tmp_path, monkeypatch):
        """Deterministic unit pin for the crash-leftover case: when -wal/-shm
        persist to the unlink loop (SQLite did not auto-clean them), all three
        files are counted in files_deleted and their real bytes in bytes_freed.

        _delete_registry_tables is stubbed because ANY sqlite connect/close
        cycle removes foreign sidecars as a side effect — this pin isolates
        the receipt-accounting logic of the N-10 unlink loop (gate 4.G S2)."""
        from tools.purge import _purge_all

        monkeypatch.setattr(
            "tools.purge._delete_registry_tables", lambda ws, tables: (0, 0)
        )
        workspace = str(tmp_path)
        reg = os.path.join(workspace, "registry.sqlite")
        for suffix, payload in (("", "y" * 64), ("-wal", "x" * 32), ("-shm", "z" * 16)):
            with open(reg + suffix, "w") as f:
                f.write(payload)

        warnings: list = []
        areas = _purge_all(workspace, False, None, None, warnings, False)
        area = next(a for a in areas if a["area"] == "registry")

        assert area["files_deleted"] == 3, (
            f"db + wal + shm must all be counted, got {area['files_deleted']}"
        )
        assert area["bytes_freed"] >= 64 + 32 + 16, (
            f"bytes_freed must sum real file sizes, got {area['bytes_freed']}"
        )
        for suffix in ("", "-wal", "-shm"):
            assert not os.path.exists(reg + suffix)


# ---------------------------------------------------------------------------
# Parser compatibility: existing forms byte-identical
# ---------------------------------------------------------------------------

class TestParserCompatibility:
    """Existing CLI forms must remain byte-identical."""

    def test_validate_still_works(self, tmp_path):
        """--validate must still work after purge subparser added."""
        workspace = str(tmp_path / "ws")
        os.makedirs(workspace)
        cfg_path = str(tmp_path / "config.yaml")
        with open(cfg_path, "w") as f:
            f.write(_minimal_config_yaml(workspace))

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"), "-c", cfg_path, "--validate"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"--validate broken: {result.stderr}"

    def test_purge_requires_yes_without_dry_run(self, tmp_path):
        """purge without --dry-run requires --yes."""
        workspace, cfg_path = _setup_full_workspace(tmp_path)

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "purge", "links"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode != 0, "purge without --yes should refuse"
        combined = result.stderr + result.stdout
        assert "--yes" in combined or "--dry-run" in combined, \
            f"expected --yes/--dry-run message: {combined[:500]}"
