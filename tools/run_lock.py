#!/usr/bin/env python3

"""
Run-lock file for mutual exclusion (slice 4.1, wave 4).

Manages ``<workspace>/.harvester.lock`` — a JSON file containing
``{"pid": <int>, "boot_ts": <float>, "holder": "pipeline"|"purge"}``.
Created atomically via ``os.open(O_CREAT|O_EXCL|O_WRONLY)`` with mode 0600.

Design constraints:
- Fail-CLOSED: a live lock blocks the second acquirer unconditionally.
- Stale-takeover: dead pid → loud INFO + atomic replacement.
- Release is pid-guarded: only the owning pid can remove the file.
- No secrets in the lock file or logs (pid/ts/holder only).

The purge-side of the mutual exclusion (purge acquiring the same lock)
lands in slice 4.2; this module provides the shared helper.
"""

import json
import os
import time
from typing import Optional

from tools.logger import get_logger

logger = get_logger("tools")

LOCK_FILENAME = ".harvester.lock"


def _lock_path(workspace: str) -> str:
    """Return the absolute path to the lock file."""
    return os.path.join(workspace, LOCK_FILENAME)


def is_alive(info: dict) -> bool:
    """Check if the process described by *info* is still alive.

    Uses ``os.kill(pid, 0)``:
    - ``ProcessLookupError`` → dead → return False.
    - ``PermissionError`` → alive but not ours → return True (fail-CLOSED).
    - Success → alive → return True.
    """
    pid = int(info["pid"])
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we don't have permission to signal it.
        # Fail-CLOSED: treat as alive.
        return True


def read_lock(workspace: str) -> Optional[dict]:
    """Read and parse the lock file.  Returns None if absent or corrupt.

    Fail-CLOSED: corrupt JSON is treated as "no lock" (not live), but a
    WARNING is logged so the operator can investigate.
    """
    path = _lock_path(workspace)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Basic validation
        if "pid" not in data or "holder" not in data:
            logger.warning(f"run-lock: file at {path} missing required fields, treating as absent")
            return None
        return data
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        logger.warning(f"run-lock: failed to parse {path}: {exc} — treating as absent")
        return None


def acquire_lock(workspace: str, holder: str) -> None:
    """Acquire the run lock for *holder* (``"pipeline"`` or ``"purge"``).

    Raises ``LockHeldError`` if a live process already holds the lock.
    Stale locks (dead pid) are taken over with a loud INFO message.

    Race note: two processes attempting stale takeover simultaneously —
    one will succeed at the O_EXCL create, the other will get LockHeldError
    (the first already wrote its lock).  This is acceptable: the second
    process sees a live lock and refuses.
    """
    from core.exceptions import LockHeldError

    path = _lock_path(workspace)

    # Check for existing lock
    existing = read_lock(workspace)
    if existing is not None:
        if is_alive(existing):
            raise LockHeldError(
                f"run lock held by live process: pid={existing['pid']} "
                f"holder={existing['holder']} boot_ts={existing.get('boot_ts', '?')}"
            )
        else:
            # Stale lock — take over loudly
            logger.info(
                f"stale lock ignored (holder={existing['holder']}, pid={existing['pid']})"
            )
            # Atomic replacement: unlink stale, then O_EXCL create
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass  # Another process already took it

    # Create lock atomically
    data = {
        "pid": os.getpid(),
        "boot_ts": time.time(),
        "holder": holder,
    }
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        # Ambiguity (05 §5.3.3): the file exists but is not a readable
        # live/stale lock (corrupt content, or a lost takeover race).
        # Fail-CLOSED: typed refusal; NEVER delete an ambiguous lock.
        raced = read_lock(workspace)
        if raced is not None and is_alive(raced):
            raise LockHeldError(
                f"run lock held by live process: pid={raced['pid']} "
                f"holder={raced['holder']} boot_ts={raced.get('boot_ts', '?')}"
            )
        raise LockHeldError(
            f"run lock at {path} exists but is corrupt/unreadable — "
            "refusing to act (fail-CLOSED); remove the file manually"
        )
    try:
        os.write(fd, json.dumps(data).encode("utf-8"))
    finally:
        os.close(fd)


def release_lock(workspace: str) -> bool:
    """Release the run lock if it belongs to this process.

    Returns True if the lock was removed, False if:
    - No lock file exists (idempotent).
    - Lock belongs to a different pid (never removes another's lock).
    """
    path = _lock_path(workspace)
    if not os.path.exists(path):
        return False

    info = read_lock(workspace)
    if info is None:
        return False

    if int(info["pid"]) != os.getpid():
        return False

    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False
