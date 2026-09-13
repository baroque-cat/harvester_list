"""Inline skip, shard non-duplication, fail-open and flag-default tests.

Traceability: key-ledger-S3, S4, S5, S6, S10, S11.

Harness: a tmp registry pre-populated through the writer API with controlled
``last_recheck_ts`` ages; the real ``CheckStage`` worker is driven directly so
"provider NOT called" is verified on the mocked provider's call log.
"""

import logging
import os
import sqlite3
import time

from core.models import CheckResult, CheckTask, Service
from config.schemas import Config
from stage.base import StageResources
from stage.definition import CheckStage
from storage.key_ledger import DEFAULT_TTL_HOURS, KeyLedger, key_hash, mask_key

PROVIDER = "openai"
ADDRESS = "https://api.example.com"
ENDPOINT = "https://api.example.com/v1"
HOUR = 3600.0
DAY = 24 * HOUR
NOW = time.time()


def _service(key):
    return Service(key=key, address=ADDRESS, endpoint=ENDPOINT, model="gpt-4o")


def _task(key, source_url_hash="src"):
    return CheckTask(provider=PROVIDER, service=_service(key), source_url_hash=source_url_hash)


def _ledger(workspace, mode="on", clock=None, ttl_hours=None, degraded_cb=None):
    return KeyLedger(
        workspace=workspace,
        mode=mode,
        ttl_hours=ttl_hours or DEFAULT_TTL_HOURS,
        registry_path=os.path.join(workspace, "registry.sqlite"),
        run_id="run-key-ledger-test",
        clock=clock or (lambda: NOW),
        degraded_cb=degraded_cb,
    )


def _resources(registry, provider, ledger, limiter=None):
    return StageResources(
        limiter=limiter,
        providers={PROVIDER: provider},
        config=Config(),
        task_configs={},
        auth=None,
        registry=registry,
        key_ledger=ledger,
    )


def _seed(registry, key, status, last_recheck):
    registry.record_key(
        key_hash(PROVIDER, key, ADDRESS, ENDPOINT),
        PROVIDER,
        mask_key(key),
        address=ADDRESS,
        endpoint=ENDPOINT,
        status=status,
        ts=last_recheck,
    )
    registry.flush(10.0)


def _row(workspace, digest):
    conn = sqlite3.connect(os.path.join(workspace, "registry.sqlite"))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM keys WHERE key_hash = ?", (digest,)).fetchone()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# key-ledger-S3
# ---------------------------------------------------------------------------
def test_s3_fresh_valid_key_skips_the_provider_call(workspace, fast_registry, mock_provider, fake_limiter):
    registry = fast_registry(workspace)
    key = "sk-valid-AAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    provider = mock_provider(results=[CheckResult.success()])
    ledger = _ledger(workspace)
    try:
        _seed(registry, key, "valid", NOW - 2 * DAY)
        output = CheckStage(_resources(registry, provider, ledger, fake_limiter), lambda o: None).process_task(_task(key))
        registry.flush(10.0)
        row = _row(workspace, key_hash(PROVIDER, key, ADDRESS, ENDPOINT))
    finally:
        registry.stop()

    assert provider.calls == []  # provider NOT called
    assert output is not None and output.results == []  # no shard records
    stats = ledger.to_stats()
    assert stats["check_skipped_by_status"]["valid"] == 1
    assert stats["provider_calls_saved"] == 1
    assert row["last_seen_ts"] > NOW - 2 * DAY  # observation advanced


# ---------------------------------------------------------------------------
# key-ledger-S4
# ---------------------------------------------------------------------------
def test_s4_unknown_key_is_always_checked(workspace, fast_registry, mock_provider, fake_limiter):
    registry = fast_registry(workspace)
    key = "sk-unknown-BBBBBBBBBBBBBBBBBBBBBBBBBB"
    provider = mock_provider(results=[CheckResult.success()])
    ledger = _ledger(workspace)
    try:
        CheckStage(_resources(registry, provider, ledger, fake_limiter), lambda o: None).process_task(_task(key))
        registry.flush(10.0)
    finally:
        registry.stop()

    assert len(provider.calls) == 1
    assert ledger.to_stats()["provider_calls_saved"] == 0
    assert _row(workspace, key_hash(PROVIDER, key, ADDRESS, ENDPOINT))["status"] == "valid"


# ---------------------------------------------------------------------------
# key-ledger-S5
# ---------------------------------------------------------------------------
def test_s5_expired_wait_check_triggers_reverification(workspace, fast_registry, mock_provider, fake_limiter):
    registry = fast_registry(workspace)
    key = "sk-wait-CCCCCCCCCCCCCCCCCCCCCCCCCCCC"
    provider = mock_provider(results=[CheckResult.success()])
    ledger = _ledger(workspace)
    try:
        _seed(registry, key, "wait_check", NOW - 13 * HOUR)  # TTL 12h
        CheckStage(_resources(registry, provider, ledger, fake_limiter), lambda o: None).process_task(_task(key))
        registry.flush(10.0)
        row = _row(workspace, key_hash(PROVIDER, key, ADDRESS, ENDPOINT))
    finally:
        registry.stop()

    assert len(provider.calls) == 1
    assert row["status"] == "valid"  # outcome recorded


