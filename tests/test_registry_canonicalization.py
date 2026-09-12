"""Tests for URL canonicalization and link identity.

Traceability: link-registry-S4, link-registry-S5, link-registry-S6.
"""

import sqlite3

from storage.registry import Registry, canonical_url, url_hash


def test_s4_line_fragment_does_not_change_identity():
    """link-registry-S4: line fragments are discarded before hashing."""
    first = "https://github.com/o/r/blob/main/a.py#L10"
    second = "https://github.com/o/r/blob/main/a.py#L42-L50"

    assert canonical_url(first) == canonical_url(second)
    assert url_hash(first) == url_hash(second)


def test_s5_path_case_is_significant():
    """link-registry-S5: path letter case is preserved in the identity."""
    upper = "https://github.com/o/r/blob/main/Config.env"
    lower = "https://github.com/o/r/blob/main/config.env"

    assert canonical_url(upper) != canonical_url(lower)
    assert url_hash(upper) != url_hash(lower)


def test_s6_transports_converge_on_one_identity(workspace, registry_path):
    """link-registry-S6: api and web transports share one row; recency wins."""
    registry = Registry(workspace, enabled=True)
    assert registry.start() is True

    url = "https://github.com/o/r/blob/main/a.py"
    registry.record_link(url, transport="api", ts=1000.0)
    registry.record_link(url, transport="web", ts=2000.0)
    registry.stop()

    conn = sqlite3.connect(registry_path(workspace))
    try:
        records = conn.execute("SELECT url_hash, first_seen_ts, last_seen_ts, transport FROM links").fetchall()
    finally:
        conn.close()

    assert len(records) == 1
    _, first_seen, last_seen, transport = records[0]
    assert first_seen == 1000.0
    assert last_seen == 2000.0
    assert transport == "web"
