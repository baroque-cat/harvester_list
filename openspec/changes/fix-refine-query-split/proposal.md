# Proposal: fix-refine-query-split

> Planning-only roadmap proposal (implementation not started). Discovered during
> the live verification of `fix-silent-losses` (2026-09-20, recorded in
> `openspec/changes/archive/*/fix-silent-losses/verification.md` §7.3b2).

## Why

`RefineEngine.clean_regex()` corrupts plain-text search syntax that it mistakes
for regex. Live evidence (real GitHub API, Bearer auth, 2026-09-20):

- `clean_regex('"sk-" filename:.env')` → `'""sk-" filename:" AND "env"'`
  (garbage wire syntax → the API answers `total_count=0`, silently zeroing the
  whole condition);
- root cause: for a part that is neither fully quoted nor matches
  `^[a-zA-Z]+:` (because it *starts* with a quote), the else-branch
  (`search/github/refine/engine.py:414-448`) runs the **regex parser** over the
  plain text: the `.` in `filename:.env` is treated as a metacharacter, fixed
  segments are re-quoted fragment-wise and rejoined with `" AND "`;
- impact class: exactly the "silent zero harvest" family that
  `fix-silent-losses` addresses at the transport level — but here the loss
  happens at query-construction time, before any request. The new
  config-query-lint does NOT cover it (the qualifier itself is API-valid; the
  mangling is internal).

Affected pattern: any API-mode condition mixing a quoted literal with an
unquoted qualifier value containing regex metacharacters (`filename:.env`,
`path:.github`, etc.).

## What Changes

- Make `clean_regex` search-syntax-aware: tokenize the query into quoted
  literals, `qualifier:value` tokens and `/regex/` parts BEFORE parsing; pass
  quoted literals and qualifier tokens through verbatim; apply fixed-string
  extraction only to genuine `/regex/` parts.
- Keep output stable for already-correct queries (`'"sk-"'` → `'"sk-"'`,
  `'/sk-[A-Za-z0-9]{16}/'` → extracted literals) — regression-pin with tests.
- Add unit vectors for the mixed forms: `'"sk-" filename:.env'`,
  `'/regex/ AND filename:.env'`, multiple qualifiers, dotted values.

## Capabilities

### Modified Capabilities
- Query refinement behavior (currently unspecified — no delta spec exists for
  RefineEngine; this change should add one, e.g. `query-refinement`).

## Impact

- **Code:** `search/github/refine/engine.py` (`clean_regex`, part classification
  at lines ~352-369 and the else-branch ~414-448).
- **Behavior:** conditions like `filename:.env` start returning real results on
  the API transport instead of guaranteed zeros; harvested volume may increase.
- **Risks:** over-eager passthrough could weaken regex cleaning for genuinely
  mixed parts — mitigated by tokenizing on search-syntax boundaries first.
- **Sequencing:** independent of `add-search-aggregation`; safe to schedule any
  time. Verification requires a live probe per the wire-format rule (compare
  `total_count` before/after on `filename:.env` conditions).
