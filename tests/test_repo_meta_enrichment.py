"""Unit-level tests for repository metadata enrichment (add-repo-meta-enrichment).

Traceability: repo-meta-enrichment-S1..S5, S12.

Harness: a tmp-workspace registry is populated through the change-1 writer API
(``record_link``) and the new repo-cache writer API (``record_repo_upsert``).
The HTTP transport is a ``FakeClient`` returning the LIVE-CAPTURED fixtures
captured by task 1.1 (200 full body + ETag, 304 empty + ETag echo, 404).  The
field-absent vectors are SYNTHETIC supplements and are labelled as such.  The
clock is frozen so TTL comparisons are deterministic.  No network.
"""

import datetime
import json
import os
import sqlite3

from storage.registry import Registry

from storage.repo_meta import (
    RepoMetaEnricher,
    RepoMetaStore,
    fetch_repo_metadata,
)

PROVIDER = "openai"
OWNER = "octocat"
REPO = "Hello-World"
OTHER_OWNER = "acme"
OTHER_REPO = "widgets"

NOW = 1_700_000_000.0
HOUR = 3600.0
DAY = 24 * HOUR
TTL_HOURS = 24.0

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


class _FastRegistryConfig:
    """Registry config that flushes synchronously-ish (batch_size=1)."""

    enabled = True
    batch_size = 1
    flush_interval = 0.05
    queue_size = 100000
    path = ""


class FakeAuth:
    """Injected credential provider; always yields one API token by default."""

    def __init__(self, tokens=("api-token",)):
        self._tokens = list(tokens)
        self._index = 0

    def get_token(self):
        if not self._tokens:
            return None
        token = self._tokens[self._index % len(self._tokens)]
        self._index += 1
        return token

    def get_session(self):
        return "session-token"

    def get_user_agent(self):
        return "test-agent"


