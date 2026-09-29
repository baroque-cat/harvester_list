"""ROI-S12, S13 — run-output-integrity: the log sink belongs to the run.

Change ``fix-run-output-hygiene``.  ROI-S14 (collection from the repository root
must not include the root package module) is a Manual entry in ``tests.md``.

S12 runs in a **subprocess** on purpose: it needs pristine module-level logging
state, and mutating ``Logger._file_handler`` / ``_logs_dir`` / ``_module_handlers``
inside the suite would leak into every other test that captures a module logger.

Expected RED at creation: ``_setup_file_handlers`` assigns the sink location
unconditionally, so a supplied location is overwritten by the first logger
creation and the file lands in a working-directory-relative ``logs/``.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import textwrap

from tools.logger import get_logger

REPO = pathlib.Path(__file__).resolve().parent.parent


def _current_sink():
    """Names the offending path in the failure message of ROI-S13."""
    import tools.logger as lg

    return lg.Logger.get_logs_directory()


def _names(directory: pathlib.Path) -> set[str]:
    return {p.name for p in directory.glob("*")} if directory.exists() else set()


def _run_probe(tmp_path: pathlib.Path, preset: bool) -> dict[str, str]:
    """Emit one record in a child process and report where the file sink landed."""
    preset_line = (
        f"import pathlib; lg.Logger._logs_dir = pathlib.Path({str(tmp_path)!r})" if preset else ""
    )
    script = textwrap.dedent(
        f"""
        import pathlib, sys
        sys.path.insert(0, {str(REPO)!r})
        import tools.logger as lg
        # Importing the package already ran the file-handler setup, so the early
        # return would hide the behaviour under test.  Force the setup path to run
        # again *after* the preset, which is exactly the ordering a caller that
        # supplies a location depends on.
        lg.Logger._file_handler = None
        {preset_line}
        lg.get_logger("roi-sink-probe").warning("probe record")
        supplied = pathlib.Path({str(tmp_path)!r}) / "roi-sink-probe.log"
        relative = pathlib.Path.cwd() / "logs" / "roi-sink-probe.log"
        print("SUPPLIED=" + str(supplied.exists()))
        print("RELATIVE=" + str(relative.exists()))
        print("REPORTED=" + str(lg.Logger.get_logs_directory()))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(tmp_path),
    )
    assert proc.returncode == 0, f"probe process failed:\n{proc.stdout}\n{proc.stderr}"
    return dict(
        line.split("=", 1) for line in proc.stdout.strip().splitlines() if "=" in line
    )


def test_roi_s12_a_supplied_sink_location_survives_logging_setup_and_is_used(tmp_path):
    """WHEN a sink location is supplied before logging is set up and a record is
    then emitted
    THEN the log file is created under the supplied location and not under a path
    relative to the working directory, and an unsupplied location still resolves to
    the shipped default."""
    supplied_dir = tmp_path / "supplied"
    supplied_dir.mkdir()
    got = _run_probe(supplied_dir, preset=True)

    assert got["SUPPLIED"] == "True", (
        f"the supplied sink location was overwritten; reported={got['REPORTED']} "
        f"relative={got['RELATIVE']}"
    )
    assert got["RELATIVE"] == "False", "the sink was also written relative to the working directory"
    assert got["REPORTED"].endswith("supplied"), f"the layer reports {got['REPORTED']}"

    default_dir = tmp_path / "default"
    default_dir.mkdir()
    baseline = _run_probe(default_dir, preset=False)

    assert baseline["RELATIVE"] == "True", (
        "with nothing supplied the shipped default must still be a working-directory-relative logs/"
    )
    assert baseline["REPORTED"] == "logs", f"the shipped default changed to {baseline['REPORTED']}"


def test_roi_s13_a_test_run_leaves_no_log_file_in_the_repository():
    """WHEN records are emitted during a test run
    THEN the repository holds no log file created by that run, so operator journals
    cannot be confused with test output."""
    repo_logs = REPO / "logs"
    before = _names(repo_logs)

    for name in ("roi-s13-alpha", "roi-s13-beta"):
        get_logger(name).warning("probe record from the suite")

    created = _names(repo_logs) - before
    assert not created, (
        f"the suite wrote into the repository's log sink: {sorted(created)}; "
        f"the sink is {_current_sink()!r}"
    )
