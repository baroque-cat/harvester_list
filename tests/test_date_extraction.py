"""Parser-level date-extraction tests (fixtures, no network).

Covers ``date-extraction-S1..S5, S10`` from the add-date-extraction test plan.
Assertions encode the spec THEN clauses; the fixtures are sanitized recordings
of the real GitHub API search and blob-page shapes.
"""

import datetime
import json
import os

import pytest

from core.metrics import DateFillMetrics
from search import client

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
KEY_PATTERN = r"sk-[A-Za-z0-9]{16}"
ALPHA_URL = "https://github.com/acme/alpha/blob/main/app.py"
BETA_URL = "https://github.com/acme/beta/blob/main/main.go"
GAMMA_URL = "https://github.com/acme/gamma/blob/main/conf.env"
BAD_URL = "https://github.com/acme/bad/blob/main/a.py"


def _fixture(name: str) -> str:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return handle.read()


def _epoch(value: str) -> float:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.datetime.fromisoformat(text).timestamp()


def _patch_api(monkeypatch, payload: str) -> None:
    """Make the GitHub client return a canned payload (no network)."""

    class _FakeClient:
        def get(self, **kwargs):
            return payload

    monkeypatch.setattr(client, "get_github_client", lambda: _FakeClient())


@pytest.fixture(autouse=True)
def _reset_counters():
    client.reset_date_parse_stats()
    yield
    client.reset_date_parse_stats()


# ---------------------------------------------------------------------------
# date-extraction-S1
# ---------------------------------------------------------------------------
def test_s1_metadata_extracted_from_well_formed_items(monkeypatch):
    """S1: well-formed item yields parsed epoch + int size; URL set unchanged."""
    _patch_api(monkeypatch, _fixture("api_search_page.json"))
    metadata = {}

    results, total, content = client.search_api_with_count("sk-", "token", metadata=metadata)

    assert set(results) == {ALPHA_URL, BETA_URL, GAMMA_URL}
    assert metadata[ALPHA_URL].repo_pushed_at == pytest.approx(_epoch("2026-01-02T03:04:05Z"))
    assert metadata[ALPHA_URL].repo_size_kb == 1234
    assert metadata[ALPHA_URL].transport == "api"


# ---------------------------------------------------------------------------
# date-extraction-S2
# ---------------------------------------------------------------------------
def test_s2_missing_repository_degrades_to_null(monkeypatch):
    """S2: item without repository still returned; metadata NULL + counted."""
    _patch_api(monkeypatch, _fixture("api_search_page.json"))
    metadata = {}

    results, total, content = client.search_api_with_count("sk-", "token", metadata=metadata)

    assert BETA_URL in results
    assert metadata[BETA_URL].repo_pushed_at is None
    assert metadata[BETA_URL].repo_size_kb is None
    assert client.get_date_parse_stats().get("missing_repository", 0) >= 1


# ---------------------------------------------------------------------------
# date-extraction-S3
# ---------------------------------------------------------------------------
def test_s3_pushed_at_preferred_over_updated_at(monkeypatch):
    """S3: pushed_at wins; updated_at is the fallback when pushed_at absent."""
    _patch_api(monkeypatch, _fixture("api_search_page.json"))
    metadata = {}

    client.search_api_with_count("sk-", "token", metadata=metadata)

    assert metadata[ALPHA_URL].repo_pushed_at == pytest.approx(_epoch("2026-01-02T03:04:05Z"))
    assert metadata[ALPHA_URL].repo_pushed_at != pytest.approx(_epoch("2026-01-01T00:00:00Z"))
    # Gamma carries only updated_at -> fallback applies.
    assert metadata[GAMMA_URL].repo_pushed_at == pytest.approx(_epoch("2026-02-03T04:05:06Z"))


# ---------------------------------------------------------------------------
# date-extraction-S4
# ---------------------------------------------------------------------------
def test_s4_date_captured_from_blob_html():
    """S4: file_commit_date == max of all relative-time datetimes."""
    html = _fixture("blob_page_with_relative_time.html")
    expected = max(
        _epoch("2026-01-02T03:04:05Z"),
        _epoch("2026-03-04T05:06:07Z"),
        _epoch("2026-02-01T00:00:00Z"),
    )

    assert client._extract_file_commit_date(html) == pytest.approx(expected)

    # And it is wired through the collect() path used by the gather stage.
    metadata = {}
    services = client.collect(key_pattern=KEY_PATTERN, text=html, metadata=metadata)
    assert services
    assert metadata["file_commit_date"] == pytest.approx(expected)


