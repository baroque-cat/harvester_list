"""Per-file freshness evidence cache (add-file-commit-dating).

Traceability: file-commit-dating-S14, S16, S17, S18, S19.

Harness: a tmp-workspace registry is populated through the file_meta writer
API.  The clock is frozen so TTL comparisons are deterministic.  No network.
"""

import os
import sqlite3

import pytest

from storage.registry import Registry

OWNER = "octocat"
REPO = "Hello-World"
# The links table stores the full URL path including the blob/ref prefix,
# so file_meta paths must match that form for propagation to work.
PATH = "blob/main/src/main.py"
OTHER_PATH = "blob/main/src/other.py"

NOW = 1_700_000_000.0
HOUR = 3600.0
DAY = 24 * HOUR
TTL_HOURS = 24.0


class _FastRegistryConfig:
    """Registry config that flushes synchronously-ish (batch_size=1)."""

    enabled = True
    batch_size = 1
    flush_interval = 0.05
    queue_size = 100000
    path = ""


def _registry(workspace: str) -> Registry:
    registry = Registry(workspace, config=_FastRegistryConfig(), enabled=True)
    assert registry.start() is True
    return registry


def _connect(workspace: str) -> sqlite3.Connection:
    conn = sqlite3.connect(os.path.join(workspace, "registry.sqlite"))
    conn.row_factory = sqlite3.Row
    return conn


def _file_meta_row(workspace: str, owner: str, repo: str, path: str):
    conn = _connect(workspace)
    try:
        return conn.execute(
            "SELECT * FROM file_meta WHERE owner = ? AND repo = ? AND path = ?",
            (owner, repo, path),
        ).fetchone()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# file-commit-dating-S16: fresh entry issues no request; expired does
# ---------------------------------------------------------------------------
def test_s16_fresh_entry_is_fresh_expired_is_not(workspace):
    """S16: a fresh cache entry is fresh; past TTL it is not."""
    from storage.file_meta import FileMetaStore, TTL_HOURS_DEFAULT

    registry = _registry(workspace)
    try:
        store = FileMetaStore(workspace, ttl_hours=TTL_HOURS, registry=registry, clock=lambda: NOW)
        store.record_upsert(OWNER, REPO, PATH, resolved_sha="abc123", committed_date=NOW - DAY, ts=NOW)
        registry.flush(10.0)

        entry = store.get(OWNER, REPO, PATH)
        assert entry is not None
        assert store.is_fresh(entry, now=NOW) is True
        assert store.is_fresh(entry, now=NOW + TTL_HOURS * HOUR + 1) is False
        assert store.is_fresh(None) is False
    finally:
        registry.stop()


# ---------------------------------------------------------------------------
# file-commit-dating-S17: expired entry exposes etag for conditional revalidation
# ---------------------------------------------------------------------------
def test_s17_expired_entry_exposes_etag_for_revalidation(workspace):
    """S17: an expired entry with an etag exposes it; record_touch refreshes."""
    from storage.file_meta import FileMetaStore

    registry = _registry(workspace)
    try:
        store = FileMetaStore(workspace, ttl_hours=TTL_HOURS, registry=registry, clock=lambda: NOW)
        store.record_upsert(
            OWNER, REPO, PATH,
            resolved_sha="abc123",
            committed_date=NOW - 2 * DAY,
            etag="E-ORIGINAL",
            ts=NOW - 2 * DAY,
        )
        registry.flush(10.0)

        entry = store.get(OWNER, REPO, PATH)
        assert entry is not None
        assert entry.etag == "E-ORIGINAL"
        # Expired but etag is still readable for conditional revalidation.
        assert store.is_fresh(entry, now=NOW) is False
        assert entry.has_date is True

        # record_touch refreshes fetched_at and etag, preserving committed_date.
        store.record_touch(OWNER, REPO, PATH, ts=NOW, etag="E-NEW")
        registry.flush(10.0)

        updated = store.get(OWNER, REPO, PATH)
        assert updated is not None
        assert updated.etag == "E-NEW"
        assert updated.committed_date == pytest.approx(NOW - 2 * DAY)
        assert updated.fetched_at == pytest.approx(NOW)
        assert store.is_fresh(updated, now=NOW) is True
    finally:
        registry.stop()


