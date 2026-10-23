#!/usr/bin/env python3

"""
Restore CLI implementation (slice 4.3, wave 4).

Restores registry tables from NDJSON dumps created by ``purge --backup-to``.
Minimum for registry: all tables that purge can back up (links, link_coverage,
file_meta, repos, wire_coverage, keys, key_sources).

Design constraints:
- Fail-CLOSED interlocks: lock symmetry (holder="restore"), wal-freshness, ambiguity refuse.
- Restore is NOT a merge tool: target tables must be empty (refuse if non-empty).
- Shards/queues/telemetry: NOT restored by this command — manual untar/byte-copy (documented).
- N-11: registry tables have no ``dedup_id`` column; queue restore = manual byte-copy.
- No receipt (canon does not require it); audit trail = log lines with counters.
- Secret values are NEVER printed (structural pin, redact_api_keys_in_text).

Traceability: 05_purge_cli.md §5.4 (restore canon), slice 4.3 wave 4.
"""

import json
import os
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from tools.logger import get_logger, redact_api_keys_in_text
from tools.purge import WAL_FRESHNESS_THRESHOLD_S, _check_wal_freshness
from tools.run_lock import acquire_lock, release_lock

logger = get_logger("tools")

# Registry tables that purge can back up (mirror of tools/purge.py)
RESTORE_TABLES = ["links", "link_coverage", "file_meta", "repos", "wire_coverage", "keys", "key_sources"]