class FakeClient:
    """Minimal stand-in for the GitHubClient transport surface.

    Responses are ``(status, body, headers)`` tuples or exceptions.  A single
    response is consumed per call; an exhausted queue answers 500.
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers_sent = []

    def get_with_status(self, url, headers=None, params=None, retries=3, interval=0, timeout=10, credential=None):
        self.calls.append(url)
        self.headers_sent.append({str(k).lower(): v for k, v in (headers or {}).items()})
        item = self.responses.pop(0) if self.responses else (500, "", {})
        if isinstance(item, Exception):
            raise item
        return item


def _registry(workspace: str) -> Registry:
    registry = Registry(workspace, config=_FastRegistryConfig(), enabled=True)
    assert registry.start() is True
    return registry


def _read(name: str) -> dict:
    with open(os.path.join(FIXTURE_DIR, name), encoding="utf-8") as handle:
        return json.load(handle)


def _fixture_200_body() -> dict:
    return _read("live_2026_09_repos_200.json")["body"]


def _iso(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(value: str) -> float:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.datetime.fromisoformat(text).timestamp()


def _body(pushed_at: float, size=1, branch="main") -> str:
    return json.dumps({"name": "x", "pushed_at": _iso(pushed_at), "size": size, "default_branch": branch})


def _seed_links(registry: Registry, owner: str, repo: str, count: int = 3, prefix: str = "f") -> list:
    urls = [f"https://github.com/{owner}/{repo}/blob/main/{prefix}{i}.py" for i in range(count)]
    for url in urls:
        registry.record_link(url, transport="web", provider=PROVIDER, ts=NOW - DAY)
    registry.flush(10.0)
    return urls


def _connect(workspace: str) -> sqlite3.Connection:
    conn = sqlite3.connect(os.path.join(workspace, "registry.sqlite"))
    conn.row_factory = sqlite3.Row
    return conn


def _repo_row(workspace: str, owner: str, repo: str):
    conn = _connect(workspace)
    try:
        return conn.execute("SELECT * FROM repos WHERE owner = ? AND repo = ?", (owner, repo)).fetchone()
    finally:
        conn.close()


def _link_rows(workspace: str, owner: str, repo: str):
    conn = _connect(workspace)
    try:
        return conn.execute(
            "SELECT repo_pushed_at, repo_size_kb FROM links WHERE owner = ? AND repo = ?", (owner, repo)
        ).fetchall()
    finally:
        conn.close()


def _link_count(workspace: str) -> int:
    conn = _connect(workspace)
    try:
        return conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
    finally:
        conn.close()


def _enricher(workspace, registry, client, **kwargs) -> RepoMetaEnricher:
    store = RepoMetaStore(workspace, ttl_hours=TTL_HOURS, registry=registry, clock=lambda: NOW)
    return RepoMetaEnricher(
        store,
        auth=FakeAuth(),
        client=client,
        enabled=True,
        ttl_hours=TTL_HOURS,
        clock=lambda: NOW,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# repo-meta-enrichment-S1
# ---------------------------------------------------------------------------
def test_s1_successful_fetch_populates_cache_and_link_columns(workspace):
    fixture200 = _read("live_2026_09_repos_200.json")
    expected_pushed = _epoch(fixture200["body"]["pushed_at"])
    expected_etag = fixture200["headers"]["etag"]
    registry = _registry(workspace)
    try:
        _seed_links(registry, OWNER, REPO, 3)
        before = _link_count(workspace)
        client = FakeClient([(200, json.dumps(fixture200["body"]), fixture200["headers"])])
        enricher = _enricher(workspace, registry, client)
        fetched = enricher.enrich_pairs([(OWNER, REPO)])
        registry.flush(10.0)
    finally:
        registry.stop()

    assert fetched == 1
    assert len(client.calls) == 1

    row = _repo_row(workspace, OWNER, REPO)
    assert row is not None
    assert row["pushed_at"] == expected_pushed
    assert row["size_kb"] == fixture200["body"]["size"]
    assert row["default_branch"] == fixture200["body"]["default_branch"]
    assert row["etag"] == expected_etag
    assert row["fetched_at"] == NOW
    assert row["gone"] == 0

    links = _link_rows(workspace, OWNER, REPO)
    assert len(links) == 3
    assert all(link["repo_pushed_at"] == expected_pushed for link in links)
    assert all(link["repo_size_kb"] == fixture200["body"]["size"] for link in links)
    # UPDATE-only: the merge must never fabricate link rows.
    assert _link_count(workspace) == before


def test_s1_supplement_field_absent_fields_become_null(workspace):
    """SYNTHETIC supplement: opportunistic contract when GitHub trims fields."""
    registry = _registry(workspace)
    try:
        _seed_links(registry, OTHER_OWNER, OTHER_REPO, 1)
        # `pushed_at` present, but `size`/`default_branch` absent from payload.
        body = json.dumps({"name": "widgets", "pushed_at": _iso(NOW - HOUR)})
        client = FakeClient([(200, body, {"etag": "W/\"trimmed\""})])
        enricher = _enricher(workspace, registry, client)
        enricher.enrich_pairs([(OTHER_OWNER, OTHER_REPO)])
        registry.flush(10.0)
    finally:
        registry.stop()

    row = _repo_row(workspace, OTHER_OWNER, OTHER_REPO)
    assert row["pushed_at"] == _epoch(_iso(NOW - HOUR))
    assert row["size_kb"] is None
    assert row["default_branch"] is None
    assert row["etag"] == "W/\"trimmed\""

    [link] = _link_rows(workspace, OTHER_OWNER, OTHER_REPO)
    assert link["repo_pushed_at"] == _epoch(_iso(NOW - HOUR))
    assert link["repo_size_kb"] is None


# ---------------------------------------------------------------------------
# repo-meta-enrichment-S2
# ---------------------------------------------------------------------------
def test_s2_taken_down_repository_marks_gone(workspace):
    fixture404 = _read("live_2026_09_repos_404.json")
    owner, repo = fixture404["provenance"]["owner"], fixture404["provenance"]["repo"]
    registry = _registry(workspace)
    try:
        _seed_links(registry, owner, repo, 2)
        client = FakeClient(
            [
                (404, json.dumps(fixture404["body"]), {}),
                # Any further request would be a spec violation; the queue's
                # default 500 would surface loudly if one were attempted.
            ]
        )
        enricher = _enricher(workspace, registry, client)
        first = enricher.enrich_pairs([(owner, repo)])
        registry.flush(10.0)
        second = enricher.enrich_pairs([(owner, repo)])
        registry.flush(10.0)
    finally:
        registry.stop()

    assert first == 1
    assert second == 0
    assert len(client.calls) == 1  # no retry within TTL

    row = _repo_row(workspace, owner, repo)
    assert row is not None
    assert row["gone"] == 1
    assert row["pushed_at"] is None
    # The takedown must not delete or fabricate the repository's link rows.
    assert len(_link_rows(workspace, owner, repo)) == 2


# ---------------------------------------------------------------------------
# repo-meta-enrichment-S3
# ---------------------------------------------------------------------------
def test_s3_transient_failure_degrades_fail_open(workspace):
    registry = _registry(workspace)
    try:
        _seed_links(registry, OWNER, REPO, 2)
        client = FakeClient([RuntimeError("connection reset"), RuntimeError("connection reset")])
        enricher = _enricher(workspace, registry, client)
        first = enricher.enrich_pairs([(OWNER, REPO)])
        # A second distinct repository fails too; the warning stays warn-once.
        second = enricher.enrich_pairs([(OTHER_OWNER, OTHER_REPO)])
        registry.flush(10.0)
    finally:
        registry.stop()

    assert first == 0 and second == 0
    stats = enricher.to_stats()
    assert stats["enrichment_failures"] == 2
    assert stats["enrichment_warnings"] == 1

    # Fail-open: nothing persisted, link date columns remain NULL.
    assert _repo_row(workspace, OWNER, REPO) is None
    links = _link_rows(workspace, OWNER, REPO)
    assert len(links) == 2
    assert all(link["repo_pushed_at"] is None and link["repo_size_kb"] is None for link in links)

    # No exception escaped the enrichment path (implicit) and the counter is
    # queryable, which is all the pipeline needs to keep going.


# ---------------------------------------------------------------------------
# repo-meta-enrichment-S4
# ---------------------------------------------------------------------------
def test_s4_unchanged_repository_refreshes_via_304(workspace):
    stale_ts = NOW - 3 * DAY
    old_pushed = stale_ts - 10 * DAY
    registry = _registry(workspace)
    try:
        _seed_links(registry, OWNER, REPO, 2)
        registry.record_repo_upsert(
            OWNER,
            REPO,
            pushed_at=old_pushed,
            size_kb=7,
            default_branch="trunk",
            etag="E-OLD",
            ts=stale_ts,
        )
        registry.flush(10.0)

        client = FakeClient([(304, "", {"etag": "E-OLD"})])
        enricher = _enricher(workspace, registry, client)
        merge_calls = []
        original_merge = enricher.store.record_link_metadata
        enricher.store.record_link_metadata = lambda *a, **k: merge_calls.append((a, k)) or original_merge(*a, **k)
        fetched = enricher.enrich_pairs([(OWNER, REPO)])
        registry.flush(10.0)
    finally:
        registry.stop()

    assert fetched == 1
    assert client.headers_sent[0].get("if-none-match") == "E-OLD"
    assert merge_calls == []  # 304 requires no merge work

    row = _repo_row(workspace, OWNER, REPO)
    assert row["pushed_at"] == old_pushed
    assert row["size_kb"] == 7
    assert row["default_branch"] == "trunk"
    assert row["etag"] == "E-OLD"
    assert row["fetched_at"] == NOW  # touch-only bump

    stats = enricher.to_stats()
    assert stats["enrichment_304s"] == 1
    assert stats["enrichment_fetches"] == 1


# ---------------------------------------------------------------------------
# repo-meta-enrichment-S5
# ---------------------------------------------------------------------------
def test_s5_changed_repository_replaces_values(workspace):
    stale_ts = NOW - 3 * DAY
    old_pushed = stale_ts - 10 * DAY
    new_pushed = NOW - HOUR
    registry = _registry(workspace)
    try:
        _seed_links(registry, OWNER, REPO, 3)
        registry.record_repo_upsert(
            OWNER,
            REPO,
            pushed_at=old_pushed,
            size_kb=7,
            default_branch="trunk",
            etag="E-OLD",
            ts=stale_ts,
        )
        registry.flush(10.0)

        client = FakeClient([(200, _body(new_pushed, size=9, branch="main"), {"etag": "E-NEW"})])
        enricher = _enricher(workspace, registry, client)
        merge_calls = []
        original_merge = enricher.store.record_link_metadata

        def _merge_spy(*args, **kwargs):
            merge_calls.append((args, kwargs))
            return original_merge(*args, **kwargs)

        enricher.store.record_link_metadata = _merge_spy
        fetched = enricher.enrich_pairs([(OWNER, REPO)])
        registry.flush(10.0)
    finally:
        registry.stop()

    assert fetched == 1
    assert client.headers_sent[0].get("if-none-match") == "E-OLD"
    assert len(merge_calls) == 1  # merge enqueued for the changed repository

    row = _repo_row(workspace, OWNER, REPO)
    assert row["pushed_at"] == new_pushed
    assert row["size_kb"] == 9
    assert row["default_branch"] == "main"
    assert row["etag"] == "E-NEW"
    assert row["fetched_at"] == NOW

    links = _link_rows(workspace, OWNER, REPO)
    assert all(link["repo_pushed_at"] == new_pushed for link in links)
    assert all(link["repo_size_kb"] == 9 for link in links)


# ---------------------------------------------------------------------------
# repo-meta-enrichment-S12
# ---------------------------------------------------------------------------
def test_s12_crash_resume_completes_without_redo(workspace):
    registry = _registry(workspace)
    try:
        _seed_links(registry, OWNER, REPO, 1, prefix="a")
        _seed_links(registry, OTHER_OWNER, OTHER_REPO, 1, prefix="b")

        # First "run": repo A is persisted, then the process dies.
        client1 = FakeClient([(200, _body(NOW - HOUR, size=1), {"etag": "E-A"})])
        enricher1 = _enricher(workspace, registry, client1)
        assert enricher1.enrich_pairs([(OWNER, REPO)]) == 1
        registry.flush(10.0)
    finally:
        registry.stop()

    # Restart: fresh registry handle and fresh in-memory enricher state, same
    # on-disk WAL database.  Repo A is fresh within TTL, repo B is still unknown.
    registry2 = _registry(workspace)
    try:
        client2 = FakeClient([(200, _body(NOW - HOUR, size=2), {"etag": "E-B"})])
        enricher2 = _enricher(workspace, registry2, client2)
        fetched = enricher2.enrich_pairs([(OWNER, REPO), (OTHER_OWNER, OTHER_REPO)])
        registry2.flush(10.0)
    finally:
        registry2.stop()

    assert fetched == 1
    assert len(client2.calls) == 1
    assert client2.calls[0].endswith(f"/repos/{OTHER_OWNER}/{OTHER_REPO}")

    row_a = _repo_row(workspace, OWNER, REPO)
    row_b = _repo_row(workspace, OTHER_OWNER, OTHER_REPO)
    assert row_a is not None and row_a["etag"] == "E-A"
    assert row_b is not None and row_b["etag"] == "E-B"


# ---------------------------------------------------------------------------
# Supporting checks for the fetch function surface itself.
# ---------------------------------------------------------------------------
def test_fetch_repo_metadata_parses_live_200_fixture():
    fixture = _read("live_2026_09_repos_200.json")
    client = FakeClient([(200, json.dumps(fixture["body"]), fixture["headers"])])
    result = fetch_repo_metadata(OWNER, REPO, client=client, credential="api-token")
    assert result.status == 200
    assert result.pushed_at == _epoch(fixture["body"]["pushed_at"])
    assert result.size_kb == fixture["body"]["size"]
    assert result.default_branch == fixture["body"]["default_branch"]
    assert result.etag == fixture["headers"]["etag"]
    sent = client.headers_sent[0]
    assert sent.get("accept") == "application/vnd.github+json"
    assert sent.get("x-github-api-version") == "2022-11-28"
    assert sent.get("authorization") == "Bearer api-token"


def test_fetch_repo_metadata_404_returns_status_without_raising():
    fixture = _read("live_2026_09_repos_404.json")
    client = FakeClient([(404, json.dumps(fixture["body"]), {})])
    result = fetch_repo_metadata(OWNER, "ThisRepoDoesNotExist-xyz-123", client=client)
    assert result.status == 404
    assert result.pushed_at is None


def test_supplement_github_client_get_with_status_surfaces_304_and_404(monkeypatch):
    """SYNTHETIC supplement: the real transport must not raise on 304/404."""
    import contextlib

    from search import client as search_client

    class _FakeResponse:
        def __init__(self, status, content=b"", headers=None):
            self.status_code = status
            self.content = content
            self.headers = headers or {}

    for status, content, headers in (
        (200, b'{"size": 1, "default_branch": "main"}', {"ETag": "E"}),
        (304, b"", {"ETag": "E"}),
        (404, b'{"message": "Not Found"}', {}),
    ):
        monkeypatch.setattr(search_client, "request", lambda *a, _r=_FakeResponse(status, content, headers), **k: _r)
        monkeypatch.setattr(
            search_client, "managed_network", lambda response, kind: contextlib.nullcontext(response)
        )
        client = search_client.GitHubClient()
        observed, body, response_headers = client.get_with_status("https://api.github.com/repos/o/r")
        assert observed == status
        if status == 304:
            assert response_headers.get("ETag") == "E"
