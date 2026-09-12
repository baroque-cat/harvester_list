# Proposal: add-date-extraction

## Why

Skip-based optimizations (gather-skip, early-stop) are blind to content changes unless links carry dates. Today both search transports discard every timestamp that passes through their hands: the API parser keeps only `item.html_url` from each JSON item, and the gather stage fetches the full blob HTML page but extracts only key patterns. The original premise — that dates can be captured **for free** from payloads already downloaded (`repository.pushed_at`/`size` in search JSON; `<relative-time datetime>` in blob HTML) — was **invalidated by live staging probe (September 2026, design D7)**: GitHub serves trimmed `repository` objects in `/search/code` and renders blob timestamps client-side. What remains true and valuable: the delivery channel (parsers → StageOutput → non-regressing registry merge), strict fail-open behavior, and fill-rate observability. This change ships that channel as a dormant trap and drift detector; the actual supply of dates is restored by the follow-up `add-repo-meta-enrichment` change (cached REST `GET /repos/{o}/{r}` with ETag economics). Dates are the invalidation signal ("repo was pushed after our visit → re-gather") that makes the skip phase precise rather than TTL-only.

## What Changes

- Both API parse points (`search/client.py:741-754`, `:848-865`) additionally read `item.repository.pushed_at` (fallback `updated_at`) and `item.repository.size` **when present**; results are enriched via a parallel `url → metadata` mapping so existing set-based contracts are untouched. Current production payloads lack these fields (D7) → NULLs + counted warnings.
- `collect()` (`search/client.py:~1080-1124`) gains one regex over the blob HTML it already downloads: `<relative-time[^>]*datetime="([^"]+)"`; the maximum of all matches is stored (conservative freshness bias); zero matches → NULL, never an exception — the prevailing production case per D7.
- Documented transport asymmetry: web **search** results (bare hrefs) yield no dates at search stage — for the web transport dates arrive at gather stage only. No fragile parsing of the search-results HTML is introduced.
- Metadata delivery to the registry through the existing `StageOutput` object pattern (`stage/base.py:57-81`); registry upsert policy: NULL never overwrites a known value; newer non-NULL replaces older; UPDATE-only (no phantom row insertion).
- New metrics: `date_fill_rate_api` / `date_fill_rate_web` — **documented baseline 0.0/0.0 (D7)**, doubling as drift detectors (rise = GitHub restored fields or enrichment landed).
- All parsing strictly fail-open: any malformed or absent data yields NULL fields plus a counted warning.

## Post-implementation findings (September 2026 amnesty)

Staging probe (task 5.3) refuted the free-source premise: `date_fill_rate_api = 0.000`, `date_fill_rate_web = 0.000` on live traffic. Root causes recorded in design D7. Consequences across the roadmap:
- This change's contract is amended to opportunistic extraction + safe degradation + observability (spec R1/R2 rewritten; scenarios S11/S12 pin live-captured reality).
- `add-gather-skip` is **NOT blocked**: its spec is amended so NULL `repo_pushed_at` passes condition (3) vacuously — TTL-only degradation is the documented prevailing mode (that change's design D3 always intended this); new metric `push_signal_coverage` exposes evidence quality.
- `add-repo-meta-enrichment` (new change) restores the date supply via cached REST with ETag conditional requests; it feeds the very columns and merge channel this change delivers.
- Verification findings W1–W3, S1–S2 fixed via tasks group 7 before archive.

## Capabilities

### New Capabilities
- `date-extraction`: zero-additional-request capture of repository freshness (`pushed_at`), repository size, and file last-commit date **when payloads expose them**, with transport-specific behavior, non-regressing delivery into the link registry, fill-rate observability, and guaranteed safe degradation to NULL (the current production norm).

### Modified Capabilities
<!-- link-registry delta specs are not modified: its schema already reserves repo_pushed_at/repo_size_kb/file_commit_date columns; this change only fills them -->

## Impact

- **Modified code:** `search/client.py` (two API parse points + `collect()`), `stage/base.py` (StageOutput metadata carrier), `stage/definition.py` (pass-through in Search/Acquisition workers), `storage/registry.py` (non-regressing UPDATE-only merge for date columns), `core/metrics.py` (thread-safe fill-rate counters), `state/display.py` (dates line).
- **Depends on:** add-link-registry (columns + writer must exist).
- **Feeds:** add-gather-skip (condition 3 + `push_signal_coverage`), add-repo-meta-enrichment (merge channel + metrics), add-target-prioritization (W4/W5 inputs).
- **Data:** nullable registry columns exist but stay NULL under current GitHub payloads (baseline 0.0/0.0); no shard format changes.
- **Risks:** materialized — both free sources dead (D7); safe direction confirmed (fail-open, shards and key extraction unaffected). Residual risk lives in downstream expectations, addressed by the gather-skip spec amendment and the enrichment change.
