# Proposal: fix-refine-query-split

> Fully specified and apply-ready (artifacts completed 2026-09-20, after
> `add-search-aggregation` landed). Originally discovered during the live
> verification of `fix-silent-losses` (2026-09-20, recorded in
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

Affected pattern (probe-confirmed 2026-09-20, post-aggregation): the mangled
class is exactly the query parts that **start with a quote character** —
including the common real-world form `'"ghp_" repo:owner/name'` →
`'""ghp_" repo:owner/name"'` (garbage) and multi-qualifier variants
(`'"sk-" filename:.env path:.github'`). Regex-plus-qualifier mixtures that do
not lead with a quote are already cleaned correctly today
(`'/sk-[a-zA-Z0-9]{32}/ AND filename:.env'` → `'"sk-" AND filename:.env'`)
and MUST stay byte-identical after the fix.

Post-landing note: since `add-search-aggregation` (archived, commit 56b7781)
the mangled wire form is additionally fingerprinted, cached and shared across
providers — dead conditions became cheaper and stickier. This is
correctness-neutral (empty-set comparisons yield Jaccard 1.0 by the empty-union
rule, `search/aggregation.py:298`, so the shadow promotion gate is not
poisoned), but it pollutes `aggregatable_pairs` economics and masks the
harvest impact of the bug.

## What Changes

- Make `clean_regex` search-syntax-aware: tokenize the query into quoted
  literals, `qualifier:value` tokens and `/regex/` parts BEFORE parsing; pass
  quoted literals and qualifier tokens through verbatim; apply fixed-string
  extraction only to genuine `/regex/` parts.
- Keep output stable for already-correct queries (`'"sk-"'` → `'"sk-"'`,
  `'/sk-[A-Za-z0-9]{16}/'` → extracted literals, `'AKIA'` → `'"AKIA"'`) —
  regression-pin with tests.
- Add unit vectors for the mixed forms: `'"sk-" filename:.env'`,
  `'"ghp_" repo:owner/name'`, `'/regex/ AND filename:.env'`, multiple
  qualifiers, dotted values.
- Zero code change outside the refine engine: `search/querykey.py::wire_query`
  delegates to `clean_regex`, so wire forms, fingerprints, cache keys and
  queue locality inherit the fix automatically (delegation identity is
  test-pinned). Fingerprints are ephemeral (in-memory cache, per-run locality)
  — no migration or invalidation is required.
- Mandatory regression gate: `tests/test_sa_fingerprint.py` golden vectors and
  the aux pin (`SearchStage._preprocess_query == wire_query`) plus the full
  suite must stay green.

## Capabilities

### New Capabilities
- `query-refinement`: search-syntax-aware API query cleaning, stability
  contract for already-correct forms, and composition with the wire-query
  identity used by aggregation (delta spec: `specs/query-refinement/spec.md`).

### Modified Capabilities
None. `search-aggregation` requirements are unchanged: aggregation consumes
wire forms opaquely through the delegation seam; only runtime fingerprint
values for the fixed query class shift (ephemeral, no spec-visible behavior).

## Impact

- **Code:** `search/github/refine/engine.py` only (`clean_regex`, part
  classification at lines ~352-369 and the else-branch ~414-448). Consumers
  inherit via delegation: `search/querykey.py` (`wire_query` → `fingerprint` →
  cache keys/locality), `SearchStage._preprocess_query`
  (`stage/definition.py:276-279`).
- **Behavior:** quote-leading mixed conditions (`filename:.env`,
  `path:.github`, `repo:...`) start returning real results on the API transport
  instead of guaranteed zeros; harvested volume may increase; fixed queries
  acquire new fingerprints at runtime only.
- **Risks:** over-eager passthrough could weaken regex cleaning for genuinely
  mixed parts — mitigated by tokenizing on search-syntax boundaries first and
  by stability guards; harvest-volume jump is bounded by existing rate-limit
  machinery and visible via counters; rollback = revert (pure functional
  change, no data/config surface).
- **Sequencing:** composes with `add-search-aggregation` in either order (its
  design D2); aggregation has already landed and been archived. RECOMMENDED
  before promoting aggregation beyond shadow so hit-rate/`aggregatable_pairs`
  statistics reflect live queries rather than cached zeros of dead conditions.
  Verification requires a live probe per the wire-format rule (compare
  `total_count` before/after on a `filename:.env` condition).
