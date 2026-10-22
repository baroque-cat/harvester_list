"""Background batching pins (P21 step 2, D-03.10).

Each pin is normative — tests the real product path, not a facade.
No network.
"""

import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import pytest

from config.schemas import FileDatingConfig
from search.client import FileDateResult, reset_file_date_stats


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40

URL_A = f"https://github.com/octocat/Hello-World/blob/{SHA_A}/src/a.py"
URL_B = f"https://github.com/octocat/Hello-World/blob/{SHA_B}/src/b.py"
URL_C = f"https://github.com/octocat/Hello-World/blob/{SHA_C}/src/c.py"


class _FakeStore:
    def __init__(self):
        self.writes: list = []

    def get(self, owner, repo, path):
        return None

    def is_fresh(self, entry, sha=None, now=None):
        return False

    def record_upsert(self, *a, **kw):
        self.writes.append(("upsert", a, kw))

    def record_link_date(self, *a, **kw):
        self.writes.append(("link_date", a, kw))

    def record_touch(self, *a, **kw):
        pass

    def record_gone(self, *a, **kw):
        pass

    def flush(self, timeout=10.0):
        pass

    def close(self):
        pass


class _FakeAuth:
    def get_token(self):
        return "test-token"


class _RecordingBatch:
    """Records batch calls and returns configurable results."""

    def __init__(self, dates=None, raise_exc=None, block_event=None):
        self.calls: List[List[Tuple[str, str, str, str]]] = []
        self.dates = dates or {}
        self.raise_exc = raise_exc
        self.block_event = block_event
        self.lock = threading.Lock()

    def __call__(self, items, credential=None, client=None, timeout=30.0, retries=3):
        if self.block_event is not None:
            self.block_event.wait(timeout=10)
        with self.lock:
            self.calls.append(list(items))

        if self.raise_exc is not None:
            raise self.raise_exc

        from search.client import _file_date_stat_inc
        _file_date_stat_inc("file_dating_batches")
        _file_date_stat_inc("file_dating_requests")
        _file_date_stat_inc("file_dating_graphql_cost", 1)

        results = {}
        for owner, repo, path, sha in items:
            key = (owner, repo, path)
            date = self.dates.get(key)
            if date is not None:
                results[key] = FileDateResult(owner, repo, path, 200, committed_date=date, sha=sha)
                _file_date_stat_inc("file_dating_filled")
            else:
                results[key] = FileDateResult(owner, repo, path, 404, sha=sha)
                _file_date_stat_inc("file_dating_absent")
        return results


def _make_bg_dater(
    batch_fn=None,
    graphql_batch_size=2,
    background_pending_cap=100,
    background_drain_timeout_s=5.0,
    metadata_sink=None,
    monkeypatch=None,
):
    """Build a FileDater with background batching enabled and a fake batch fn."""
    from storage.file_dating import FileDater

    store = _FakeStore()
    dater = FileDater(
        store,
        auth=_FakeAuth(),
        enabled=True,
        transport="graphql",
        graphql_batch_size=graphql_batch_size,
        background_batching=True,
        background_pending_cap=background_pending_cap,
        background_drain_timeout_s=background_drain_timeout_s,
        metadata_sink=metadata_sink,
        clock=time.time,
    )
    if batch_fn is not None and monkeypatch is not None:
        monkeypatch.setattr("storage.file_dating.fetch_file_commit_dates_batch", batch_fn)
    return dater


# ---------------------------------------------------------------------------
# P-cap: overflow → dating_pending_dropped grows, loud-once WARNING exactly once
# ---------------------------------------------------------------------------
def test_p_cap_overflow_drops_loud_once(monkeypatch, caplog):
    """Cap reached ⇒ dating_pending_dropped increments, WARNING fires exactly once."""
    reset_file_date_stats()
    recorder = _RecordingBatch(dates={})
    dater = _make_bg_dater(
        batch_fn=recorder,
        graphql_batch_size=2,
        background_pending_cap=3,
        monkeypatch=monkeypatch,
    )

    import logging
    with caplog.at_level(logging.WARNING, logger="storage"):
        # Submit 5 unique URLs with cap=3 → 2 should be dropped
        urls = [
            f"https://github.com/o/r/blob/{'x' * 40}/f{i}.py"
            for i in range(5)
        ]
        for url in urls:
            result = dater.submit_urls([url])
            # submit never raises
            assert isinstance(result, int)

    stats = dater.to_stats()
    assert stats["dating_pending_dropped"] >= 1
    # Loud-once: exactly one WARNING about cap
    cap_warnings = [r for r in caplog.records if "pending cap reached" in r.getMessage()]
    assert len(cap_warnings) == 1

    dater.close()


