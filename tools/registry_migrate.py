#!/usr/bin/env python3

"""
One-time, idempotent migration of existing workspace shards into the registry.

Populates the `links` table from ``providers/*/shards/links/*.ndjson`` and the
`keys` table from ``providers/*/shards/{material,valid,invalid,no_quota,wait_check}``
plus ``summary.json``.  Migration is conservative: historical links cannot
distinguish "seen" from "gathered", so migrated links get
``visit_status='discovered'`` and ``gathered_ts=NULL``.  Re-running never
creates duplicates and never regresses fresher in-place state.

Usage:
    python -m tools.registry_migrate --workspace ./data [--dry-run] [--registry PATH]
"""

import argparse
import datetime
import glob
import hashlib
import json
import os
import sqlite3
import sys
from typing import Any, Dict, Iterable, List, Optional, Tuple

from storage.registry import (
    REGISTRY_FILENAME,
    bootstrap_schema,
    canonical_url,
    parse_github_url,
)
from tools.logger import get_logger

logger = get_logger("tools")

_RESULT_TYPES = ("material", "valid", "invalid", "no_quota", "wait_check")
_SERVICE_FIELDS = ("key", "address", "endpoint", "model")

_MIGRATE_LINK_SQL = """
INSERT INTO links (url_hash, url, owner, repo, path, first_seen_ts, last_seen_ts,
                   gathered_ts, visit_status, provider)
VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'discovered', ?)
ON CONFLICT(url_hash) DO UPDATE SET
  first_seen_ts = MIN(COALESCE(links.first_seen_ts, excluded.first_seen_ts), excluded.first_seen_ts),
  last_seen_ts = MAX(COALESCE(links.last_seen_ts, excluded.last_seen_ts), excluded.last_seen_ts),
  provider = COALESCE(links.provider, excluded.provider)
"""

_MIGRATE_KEY_SQL = """
INSERT INTO keys (key_hash, provider, key_ref_masked, address, endpoint, status,
                  first_seen_ts, last_recheck_ts, source_url_hash)
VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)
ON CONFLICT(key_hash) DO UPDATE SET
  key_ref_masked = COALESCE(excluded.key_ref_masked, keys.key_ref_masked),
  address = COALESCE(excluded.address, keys.address),
  endpoint = COALESCE(excluded.endpoint, keys.endpoint),
  status = CASE
      WHEN keys.status IS NOT NULL AND keys.status != '' THEN keys.status
      ELSE excluded.status
  END,
  first_seen_ts = COALESCE(keys.first_seen_ts, excluded.first_seen_ts)
"""


def _to_epoch(value: Any) -> Optional[float]:
    """Convert an ISO timestamp (or numeric) to epoch seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


def _index_times(shard_path: str) -> Tuple[Optional[float], Optional[float]]:
    """Read ``(first_ts, last_ts)`` from a shard's sidecar index."""
    index_path = os.path.splitext(shard_path)[0] + ".index.json"
    try:
        with open(index_path, encoding="utf-8") as f:
            payload = json.load(f)
        return _to_epoch(payload.get("first_ts")), _to_epoch(payload.get("last_ts"))
    except Exception:
        return None, None


