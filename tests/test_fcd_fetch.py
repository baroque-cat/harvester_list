"""Per-file commit-date fetcher contract (file-commit-dating S1/S2/S4/S5/S6/S8/S9/S12/S13/S15).

All external shapes come from live-captured fixtures in
``tests/fixtures/file_dating/``.  Synthetic vectors are labelled as such.

Network is stubbed at the FakeClient seam (same pattern as
``test_repo_meta_enrichment.py``); no real HTTP.
"""

import json
import os
import datetime

import pytest

from core.exceptions import RateLimitDeferral, TransientFetchError
from search import client as sc

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "file_dating")


def _read_fixture(name: str):
    path = os.path.join(FIXTURE_DIR, name)
    with open(path, encoding="utf-8") as f:
        if name.endswith(".json"):
            return json.load(f)
        return f.read()


class FakeClient:
    """Minimal stand-in for the GitHubClient transport surface.

    Responses are ``(status, body, headers)`` tuples or exceptions.
    """

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []
        self.post_calls = []

    def get_with_status(self, url, headers=None, params=None, retries=3, interval=0, timeout=10, credential=None):
        self.calls.append({"url": url, "headers": headers, "params": params})
        item = self.responses.pop(0) if self.responses else (500, "", {})
        if isinstance(item, Exception):
            raise item
        return item

    def post_with_status(self, url, headers=None, json_data=None, retries=3, interval=0, timeout=10, credential=None):
        self.post_calls.append({"url": url, "headers": headers, "json_data": json_data})
        item = self.responses.pop(0) if self.responses else (500, "", {})
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def reset_stats():
    sc.reset_file_date_stats()
    yield
    sc.reset_file_date_stats()


# ---------------------------------------------------------------------------
# file-commit-dating-S1: date captured + normalised to UTC epoch seconds
# ---------------------------------------------------------------------------
def test_s1_date_captured_and_normalised():
    """Live fixture: REST commits response for Task.md."""
    body = _read_fixture("rest_commits_AP-Skill-Development-Corporation_AAD-Internship-2k21_Task.md.json")
    fake = FakeClient([(200, json.dumps(body), {"etag": "W/\"abc123\""})])

    result = sc.fetch_file_commit_date(
        "AP-Skill-Development-Corporation",
        "AAD-Internship-2k21",
        "Task.md",
        client=fake,
    )

    assert result.status == 200
    assert result.committed_date is not None
    # 2021-05-29T14:24:14Z → epoch
    expected = datetime.datetime(2021, 5, 29, 14, 24, 14, tzinfo=datetime.timezone.utc).timestamp()
    assert result.committed_date == expected
    assert result.etag == "W/\"abc123\""
    assert sc.get_file_date_stats()["file_dating_filled"] == 1
    assert sc.get_file_date_stats()["file_dating_requests"] == 1


# ---------------------------------------------------------------------------
# file-commit-dating-S2: well-formed response without usable date → no evidence
# ---------------------------------------------------------------------------
def test_s2_response_without_usable_date():
    """SYNTHETIC: well-formed list but empty."""
    fake = FakeClient([(200, json.dumps([]), {"etag": "W/\"x\""})])

    result = sc.fetch_file_commit_date("owner", "repo", "file.txt", client=fake)

    assert result.status == 200
    assert result.committed_date is None
    assert sc.get_file_date_stats()["file_dating_absent"] == 1
    assert sc.get_file_date_stats()["file_dating_filled"] == 0


def test_s2_missing_commit_object():
    """SYNTHETIC: list element without a commit object."""
    body = [{"sha": "abc", "no_commit_key": True}]
    fake = FakeClient([(200, json.dumps(body), {})])

    result = sc.fetch_file_commit_date("owner", "repo", "file.txt", client=fake)

    assert result.committed_date is None
    assert sc.get_file_date_stats()["file_dating_absent"] == 1