def _validate_ndjson(path: str) -> Tuple[bool, List[Dict[str, Any]], List[str]]:
    """Validate and parse an NDJSON file. Returns (ok, records, errors).

    If any line is invalid JSON, the entire file is rejected (no partial insert).
    """
    records: List[Dict[str, Any]] = []
    errors: List[str] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    records.append(record)
                except json.JSONDecodeError as exc:
                    errors.append(f"line {lineno}: {exc}")
                    # Reject entire file on first bad line (no partial insert)
                    return False, [], errors
    except OSError as exc:
        errors.append(f"read error: {exc}")
        return False, [], errors
    return True, records, errors


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Check if a table exists in the database."""
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return cursor.fetchone() is not None


def _count_rows(conn: sqlite3.Connection, table: str) -> int:
    """Count rows in a table."""
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except Exception:
        return 0


def _get_table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    """Get column names for a table."""
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cursor.fetchall()]


def _insert_records(
    conn: sqlite3.Connection, table: str, records: List[Dict[str, Any]]
) -> int:
    """Insert records into a table by column names. Returns row count inserted."""
    if not records:
        return 0

    # Get actual columns of the table
    table_columns = set(_get_table_columns(conn, table))

    inserted = 0
    for record in records:
        # Filter to columns that exist in the table
        filtered = {k: v for k, v in record.items() if k in table_columns}
        if not filtered:
            continue
        columns = list(filtered.keys())
        placeholders = ", ".join(["?"] * len(columns))
        col_names = ", ".join(columns)
        values = [filtered[c] for c in columns]
        sql = f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})"
        conn.execute(sql, values)
        inserted += 1
    return inserted


def _ensure_registry(workspace: str) -> str:
    """Ensure registry.sqlite exists. If not, create it via bootstrap.

    Returns the database path.
    """
    db_path = os.path.join(workspace, "registry.sqlite")
    if os.path.exists(db_path):
        return db_path

    # Create via bootstrap: execute the DDL from storage/registry.py
    # We import the DDL string and execute it
    from storage.registry import _DDL, SCHEMA_VERSION

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_DDL)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    conn.close()
    return db_path


def restore(
    from_dir: str,
    workspace: str,
    config_path: Optional[str] = None,
    dry_run: bool = False,
) -> int:
    """Execute restore from NDJSON dumps.

    Returns exit code (0 = success, non-zero = failure).
    """
    started_ts = time.time()

    # Validate --from directory
    if not os.path.isdir(from_dir):
        msg = f"restore: --from directory '{from_dir}' does not exist or is not a directory"
        logger.error(msg)
        print(f"REFUSE: {msg}", file=sys.stderr)
        return 1

    # Validate workspace
    if not os.path.isdir(workspace):
        msg = f"restore: workspace '{workspace}' does not exist or is not a directory"
        logger.error(msg)
        print(f"REFUSE: {msg}", file=sys.stderr)
        return 1

    # Acquire lock (symmetry, holder="restore")
    try:
        acquire_lock(workspace, holder="restore")
    except Exception as exc:
        msg = f"restore: failed to acquire lock: {exc}"
        logger.error(msg)
        print(f"REFUSE: {exc}", file=sys.stderr)
        return 1

    try:
        # WAL freshness check (fallback layer, reuse purge constant)
        if _check_wal_freshness(workspace):
            msg = "restore: WAL files are too fresh, refusing to proceed"
            logger.error(msg)
            print("REFUSE: WAL files are too fresh (< 120s), a pipeline may be running", file=sys.stderr)
            return 1

        # Ensure registry exists (create if missing)
        db_path = _ensure_registry(workspace)

        # Open connection
        conn = sqlite3.connect(db_path)

        # Discover which NDJSON files exist in --from
        available_files: Dict[str, str] = {}
        for table in RESTORE_TABLES:
            ndjson_path = os.path.join(from_dir, f"{table}.ndjson")
            if os.path.isfile(ndjson_path):
                available_files[table] = ndjson_path

        if not available_files:
            msg = "restore: no NDJSON files found in --from directory"
            logger.error(msg)
            print(f"REFUSE: {msg}", file=sys.stderr)
            conn.close()
            return 1

        # Check the tables PRESENT IN THE BACKUP are empty (restore is NOT a
        # merge tool). Tables absent from the backup are left untouched, so a
        # single-profile purge can be undone without emptying unrelated tables
        # (slice 4.3 remediation; canon 05 §5.4 "minimum for registry").
        for table in available_files:
            if _table_exists(conn, table):
                count = _count_rows(conn, table)
                if count > 0:
                    msg = f"restore: table '{table}' is non-empty ({count} rows); restore is not a merge tool"
                    logger.error(msg)
                    print(f"REFUSE: {msg}", file=sys.stderr)
                    conn.close()
                    return 1

        # Validate ALL NDJSON files before any insert (fail-CLOSED: no partial insert)
        parsed_data: Dict[str, List[Dict[str, Any]]] = {}
        for table, ndjson_path in available_files.items():
            ok, records, errors = _validate_ndjson(ndjson_path)
            if not ok:
                msg = f"restore: corrupted NDJSON in {table}.ndjson: {errors[0] if errors else 'unknown error'}"
                logger.error(msg)
                # Redact any potential secret values in error messages
                msg_redacted = redact_api_keys_in_text(msg)
                print(f"REFUSE: {msg_redacted}", file=sys.stderr)
                conn.close()
                return 1
            parsed_data[table] = records

        if dry_run:
            # Dry-run: enumerate what would be restored
            print(f"\nRestore [dry-run] from: {from_dir}")
            total_rows = 0
            for table in RESTORE_TABLES:
                if table in parsed_data:
                    count = len(parsed_data[table])
                    print(f"  {table}: {count} rows would be restored")
                    total_rows += count
                else:
                    print(f"  {table}: skipped (no backup file)")
            print(f"Total: {total_rows} rows would be restored")
            conn.close()
            return 0

        # Insert records
        results: Dict[str, int] = {}
        skipped_tables: List[str] = []
        for table in RESTORE_TABLES:
            if table in parsed_data:
                records = parsed_data[table]
                inserted = _insert_records(conn, table, records)
                results[table] = inserted
                logger.info(f"restore: {table} — {inserted} rows restored")
            else:
                skipped_tables.append(table)
                logger.info(f"restore: {table} — skipped (no backup file)")

        conn.commit()
        conn.close()

        duration_s = time.time() - started_ts

        # Print summary (redacted)
        summary_lines = [f"\nRestore complete: duration={duration_s:.2f}s"]
        for table in RESTORE_TABLES:
            if table in results:
                summary_lines.append(f"  {table}: {results[table]} rows restored")
            else:
                summary_lines.append(f"  {table}: skipped")
        if skipped_tables:
            summary_lines.append(f"  skipped tables: {len(skipped_tables)}")

        summary_text = "\n".join(summary_lines)
        summary_redacted = redact_api_keys_in_text(summary_text)
        print(summary_redacted)

        return 0

    finally:
        release_lock(workspace)
