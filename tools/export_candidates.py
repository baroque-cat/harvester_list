#!/usr/bin/env python3

"""
Read-only export of deep-scan candidates, one record per repository.

The export is the stable handoff contract for downstream deep-scan consumers
(clone + TruffleHog extension project).  It is deterministic and versioned:

* ordering: ``priority`` desc, then ``repo_pushed_at`` desc (NULLs last), then
  ``owner``/``repo``;
* ``schema_version`` is frozen per field-set; additions require a bump;
* the registry is opened read-only (``mode=ro``) and never written;
* no network access is performed.

Usage:
    python -m tools.export_candidates --workspace PATH [--format ndjson|csv]
        [--min-priority N] [--limit N] [--sample-links N]

``--workspace`` is required (design D4): an export is a handoff artifact, so
the target registry must be named explicitly rather than defaulted.
"""

import argparse
import csv
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

from storage.priority import best_status, open_readonly, status_counts_for_repos

SCHEMA_VERSION = "1.0"
REGISTRY_FILENAME = "registry.sqlite"

# Frozen field set (additions require a schema_version bump).
FIELD_NAMES = (
    "schema_version",
    "owner",
    "repo",
    "priority",
    "best_status",
    "status_counts",
    "repo_pushed_at",
    "repo_size_kb",
    "links_total",
    "sample_links",
)


def _repo_aggregates(conn) -> Dict[Tuple[str, str], Dict[str, Any]]:
    rows = conn.execute(
        "SELECT owner, repo, MAX(priority), MAX(repo_pushed_at), MAX(repo_size_kb), COUNT(*) "
        "FROM links "
        "WHERE owner IS NOT NULL AND owner != '' AND repo IS NOT NULL AND repo != '' "
        "GROUP BY owner, repo"
    )
    aggregates: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for owner, repo, priority, pushed_at, size_kb, links_total in rows:
        aggregates[(owner, repo)] = {
            "priority": float(priority or 0.0),
            "repo_pushed_at": pushed_at,
            "repo_size_kb": size_kb,
            "links_total": int(links_total or 0),
        }
    return aggregates


def _sample_links(conn, sample_links: int) -> Dict[Tuple[str, str], List[str]]:
    if sample_links <= 0:
        return {}
    samples: Dict[Tuple[str, str], List[str]] = {}
    rows = conn.execute(
        "SELECT owner, repo, url FROM links "
        "WHERE owner IS NOT NULL AND owner != '' AND repo IS NOT NULL AND repo != '' "
        "ORDER BY owner, repo, url_hash"
    )
    for owner, repo, url in rows:
        bucket = samples.setdefault((owner, repo), [])
        if len(bucket) < sample_links:
            bucket.append(url)
    return samples


def _record(owner: str, repo: str, stats: Dict[str, Any], counts: Dict[str, int], sample: List[str]) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "owner": owner,
        "repo": repo,
        "priority": stats["priority"],
        "best_status": best_status(counts),
        "status_counts": dict(counts or {}),
        "repo_pushed_at": stats["repo_pushed_at"],
        "repo_size_kb": stats["repo_size_kb"],
        "links_total": stats["links_total"],
        "sample_links": list(sample or []),
    }


def _sort_key(record: Dict[str, Any]):
    pushed_at = record["repo_pushed_at"]
    return (
        -record["priority"],
        1 if pushed_at is None else 0,
        -(pushed_at or 0.0),
        record["owner"],
        record["repo"],
    )


def collect_candidates(
    registry_path: str,
    min_priority: Optional[float] = None,
    limit: Optional[int] = None,
    sample_links: int = 5,
) -> List[Dict[str, Any]]:
    """Aggregate, filter and order candidate records (read-only)."""
    conn = open_readonly(registry_path)
    try:
        aggregates = _repo_aggregates(conn)
        counts_by_repo = status_counts_for_repos(conn)
        samples = _sample_links(conn, int(sample_links))
    finally:
        conn.close()

    records = [
        _record(owner, repo, stats, counts_by_repo.get((owner, repo), {}), samples.get((owner, repo), []))
        for (owner, repo), stats in aggregates.items()
    ]

    if min_priority is not None:
        records = [record for record in records if record["priority"] >= float(min_priority)]

    records.sort(key=_sort_key)

    if limit is not None:
        records = records[: max(0, int(limit))]

    return records


def _write_ndjson(records: List[Dict[str, Any]], out) -> int:
    for record in records:
        out.write(json.dumps(record, ensure_ascii=False) + "\n")
    return len(records)


def _write_csv(records: List[Dict[str, Any]], out) -> int:
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(FIELD_NAMES)
    for record in records:
        writer.writerow(
            [
                record["schema_version"],
                record["owner"],
                record["repo"],
                record["priority"],
                record["best_status"],
                json.dumps(record["status_counts"], sort_keys=True, separators=(",", ":")),
                "" if record["repo_pushed_at"] is None else record["repo_pushed_at"],
                "" if record["repo_size_kb"] is None else record["repo_size_kb"],
                record["links_total"],
                json.dumps(record["sample_links"], separators=(",", ":")),
            ]
        )
    return len(records)


def export_candidates(
    workspace: str,
    registry_path: Optional[str] = None,
    fmt: str = "ndjson",
    min_priority: Optional[float] = None,
    limit: Optional[int] = None,
    sample_links: int = 5,
    out=None,
) -> int:
    """Export candidates for ``workspace`` and return the record count."""
    target = registry_path or os.path.join(os.path.abspath(str(workspace)), REGISTRY_FILENAME)
    if fmt not in ("ndjson", "csv"):
        raise ValueError(f"unsupported export format: {fmt!r}")

    records = collect_candidates(target, min_priority=min_priority, limit=limit, sample_links=sample_links)

    if out is None:
        out = sys.stdout

    if fmt == "csv":
        return _write_csv(records, out)
    return _write_ndjson(records, out)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Export deep-scan candidates from the link registry")
    parser.add_argument("--workspace", required=True, help="Workspace directory holding registry.sqlite")
    parser.add_argument("--registry", default="", help="Explicit registry database path")
    parser.add_argument("--format", default="ndjson", choices=("ndjson", "csv"), help="Output format")
    parser.add_argument("--csv", action="store_true", help="Shorthand for --format csv")
    parser.add_argument("--min-priority", type=float, default=None, help="Drop records below this priority")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of records")
    parser.add_argument("--sample-links", type=int, default=5, help="Sample link URLs per record")
    args = parser.parse_args(argv)

    fmt = "csv" if args.csv else args.format
    export_candidates(
        args.workspace,
        registry_path=args.registry or None,
        fmt=fmt,
        min_priority=args.min_priority,
        limit=args.limit,
        sample_links=args.sample_links,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
