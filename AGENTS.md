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
