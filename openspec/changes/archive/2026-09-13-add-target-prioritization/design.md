# Design: add-target-prioritization

## Context

See proposal.md — Why. Constraints:

- Registry tables `links` (with reserved `priority` column and `idx_links_repo(owner,repo)` index), `keys` (statuses, source_url_hash), and date columns populated by changes 1–2 and 5 already exist; this capability is strictly read-side plus one denormalized write target.
- The writer-thread pattern (single SQLite connection, bounded queue, batches) is established; any rescore writes must go through it to preserve WAL concurrency guarantees.
- `PeriodicTaskManager` / run lifecycle hooks (`Pipeline._on_stop`, TaskManager completion events) provide natural sweep trigger points.
- Downstream consumer (clone + TruffleHog extension project) does not exist yet — the export document IS the contract; stability matters more than richness.

## Goals / Non-Goals

**Goals:**
- Deterministic, explainable repository ranking from existing evidence.
- Scores converged with ledger state within one run.
- Versioned, filterable export suitable as a machine-read handoff.

**Non-Goals:**
- No force-push/GH-Archive component (W3 deferred to the extension project by plan).
- No scheduling of actual deep scans (export only; consumption is out of scope).
- No cross-workspace aggregation (one workspace = one registry, v1 limitation stands).

## Decisions

**D1 — Repo-level score denormalized into `links.priority`.**
Group-by `(owner, repo)` via the existing index; every link row of a repo carries the repo score. Alternative rejected: separate `repos` table — adds schema surface and join cost for a value that is a pure function of existing rows; the reserved column was planned for exactly this. Trade-off accepted: rescoring touches multiple rows (batched UPDATE, cheap under the writer thread).

**D2 — Formula and defaults (all configurable under `prioritization.weights`).**
```
score = W1·valid_present + W2·soft_present + W4·exp(−ln2·age_days/H) − W5·clamp((size_kb−T)/(R−T), 0, 1)
defaults: W1=100, W2=40, W4=30, H=30 days, W5=20, T=50_000 KB (~50 MB), R=500_000 KB (~500 MB)
valid_present  = ∃ key with status 'valid' linked to this repo
soft_present   = ∃ key with status 'wait_check' or 'no_quota' linked to this repo
age_days       = (now − max(repo_pushed_at over repo rows)) in days; NULL ⇒ component 0
size_kb        = max(repo_size_kb over repo rows); NULL ⇒ penalty 0
```
Key→repo attribution: `keys.source_url_hash → links.url_hash → (owner,repo)`; keys lacking a source link (legacy imports) attribute via masked-ref match where possible, else contribute only to global stats, not repo scores (documented limitation). Rationale for magnitudes: W1 dominates (locality/amplification evidence), W2 secondary (live repo, temporarily refused provider), freshness comparable to soft evidence, penalty capped below W2 so size alone never buries a valid-key repo.

**D3 — Recompute: dirty-set + run-finish sweep.**
Writer thread maintains an in-memory dirty repo set; key-status upserts and date merges enqueue `(owner,repo)`; each batch flush rescores dirty repos (two indexed queries + batched UPDATE). Full sweep at run finish iterates `DISTINCT owner,repo` — bounded cost (≈ms per repo), guarantees convergence even for mutations that bypassed dirty-marking (migration imports, manual DB edits). No periodic timer needed inside a run; between runs nothing changes.

**D4 — Export tool `tools/export_candidates.py`.**
Standalone CLI (argparse, mirrors `registry_migrate.py` conventions): `--workspace` (required), `--format ndjson|csv` (default ndjson), `--min-priority`, `--limit`, `--sample-links N` (default 5). Opens the registry read-only (`file:...?mode=ro`), streams rows (no full materialization), emits `schema_version: "1.0"`. Sort: `priority DESC, repo_pushed_at DESC NULLS LAST, owner, repo`. Record fields: schema_version, owner, repo, priority, best_status (rank valid>wait_check>no_quota>invalid>none), status_counts{}, repo_pushed_at, repo_size_kb, links_total, sample_links[]. The field list is frozen by the spec scenario test — additions require a schema_version bump.

**D5 — Status visibility optional and off.**
`prioritization.display_top_n` (default 0 = off); when >0, StatusManager renders one extra line with top-N owner/repo:priority. Zero-risk cosmetic; excluded from spec requirements (implementation detail), covered by a task.

## Risks / Trade-offs

- [Score staleness between mutation and batch flush] → bounded by batch interval; run-finish sweep is the hard convergence guarantee.
- [Legacy keys without source attribution skew status_counts low] → documented; migration improvement (attribute via summary.json provider mapping) left as future work, export includes `links_total` so consumers can sanity-check coverage.
- [Weight tuning is empirical] → all knobs configurable; export enables offline what-if re-ranking without touching the DB (consumer can re-sort by raw fields).
- [Large registries → big exports] → streaming + `--limit`; NDJSON tolerates partial consumption.
- [Denormalized priority duplicated across rows] → single-writer invariant makes inconsistency impossible within a run; sweep heals any external tampering.

## Migration Plan

Pure addition: deploy, run once (sweep populates `links.priority`), export. No flags needed for correctness (read-side); rollback = stop using the tool (priority column simply goes stale, harming nothing).

## Open Questions

- None blocking. (Whether the extension project prefers direct SQLite access over NDJSON was plan open question #3 — resolved: NDJSON is the contract, SQLite path documented as an advanced option.)
