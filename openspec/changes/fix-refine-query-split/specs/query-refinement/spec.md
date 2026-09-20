# Delta Spec: query-refinement

## Purpose

Guarantees that API-mode query cleaning (`RefineEngine.clean_regex`) is search-syntax-aware: plain-text tokens the operator wrote (quoted literals, `qualifier:value` pairs) pass through verbatim, and regex fixed-string extraction applies only to genuine `/regex/` parts — so no condition is silently mangled into a zero-result wire query (live evidence 2026-09-20: `'"sk-" filename:.env'` → `'""sk-" filename:" AND "env"'` → `total_count=0`).

## ADDED Requirements

### Requirement: Search-syntax-aware query cleaning
`clean_regex` SHALL tokenize a condition query into quoted literals, `qualifier:value` tokens and `/regex/` parts before any parsing. Quoted literals and qualifier tokens SHALL be emitted verbatim; regex fixed-string extraction SHALL apply only to `/regex/` parts. A query that is already valid wire syntax SHALL be returned semantically unchanged.

#### Scenario: Mixed literal and dotted qualifier passes through
- **WHEN** `clean_regex` receives `'"sk-" filename:.env'`
- **THEN** both tokens are preserved (`"sk-"` quoted literal, `filename:.env` qualifier verbatim), the `.` is not treated as a regex metacharacter, and no fragment re-quoting or `" AND "` splicing occurs

#### Scenario: Regex part is still cleaned
- **WHEN** `clean_regex` receives a `/regex/` part with extractable fixed strings (e.g. `'/sk-[0-9]{16}/'`)
- **THEN** fixed strings are extracted and quoted exactly as before this change

#### Scenario: Already-correct queries stay stable
- **WHEN** `clean_regex` receives `'"sk-"'` or a qualifier-only query such as `'filename:.env'`
- **THEN** the output equals the input (regression pin for existing behavior)