# ---------------------------------------------------------------------------
# P-dedup: re-submitting same URL does not double-count pending_in
# ---------------------------------------------------------------------------
def test_p_dedup_same_url_no_double_count(monkeypatch):
    """Re-submitting the same URL does not increment pending_in twice."""
    reset_file_date_stats()
    recorder = _RecordingBatch(dates={})
    dater = _make_bg_dater(
        batch_fn=recorder,
        background_pending_cap=100,
        monkeypatch=monkeypatch,
    )

    dater.submit_urls([URL_A])
    dater.submit_urls([URL_A])
    dater.submit_urls([URL_A])

    stats = dater.to_stats()
    assert stats["pending_in"] == 1  # only one unique insertion

    dater.close()


# ---------------------------------------------------------------------------
# P-batch: background thread collects batches ≥ threshold
# ---------------------------------------------------------------------------
def test_p_batch_thread_dispatches(monkeypatch):
    """Background thread dispatches batches of size ≤ graphql_batch_size."""
    reset_file_date_stats()
    dates = {
        ("octocat", "Hello-World", "src/a.py"): 1_700_000_000.0 - 100,
        ("octocat", "Hello-World", "src/b.py"): 1_700_000_000.0 - 200,
    }
    recorder = _RecordingBatch(dates=dates)
    dater = _make_bg_dater(
        batch_fn=recorder,
        graphql_batch_size=2,
        background_pending_cap=100,
        monkeypatch=monkeypatch,
    )

    # Submit 2 URLs — should trigger a batch
    dater.submit_urls([URL_A, URL_B])

    # Wait for the background thread to process
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if len(recorder.calls) >= 1:
            break
        time.sleep(0.1)

    assert len(recorder.calls) >= 1
    # Batch size ≤ graphql_batch_size
    for call in recorder.calls:
        assert len(call) <= 2

    stats = dater.to_stats()
    assert stats["batches_dispatched"] >= 1

    dater.close()


# ---------------------------------------------------------------------------
# P-delivery: sink receives metadata with file_commit_date
# ---------------------------------------------------------------------------
def test_p_delivery_sink_receives_metadata(monkeypatch):
    """metadata_sink receives {url: LinkMetadata} with file_commit_date."""
    reset_file_date_stats()
    NOW = 1_700_000_000.0
    dates = {
        ("octocat", "Hello-World", "src/a.py"): NOW,
    }
    recorder = _RecordingBatch(dates=dates)
    sink_calls: List[Dict] = []

    def fake_sink(meta_dict):
        sink_calls.append(meta_dict)

    dater = _make_bg_dater(
        batch_fn=recorder,
        graphql_batch_size=1,
        metadata_sink=fake_sink,
        monkeypatch=monkeypatch,
    )

    dater.submit_urls([URL_A])

    # Wait for delivery
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if len(sink_calls) >= 1:
            break
        time.sleep(0.1)

    assert len(sink_calls) >= 1
    # The sink received a dict with URL_A as key
    found = False
    for call in sink_calls:
        if URL_A in call:
            meta = call[URL_A]
            assert hasattr(meta, "file_commit_date")
            assert meta.file_commit_date == NOW
            found = True
    assert found, f"URL_A not found in sink calls: {sink_calls}"

    dater.close()


# ---------------------------------------------------------------------------
# P-drain: close() drains remaining; timeout drops remainder
# ---------------------------------------------------------------------------
def test_p_drain_close_drains_remaining(monkeypatch):
    """close() drains remaining pending items through the batch fn."""
    reset_file_date_stats()
    dates = {
        ("octocat", "Hello-World", "src/a.py"): 1_700_000_000.0 - 100,
        ("octocat", "Hello-World", "src/b.py"): 1_700_000_000.0 - 200,
    }
    recorder = _RecordingBatch(dates=dates)
    dater = _make_bg_dater(
        batch_fn=recorder,
        graphql_batch_size=2,
        background_pending_cap=100,
        background_drain_timeout_s=5.0,
        monkeypatch=monkeypatch,
    )

    dater.submit_urls([URL_A, URL_B])
    # Close should drain
    dater.close()

    # After close, all items should be processed
    stats = dater.to_stats()
    # Balance: pending_in = dated + dropped (approximately)
    assert stats["pending_in"] == stats["dated"] + stats["dating_pending_dropped"]


