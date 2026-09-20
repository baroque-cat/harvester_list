# Test Plan

Derived mechanically from the delta spec under `specs/query-refinement/`. One spec scenario = exactly one automated test group, or one Manual entry with a reason. Scenario IDs (`query-refinement-S<n>`) are stable: never renumber, append only.

Runner: `python3 -m pytest` (project convention: flat `tests/test_*.py`). Observed statuses recorded from the run of 2026-09-20 (pre-implementation): **4 failed, 9 passed** — failures are assertion-level (the recorded mangling `'""sk-" filename:" AND "env"'` etc.), not import errors, because both modules under test (`search/github/refine/engine.py`, `search/querykey.py`) already exist.

Guard policy (design D3/D5): S2–S6 encode behavior that exists today and MUST stay byte-identical after the fix — they are expected GREEN now and GREEN after; any post-fix RED there means the stability contract was violated. `tests/test_sa_fingerprint.py` (search-aggregation goldens + aux pin) is re-run as a mandatory cross-change gate and was confirmed GREEN in the same baseline run.

## Traceability

| Scenario ID | Spec | Requirement | Scenario | Test file | Status |
|-------------|------|-------------|----------|-----------|--------|
| `query-refinement-S1` | `specs/query-refinement/spec.md` | Search-syntax-aware query cleaning | Mixed literal and dotted qualifier passes through | `tests/test_qr_clean_regex.py` | GREEN (was 4 FAILED; all 4 S1 vectors PASS after tokenizer) |
| `query-refinement-S2` | `specs/query-refinement/spec.md` | Search-syntax-aware query cleaning | Regex part is still cleaned | `tests/test_qr_clean_regex.py` | GREEN observed (guard; must stay GREEN) |
| `query-refinement-S3` | `specs/query-refinement/spec.md` | Search-syntax-aware query cleaning | Already-correct queries stay stable | `tests/test_qr_clean_regex.py` | GREEN observed (guard; incl. `'AKIA'`→`'"AKIA"'` bare-word semantics) |
| `query-refinement-S4` | `specs/query-refinement/spec.md` | Search-syntax-aware query cleaning | Mixed regex and qualifier compose | `tests/test_qr_clean_regex.py` | GREEN observed (guard; form already correct today) |
| `query-refinement-S5` | `specs/query-refinement/spec.md` | Composition with wire-query identity | Fingerprint follows cleaning automatically | `tests/test_qr_clean_regex.py` | GREEN observed (structural delegation pin — both sides move together) |
| `query-refinement-S6` | `specs/query-refinement/spec.md` | Composition with wire-query identity | Shared stability pins survive the fix | `tests/test_qr_clean_regex.py` (+ `tests/test_sa_fingerprint.py` gate) | GREEN observed (guard; duplicates sa goldens deliberately) |

## Automated

### File: tests/test_qr_clean_regex.py

Describe: Search-syntax-aware query cleaning + composition with wire-query identity

Modules under test exist; S1 fails at assertion level reproducing the probe-recorded garbage (expected RED until the tokenizer lands).

- [x] `query-refinement-S1` — it("Mixed literal and dotted qualifier passes through") <!-- WHEN '"sk-" filename:.env' THEN verbatim tokens, no '" AND "' splicing -->
- [x] `query-refinement-S1` — it("Multiple qualifiers pass through") <!-- WHEN '"sk-" filename:.env path:.github' THEN verbatim -->
- [x] `query-refinement-S1` — it("Quoted literal with repo qualifier passes through") <!-- WHEN '"ghp_" repo:owner/name' THEN verbatim -->
- [x] `query-refinement-S1` — it("Unclassified remnant fails open verbatim") <!-- WHEN unbalanced quote THEN verbatim (design D2), never regex-parsed -->
- [x] `query-refinement-S2` — it("Regex part is still cleaned") <!-- '/sk-[0-9]{16}/' → '"sk-"', '/sk-[a-zA-Z0-9]{32}/' → '"sk-"' -->
- [x] `query-refinement-S3` — it("Already-correct queries stay stable") <!-- '"sk-"', 'filename:.env', 'content:"llm"', 'AKIA'→'"AKIA"' -->
- [x] `query-refinement-S4` — it("Mixed regex and qualifier compose") <!-- '/sk-…/ AND filename:.env' → '"sk-" AND filename:.env'; content:"llm" variant; empty-fixed '/ab/ AND filename:.env' → 'filename:.env' (no dangling operator) -->
- [x] `query-refinement-S5` — it("Fingerprint follows cleaning automatically") <!-- wire_query(q,True)==clean_regex(q); fingerprint sha256 identity; web verbatim -->
- [x] `query-refinement-S6` — it("Shared stability pins survive the fix") <!-- sa golden vectors + aux pin _preprocess_query==wire_query via real SearchStage harness -->

### Cross-change gate: tests/test_sa_fingerprint.py

Describe: search-aggregation golden vectors (S1–S3 there) + aux delegation pin — confirmed GREEN in the baseline run; MUST be re-run and stay GREEN after the tokenizer lands (proposal Sequencing / design D3).

## Manual

- Live `total_count` probe pair (wire-format rule; requires GitHub credentials via env `GITHUB_TOKENS`/`GITHUB_SESSIONS` and wall-clock): (a) BEFORE implementation — mangled wire form `'""sk-" filename:" AND "env"'` expects `total_count=0`; (b) AFTER — fixed form `'"sk-" filename:.env'` end-to-end through `wire_query` expects `total_count>0`. Both halves captured as fixtures with provenance headers under `tests/fixtures/`. Cannot be automated in-repo: hits the live third-party API. Executed within tasks.md groups 1 and 4.
- Operator sequencing note (not a scenario): land this change before promoting `aggregation.mode` beyond `shadow` (design D5) so hit-rate/`aggregatable_pairs` statistics reflect live queries.