# ---------------------------------------------------------------------------
# key-ledger-S6
# ---------------------------------------------------------------------------
def test_s6_skipped_checks_do_not_duplicate_shard_records(workspace, fast_registry, mock_provider, fake_limiter):
    registry = fast_registry(workspace)
    key = "sk-dup-DDDDDDDDDDDDDDDDDDDDDDDDDDDD"
    provider = mock_provider(results=[CheckResult.success()])
    ledger = _ledger(workspace)
    try:
        _seed(registry, key, "valid", NOW - 3 * DAY)
        stage = CheckStage(_resources(registry, provider, ledger, fake_limiter), lambda o: None)
        for _ in range(3):
            output = stage.process_task(_task(key))
            assert output is not None
            assert output.results == []  # zero result records on every skip
            assert output.new_tasks == []
    finally:
        registry.stop()

    assert provider.calls == []
    assert ledger.to_stats()["check_skipped_by_status"]["valid"] == 3


# ---------------------------------------------------------------------------
# key-ledger-S10
# ---------------------------------------------------------------------------
def test_s10_ledger_outage_never_suppresses_verification(workspace, fast_registry, mock_provider, fake_limiter, monkeypatch, caplog):
    registry = fast_registry(workspace)
    key = "sk-outage-EEEEEEEEEEEEEEEEEEEEEEEEEE"
    provider = mock_provider(results=[CheckResult.success()])
    degraded = []
    ledger = _ledger(workspace, degraded_cb=lambda: degraded.append(True))
    try:
        _seed(registry, key, "valid", NOW - HOUR)

        def _boom(*args, **kwargs):
            raise sqlite3.OperationalError("simulated ledger outage")

        monkeypatch.setattr(ledger, "_lookup", _boom)
        with caplog.at_level(logging.WARNING, logger="storage"):
            CheckStage(_resources(registry, provider, ledger, fake_limiter), lambda o: None).process_task(_task(key))
    finally:
        registry.stop()

    assert len(provider.calls) == 1  # fail-open: checked anyway
    assert ledger.read_errors >= 1
    assert degraded  # run marked degraded
    assert any("ledger" in record.message.lower() for record in caplog.records)


# ---------------------------------------------------------------------------
# key-ledger-S11
# ---------------------------------------------------------------------------
def test_s11_defaults_preserve_current_behavior(workspace, fast_registry, mock_provider, fake_limiter):
    config = Config()
    assert config.check_skip.mode == "off"
    assert config.recheck.enabled is False

    registry = fast_registry(workspace)
    key = "sk-default-FFFFFFFFFFFFFFFFFFFFFFFF"
    provider = mock_provider(results=[CheckResult.success()])
    ledger = _ledger(workspace, mode=config.check_skip.mode)
    try:
        # Even with a fresh valid row present, the off flag never consults it.
        _seed(registry, key, "valid", NOW - HOUR)
        output = CheckStage(_resources(registry, provider, ledger, fake_limiter), lambda o: None).process_task(_task(key))
        registry.flush(10.0)
    finally:
        registry.stop()

    assert ledger.enabled is False
    assert len(provider.calls) == 1  # every arriving key is provider-checked
    assert output is not None and output.results != []
    # Recording still populates the keys table.
    assert _row(workspace, key_hash(PROVIDER, key, ADDRESS, ENDPOINT)) is not None


# ---------------------------------------------------------------------------
# Task 6.2 A/B: flags-off parity with the pre-change pipeline
# ---------------------------------------------------------------------------
def test_ab_flags_off_provider_call_parity(workspace, fast_registry, mock_provider, fake_limiter):
    """Pre-change resources (no ledger at all) vs post-change with mode=off:
    identical provider calls, identical shard-bound results, identical routing.
    Ledger recording still happens in the post-change arm (spec S11)."""
    registry = fast_registry(workspace)
    keys = [f"sk-ab-{i:04d}-ZZZZZZZZZZZZZZZZZZZZZZZZZZ" for i in range(5)]

    def _run(ledger):
        provider = mock_provider(results=[CheckResult.success()])
        stage = CheckStage(_resources(registry, provider, ledger, fake_limiter), lambda o: None)
        results, routed = [], []
        for key in keys:
            output = stage.process_task(_task(key))
            assert output is not None
            results.extend((p, rt, tuple(s.key for s in data)) for p, rt, data in output.results)
            routed.extend((t.provider, target) for t, target in output.new_tasks)
        return provider, results, routed

    try:
        provider_a, results_a, routed_a = _run(None)  # A: pre-change (no engine)
        provider_b, results_b, routed_b = _run(_ledger(workspace, mode="off"))  # B: flags off
        registry.flush(10.0)
        rows = [_row(workspace, key_hash(PROVIDER, k, ADDRESS, ENDPOINT)) for k in keys]
    finally:
        registry.stop()

    # Provider-call counts and payloads are unchanged by the feature existing.
    assert len(provider_a.calls) == len(provider_b.calls) == len(keys)
    assert [c["token"] for c in provider_a.calls] == [c["token"] for c in provider_b.calls]
    assert results_a == results_b
    assert routed_a == routed_b
    # Recording is flag-independent: every key has a ledger row.
    assert all(row is not None and row["status"] == "valid" for row in rows)
