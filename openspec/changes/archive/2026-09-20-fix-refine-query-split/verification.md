# Verification: fix-refine-query-split

Recorded 2026-09-20 on workspace `/home/openuser/scrap/harverster_gist`
(Python 3.14.7, `python3 -m pytest`). Live-network evidence was produced
in-session against the real GitHub code-search API with credentials supplied
in the git-ignored `.secrets` file, passed only via the environment
(`GITHUB_TOKENS`), never written to any file, fixture, log or commit
(plan_agr.md §3.4 guardrails).

## 1. RED baseline (task 1.1)

`python3 -m pytest tests/test_qr_clean_regex.py tests/test_sa_fingerprint.py -v`
→ **4 failed, 9 passed**, exactly matching `tests.md`: the four
`query-refinement-S1` assertions reproduced the recorded garbage
(`'""sk-" filename:" AND "env"'`, `'""sk-" filename:" AND "env path:" AND "github"'`,
`'""ghp_" repo:owner/name"'`, unbalanced-quote splice) while every guard
(S2–S6) and the search-aggregation goldens/aux pin passed.

## 2. GREEN run (tasks 2.4, 3.1–3.3)

| Suite | Result |
|-------|--------|
| `tests/test_qr_clean_regex.py` (S1–S6) | 9 passed |
| `tests/test_sa_fingerprint.py` (cross-change gate) | 4 passed |
| **Full suite** `python3 -m pytest tests/` | **172 passed** |

The four S1 vectors turned GREEN with no guard expectation edited. S2/S3/S4
stay byte-identical, S5 delegation identity holds, S6 shared goldens/aux pin
`_preprocess_query == wire_query` hold.

### Implementation shape

`RefineEngine.clean_regex` now tokenizes the query left-to-right into
escape-aware `/regex/` spans, double-quoted literals, `qualifier:value` tokens,
boolean operators (`AND`/`OR`/`NOT`) and bare words (`_tokenize_query`).
`RegexParser` fixed-string extraction runs **only** over genuine `/regex/`
spans (`_clean_regex_token`); everything else is emitted verbatim and bare
words keep the historical quoting semantics (`AKIA` → `"AKIA"`). The former
else-branch that ran the regex parser over plain text is removed and replaced
by the design-D2 fail-open path: an unclassifiable remnant (e.g. an unbalanced
quote) is returned verbatim plus a counted warning
(`RefineEngine._remnant_count`). Zero code change outside
`search/github/refine/engine.py`.

## 3. Live probe before/after (tasks 1.3, 4.1)

Real `GET https://api.github.com/search/code` (Bearer auth, API version
2022-11-28, `per_page=1`), fixtures with `provenance` headers under
`tests/fixtures/`:

| Form | Query | total_count | Fixture |
|------|-------|-------------|---------|
| BEFORE (mangled wire form) | `""sk-" filename:" AND "env"` | **0** | `live_2026_09_qr_mangled_before.json` |
| Fixed form (early probe) | `"sk-" filename:.env` | **148992** | `live_2026_09_qr_fixed.json` |
| AFTER (end-to-end via `wire_query`) | `wire_query('"sk-" filename:.env', True)` → `"sk-" filename:.env` | **148992** | `live_2026_09_qr_fixed_after_wire_query.json` |

The mangled form is a guaranteed zero; the fixed form returns 148 992 matches.
The after-fixture records the derived wire form and its fingerprint
`35c1db7f1be67ca75084abb4eb8ee9ac879aad3b8f88437f91ce974fa221952d`.

## 4. Bounded real shadow run (task 4.2)

Harness `/tmp/opencode/qr_shadow_run.py` (not committed) drives the **real**
`SearchAggregator(mode="shadow")` around the **real** API transport
`search_api_with_count` against `api.github.com`, with the fixed query
`'"sk-" filename:.env'` end-to-end through `wire_query`. Temp workspace
`/tmp/qr_shadow_e_632y4c`.

| Measurement | Result |
|-------------|--------|
| First call (live) | **5 links**, non-zero total (148 992 per §3) |
| Second call (live request, would-be hit) | 5 links, `total_delta = 0` |
| `aggregation_decisions.jsonl` record | `jaccard = 1.0`, `n_live = 5`, `n_cached = 5`, `total_delta = 0`, `transport = api`, fingerprint `35c1db7f…1952d` |
| `aggregatable_pairs` (two same-query API tasks) | **1** (identity = fixed-wire fingerprint) |

The empty-union rule (`search/aggregation.py:298`) is **not** exercised as a
false 1.0: both URL sets are non-empty (5 real links), so the decision Jaccard
reflects genuine live consistency. The aggregatable pair's identity fingerprint
matches the live fixed wire form, i.e. the metric now counts a condition that
harvests real links rather than a dead zero.

## 5. Guardrails & secret hygiene

- `logs/` was snapshotted to
  `/tmp/opencode/logs_snapshot_fix_query_split_pre_shadow/` before the shadow
  run; that run's logger initialization removed `logs/stage.log` (the incident
  0.4 pattern), so `logs/` was **restored byte-for-byte** from the snapshot
  afterwards.
- Credentials were read from the git-ignored `.secrets` only into the process
  environment; no probe script, fixture, log or repo file contains them. A
  working-tree scan for both exact credential values (excluding `.git/` and
  `.secrets` itself) returned **0 hits**.

## 6. `openspec validate --strict` (task 5.3)

`openspec validate --strict fix-refine-query-split` → **Change
'fix-refine-query-split' is valid** (exit 0). Every automated scenario in
`tests.md` is GREEN and the Manual live-probe entries are executed above.

## 7. Post-review hardening (dangling operators)

Independent review surfaced one regression in the first tokenizer draft: because
boolean operators are now preserved as tokens (the old code consumed them via a
delimiter split), a `/regex/` part that yields no fixed strings (all extracted
literals `< 3` chars, e.g. `/ab/`) was dropped while its operator survived,
producing a malformed wire query such as `AND filename:.env`. That
reintroduced the same silent-zero class through a different mechanism.

Fixed in `search/github/refine/engine.py`: the assembly loop drops an operator
that becomes dangling when an adjacent `/regex/` token disappears, restoring the
pre-fix byte-identical output. Regression vectors added to the S4 guard
(`'/ab/ AND filename:.env'` → `'filename:.env'`, `'filename:.env AND /ab/'` →
`'filename:.env'`), both GREEN, and `tests.md` S4 updated. Re-verified: target
suites 13 passed, full suite **172 passed**.

## 8. Not exercised live

- A deliberate rate-limit storm was not induced (avoiding self-inflicted
  abuse); aggregation hardening remains covered offline by
  `tests/test_sa_hardening.py`.
- `shadow → on` promotion remains an operator decision (design D5): land this
  change before promoting `aggregation.mode` beyond `shadow`.
