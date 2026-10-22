"""Run-lock file tests (slice 4.1, wave 4).

Traceability: 05_purge_cli.md §5.3, 00_INDEX.md line 99 (wave 4),
T14 (mutual exclusion both directions — pipeline half only; purge half lands 4.2).

Design:
- ``tools/run_lock.py`` manages ``<workspace>/.harvester.lock`` (JSON with
  pid/boot_ts/holder, atomic O_EXCL creation, mode 0600).
- ``acquire_lock`` raises ``LockHeldError`` when a live process holds the lock.
- Stale lock (dead pid) → loud INFO + atomic takeover.
- ``release_lock`` removes only own lock (pid match); idempotent.
- ``main.py`` refuses to start when a live lock exists (rc≠0).
- Pipeline acquires lock on ``_on_start`` and releases on ``_on_stop``.

RED predictions (all pins must FAIL before product code):
- P1: ``tools.run_lock`` module does not exist → ImportError.
- P2: ``LockHeldError`` not importable from ``core.exceptions`` → ImportError.
- P3-P7: same ImportError cascade.
- P8: ``main.py`` does not check lock → subprocess exits 0 (refuse not triggered).
- P9: trivially passes (--validate doesn't touch lock path) — insurance pin.
- P10: Pipeline._on_start does not create lock file → assert fails.
- P11: Pipeline start does not raise on live lock → assert fails.
- P12: stale lock not taken over → assert fails.
"""

from __future__ import annotations

import json
import os
import stat
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
    # Wait for it to actually start
    time.sleep(0.3)
    assert proc.poll() is None, "spawned process died immediately"
    return proc


def _minimal_config_yaml(workspace: str) -> str:
    """Generate a minimal valid config YAML pointing workspace at tmp dir."""
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


# ---------------------------------------------------------------------------
# Unit pins — tools/run_lock.py helper
# ---------------------------------------------------------------------------

class TestRunLockHelper:
    """P1-P7: unit tests for the lock helper."""

    def test_p1_acquire_creates_file_with_correct_fields_and_mode(self, tmp_path):
        """P1: acquire_lock creates .harvester.lock with pid/boot_ts/holder and mode 0600."""
        from tools.run_lock import acquire_lock, read_lock

        workspace = str(tmp_path / "ws")
        os.makedirs(workspace)

        acquire_lock(workspace, holder="pipeline")

        path = _lock_path(workspace)
        assert os.path.exists(path), "lock file was not created"

        # Check file mode
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600, f"expected mode 0o600, got {oct(mode)}"

        # Check content
        info = read_lock(workspace)
        assert info is not None
        assert info["pid"] == os.getpid()
        assert info["holder"] == "pipeline"
        assert "boot_ts" in info
        assert isinstance(info["boot_ts"], float)

        # Cleanup
        from tools.run_lock import release_lock
        release_lock(workspace)

    def test_p2_acquire_raises_on_live_other_lock(self, tmp_path):
        """P2: acquire_lock raises LockHeldError when a live process holds the lock."""
        from core.exceptions import LockHeldError
        from tools.run_lock import acquire_lock, release_lock

        workspace = str(tmp_path / "ws")
        os.makedirs(workspace)

        proc = _spawn_live_process()
        try:
            # Write a lock pretending to be held by the spawned process
            _write_lock(workspace, pid=proc.pid, holder="purge")

            with pytest.raises(LockHeldError) as exc_info:
                acquire_lock(workspace, holder="pipeline")

            assert str(proc.pid) in str(exc_info.value) or "purge" in str(exc_info.value)
        finally:
            proc.terminate()
            proc.wait(timeout=5)
            # Cleanup
            path = _lock_path(workspace)
            if os.path.exists(path):
                os.unlink(path)

    def test_p3_stale_lock_taken_over_loudly(self, tmp_path, caplog):
        """P3: stale lock (dead pid) → INFO 'stale lock ignored' + file replaced."""
        import logging
        from tools.run_lock import acquire_lock, read_lock, release_lock

        workspace = str(tmp_path / "ws")
        os.makedirs(workspace)

        # Spawn then kill a process to get a dead pid
        proc = _spawn_live_process()
        dead_pid = proc.pid
        proc.terminate()
        proc.wait(timeout=5)

        # Write lock with the now-dead pid
        _write_lock(workspace, pid=dead_pid, holder="purge", boot_ts=1000.0)

        with caplog.at_level(logging.INFO):
            acquire_lock(workspace, holder="pipeline")

        # Check stale message was logged
        stale_messages = [r.message for r in caplog.records if "stale lock" in r.message.lower()]
        assert len(stale_messages) >= 1, f"expected 'stale lock' INFO, got: {[r.message for r in caplog.records]}"

        # Check file was replaced with our pid
        info = read_lock(workspace)
        assert info is not None
        assert info["pid"] == os.getpid()
        assert info["holder"] == "pipeline"

        release_lock(workspace)

    def test_p4_release_removes_own_lock(self, tmp_path):
        """P4: release_lock removes the lock file when pid matches."""
        from tools.run_lock import acquire_lock, release_lock

        workspace = str(tmp_path / "ws")
        os.makedirs(workspace)

        acquire_lock(workspace, holder="pipeline")
        assert os.path.exists(_lock_path(workspace))

        result = release_lock(workspace)
        assert result is True
        assert not os.path.exists(_lock_path(workspace))

    def test_p5_release_idempotent_no_file(self, tmp_path):
        """P5: release_lock with no lock file returns False, no exception."""
        from tools.run_lock import release_lock

        workspace = str(tmp_path / "ws")
        os.makedirs(workspace)

        result = release_lock(workspace)
        assert result is False

    def test_p6_release_does_not_remove_other_lock(self, tmp_path):
        """P6: release_lock does NOT remove a lock held by another pid."""
        from tools.run_lock import release_lock

        workspace = str(tmp_path / "ws")
        os.makedirs(workspace)

        proc = _spawn_live_process()
        try:
            _write_lock(workspace, pid=proc.pid, holder="purge")

            result = release_lock(workspace)
            assert result is False
            # File must still exist
            assert os.path.exists(_lock_path(workspace))
        finally:
            proc.terminate()
            proc.wait(timeout=5)
            path = _lock_path(workspace)
            if os.path.exists(path):
                os.unlink(path)

    def test_p7_read_lock_corrupt_json_returns_none(self, tmp_path):
        """P7: read_lock with corrupt JSON returns None (fail-CLOSED: not counted as live)."""
        from tools.run_lock import read_lock

        workspace = str(tmp_path / "ws")
        os.makedirs(workspace)

        path = _lock_path(workspace)
        with open(path, "w") as f:
            f.write("NOT VALID JSON {{{")

        result = read_lock(workspace)
        assert result is None


