#!/usr/bin/env python3

"""
Maintain CLI implementation (slice 4.4, wave 4).

Provides ``vacuum()`` — manual VACUUM of registry.sqlite and all
queue_state/*.sqlite databases.  Returns high-water pages to the filesystem
after queue drain (25–35 GB in wide mode; DELETE does not shrink files).

Design constraints:
- Fail-CLOSED interlocks: lock symmetry (holder="maintain"), wal-freshness
  (reuse purge helpers), workspace existence.
- Targets: ``<workspace>/registry.sqlite`` + ALL ``<workspace>/queue_state/*.sqlite``.
- Non-destructive: VACUUM does not delete data; integrity pin mandatory.
- N-10 sidecars: VACUUM + checkpoint(TRUNCATE) leaves -wal/-shm alive —
  this is normal for a subsequently restarted DB; NEVER delete them.
- Summary is masked via redact_api_keys_in_text (structural pin).
- No receipt (canon does not require it); audit trail = log lines.
- Order: PRAGMA wal_checkpoint(TRUNCATE) BEFORE VACUUM maximises page return
  (WAL-mode: checkpoint flushes WAL pages into the main DB file, then VACUUM
  compacts the now-complete file).

Traceability: 06_disk_budget.md §6.5 (D-06.2 expanded F-04), OPEN-8/12.
"""

import glob
import os
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from tools.logger import get_logger, redact_api_keys_in_text
from tools.purge import WAL_FRESHNESS_THRESHOLD_S, _check_wal_freshness
from tools.run_lock import acquire_lock, release_lock

logger = get_logger("tools")


def _db_file_size(db_path: str) -> int:
    """Return the size of the main .sqlite file in bytes (not -wal/-shm)."""
    try:
        return os.path.getsize(db_path)
    except OSError:
        return 0


def _get_page_info(conn: sqlite3.Connection) -> Tuple[int, int]:
    """Return (page_count, page_size) for the database."""
    page_count = conn.execute("PRAGMA page_count").fetchone()[0]
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    return page_count, page_size


def _get_user_version(conn: sqlite3.Connection) -> int:
    """Return the user_version pragma value."""
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _count_all_rows(conn: sqlite3.Connection) -> int:
    """Count total rows across all user tables (integrity check)."""
    total = 0
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    for (table_name,) in tables:
        try:
            total += conn.execute(
                f"SELECT COUNT(*) FROM [{table_name}]"
            ).fetchone()[0]
        except Exception:
            pass
    return total


def _vacuum_single_db(db_path: str, dry_run: bool = False) -> Optional[Dict[str, Any]]:
    """VACUUM a single database file. Returns result dict or None on error.

    Order: checkpoint(TRUNCATE) → VACUUM → verify.
    Checkpoint before VACUUM maximises page return in WAL mode.
    """
    if not os.path.exists(db_path):
        return None

    db_name = os.path.basename(db_path)
    size_before = _db_file_size(db_path)

    if dry_run:
        return {
            "db": db_name,
            "path": db_path,
            "bytes_before": size_before,
            "bytes_after": size_before,
            "bytes_reclaimed": 0,
            "dry_run": True,
        }

    try:
        conn = sqlite3.connect(db_path)

        # Pre-measure
        page_count_before, page_size = _get_page_info(conn)
        user_version_before = _get_user_version(conn)
        rows_before = _count_all_rows(conn)

        # Step 1: checkpoint(TRUNCATE) — flush WAL into main DB, truncate WAL
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        # Step 2: VACUUM — compact the main DB file
        conn.execute("VACUUM")

        # Post-measure
        page_count_after, _ = _get_page_info(conn)
        user_version_after = _get_user_version(conn)
        rows_after = _count_all_rows(conn)

        conn.close()

        size_after = _db_file_size(db_path)

        # Integrity pin: user_version and row count must be preserved
        if user_version_after != user_version_before:
            logger.error(
                f"maintain: VACUUM changed user_version of {db_name}: "
                f"{user_version_before} → {user_version_after}"
            )
        if rows_after != rows_before:
            logger.error(
                f"maintain: VACUUM changed row count of {db_name}: "
                f"{rows_before} → {rows_after}"
            )

        bytes_reclaimed = size_before - size_after

        return {
            "db": db_name,
            "path": db_path,
            "bytes_before": size_before,
            "bytes_after": size_after,
            "bytes_reclaimed": bytes_reclaimed,
            "pages_before": page_count_before,
            "pages_after": page_count_after,
            "page_size": page_size,
            "user_version": user_version_after,
            "rows": rows_after,
            "dry_run": False,
        }

    except Exception as exc:
        logger.error(f"maintain: VACUUM failed for {db_path}: {exc}")
        return {
            "db": db_name,
            "path": db_path,
            "bytes_before": size_before,
            "bytes_after": _db_file_size(db_path),
            "bytes_reclaimed": 0,
            "error": str(exc),
            "dry_run": False,
        }


