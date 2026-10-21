"""NEW-4 — resource_provider plumbing pins (slice 2.3).

Traceability: 02_critical_fixes.md §2.3 NEW-4 (MEDIUM); 07_verification.md §slice 2.3.

Three plumbing defects:
(a) ``FileDater.coalesce_window_s`` — ctor accepts it, but no config key exists,
    so rollback (P21) requires code change.  Fix: new ``file_dating.coalesce_window_s``
    config key, default 0.25 (neutral = current behavior), 0/negative ALLOWED
    (that IS the rollback).
(b) ``gather.max_payload_bytes`` — parsed+validated+documented but never reaches
    ``fetch_gather_content`` (hardcoded 8 MiB at call site).  Fix: plumb through
    a new ``_configured_max_payload_bytes()`` accessor mirroring the existing
    ``_configured_gather_transport`` pattern.
(c) ``graphql_batch_max`` — leave as validation ceiling, add clarifying comment.

Pins:
* P1  loader parses file_dating.coalesce_window_s → schema attribute
* P2  absent key ⇒ default 0.25 AND equals DEFAULT_COALESCE_WINDOW_SECONDS
* P3  WIRING (a): configured value reaches pipeline.file_dating.coalesce_window_s
* P4  rollback semantics: 0 and -1 accepted by schema, FileDater gets them
* P5  WIRING (b): configured max_payload_bytes reaches fetch_gather_content
* P6  neutrality (b): absent ⇒ 8*1024*1024 in fetch_gather_content
* P7  graphql_batch_size > graphql_batch_max still ValueError (unchanged)
"""

from __future__ import annotations

import pytest

from config.loader import ConfigLoader
from config.schemas import Config, FileDatingConfig
from storage.file_dating import DEFAULT_COALESCE_WINDOW_SECONDS


# ---------------------------------------------------------------------------
# P1 — loader parses file_dating.coalesce_window_s → schema attribute
# ---------------------------------------------------------------------------
def test_p1_loader_parses_coalesce_window_s():
    """WHEN config data contains file_dating.coalesce_window_s=1.5
    THEN the loader populates FileDatingConfig.coalesce_window_s=1.5."""
    loader = ConfigLoader("/dev/null")
    data = {"coalesce_window_s": 1.5}
    fdc = loader._parse_file_dating_config(data)
    assert fdc.coalesce_window_s == 1.5


# ---------------------------------------------------------------------------
# P2 — absent key ⇒ default 0.25 AND equals DEFAULT_COALESCE_WINDOW_SECONDS
# ---------------------------------------------------------------------------
def test_p2_absent_coalesce_window_s_defaults_to_constant():
    """WHEN no coalesce_window_s key is present
    THEN FileDatingConfig.coalesce_window_s == 0.25
    AND 0.25 == DEFAULT_COALESCE_WINDOW_SECONDS (drift guard)."""
    loader = ConfigLoader("/dev/null")
    fdc = loader._parse_file_dating_config({})
    assert fdc.coalesce_window_s == 0.25
    # Drift guard: schema default MUST match the storage constant
    assert fdc.coalesce_window_s == DEFAULT_COALESCE_WINDOW_SECONDS


# ---------------------------------------------------------------------------
# P3 — WIRING (a): configured value reaches pipeline.file_dating.coalesce_window_s
# ---------------------------------------------------------------------------
def test_p3_pipeline_wiring_coalesce_window_s(tmp_path):
    """WHEN Config has file_dating.coalesce_window_s=1.5
    THEN Pipeline(cfg, providers={}).file_dating.coalesce_window_s == 1.5.

    This is a real wiring pin through Pipeline construction, not a direct
    FileDater instantiation (anti-falsification: must go through the real path).
    """
    from config.schemas import PipelineConfig, StageConfig, TaskConfig
    from core.models import Patterns
    from manager.pipeline import Pipeline

    cfg = Config()
    cfg.global_config.workspace = str(tmp_path)
    cfg.persistence.simple = True
    cfg.file_dating.coalesce_window_s = 1.5

    task_cfg = TaskConfig(
        name="deepseek",
        enabled=True,
        provider_type="openai_like",
        use_api=False,
        stages=StageConfig(search=True, gather=True, check=True, inspect=True),
        patterns=Patterns(key_pattern=r"sk-[A-Za-z0-9]{16}"),
    )
    cfg.tasks = [task_cfg]

    pipeline = Pipeline(cfg, providers={})
    try:
        assert pipeline.file_dating.coalesce_window_s == 1.5
    finally:
        for stage in pipeline.stages.values():
            if hasattr(stage.queue, "close"):
                stage.queue.close()


