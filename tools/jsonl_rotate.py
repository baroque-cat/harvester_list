#!/usr/bin/env python3

"""
Size-based rotation for JSONL decision logs (C8/F-20, slice 2.5).

Provides a single function ``maybe_rotate_jsonl`` that rotates a JSONL file
when it exceeds a byte threshold, keeping the last N generations.  Used by
gather-skip, early-stop, and aggregation decision-log writers.

Design constraints:
- Fail-open: OS errors during rotation are caught, logged at debug level,
  and the function returns False.  Decision logs are observability data;
  rotation failure must never halt the pipeline.
- Per-path modular lock: two independent handle-writers of the same file
  (e.g. gather-skip and early-stop both write to registry_decisions.jsonl)
  must not double-shift generations.  The lock is module-level, keyed by
  absolute path.
- Interleave of two writers is acceptable (observability data, fail-open
  doctrine); generations may mix only if both writers are active simultaneously.
"""

import os
import threading
from typing import Dict

from tools.logger import get_logger

logger = get_logger("tools")

# Per-path locks to prevent double-rotation when two writers share a file.
_rotation_locks: Dict[str, threading.Lock] = {}
_rotation_locks_guard = threading.Lock()


def _get_path_lock(path: str) -> threading.Lock:
    """Return the per-path rotation lock, creating it if needed."""
    abs_path = os.path.abspath(path)
    with _rotation_locks_guard:
        if abs_path not in _rotation_locks:
            _rotation_locks[abs_path] = threading.Lock()
        return _rotation_locks[abs_path]


def maybe_rotate_jsonl(path: str, max_bytes: int, keep_n: int) -> bool:
    """Rotate a JSONL file if it exceeds ``max_bytes``.

    Shifts existing generations: ``path.{keep_n}`` is deleted, ``path.{i}``
    becomes ``path.{i+1}`` for i in [keep_n-1..1], and ``path`` becomes
    ``path.1``.  Returns True if rotation occurred, False otherwise.

    Fail-open: any OS error during rotation is caught, logged at debug level,
    and False is returned.  The caller should continue writing to the live
    file regardless.

    Args:
        path: Path to the live JSONL file.
        max_bytes: Size threshold in bytes; rotation triggers at >= this.
        keep_n: Number of rotated generations to keep (e.g. 2 keeps .1 and .2).

    Returns:
        True if rotation occurred, False otherwise (including on error).
    """
    try:
        # Fast path: stat without lock; if below threshold, no rotation needed.
        try:
            size = os.path.getsize(path)
        except OSError:
            return False

        if size < max_bytes:
            return False

        # Threshold exceeded; acquire per-path lock and re-check (another
        # writer may have rotated between our stat and lock acquisition).
        path_lock = _get_path_lock(path)
        with path_lock:
            try:
                size = os.path.getsize(path)
            except OSError:
                return False

            if size < max_bytes:
                return False

            # Shift generations: delete path.{keep_n}, shift i→i+1.
            abs_path = os.path.abspath(path)
            dir_name = os.path.dirname(abs_path)
            base_name = os.path.basename(abs_path)

            # Delete the oldest generation if it exists.
            oldest = os.path.join(dir_name, f"{base_name}.{keep_n}")
            try:
                if os.path.exists(oldest):
                    os.remove(oldest)
            except OSError as exc:
                logger.debug(f"[jsonl-rotate] failed to remove {oldest}: {exc}")

            # Shift i → i+1 for i in [keep_n-1..1].
            for i in range(keep_n - 1, 0, -1):
                src = os.path.join(dir_name, f"{base_name}.{i}")
                dst = os.path.join(dir_name, f"{base_name}.{i + 1}")
                try:
                    if os.path.exists(src):
                        os.rename(src, dst)
                except OSError as exc:
                    logger.debug(f"[jsonl-rotate] failed to shift {src} → {dst}: {exc}")

            # Move live file to .1.
            gen_1 = os.path.join(dir_name, f"{base_name}.1")
            try:
                os.rename(abs_path, gen_1)
            except OSError as exc:
                logger.debug(f"[jsonl-rotate] failed to rotate {abs_path} → {gen_1}: {exc}")
                return False

            return True

    except Exception as exc:  # fail-open: never fatal
        logger.debug(f"[jsonl-rotate] rotation failed (fail-open): {exc}")
        return False