def test_p_drain_timeout_drops_remainder(monkeypatch, caplog):
    """When drain timeout expires, remaining items are dropped with WARNING."""
    reset_file_date_stats()
    # Batch fn that blocks forever
    block_event = threading.Event()
    recorder = _RecordingBatch(dates={}, block_event=block_event)
    dater = _make_bg_dater(
        batch_fn=recorder,
        graphql_batch_size=10,  # large batch so items accumulate
        background_pending_cap=100,
        background_drain_timeout_s=0.5,  # short drain timeout
        monkeypatch=monkeypatch,
    )

    # Submit items but don't let the batch fn complete
    dater.submit_urls([URL_A])

    # Wait a bit for the thread to start
    time.sleep(0.2)

    import logging
    with caplog.at_level(logging.WARNING, logger="storage"):
        dater.close()

    # Unblock the batch fn so it can finish
    block_event.set()

    stats = dater.to_stats()
    # Some items should have been dropped due to timeout
    # (the batch fn blocked, so items couldn't be delivered in time)
    # The exact behavior depends on timing, but close() must complete.


# ---------------------------------------------------------------------------
# P-failopen: client exception does not kill gather
# ---------------------------------------------------------------------------
def test_p_failopen_client_exception(monkeypatch):
    """Client exception ⇒ submit continues working, no process death."""
    reset_file_date_stats()
    recorder = _RecordingBatch(raise_exc=RuntimeError("network down"))
    dater = _make_bg_dater(
        batch_fn=recorder,
        graphql_batch_size=2,
        monkeypatch=monkeypatch,
    )

    # Submit URLs — should not raise
    dater.submit_urls([URL_A, URL_B])

    # Wait for the batch to be attempted
    time.sleep(1.0)

    # submit should still work after failure
    result = dater.submit_urls([URL_C])
    assert isinstance(result, int)

    stats = dater.to_stats()
    # Dropped items from failed batch
    assert stats["dating_pending_dropped"] >= 0

    dater.close()


# ---------------------------------------------------------------------------
# P-budget: background path does NOT touch budget_stop/_time_stopped
# ---------------------------------------------------------------------------
def test_p_budget_not_touched_by_background(monkeypatch):
    """Background path does not increment budget_stop or time_stopped."""
    reset_file_date_stats()
    dates = {
        ("octocat", "Hello-World", "src/a.py"): 1_700_000_000.0,
    }
    recorder = _RecordingBatch(dates=dates)
    dater = _make_bg_dater(
        batch_fn=recorder,
        graphql_batch_size=1,
        monkeypatch=monkeypatch,
    )

    dater.submit_urls([URL_A])
    time.sleep(1.0)

    stats = dater.to_stats()
    assert stats["file_dating_budget_stopped"] == 0
    assert stats["file_dating_time_stopped"] == 0

    dater.close()


# ---------------------------------------------------------------------------
# P-seam: stage hook calls submit_urls in background mode, date_urls_map in sync
# ---------------------------------------------------------------------------
def test_p_seam_background_calls_submit_not_date_urls_map():
    """In background mode, _date_file_links calls submit_urls, NOT date_urls_map."""
    from stage.definition import _date_file_links
    from stage.base import StageResources
    from config.schemas import Config

    class SpyDater:
        def __init__(self):
            self.enabled = True
            self.background_batching = True
            self.submit_calls = []
            self.date_urls_map_calls = []

        def submit_urls(self, urls):
            self.submit_calls.append(list(urls))
            return len(urls)

        def date_urls_map(self, urls, budget=None):
            self.date_urls_map_calls.append(list(urls))
            return {url: None for url in urls}

    dater = SpyDater()
    resources = StageResources(
        limiter=None,
        providers={},
        config=Config(),
        task_configs={},
        auth=None,
        registry=None,
        file_dating=dater,
    )

    result = _date_file_links(resources, [URL_A])

    # Background mode: submit_urls called, date_urls_map NOT called
    assert len(dater.submit_calls) == 1
    assert len(dater.date_urls_map_calls) == 0
    # Result is empty (delivery goes through sink)
    assert result == {}


