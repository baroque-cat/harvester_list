# Design: fix-refine-query-split

## Context

See proposal.md — Why. Current-state facts relevant to the approach (all probe-verified 2026-09-20):

- `clean_regex` splits the query on whitespace into parts and classifies each part (engine.py ~352-369): fully quoted → verbatim; matches `^[a-zA-Z]+:` → verbatim qualifier; `/regex/` → fixed-string extraction via `RegexParser`; **else → the regex parser runs over plain text** (~414-448), treating `.` as a metacharacter and re-splicing fragments with `" AND "` — the root cause.
- Consequently the broken class is exactly parts that *start with a quote* but are not fully quoted (`'"sk-" filename:.env'`, `'"ghp_" repo:owner/name'`); parts like `filename:.env` or `content:"llm"` already pass verbatim, and regex+qualifier mixtures (`'/sk-.../ AND filename:.env'`) are already cleaned correctly.
- Since commit 56b7781 (`add-search-aggregation`, archived) `clean_regex` has a second consumer seam: `search/querykey.py::wire_query(query, use_api)` returns the `clean_regex` output for API (raw when empty) and feeds `fingerprint = sha256("<api|web>|<wire>")`, cache keys, and queue locality; `SearchStage._preprocess_query` delegates to it (`stage/definition.py:276-279`). The refine engine itself was untouched by that change.
- Shared stability pins exist in `tests/test_sa_fingerprint.py` (golden vectors `'/sk-[a-zA-Z0-9]{32}/'` → `'"sk-"'`, web verbatim, sha256 goldens, aux pin `_preprocess_query == wire_query`). They fall inside this spec's "already-correct stays stable" class.
- Project conventions binding this change: parsers fail-open (degradation + counted warning, never an exception into worker loops); GitHub is a moving target — wire assumptions require live probes; rollback prefers config flips, deviations recorded as numbered decisions.

## Goals / Non-Goals

**Goals:**
- Quote-leading mixed forms clean to valid wire syntax (tokens verbatim, regex parts extracted).
- Byte-identical outputs for every already-correct class (guards pinned by tests).
- Fix propagates through the wire-identity seam with zero code change outside `search/github/refine/engine.py`.
- Live-probe evidence (total_count before/after) per the wire-format rule.

**Non-Goals:**
- No full grammar parser for GitHub search syntax (moving target; out of scope).
- No changes to `search/querykey.py`, `search/aggregation.py`, stage code, config schema, or shard/registry surfaces.
- No retroactive cache/fingerprint migration (ephemeral by construction).
- No touch-up of the refine split/enumeration machinery (`generate_queries`, optimizer, generator) — only the cleaning path.

## Decisions

**D1 — Tokenize on search-syntax boundaries before any parsing.**
Rewrite the part classifier to scan the query left-to-right recognizing, in order: `/regex/` spans (escape-aware), double-quoted literals (`"..."`), `qualifier:value` tokens (name `[a-zA-Z][A-Za-z0-9_-]*:`, value bare or quoted), operator separators (`AND`/`OR`/`NOT`, preserved), and bare words. `RegexParser` fixed-string extraction applies ONLY to `/regex/` spans; every other token is emitted verbatim; operators/spacing are preserved so the output remains one well-formed wire query.
Alternatives rejected: (a) full GitHub-search grammar parser — over-engineered against a moving external contract; (b) keep whitespace-splitting and patch the quote-leading case ad hoc — leaves the root cause (regex parser applied to non-regex text) alive for the next syntax form.

**D2 — Fail-open verbatim fallback replaces the regex-parser else-branch.**
Any remnant the tokenizer cannot classify is emitted VERBATIM plus a counted warning — never fed to the regex parser. Rationale: within project doctrine a silent zero-harvest (mangled wire query answering `total_count=0`) is strictly worse than passing the operator's literal text through; this mirrors the fail-open parsing convention. The old else-branch (~414-448) is removed.

**D3 — Stability contract enforced by shared golden pins.**
Outputs for the already-correct classes stay byte-identical: `'"sk-"'`→`'"sk-"'`, `'filename:.env'`→verbatim, `'content:"llm"'`→verbatim, `'AKIA'`→`'"AKIA"'` (bare-word quoting semantics preserved), `'/sk-[0-9]{16}/'`→`'"sk-"'`, `'/sk-[a-zA-Z0-9]{32}/ AND filename:.env'`→`'"sk-" AND filename:.env'`, `'/sk-[a-zA-Z0-9]{32}/ AND content:"llm"'`→`'"sk-" AND content:"llm"'`. The change surface is confined to the quote-leading mangled class. `tests/test_sa_fingerprint.py` (golden vectors + aux pin) is a mandatory regression gate — it encodes the same stability class from the aggregation side.

**D4 — No feature flag; rollback = revert.**
Deviation from the ship-behind-flags paradigm, recorded deliberately: the affected class today produces guaranteed-zero garbage, so there is no ambiguous regime worth measuring in shadow — every output change is a strict improvement bounded by D3 pins, and the before/after live probe (D6) quantifies it. A tri-mode flag (as in `failure_handling`) was considered and rejected as ceremony without a measurable middle state. No data/config/schema surface exists, so reverting the commit is a complete rollback.

**D5 — Composition with landed aggregation requires zero seam changes.**
`wire_query` delegates to `clean_regex`; fingerprints/cache keys/locality therefore follow the fix automatically (spec requirement "Composition with wire-query identity", pinned by a delegation-identity test). Cache entries and locality are ephemeral (per-process/per-run), so fixed queries simply acquire new fingerprints at next run start — no invalidation or migration. Sequencing guidance: land BEFORE promoting `aggregation.mode` beyond shadow, so hit-rate and `aggregatable_pairs` statistics reflect live queries instead of cached zeros of dead conditions (correctness-neutral either way: empty-union Jaccard is 1.0 by `search/aggregation.py:298`, keeping the p95≥0.95 promotion gate clean).

**D6 — Live-probe verification per the wire-format rule.**
Before implementation: real API `total_count` for the mangled wire form `'""sk-" filename:" AND "env"'` (expect 0) and for the intended fixed form `'"sk-" filename:.env'` (expect > 0) — proving the fix's value up front. After implementation: re-probe the representative condition end-to-end through `wire_query`. Both halves captured as fixtures with provenance headers (endpoint, date, auth mode) under `tests/fixtures/`; synthetic vectors labeled as supplementary.

## Risks / Trade-offs

- [Over-eager passthrough weakens regex cleaning] → tokenizer recognizes `/regex/` spans first; extraction still applies to them; guards S2/S4 pin the behavior.
- [Tokenizer bug on pathological quoting (unbalanced quotes, nested slashes)] → D2 fail-open verbatim + counted warning; unit vectors include unbalanced-quote forms.
- [Harvest-volume jump shifts rate-limit budget] → bounded by existing cooldown/bucket machinery; visible via stage counters and `aggregatable_pairs`; revert available (D4).
- [Cross-consumer drift between clean_regex and wire_query] → single-source delegation + aux pin test (`_preprocess_query == wire_query`) fails loudly on drift.
- [sa-suite coupling surprises] → mandatory full-suite gate in tasks (163+ tests green) before archive.

## Migration Plan

Pure functional change in one module. Deploy → observe first runs (counters, harvest volume for fixed conditions, aggregation decision log if mode≠off) → rollback = revert commit if anything regresses. No persistence, config, or schema surface; no operator action required.

## Open Questions

None blocking. (Whether GitHub will ever support `content:`-style qualifiers on the REST API is tracked by `config-query-lint`, not here.)