# ---------------------------------------------------------------------------
# date-extraction-S5
# ---------------------------------------------------------------------------
def test_s5_layout_without_markers_yields_null_safely():
    """S5: no relative-time markers -> NULL + warning; key extraction unaffected."""
    html = _fixture("blob_page_without_markers.html")
    metadata = {}

    services = client.collect(key_pattern=KEY_PATTERN, text=html, metadata=metadata)

    assert metadata["file_commit_date"] is None
    assert services  # key extraction still succeeded
    assert client.get_date_parse_stats().get("no_relative_time", 0) >= 1


# ---------------------------------------------------------------------------
# date-extraction-S10
# ---------------------------------------------------------------------------
def test_s10_garbage_payload_does_not_break_search_or_gather(monkeypatch):
    """S10: structurally invalid items / undecodable bytes -> NULLs, no exception."""
    _patch_api(monkeypatch, _fixture("garbage_payload.json"))
    metadata = {}

    results, total, content = client.search_api_with_count("sk-", "token", metadata=metadata)

    assert results == [BAD_URL]
    assert metadata[BAD_URL].repo_pushed_at is None
    assert metadata[BAD_URL].repo_size_kb is None
    stats = client.get_date_parse_stats()
    assert stats.get("bad_item", 0) >= 1
    assert stats.get("missing_repository", 0) >= 1

    # Gather side: undecodable / unexpected payloads never raise.
    assert client._extract_file_commit_date(b"\xff\xfe\x00") is None
    assert client._extract_file_commit_date(None) is None
    assert client._extract_file_commit_date(object()) is None
    assert client.get_date_parse_stats().get("bad_html", 0) >= 1

    # Invalid JSON is swallowed by the existing fail-open contract.
    _patch_api(monkeypatch, "not-json{{")
    results, total, content = client.search_api_with_count("sk-", "token", metadata={})
    assert results == []


# ---------------------------------------------------------------------------
# date-extraction-S11 (live-captured regression pin; expected GREEN immediately)
# ---------------------------------------------------------------------------
def test_s11_live_trimmed_api_payload_yields_nulls(monkeypatch):
    """S11: live-captured trimmed repository objects -> all NULL + counted warnings."""
    fixture = _fixture("live_2026_09_api_search_page.json")
    expected = {
        item["html_url"]
        for item in json.loads(fixture)["items"]
        if isinstance(item, dict) and item.get("html_url")
    }
    # Constructing metrics resets the module-level parse counters (finding S1).
    metrics = DateFillMetrics()
    _patch_api(monkeypatch, fixture)
    metadata = {}

    results, total, content = client.search_api_with_count("filename:.env", "token", metadata=metadata)

    # URL set identical to the pre-change set-based behavior.
    assert set(results) == expected
    # Every metadata entry is NULL: GitHub serves no pushed_at/updated_at/size.
    assert metadata
    assert all(m.repo_pushed_at is None for m in metadata.values())
    assert all(m.repo_size_kb is None for m in metadata.values())
    # Absent fields are counted (spec: NULL plus a counted warning).
    assert client.get_date_parse_stats().get("missing_date", 0) >= 1
    # Drift metric reports the documented 0.0 baseline (design D7) for the run.
    for url in results:
        metrics.record_api(metadata[url].has_repo_date)
    assert metrics.date_fill_rate_api == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# date-extraction-S12 (live-captured regression pin; expected GREEN immediately)
# ---------------------------------------------------------------------------
def test_s12_live_blob_page_yields_null_safely():
    """S12: live client-rendered blob page -> NULL + warning; extraction unaffected."""
    html = _fixture("live_2026_09_blob_page.html")
    metrics = DateFillMetrics()
    metadata = {}

    assert client._extract_file_commit_date(html) is None

    services = client.collect(key_pattern=KEY_PATTERN, text=html, metadata=metadata)

    assert metadata["file_commit_date"] is None
    assert client.get_date_parse_stats().get("no_relative_time", 0) >= 1
    # Key extraction proceeds unaffected (no exception; a list is returned).
    assert isinstance(services, list)
    metrics.record_web(metadata["file_commit_date"] is not None)
    assert metrics.date_fill_rate_web == pytest.approx(0.0)
