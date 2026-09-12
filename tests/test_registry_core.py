"""Core registry behaviour: storage, lifecycle, coverage, journaling, fail-open.

Traceability: link-registry-S1..S3, S7, S9..S18.
"""

import json
import os

from storage.registry import Registry

URL = "https://github.com/o/r/blob/main/a.py"


def _open(workspace, enabled=True):
    registry = Registry(workspace, enabled=enabled)
    registry.start()
    return registry


def test_s1_registry_persists_across_restarts(workspace, db_scalar):
    """link-registry-S1: rows survive a graceful stop and reopen."""
    first = _open(workspace)
    first.record_link(URL, ts=100.0)
    first.stop()

    second = _open(workspace)
    # Rows must be queryable before the new instance writes anything.
    assert db_scalar(workspace, "SELECT COUNT(*) FROM links") == 1

    second.record_link("https://github.com/o/r/blob/main/b.py", ts=200.0)
    second.stop()

    assert db_scalar(workspace, "SELECT COUNT(*) FROM links") == 2


def test_s2_registry_is_shared_across_provider_set_changes(workspace, db_rows, db_scalar):
    """link-registry-S2: coverage rows keep per-provider attribution."""
    first = _open(workspace)
    first.start_run(run_id="run-a", config_digest=json.dumps({"providers": ["openai"]}))
    first.record_link(URL, ts=100.0)
    first.record_gather(URL, provider="openai", patterns_hash="ph-openai", success=True, ts=101.0)
    first.finish_run("run-a")
    first.stop()

    second = _open(workspace)
    second.start_run(run_id="run-b", config_digest=json.dumps({"providers": ["anthropic"]}))
    second.record_gather(URL, provider="anthropic", patterns_hash="ph-anthropic", success=True, ts=201.0)
    second.finish_run("run-b")
    second.stop()

    providers = {row["provider"] for row in db_rows(workspace, "SELECT provider FROM link_coverage")}
    assert providers == {"openai", "anthropic"}
    assert db_scalar(workspace, "SELECT COUNT(*) FROM links") == 1


