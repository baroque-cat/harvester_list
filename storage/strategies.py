#!/usr/bin/env python3

"""
Persistence strategies for different storage modes.

This module defines the strategy pattern implementation for handling
different persistence approaches: simple file-based and shard-based storage.
"""

import json
import os
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from state.models import PersistenceMetrics
from tools.logger import get_logger

from .atomic import AtomicFileWriter
from .shard import NDJSONShardWriter
from .snapshot import BaseSnapshotManager

logger = get_logger("storage")


class PersistenceStrategy(ABC):
    """Abstract base class for persistence strategies.

    Defines the interface that all persistence strategies must implement.
    This allows for clean separation between simple file-based and
    shard-based persistence approaches.
    """

    def __init__(self, directory: str, files: Dict[str, str]):
        """Initialize strategy with directory and file mappings.

        Args:
            directory: Base directory for storage
            files: Mapping of result types to file paths
        """
        self.directory = directory
        self.files = files

    @abstractmethod
    def write_data(self, result_type: str, items: List[Any], stats: PersistenceMetrics) -> None:
        """Write data items to storage.

        Args:
            result_type: Type of result being stored
            items: List of items to store
            stats: Metrics object to update
        """
        pass

    @abstractmethod
    def supports_snapshots(self) -> bool:
        """Check if this strategy supports snapshot generation.

        Returns:
            True if snapshots are supported, False otherwise
        """
        pass

    @abstractmethod
    def cleanup(self) -> None:
        """Cleanup strategy resources."""
        pass


class SimpleFileStrategy(PersistenceStrategy):
    """Simple text file persistence strategy.

    Stores data as plain text files, one item per line.
    Does not support snapshots or advanced features.
    """

    def write_data(self, result_type: str, items: List[Any], stats: PersistenceMetrics) -> None:
        """Write items to simple text file.

        Args:
            result_type: Type of result being stored
            items: List of items to store
            stats: Metrics object to update
        """
        if not items:
            return

        filepath = self.files.get(result_type)
        if not filepath:
            logger.error(f"No file path configured for result type: {result_type}")
            return

        try:
            # Convert items to text lines
            lines = []
            for item in items:
                if hasattr(item, "serialize"):
                    lines.append(item.serialize())
                else:
                    lines.append(str(item))

            # Write to file atomically
            AtomicFileWriter.append_atomic(filepath, lines)

            logger.info(f"Saved {len(lines)} {result_type} items to simple file")

        except Exception as e:
            logger.error(f"Failed to write {result_type} to simple file: {e}")

    def supports_snapshots(self) -> bool:
        """Simple files do not support snapshots."""
        return False

    def cleanup(self) -> None:
        """No cleanup needed for simple files."""
        pass


class ShardStrategy(PersistenceStrategy):
    """NDJSON shard persistence strategy.

    Stores data as NDJSON shard files with indexing and rotation.
    Supports snapshot generation and advanced features.
    """

    def __init__(self, directory: str, files: Dict[str, str]):
        """Initialize shard strategy.

        Args:
            directory: Base directory for storage
            files: Mapping of result types to file paths
        """
        super().__init__(directory, files)
        self._shard_writers: Dict[str, NDJSONShardWriter] = {}
        self._lock = threading.Lock()

    def write_data(self, result_type: str, items: List[Any], stats: PersistenceMetrics) -> None:
        """Write items to NDJSON shard files.

        Args:
            result_type: Type of result being stored
            items: List of items to store
            stats: Metrics object to update
        """
        if not items:
            return

        try:
            # Convert items to NDJSON records
            records = []
            for item in items:
                if hasattr(item, "serialize"):
                    serialized = item.serialize()
                    try:
                        # Try to parse as JSON for structured storage
                        records.append(json.loads(serialized))
                    except Exception:
                        # Fallback to string envelope
                        records.append({"value": serialized})
                else:
                    records.append({"value": str(item)})

            # Write to shard
            writer = self._get_shard_writer(result_type)
            writer.append_records(records, stats)

            logger.info(f"Saved {len(records)} {result_type} items to shard")

        except Exception as e:
            logger.error(f"Failed to write {result_type} to shard: {e}")

    def supports_snapshots(self) -> bool:
        """Shard files support snapshots."""
        return True

    def cleanup(self) -> None:
        """Cleanup shard writers and resources."""
        with self._lock:
            self._shard_writers.clear()

    def _get_shard_writer(self, result_type: str) -> NDJSONShardWriter:
        """Get or create shard writer for result type.

        Args:
            result_type: Type of result

        Returns:
            NDJSONShardWriter instance
        """
        with self._lock:
            writer = self._shard_writers.get(result_type)
            if not writer:
                shard_root = os.path.join(self.directory, "shards")
                writer = NDJSONShardWriter(shard_root, result_type)
                self._shard_writers[result_type] = writer
            return writer


