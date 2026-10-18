# AGENTS.md — operating rules for AI agents in this repository

Domain conventions (parsing, registry, flags, wire-format probing) live in
`openspec/config.yaml` → `context:` and in `plan.md`. This file covers the
**environment and operational** rules that are not domain logic but have already
caused real incidents.

## 1. Temporary storage: `/tmp` is a small tmpfs — never put run data there

`/tmp` on this host is a **3.9 GB tmpfs backed by RAM**, not disk. `/var/tmp` is
on the real volume (`/dev/vda2`, tens of GB free).

Measured incident (2026-09-25, `fix-credential-liveness` live gate): a single
600 s production-shaped soak wrote **2.5 GB** into `/tmp/opencode/cl_gate_*`
(2.3 GB of it `data/providers/**` NDJSON shards + snapshots, 78 MB
`queue_state/*.sqlite`, 56 MB console capture). A second soak added 425 MB.
`/tmp` reached 81 % and then failed with `Disk quota exceeded`: the pipeline
process was killed mid-run, evidence files could not be written, and the
remaining gate steps (restart recovery, rollback drills) were lost.

**Policy**

| Artifact                                              | Location                                    |
| ----------------------------------------------------- | ------------------------------------------- |
| Pipeline run roots: `data/`, `logs/`, `*.sqlite`, soak console captures, live-gate evidence | `/var/tmp/opencode-<task>/<run-root>/` |
| Throwaway scripts, small excerpts, probes (< 50 MB)   | `/tmp/opencode/`                            |
| Anything the report must cite                         | distil into the change's `verification.md`; keep raw files under `/var/tmp` |

Rules:

1. Create run roots with `RUN=/var/tmp/opencode-<task>_$TS` — never under `/tmp`.
2. `main.py` resolves `global.workspace` and the `logs/` directory **relative to
   the CWD**, so `cd "$RUN"` before launching: this keeps the repository's own
   `data/` and `logs/` untouched and makes cleanup a single `rm -rf "$RUN"`.
3. Before deleting a run root, distil what the report needs (counters, summary
   lines, queue states) into small text files, and move `evidence/` plus
   `data/queue_state/` to `/var/tmp` if a later step (restart recovery,
   transport flip) still needs the durable backlog. `data/providers/**` is
   harvested output — disposable, and by far the biggest consumer.
4. Never leave gigabytes on the tmpfs after a task: `df -h /tmp` must return to
   its pre-task level. Check it after every live run, not only on failure.
5. A run root that will hold a long soak should be sized first: expect roughly
   **4 MB of `data/` per second of soak** at the current fan-out (2.5 GB / 600 s).

## 2. Live gates and the test suite do not mix

Do **not** run `pytest tests/` while a live pipeline run is active on the same
host. Observed: a full-suite run started at the moment a gate run restarted
(15 worker threads + recovery over ~36 k queued rows) produced ~30 failures
across unrelated files; the identical suite passed `319` before, immediately
after, and again under load average 2.24. These are timing-sensitive harness
artifacts (thread/queue races, wall-clock budget assertions), not code defects —
but they destroy the evidential value of a GREEN run. Sequence them: gate first,
suite after (or vice versa), and record the clean run in `verification.md`.

## 3. Live-gate hygiene (applies to every `runbook.md`)

- Credentials are read at runtime from the git-ignored `.secrets` / `config.yaml`;
  never print a token, cookie, or a full config into evidence. Mask with
  `tools.state.mask_credential` (`ghp_<REDACTED>` style).
- Never provoke a GitHub secondary/abuse limit on purpose; secondary-limit
  evidence is collected only from organic windows.
- `main.py --timeout N` keeps running for a long graceful stop afterwards
  (measured 150 s with ~36 k queued rows; precedent 121 s). Budget `N + 300 s`
  and wrap the launch in `timeout -k 30 <N+300>` so a stuck stop cannot hang a
  gate script.
- Launch long runs with `setsid nohup … &` from a **script file**; a background
  job started directly from an interactive shell dies when that shell exits.
- Guard globs in gate scripts (`for f in "$RUN"/data/queue_state/*.sqlite` +
  `[ -e "$f" ] || continue`): under `zsh` a non-matching glob aborts the whole
  script with `no matches found`.