def _fallback_ts(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _extract_url(record: Any) -> str:
    """Pull a URL out of either a ``{"value": url}`` envelope or a raw line."""
    if isinstance(record, dict):
        value = record.get("value", "")
        return str(value) if value else ""
    return str(record) if record else ""


def _parse_service(record: Any) -> Dict[str, str]:
    """Normalise a shard record into a service dict."""
    if not isinstance(record, dict):
        return {"key": str(record)} if record else {}
    if "value" in record and not any(field in record for field in _SERVICE_FIELDS):
        text = str(record["value"])
        try:
            parsed = json.loads(text)
        except Exception:
            return {"key": text}
        if isinstance(parsed, dict):
            return {field: str(parsed.get(field, "") or "") for field in _SERVICE_FIELDS}
        return {"key": text}
    return {field: str(record.get(field, "") or "") for field in _SERVICE_FIELDS}


def _mask_key(key: str) -> str:
    text = key or ""
    if not text:
        return ""
    if len(text) <= 4:
        return "*" * len(text)
    if len(text) <= 8:
        return f"{text[:2]}...{text[-2:]}"
    return f"{text[:4]}...{text[-4:]}"


def _key_hash(provider: str, service: Dict[str, str]) -> str:
    payload = json.dumps(
        [
            provider,
            service.get("key", ""),
            service.get("address", ""),
            service.get("endpoint", ""),
        ],
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _provider_dirs(workspace: str) -> Iterable[Tuple[str, str]]:
    base = os.path.join(workspace, "providers")
    if not os.path.isdir(base):
        return []
    result = []
    for name in sorted(os.listdir(base)):
        directory = os.path.join(base, name)
        if os.path.isdir(directory):
            result.append((name, directory))
    return result


def _collect_links(workspace: str) -> Dict[str, Dict[str, Any]]:
    """Collect the newest/oldest observation per distinct canonical URL."""
    collected: Dict[str, Dict[str, Any]] = {}
    for provider, directory in _provider_dirs(workspace):
        for shard_path in sorted(glob.glob(os.path.join(directory, "shards", "links", "*.ndjson"))):
            first_ts, last_ts = _index_times(shard_path)
            if first_ts is None:
                first_ts = _fallback_ts(shard_path)
            if last_ts is None:
                last_ts = first_ts

            try:
                with open(shard_path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            record = line
                        url = _extract_url(record)
                        if not url:
                            continue
                        canon = canonical_url(url)
                        if not canon:
                            continue
                        digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()
                        existing = collected.get(digest)
                        if existing is None:
                            owner, repo, path = parse_github_url(canon)
                            collected[digest] = {
                                "url_hash": digest,
                                "url": canon,
                                "owner": owner,
                                "repo": repo,
                                "path": path,
                                "first": first_ts,
                                "last": last_ts,
                                "provider": provider,
                            }
                        else:
                            existing["first"] = min(existing["first"], first_ts)
                            existing["last"] = max(existing["last"], last_ts)
            except OSError as exc:
                logger.warning(f"[registry-migrate] cannot read {shard_path}: {exc}")
    return collected


def _collect_keys(workspace: str) -> Dict[str, Dict[str, Any]]:
    """Collect keys from result shards and provider summary files."""
    collected: Dict[str, Dict[str, Any]] = {}
    for provider, directory in _provider_dirs(workspace):
        for result_type in _RESULT_TYPES:
            for shard_path in sorted(glob.glob(os.path.join(directory, "shards", result_type, "*.ndjson"))):
                first_ts, _ = _index_times(shard_path)
                if first_ts is None:
                    first_ts = _fallback_ts(shard_path)
                try:
                    with open(shard_path, encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                record = json.loads(line)
                            except json.JSONDecodeError:
                                record = line
                            _add_key(collected, provider, result_type, _parse_service(record), first_ts)
                except OSError as exc:
                    logger.warning(f"[registry-migrate] cannot read {shard_path}: {exc}")

        _collect_summary_keys(directory, provider, collected)

    return collected


def _collect_summary_keys(directory: str, provider: str, collected: Dict[str, Dict[str, Any]]) -> None:
    path = os.path.join(directory, "summary.json")
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as exc:
        logger.warning(f"[registry-migrate] cannot read summary {path}: {exc}")
        return

    models = payload.get("models", {}) if isinstance(payload, dict) else {}
    if not isinstance(models, dict):
        return
    for key, info in models.items():
        ts = _to_epoch(info.get("timestamp")) if isinstance(info, dict) else None
        _add_key(collected, provider, "inspect", {"key": str(key)}, ts)


def _add_key(
    collected: Dict[str, Dict[str, Any]],
    provider: str,
    status: str,
    service: Dict[str, str],
    first_seen: Optional[float],
) -> None:
    key = service.get("key", "")
    address = service.get("address", "")
    endpoint = service.get("endpoint", "")
    if not (key or address or endpoint):
        return
    digest = _key_hash(provider, service)
    collected[digest] = {
        "key_hash": digest,
        "provider": provider,
        "key_ref_masked": _mask_key(key),
        "address": address,
        "endpoint": endpoint,
        "status": status,
        "first_seen": first_seen,
    }


def migrate_workspace(
    workspace: str,
    dry_run: bool = False,
    registry_path: Optional[str] = None,
) -> Dict[str, int]:
    """Migrate a workspace's shards into its registry. Returns row counts."""
    workspace = os.path.abspath(str(workspace))
    target = registry_path or os.path.join(workspace, REGISTRY_FILENAME)

    links = _collect_links(workspace)
    keys = _collect_keys(workspace)

    link_rows = [
        (
            item["url_hash"],
            item["url"],
            item["owner"],
            item["repo"],
            item["path"],
            item["first"],
            item["last"],
            item["provider"],
        )
        for item in links.values()
    ]
    key_rows = [
        (
            item["key_hash"],
            item["provider"],
            item["key_ref_masked"],
            item["address"],
            item["endpoint"],
            item["status"],
            item["first_seen"],
        )
        for item in keys.values()
    ]

    if not dry_run:
        os.makedirs(os.path.dirname(os.path.abspath(target)) or ".", exist_ok=True)
        conn = sqlite3.connect(target)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            bootstrap_schema(conn)
            if link_rows:
                conn.executemany(_MIGRATE_LINK_SQL, link_rows)
            if key_rows:
                conn.executemany(_MIGRATE_KEY_SQL, key_rows)
            conn.commit()
        finally:
            conn.close()

    logger.info(
        f"[registry-migrate] {'(dry-run) ' if dry_run else ''}"
        f"links={len(link_rows)} keys={len(key_rows)} target={target}"
    )
    return {"links": len(link_rows), "keys": len(key_rows), "providers": len(list(_provider_dirs(workspace)))}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate workspace shards into the link registry")
    parser.add_argument("--workspace", default="./data", help="Workspace directory (default: ./data)")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be imported without writing")
    parser.add_argument("--registry", default="", help="Explicit registry database path")
    args = parser.parse_args(argv)

    stats = migrate_workspace(args.workspace, dry_run=args.dry_run, registry_path=args.registry or None)
    print(f"links={stats['links']} keys={stats['keys']} providers={stats['providers']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
