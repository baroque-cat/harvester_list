# Proposal: add-target-prioritization

## Why

After five changes the registry holds everything needed to answer the question that motivated this whole roadmap: **"which repositories deserve deep scanning first?"** Field evidence says leaks cluster — a repo that yielded one valid key statistically yields more (platform-locality and amplification effects), active repos keep leaking, and oversized repos cost disproportionate effort. Today nothing orders the harvested corpus: links sit in insertion-order shards, and any future deep-scan consumer (clone + TruffleHog pipeline — a separate project) would have to re-derive targeting from raw files. This change turns the registry into an ordered, exportable target list — the stable handoff bridge to the extension project — as a purely read-side capability with zero pipeline risk.

## What Changes

- **Priority scoring:** a configurable weighted score per repository, denormalized into `links.priority`:
  `priority = W1·[repo has VALID key] + W2·[repo has wait_check/no_quota key] + W4·freshness(repo_pushed_at) − W5·size_penalty(repo_size_kb)`
  with documented defaults (W1=100, W2=40, W4∈[0,30] exponential decay ≈30-day half-life, W5∈[0,20] threshold ramp). The force-push component (W3, joins against GH Archive datasets) is deliberately deferred to the extension project.
- **Recomputation:** event-driven (key-status upserts, date updates enqueue the repo for rescoring via the writer thread, batched) plus a full sweep at run finish — scores never lag behind ledger state by more than one run.
- **Export utility `tools/export_candidates.py`:** read-only CLI producing a deterministic, schema-versioned NDJSON dump (default; resolves plan open question #3) with optional `--csv`, aggregating per repository: owner/repo, best key status observed, counts by status, repo_pushed_at, repo_size_kb, priority, sample link URLs, deep-scan readiness flags. Sorted by priority desc, pushed_at desc, url_hash tiebreak.
- **Optional ops visibility:** top-N candidates line in StatusManager display (config-gated, off by default).
- Missing data degrades neutrally: NULL dates/sizes contribute zero to their components; scoring never crashes on sparse rows.

## Capabilities

### New Capabilities
- `target-prioritization`: repository-level priority scoring over accumulated registry evidence (key statuses, freshness, size) with event-driven plus swept recomputation, and a deterministic versioned export of deep-scan candidates for downstream consumers.

### Modified Capabilities
<!-- none: strictly read-side over link-registry/date-extraction/key-ledger data; the only registry write is the pre-existing reserved links.priority column -->

## Impact

- **New code:** scoring module (`storage/priority.py` or `tools/priority.py`), `tools/export_candidates.py`, optional StatusManager extension.
- **Modified code:** `storage/registry.py` (rescore hooks in writer batch path + sweep query), `manager/pipeline.py` or `manager/task.py` (run-finish sweep trigger), config schemas/loader (weights + display flag), `state/display.py` (optional line).
- **Depends on:** add-link-registry (schema, indexes incl. `idx_links_repo`), add-date-extraction (freshness/size inputs), add-key-ledger (status evidence). Functional with partial data (earlier phases simply leave components at zero).
- **Behavior:** pipeline decisions untouched — scoring affects only `links.priority` values and offline exports; no network activity; no shard format changes.
- **Downstream:** the export document doubles as the **handoff contract** for the future clone/TruffleHog/dangling-commits project (stable field names, `schema_version`).