# ---------------------------------------------------------------------------
# Integration pins — main.py refuse + Pipeline start/stop
# ---------------------------------------------------------------------------

class TestMainRefuse:
    """P8-P9: main.py startup refuse and --validate exemption."""

    def test_p8_main_refuses_with_live_lock(self, tmp_path):
        """P8: main.py with live lock → rc≠0 + stderr/log mentions pid/holder."""
        workspace = str(tmp_path / "ws")
        os.makedirs(workspace)

        # Write a minimal config
        cfg_path = str(tmp_path / "test_config.yaml")
        with open(cfg_path, "w") as f:
            f.write(_minimal_config_yaml(workspace))

        # Spawn a live process and write its lock
        proc = _spawn_live_process()
        try:
            _write_lock(workspace, pid=proc.pid, holder="purge")

            result = subprocess.run(
                [sys.executable, os.path.join(REPO_ROOT, "main.py"), "-c", cfg_path],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(tmp_path),
            )

            assert result.returncode != 0, (
                f"expected nonzero exit, got {result.returncode}; "
                f"stderr={result.stderr[:500]}"
            )
            combined = result.stderr + result.stdout
            assert str(proc.pid) in combined or "purge" in combined, (
                f"expected pid/holder in output; stderr={result.stderr[:500]}"
            )
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_p9_validate_passes_with_live_lock(self, tmp_path):
        """P9: --validate must NOT refuse even with a live lock."""
        workspace = str(tmp_path / "ws")
        os.makedirs(workspace)

        cfg_path = str(tmp_path / "test_config.yaml")
        with open(cfg_path, "w") as f:
            f.write(_minimal_config_yaml(workspace))

        proc = _spawn_live_process()
        try:
            _write_lock(workspace, pid=proc.pid, holder="purge")

            result = subprocess.run(
                [sys.executable, os.path.join(REPO_ROOT, "main.py"), "-c", cfg_path, "--validate"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(tmp_path),
            )

            assert result.returncode == 0, (
                f"--validate should pass with live lock; rc={result.returncode}; "
                f"stderr={result.stderr[:500]}"
            )
        finally:
            proc.terminate()
            proc.wait(timeout=5)


class TestPipelineLock:
    """P10-P12: Pipeline acquires/releases lock on start/stop."""

    def test_p10_pipeline_start_creates_lock_stop_removes(self, tmp_path):
        """P10: Pipeline start creates lock; stop removes it."""
        from config.schemas import Config
        from manager.pipeline import Pipeline

        workspace = str(tmp_path / "ws")
        os.makedirs(workspace)

        cfg = Config()
        cfg.global_config.workspace = workspace
        cfg.persistence.simple = True

        pipeline = Pipeline(cfg, providers={})
        try:
            pipeline.start()

            path = _lock_path(workspace)
            assert os.path.exists(path), "lock file not created after pipeline.start()"

            with open(path) as f:
                info = json.load(f)
            assert info["pid"] == os.getpid()
            assert info["holder"] == "pipeline"
        finally:
            pipeline.stop()

        # After stop, lock should be gone
        assert not os.path.exists(path), "lock file still present after pipeline.stop()"

    def test_p11_pipeline_start_refuses_with_live_lock(self, tmp_path):
        """P11: Pipeline start raises LockHeldError when a live lock exists."""
        from core.exceptions import LockHeldError
        from config.schemas import Config
        from manager.pipeline import Pipeline

        workspace = str(tmp_path / "ws")
        os.makedirs(workspace)

        proc = _spawn_live_process()
        try:
            _write_lock(workspace, pid=proc.pid, holder="purge")

            cfg = Config()
            cfg.global_config.workspace = workspace
            cfg.persistence.simple = True

            pipeline = Pipeline(cfg, providers={})
            with pytest.raises(LockHeldError):
                pipeline.start()
        finally:
            proc.terminate()
            proc.wait(timeout=5)
            path = _lock_path(workspace)
            if os.path.exists(path):
                os.unlink(path)

    def test_p12_crash_recovery_stale_takeover(self, tmp_path):
        """P12: lock left after crash → next acquire does stale takeover."""
        from config.schemas import Config
        from manager.pipeline import Pipeline

        workspace = str(tmp_path / "ws")
        os.makedirs(workspace)

        # Simulate a crash: write a lock with a dead pid
        proc = _spawn_live_process()
        dead_pid = proc.pid
        proc.terminate()
        proc.wait(timeout=5)

        _write_lock(workspace, pid=dead_pid, holder="pipeline", boot_ts=1000.0)

        # Now start a new pipeline — should take over the stale lock
        cfg = Config()
        cfg.global_config.workspace = workspace
        cfg.persistence.simple = True

        pipeline = Pipeline(cfg, providers={})
        try:
            pipeline.start()

            path = _lock_path(workspace)
            assert os.path.exists(path)

            with open(path) as f:
                info = json.load(f)
            assert info["pid"] == os.getpid()
            assert info["holder"] == "pipeline"
        finally:
            pipeline.stop()

        assert not os.path.exists(path)


class TestCorruptLockRefusal:
    """P13: corrupt/unreadable lock file ⇒ typed fail-CLOSED refusal."""

    def test_p13_corrupt_lock_acquire_raises_typed(self, tmp_path):
        """P13: acquire_lock on a corrupt (non-JSON) lock file raises
        LockHeldError — never a bare FileExistsError (05 §5.3.3: ambiguity
        refuses BEFORE any deletion; fail-CLOSED doctrine for purge)."""
        from core.exceptions import LockHeldError
        from tools.run_lock import acquire_lock

        workspace = str(tmp_path / "ws")
        os.makedirs(workspace)
        path = _lock_path(workspace)
        with open(path, "w") as f:
            f.write("NOT VALID JSON {{{")

        with pytest.raises(LockHeldError) as exc_info:
            acquire_lock(workspace, holder="pipeline")
        assert "corrupt" in str(exc_info.value).lower() or "unreadable" in str(exc_info.value).lower(), (
            f"refusal message must name the corrupt-lock condition, got: {exc_info.value}"
        )
        # Fail-CLOSED: the ambiguous file must NOT have been deleted.
        assert os.path.exists(path), "corrupt lock must not be silently removed"
