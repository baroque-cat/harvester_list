"""Maintain CLI tests (slice 4.4, wave 4).

Traceability: 06_disk_budget.md §6.5 (D-06.2 expanded F-04), OPEN-8/12.

Design:
- ``tools/maintain.py`` implements the vacuum logic.
- ``main.py`` adds a ``maintain`` subparser with dest="command".
- Targets: registry.sqlite + ALL queue_state/*.sqlite.
- Interlocks: lock (symmetry, holder="maintain"), wal-freshness, workspace existence.
- Non-destructive: VACUUM does not delete data; integrity pin mandatory.
- N-10 sidecars: -wal/-shm left alive after VACUUM (normal for WAL-mode DB).
- Summary masked via redact_api_keys_in_text (structural pin).

RED predictions (all pins must FAIL before product code):
- P1: ``tools.maintain`` module does not exist → ImportError.
- P2: ``maintain`` subparser not in main.py → subprocess exits with usage error.
- P3-P10: same ImportError/AttributeError cascade or missing functionality.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
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


def _create_registry(workspace: str) -> str:
    """Create a minimal registry.sqlite with schema and some data."""
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
    # Set user_version to match SCHEMA_VERSION (8)
    conn.execute("PRAGMA user_version = 8")
    # Insert test data
    conn.execute("INSERT INTO links (url_hash, url, visit_status) VALUES ('h1', 'http://example.com/1', 'discovered')")
    conn.execute("INSERT INTO links (url_hash, url, visit_status) VALUES ('h2', 'http://example.com/2', 'gathered')")
    conn.execute("INSERT INTO keys (key_hash, provider, status) VALUES ('k1', 'openai', 'valid')")
    conn.commit()
    conn.close()

    # Make WAL files old (> 120s) to avoid triggering WAL freshness check
    _age_wal_files(db_path)
    return db_path


def _inflate_and_delete(db_path: str, n_rows: int = 500) -> int:
    """Insert N rows then DELETE them to inflate freelist/page_count.

    Returns the file size after deletion (before VACUUM).
    """
    conn = sqlite3.connect(db_path)
    # Insert many rows with payload to inflate the DB
    for i in range(n_rows):
        conn.execute(
            "INSERT INTO links (url_hash, url, visit_status) VALUES (?, ?, 'discovered')",
            (f"inflated_{i}", f"http://example.com/inflated/{i}" + "x" * 200),
        )
    conn.commit()
    # Delete all inflated rows (freelist grows, file stays large)
    conn.execute("DELETE FROM links WHERE url_hash LIKE 'inflated_%'")
    conn.commit()
    conn.close()
    # Age WAL files
    _age_wal_files(db_path)
    return os.path.getsize(db_path)


def _age_wal_files(db_path: str) -> None:
    """Make WAL/SHM files old (> 120s) to avoid WAL freshness check."""
    for suffix in ("-wal", "-shm"):
        p = db_path + suffix
        if os.path.exists(p):
            old_time = time.time() - 200
            os.utime(p, (old_time, old_time))


def _create_queue_state(workspace: str, n_queues: int = 3) -> list:
    """Create queue_state directory with multiple test sqlite databases."""
    qs_dir = os.path.join(workspace, "queue_state")
    os.makedirs(qs_dir, exist_ok=True)
    db_paths = []
    for i in range(n_queues):
        db_path = os.path.join(qs_dir, f"queue_{i}.sqlite")
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, data TEXT)")
        conn.execute("INSERT INTO tasks (data) VALUES ('test_task')")
        conn.commit()
        conn.close()
        _age_wal_files(db_path)
        db_paths.append(db_path)
    return db_paths


def _setup_workspace(tmp_path, with_queues: bool = True, n_queues: int = 3) -> tuple:
    """Set up a workspace with registry and optionally queues."""
    workspace = str(tmp_path / "ws")
    os.makedirs(workspace)
    cfg_path = str(tmp_path / "config.yaml")
    with open(cfg_path, "w") as f:
        f.write(_minimal_config_yaml(workspace))
    _create_registry(workspace)
    if with_queues:
        _create_queue_state(workspace, n_queues=n_queues)
    return workspace, cfg_path


# ---------------------------------------------------------------------------
# M1: functional — bloated registry, vacuum reclaims space
# ---------------------------------------------------------------------------

class TestFunctionalVacuum:
    """M1: vacuum reduces file size after inflate+delete; data integrity preserved."""

    def test_vacuum_reclaims_space_and_preserves_data(self, tmp_path):
        """M1: bloated registry ⇒ vacuum shrinks file; other tables intact; user_version preserved."""
        workspace, cfg_path = _setup_workspace(tmp_path, with_queues=False)
        db_path = os.path.join(workspace, "registry.sqlite")

        # Inflate and delete to grow freelist
        size_before_vacuum = _inflate_and_delete(db_path, n_rows=500)

        # Count rows in non-target tables before vacuum
        conn = sqlite3.connect(db_path)
        keys_before = conn.execute("SELECT COUNT(*) FROM keys").fetchone()[0]
        user_version_before = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "maintain", "--vacuum"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"vacuum failed: {result.stderr}"

        # File should be smaller after VACUUM
        size_after_vacuum = os.path.getsize(db_path)
        assert size_after_vacuum < size_before_vacuum, (
            f"VACUUM did not shrink file: before={size_before_vacuum}, after={size_after_vacuum}"
        )

        # Data integrity: keys table intact
        conn = sqlite3.connect(db_path)
        keys_after = conn.execute("SELECT COUNT(*) FROM keys").fetchone()[0]
        user_version_after = conn.execute("PRAGMA user_version").fetchone()[0]
        links_after = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
        conn.close()

        assert keys_after == keys_before, f"keys count changed: {keys_before} → {keys_after}"
        assert user_version_after == user_version_before == 8, (
            f"user_version changed: {user_version_before} → {user_version_after}"
        )
        # Original 2 links should still exist (inflated ones were deleted)
        assert links_after == 2, f"links count unexpected: {links_after}"

        # Verify output mentions reclaimed bytes
        combined = result.stdout + result.stderr
        assert "reclaimed" in combined.lower() or "registry" in combined.lower(), (
            f"expected vacuum summary: {combined[:500]}"
        )


# ---------------------------------------------------------------------------
# M2: queue_state coverage — all queues vacuumed
# ---------------------------------------------------------------------------

class TestQueueStateCoverage:
    """M2: all queue_state/*.sqlite are vacuumed, none skipped."""

    def test_all_queues_vacuumed(self, tmp_path):
        """M2: multiple queue DBs ⇒ all vacuumed (per-DB log lines)."""
        workspace, cfg_path = _setup_workspace(tmp_path, with_queues=True, n_queues=3)

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "maintain", "--vacuum"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"vacuum failed: {result.stderr}"

        combined = result.stdout + result.stderr
        # All 3 queue DBs + registry should appear in output
        for i in range(3):
            assert f"queue_{i}.sqlite" in combined, (
                f"queue_{i}.sqlite not mentioned in output: {combined[:500]}"
            )
        assert "registry.sqlite" in combined, (
            f"registry.sqlite not mentioned in output: {combined[:500]}"
        )


# ---------------------------------------------------------------------------
# M3: live lock ⇒ refuse
# ---------------------------------------------------------------------------

class TestRefuseLiveLock:
    """M3: live lock holder ⇒ rc≠0 + pid/holder in output."""

    def test_refuse_live_pipeline_lock(self, tmp_path):
        """M3: maintain refuses with nonzero exit when pipeline lock is live."""
        workspace, cfg_path = _setup_workspace(tmp_path, with_queues=False)

        proc = _spawn_live_process()
        try:
            _write_lock(workspace, pid=proc.pid, holder="pipeline")

            result = subprocess.run(
                [sys.executable, os.path.join(REPO_ROOT, "main.py"),
                 "-c", cfg_path, "maintain", "--vacuum"],
                capture_output=True, text=True, timeout=30,
                cwd=str(tmp_path),
            )
            assert result.returncode != 0, "maintain should refuse with live lock"
            combined = result.stderr + result.stdout
            assert "lock" in combined.lower() or "refuse" in combined.lower(), (
                f"expected lock/refuse message: {combined[:500]}"
            )
            # pid and holder should appear
            assert str(proc.pid) in combined, (
                f"expected pid in output: {combined[:500]}"
            )
            assert "pipeline" in combined, (
                f"expected holder in output: {combined[:500]}"
            )
        finally:
            proc.terminate()
            proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# M4: stale lock ⇒ loud INFO + success
# ---------------------------------------------------------------------------

class TestStaleLockIgnored:
    """M4: stale lock (dead pid) ⇒ loud INFO + vacuum proceeds."""

    def test_stale_lock_ignored(self, tmp_path):
        """M4: stale lock → INFO message + vacuum proceeds."""
        workspace, cfg_path = _setup_workspace(tmp_path, with_queues=False)

        # Spawn then kill a process
        proc = _spawn_live_process()
        dead_pid = proc.pid
        proc.terminate()
        proc.wait(timeout=5)

        _write_lock(workspace, pid=dead_pid, holder="pipeline", boot_ts=1000.0)

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "maintain", "--vacuum"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"maintain should proceed with stale lock: {result.stderr}"
        combined = result.stderr + result.stdout
        assert "stale" in combined.lower(), f"expected 'stale' message: {combined[:500]}"


# ---------------------------------------------------------------------------
# M5: WAL-freshness refuse
# ---------------------------------------------------------------------------

class TestWalFreshnessRefuse:
    """M5: fresh WAL (mtime now) ⇒ REFUSE rc≠0."""

    def test_fresh_wal_refuses(self, tmp_path):
        """M5: fresh WAL (no lock) → refuse with nonzero exit."""
        workspace, cfg_path = _setup_workspace(tmp_path, with_queues=False)

        # Create a fresh WAL file (touch it now)
        wal_path = os.path.join(workspace, "registry.sqlite-wal")
        with open(wal_path, "w") as f:
            f.write("")
        # WAL is fresh (just created)

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "maintain", "--vacuum"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode != 0, "maintain should refuse with fresh WAL"


# ---------------------------------------------------------------------------
# M6: parser compatibility
# ---------------------------------------------------------------------------

class TestParserCompatibility:
    """M6: --validate still works; maintain without --vacuum ⇒ rc=1."""

    def test_validate_still_works(self, tmp_path):
        """--validate must still work after maintain subparser added."""
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

    def test_maintain_requires_vacuum(self, tmp_path):
        """maintain without --vacuum ⇒ rc=1 + error message."""
        workspace = str(tmp_path / "ws")
        os.makedirs(workspace)
        cfg_path = str(tmp_path / "config.yaml")
        with open(cfg_path, "w") as f:
            f.write(_minimal_config_yaml(workspace))

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "maintain"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode != 0, "maintain without --vacuum should refuse"
        combined = result.stderr + result.stdout
        assert "--vacuum" in combined, (
            f"expected --vacuum message: {combined[:500]}"
        )


# ---------------------------------------------------------------------------
# M7: works without credentials section
# ---------------------------------------------------------------------------

class TestNoCredentials:
    """M7: maintain works without credentials section in config (YAML-only)."""

    def test_maintain_without_credentials(self, tmp_path):
        """M7: maintain works with config that has no credentials section."""
        workspace = str(tmp_path / "ws")
        os.makedirs(workspace)
        cfg_path = str(tmp_path / "config.yaml")
        with open(cfg_path, "w") as f:
            f.write(_minimal_config_yaml(workspace, with_credentials=False))
        _create_registry(workspace)

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "maintain", "--vacuum"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"maintain without credentials failed: {result.stderr}"


# ---------------------------------------------------------------------------
# M8: missing targets ⇒ skip counter or REFUSE
# ---------------------------------------------------------------------------

class TestMissingTargets:
    """M8: missing targets ⇒ skip counter; empty workspace ⇒ REFUSE."""

    def test_missing_registry_skip_with_queues(self, tmp_path):
        """M8a: no registry.sqlite but queues exist ⇒ skip registry, vacuum queues, rc=0."""
        workspace = str(tmp_path / "ws")
        os.makedirs(workspace)
        cfg_path = str(tmp_path / "config.yaml")
        with open(cfg_path, "w") as f:
            f.write(_minimal_config_yaml(workspace))
        # Only queues, no registry
        _create_queue_state(workspace, n_queues=2)

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "maintain", "--vacuum"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"maintain should skip missing registry: {result.stderr}"
        combined = result.stderr + result.stdout
        assert "skip" in combined.lower() or "not found" in combined.lower(), (
            f"expected skip message: {combined[:500]}"
        )

    def test_empty_workspace_refuses(self, tmp_path):
        """M8b: no targets at all ⇒ REFUSE rc≠0."""
        workspace = str(tmp_path / "ws")
        os.makedirs(workspace)
        cfg_path = str(tmp_path / "config.yaml")
        with open(cfg_path, "w") as f:
            f.write(_minimal_config_yaml(workspace))
        # No registry, no queue_state

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "maintain", "--vacuum"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode != 0, "maintain should refuse with no targets"
        combined = result.stderr + result.stdout
        assert "no vacuum targets" in combined.lower() or "refuse" in combined.lower(), (
            f"expected refuse message: {combined[:500]}"
        )


# ---------------------------------------------------------------------------
# M9: masking insurance — summary contains only names/numbers
# ---------------------------------------------------------------------------

class TestMaskingInsurance:
    """M9: summary does not contain secret values (structural pin)."""

    def test_no_secret_in_output(self, tmp_path):
        """M9: vacuum output contains only names and numbers, no row content."""
        workspace, cfg_path = _setup_workspace(tmp_path, with_queues=False)

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "maintain", "--vacuum"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"vacuum failed: {result.stderr}"

        combined = result.stdout + result.stderr
        # Structural pin: output should not contain URL data or row content
        assert "http://example.com" not in combined, (
            f"vacuum output should not contain row data: {combined[:500]}"
        )
        # No sk- patterns
        import re
        sk_pattern = re.compile(r'sk-[a-zA-Z0-9]{20,}')
        assert not sk_pattern.search(combined), "sk- pattern in vacuum output"


# ---------------------------------------------------------------------------
# M10: dry-run lists targets with sizes, rc=0
# ---------------------------------------------------------------------------

class TestDryRun:
    """M10: --dry-run enumerates targets with sizes, makes no changes."""

    def test_dry_run_lists_targets(self, tmp_path):
        """M10: dry-run lists targets with current sizes, rc=0, no changes."""
        workspace, cfg_path = _setup_workspace(tmp_path, with_queues=True, n_queues=2)
        db_path = os.path.join(workspace, "registry.sqlite")
        size_before = os.path.getsize(db_path)

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "maintain", "--vacuum", "--dry-run"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"dry-run failed: {result.stderr}"

        combined = result.stdout + result.stderr
        # Should mention targets
        assert "registry.sqlite" in combined, f"registry not in dry-run output: {combined[:500]}"
        assert "queue_0.sqlite" in combined, f"queue_0 not in dry-run output: {combined[:500]}"
        assert "queue_1.sqlite" in combined, f"queue_1 not in dry-run output: {combined[:500]}"
        # Should mention dry_run=True
        assert "dry_run=True" in combined or "dry-run" in combined.lower() or "dry_run" in combined, (
            f"dry-run flag not in output: {combined[:500]}"
        )
        # Size should match actual file size
        assert str(size_before) in combined, (
            f"actual size {size_before} not in output: {combined[:500]}"
        )

        # File sizes should be unchanged after dry-run
        size_after = os.path.getsize(db_path)
        assert size_after == size_before, "dry-run changed file size"


# ---------------------------------------------------------------------------
# N-10 sidecars: -wal/-shm left alive after VACUUM
# ---------------------------------------------------------------------------

class TestSidecarsPreserved:
    """N-10: VACUUM+checkpoint(TRUNCATE) leaves -wal/-shm alive (normal for WAL-mode)."""

    def test_sidecars_not_deleted(self, tmp_path):
        """N-10: after vacuum, -wal/-shm may exist but DB is valid."""
        workspace, cfg_path = _setup_workspace(tmp_path, with_queues=False)
        db_path = os.path.join(workspace, "registry.sqlite")

        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "main.py"),
             "-c", cfg_path, "maintain", "--vacuum"],
            capture_output=True, text=True, timeout=30,
            cwd=str(tmp_path),
        )
        assert result.returncode == 0, f"vacuum failed: {result.stderr}"

        # Verify DB is valid: can be opened, tables exist, rows intact
        conn = sqlite3.connect(db_path)
        links_count = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
        keys_count = conn.execute("SELECT COUNT(*) FROM keys").fetchone()[0]
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()

        assert links_count == 2, f"links count unexpected after vacuum: {links_count}"
        assert keys_count == 1, f"keys count unexpected after vacuum: {keys_count}"
        assert user_version == 8, f"user_version unexpected after vacuum: {user_version}"
