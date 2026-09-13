"""Ledger-recording and secret-hygiene tests for add-key-ledger.

Traceability: key-ledger-S1, key-ledger-S2, key-ledger-S9.

Harness: a tmp registry plus a mocked provider; the real ``CheckStage`` worker
is driven directly with synthetic ``CheckTask``s.  No network, no live GitHub.
"""

import glob
import os
import sqlite3

from core.enums import ErrorReason
from core.models import CheckResult, CheckTask, Service
from config.schemas import Config
from stage.base import StageResources
from stage.definition import CheckStage
from storage.key_ledger import key_hash, mask_key

PROVIDER = "openai"
KEY = "sk-canary-abcdefghijklmnopqrstuvwxyz0123456789"
ADDRESS = "https://api.example.com"
ENDPOINT = "https://api.example.com/v1"
SOURCE_HASH = "source-url-hash-1"
NOW = 1_700_000_000.0
HOUR = 3600.0


def _task(service, source_url_hash=SOURCE_HASH):
    return CheckTask(provider=PROVIDER, service=service, source_url_hash=source_url_hash)


def _resources(registry, provider, ledger=None, limiter=None):
    return StageResources(
        limiter=limiter,
        providers={PROVIDER: provider},
        config=Config(),
        task_configs={PROVIDER: None},
        auth=None,
        registry=registry,
        key_ledger=ledger,
    )


def _stage(registry, provider, ledger=None, limiter=None):
    return CheckStage(_resources(registry, provider, ledger, limiter), lambda output: None)


def _service(key=KEY, address=ADDRESS, endpoint=ENDPOINT):
    return Service(key=key, address=address, endpoint=endpoint, model="gpt-4o")


def _key_row(workspace, digest):
    conn = sqlite3.connect(os.path.join(workspace, "registry.sqlite"))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM keys WHERE key_hash = ?", (digest,)).fetchone()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# key-ledger-S1
# ---------------------------------------------------------------------------
def test_s1_first_verification_creates_the_ledger_row(workspace, fast_registry, mock_provider, fake_limiter):
    registry = fast_registry(workspace)
    provider = mock_provider(results=[CheckResult.success()])
    try:
        output = _stage(registry, provider, limiter=fake_limiter).process_task(_task(_service()))
        registry.flush(10.0)
    finally:
        registry.stop()

    assert output is not None
    digest = key_hash(PROVIDER, KEY, ADDRESS, ENDPOINT)
    row = _key_row(workspace, digest)
    assert row is not None
    assert row["status"] == "valid"
    assert row["key_ref_masked"] == mask_key(KEY)
    assert row["provider"] == PROVIDER
    assert row["address"] == ADDRESS
    assert row["endpoint"] == ENDPOINT
    assert row["source_url_hash"] == SOURCE_HASH

    # first_seen == last_recheck == last_seen (within tolerance)
    assert row["first_seen_ts"] is not None
    assert abs(row["first_seen_ts"] - row["last_recheck_ts"]) < 1.0
    assert abs(row["first_seen_ts"] - row["last_seen_ts"]) < 1.0


# ---------------------------------------------------------------------------
# key-ledger-S2
# ---------------------------------------------------------------------------
def test_s2_reverification_updates_status_preserves_origin(workspace, fast_registry, mock_provider, fake_limiter):
    registry = fast_registry(workspace)
    digest = key_hash(PROVIDER, KEY, ADDRESS, ENDPOINT)
    original = NOW - 1000.0
    try:
        # Seed a previously seen wait_check key.
        registry.record_key(
            digest,
            PROVIDER,
            mask_key(KEY),
            address=ADDRESS,
            endpoint=ENDPOINT,
            status="wait_check",
            ts=original,
        )
        registry.flush(10.0)

        provider = mock_provider(results=[CheckResult.success()])
        _stage(registry, provider, limiter=fake_limiter).process_task(_task(_service()))
        registry.flush(10.0)
    finally:
        registry.stop()

    row = _key_row(workspace, digest)
    assert row["status"] == "valid"
    assert abs(row["first_seen_ts"] - original) < 0.01  # origin preserved
    assert row["last_recheck_ts"] > original  # recency advanced


# ---------------------------------------------------------------------------
# key-ledger-S9
# ---------------------------------------------------------------------------
def test_s9_database_contains_no_plaintext_secrets(workspace, fast_registry, mock_provider, fake_limiter):
    canaries = [
        "sk-canary-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "sk-canary-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
    ]
    registry = fast_registry(workspace)
    provider = mock_provider(results=[CheckResult.success()])
    try:
        for canary in canaries:
            service = Service(key=canary, address=ADDRESS, endpoint=ENDPOINT)
            # Debug dumps / log interpolation must not render the secret either.
            assert canary not in repr(service)
            assert canary not in repr(_task(service))
            _stage(registry, provider, limiter=fake_limiter).process_task(_task(service))
        registry.flush(10.0)
    finally:
        registry.stop()

    # Raw byte scan of the database and its WAL sidecar.
    blobs = []
    for suffix in ("", "-wal", "-shm"):
        path = os.path.join(workspace, "registry.sqlite") + suffix
        if os.path.exists(path):
            with open(path, "rb") as handle:
                blobs.append(handle.read())
    for decision_log in glob.glob(os.path.join(workspace, "*.jsonl")):
        with open(decision_log, "rb") as handle:
            blobs.append(handle.read())

    raw = b"".join(blobs)
    for canary in canaries:
        assert canary.encode("utf-8") not in raw

    # Hashes and masks are present (the ledger is populated, just redacted).
    for canary in canaries:
        digest = key_hash(PROVIDER, canary, ADDRESS, ENDPOINT)
        assert digest.encode("ascii") in raw
        assert mask_key(canary).encode("utf-8") in raw