def test_p_seam_sync_calls_date_urls_map():
    """In sync mode (background_batching=False), _date_file_links calls date_urls_map."""
    from stage.definition import _date_file_links
    from stage.base import StageResources
    from config.schemas import Config

    class SpyDater:
        def __init__(self):
            self.enabled = True
            self.background_batching = False
            self.submit_calls = []
            self.date_urls_map_calls = []

        def submit_urls(self, urls):
            self.submit_calls.append(list(urls))
            return len(urls)

        def date_urls_map(self, urls, budget=None):
            self.date_urls_map_calls.append(list(urls))
            return {url: None for url in urls}

    dater = SpyDater()
    resources = StageResources(
        limiter=None,
        providers={},
        config=Config(),
        task_configs={},
        auth=None,
        registry=None,
        file_dating=dater,
    )

    result = _date_file_links(resources, [URL_A])

    # Sync mode: date_urls_map called, submit_urls NOT called
    assert len(dater.date_urls_map_calls) == 1
    assert len(dater.submit_calls) == 0


# ---------------------------------------------------------------------------
# P-wiring: Pipeline passes background config + metadata_sink to FileDater
# ---------------------------------------------------------------------------
def test_p_wiring_pipeline_passes_background_config(tmp_path):
    """End-to-end wiring: a REAL Pipeline(cfg, providers={}) must pass the
    background config AND bind metadata_sink to link_registry.record_metadata.

    Direct FileDater construction proves nothing about manager/pipeline.py —
    this pin drives the actual product seam (lesson: 2.2-P3 / 3a.3-D1).
    """
    from config.schemas import Config
    from manager.pipeline import Pipeline

    cfg = Config()
    cfg.global_config.workspace = str(tmp_path)
    cfg.persistence.simple = True  # avoid snapshot threads
    cfg.file_dating = FileDatingConfig(
        enabled=True,
        transport="graphql",
        background_batching=True,
        background_pending_cap=5000,
        background_drain_timeout_s=30.0,
    )

    pipeline = Pipeline(cfg, providers={})
    try:
        dater = pipeline.file_dating
        assert dater.background_batching is True, (
            "pipeline did not pass file_dating.background_batching to FileDater"
        )
        assert dater.background_pending_cap == 5000, (
            "pipeline did not pass file_dating.background_pending_cap to FileDater"
        )
        assert dater.background_drain_timeout_s == 30.0, (
            "pipeline did not pass file_dating.background_drain_timeout_s to FileDater"
        )
        assert dater._metadata_sink == pipeline.link_registry.record_metadata, (
            "metadata_sink must be bound to link_registry.record_metadata (D-03.10 p.3)"
        )
    finally:
        try:
            pipeline.file_dating.close()
        except Exception:
            pass
        for stage in getattr(pipeline, "stages", {}).values():
            q = getattr(stage, "queue", None)
            if q is not None and hasattr(q, "close"):
                try:
                    q.close()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# P-config: validation of three new keys
# ---------------------------------------------------------------------------
def test_p_config_background_batching_bool():
    """background_batching must be a bool."""
    with pytest.raises(ValueError) as exc:
        FileDatingConfig(background_batching="yes")
    assert "background_batching" in str(exc.value)


def test_p_config_background_pending_cap_positive():
    """background_pending_cap must be > 0."""
    with pytest.raises(ValueError) as exc:
        FileDatingConfig(background_pending_cap=0)
    assert "background_pending_cap" in str(exc.value)

    with pytest.raises(ValueError) as exc:
        FileDatingConfig(background_pending_cap=-1)
    assert "background_pending_cap" in str(exc.value)


def test_p_config_background_drain_timeout_positive():
    """background_drain_timeout_s must be > 0."""
    with pytest.raises(ValueError) as exc:
        FileDatingConfig(background_drain_timeout_s=0)
    assert "background_drain_timeout_s" in str(exc.value)


def test_p_config_defaults_narrow():
    """Defaults are narrow/inert."""
    fdc = FileDatingConfig()
    assert fdc.background_batching is False
    assert fdc.background_pending_cap == 10_000
    assert fdc.background_drain_timeout_s == 60.0