# ---------------------------------------------------------------------------
# P4 — rollback semantics: 0 and -1 accepted, no positivity check
# ---------------------------------------------------------------------------
def test_p4_coalesce_window_s_zero_and_negative_accepted():
    """WHEN coalesce_window_s is 0 or negative
    THEN schema accepts it without error (this IS the rollback knob, P21)."""
    # Zero: disables coalescing entirely
    fdc_zero = FileDatingConfig(coalesce_window_s=0)
    assert fdc_zero.coalesce_window_s == 0.0

    # Negative: also disables (per storage/file_dating.py:46-48 docstring)
    fdc_neg = FileDatingConfig(coalesce_window_s=-1)
    assert fdc_neg.coalesce_window_s == -1.0

    # FileDater receives the value as-is
    from storage.file_dating import FileDater

    class FakeStore:
        def get(self, *a, **kw): return None
        def is_fresh(self, *a, **kw): return False
        def stale_or_missing(self, *a, **kw): return []
        def record_upsert(self, *a, **kw): pass
        def flush(self, *a, **kw): pass
        def close(self, *a, **kw): pass

    dater = FileDater(FakeStore(), enabled=False, coalesce_window_s=0.0)
    assert dater.coalesce_window_s == 0.0

    dater2 = FileDater(FakeStore(), enabled=False, coalesce_window_s=-1.0)
    assert dater2.coalesce_window_s == -1.0


# ---------------------------------------------------------------------------
# P5 — WIRING (b): configured max_payload_bytes reaches fetch_gather_content
# ---------------------------------------------------------------------------
def test_p5_max_payload_bytes_reaches_fetch(monkeypatch):
    """WHEN gather.max_payload_bytes is configured to N
    THEN the collect() → fetch path passes max_payload_bytes=N to fetch_gather_content.

    This pins through the REAL call path (collect → fetch_gather_content),
    not a direct function call with a manual parameter.
    """
    import config as config_pkg
    from search import client as sc

    # Install a config with a non-default max_payload_bytes
    cfg = Config()
    cfg.gather.max_payload_bytes = 42_000
    monkeypatch.setattr(config_pkg, "_config_instance", cfg, raising=False)

    # Verify the accessor resolves the configured value
    assert sc._configured_max_payload_bytes() == 42_000

    # Spy on fetch_gather_content in the caller's namespace
    captured = {}
    original = sc.fetch_gather_content

    def spy_fetch_gather_content(**kwargs):
        captured.update(kwargs)
        return ""  # empty → will raise TransientFetchError, that's fine

    monkeypatch.setattr(sc, "fetch_gather_content", spy_fetch_gather_content)

    # Call through the real collect() path
    try:
        sc.collect(key_pattern=r"AKIA[0-9A-Z]{16}", url="https://github.com/o/r/blob/" + "a" * 40 + "/f.txt")
    except Exception:
        pass  # we expect failure — we just want to capture the kwargs

    assert "max_payload_bytes" in captured, (
        "fetch_gather_content was called without max_payload_bytes — "
        "the configured value did not reach the fetch chain"
    )
    assert captured["max_payload_bytes"] == 42_000


# ---------------------------------------------------------------------------
# P6 — neutrality (b): absent ⇒ 8*1024*1024 in fetch_gather_content
# ---------------------------------------------------------------------------
def test_p6_absent_max_payload_bytes_defaults_to_8mib(monkeypatch):
    """WHEN no config is loaded
    THEN _configured_max_payload_bytes() returns 8*1024*1024 (neutrality)."""
    import config as config_pkg
    from search import client as sc

    monkeypatch.setattr(config_pkg, "_config_instance", None, raising=False)
    assert sc._configured_max_payload_bytes() == 8 * 1024 * 1024


# ---------------------------------------------------------------------------
# P7 — graphql_batch_size > graphql_batch_max still ValueError (unchanged)
# ---------------------------------------------------------------------------
def test_p7_batch_size_above_ceiling_still_rejected():
    """WHEN graphql_batch_size > graphql_batch_max
    THEN ValueError naming both fields (behavior unchanged by comment-only fix)."""
    with pytest.raises(ValueError) as exc:
        FileDatingConfig(graphql_batch_size=100, graphql_batch_max=50)
    assert "graphql_batch_size" in str(exc.value)
    assert "graphql_batch_max" in str(exc.value)
