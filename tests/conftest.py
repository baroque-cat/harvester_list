"""Pytest fixtures and synthetic workspace builders for the registry suite.

Kept in ``conftest.py`` (rather than a helper module) so the test directory
does not need to be a Python package -- the repository root is itself a
package and importing it has unrelated side effects.
"""

import datetime
import json
import os
import sqlite3
from typing import Any, Dict, Iterable, List, Optional

import pytest

REGISTRY_FILENAME = "registry.sqlite"


def _registry_path(workspace: str) -> str:
    return os.path.join(str(workspace), REGISTRY_FILENAME)


def _connect(workspace: str) -> sqlite3.Connection:
    conn = sqlite3.connect(_registry_path(workspace))
    conn.row_factory = sqlite3.Row
    return conn


def _rows(workspace: str, sql: str, params: Iterable[Any] = ()) -> List[sqlite3.Row]:
    conn = _connect(workspace)
    try:
        return conn.execute(sql, tuple(params)).fetchall()
    finally:
        conn.close()


def _scalar(workspace: str, sql: str, params: Iterable[Any] = ()) -> Any:
    conn = _connect(workspace)
    try:
        row = conn.execute(sql, tuple(params)).fetchone()
        return row[0] if row is not None else None
    finally:
        conn.close()


def _epoch_from_iso(value: str) -> float:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.datetime.fromisoformat(text).timestamp()


def _provider_dir(workspace: str, provider: str) -> str:
    path = os.path.join(str(workspace), "providers", provider)
    os.makedirs(path, exist_ok=True)
    return path


def _write_index(shard_path: str, first_ts: str, last_ts: str, lines: int) -> None:
    index_path = os.path.splitext(shard_path)[0] + ".index.json"
    payload = {
        "first_ts": first_ts,
        "last_ts": last_ts,
        "lines": lines,
        "bad_lines": 0,
        "schema_version": "1.0",
        "file": os.path.basename(shard_path),
    }
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(payload, f)


def _write_links_shard(
    workspace: str,
    provider: str,
    urls: List[str],
    first_ts: Optional[float] = None,
    last_ts: Optional[float] = None,
    name: Optional[str] = None,
) -> str:
    first_ts = first_ts if first_ts is not None else 1_700_000_000.0
    last_ts = last_ts if last_ts is not None else first_ts
    first_iso = datetime.datetime.fromtimestamp(first_ts, tz=datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    last_iso = datetime.datetime.fromtimestamp(last_ts, tz=datetime.timezone.utc).isoformat().replace("+00:00", "Z")

    directory = os.path.join(_provider_dir(workspace, provider), "shards", "links")
    os.makedirs(directory, exist_ok=True)
    shard_name = name or f"links_{provider}.ndjson"
    shard_path = os.path.join(directory, shard_name)
    with open(shard_path, "w", encoding="utf-8") as f:
        for url in urls:
            f.write(json.dumps({"value": url}, ensure_ascii=False) + "\n")
    _write_index(shard_path, first_iso, last_iso, len(urls))
    return shard_path


def _write_result_shard(
    workspace: str,
    provider: str,
    result_type: str,
    services: List[Dict[str, Any]],
    name: Optional[str] = None,
) -> str:
    directory = os.path.join(_provider_dir(workspace, provider), "shards", result_type)
    os.makedirs(directory, exist_ok=True)
    shard_name = name or f"{result_type}_{provider}.ndjson"
    shard_path = os.path.join(directory, shard_name)
    with open(shard_path, "w", encoding="utf-8") as f:
        for service in services:
            f.write(json.dumps(service, ensure_ascii=False) + "\n")
    return shard_path


@pytest.fixture
def workspace(tmp_path):
    """Provide an isolated workspace directory per test."""
    path = tmp_path / "workspace"
    path.mkdir()
    return str(path)


@pytest.fixture
def registry_path():
    return _registry_path


@pytest.fixture
def db_rows():
    return _rows


@pytest.fixture
def db_scalar():
    return _scalar


@pytest.fixture
def write_links_shard():
    return _write_links_shard


@pytest.fixture
def write_result_shard():
    return _write_result_shard
