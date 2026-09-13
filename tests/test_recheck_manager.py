"""Periodic re-check driver tests.

Traceability: key-ledger-S7, S8.

Harness: the ledger is seeded with expired keys across statuses; the manager
tick is invoked synchronously and the enqueued CheckTasks are inspected.  A
fake resolver stands in for the result-store lookup (the registry itself never
stores plaintext keys).
"""

import json
import os
import time

from core.models import Service
from storage.key_ledger import DEFAULT_TTL_HOURS, key_hash, mask_key

PROVIDER = "openai"
ADDRESS = "https://api.example.com"
ENDPOINT = "https://api.example.com/v1"
HOUR = 3600.0
DAY = 24 * HOUR
NOW = time.time()


def _seed(registry, key, status, last_recheck, marker, provider=PROVIDER, address=ADDRESS, endpoint=ENDPOINT):
    registry.record_key(
        key_hash(provider, key, address, endpoint),
        provider,
        mask_key(key),
        address=address,
        endpoint=endpoint,
        status=status,
        source_url_hash=marker,
        ts=last_recheck,
    )


def _resolver(row):
    # The registry stores no plaintext; embed the seeded marker so the test can
    # identify which rows were selected.
    return Service(
        key=f"{row['status']}:{row['source_url_hash']}",
        address=row["address"] or "",
        endpoint=row["endpoint"] or "",
    )


def _manager(workspace, enqueue, *, enabled=True, batch_size=50, resolver=_resolver):
    from manager.recheck import RecheckManager

    return RecheckManager(
        workspace=workspace,
        registry_path=os.path.join(workspace, "registry.sqlite"),
        ttl_hours=DEFAULT_TTL_HOURS,
        enabled=enabled,
        interval_hours=6.0,
        batch_size=batch_size,
        enqueue=enqueue,
        resolve=resolver,
        clock=lambda: NOW,
    )


def _seed_expired(registry):
    _seed(registry, "sk-wait-1-aaaaaaaaaaaaaaaaaaaaaaaaaaaa", "wait_check", NOW - 13 * HOUR, "wait-new")
    _seed(registry, "sk-wait-2-bbbbbbbbbbbbbbbbbbbbbbbbbbbb", "wait_check", NOW - 20 * HOUR, "wait-old")
    _seed(registry, "sk-valid-old-cccccccccccccccccccccccc", "valid", NOW - 20 * DAY, "valid-old")
    _seed(registry, "sk-valid-new-ddddddddddddddddddddddddd", "valid", NOW - 15 * DAY, "valid-new")
    _seed(registry, "sk-invalid-eeeeeeeeeeeeeeeeeeeeeeeeee", "invalid", NOW - 30 * DAY, "invalid")
    registry.flush(10.0)


# ---------------------------------------------------------------------------
# key-ledger-S7
# ---------------------------------------------------------------------------
def test_s7_expired_keys_are_requeued_by_priority(workspace, fast_registry):
    registry = fast_registry(workspace)
    try:
        _seed_expired(registry)
        tasks = []
        manager = _manager(workspace, tasks.append, batch_size=3)
        manager._execute_periodic_task()
    finally:
        registry.stop()

    assert len(tasks) == 3  # bounded batch
    statuses = [task.service.key.split(":", 1)[0] for task in tasks]
    assert statuses == ["wait_check", "wait_check", "valid"]
    # Both wait_check keys selected (oldest first), then the oldest valid key.
    assert [task.service.key for task in tasks] == [
        "wait_check:wait-old",
        "wait_check:wait-new",
        "valid:valid-old",
    ]
    assert manager.to_stats()["rechecks_enqueued"] == 3


# ---------------------------------------------------------------------------
# key-ledger-S8
# ---------------------------------------------------------------------------
def test_s8_cron_disabled_produces_no_background_checks(workspace, fast_registry):
    registry = fast_registry(workspace)
    try:
        _seed_expired(registry)
        tasks = []
        manager = _manager(workspace, tasks.append, enabled=False, batch_size=3)
        manager._execute_periodic_task()
    finally:
        registry.stop()

    assert tasks == []
    assert manager.to_stats()["rechecks_enqueued"] == 0


# ---------------------------------------------------------------------------
# Regression (staging run3): resolver injected as an OBJECT, real shard files
# ---------------------------------------------------------------------------
def test_resolver_object_recovers_plaintext_from_result_files(workspace, fast_registry):
    """Pipeline injects ``ShardKeyResolver(...)`` (an object, not a callable).

    The manager must accept it via ``.resolve`` and recover plaintext keys from
    both legacy bare-key ``.txt`` lines and ndjson ``{"value": ...}`` envelopes.
    Found by the live staging run: the lambda-based unit tests could not catch
    ``'ShardKeyResolver' object is not callable``.
    """
    from manager.recheck import RecheckManager, ShardKeyResolver

    registry = fast_registry(workspace)
    provider_dir = os.path.join(workspace, "providers", PROVIDER)
    shards_dir = os.path.join(provider_dir, "shards", "valid")
    os.makedirs(shards_dir, exist_ok=True)

    key_txt = "sk-live-object-resolver-aaaaaaaaaaaa"
    with open(os.path.join(provider_dir, "material.txt"), "w") as fh:
        fh.write(key_txt + "\n")

    key_json = "sk-live-object-resolver-bbbbbbbbbbbb"
    with open(os.path.join(shards_dir, "valid.ndjson"), "w") as fh:
        fh.write(json.dumps({"value": Service(key=key_json).serialize()}) + "\n")

    try:
        for plain in (key_txt, key_json):
            registry.record_key(
                key_hash(PROVIDER, plain, "", ""),
                PROVIDER,
                mask_key(plain),
                address="",
                endpoint="",
                status="wait_check",
                ts=NOW - 20 * HOUR,
            )
        registry.flush(10.0)

        tasks = []
        manager = RecheckManager(
            workspace=workspace,
            registry_path=os.path.join(workspace, "registry.sqlite"),
            ttl_hours=DEFAULT_TTL_HOURS,
            enabled=True,
            interval_hours=6.0,
            batch_size=10,
            enqueue=tasks.append,
            resolve=ShardKeyResolver(workspace),  # object with .resolve, not a lambda
            clock=lambda: NOW,
        )
        manager._execute_periodic_task()
    finally:
        registry.stop()

    stats = manager.to_stats()
    assert stats["unresolved"] == 0
    assert stats["rechecks_enqueued"] == 2
    assert sorted(t.service.key for t in tasks) == sorted([key_txt, key_json])
    assert all(t.provider == PROVIDER for t in tasks)