# ---------------------------------------------------------------------------
# file-commit-dating-S18: gone target suppresses further attempts
# ---------------------------------------------------------------------------
def test_s18_gone_target_suppresses_within_ttl(workspace):
    """S18: record_gone marks the entry; is_fresh stays True within TTL."""
    from storage.file_meta import FileMetaStore

    registry = _registry(workspace)
    try:
        store = FileMetaStore(workspace, ttl_hours=TTL_HOURS, registry=registry, clock=lambda: NOW)
        store.record_gone(OWNER, REPO, PATH, ts=NOW)
        registry.flush(10.0)

        entry = store.get(OWNER, REPO, PATH)
        assert entry is not None
        assert entry.gone == 1
        # Within TTL: gone entry is still fresh (suppresses retries).
        assert store.is_fresh(entry, now=NOW) is True
        assert store.is_fresh(entry, now=NOW + HOUR) is True
        # Past TTL: no longer fresh, a later run may retry.
        assert store.is_fresh(entry, now=NOW + TTL_HOURS * HOUR + 1) is False
    finally:
        registry.stop()


# ---------------------------------------------------------------------------
# file-commit-dating-S19: sha mismatch invalidates cached entry
# ---------------------------------------------------------------------------
def test_s19_sha_mismatch_invalidates_cached_entry(workspace):
    """S19: is_fresh with a different sha is False; matching sha is True."""
    from storage.file_meta import FileMetaStore

    registry = _registry(workspace)
    try:
        store = FileMetaStore(workspace, ttl_hours=TTL_HOURS, registry=registry, clock=lambda: NOW)
        store.record_upsert(OWNER, REPO, PATH, resolved_sha="first", committed_date=NOW - HOUR, ts=NOW)
        registry.flush(10.0)

        entry = store.get(OWNER, REPO, PATH)
        assert entry is not None
        assert store.is_fresh(entry, sha="first", now=NOW) is True
        assert store.is_fresh(entry, sha="other", now=NOW) is False
        # No sha argument: freshness depends only on TTL.
        assert store.is_fresh(entry, now=NOW) is True
    finally:
        registry.stop()


# ---------------------------------------------------------------------------
# file-commit-dating-S14: gone persists across store re-open
# ---------------------------------------------------------------------------
def test_s14_gone_persists_across_reopen(workspace):
    """S14: record_gone for a not-found target persists across a store re-open."""
    from storage.file_meta import FileMetaStore

    registry = _registry(workspace)
    try:
        store = FileMetaStore(workspace, ttl_hours=TTL_HOURS, registry=registry, clock=lambda: NOW)
        store.record_gone(OWNER, REPO, PATH, ts=NOW)
        registry.flush(10.0)
    finally:
        registry.stop()
        store.close()

    # Re-open: fresh store handle, same on-disk database.
    registry2 = _registry(workspace)
    try:
        store2 = FileMetaStore(workspace, ttl_hours=TTL_HOURS, registry=registry2, clock=lambda: NOW)
        entry = store2.get(OWNER, REPO, PATH)
        assert entry is not None
        assert entry.gone == 1
        assert store2.is_fresh(entry, now=NOW) is True
    finally:
        registry2.stop()
        store2.close()


# ---------------------------------------------------------------------------
# Fail-open: corrupt/missing database
# ---------------------------------------------------------------------------
def test_fail_open_missing_database(workspace):
    """A store pointed at a missing database returns None/empty, never raises."""
    from storage.file_meta import FileMetaStore

    store = FileMetaStore(
        workspace,
        ttl_hours=TTL_HOURS,
        registry_path=os.path.join(workspace, "nonexistent.sqlite"),
        clock=lambda: NOW,
    )
    assert store.get(OWNER, REPO, PATH) is None
    assert store.get_many([(OWNER, REPO, PATH)]) == {}
    assert store.read_errors >= 1