# ---------------------------------------------------------------------------
# file-commit-dating-S4: commit date earlier than repo creation is recorded as-is
# ---------------------------------------------------------------------------
def test_s4_date_predating_repo_creation():
    """Live fixture: INSTALL in asterisk-chan-dongle has date 2011-06-30 but repo created 2015-04-11."""
    body = _read_fixture("rest_commits_bg111_asterisk-chan-dongle_INSTALL.json")
    fake = FakeClient([(200, json.dumps(body), {})])

    result = sc.fetch_file_commit_date("bg111", "asterisk-chan-dongle", "INSTALL", client=fake)

    assert result.status == 200
    assert result.committed_date is not None
    # 2011-06-30T19:18:15Z
    expected = datetime.datetime(2011, 6, 30, 19, 18, 15, tzinfo=datetime.timezone.utc).timestamp()
    assert result.committed_date == expected
    # The date is before the repo's creation (2015-04-11), but that's expected
    assert result.committed_date < datetime.datetime(2015, 4, 11, tzinfo=datetime.timezone.utc).timestamp()


# ---------------------------------------------------------------------------
# file-commit-dating-S5: REST issues exactly one request per path
# ---------------------------------------------------------------------------
def test_s5_rest_one_request_per_path():
    body = _read_fixture("rest_commits_AP-Skill-Development-Corporation_AAD-Internship-2k21_Task.md.json")
    fake = FakeClient([(200, json.dumps(body), {})])

    sc.fetch_file_commit_date("AP-Skill-Development-Corporation", "AAD-Internship-2k21", "Task.md", client=fake)

    assert len(fake.calls) == 1
    assert "commits" in fake.calls[0]["url"]
    assert fake.calls[0]["params"]["path"] == "Task.md"
    assert fake.calls[0]["params"]["per_page"] == "1"


# ---------------------------------------------------------------------------
# file-commit-dating-S6: batch resolves several aliases in one request
# ---------------------------------------------------------------------------
def test_s6_batch_resolves_several_aliases():
    """Live fixture: GraphQL batch with 4 files."""
    body = _read_fixture("graphql_batch_4files.json")
    fake = FakeClient([(200, json.dumps(body), {})])

    items = [
        ("AP-Skill-Development-Corporation", "AAD-Internship-2k21", "Task.md", "a" * 40),
        ("BrainyMachines", "fgvc5-cvpr2018-food-fashion", "cmds.txt", "b" * 40),
        ("bg111", "asterisk-chan-dongle", "INSTALL", "c" * 40),
        ("SemEval", "SemEval2021", "tasks.md", "d" * 40),
    ]
    results = sc.fetch_file_commit_dates_batch(items, client=fake)

    assert len(fake.post_calls) == 1  # one request for all 4
    assert len(results) == 4
    # All should have dates
    for key, result in results.items():
        assert result.status == 200
        assert result.committed_date is not None
    assert sc.get_file_date_stats()["file_dating_batches"] == 1
    assert sc.get_file_date_stats()["file_dating_graphql_cost"] == 1


def test_s6_one_failing_alias_does_not_discard_others():
    """SYNTHETIC: one alias returns no data, others succeed."""
    body = {
        "data": {
            "f0": {"object": {"history": {"nodes": [{"committedDate": "2021-05-29T14:24:14Z"}]}}},
            "f1": {"object": {"history": {"nodes": []}}},  # no data
            "f2": {"object": {"history": {"nodes": [{"committedDate": "2018-06-04T12:59:03Z"}]}}},
            "rateLimit": {"cost": 1, "remaining": 4999},
        }
    }
    fake = FakeClient([(200, json.dumps(body), {})])

    items = [
        ("o1", "r1", "p1", "a" * 40),
        ("o2", "r2", "p2", "b" * 40),
        ("o3", "r3", "p3", "c" * 40),
    ]
    results = sc.fetch_file_commit_dates_batch(items, client=fake)

    assert len(results) == 3
    assert results[("o1", "r1", "p1")].committed_date is not None
    assert results[("o2", "r2", "p2")].committed_date is None  # no data
    assert results[("o3", "r3", "p3")].committed_date is not None


# ---------------------------------------------------------------------------
# file-commit-dating-S8: both transports yield the same recorded value
# ---------------------------------------------------------------------------
def test_s8_transports_yield_same_value():
    """Live fixtures: REST and GraphQL for the same file."""
    rest_body = _read_fixture("rest_commits_AP-Skill-Development-Corporation_AAD-Internship-2k21_Task.md.json")
    graphql_body = _read_fixture("graphql_batch_4files.json")

    # REST
    fake_rest = FakeClient([(200, json.dumps(rest_body), {})])
    rest_result = sc.fetch_file_commit_date(
        "AP-Skill-Development-Corporation",
        "AAD-Internship-2k21",
        "Task.md",
        client=fake_rest,
    )

    # GraphQL (f0 is the same file)
    fake_gql = FakeClient([(200, json.dumps(graphql_body), {})])
    gql_results = sc.fetch_file_commit_dates_batch(
        [("AP-Skill-Development-Corporation", "AAD-Internship-2k21", "Task.md", "a" * 40)],
        client=fake_gql,
    )
    gql_result = gql_results[("AP-Skill-Development-Corporation", "AAD-Internship-2k21", "Task.md")]

    assert rest_result.committed_date == gql_result.committed_date