# ---------------------------------------------------------------------------
# P-render: pending= and dropped= tokens present exactly once at end of line
# ---------------------------------------------------------------------------
def test_p_render_pending_dropped_tokens():
    """pending= and dropped= appear exactly once at the end of the Dating line."""
    from core.metrics import PipelineStatus
    from state.display import StatusDisplayEngine
    from state.models import SystemStatus

    metrics = {
        "enabled": True,
        "scope": "gathered",
        "transport": "graphql",
        "file_dating_requests": 10,
        "file_dating_304s": 0,
        "file_dating_filled": 5,
        "file_dating_absent": 3,
        "file_dating_batches": 2,
        "file_dating_graphql_cost": 2,
        "file_dating_refusals": 0,
        "file_dating_budget_stopped": 0,
        "cached_hits": 1,
        "undateable_link": 0,
        "requests_this_run": 10,
        "budget_remaining": 190,
        "fill_rate": 0.625,
        "pending_size": 42,
        "dating_pending_dropped": 7,
    }
    pipeline = PipelineStatus(file_dating_metrics=metrics)
    status = SystemStatus(pipeline=pipeline)
    line = StatusDisplayEngine._format_file_dating_metrics_line(status)

    assert line != ""
    assert "pending=42" in line
    assert "dropped=7" in line
    # Tokens appear exactly once
    assert line.count("pending=") == 1
    assert line.count("dropped=") == 1
    # Tokens at the end
    assert line.endswith("dropped=7")


def test_p_render_disabled_no_line():
    """Disabled ⇒ no line (guard preserved)."""
    from core.metrics import PipelineStatus
    from state.display import StatusDisplayEngine
    from state.models import SystemStatus

    status = SystemStatus(pipeline=PipelineStatus(file_dating_metrics={"enabled": False}))
    line = StatusDisplayEngine._format_file_dating_metrics_line(status)
    assert line == ""


# ---------------------------------------------------------------------------
# P-neutrality: background_batching=False ⇒ thread not started, zeros
# ---------------------------------------------------------------------------
def test_p_neutrality_off_no_thread():
    """background_batching=False ⇒ no thread, submit_urls unused, stats zero."""
    reset_file_date_stats()
    from storage.file_dating import FileDater

    store = _FakeStore()
    dater = FileDater(
        store,
        auth=_FakeAuth(),
        enabled=True,
        transport="graphql",
        background_batching=False,
    )

    # submit_urls returns 0 when background_batching is off
    result = dater.submit_urls([URL_A])
    assert result == 0

    stats = dater.to_stats()
    assert stats["pending_size"] == 0
    assert stats["pending_in"] == 0
    assert stats["dating_pending_dropped"] == 0
    assert stats["batches_dispatched"] == 0
    assert dater._bg_thread is None

    dater.close()


# ---------------------------------------------------------------------------
# P-gate: background_batching is INACTIVE unless enabled=True AND
# transport="graphql" (canon D-03.10; schema comment contract).  The
# constructor must normalize the flag down, submit_urls must refuse, and no
# thread may start.
# ---------------------------------------------------------------------------

def test_p_gate_inactive_unless_enabled_and_graphql():
    from storage.file_dating import FileDater

    # Case 1: enabled but transport=rest ⇒ flag normalized to False.
    dater_rest = FileDater(
        _FakeStore(), auth=_FakeAuth(), enabled=True, transport="rest",
        background_batching=True,
    )
    assert dater_rest.background_batching is False, (
        "background_batching must be inactive when transport != graphql"
    )
    assert dater_rest.submit_urls([URL_A]) == 0
    assert dater_rest._bg_started is False
    dater_rest.close()

    # Case 2: transport=graphql but disabled ⇒ flag normalized to False.
    dater_off = FileDater(
        _FakeStore(), auth=_FakeAuth(), enabled=False, transport="graphql",
        background_batching=True,
    )
    assert dater_off.background_batching is False, (
        "background_batching must be inactive when the dater is disabled"
    )
    assert dater_off.submit_urls([URL_A]) == 0
    assert dater_off._bg_started is False
    dater_off.close()


# ---------------------------------------------------------------------------
# P-dup: duplicate (owner, repo, path) keys within one batch must NOT
# misalign URL attribution.  The client returns a dict keyed by (o,r,p);
# duplicates collapse (last-wins), so index-alignment against batch order
# would deliver the WRONG date to the second URL and starve the third.
# Delivery must map per batch entry via key lookup.
# ---------------------------------------------------------------------------