def test_fail_open_corrupt_database(workspace):
    """A store pointed at a corrupt database returns None/empty, never raises."""
    from storage.file_meta import FileMetaStore

    corrupt_path = os.path.join(workspace, "corrupt.sqlite")
    with open(corrupt_path, "wb") as f:
        f.write(b"not a sqlite database")

    store = FileMetaStore(
        workspace,
        ttl_hours=TTL_HOURS,
        registry_path=corrupt_path,
        clock=lambda: NOW,
    )
    assert store.get(OWNER, REPO, PATH) is None
    assert store.get_many([(OWNER, REPO, PATH)]) == {}
    assert store.read_errors >= 1


# ---------------------------------------------------------------------------
# stale_or_missing mirrors RepoMetaStore
# ---------------------------------------------------------------------------
def test_stale_or_missing_returns_absent_and_expired(workspace):
    """stale_or_missing returns entries needing a fetch."""
    from storage.file_meta import FileMetaStore

    registry = _registry(workspace)
    try:
        store = FileMetaStore(workspace, ttl_hours=TTL_HOURS, registry=registry, clock=lambda: NOW)
        store.record_upsert(OWNER, REPO, PATH, resolved_sha="sha1", committed_date=NOW - HOUR, ts=NOW)
        registry.flush(10.0)

        # Fresh entry: not stale.
        result = store.stale_or_missing([(OWNER, REPO, PATH)], now=NOW)
        assert result == []

        # Expired entry: stale.
        result = store.stale_or_missing([(OWNER, REPO, PATH)], now=NOW + TTL_HOURS * HOUR + 1)
        assert (OWNER, REPO, PATH) in result

        # Unknown entry: missing.
        result = store.stale_or_missing([(OWNER, REPO, OTHER_PATH)], now=NOW)
        assert (OWNER, REPO, OTHER_PATH) in result
    finally:
        registry.stop()


# ---------------------------------------------------------------------------
# FileMetaEntry properties
# ---------------------------------------------------------------------------
def test_entry_properties():
    """FileMetaEntry.has_date and matches_sha behave correctly."""
    from storage.file_meta import FileMetaEntry

    with_date = FileMetaEntry(owner="o", repo="r", path="p", resolved_sha="abc", committed_date=1000.0)
    assert with_date.has_date is True
    assert with_date.matches_sha("abc") is True
    assert with_date.matches_sha("other") is False

    without_date = FileMetaEntry(owner="o", repo="r", path="p")
    assert without_date.has_date is False

    # Both empty/None sha counts as a match only when both are empty.
    no_sha = FileMetaEntry(owner="o", repo="r", path="p", resolved_sha=None)
    assert no_sha.matches_sha(None) is True
    assert no_sha.matches_sha("abc") is False


# ---------------------------------------------------------------------------
# record_link_date propagates to links
# ---------------------------------------------------------------------------
def test_record_link_date_propagates(workspace, db_scalar):
    """record_link_date propagates a known committed_date to links rows."""
    from storage.file_meta import FileMetaStore

    # The URL path includes "blob/main/" but the file_meta path is the full URL path.
    file_path = "blob/main/src/main.py"
    url = f"https://github.com/{OWNER}/{REPO}/{file_path}"
    registry = _registry(workspace)
    try:
        registry.record_link(url, ts=NOW - DAY)
        registry.flush(10.0)

        store = FileMetaStore(workspace, ttl_hours=TTL_HOURS, registry=registry, clock=lambda: NOW)
        store.record_link_date(OWNER, REPO, file_path, committed_date=NOW - HOUR)
        registry.flush(10.0)

        stored = db_scalar(workspace, "SELECT file_commit_date FROM links WHERE url = ?", (url,))
        assert stored == pytest.approx(NOW - HOUR)
    finally:
        registry.stop()


# ---------------------------------------------------------------------------
# No-registry store does nothing on writes
# ---------------------------------------------------------------------------
def test_store_without_registry_is_noop(workspace):
    """A store with no registry delegates nothing and does not raise."""
    from storage.file_meta import FileMetaStore

    store = FileMetaStore(workspace, ttl_hours=TTL_HOURS, registry=None, clock=lambda: NOW)
    # These must not raise.
    store.record_upsert(OWNER, REPO, PATH, committed_date=NOW)
    store.record_touch(OWNER, REPO, PATH)
    store.record_gone(OWNER, REPO, PATH)
    store.record_link_date(OWNER, REPO, PATH, committed_date=NOW)
    store.flush()
    store.close()
