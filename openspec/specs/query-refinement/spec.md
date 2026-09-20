# query-refinement Specification

## Purpose

Guarantees that API-mode query cleaning (`RefineEngine.clean_regex`) is search-syntax-aware: plain-text tokens the operator wrote (quoted literals, `qualifier:value` pairs) pass through verbatim, and regex fixed-string extraction applies only to genuine `/regex/` parts — so no condition is silently mangled into a zero-result wire query (live evidence 2026-09-20: `'"sk-" filename:.env'` → `'""sk-" filename:" AND "env"'` → `total_count=0`).

## Requirements

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

#### Scenario: Mixed regex and qualifier compose
- **WHEN** `clean_regex` receives `'/sk-[a-zA-Z0-9]{32}/ AND filename:.env'`
- **THEN** the output is `'"sk-" AND filename:.env'` — the regex part is reduced to its quoted fixed string while the qualifier token is preserved verbatim (this form is already correct today; the fix MUST NOT alter it)

### Requirement: Composition with wire-query identity
API wire-form derivation SHALL delegate to `clean_regex` as the single source of truth, so this cleaning fix propagates automatically to wire queries, fingerprints, cache keys and queue locality with no code change outside the refine engine. Fingerprints are ephemeral (in-memory cache, per-run locality), so changed wire forms require no migration or invalidation. Outputs for the already-correct query class SHALL remain byte-identical across the fix, keeping the shared stability pins (search-aggregation golden vectors and the `_preprocess_query == wire_query` aux pin) green.

#### Scenario: Fingerprint follows cleaning automatically
- **WHEN** `clean_regex` output for a query changes as a result of this fix
- **THEN** the API wire form and its fingerprint reflect the new cleaning with no code change in the wire-identity module, and the web-transport wire form remains the raw query verbatim

#### Scenario: Shared stability pins survive the fix
- **WHEN** the tokenizer fix lands
- **THEN** previously pinned conversions remain byte-identical (`'/sk-[a-zA-Z0-9]{32}/'` → `'"sk-"'`, `'"sk-"'` → `'"sk-"'`), and the search-aggregation golden-vector tests plus the `_preprocess_query == wire_query` aux pin pass unchanged
