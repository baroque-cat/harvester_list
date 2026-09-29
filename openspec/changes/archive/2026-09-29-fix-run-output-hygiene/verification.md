# Verification — `fix-run-output-hygiene`

Status: **implemented and verified** (offline suite green; live gate run 2026-09-29).
Suite: **461 passed** (three consecutive runs). New files: **15 passed**
(`test_roi_redaction.py` 6, `test_roi_shutdown.py` 4, `test_roi_logsink.py` 2,
`test_prt_cap_surface.py` 3).

Live gate run root: `/var/tmp/opencode-roi-gate/gate_20260929T205723` (evidence
distilled to `evidence/09-acceptance.txt` there). Config referenced by absolute
path, md5 `a8685be3b149aa3780871948587806d8`, never copied.

## 1. Scenario → evidence

| Scenario | Kind | Evidence | Verdict |
|---|---|---|---|
| `ROI-S1` | automated | `tests/test_roi_redaction.py::test_roi_s1_…` | PASS |
| `ROI-S2` | automated | `test_roi_s2_…` (credential inside an attached traceback) | PASS |
| `ROI-S3` | automated | `test_roi_s3_…` (`redact_api_keys_in_text` monkeypatched to raise; marker carries ERROR / `roi.probe` / `client.py`) | PASS |
| `ROI-S4` | automated | `test_roi_s4_…` (enumerates every `logging.Formatter` subclass in `tools/logger.py`; requires ≥ 4; each redacts) | PASS |
| `ROI-S5` | automated | `test_roi_s5_…` (spy on root logger sees nothing; source has no `getLogger().addFilter`) | PASS |
| `ROI-S6` | automated | `test_roi_s6_…` (loopback 401; header name present, no value) | PASS |
| `ROI-S6` (live) | live | soak capture: 231 `[chat] failed …` lines, each `headers: ['Authorization', 'accept', 'content-type', 'user-agent']`; 0 `Bearer sk-` | PASS |
| `ROI-S7` | automated | `tests/test_roi_shutdown.py::test_roi_s7_…` (post-finalization emit produces no write, raises nothing) | PASS |
| `ROI-S8` | automated | `test_roi_s8_…` (preservation pin; pre-finalization output still present in the file sink) | PASS |
| `ROI-S8` (live) | live | final status block rendered in full, ending `Logs flushed to disk`; 0 `Logging error` | PASS |
| `ROI-S9` | **manual** | live `kill -9` drill: 5 claims in flight (search 1 worker + gather 4) → restart `reclaimed 1` + `reclaimed 4`; `claimed=0` on all four queues; `integrity_check=ok`; `RESTART_EXIT=0`; 0 `_enter_buffered_busy`, 0 `Fatal Python error` | PASS |
| `ROI-S10` | automated | `test_roi_s10_…` (5 discards → exactly 1 report, counter 5) | PASS |
| `ROI-S10` (live) | live | exactly 1 `[gather] not accepting tasks …; further discards are counted, not logged` | PASS |
| `ROI-S11` | automated | `test_roi_s11_…` (completion record carries total 3, no survivors) | PASS |
| `ROI-S11` (live) | live | `[gather] all workers stopped gracefully, tasks discarded after shutdown: 100` (+ 0/0/0 for search/check/inspect) | PASS |
| `ROI-S12` | automated | `tests/test_roi_logsink.py::test_roi_s12_…` (subprocess; supplied location survives setup; unsupplied resolves to CWD-relative `logs`) | PASS |
| `ROI-S13` | automated | `test_roi_s13_…` (repository `logs/` gains no file) | PASS |
| `ROI-S13` (suite) | offline | full-suite run: repository `logs/` empty before and after (three runs) | PASS |
| `ROI-S14` | **manual** | `git worktree` at HEAD: with `pytest.ini` collection is 446 tests including 0 root-module items; the pre-fix mechanism reproduced on demand — `python -m pytest __init__.py` dies `ImportError: attempted relative import with no known parent package` at `__init__.py:97` | PASS |
| `PRT-S26` | automated | `tests/test_prt_cap_surface.py::test_prt_s26_…` (CheckStage/InspectStage bounded by 25.0, deferral names provider ceiling) | PASS |
| `PRT-S26` (live) | live | deferral records name the surface: `(cap 60.0s, gather surface)` ×1760, `(cap 60.0s, provider surface)` ×340 | PASS |
| `PRT-S27` | automated | `test_prt_s27_…` (preservation pin, SearchStage/AcquisitionStage keep the gather ceiling) | PASS |
| `PRT-S28` | automated | `test_prt_s28_…` (preservation pin; identical wait at shipped defaults; strictly below visibility window) | PASS |

## 2. Acceptance highlights (live gate, 300 s soak)

- **Throughput unchanged:** gather 8.49 req/s over ~300 s (R9 baseline 8.50 / 8.52);
  search processed 14 619, gather 2 548, check 231, 0 stage errors except 1 search
  error (pre-existing, unrelated).
- **Counters unchanged / 1:1:1:** gather deferrals 1 760 = `defer[budget=1760]`;
  check deferrals 340 = `deferred_provider_budget=340`. `ProviderRefusals:` all
  zero. HTTP 429 = 403 = secondary = 0. `Credential liveness:` all six counters 0.
- **ROI-S1/S6 under real traffic:** 0 credential-shaped tokens and 0 `Bearer sk-`
  in the console capture. A naive `sk-[A-Za-z0-9_-]{20,}` grep returns 4 **false
  positives** from repo names (`Fla**sk**-JWT-RestAPI-Template`,
  `A**sk**-Questions-The-Stupid-Ways`); neither is a key. This is the same class of
  trap the R5.3 runbook warns about for raw substring greps.
- **RSS:** 27 samples, min 79 MB / mean 139 MB / max 160 MB — the R9 plateau.
- **Suppression does not eat the report:** the full shutdown block is present.

## 3. Deviations recorded

- **D12** (`design.md`, append-only): implementing ROI-S6 also removed the
  `if code != 401` guard in `chat()`'s failure log. A 401 previously fell through
  silently; it is now logged like every other status, which is what lets the
  scenario see the header names. No value is interpolated; classification and
  counters are untouched (pinned by `tests/test_prc_classes.py::test_s25`).

## 4. Gate-script defect (recorded, not hidden)

The queue-status sampling used the column name `status`, copied from the archived
R5.3 `runbook.md`; the shipped schema column is `state`. The queries returned
nothing (stderr suppressed), so the intended **pre-kill** in-flight snapshot is
NOT MEASURED. The `ROI-S9` verdict does not depend on it: in-flight equals the
worker count by construction (each worker holds at most one claimed row), the
claim counts are 1 (search, 1 worker) and 4 (gather, 4 workers) — confirmed
against `pipeline.threads` in the live config — and the startup reclaim log states
exactly 5. The final `state` queries and `integrity_check=ok` were taken after the
fix. A future gate should read the schema, not the runbook, for column names.

## 5. Hygiene

`data/providers/**` and run-root `logs/` deleted; no config copy created anywhere
in the run root; operator-credential scan 0; `df -h /tmp` back to its pre-task 7 %;
repository `data/` and `logs/` untouched; `git status --short` shows only the
intended modifications plus the pre-existing `?? examples/harvester.service`.
