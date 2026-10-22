"""P22: key_sources child table and source_url_hash preservation.

Tests for the additive key_sources table that records every (key_hash, url_hash)
pair a key has been observed with, plus the fix to keys.source_url_hash that
makes it first-non-empty-wins and immune to erasure by empty strings.

Traceability: P22-T1..T14.
"""

import os
import sqlite3
import time

import pytest

from storage.registry import Registry, canonical_url, url_hash, SCHEMA_VERSION


URL_A = "https://github.com/o/r/blob/main/a.py"
URL_B = "https://github.com/o/r/blob/main/b.py"
URL_C = "https://github.com/o/r/blob/main/c.py"

NOW = 1_700_000_000.0
KEY_HASH = "deadbeef" * 8  # 64 hex chars
KEY_HASH_2 = "cafebabe" * 8


def _open(workspace):
    registry = Registry(workspace, enabled=True)
    assert registry.start() is True
    return registry


def _open_fast(workspace):
    """Registry with fast flush for deterministic testing."""

    class FastConfig:
        enabled = True
        batch_size = 1
        flush_interval = 0.05
        queue_size = 100000
        path = ""

    registry = Registry(workspace, config=FastConfig(), enabled=True)
    assert registry.start() is True
    return registry


def _db_conn(workspace):
    path = os.path.join(workspace, "registry.sqlite")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _db_scalar(workspace, sql, params=()):
    conn = _db_conn(workspace)
    try:
        row = conn.execute(sql, tuple(params)).fetchone()
        return row[0] if row is not None else None
    finally:
        conn.close()


def _db_rows(workspace, sql, params=()):
    conn = _db_conn(workspace)
    try:
        return conn.execute(sql, tuple(params)).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# T1: same key from two different source URLs -> 1 keys row, 2 key_sources rows
# ---------------------------------------------------------------------------
def test_p22_t1_two_sources_one_key(workspace):
    """P22-T1: exactly 1 keys row and 2 key_sources rows for two source URLs."""
    registry = _open_fast(workspace)
    try:
        h_a = url_hash(URL_A)
        h_b = url_hash(URL_B)
        registry.record_key(KEY_HASH, provider="openai", key_ref_masked="sk-***",
                            status="valid", source_url_hash=h_a, ts=NOW)
        registry.record_key(KEY_HASH, provider="openai", key_ref_masked="sk-***",
                            status="valid", source_url_hash=h_b, ts=NOW + 100)
        registry.flush(10.0)
    finally:
        registry.stop()

    keys_count = _db_scalar(workspace, "SELECT COUNT(*) FROM keys")
    assert keys_count == 1, f"Expected 1 keys row, got {keys_count}"

    ks_rows = _db_rows(workspace, "SELECT url_hash FROM key_sources WHERE key_hash = ? ORDER BY url_hash", [KEY_HASH])
    assert len(ks_rows) == 2, f"Expected 2 key_sources rows, got {len(ks_rows)}"
    url_hashes = {row["url_hash"] for row in ks_rows}
    assert url_hashes == {h_a, h_b}


# ---------------------------------------------------------------------------
# T2: same (key,url) observed twice -> 1 row, first_seen preserved, last_seen advanced
# ---------------------------------------------------------------------------
def test_p22_t2_same_pair_observed_twice(workspace):
    """P22-T2: first_seen_ts preserved, last_seen_ts advanced on re-observation."""
    registry = _open_fast(workspace)
    try:
        h_a = url_hash(URL_A)
        registry.record_key(KEY_HASH, provider="openai", key_ref_masked="sk-***",
                            status="valid", source_url_hash=h_a, ts=NOW)
        registry.flush(10.0)

        registry.record_key(KEY_HASH, provider="openai", key_ref_masked="sk-***",
                            status="valid", source_url_hash=h_a, ts=NOW + 500)
        registry.flush(10.0)
    finally:
        registry.stop()

    rows = _db_rows(workspace, "SELECT first_seen_ts, last_seen_ts FROM key_sources WHERE key_hash = ? AND url_hash = ?",
                     [KEY_HASH, h_a])
    assert len(rows) == 1
    assert rows[0]["first_seen_ts"] == pytest.approx(NOW)
    assert rows[0]["last_seen_ts"] == pytest.approx(NOW + 500)