# ---------------------------------------------------------------------------
# file-commit-dating-S9: batched traffic does not consume code-search budget
# ---------------------------------------------------------------------------
def test_s9_batch_uses_graphql_bucket():
    """The GraphQL endpoint must route to SERVICE_TYPE_GITHUB_GRAPHQL, not github_api."""
    from constant.system import SERVICE_TYPE_GITHUB_GRAPHQL, SERVICE_TYPE_GITHUB_API

    body = _read_fixture("graphql_batch_4files.json")
    fake = FakeClient([(200, json.dumps(body), {})])

    # Spy on _service to verify the bucket
    original_service = fake.post_with_status
    services_seen = []

    def spy_post(*args, **kwargs):
        # The URL is the first positional arg
        url = args[0] if args else kwargs.get("url", "")
        # We can't call fake._service directly, but we can check the URL
        services_seen.append(url)
        return original_service(*args, **kwargs)

    fake.post_with_status = spy_post

    items = [("o", "r", "p", "a" * 40)]
    sc.fetch_file_commit_dates_batch(items, client=fake)

    # Verify the URL is the GraphQL endpoint
    assert len(services_seen) == 1
    assert "graphql" in services_seen[0]

    # Verify the service type routing
    from search.client import GitHubClient
    gh = GitHubClient()
    assert gh._service("https://api.github.com/graphql") == SERVICE_TYPE_GITHUB_GRAPHQL
    assert gh._service("https://api.github.com/repos/o/r/commits") == SERVICE_TYPE_GITHUB_API
    assert SERVICE_TYPE_GITHUB_GRAPHQL != SERVICE_TYPE_GITHUB_API


# ---------------------------------------------------------------------------
# file-commit-dating-S12: 200-with-errors classified, never empty success
# ---------------------------------------------------------------------------
def test_s12_errors_inside_200_classified():
    """Live fixture: known-bad Blob.committedDate query returns 200 with errors."""
    body = _read_fixture("graphql_bad_blob_committeddate.json")
    fake = FakeClient([(200, json.dumps(body), {})])

    items = [("o", "r", "p", "a" * 40)]

    # The bad query has errors with code "cannotSpreadFragment" and "undefinedField"
    # These are not rate-limit or not-found, so they should be transient
    with pytest.raises(TransientFetchError):
        sc.fetch_file_commit_dates_batch(items, client=fake)


# ---------------------------------------------------------------------------
# file-commit-dating-S13: allowance exhaustion defers without burning attempts
# ---------------------------------------------------------------------------
def test_s13_rate_limit_defers_with_bounded_wait():
    """SYNTHETIC: GraphQL response with rate-limit error."""
    body = {
        "errors": [
            {
                "message": "API rate limit exceeded",
                "type": "RATE_LIMITED",
                "extensions": {"code": "RATE_LIMITED"},
            }
        ],
        "data": None,
    }
    fake = FakeClient([(200, json.dumps(body), {"retry-after": "30"})])

    items = [("o", "r", "p", "a" * 40)]

    with pytest.raises(RateLimitDeferral) as exc_info:
        sc.fetch_file_commit_dates_batch(items, client=fake)

    assert exc_info.value.wait_s <= 60.0  # bounded
    assert sc.get_file_date_stats()["file_dating_refusals"] >= 1


# ---------------------------------------------------------------------------
# file-commit-dating-S15: unrecognised error cause stays transient
# ---------------------------------------------------------------------------
def test_s15_unrecognised_error_stays_transient():
    """SYNTHETIC: GraphQL error with unrecognised code."""
    body = {
        "errors": [
            {
                "message": "Something weird happened",
                "extensions": {"code": "WEIRD_ERROR"},
            }
        ],
        "data": None,
    }
    fake = FakeClient([(200, json.dumps(body), {})])

    items = [("o", "r", "p", "a" * 40)]

    with pytest.raises(TransientFetchError):
        sc.fetch_file_commit_dates_batch(items, client=fake)
