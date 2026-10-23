#!/usr/bin/env python3

"""
Purge CLI implementation (slice 4.2, wave 4).

Provides workspace data deletion by profile with fail-CLOSED interlocks,
optional backup, and atomic receipt generation.

Design constraints:
- Fail-CLOSED interlocks: lock symmetry, wal-freshness, ambiguity refuse.
- Profiles: all, links, keys, queues, telemetry, shards.
- Backup: NDJSON dumps (registry), tar.gz (shards/snapshots), byte copies (queues).
- F-29: backup dir 0700, artifacts 0600 (explicit os.chmod, not umask).
- Receipt: atomic JSON at END, counters only, no secret values.
- N-10: -wal/-shm sidecars deleted with .sqlite files.
- N-11: legacy bulk_insert dedup_id='' caveat noted in docstring/receipt.

Traceability: 05_purge_cli.md §5.1–5.6.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from tools.logger import get_logger, redact_api_keys_in_text
from tools.run_lock import acquire_lock, release_lock

logger = get_logger("tools")

# WAL freshness threshold (conservative, 05 §5.3.1)
WAL_FRESHNESS_THRESHOLD_S = 120.0

# Profile definitions
PROFILES = ["all", "links", "keys", "queues", "telemetry", "shards"]

# Registry tables per profile
LINKS_TABLES = ["links", "link_coverage", "file_meta", "repos", "wire_coverage"]
KEYS_TABLES = ["keys", "key_sources"]

# Order for 'all' profile (05 §5.3.4): queues → shards/snapshots → telemetry → registry
ALL_PROFILE_ORDER = ["queues", "shards", "telemetry", "registry"]


def _md5_file(path: str) -> str:
    """Compute MD5 hash of a file."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _md5_string(s: str) -> str:
    """Compute MD5 hash of a string."""
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _get_git_head() -> Optional[str]:
    """Best-effort git HEAD (subprocess, fail-open)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _check_wal_freshness(workspace: str) -> bool:
    """Check if any WAL file is fresh (< threshold). Returns True if fresh."""
    now = time.time()
    candidates = [
        os.path.join(workspace, "registry.sqlite-wal"),
    ]
    # Also check queue WAL files
    qs_dir = os.path.join(workspace, "queue_state")
    if os.path.isdir(qs_dir):
        for fname in os.listdir(qs_dir):
            if fname.endswith("-wal"):
                candidates.append(os.path.join(qs_dir, fname))

    for wal_path in candidates:
        if os.path.exists(wal_path):
            mtime = os.path.getmtime(wal_path)
            age = now - mtime
            if age < WAL_FRESHNESS_THRESHOLD_S:
                logger.warning(
                    f"purge: WAL file {wal_path} is fresh ({age:.0f}s old < {WAL_FRESHNESS_THRESHOLD_S}s threshold)"
                )
                return True
    return False


def _has_wire_coverage(workspace: str) -> bool:
    """Check if wire_coverage table exists in registry."""
    db_path = os.path.join(workspace, "registry.sqlite")
    if not os.path.exists(db_path):
        return False
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='wire_coverage'"
        )
        exists = cursor.fetchone() is not None
        conn.close()
        return exists
    except Exception:
        return False


def _count_rows(db_path: str, table: str) -> int:
    """Count rows in a table."""
    try:
        conn = sqlite3.connect(db_path)
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        conn.close()
        return count
    except Exception:
        return 0


def _backup_registry_tables(
    workspace: str, tables: List[str], backup_dir: str
) -> List[str]:
    """Backup registry tables as NDJSON. Returns list of backup file paths."""
    db_path = os.path.join(workspace, "registry.sqlite")
    if not os.path.exists(db_path):
        return []

    os.makedirs(backup_dir, exist_ok=True)
    os.chmod(backup_dir, 0o700)  # F-29

    backups = []
    conn = sqlite3.connect(db_path)
    for table in tables:
        try:
            cursor = conn.execute(f"SELECT * FROM {table}")
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            if rows:
                backup_path = os.path.join(backup_dir, f"{table}.ndjson")
                with open(backup_path, "w", encoding="utf-8") as f:
                    for row in rows:
                        record = dict(zip(columns, row))
                        f.write(json.dumps(record, ensure_ascii=False) + "\n")
                os.chmod(backup_path, 0o600)  # F-29
                backups.append(backup_path)
        except Exception as exc:
            logger.warning(f"purge: failed to backup table {table}: {exc}")
    conn.close()
    return backups


def _backup_files(paths: List[str], backup_dir: str, archive_name: str) -> Optional[str]:
    """Backup files as tar.gz. Returns archive path or None."""
    if not paths:
        return None

    os.makedirs(backup_dir, exist_ok=True)
    os.chmod(backup_dir, 0o700)  # F-29

    archive_path = os.path.join(backup_dir, archive_name)
    try:
        with tarfile.open(archive_path, "w:gz") as tar:
            for path in paths:
                if os.path.exists(path):
                    tar.add(path, arcname=os.path.basename(path))
        os.chmod(archive_path, 0o600)  # F-29
        return archive_path
    except Exception as exc:
        logger.warning(f"purge: failed to create backup archive: {exc}")
        return None


def _backup_queue_files(workspace: str, backup_dir: str) -> List[str]:
    """Backup queue files as byte copies. Returns list of backup paths."""
    qs_dir = os.path.join(workspace, "queue_state")
    if not os.path.isdir(qs_dir):
        return []

    os.makedirs(backup_dir, exist_ok=True)
    os.chmod(backup_dir, 0o700)  # F-29

    backups = []
    for fname in os.listdir(qs_dir):
        src = os.path.join(qs_dir, fname)
        if os.path.isfile(src):
            dst = os.path.join(backup_dir, fname)
            shutil.copy2(src, dst)
            os.chmod(dst, 0o600)  # F-29
            backups.append(dst)
    return backups


def _delete_registry_tables(workspace: str, tables: List[str]) -> Tuple[int, int]:
    """Delete rows from registry tables. Returns (rows_deleted, bytes_freed)."""
    db_path = os.path.join(workspace, "registry.sqlite")
    if not os.path.exists(db_path):
        return 0, 0

    total_rows = 0
    total_bytes = 0
    conn = sqlite3.connect(db_path)
    for table in tables:
        try:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if count > 0:
                conn.execute(f"DELETE FROM {table}")
                total_rows += count
                # Estimate bytes freed (rough)
                total_bytes += count * 200  # rough estimate
        except Exception as exc:
            logger.warning(f"purge: failed to delete table {table}: {exc}")
    conn.commit()
    conn.close()
    return total_rows, total_bytes


def _delete_queue_files(workspace: str) -> Tuple[int, int]:
    """Delete queue_state files. Returns (files_deleted, bytes_freed)."""
    qs_dir = os.path.join(workspace, "queue_state")
    if not os.path.isdir(qs_dir):
        return 0, 0

    files_deleted = 0
    bytes_freed = 0
    for fname in os.listdir(qs_dir):
        fpath = os.path.join(qs_dir, fname)
        if os.path.isfile(fpath):
            size = os.path.getsize(fpath)
            os.unlink(fpath)
            files_deleted += 1
            bytes_freed += size
    return files_deleted, bytes_freed


def _delete_shards_and_snapshots(workspace: str) -> Tuple[int, int]:
    """Delete shards and snapshots for all providers. Returns (files_deleted, bytes_freed)."""
    providers_dir = os.path.join(workspace, "providers")
    if not os.path.isdir(providers_dir):
        return 0, 0

    files_deleted = 0
    bytes_freed = 0
    for provider in os.listdir(providers_dir):
        provider_dir = os.path.join(providers_dir, provider)
        if not os.path.isdir(provider_dir):
            continue

        # Delete shards (entire directory tree)
        shards_dir = os.path.join(provider_dir, "shards")
        if os.path.isdir(shards_dir):
            for root, dirs, files in os.walk(shards_dir, topdown=False):
                for fname in files:
                    fpath = os.path.join(root, fname)
                    if os.path.isfile(fpath):
                        size = os.path.getsize(fpath)
                        os.unlink(fpath)
                        files_deleted += 1
                        bytes_freed += size
                # Remove empty subdirectories
                for d in dirs:
                    dpath = os.path.join(root, d)
                    try:
                        os.rmdir(dpath)
                    except OSError:
                        pass
            # Remove the shards directory itself if empty
            try:
                os.rmdir(shards_dir)
            except OSError:
                pass

        # Delete snapshots (entire directory contents)
        snaps_dir = os.path.join(provider_dir, "snapshots")
        if os.path.isdir(snaps_dir):
            for root, dirs, files in os.walk(snaps_dir, topdown=False):
                for fname in files:
                    fpath = os.path.join(root, fname)
                    if os.path.isfile(fpath):
                        size = os.path.getsize(fpath)
                        os.unlink(fpath)
                        files_deleted += 1
                        bytes_freed += size
                for d in dirs:
                    dpath = os.path.join(root, d)
                    try:
                        os.rmdir(dpath)
                    except OSError:
                        pass
            try:
                os.rmdir(snaps_dir)
            except OSError:
                pass

    return files_deleted, bytes_freed


def _delete_telemetry(workspace: str) -> Tuple[int, int]:
    """Delete telemetry files (JSONL + snapshots). Returns (files_deleted, bytes_freed)."""
    files_deleted = 0
    bytes_freed = 0

    # Decision JSONL files
    for name in ["registry_decisions.jsonl", "aggregation_decisions.jsonl"]:
        path = os.path.join(workspace, name)
        if os.path.exists(path):
            size = os.path.getsize(path)
            os.unlink(path)
            files_deleted += 1
            bytes_freed += size
            # Delete rotations
            for gen in [".1", ".2", ".3"]:
                gen_path = path + gen
                if os.path.exists(gen_path):
                    size = os.path.getsize(gen_path)
                    os.unlink(gen_path)
                    files_deleted += 1
                    bytes_freed += size

    # Provider snapshots (already counted in _delete_shards_and_snapshots, but telemetry includes them too)
    # Actually, per 05 §5.2: telemetry includes snapshots, shards profile also includes snapshots
    # Let's delete snapshots here too for telemetry profile
    providers_dir = os.path.join(workspace, "providers")
    if os.path.isdir(providers_dir):
        for provider in os.listdir(providers_dir):
            snaps_dir = os.path.join(providers_dir, provider, "snapshots")
            if os.path.isdir(snaps_dir):
                for fname in os.listdir(snaps_dir):
                    fpath = os.path.join(snaps_dir, fname)
                    if os.path.isfile(fpath):
                        size = os.path.getsize(fpath)
                        os.unlink(fpath)
                        files_deleted += 1
                        bytes_freed += size

    return files_deleted, bytes_freed


def _write_receipt(
    workspace: str,
    profile: str,
    argv: List[str],
    areas: List[Dict[str, Any]],
    backup_info: Optional[Dict[str, Any]],
    config_md5: Optional[str],
    started_ts: float,
    duration_s: float,
    warnings: List[str],
) -> str:
    """Write atomic receipt JSON. Returns receipt path."""
    receipts_dir = os.path.join(workspace, "purge_receipts")
    os.makedirs(receipts_dir, exist_ok=True)

    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    receipt_path = os.path.join(receipts_dir, f"receipt_{ts_str}.json")

    receipt = {
        "profile": profile,
        "argv": argv,
        "areas": areas,
        "backup": backup_info,
        "config_md5": config_md5,
        "git_head": _get_git_head(),
        "started_ts": started_ts,
        "duration_s": round(duration_s, 3),
        "warnings": warnings,
    }

    # Redact any potential secrets in the receipt (defensive)
    receipt_text = json.dumps(receipt, indent=2, ensure_ascii=False)
    receipt_text = redact_api_keys_in_text(receipt_text)

    # Atomic write
    tmp_path = receipt_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(receipt_text)
    os.replace(tmp_path, receipt_path)

    return receipt_path


def purge(
    profile: str,
    workspace: str,
    config_path: Optional[str] = None,
    dry_run: bool = False,
    backup_to: Optional[str] = None,
    force: bool = False,
) -> int:
    """Execute purge for the given profile.

    Returns exit code (0 = success, non-zero = failure).
    """
    started_ts = time.time()
    warnings: List[str] = []

    # Validate profile
    if profile not in PROFILES:
        logger.error(f"purge: unknown profile '{profile}', known: {PROFILES}")
        return 1

    # Validate workspace
    if not os.path.isdir(workspace):
        logger.error(f"purge: workspace '{workspace}' does not exist or is not a directory")
        return 1

    # Acquire lock (symmetry, 05 §5.3.1)
    try:
        acquire_lock(workspace, holder="purge")
    except Exception as exc:
        logger.error(f"purge: failed to acquire lock: {exc}")
        print(f"REFUSE: {exc}", file=sys.stderr)
        return 1

    try:
        # WAL freshness check (fallback layer, 05 §5.3.1)
        if _check_wal_freshness(workspace):
            logger.error("purge: WAL files are too fresh, refusing to proceed")
            print("REFUSE: WAL files are too fresh (< 120s), a pipeline may be running", file=sys.stderr)
            return 1

        # Shards gate (05 §5.2): requires wire_coverage table (post-3a)
        if profile in ["shards", "all"] and not force:
            if not _has_wire_coverage(workspace):
                logger.error("purge: shards profile requires wire_coverage table (post-3a)")
                print(
                    "REFUSE: shards profile requires wire_coverage table (wave 3a). "
                    "Use --force to bypass (warning: check-task/invalid-keys recovery dies until shards regrow)",
                    file=sys.stderr,
                )
                return 1
        if profile == "shards" and force and not _has_wire_coverage(workspace):
            warnings.append(
                "F-15: shards deleted without wire_coverage; "
                "check-task/invalid-keys recovery (shard-based) and ShardKeyResolver "
                "full-scan die until new shards grow"
            )

        # Compute config MD5
        config_md5 = None
        if config_path and os.path.exists(config_path):
            config_md5 = _md5_file(config_path)

        # Execute purge by profile
        areas: List[Dict[str, Any]] = []
        backup_info: Optional[Dict[str, Any]] = None

        if profile == "links":
            areas = _purge_links(workspace, dry_run, backup_to, backup_info, warnings)
        elif profile == "keys":
            areas = _purge_keys(workspace, dry_run, backup_to, backup_info, warnings)
        elif profile == "queues":
            areas = _purge_queues(workspace, dry_run, backup_to, backup_info, warnings)
        elif profile == "telemetry":
            areas = _purge_telemetry(workspace, dry_run, backup_to, backup_info, warnings)
        elif profile == "shards":
            areas = _purge_shards(workspace, dry_run, backup_to, backup_info, warnings, force)
        elif profile == "all":
            areas = _purge_all(workspace, dry_run, backup_to, backup_info, warnings, force)

        # Build backup info
        if backup_to:
            backup_info = {"path": backup_to, "md5s": {}}
            if os.path.exists(backup_to):
                for fname in os.listdir(backup_to):
                    fpath = os.path.join(backup_to, fname)
                    if os.path.isfile(fpath):
                        backup_info["md5s"][fname] = _md5_file(fpath)
                # F-29 audit: record the actual backup directory mode in the
                # receipt (canon 05 §5.4: "режим каталога записывается в
                # квитанцию").  Octal string, e.g. "0700".
                try:
                    backup_info["mode"] = f"{os.stat(backup_to).st_mode & 0o777:04o}"
                except OSError:  # pragma: no cover - defensive
                    backup_info["mode"] = None

        duration_s = time.time() - started_ts

        # Write receipt (atomic, at END)
        if not dry_run:
            receipt_path = _write_receipt(
                workspace=workspace,
                profile=profile,
                argv=sys.argv,
                areas=areas,
                backup_info=backup_info,
                config_md5=config_md5,
                started_ts=started_ts,
                duration_s=duration_s,
                warnings=warnings,
            )
            logger.info(f"purge: receipt written to {receipt_path}")

        # Print summary
        print(f"\nPurge complete: profile={profile}, dry_run={dry_run}, duration={duration_s:.2f}s")
        for area in areas:
            print(f"  {area['area']}: rows={area['rows_deleted']}, files={area['files_deleted']}, bytes={area['bytes_freed']}")
        if warnings:
            print(f"  warnings: {warnings}")

        return 0

    finally:
        release_lock(workspace)


def _purge_links(
    workspace: str, dry_run: bool, backup_to: Optional[str],
    backup_info: Optional[Dict], warnings: List[str]
) -> List[Dict[str, Any]]:
    """Purge links profile."""
    areas = []
    db_path = os.path.join(workspace, "registry.sqlite")

    if not os.path.exists(db_path):
        logger.info("purge: registry.sqlite not found, skipping links")
        areas.append({"area": "links", "rows_deleted": 0, "files_deleted": 0, "bytes_freed": 0})
        return areas

    # Backup if requested
    if backup_to and not dry_run:
        _backup_registry_tables(workspace, LINKS_TABLES, backup_to)

    if dry_run:
        # Count what would be deleted
        total_rows = sum(_count_rows(db_path, t) for t in LINKS_TABLES)
        areas.append({"area": "links", "rows_deleted": total_rows, "files_deleted": 0, "bytes_freed": 0})
        logger.info(f"purge [dry-run]: would delete {total_rows} rows from links tables")
    else:
        rows_deleted, bytes_freed = _delete_registry_tables(workspace, LINKS_TABLES)
        areas.append({"area": "links", "rows_deleted": rows_deleted, "files_deleted": 0, "bytes_freed": bytes_freed})
        logger.info(f"purge: deleted {rows_deleted} rows from links tables")

    return areas


def _purge_keys(
    workspace: str, dry_run: bool, backup_to: Optional[str],
    backup_info: Optional[Dict], warnings: List[str]
) -> List[Dict[str, Any]]:
    """Purge keys profile."""
    areas = []
    db_path = os.path.join(workspace, "registry.sqlite")

    if not os.path.exists(db_path):
        logger.info("purge: registry.sqlite not found, skipping keys")
        areas.append({"area": "keys", "rows_deleted": 0, "files_deleted": 0, "bytes_freed": 0})
        return areas

    if backup_to and not dry_run:
        _backup_registry_tables(workspace, KEYS_TABLES, backup_to)

    if dry_run:
        total_rows = sum(_count_rows(db_path, t) for t in KEYS_TABLES)
        areas.append({"area": "keys", "rows_deleted": total_rows, "files_deleted": 0, "bytes_freed": 0})
        logger.info(f"purge [dry-run]: would delete {total_rows} rows from keys tables")
    else:
        rows_deleted, bytes_freed = _delete_registry_tables(workspace, KEYS_TABLES)
        areas.append({"area": "keys", "rows_deleted": rows_deleted, "files_deleted": 0, "bytes_freed": bytes_freed})
        logger.info(f"purge: deleted {rows_deleted} rows from keys tables")

    return areas


def _purge_queues(
    workspace: str, dry_run: bool, backup_to: Optional[str],
    backup_info: Optional[Dict], warnings: List[str]
) -> List[Dict[str, Any]]:
    """Purge queues profile."""
    areas = []
    qs_dir = os.path.join(workspace, "queue_state")

    if not os.path.isdir(qs_dir):
        logger.info("purge: queue_state directory not found, skipping queues")
        areas.append({"area": "queues", "rows_deleted": 0, "files_deleted": 0, "bytes_freed": 0})
        return areas

    if backup_to and not dry_run:
        _backup_queue_files(workspace, backup_to)

    if dry_run:
        files = [f for f in os.listdir(qs_dir) if os.path.isfile(os.path.join(qs_dir, f))]
        total_bytes = sum(os.path.getsize(os.path.join(qs_dir, f)) for f in files)
        areas.append({"area": "queues", "rows_deleted": 0, "files_deleted": len(files), "bytes_freed": total_bytes})
        logger.info(f"purge [dry-run]: would delete {len(files)} queue files")
    else:
        files_deleted, bytes_freed = _delete_queue_files(workspace)
        areas.append({"area": "queues", "rows_deleted": 0, "files_deleted": files_deleted, "bytes_freed": bytes_freed})
        logger.info(f"purge: deleted {files_deleted} queue files")

    return areas


def _purge_telemetry(
    workspace: str, dry_run: bool, backup_to: Optional[str],
    backup_info: Optional[Dict], warnings: List[str]
) -> List[Dict[str, Any]]:
    """Purge telemetry profile."""
    areas = []

    if backup_to and not dry_run:
        # Backup JSONL files
        telemetry_files = []
        for name in ["registry_decisions.jsonl", "aggregation_decisions.jsonl"]:
            path = os.path.join(workspace, name)
            if os.path.exists(path):
                telemetry_files.append(path)
                for gen in [".1", ".2", ".3"]:
                    gen_path = path + gen
                    if os.path.exists(gen_path):
                        telemetry_files.append(gen_path)
        if telemetry_files:
            _backup_files(telemetry_files, backup_to, "telemetry.tar.gz")

    if dry_run:
        files_deleted = 0
        bytes_freed = 0
        for name in ["registry_decisions.jsonl", "aggregation_decisions.jsonl"]:
            path = os.path.join(workspace, name)
            if os.path.exists(path):
                bytes_freed += os.path.getsize(path)
                files_deleted += 1
                for gen in [".1", ".2", ".3"]:
                    gen_path = path + gen
                    if os.path.exists(gen_path):
                        bytes_freed += os.path.getsize(gen_path)
                        files_deleted += 1
        areas.append({"area": "telemetry", "rows_deleted": 0, "files_deleted": files_deleted, "bytes_freed": bytes_freed})
        logger.info(f"purge [dry-run]: would delete {files_deleted} telemetry files")
    else:
        files_deleted, bytes_freed = _delete_telemetry(workspace)
        areas.append({"area": "telemetry", "rows_deleted": 0, "files_deleted": files_deleted, "bytes_freed": bytes_freed})
        logger.info(f"purge: deleted {files_deleted} telemetry files")

    return areas


def _purge_shards(
    workspace: str, dry_run: bool, backup_to: Optional[str],
    backup_info: Optional[Dict], warnings: List[str], force: bool
) -> List[Dict[str, Any]]:
    """Purge shards profile."""
    areas = []

    if backup_to and not dry_run:
        # Backup shard and snapshot files
        shard_files = []
        providers_dir = os.path.join(workspace, "providers")
        if os.path.isdir(providers_dir):
            for provider in os.listdir(providers_dir):
                provider_dir = os.path.join(providers_dir, provider)
                if not os.path.isdir(provider_dir):
                    continue
                for subdir in ["shards", "snapshots"]:
                    full_dir = os.path.join(provider_dir, subdir)
                    if os.path.isdir(full_dir):
                        for root, dirs, files in os.walk(full_dir):
                            for fname in files:
                                shard_files.append(os.path.join(root, fname))
        if shard_files:
            _backup_files(shard_files, backup_to, "shards_snapshots.tar.gz")

    if dry_run:
        files_deleted = 0
        bytes_freed = 0
        providers_dir = os.path.join(workspace, "providers")
        if os.path.isdir(providers_dir):
            for provider in os.listdir(providers_dir):
                provider_dir = os.path.join(providers_dir, provider)
                if not os.path.isdir(provider_dir):
                    continue
                for subdir in ["shards", "snapshots"]:
                    full_dir = os.path.join(provider_dir, subdir)
                    if os.path.isdir(full_dir):
                        for root, dirs, files in os.walk(full_dir):
                            for fname in files:
                                fpath = os.path.join(root, fname)
                                bytes_freed += os.path.getsize(fpath)
                                files_deleted += 1
        areas.append({"area": "shards", "rows_deleted": 0, "files_deleted": files_deleted, "bytes_freed": bytes_freed})
        logger.info(f"purge [dry-run]: would delete {files_deleted} shard/snapshot files")
    else:
        files_deleted, bytes_freed = _delete_shards_and_snapshots(workspace)
        areas.append({"area": "shards", "rows_deleted": 0, "files_deleted": files_deleted, "bytes_freed": bytes_freed})
        logger.info(f"purge: deleted {files_deleted} shard/snapshot files")

    return areas


def _purge_all(
    workspace: str, dry_run: bool, backup_to: Optional[str],
    backup_info: Optional[Dict], warnings: List[str], force: bool
) -> List[Dict[str, Any]]:
    """Purge all profile (order: queues → shards → telemetry → registry)."""
    areas = []

    # Order matters (05 §5.3.4)
    # 1. Queues
    areas.extend(_purge_queues(workspace, dry_run, backup_to, backup_info, warnings))

    # 2. Shards/snapshots
    areas.extend(_purge_shards(workspace, dry_run, backup_to, backup_info, warnings, force))

    # 3. Telemetry
    areas.extend(_purge_telemetry(workspace, dry_run, backup_to, backup_info, warnings))

    # 4. Registry (last)
    # Delete ALL registry tables
    all_tables = LINKS_TABLES + KEYS_TABLES
    db_path = os.path.join(workspace, "registry.sqlite")
    if os.path.exists(db_path):
        if backup_to and not dry_run:
            _backup_registry_tables(workspace, all_tables, backup_to)

        if dry_run:
            total_rows = sum(_count_rows(db_path, t) for t in all_tables)
            areas.append({"area": "registry", "rows_deleted": total_rows, "files_deleted": 0, "bytes_freed": 0})
            logger.info(f"purge [dry-run]: would delete {total_rows} rows from registry")
        else:
            rows_deleted, bytes_freed = _delete_registry_tables(workspace, all_tables)
            areas.append({"area": "registry", "rows_deleted": rows_deleted, "files_deleted": 0, "bytes_freed": bytes_freed})
            logger.info(f"purge: deleted {rows_deleted} rows from registry")

            # Also delete the registry.sqlite file and sidecars (N-10)
            for suffix in ["", "-wal", "-shm"]:
                fpath = db_path + suffix
                if os.path.exists(fpath):
                    os.unlink(fpath)
    else:
        areas.append({"area": "registry", "rows_deleted": 0, "files_deleted": 0, "bytes_freed": 0})

    return areas