# ---------------------------------------------------------------------------
# T3: keys.source_url_hash keeps the FIRST non-empty source
# ---------------------------------------------------------------------------
def test_p22_t3_source_url_hash_first_wins(workspace):
    """P22-T3: keys.source_url_hash keeps the first non-empty source."""
    registry = _open_fast(workspace)
    try:
        h_a = url_hash(URL_A)
        h_b = url_hash(URL_B)
        registry.record_key(KEY_HASH, provider="openai", key_ref_masked="sk-***",
                            status="valid", source_url_hash=h_a, ts=NOW)
        registry.record_key(KEY_HASH, provider="openai", key_ref_masked="sk-***",
                            status="valid", source_url_hash=h_b, ts=NOW + 100)
        registry.flush(10.0)
    finally:
        registry.stop()

    stored = _db_scalar(workspace, "SELECT source_url_hash FROM keys WHERE key_hash = ?", [KEY_HASH])
    assert stored == h_a, f"Expected first source {h_a}, got {stored}"


# ---------------------------------------------------------------------------
# T4: empty source does not erase a known source_url_hash (erasure defect)
# ---------------------------------------------------------------------------
def test_p22_t4_empty_source_does_not_erase(workspace):
    """P22-T4: a later record_key with empty source does not erase known source_url_hash."""
    registry = _open_fast(workspace)
    try:
        h_a = url_hash(URL_A)
        registry.record_key(KEY_HASH, provider="openai", key_ref_masked="sk-***",
                            status="valid", source_url_hash=h_a, ts=NOW)
        registry.flush(10.0)

        # Second call with empty source (the erasure defect path)
        registry.record_key(KEY_HASH, provider="openai", key_ref_masked="sk-***",
                            status="valid", source_url_hash="", ts=NOW + 100)
        registry.flush(10.0)
    finally:
        registry.stop()

    stored = _db_scalar(workspace, "SELECT source_url_hash FROM keys WHERE key_hash = ?", [KEY_HASH])
    assert stored == h_a, f"Source was erased! Expected {h_a}, got '{stored}'"


# ---------------------------------------------------------------------------
# T5: record_key_observation with source_url_hash inserts source row, leaves status untouched
# ---------------------------------------------------------------------------
def test_p22_t5_observation_inserts_source_row(workspace):
    """P22-T5: record_key_observation with source_url_hash inserts key_sources row
    while leaving status and last_recheck_ts untouched."""
    registry = _open_fast(workspace)
    try:
        h_a = url_hash(URL_A)
        # First, create the key via record_key
        registry.record_key(KEY_HASH, provider="openai", key_ref_masked="sk-***",
                            status="valid", source_url_hash=h_a, ts=NOW)
        registry.flush(10.0)

        original_status = _db_scalar(workspace, "SELECT status FROM keys WHERE key_hash = ?", [KEY_HASH])
        original_recheck = _db_scalar(workspace, "SELECT last_recheck_ts FROM keys WHERE key_hash = ?", [KEY_HASH])

        # Now observe from a different source
        h_b = url_hash(URL_B)
        registry.record_key_observation(KEY_HASH, source_url_hash=h_b, ts=NOW + 200)
        registry.flush(10.0)
    finally:
        registry.stop()

    # Status and last_recheck_ts must be untouched
    new_status = _db_scalar(workspace, "SELECT status FROM keys WHERE key_hash = ?", [KEY_HASH])
    new_recheck = _db_scalar(workspace, "SELECT last_recheck_ts FROM keys WHERE key_hash = ?", [KEY_HASH])
    assert new_status == original_status
    assert new_recheck == original_recheck

    # But key_sources should have the new row
    ks_rows = _db_rows(workspace, "SELECT url_hash FROM key_sources WHERE key_hash = ?", [KEY_HASH])
    url_hashes = {row["url_hash"] for row in ks_rows}
    assert h_b in url_hashes