def test_s3_schema_bootstrap_on_empty_workspace(workspace, db_rows, db_scalar, registry_path):
    """link-registry-S3: file, tables, indexes and user_version are created."""
    registry = _open(workspace)
    registry.stop()

    assert os.path.exists(registry_path(workspace))

    names = {row["name"] for row in db_rows(workspace, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"links", "link_coverage", "keys", "runs"} <= names

    indexes = {row["name"] for row in db_rows(workspace, "SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_links_repo", "idx_links_last_seen"} <= indexes

    assert db_scalar(workspace, "PRAGMA user_version") >= 1


def test_s7_flag_off_behaves_exactly_as_today(workspace, registry_path):
    """link-registry-S7: disabled registry never creates or opens a file."""
    registry = Registry(workspace, enabled=False)
    assert registry.start() is False

    # Hooks are no-ops and must not raise.
    registry.record_link(URL, ts=1.0)
    registry.record_gather(URL, provider="openai", patterns_hash="ph", success=True, ts=2.0)
    registry.mark_degraded()
    registry.stop()

    assert not os.path.exists(registry_path(workspace))


def test_s9_first_discovery_inserts(workspace, db_rows):
    """link-registry-S9: unseen URL inserted with discovered status."""
    registry = _open(workspace)
    registry.record_link(URL, transport="web", query_origin="some query", ts=111.0)
    registry.stop()

    row = db_rows(workspace, "SELECT * FROM links WHERE url = ?", [URL])[0]
    assert row["visit_status"] == "discovered"
    assert row["first_seen_ts"] == 111.0
    assert row["last_seen_ts"] == 111.0
    assert row["gathered_ts"] is None
    assert row["transport"] == "web"
    assert row["query_origin"] == "some query"


def test_s10_rediscovery_updates_only_recency_fields(workspace, db_rows, db_scalar):
    """link-registry-S10: last_seen advances; first_seen and row count stay."""
    registry = _open(workspace)
    registry.record_link(URL, ts=111.0)
    registry.record_link(URL, ts=222.0)
    registry.stop()

    assert db_scalar(workspace, "SELECT COUNT(*) FROM links WHERE url = ?", [URL]) == 1
    row = db_rows(workspace, "SELECT first_seen_ts, last_seen_ts FROM links WHERE url = ?", [URL])[0]
    assert row["first_seen_ts"] == 111.0
    assert row["last_seen_ts"] == 222.0


def test_s11_gather_outcome_is_recorded(workspace, db_rows):
    """link-registry-S11: success/failure recorded; gathered_ok never downgraded."""
    registry = _open(workspace)
    registry.record_link(URL, ts=1.0)
    registry.record_gather(URL, provider="openai", patterns_hash="ph", success=True, ts=5.0)
    # A later re-observation must not downgrade a gathered_ok row.
    registry.record_link(URL, ts=10.0)
    registry.stop()

    row = db_rows(workspace, "SELECT visit_status, gathered_ts FROM links WHERE url = ?", [URL])[0]
    assert row["visit_status"] == "gathered_ok"
    assert row["gathered_ts"] == 5.0

    failed_url = "https://github.com/o/r/blob/main/broken.py"
    failure = _open(workspace)
    failure.record_link(failed_url, ts=1.0)
    failure.record_gather(failed_url, provider="openai", patterns_hash="ph", success=False, ts=6.0)
    failure.stop()

    failed = db_rows(workspace, "SELECT visit_status, gathered_ts FROM links WHERE url = ?", [failed_url])[0]
    assert failed["visit_status"] == "failed"
    assert failed["gathered_ts"] is None


def test_s12_coverage_recorded_without_findings(workspace, db_rows, db_scalar):
    """link-registry-S12: a successful zero-finding gather still writes coverage."""
    registry = _open(workspace)
    registry.record_link(URL, ts=1.0)
    registry.record_gather(URL, provider="openai", patterns_hash="ph", success=True, ts=9.0)
    registry.stop()

    assert (
        db_scalar(
            workspace,
            "SELECT COUNT(*) FROM link_coverage WHERE provider = ? AND patterns_hash = ?",
            ["openai", "ph"],
        )
        == 1
    )
    row = db_rows(workspace, "SELECT gathered_ts FROM link_coverage WHERE provider = ?", ["openai"])[0]
    assert row["gathered_ts"] == 9.0


def test_s13_pattern_set_change_is_visible(workspace, db_rows):
    """link-registry-S13: a new patterns_hash adds a row; the old one remains."""
    registry = _open(workspace)
    registry.record_link(URL, ts=1.0)
    registry.record_gather(URL, provider="openai", patterns_hash="ph-v1", success=True, ts=9.0)
    registry.record_gather(URL, provider="openai", patterns_hash="ph-v2", success=True, ts=19.0)
    registry.stop()

    hashes = {
        row["patterns_hash"]
        for row in db_rows(workspace, "SELECT patterns_hash FROM link_coverage WHERE provider = ?", ["openai"])
    }
    assert hashes == {"ph-v1", "ph-v2"}


def test_s14_run_lifecycle_is_journaled(workspace, db_rows):
    """link-registry-S14: runs row carries timestamps and a parseable digest."""
    registry = _open(workspace)
    digest = json.dumps({"providers": ["openai"], "use_api": [False]})
    registry.start_run(run_id="run-1", config_digest=digest)
    registry.finish_run("run-1")
    registry.stop()

    row = db_rows(workspace, "SELECT * FROM runs WHERE run_id = ?", ["run-1"])[0]
    assert row["started_at"] is not None
    assert row["finished_at"] is not None
    assert json.loads(row["config_digest"])["providers"] == ["openai"]


def test_s15_degraded_run_is_marked(workspace, db_scalar, monkeypatch):
    """link-registry-S15: a failed write marks that run degraded."""
    registry = _open(workspace)
    registry.start_run(run_id="run-degraded", config_digest="{}")

    def boom(_batch):
        raise RuntimeError("simulated disk failure")

    monkeypatch.setattr(registry, "_flush_batch", boom)

    registry.record_link(URL, ts=1.0)
    registry.flush()
    assert registry.degraded is True

    registry.finish_run("run-degraded")
    registry.stop()

    assert db_scalar(workspace, "SELECT degraded FROM runs WHERE run_id = ?", ["run-degraded"]) == 1


def test_s16_corrupt_database_does_not_stop_the_pipeline(workspace, registry_path):
    """link-registry-S16: a corrupt file degrades to a no-op, never raises."""
    path = registry_path(workspace)
    with open(path, "wb") as f:
        f.write(b"this is definitely not a sqlite database")

    registry = Registry(workspace, enabled=True)
    assert registry.start() is False
    assert registry.available is False
    assert registry.degraded is True

    # Hooks must remain safe no-ops.
    registry.record_link(URL, ts=1.0)
    registry.record_gather(URL, provider="openai", patterns_hash="ph", success=True, ts=2.0)
    registry.stop()


def test_s17_mid_run_write_failure_degrades_silently(workspace, db_scalar, monkeypatch):
    """link-registry-S17: a failed batch suppresses writes and degrades."""
    registry = _open(workspace)

    def boom(_batch):
        raise RuntimeError("simulated disk full")

    monkeypatch.setattr(registry, "_flush_batch", boom)

    registry.record_link(URL, ts=1.0)
    registry.flush()  # must not raise into stage code

    registry.record_link("https://github.com/o/r/blob/main/b.py", ts=2.0)
    registry.flush()
    assert registry.degraded is True
    registry.stop()

    # Suppressed writes never reached the database.
    assert db_scalar(workspace, "SELECT COUNT(*) FROM links") == 0


def test_s18_graceful_stop_drains_pending_writes(workspace, db_scalar):
    """link-registry-S18: stop() leaves every queued row queryable."""
    registry = _open(workspace)
    urls = [f"https://github.com/o/r/blob/main/file{i}.py" for i in range(200)]
    for i, url in enumerate(urls):
        registry.record_link(url, ts=float(i))
    registry.stop()

    assert db_scalar(workspace, "SELECT COUNT(*) FROM links") == len(urls)


def test_pipeline_link_registry_does_not_shadow_stage_registry(workspace):
    """Regression guard: `link_registry` must not clobber the stage-registry property."""
    from config.schemas import Config
    from manager.pipeline import Pipeline
    from storage.registry import shutdown_registry

    config = Config()
    config.global_config.workspace = workspace
    config.persistence.simple = True  # avoid periodic snapshot threads

    pipeline = Pipeline(config, providers={})
    try:
        pipeline.start()
        assert pipeline.link_registry is not None
        # The inherited stage-registry property must remain functional.
        assert pipeline.registry is not None
        assert pipeline.registry is not pipeline.link_registry
    finally:
        pipeline.stop()
        shutdown_registry()


def test_pipeline_run_journaling_is_wired(workspace, db_rows):
    """Task 3.5 wiring: Pipeline.start/finish_registry_run journal one run."""
    from config.schemas import Config
    from manager.pipeline import Pipeline
    from storage.registry import shutdown_registry

    config = Config()
    config.global_config.workspace = workspace
    config.persistence.simple = True
    config.registry.enabled = True

    pipeline = Pipeline(config, providers={})
    try:
        pipeline.start()
        pipeline.start_registry_run()
        pipeline.finish_registry_run()
    finally:
        pipeline.stop()
        shutdown_registry()

    row = db_rows(workspace, "SELECT started_at, finished_at FROM runs")[0]
    assert row["started_at"] is not None
    assert row["finished_at"] is not None
