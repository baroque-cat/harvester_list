"""Candidate export tests for add-target-prioritization.

Traceability: target-prioritization-S7..S9.

Harness: a seeded tmp workspace (links + keys, then an explicit sweep to
populate ``links.priority``) plus dummy shard/snapshot files hashed before and
after.  The tool is exercised both as a library function and as a subprocess
CLI.  No network: S9 installs a socket guard that fails on any connection.
"""

import csv
import glob
import hashlib
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys

import pytest

from storage.priority import PriorityWeights, sweep
from storage.registry import url_hash
from tools.export_candidates import SCHEMA_VERSION, export_candidates

DAY = 86400.0
NOW = 1_700_000_000.0
WEIGHTS = PriorityWeights()
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQUIRED_FIELDS = {
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
}


def _registry_file(workspace: str) -> str:
    return os.path.join(str(workspace), "registry.sqlite")


def _seed(workspace, fast_registry):
    """Seed three repos with distinct evidence and persist their scores."""
    registry = fast_registry(workspace)
    try:
        # alpha: valid key, fresh, small -> highest
        url_a = "https://github.com/acme/alpha/blob/main/a.py"
        registry.record_link(url_a, ts=NOW)
        registry.record_repo_link_metadata("acme", "alpha", pushed_at=NOW, size_kb=1000)
        registry.record_key(
            key_hash="key-alpha",
            provider="openai",
            key_ref_masked="sk-***",
            address="https://api.example.com",
            endpoint="https://api.example.com/v1",
            status="valid",
            source_url_hash=url_hash(url_a),
            ts=NOW,
        )

        # beta: soft (wait_check) key, middle age -> middle
        url_b = "https://github.com/acme/beta/blob/main/b.py"
        registry.record_link(url_b, ts=NOW)
        registry.record_repo_link_metadata("acme", "beta", pushed_at=NOW - 10 * DAY, size_kb=1000)
        registry.record_key(
            key_hash="key-beta",
            provider="openai",
            key_ref_masked="sk-***",
            address="https://api.example.com",
            endpoint="https://api.example.com/v1",
            status="wait_check",
            source_url_hash=url_hash(url_b),
            ts=NOW,
        )

        # gamma: no key, stale, oversized -> lowest
        url_g = "https://github.com/acme/gamma/blob/main/c.py"
        registry.record_link(url_g, ts=NOW)
        registry.record_repo_link_metadata("acme", "gamma", pushed_at=NOW - 365 * DAY, size_kb=1_000_000)

        registry.flush(10.0)
    finally:
        registry.stop()

    _rescore(workspace)


def _rescore(workspace):
    conn = sqlite3.connect(_registry_file(workspace))
    try:
        sweep(conn, WEIGHTS, now=NOW)
        conn.commit()
    finally:
        conn.close()


def _run_cli(workspace, *args):
    return subprocess.run(
        [sys.executable, "-m", "tools.export_candidates", "--workspace", workspace, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _records(text):
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# target-prioritization-S7
# ---------------------------------------------------------------------------
def test_s7_ndjson_export_is_ordered_and_complete(workspace, fast_registry):
    _seed(workspace, fast_registry)

    buffer = io.StringIO()
    count = export_candidates(workspace, out=buffer)
    records = _records(buffer.getvalue())

    assert count == len(records) == 3
    for record in records:
        assert REQUIRED_FIELDS <= set(record)
        assert record["schema_version"] == SCHEMA_VERSION

    priorities = [record["priority"] for record in records]
    assert priorities == sorted(priorities, reverse=True)
    assert [record["repo"] for record in records] == ["alpha", "beta", "gamma"]
    assert records[0]["best_status"] == "valid"
    assert records[1]["best_status"] == "wait_check"
    assert records[2]["best_status"] == "none"
    assert records[0]["status_counts"] == {"valid": 1}
    assert records[0]["sample_links"] and len(records[0]["sample_links"]) == 1

    # --limit honors the deterministic top of the ranking.
    limited = io.StringIO()
    assert export_candidates(workspace, limit=1, out=limited) == 1
    assert _records(limited.getvalue())[0]["repo"] == "alpha"

    # --min-priority drops everything below the cutoff.
    cutoff = records[1]["priority"]
    filtered = io.StringIO()
    assert export_candidates(workspace, min_priority=cutoff, out=filtered) == 2
    assert all(record["priority"] >= cutoff for record in _records(filtered.getvalue()))

    # Subprocess CLI produces the same ranking.
    proc = _run_cli(workspace)
    assert proc.returncode == 0, proc.stderr
    assert [record["repo"] for record in _records(proc.stdout)] == ["alpha", "beta", "gamma"]


# ---------------------------------------------------------------------------
# target-prioritization-S8
# ---------------------------------------------------------------------------
def test_s8_csv_carries_equivalent_content(workspace, fast_registry):
    _seed(workspace, fast_registry)

    ndjson = io.StringIO()
    export_candidates(workspace, out=ndjson)
    expected = _records(ndjson.getvalue())

    csv_buffer = io.StringIO()
    export_candidates(workspace, fmt="csv", out=csv_buffer)
    rows = list(csv.DictReader(io.StringIO(csv_buffer.getvalue())))

    assert len(rows) == len(expected)
    for row, record in zip(rows, expected):
        assert row["owner"] == record["owner"]
        assert row["repo"] == record["repo"]
        assert float(row["priority"]) == pytest.approx(record["priority"])
        assert row["best_status"] == record["best_status"]
        assert json.loads(row["status_counts"]) == record["status_counts"]
        assert json.loads(row["sample_links"]) == record["sample_links"]
        assert int(row["links_total"]) == record["links_total"]

    proc = _run_cli(workspace, "--csv")
    assert proc.returncode == 0, proc.stderr
    cli_rows = list(csv.DictReader(io.StringIO(proc.stdout)))
    assert [row["repo"] for row in cli_rows] == [row["repo"] for row in rows]


# ---------------------------------------------------------------------------
# target-prioritization-S9
# ---------------------------------------------------------------------------
def test_s9_export_leaves_operational_artifacts_untouched(workspace, fast_registry, write_links_shard, write_result_shard, monkeypatch):
    _seed(workspace, fast_registry)

    write_links_shard(workspace, "openai", ["https://github.com/acme/alpha/blob/main/a.py"])
    write_result_shard(workspace, "openai", "valid", [{"key": "sk-***", "address": "", "endpoint": ""}])
    snapshot = os.path.join(workspace, "snapshot.bin")
    with open(snapshot, "wb") as f:
        f.write(b"snapshot-bytes-v1")

    watched = sorted(
        path
        for path in glob.glob(os.path.join(workspace, "providers", "**", "*"), recursive=True)
        if os.path.isfile(path)
    )
    watched.append(snapshot)
    watched.append(_registry_file(workspace))
    before = {path: _sha256(path) for path in watched}

    def _no_network(*args, **kwargs):
        raise AssertionError("export attempted network access")

    monkeypatch.setattr(socket, "socket", _no_network)

    buffer = io.StringIO()
    export_candidates(workspace, out=buffer)
    assert _records(buffer.getvalue())

    after = {path: _sha256(path) for path in watched}
    assert after == before