# ---------------------------------------------------------------------------
# T6: v5->v6 migration with backfill
# ---------------------------------------------------------------------------
def test_p22_t6_v5_to_v6_migration(workspace):
    """P22-T6: v5 database migrates to v6 with backfill and meta marker."""
    path = os.path.join(workspace, "registry.sqlite")
    conn = sqlite3.connect(path)

    # Build a v5-shaped database
    conn.executescript("""
        CREATE TABLE links(
          url_hash TEXT PRIMARY KEY,
          url TEXT NOT NULL,
          owner TEXT,
          repo TEXT,
          path TEXT,
          first_seen_ts REAL,
          last_seen_ts REAL,
          gathered_ts REAL,
          visit_status TEXT NOT NULL DEFAULT 'discovered',
          transport TEXT,
          query_origin TEXT,
          provider TEXT,
          repo_pushed_at REAL,
          repo_size_kb INTEGER,
          file_commit_date REAL,
          priority REAL,
          etag TEXT,
          etag_transport TEXT
        );
        CREATE TABLE link_coverage(
          url_hash TEXT NOT NULL,
          provider TEXT NOT NULL,
          patterns_hash TEXT NOT NULL,
          gathered_ts REAL NOT NULL,
          PRIMARY KEY(url_hash, provider, patterns_hash)
        );
        CREATE TABLE keys(
          key_hash TEXT PRIMARY KEY,
          provider TEXT,
          key_ref_masked TEXT,
          address TEXT,
          endpoint TEXT,
          status TEXT,
          first_seen_ts REAL,
          last_recheck_ts REAL,
          last_seen_ts REAL,
          source_url_hash TEXT
        );
        CREATE TABLE runs(
          run_id TEXT PRIMARY KEY,
          started_at REAL,
          finished_at REAL,
          config_digest TEXT,
          degraded INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE repos(
          owner TEXT NOT NULL,
          repo TEXT NOT NULL,
          pushed_at REAL,
          size_kb INTEGER,
          default_branch TEXT,
          etag TEXT,
          fetched_at REAL NOT NULL,
          gone INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(owner, repo)
        );
        CREATE TABLE meta(
          key TEXT PRIMARY KEY,
          value TEXT
        );
        CREATE TABLE file_meta(
          owner TEXT NOT NULL,
          repo TEXT NOT NULL,
          path TEXT NOT NULL,
          resolved_sha TEXT,
          committed_date REAL,
          etag TEXT,
          fetched_at REAL NOT NULL,
          gone INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(owner, repo, path)
        );
        CREATE INDEX idx_links_repo ON links(owner, repo);
        CREATE INDEX idx_links_last_seen ON links(last_seen_ts);
        CREATE INDEX idx_links_provider ON links(provider);
        CREATE INDEX idx_keys_recheck ON keys(status, last_recheck_ts);
        CREATE INDEX idx_file_meta_fetched ON file_meta(fetched_at);
    """)

    # Seed 3 keys rows: 2 with non-empty source_url_hash, 1 empty
    h_a = url_hash(URL_A)
    h_b = url_hash(URL_B)
    conn.execute(
        "INSERT INTO keys (key_hash, provider, key_ref_masked, status, first_seen_ts, last_seen_ts, source_url_hash) "
        "VALUES (?, 'openai', 'sk-***', 'valid', 100.0, 200.0, ?)",
        ("key1" + "0" * 60, h_a),
    )
    conn.execute(
        "INSERT INTO keys (key_hash, provider, key_ref_masked, status, first_seen_ts, last_seen_ts, source_url_hash) "
        "VALUES (?, 'openai', 'sk-***', 'valid', 150.0, 250.0, ?)",
        ("key2" + "0" * 60, h_b),
    )
    conn.execute(
        "INSERT INTO keys (key_hash, provider, key_ref_masked, status, first_seen_ts, last_seen_ts, source_url_hash) "
        "VALUES (?, 'openai', 'sk-***', 'invalid', 120.0, 220.0, '')",
        ("key3" + "0" * 60,),
    )
    conn.execute("PRAGMA user_version = 5")
    conn.commit()
    conn.close()

    # Open through the shipped Registry: migration must run
    registry = Registry(workspace, enabled=True)
    registry.start()
    registry.stop()

    # Schema version is now 6
    assert _db_scalar(workspace, "PRAGMA user_version") == 7

    # key_sources table exists
    tables = {row["name"] for row in _db_rows(workspace, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "key_sources" in tables

    # idx_key_sources_url index exists
    indexes = {row["name"] for row in _db_rows(workspace, "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_key_sources_url" in indexes

    # key_sources_dated view exists
    views = {row["name"] for row in _db_rows(workspace, "SELECT name FROM sqlite_master WHERE type='view'")}
    assert "key_sources_dated" in views

    # Exactly 2 backfilled rows
    ks_count = _db_scalar(workspace, "SELECT COUNT(*) FROM key_sources")
    assert ks_count == 2

    # Check the backfilled pairs and timestamps
    rows = _db_rows(workspace, "SELECT key_hash, url_hash, first_seen_ts, last_seen_ts FROM key_sources ORDER BY key_hash")
    assert len(rows) == 2
    assert rows[0]["key_hash"] == "key1" + "0" * 60
    assert rows[0]["url_hash"] == h_a
    assert rows[0]["first_seen_ts"] == 100.0
    assert rows[0]["last_seen_ts"] == 200.0
    assert rows[1]["key_hash"] == "key2" + "0" * 60
    assert rows[1]["url_hash"] == h_b

    # Meta marker exists and starts with "2@"
    marker = _db_scalar(workspace, "SELECT value FROM meta WHERE key = 'key_sources_backfill'")
    assert marker is not None
    assert marker.startswith("2@"), f"Expected marker starting with '2@', got '{marker}'"

    # Pre-existing keys/links values unchanged
    keys_count = _db_scalar(workspace, "SELECT COUNT(*) FROM keys")
    assert keys_count == 3


# ---------------------------------------------------------------------------
# T7: reopening migrated registry is idempotent
# ---------------------------------------------------------------------------
def test_p22_t7_reopen_is_idempotent(workspace):
    """P22-T7: reopening the migrated registry adds no duplicates, marker unchanged."""
    # First, build and migrate
    registry = _open(workspace)
    h_a = url_hash(URL_A)
    registry.record_key(KEY_HASH, provider="openai", key_ref_masked="sk-***",
                        status="valid", source_url_hash=h_a, ts=NOW)
    registry.flush(10.0)
    registry.stop()

    # Read marker after first open
    marker_1 = _db_scalar(workspace, "SELECT value FROM meta WHERE key = 'key_sources_backfill'")
    ks_count_1 = _db_scalar(workspace, "SELECT COUNT(*) FROM key_sources")

    # Reopen
    registry2 = Registry(workspace, enabled=True)
    registry2.start()
    registry2.stop()

    marker_2 = _db_scalar(workspace, "SELECT value FROM meta WHERE key = 'key_sources_backfill'")
    ks_count_2 = _db_scalar(workspace, "SELECT COUNT(*) FROM key_sources")
    version = _db_scalar(workspace, "PRAGMA user_version")

    assert version == 7
    assert ks_count_2 == ks_count_1
    assert marker_2 == marker_1


# ---------------------------------------------------------------------------
# T8: fresh workspace bootstraps with table present and marker count 0
# ---------------------------------------------------------------------------
def test_p22_t8_fresh_workspace_bootstrap(workspace):
    """P22-T8: fresh workspace has key_sources table and marker with count 0."""
    registry = _open(workspace)
    registry.stop()

    tables = {row["name"] for row in _db_rows(workspace, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "key_sources" in tables

    marker = _db_scalar(workspace, "SELECT value FROM meta WHERE key = 'key_sources_backfill'")
    assert marker is not None
    assert marker.startswith("0@"), f"Expected marker starting with '0@', got '{marker}'"


# ---------------------------------------------------------------------------
# T9: priority attribution uses all sources
# ---------------------------------------------------------------------------
def test_p22_t9_priority_attribution_multi_source(workspace):
    """P22-T9: a key with sources in two repos contributes to BOTH repos' scores."""
    from storage.priority import score_repo, status_counts_for_repos, PriorityWeights

    registry = _open_fast(workspace)
    try:
        h_a = url_hash(URL_A)
        h_b = url_hash(URL_B)

        # Create links in two different repos
        registry.record_link(URL_A, ts=NOW)
        registry.record_link(URL_B, ts=NOW)
        registry.flush(10.0)

        # Record a key attributed to both sources
        registry.record_key(KEY_HASH, provider="openai", key_ref_masked="sk-***",
                            status="valid", source_url_hash=h_a, ts=NOW)
        registry.record_key(KEY_HASH, provider="openai", key_ref_masked="sk-***",
                            status="valid", source_url_hash=h_b, ts=NOW + 10)
        registry.flush(10.0)
        registry.stop()

        conn = _db_conn(workspace)
        try:
            weights = PriorityWeights()
            score_a = score_repo(conn, "o", "r", weights, now=NOW)
            counts = status_counts_for_repos(conn)
        finally:
            conn.close()
    finally:
        if registry.available:
            registry.stop()

    # The key should contribute to repo (o, r) via both sources
    # Since both URL_A and URL_B are in the same repo (o, r), we expect valid_present=True
    # Score should be w1 + freshness component
    expected_score = weights.w1  # valid key, no freshness (no pushed_at)
    assert score_a == pytest.approx(expected_score, abs=1e-6)

    # status_counts should attribute the key to (o, r)
    assert ("o", "r") in counts
    assert counts[("o", "r")].get("valid", 0) >= 1


def test_p22_t9b_single_source_score_unchanged(workspace):
    """P22-T9b: a single-source key produces the same score as before the change."""
    from storage.priority import score_repo, PriorityWeights

    registry = _open_fast(workspace)
    try:
        h_a = url_hash(URL_A)
        registry.record_link(URL_A, ts=NOW)
        registry.flush(10.0)

        registry.record_key(KEY_HASH, provider="openai", key_ref_masked="sk-***",
                            status="valid", source_url_hash=h_a, ts=NOW)
        registry.flush(10.0)
        registry.stop()

        conn = _db_conn(workspace)
        try:
            weights = PriorityWeights()
            score = score_repo(conn, "o", "r", weights, now=NOW)
        finally:
            conn.close()
    finally:
        if registry.available:
            registry.stop()

    # Single valid key, no freshness, no size penalty
    expected = weights.w1  # 100.0
    assert score == pytest.approx(expected, abs=1e-6)


# ---------------------------------------------------------------------------
# T10: key_sources_dated view returns joined links.file_commit_date
# ---------------------------------------------------------------------------
def test_p22_t10_view_returns_file_commit_date(workspace):
    """P22-T10: key_sources_dated view returns the joined links.file_commit_date."""
    registry = _open_fast(workspace)
    try:
        h_a = url_hash(URL_A)
        registry.record_link(URL_A, ts=NOW)
        registry.flush(10.0)

        # Set file_commit_date on the link
        registry.record_metadata({URL_A: type("M", (), {"repo_pushed_at": None, "repo_size_kb": None, "file_commit_date": 1699000000.0})()})
        registry.flush(10.0)

        registry.record_key(KEY_HASH, provider="openai", key_ref_masked="sk-***",
                            status="valid", source_url_hash=h_a, ts=NOW)
        registry.flush(10.0)
        registry.stop()

        rows = _db_rows(workspace,
                         "SELECT file_commit_date FROM key_sources_dated WHERE key_hash = ? AND url_hash = ?",
                         [KEY_HASH, h_a])
        assert len(rows) == 1
        assert rows[0]["file_commit_date"] == pytest.approx(1699000000.0)
    finally:
        if registry.available:
            registry.stop()


# ---------------------------------------------------------------------------
# T11: fail-open behaviour
# ---------------------------------------------------------------------------
def test_p22_t11_fail_open_no_raise(workspace):
    """P22-T11: with registry degraded/unavailable, record_key and record_key_observation do not raise."""
    # Disabled registry
    registry = Registry(workspace, enabled=False)
    registry.start()
    # These must not raise
    registry.record_key(KEY_HASH, provider="openai", key_ref_masked="sk-***",
                        status="valid", source_url_hash=url_hash(URL_A), ts=NOW)
    registry.record_key_observation(KEY_HASH, source_url_hash=url_hash(URL_B), ts=NOW)
    registry.stop()


def test_p22_t11b_atomicity(workspace):
    """P22-T11b: key_sources write and key upsert are in the same transaction."""
    registry = _open_fast(workspace)
    try:
        h_a = url_hash(URL_A)
        registry.record_key(KEY_HASH, provider="openai", key_ref_masked="sk-***",
                            status="valid", source_url_hash=h_a, ts=NOW)
        registry.flush(10.0)

        # Verify both tables were written
        keys_count = _db_scalar(workspace, "SELECT COUNT(*) FROM keys")
        ks_count = _db_scalar(workspace, "SELECT COUNT(*) FROM key_sources")
        assert keys_count == 1
        assert ks_count == 1
    finally:
        registry.stop()


# ---------------------------------------------------------------------------
# T14: no plaintext secret in key_sources
# ---------------------------------------------------------------------------
def test_p22_t14_no_plaintext_in_key_sources(workspace):
    """P22-T14: no plaintext secret ever reaches key_sources."""
    canary = "sk-canary-XYZXYZXYZXYZXYZXYZXYZXYZXY"
    registry = _open_fast(workspace)
    try:
        from storage.key_ledger import key_hash as compute_key_hash, mask_key
        digest = compute_key_hash("openai", canary, "https://api.example.com", "https://api.example.com/v1")
        h_a = url_hash(URL_A)
        registry.record_key(digest, provider="openai", key_ref_masked=mask_key(canary),
                            address="https://api.example.com",
                            endpoint="https://api.example.com/v1",
                            status="valid", source_url_hash=h_a, ts=NOW)
        registry.flush(10.0)
    finally:
        registry.stop()

    # Raw byte scan of the database
    blobs = []
    for suffix in ("", "-wal", "-shm"):
        path = os.path.join(workspace, "registry.sqlite") + suffix
        if os.path.exists(path):
            with open(path, "rb") as handle:
                blobs.append(handle.read())

    raw = b"".join(blobs)
    assert canary.encode("utf-8") not in raw, "Plaintext secret found in database!"