def vacuum(workspace: str, dry_run: bool = False) -> int:
    """Execute VACUUM on registry.sqlite and all queue_state/*.sqlite.

    Returns exit code (0 = success, non-zero = failure).
    """
    started_ts = time.time()

    # Validate workspace
    if not os.path.isdir(workspace):
        msg = f"maintain: workspace '{workspace}' does not exist or is not a directory"
        logger.error(msg)
        print(f"REFUSE: {msg}", file=sys.stderr)
        return 1

    # Discover targets
    targets: List[str] = []
    skipped = 0

    # 1. Registry
    registry_path = os.path.join(workspace, "registry.sqlite")
    if os.path.exists(registry_path):
        targets.append(registry_path)
    else:
        logger.info("maintain: registry.sqlite not found, skipping")
        skipped += 1

    # 2. Queue state databases (glob for *.sqlite)
    qs_dir = os.path.join(workspace, "queue_state")
    if os.path.isdir(qs_dir):
        queue_dbs = sorted(glob.glob(os.path.join(qs_dir, "*.sqlite")))
        if queue_dbs:
            targets.extend(queue_dbs)
        else:
            logger.info("maintain: no *.sqlite files in queue_state/, skipping")
            skipped += 1
    else:
        logger.info("maintain: queue_state/ directory not found, skipping")
        skipped += 1

    # Fail-CLOSED: if no targets exist at all, refuse (ambiguity)
    if not targets:
        msg = "maintain: no vacuum targets found (no registry.sqlite, no queue_state/*.sqlite)"
        logger.error(msg)
        print(f"REFUSE: {msg}", file=sys.stderr)
        return 1

    # Acquire lock (symmetry, holder="maintain")
    try:
        acquire_lock(workspace, holder="maintain")
    except Exception as exc:
        msg = f"maintain: failed to acquire lock: {exc}"
        logger.error(msg)
        print(f"REFUSE: {exc}", file=sys.stderr)
        return 1

    try:
        # WAL freshness check (reuse purge helper)
        if _check_wal_freshness(workspace):
            msg = "maintain: WAL files are too fresh, refusing to proceed"
            logger.error(msg)
            print(
                "REFUSE: WAL files are too fresh (< 120s), a pipeline may be running",
                file=sys.stderr,
            )
            return 1

        # Execute VACUUM on each target
        results: List[Dict[str, Any]] = []
        for target in targets:
            result = _vacuum_single_db(target, dry_run=dry_run)
            if result is not None:
                results.append(result)
                if not dry_run:
                    logger.info(
                        f"maintain: {result['db']} — "
                        f"before={result['bytes_before']} "
                        f"after={result['bytes_after']} "
                        f"reclaimed={result['bytes_reclaimed']}"
                    )
                else:
                    logger.info(
                        f"maintain [dry-run]: {result['db']} — "
                        f"size={result['bytes_before']}"
                    )

        duration_s = time.time() - started_ts

        # Build summary (masked)
        total_reclaimed = sum(r.get("bytes_reclaimed", 0) for r in results)
        summary_lines = [
            f"\nVacuum complete: targets={len(results)}, skipped={skipped}, "
            f"dry_run={dry_run}, duration={duration_s:.2f}s"
        ]
        for r in results:
            if r.get("dry_run"):
                summary_lines.append(
                    f"  {r['db']}: size={r['bytes_before']}"
                )
            else:
                summary_lines.append(
                    f"  {r['db']}: before={r['bytes_before']} "
                    f"after={r['bytes_after']} "
                    f"reclaimed={r['bytes_reclaimed']}"
                )
        if total_reclaimed > 0:
            summary_lines.append(f"  total reclaimed: {total_reclaimed} bytes")

        summary_text = "\n".join(summary_lines)
        summary_redacted = redact_api_keys_in_text(summary_text)
        print(summary_redacted)

        return 0

    finally:
        release_lock(workspace)
