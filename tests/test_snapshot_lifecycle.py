"""Snapshot lifecycle pins (C1–C9) — wave 1 slice 1.4.

Design: 04_snapshot_lifecycle.md.  These pins verify the configurable snapshot
lifecycle: master flag, interval, rotation, incremental build, size-guard,
loader parsing, validator rejection, compact JSON, and simple-mode semantics.

Expected RED at creation (before fix):
- C1: final build happens unconditionally even when snapshots_enabled=False
- C2: periodic snapshots never start (starter-ordering bug: managers empty)
- C3: no rotation mechanism exists
- C4: no incremental build (always full rebuild)
- C5: no size-guard on final build
- C6: loader doesn't parse new keys (KeyError or missing attributes)
- C7: no validator for persistence snapshot keys
- C8: JSON output uses indent=2 (not compact)
- C9: simple mode behavior unchanged but needs verification with new flag
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest

from config.schemas import PersistenceConfig


# ---------------------------------------------------------------------------
# C1 — flag off ⇒ no snapshots at all (periodic AND final)
# ---------------------------------------------------------------------------


def test_c1_flag_off_no_snapshots_built():
    """WHEN snapshots_enabled=False
    THEN neither periodic nor final snapshots are built."""
    from storage.persistence import MultiResultManager

    with tempfile.TemporaryDirectory() as tmpdir:
        provider = MagicMock()
        provider.name = "test_provider"
        provider.result.folder = "test"
        provider.result.filenames = {
            "links": "links.ndjson",
            "material": "material.ndjson",
            "invalid": "invalid.ndjson",
        }

        mgr = MultiResultManager(
            workspace=tmpdir,
            providers={"test_provider": provider},
            batch_size=50,
            save_interval=30.0,
            simple=False,
            shutdown_timeout=5.0,
            snapshots_enabled=False,
            snapshot_interval_s=3600,
            snapshot_keep_last_n=3,
            snapshot_final_max_bytes=2_000_000_000,
        )

        # Create a manager and add some data
        rm = mgr.get_manager("test_provider")
        rm.add_result("links", [{"url": "https://example.com"}])
        rm.flush_all()

        # Mock build_all_snapshots to track calls
        original_build = rm.build_all_snapshots
        build_calls = []

        def tracked_build():
            build_calls.append(time.time())
            return original_build()

        rm.build_all_snapshots = tracked_build

        # Stop should NOT build snapshots when disabled
        rm.stop()

        assert len(build_calls) == 0, "Final snapshot built despite snapshots_enabled=False"


# ---------------------------------------------------------------------------
# C2 — flag on ⇒ periodic snapshots start and respect interval
# ---------------------------------------------------------------------------


def test_c2_flag_on_periodic_snapshots_start_with_interval():
    """WHEN snapshots_enabled=True and snapshot_interval_s=60
    THEN periodic snapshots start and respect the interval."""
    from storage.persistence import MultiResultManager

    with tempfile.TemporaryDirectory() as tmpdir:
        provider = MagicMock()
        provider.name = "test_provider"
        provider.result.folder = "test"
        provider.result.filenames = {
            "links": "links.ndjson",
            "material": "material.ndjson",
            "invalid": "invalid.ndjson",
        }

        mgr = MultiResultManager(
            workspace=tmpdir,
            providers={"test_provider": provider},
            batch_size=50,
            save_interval=30.0,
            simple=False,
            shutdown_timeout=5.0,
            snapshots_enabled=True,
            snapshot_interval_s=60,
            snapshot_keep_last_n=3,
            snapshot_final_max_bytes=2_000_000_000,
        )

        # Create a manager - should auto-start periodic snapshots
        rm = mgr.get_manager("test_provider")

        # Verify snapshot_manager exists and periodic thread is running
        assert rm.snapshot_manager is not None, "snapshot_manager should exist in non-simple mode"
        assert rm.snapshot_manager._periodic_thread is not None, "Periodic thread should be started"
        assert rm.snapshot_manager._running, "Periodic snapshot thread should be running"

        # Cleanup
        rm.stop_periodic_snapshot()


# ---------------------------------------------------------------------------
# C3 — rotation keep-last-N
# ---------------------------------------------------------------------------


def test_c3_rotation_keep_last_n():
    """WHEN snapshot_keep_last_n=2 and 3 generations are built
    THEN only the 2 most recent generations remain on disk."""
    from storage.strategies import SnapshotManager

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create shard directory
        shard_root = os.path.join(tmpdir, "shards", "links")
        os.makedirs(shard_root, exist_ok=True)

        # Create a shard file
        shard_file = os.path.join(shard_root, "shard_001.ndjson")
        with open(shard_file, "w") as f:
            f.write('{"url": "https://example.com/1"}\n')

        # Create index
        index_file = os.path.join(shard_root, "shard_001.index.json")
        with open(index_file, "w") as f:
            json.dump({"first_ts": "2026-01-01T00:00:00Z", "last_ts": "2026-01-01T00:01:00Z"}, f)

        snapshots_dir = os.path.join(tmpdir, "snapshots")
        os.makedirs(snapshots_dir, exist_ok=True)

        sm = SnapshotManager(tmpdir, ["links"], "test_provider")

        # Build 3 generations with keep_last_n=2
        # Use build_all_snapshots to create complete generations
        for i in range(3):
            sm.build_all_snapshots(keep_last_n=2)
            time.sleep(0.01)  # Ensure different timestamps

        # Check that only 2 generations remain on disk
        # Generations are versioned: links.json.1, links.json.2, links.json.3
        # After 3 builds with keep_last_n=2, only .2 and .3 should exist
        gen_files = sorted([f for f in os.listdir(snapshots_dir) if f.startswith("links.json.")])
        
        assert len(gen_files) == 2, f"Expected 2 generations, found {len(gen_files)}: {gen_files}"
        assert "links.json.2" in gen_files, "Generation 2 should be present"
        assert "links.json.3" in gen_files, "Generation 3 should be present"
        assert "links.json.1" not in gen_files, "Generation 1 should be deleted (oldest)"
        
        # Verify current snapshot exists
        current_snapshot = os.path.join(snapshots_dir, "links.json")
        assert os.path.exists(current_snapshot), "Current snapshot should exist"


# ---------------------------------------------------------------------------
# C4 — incremental build (second build reads only new shards)
# ---------------------------------------------------------------------------


def test_c4_incremental_build_reads_only_new_shards():
    """WHEN a second build is triggered after adding new shards
    THEN only the new shards are read (incremental via .build_meta.json)."""
    from storage.strategies import SnapshotManager

    with tempfile.TemporaryDirectory() as tmpdir:
        shard_root = os.path.join(tmpdir, "shards", "links")
        os.makedirs(shard_root, exist_ok=True)

        # Create first shard with timestamp in the past
        shard1 = os.path.join(shard_root, "shard_001.ndjson")
        with open(shard1, "w") as f:
            f.write('{"url": "https://example.com/1"}\n')

        index1 = os.path.join(shard_root, "shard_001.index.json")
        with open(index1, "w") as f:
            json.dump({"first_ts": "2020-01-01T00:00:00Z", "last_ts": "2020-01-01T00:01:00Z"}, f)

        sm = SnapshotManager(tmpdir, ["links"], "test_provider")

        # First build — should read shard_001
        count1 = sm.build_snapshot("links")
        assert count1 == 1

        # Verify .build_meta.json was created with ts and processed shards
        snapshots_dir = os.path.join(tmpdir, "snapshots")
        build_meta_path = os.path.join(snapshots_dir, ".build_meta.json")
        assert os.path.exists(build_meta_path), ".build_meta.json should exist after first build"

        with open(build_meta_path) as f:
            meta1 = json.load(f)
        assert "ts" in meta1, "build_meta should have ts marker"
        assert "shard_001.ndjson" in str(meta1), "build_meta should track shard_001"

        # Add second shard with timestamp in the future (after the first build)
        shard2 = os.path.join(shard_root, "shard_002.ndjson")
        with open(shard2, "w") as f:
            f.write('{"url": "https://example.com/2"}\n')

        index2 = os.path.join(shard_root, "shard_002.index.json")
        with open(index2, "w") as f:
            json.dump({"first_ts": "2030-01-01T00:02:00Z", "last_ts": "2030-01-01T00:03:00Z"}, f)

        # Spy on file opens to verify incremental behavior
        original_open = open
        files_read = []

        def tracked_open(file, mode="r", *args, **kwargs):
            if isinstance(file, str) and file.endswith(".ndjson") and "shards" in file:
                files_read.append(os.path.basename(file))
            return original_open(file, mode, *args, **kwargs)

        with patch("builtins.open", side_effect=tracked_open):
            count2 = sm.build_snapshot("links")

        # Second build should read ONLY the new shard (incremental)
        # Incremental build: existing snapshot (1 record) + new shard (1 record) = 2 records
        assert count2 == 2, "Second build should include both shards in output"
        assert "shard_002.ndjson" in files_read, "Should read the new shard"
        assert "shard_001.ndjson" not in files_read, "Should NOT re-read the old shard (incremental)"


def test_c4_incremental_build_detects_updated_shard():
    """WHEN a shard is updated (new last_ts in .index.json) after first build
    THEN second build MUST re-read that shard (ts-based filter, not name-based)."""
    from storage.strategies import SnapshotManager
    import datetime

    with tempfile.TemporaryDirectory() as tmpdir:
        shard_root = os.path.join(tmpdir, "shards", "links")
        os.makedirs(shard_root, exist_ok=True)

        # Create first shard with timestamp in the past
        shard1 = os.path.join(shard_root, "shard_001.ndjson")
        with open(shard1, "w") as f:
            f.write('{"url": "https://example.com/1"}\n')

        # Use a timestamp that's definitely in the past
        past_ts = "2020-01-01T00:01:00Z"
        index1 = os.path.join(shard_root, "shard_001.index.json")
        with open(index1, "w") as f:
            json.dump({"first_ts": "2020-01-01T00:00:00Z", "last_ts": past_ts}, f)

        sm = SnapshotManager(tmpdir, ["links"], "test_provider")

        # First build
        count1 = sm.build_snapshot("links")
        assert count1 == 1

        # Update shard_001 with new data and a timestamp in the future (after the build)
        with open(shard1, "w") as f:
            f.write('{"url": "https://example.com/1"}\n')
            f.write('{"url": "https://example.com/1-updated"}\n')

        # Use a timestamp that's definitely in the future (after the first build)
        future_ts = "2030-01-01T00:05:00Z"
        with open(index1, "w") as f:
            json.dump({"first_ts": "2020-01-01T00:00:00Z", "last_ts": future_ts}, f)

        # Spy on file opens
        original_open = open
        files_read = []

        def tracked_open(file, mode="r", *args, **kwargs):
            if isinstance(file, str) and file.endswith(".ndjson") and "shards" in file:
                files_read.append(os.path.basename(file))
            return original_open(file, mode, *args, **kwargs)

        with patch("builtins.open", side_effect=tracked_open):
            count2 = sm.build_snapshot("links")

        # Second build MUST re-read shard_001 because its last_ts is newer
        assert "shard_001.ndjson" in files_read, "Should re-read updated shard (ts-based filter)"
        # Incremental build: existing snapshot (1 record) + updated shard (2 records) = 3 records
        assert count2 == 3, "Should include old records plus updated shard records"


# ---------------------------------------------------------------------------
# C5 — size-guard on final build
# ---------------------------------------------------------------------------


def test_c5_size_guard_skips_final_build_when_exceeded():
    """WHEN snapshot_final_max_bytes is exceeded by shard size
    THEN final build is skipped with a WARNING."""
    from storage.persistence import ResultManager

    with tempfile.TemporaryDirectory() as tmpdir:
        provider = MagicMock()
        provider.name = "test_provider"
        provider.result.folder = "test"
        provider.result.filenames = {
            "links": "links.ndjson",
            "material": "material.ndjson",
            "invalid": "invalid.ndjson",
        }

        rm = ResultManager(
            provider=provider,
            workspace=tmpdir,
            batch_size=50,
            save_interval=30.0,
            simple=False,
            shutdown_timeout=5.0,
            snapshots_enabled=True,
            snapshot_final_max_bytes=100,  # Very small limit
        )

        # Add data that exceeds the limit
        rm.add_result("links", [{"url": "https://example.com/" + "x" * 200}])
        rm.flush_all()

        # Mock build to track if it's called
        build_called = []
        original_build = rm.build_all_snapshots

        def tracked_build():
            build_called.append(True)
            return original_build()

        rm.build_all_snapshots = tracked_build

        # Stop should skip final build due to size guard
        rm.stop()

        assert len(build_called) == 0, "Final build should be skipped when size exceeds limit"


# ---------------------------------------------------------------------------
# C6 — loader parses all four new keys with correct defaults
# ---------------------------------------------------------------------------


def test_c6_loader_parses_new_keys_with_defaults():
    """WHEN persistence config is loaded without snapshot keys
    THEN defaults are applied correctly."""
    from config.loader import ConfigLoader

    loader = ConfigLoader()

    # Parse with minimal data (no snapshot keys)
    data = {
        "batch_size": 50,
        "save_interval": 30,
        "queue_interval": 60,
        "auto_restore": True,
        "shutdown_timeout": 30,
        "format": "ndjson",
    }

    cfg = loader._parse_persistence_config(data)

    assert cfg.snapshots_enabled is False, "Default snapshots_enabled should be False"
    assert cfg.snapshot_interval_s == 3600, "Default snapshot_interval_s should be 3600"
    assert cfg.snapshot_keep_last_n == 3, "Default snapshot_keep_last_n should be 3"
    assert cfg.snapshot_final_max_bytes == 2_000_000_000, "Default snapshot_final_max_bytes should be 2e9"


def test_c6_loader_parses_explicit_values():
    """WHEN persistence config is loaded with explicit snapshot keys
    THEN values are parsed correctly."""
    from config.loader import ConfigLoader

    loader = ConfigLoader()

    data = {
        "batch_size": 50,
        "save_interval": 30,
        "queue_interval": 60,
        "auto_restore": True,
        "shutdown_timeout": 30,
        "format": "ndjson",
        "snapshots_enabled": True,
        "snapshot_interval_s": 1800,
        "snapshot_keep_last_n": 5,
        "snapshot_final_max_bytes": 1_000_000_000,
    }

    cfg = loader._parse_persistence_config(data)

    assert cfg.snapshots_enabled is True
    assert cfg.snapshot_interval_s == 1800
    assert cfg.snapshot_keep_last_n == 5
    assert cfg.snapshot_final_max_bytes == 1_000_000_000


# ---------------------------------------------------------------------------
# C7 — validator rejects negative values with mirrored messages
# ---------------------------------------------------------------------------


def test_c7_validator_rejects_negative_snapshot_interval():
    """WHEN snapshot_interval_s is negative
    THEN validator rejects with appropriate error."""
    # The dataclass __post_init__ should reject this
    with pytest.raises(ValueError, match="persistence.snapshot_interval_s must be positive"):
        PersistenceConfig(snapshot_interval_s=-100)


def test_c7_validator_rejects_negative_keep_last_n():
    """WHEN snapshot_keep_last_n is negative
    THEN validator rejects with appropriate error."""
    with pytest.raises(ValueError, match="persistence.snapshot_keep_last_n must be non-negative"):
        PersistenceConfig(snapshot_keep_last_n=-1)


def test_c7_validator_rejects_negative_final_max_bytes():
    """WHEN snapshot_final_max_bytes is negative
    THEN validator rejects with appropriate error."""
    with pytest.raises(ValueError, match="persistence.snapshot_final_max_bytes must be positive"):
        PersistenceConfig(snapshot_final_max_bytes=-100)


# ---------------------------------------------------------------------------
# C8 — compact JSON without indent
# ---------------------------------------------------------------------------


def test_c8_compact_json_without_indent():
    """WHEN a snapshot is built
    THEN the JSON output is compact (no indent)."""
    from storage.strategies import SnapshotManager

    with tempfile.TemporaryDirectory() as tmpdir:
        shard_root = os.path.join(tmpdir, "shards", "links")
        os.makedirs(shard_root, exist_ok=True)

        # Create a shard with pretty data
        shard_file = os.path.join(shard_root, "shard_001.ndjson")
        with open(shard_file, "w") as f:
            f.write('{"url": "https://example.com", "name": "test"}\n')

        index_file = os.path.join(shard_root, "shard_001.index.json")
        with open(index_file, "w") as f:
            json.dump({"first_ts": "2026-01-01T00:00:00Z", "last_ts": "2026-01-01T00:01:00Z"}, f)

        sm = SnapshotManager(tmpdir, ["links"], "test_provider")
        sm.build_snapshot("links")

        # Read the snapshot
        snapshot_path = os.path.join(tmpdir, "snapshots", "links.json")
        with open(snapshot_path) as f:
            content = f.read()

        # Should be compact (no indentation)
        assert "\n  " not in content, "Snapshot should be compact JSON without indent"
        # Should still be valid JSON
        parsed = json.loads(content)
        assert len(parsed) == 1


# ---------------------------------------------------------------------------
# C9 — simple mode semantics with new flag
# ---------------------------------------------------------------------------


def test_c9_simple_mode_flag_does_not_leak():
    """WHEN simple=True (format: txt) regardless of snapshots_enabled value
    THEN behavior is byte-identical to legacy simple mode:
    1. No snapshot files are created
    2. TXT output is identical
    3. Periodic thread never starts"""
    from storage.persistence import ResultManager

    def run_simple_arm(snapshots_enabled: bool, tmpdir: str) -> dict:
        """Run one arm of simple mode test and return results."""
        provider = MagicMock()
        provider.name = "test_provider"
        provider.result.folder = "test"
        provider.result.filenames = {
            "links": "links.txt",
            "material": "material.txt",
            "invalid": "invalid.txt",
        }

        # Spy on start_periodic_snapshot
        periodic_started = []
        original_start = ResultManager.start_periodic_snapshot
        def tracked_start(self, *args, **kwargs):
            periodic_started.append(True)
            return original_start(self, *args, **kwargs)
        
        with patch.object(ResultManager, 'start_periodic_snapshot', tracked_start):
            rm = ResultManager(
                provider=provider,
                workspace=tmpdir,
                batch_size=50,
                save_interval=30.0,
                simple=True,
                shutdown_timeout=5.0,
                snapshots_enabled=snapshots_enabled,
                snapshot_final_max_bytes=2_000_000_000,
            )

            # Verify snapshot_manager is None in simple mode
            assert rm.snapshot_manager is None, f"snapshot_manager should be None in simple mode (enabled={snapshots_enabled})"

            # Add identical data
            rm.add_result("links", ["https://example.com/1", "https://example.com/2"])
            rm.flush_all()
            rm.stop()

        return {
            "periodic_started": len(periodic_started),
            "snapshot_manager": rm.snapshot_manager,
        }

    # Run both arms in separate temp directories
    with tempfile.TemporaryDirectory() as tmpdir_true:
        result_true = run_simple_arm(True, tmpdir_true)
        
        # Read TXT output from enabled=True arm
        txt_true_path = os.path.join(tmpdir_true, "providers", "test", "links.txt")
        with open(txt_true_path, 'r') as f:
            txt_true_content = f.read()
        
        # Check no snapshot files created
        snapshot_dir_true = os.path.join(tmpdir_true, "providers", "test", "snapshots")
        assert not os.path.exists(snapshot_dir_true), "No snapshots directory should exist in simple mode (enabled=True)"

    with tempfile.TemporaryDirectory() as tmpdir_false:
        result_false = run_simple_arm(False, tmpdir_false)
        
        # Read TXT output from enabled=False arm
        txt_false_path = os.path.join(tmpdir_false, "providers", "test", "links.txt")
        with open(txt_false_path, 'r') as f:
            txt_false_content = f.read()
        
        # Check no snapshot files created
        snapshot_dir_false = os.path.join(tmpdir_false, "providers", "test", "snapshots")
        assert not os.path.exists(snapshot_dir_false), "No snapshots directory should exist in simple mode (enabled=False)"

    # Assert byte-identical TXT output
    assert txt_true_content == txt_false_content, "TXT output must be byte-identical regardless of snapshots_enabled"
    
    # Assert no snapshot files in either arm
    assert result_true["snapshot_manager"] is None
    assert result_false["snapshot_manager"] is None
    
    # Assert periodic thread never started in either arm
    assert result_true["periodic_started"] == 0, "Periodic snapshots should not start in simple mode (enabled=True)"
    assert result_false["periodic_started"] == 0, "Periodic snapshots should not start in simple mode (enabled=False)"


def test_c10_unknown_persistence_key_warns(caplog):
    """WHEN persistence config contains an unknown key (e.g., old dead snapshot_interval)
    THEN loader produces a WARNING naming the unknown key and listing known keys.
    
    This is a pin-insurance test: the mechanism was implemented in 1fd18d2 (loader.py:325-335),
    so RED is not possible — this pin locks in the existing behavior."""
    import logging
    from config.loader import ConfigLoader
    
    loader = ConfigLoader("/fake/path.yaml")
    
    # Config with unknown key (old dead snapshot_interval)
    data = {
        "batch_size": 50,
        "save_interval": 30,
        "queue_interval": 60,
        "auto_restore": True,
        "shutdown_timeout": 30,
        "format": "ndjson",
        "snapshots_enabled": False,
        "snapshot_interval": 300,  # Unknown key (old dead key)
    }
    
    with caplog.at_level(logging.WARNING):
        cfg = loader._parse_persistence_config(data)
    
    # Verify WARNING was logged
    assert any(
        r.levelno >= logging.WARNING and "snapshot_interval" in r.getMessage() and "ignored" in r.getMessage()
        for r in caplog.records
    ), "Unknown persistence key should produce a WARNING naming it"
    
    # Verify config still loaded successfully with defaults
    assert cfg.batch_size == 50
    assert cfg.snapshot_interval_s == 3600  # Default, not the unknown key
