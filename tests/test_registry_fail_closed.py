"""Fail-closed registry liveness (Д14 / OPEN-5, slice 3a.0).

Operator directive (verbatim, 2026-10-22): «Переход на шарды, если что-то
случится с sql? Нет, не нужно, нужно только чтобы программа крашнулась и не
работала, если бд сдохла.»

When the registry is enabled and its SQLite store dies (open failure or
database-class error), the program MUST crash loudly and NOT continue
operating.  Transient busy/locked errors keep the existing degrade-and-retry
behaviour.  A disabled registry remains a legal no-op.

Traceability: Д14, OPEN-5, link-registry spec (fail-closed liveness).
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from core.exceptions import RegistryUnavailableError
from storage.registry import Registry, _is_fatal_db_error

URL = "https://github.com/o/r/blob/main/a.py"


# ---------------------------------------------------------------------------
# P1: enabled + unopenable db → start() raises RegistryUnavailableError
# ---------------------------------------------------------------------------
def test_p1_enabled_garbage_db_raises(workspace, registry_path):
    """WHEN registry is enabled AND the db file is garbage bytes
    THEN start() raises RegistryUnavailableError.

    RED prediction: no exception, start() returns False (current fail-open).
    """
    path = registry_path(workspace)
    with open(path, "wb") as f:
        f.write(b"NOT SQLITE" * 100)

    registry = Registry(workspace, enabled=True)
    with pytest.raises(RegistryUnavailableError):
        registry.start()


# ---------------------------------------------------------------------------
# P2: disabled + same garbage file → start() returns falsy, no raise
# ---------------------------------------------------------------------------
def test_p2_disabled_garbage_db_no_raise(workspace, registry_path):
    """WHEN registry is disabled AND the db file is garbage
    THEN start() returns falsy and does NOT raise.

    Insurance pin — expected PASS even before product changes.
    """
    path = registry_path(workspace)
    with open(path, "wb") as f:
        f.write(b"NOT SQLITE" * 100)

    registry = Registry(workspace, enabled=False)
    result = registry.start()
    assert not result


# ---------------------------------------------------------------------------
# P3: mid-run fatal → check_alive() raises RegistryUnavailableError
# ---------------------------------------------------------------------------
def test_p3_midrun_fatal_check_alive_raises(workspace, monkeypatch):
    """WHEN a fatal database error occurs mid-run
    THEN check_alive() raises RegistryUnavailableError.

    RED prediction: AttributeError — check_alive() does not exist yet.
    """
    registry = Registry(workspace, enabled=True)
    registry.start()

    # Simulate a fatal database-class error in the flush path.
    def boom(_batch):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(registry, "_flush_batch", boom)

    registry.record_link(URL, ts=1.0)
    registry.flush()

    with pytest.raises(RegistryUnavailableError):
        registry.check_alive()

    registry.stop()


# ---------------------------------------------------------------------------
# P4: transient NOT fatal → check_alive() does not raise
# ---------------------------------------------------------------------------
def test_p4_transient_busy_not_fatal(workspace, monkeypatch):
    """WHEN a transient busy/locked error occurs
    THEN check_alive() does NOT raise (transient, not death).

    RED prediction: AttributeError — _is_fatal_db_error does not exist yet.
    """
    # Verify the classification helper directly.
    busy_exc = sqlite3.OperationalError("database is locked")
    assert not _is_fatal_db_error(busy_exc)

    locked_exc = sqlite3.OperationalError("[SQLITE_LOCKED] table is locked")
    assert not _is_fatal_db_error(locked_exc)

    # Verify a real DatabaseError IS fatal.
    corrupt_exc = sqlite3.DatabaseError("database disk image is malformed")
    assert _is_fatal_db_error(corrupt_exc)

    # OperationalError with non-transient message → fatal (it's a DatabaseError
    # subclass, and the message doesn't match busy/locked patterns).
    op_fatal = sqlite3.OperationalError("no such table: links")
    assert _is_fatal_db_error(op_fatal)


# ---------------------------------------------------------------------------
# P5: pipeline fail-fast — enabled + garbage db → _on_start raises
# ---------------------------------------------------------------------------
def test_p5_pipeline_on_start_raises(workspace):
    """WHEN Pipeline._on_start is called with registry enabled on garbage db
    THEN RegistryUnavailableError propagates through _on_start.

    RED prediction: currently _on_start ignores the return value of start(),
    so no exception is raised.
    """
    from config.schemas import Config, Patterns, StageConfig, TaskConfig
    from manager.pipeline import Pipeline

    cfg = Config()
    cfg.global_config.workspace = str(workspace)
    cfg.persistence.simple = True
    cfg.registry.enabled = True

    # Point registry at a garbage file.
    garbage_path = os.path.join(str(workspace), "registry.sqlite")
    with open(garbage_path, "wb") as f:
        f.write(b"NOT SQLITE" * 100)

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
        with pytest.raises(RegistryUnavailableError):
            pipeline._on_start()
    finally:
        # Clean up sqlite queues if created.
        for stage in pipeline.stages.values():
            if hasattr(stage.queue, "close"):
                stage.queue.close()


# ---------------------------------------------------------------------------
# P6: main exit-code mapping — pin via pipeline-level propagation
# ---------------------------------------------------------------------------
# P6 is covered by P5 (pipeline propagation).  Exit-code honesty is a
# one-liner in main.py (sys.exit(0 if success else 1)) and is verified by
# reading the source; no separate unit pin is needed — the pipeline-level
# crash (P5) is the meaningful propagation test.


# ---------------------------------------------------------------------------
# P7: adaptation legitimacy — existing pins adapted in-place
# ---------------------------------------------------------------------------
# Adaptations are in tests/test_registry_core.py (S15, S16, S17) and
# tests/test_key_sources.py (p22_t11).  See the adapted-pin report section.