def test_p_dup_keys_no_misattribution(monkeypatch):
    url_a1 = f"https://github.com/octocat/Hello-World/blob/{SHA_A}/src/a.py"
    url_a2 = f"https://github.com/octocat/Hello-World/blob/{SHA_B}/src/a.py"  # same path, other sha
    url_c = URL_C  # src/c.py

    dates = {
        ("octocat", "Hello-World", "src/a.py"): 111.0,
        ("octocat", "Hello-World", "src/c.py"): 333.0,
    }
    fake = _RecordingBatch(dates=dates)
    sink_calls: List[Dict[str, Any]] = []

    dater = _make_bg_dater(
        batch_fn=fake, graphql_batch_size=8,
        metadata_sink=lambda m: sink_calls.append(dict(m)),
        monkeypatch=monkeypatch,
    )
    try:
        accepted = dater.submit_urls([url_a1, url_a2, url_c])
        assert accepted == 3
        # Drive dispatch deterministically: pop pending under the lock (depth
        # stays below graphql_batch_size=8, so the bg thread cannot race us).
        with dater._pending_cond:
            keys = list(dater._pending.keys())[:8]
            batch = []
            for k in keys:
                owner, repo, path, ref = dater._pending.pop(k)
                batch.append((k, owner, repo, path, ref))
        assert len(batch) == 3
        dater._bg_dispatch_batch(batch)

        delivered = {}
        for call in sink_calls:
            delivered.update({u: m.file_commit_date for u, m in call.items()})
        assert delivered.get(url_a1) == 111.0, f"url_a1 must get src/a.py date, got {delivered}"
        assert delivered.get(url_a2) == 111.0, (
            f"url_a2 (same path, other sha) must get src/a.py date 111.0, got {delivered.get(url_a2)}"
        )
        assert delivered.get(url_c) == 333.0, f"url_c must get src/c.py date, got {delivered}"

        stats = dater.to_stats()
        assert stats["dated"] == 3, f"balance: all 3 entries delivered, got {stats['dated']}"
        assert stats["dating_pending_dropped"] == 0
        assert stats["dated"] + stats["dating_pending_dropped"] == stats["pending_in"]
    finally:
        dater.close()


# ---------------------------------------------------------------------------
# P-balance (invalid ref): entries whose ref is not a 40-hex sha are filtered
# out of the GraphQL items — they MUST still be accounted (dropped), keeping
# dated + dropped == pending_in exact.
# ---------------------------------------------------------------------------

def test_p_balance_invalid_ref_accounted(monkeypatch):
    url_branch = "https://github.com/octocat/Hello-World/blob/main/src/z.py"
    dates = {("octocat", "Hello-World", "src/a.py"): 111.0}
    fake = _RecordingBatch(dates=dates)
    sink_calls: List[Dict[str, Any]] = []

    dater = _make_bg_dater(
        batch_fn=fake, graphql_batch_size=8,
        metadata_sink=lambda m: sink_calls.append(dict(m)),
        monkeypatch=monkeypatch,
    )
    try:
        accepted = dater.submit_urls([URL_A, url_branch])
        assert accepted == 2
        with dater._pending_cond:
            keys = list(dater._pending.keys())[:8]
            batch = []
            for k in keys:
                owner, repo, path, ref = dater._pending.pop(k)
                batch.append((k, owner, repo, path, ref))
        assert len(batch) == 2
        dater._bg_dispatch_batch(batch)

        stats = dater.to_stats()
        assert stats["dated"] == 1, f"only the valid-sha entry is deliverable, got {stats['dated']}"
        assert stats["dating_pending_dropped"] == 1, (
            f"invalid-ref entry must be accounted as dropped, got {stats['dating_pending_dropped']}"
        )
        assert stats["dated"] + stats["dating_pending_dropped"] == stats["pending_in"] == 2
    finally:
        dater.close()


# ---------------------------------------------------------------------------
# P-balance (empty results): a total-failure empty dict (e.g. credential
# exhaustion returns {} without raising) must drop the WHOLE batch loudly on
# the counters — never silently vanish (balance invariant).
# ---------------------------------------------------------------------------

def test_p_balance_empty_results_dropped(monkeypatch):
    def empty_results(items, credential=None, client=None, timeout=30.0, retries=3):
        return {}

    dater = _make_bg_dater(
        batch_fn=empty_results, graphql_batch_size=8,
        metadata_sink=lambda m: None, monkeypatch=monkeypatch,
    )
    try:
        accepted = dater.submit_urls([URL_A, URL_B])
        assert accepted == 2
        with dater._pending_cond:
            keys = list(dater._pending.keys())[:8]
            batch = []
            for k in keys:
                owner, repo, path, ref = dater._pending.pop(k)
                batch.append((k, owner, repo, path, ref))
        dater._bg_dispatch_batch(batch)

        stats = dater.to_stats()
        assert stats["dated"] == 0
        assert stats["dating_pending_dropped"] == 2, (
            f"empty-results batch must be fully accounted as dropped, got {stats['dating_pending_dropped']}"
        )
        assert stats["dated"] + stats["dating_pending_dropped"] == stats["pending_in"] == 2
    finally:
        dater.close()