## 4. Test suite: sink isolation and collection scope

A suite run no longer writes log files into the repository (`fix-run-output-hygiene`):

- `pytest.ini` declares `testpaths = tests`, so collection from the repository
  root never includes the root package module (`__init__.py`, whose relative
  `from .main import ...` dies with `ImportError: attempted relative import with
  no known parent package` when collected standalone). This is what makes a
  measurement reproducible at a parent commit from a separate `git worktree`
  (lesson И4 in `plan_races.md`).
- `tests/conftest.py::pytest_configure` points `Logger._logs_dir` at a
  per-session temp directory before collection imports product code, and strips
  the file handlers that those imports already attached. It must be a
  `pytest_configure` hook and not a fixture: importing `tools.logger` executes
  `tools/__init__.py`, whose module-level `get_logger(...)` calls resolve the
  sink location at import time — before any fixture could run.
- Consequence: an empty repository `logs/` after a suite run is an invariant. A
  file appearing there is a regression in the isolation, not harmless output.

## 5. Two interpreters: dev runs Python 3.14, prod runs pyenv 3.11.15

The dev host runs the suite and every live gate under `/usr/bin/python3` =
**3.14.8**. The production host (`/home/scrapuser/harvester_list`) runs
**pyenv 3.11.15**. Since 3.14, PEP 649 makes annotations *lazy* (never
evaluated at import); on 3.11 they are evaluated *eagerly* during class-body
execution. Anything that only breaks under eager evaluation is invisible to
the whole dev loop — suite and gates included.

Measured incident (2026-10-18): `manager/pipeline.py` shipped
`def get_refine_metrics(self) -> Dict[str, Any]:` since `e6d81ea` (R1,
2026-09-22) while its typing import listed only `Dict, List, Optional`. A
month, 30 commits, a 727-test suite and every live gate passed under 3.14;
the first prod launch died before executing a single statement:
`NameError: name 'Any' is not defined` (main.py:29 → `manager/__init__.py`:16
→ `pipeline.py`:45 → :294). A 3.11 sweep of all 104 product modules showed
exactly one root cause (the other eight failures were cascades through
`manager/__init__`).

**Policy**

1. `tests/test_annotation_hygiene.py` is the interpreter-independent guard:
   an AST scan requires every annotation name to be bound (strictly at module
   level for `manager/pipeline.py`), and every product source must parse with
   `feature_version=(3, 11)`. Never weaken it to an import-based check — an
   import cannot fail on this class under 3.14.
2. Before deploying to the prod host, run the 3.11 import sweep from a
   **sandbox copy** (never from the repository cwd — importing `tools`
   attaches file handlers at import time and would write into the repo's
   `logs/`, §4):

   ```bash
   SB=/tmp/opencode/py311_sweep; rm -rf "$SB"; mkdir -p "$SB"
   tar cf - --exclude=./.git --exclude=./data --exclude=./logs \
       --exclude=./openspec --exclude=./docs --exclude=./examples \
       --exclude=./tests --exclude=__pycache__ --exclude=.secrets \
       --exclude=./config.yaml . | (cd "$SB" && tar xf -)
   cd "$SB" && ~/.pyenv/versions/3.11.15/bin/python3 - <<'EOF'
   import importlib, os, sys
   sys.path.insert(0, ".")
   mods = []
   for root, dirs, files in os.walk("."):
       dirs[:] = [d for d in dirs if d != "__pycache__"]
       for f in files:
           if f.endswith(".py"):
               p = os.path.join(root, f)[2:-3].replace("/", ".")
               mods.append(p[:-9] if p.endswith(".__init__") else p)
   fails = 0
   for m in sorted(mods):
       try:
           importlib.import_module(m)
       except Exception as e:
           fails += 1; print("FAIL", m, type(e).__name__, e)
   print(f"attempted {len(mods)}, failures {fails}")
   EOF
   ```

   Expect 0 failures except the documented root `__init__.py` artifact (§4:
   relative import with no known parent package — it is never imported
   standalone in production; `main.py` is the entry point).
3. Interpreter-sensitive smoke checks (`main.py --validate`) may run under
   3.11 with the config referenced **by absolute path** from the sandbox cwd;
   the config is never copied (§3, lesson И8).
