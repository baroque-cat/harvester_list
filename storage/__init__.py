#!/usr/bin/env python3

"""
Storage Package - Data Persistence and Storage Management

This package provides comprehensive data storage and persistence capabilities:
- Result management and aggregation
- Atomic file operations with fsync
- NDJSON shard management with rotation
- Snapshot management for backup/restore
- Cross-platform file locking

Key Features:
- Thread-safe storage operations
- Atomic writes to prevent corruption
- Automatic shard rotation and indexing
- Performance-optimized batch operations
"""

# Low-level storage operations
from .atomic import AtomicFileWriter, repair_trailing_partial

# Core persistence management
from .persistence import MultiResultManager, ResultBuffer, ResultManager

# Recovery mechanisms (moved to manager package)
from .shard import NDJSONShardWriter

# Snapshot and backup management
from .strategies import SnapshotManager

__all__ = [
    # Result management
    "MultiResultManager",
    "ResultBuffer",
    "ResultManager",
    # Atomic storage operations
    "AtomicFileWriter",
    "NDJSONShardWriter",
    "repair_trailing_partial",
    # Snapshot management
    "SnapshotManager",
    # Recovery mechanisms (moved to manager package)
]