class SnapshotManager:
    """Dedicated snapshot management with periodic building support.

    Manages the lifecycle of snapshot generation including periodic
    background building, incremental builds, rotation, and proper cleanup.
    
    Generation tracking:
    - Each build creates versioned files: {type}.json.{generation}
    - .build_meta.json tracks: current_generation, processed_shards per type, generations list
    - Rotation removes old generations keeping only last N
    """

    def __init__(self, directory: str, result_types: List[str], provider_name: str, keep_last_n: int = 0):
        """Initialize snapshot manager.

        Args:
            directory: Base directory containing shards
            result_types: List of result types to manage
            provider_name: Name of the provider for logging
            keep_last_n: Configured rotation default for no-arg builds
                (0 = keep all; explicit per-call arguments override)
        """
        self.directory = directory
        self.result_types = result_types
        self.provider_name = provider_name
        self.keep_last_n = keep_last_n

        # Thread management
        self._periodic_thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.Lock()

        # Statistics
        self.stats = {"last_snapshot": 0.0, "snapshot_count": 0, "total_snapshot_time": 0.0, "snapshot_operations": 0}
        
        # Snapshots directory
        self.snapshots_dir = os.path.join(self.directory, "snapshots")
        os.makedirs(self.snapshots_dir, exist_ok=True)
        
        # Build metadata path
        self.build_meta_path = os.path.join(self.snapshots_dir, ".build_meta.json")

    def _load_build_meta(self) -> dict:
        """Load build metadata from .build_meta.json."""
        if not os.path.exists(self.build_meta_path):
            return {
                "current_generation": 1,
                "ts": None,  # No previous build timestamp
                "shards": [],  # List of shard files included in last build
                "generations": []
            }
        try:
            with open(self.build_meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
                # Ensure required fields exist (backward compatibility)
                if "ts" not in meta:
                    meta["ts"] = None
                if "shards" not in meta:
                    meta["shards"] = []
                return meta
        except Exception as e:
            logger.warning(f"Failed to load build metadata, starting fresh: {e}")
            return {
                "current_generation": 1,
                "ts": None,
                "shards": [],
                "generations": []
            }

    def _save_build_meta(self, meta: dict) -> None:
        """Save build metadata to .build_meta.json."""
        try:
            with open(self.build_meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save build metadata: {e}")

    def build_snapshot(self, result_type: str, keep_last_n: int = 0) -> int:
        """Build snapshot for specific result type with incremental support.

        Args:
            result_type: Type of result to build snapshot for
            keep_last_n: Rotation parameter - keep last N generations (0 = keep all)

        Returns:
            Number of records in the snapshot
        """
        shard_root = os.path.join(self.directory, "shards", result_type)
        if not os.path.isdir(shard_root):
            logger.debug(f"No shard directory for {result_type} in {self.provider_name}")
            return 0

        # Load build metadata
        meta = self._load_build_meta()
        current_gen = meta["current_generation"]
        last_build_ts = meta.get("ts")  # None if missing (full rebuild)
        
        # Log full rebuild scenarios
        if last_build_ts is None:
            if not os.path.exists(self.build_meta_path):
                logger.info(f"Full rebuild for {result_type} in {self.provider_name}: no build metadata found")
            else:
                logger.info(f"Full rebuild for {result_type} in {self.provider_name}: build metadata missing ts field")
        
        # Versioned snapshot path
        snapshot_path = os.path.join(self.snapshots_dir, f"{result_type}.json.{current_gen}")

        try:
            manager = BaseSnapshotManager(shard_root, snapshot_path, last_build_ts)
            start_time = time.time()
            count, new_shards = manager.build_snapshot()
            duration = time.time() - start_time

            # Update build metadata with current timestamp and shard list
            meta["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            meta["shards"] = new_shards
            
            # Also create/update current symlink or copy
            current_snapshot = os.path.join(self.snapshots_dir, f"{result_type}.json")
            if os.path.exists(snapshot_path):
                try:
                    # Copy to current (simple approach, no symlink for portability)
                    with open(snapshot_path, "r", encoding="utf-8") as src:
                        with open(current_snapshot, "w", encoding="utf-8") as dst:
                            dst.write(src.read())
                except Exception as e:
                    logger.warning(f"Failed to update current snapshot: {e}")
            
            self._save_build_meta(meta)

            # Update statistics
            with self._lock:
                self.stats["last_snapshot"] = time.time()
                self.stats["snapshot_count"] += 1
                self.stats["total_snapshot_time"] += duration
                self.stats["snapshot_operations"] += 1

            logger.info(f"Built snapshot for {result_type} with {count} records in {duration:.3f}s")
            
            # Apply rotation if keep_last_n > 0
            if keep_last_n > 0:
                self._apply_rotation(result_type, keep_last_n, meta)
            
            return count

        except Exception as e:
            logger.error(f"Failed to build snapshot for {result_type}: {e}")
            return 0

    def _apply_rotation(self, result_type: str, keep_last_n: int, meta: dict) -> None:
        """Apply rotation to keep only the last N generations of snapshots.
        
        A generation is defined as a complete set of {type}.json files from one build.
        Removes old generation files and updates metadata.
        
        Args:
            result_type: Type of result
            keep_last_n: Number of generations to keep
            meta: Build metadata dict
        """
        if keep_last_n <= 0:
            return
        
        # Find all generation files for this type
        generation_files = []
        for fn in os.listdir(self.snapshots_dir):
            if fn.startswith(f"{result_type}.json."):
                try:
                    gen_num = int(fn.split(".")[-1])
                    generation_files.append((gen_num, fn))
                except ValueError:
                    continue
        
        # Sort by generation number
        generation_files.sort()
        
        # Keep only last N generations
        if len(generation_files) > keep_last_n:
            to_delete = generation_files[:-keep_last_n]
            total_bytes_deleted = 0
            
            for gen_num, fn in to_delete:
                fp = os.path.join(self.snapshots_dir, fn)
                try:
                    file_size = os.path.getsize(fp)
                    os.remove(fp)
                    total_bytes_deleted += file_size
                    logger.info(f"Rotated out generation {gen_num} for {result_type} ({file_size} bytes)")
                except Exception as e:
                    logger.error(f"Failed to delete old generation file {fp}: {e}")
            
            if total_bytes_deleted > 0:
                logger.info(f"Rotation complete for {result_type}: deleted {total_bytes_deleted} bytes")

    def build_all_snapshots(self, keep_last_n: int = 0) -> Dict[str, int]:
        """Build snapshots for all result types and increment generation.

        Args:
            keep_last_n: Rotation parameter - keep last N generations
                (0 or negative = fall back to the configured instance default,
                which is 0 "keep all" unless injected from snapshot_keep_last_n)

        Returns:
            Dictionary mapping result types to record counts
        """
        # L3-F1 fix: production paths (periodic loop, stop-time final build)
        # call this method without arguments; resolve the configured rotation
        # so snapshot_keep_last_n is honored outside unit-test call sites.
        if keep_last_n <= 0:
            keep_last_n = self.keep_last_n
        results = {}
        
        # Build snapshots for all types
        for result_type in self.result_types:
            try:
                results[result_type] = self.build_snapshot(result_type, keep_last_n=0)
            except Exception as e:
                logger.error(f"Failed to build snapshot for {result_type}: {e}")
                results[result_type] = 0
        
        # After all types are built, increment generation and record it
        meta = self._load_build_meta()
        current_gen = meta["current_generation"]
        
        # Record this generation
        generation_record = {
            "gen": current_gen,
            "ts": time.time(),
            "files": {rt: f"{rt}.json.{current_gen}" for rt in self.result_types}
        }
        meta["generations"].append(generation_record)
        meta["current_generation"] = current_gen + 1
        self._save_build_meta(meta)
        
        # Apply rotation for all types
        if keep_last_n > 0:
            for result_type in self.result_types:
                self._apply_rotation(result_type, keep_last_n, meta)
        
        return results

    def start_periodic(self, interval_sec: int = 300) -> None:
        """Start periodic snapshot building.

        Args:
            interval_sec: Interval between snapshots in seconds
        """
        with self._lock:
            if self._periodic_thread and self._periodic_thread.is_alive():
                logger.debug(f"Periodic snapshot already running for {self.provider_name}")
                return

            self._running = True
            self._periodic_thread = threading.Thread(
                target=self._periodic_loop, args=(interval_sec,), daemon=True, name=f"snapshot-{self.provider_name}"
            )
            self._periodic_thread.start()
            logger.info(f"Started periodic snapshots for {self.provider_name} (interval: {interval_sec}s)")

    def stop(self) -> None:
        """Stop periodic snapshot building and cleanup."""
        with self._lock:
            if not self._running:
                return

            self._running = False

            if self._periodic_thread and self._periodic_thread.is_alive():
                # Wait for thread to finish
                self._periodic_thread.join(timeout=5.0)
                if self._periodic_thread.is_alive():
                    logger.warning(f"Snapshot thread for {self.provider_name} did not stop gracefully")

            self._periodic_thread = None
            logger.info(f"Stopped periodic snapshots for {self.provider_name}")

    def get_stats(self) -> Dict[str, Any]:
        """Get snapshot statistics.

        Returns:
            Dictionary with snapshot statistics
        """
        with self._lock:
            return self.stats.copy()

    def _periodic_loop(self, interval_sec: int) -> None:
        """Periodic snapshot building loop.

        Args:
            interval_sec: Interval between snapshots
        """
        while self._running:
            try:
                time.sleep(interval_sec)
                if not self._running:
                    break

                self.build_all_snapshots()

            except Exception as e:
                logger.error(f"Error in periodic snapshot for {self.provider_name}: {e}")
