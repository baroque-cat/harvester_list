# Tasks: fix-refine-query-split

## 1. RED baseline & early live probes

- [x] 1.1 Run `python3 -m pytest tests/test_qr_clean_regex.py tests/test_sa_fingerprint.py -v` and confirm the baseline matches tests.md: exactly the 4 S1 assertions FAIL reproducing the recorded garbage (`'""sk-" filename:" AND "env"'`, `'""sk-" filename:" AND "env path:" AND "github"'`, `'""ghp_" repo:owner/name"'`, unbalanced-quote splice), while all guards/pins (S2–S6 + sa goldens) PASS
- [x] 1.2 Apply tooling guardrails (plan_agr.md §3.4): snapshot `logs/` before any instrumented run; credentials ONLY via env `GITHUB_TOKENS`/`GITHUB_SESSIONS` (`config/loader.py:174-182`); never write secrets into files or the repo
- [x] 1.3 EARLY live probe (wire-format rule, before implementation): against the real GitHub code-search API (Bearer auth) measure `total_count` for (a) the mangled wire form `""sk-" filename:" AND "env"` — expect 0; (b) the intended fixed form `"sk-" filename:.env` — expect > 0, proving the fix's value up front; capture both raw responses as fixtures under `tests/fixtures/` with provenance headers (endpoint, capture date, auth mode); label any synthesized variants as supplementary unit vectors

## 2. Tokenizer implementation (query-refinement-S1)

- [x] 2.1 Implement syntax-aware tokenization in the `clean_regex` part classification (`search/github/refine/engine.py` ~352-369): recognize left-to-right — escape-aware `/regex/` spans, double-quoted literals `"..."`, `qualifier:value` tokens (name `[a-zA-Z][A-Za-z0-9_-]*:`, value bare or quoted), operator separators (`AND`/`OR`/`NOT`, preserved), bare words (existing quoting semantics preserved: `'AKIA'` → `'"AKIA"'`)
- [x] 2.2 Replace the else-branch (~414-448): unclassified remnants are emitted VERBATIM plus a counted warning (design D2 fail-open); the regex parser must never run over non-`/regex/` text again
- [x] 2.3 Apply `RegexParser` fixed-string extraction ONLY to `/regex/` spans; preserve operators/spacing when rejoining so the output stays one well-formed wire query (design D1)
- [x] 2.4 Drive all four S1 vectors in `tests/test_qr_clean_regex.py` to GREEN without editing any guard expectation

## 3. Regression pins (query-refinement-S2..S6)

- [x] 3.1 Guards in `tests/test_qr_clean_regex.py` stay GREEN: S2/S3/S4 byte-identical outputs, S5 delegation identity, S6 shared goldens (design D3 stability contract)
- [x] 3.2 Mandatory cross-change gate: `tests/test_sa_fingerprint.py` golden vectors + aux pin (`_preprocess_query == wire_query`) GREEN — required by proposal Sequencing before this change may archive
- [x] 3.3 Full suite `python3 -m pytest tests/` GREEN (≥163 existing + new qr tests; zero regressions in refine generator/optimizer/splittability consumers)

## 4. Live verification after the fix

- [x] 4.1 Re-probe the representative condition end-to-end through `wire_query('"sk-" filename:.env', True)`: real API `total_count > 0` where the mangled form gave 0; save the after-fixture with provenance next to the before-fixture
- [x] 4.2 Short real run (with `aggregation.mode: shadow` if aggregation is active): confirm the fixed condition harvests non-zero links, `aggregation_decisions.jsonl` Jaccards unaffected (empty-union rule `search/aggregation.py:298`), and `aggregatable_pairs` now counts live queries instead of dead zeros
- [x] 4.3 Record probe outputs, fixture paths, and counter snapshots in `verification.md` inside the change directory (pattern: archived fix-silent-losses/add-search-aggregation verifications)

## 5. Docs & rollout

- [x] 5.1 README: brief note in the query-refinement/clean_regex area — syntax-aware cleaning, stability contract for already-correct forms, fail-open verbatim fallback
- [x] 5.2 Sequencing reminder for operators (design D5): land this change BEFORE promoting `aggregation.mode` beyond `shadow`; rollback here = revert commit (no flag, no data surface — design D4)
- [x] 5.3 `openspec validate --strict fix-refine-query-split` passes; every automated scenario GREEN and Manual entries executed; change ready for archive
