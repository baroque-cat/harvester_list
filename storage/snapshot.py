"""
Snapshot manager for building pretty JSON from NDJSON shards.
"""

import json
import os
from typing import List, Optional, Set

from tools.logger import get_logger, log_aggregated_error

from .atomic import repair_trailing_partial

logger = get_logger("storage")


class BaseSnapshotManager:
    """Build pretty JSON snapshots from NDJSON shards with atomic replace.
    
    Supports incremental builds: reads only shards with last_ts newer than build marker.
    """

    def __init__(
        self,
        shard_root: str,
        snapshot_path: str,
        last_build_ts: Optional[str] = None,
    ):
        """Initialize snapshot manager.
        
        Args:
            shard_root: Directory containing shard files
            snapshot_path: Path to output snapshot file
            last_build_ts: Timestamp marker from last build (None = full rebuild)
        """
        self.shard_root = shard_root
        self.snapshot_path = snapshot_path
        self.last_build_ts = last_build_ts

    def build_snapshot(self) -> tuple[int, List[str]]:
        """Merge shards under shard_root into a streaming JSON snapshot.

        Uses streaming JSON array output to avoid loading all records into memory.
        If sidecar indexes exist, use them to order and quickly estimate content.
        Supports incremental builds: reads only shards with last_ts newer than last_build_ts.
        
        Returns:
            Tuple of (number of records written, list of shard basenames processed)
        """
        # First, try to use sidecar indexes to get a deterministic order of shards
        shards: List[str] = []
        indexed: List[tuple[str, dict]] = []
        processed_shards: List[str] = []
        
        for root, _, files in os.walk(self.shard_root):
            for fn in files:
                if not fn.endswith(".ndjson"):
                    continue
                fp = os.path.join(root, fn)
                idx_fp = os.path.splitext(fp)[0] + ".index.json"
                try:
                    with open(idx_fp, "r", encoding="utf-8") as f:
                        idx = json.load(f)
                        # Incremental build: only include shards with last_ts newer than marker
                        if self.last_build_ts is not None:
                            shard_last_ts = idx.get("last_ts", "")
                            if shard_last_ts <= self.last_build_ts:
                                # Shard is older than or equal to last build, skip it
                                continue
                        indexed.append((fp, idx))
                except Exception:
                    # No index or can't read it - include shard (full rebuild for this shard)
                    logger.debug(f"Including shard {fp} without valid index (conservative full rebuild for this shard)")
                    shards.append(fp)

        # Sort indexed shards by first_ts then last_ts
        def _ts(d: dict, key: str) -> str:
            return d.get(key) or ""

        indexed.sort(key=lambda t: (_ts(t[1], "first_ts"), _ts(t[1], "last_ts")))
        ordered = [fp for fp, _ in indexed] + sorted(shards)

        # Read existing snapshot records if doing incremental build
        existing_records: List[dict] = []
        if self.last_build_ts is not None and os.path.exists(self.snapshot_path):
            try:
                with open(self.snapshot_path, "r", encoding="utf-8") as f:
                    existing_records = json.load(f)
            except Exception as e:
                logger.warning(f"Failed to read existing snapshot for incremental build: {e}")
                existing_records = []

        # Stream write JSON array to avoid memory pressure
        temp_path = self.snapshot_path + ".tmp"
        record_count = 0

        try:
            with open(temp_path, "w", encoding="utf-8") as out_f:
                out_f.write("[")
                first_record = True

                # Write existing records first
                for record in existing_records:
                    if not first_record:
                        out_f.write(",")
                    json.dump(record, out_f, ensure_ascii=False, separators=(",", ":"))
                    first_record = False
                    record_count += 1

                # Write new records from new shards
                for fp in ordered:
                    processed_shards.append(os.path.basename(fp))
                    try:
                        with open(fp, "r", encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if not line:
                                    continue
                                try:
                                    # Validate JSON and write directly
                                    record = json.loads(line)
                                    if not first_record:
                                        out_f.write(",")
                                    json.dump(record, out_f, ensure_ascii=False, separators=(",", ":"))
                                    first_record = False
                                    record_count += 1
                                except Exception as e:
                                    # Skip bad lines in snapshot to maximize robustness
                                    log_aggregated_error(
                                        logger, f"bad_line_snapshot_{fp}", f"[snapshot] skipping bad line in {fp}: {e}"
                                    )
                                    continue
                    except Exception:
                        # If we failed to read, attempt to repair trailing partial and retry once
                        try:
                            repair_trailing_partial(fp)
                            with open(fp, "r", encoding="utf-8") as f:
                                for line in f:
                                    line = line.strip()
                                    if not line:
                                        continue
                                    try:
                                        record = json.loads(line)
                                        if not first_record:
                                            out_f.write(",")
                                        json.dump(record, out_f, ensure_ascii=False, separators=(",", ":"))
                                        first_record = False
                                        record_count += 1
                                    except Exception as e:
                                        log_aggregated_error(
                                            logger,
                                            f"bad_line_retry_{fp}",
                                            f"[snapshot] skipping bad line in retry {fp}: {e}",
                                        )
                                        continue
                        except Exception:
                            continue

                out_f.write("]")
                out_f.flush()
                os.fsync(out_f.fileno())

            # Atomic replace
            os.replace(temp_path, self.snapshot_path)
            return record_count, processed_shards

        except Exception as e:
            # Clean up temp file on error
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            raise e
