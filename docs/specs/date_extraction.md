# Date Extraction (free freshness metadata)

**Owning change:** `add-date-extraction`
**Depends on:** `add-link-registry` (reserved columns `repo_pushed_at`,
`repo_size_kb`, `file_commit_date`)

The harvester captures freshness data **for free** from HTTP payloads it
already downloads — no extra requests, no extra quota. The values land in the
link registry and are the invalidation signal the later gather-skip change
reads (`repo_pushed_at > gathered_ts` ⇒ re-gather).

## Transport asymmetry (by design)

| Transport | Where dates are captured | What is captured |
|-----------|--------------------------|------------------|
| API (`use_api: true`) | search stage, from the code-search JSON | `repository.pushed_at` (fallback `updated_at`) → `repo_pushed_at`; `repository.size` → `repo_size_kb` |
| Web (`use_api: false`) | gather stage only, from the blob HTML | `file_commit_date` (MAX of `<relative-time datetime="…">`) |

Web **search-results** HTML is deliberately **not** parsed for dates: that
surface is brittle (it changes often) and parsing it would widen the fragile
area for no required gain. Consequently a link discovered via the web transport
carries NULL date metadata until it is gathered.

## Field preference and parsing

- `repository.pushed_at` is preferred over `updated_at`: a push is the event
  that changes file contents, whereas `updated_at` also moves on metadata-only
  edits — an acceptable overestimate in the safe direction.
- All timestamps are normalised to UTC epoch seconds (`Z` suffix handled).
- `repository.size` is recorded as-is (KB, per GitHub API docs).

## MAX-bias rationale for `file_commit_date`

A blob page contains several `relative-time` elements (file header, sidebar,
related entries). The extractor records the **maximum** of all matches. This
biases the recorded date upward on purpose because the errors are asymmetric:

- **Overestimate** → `repo_pushed_at`/`file_commit_date` looks newer than the
  gather → at most one extra re-gather (cheap, safe).
- **Underestimate** → a changed file can be falsely skipped (missed findings —
  dangerous).

When no elements match (layout drift, non-blob page), the value degrades to
NULL and a counter increments; gather-skip then falls back to TTL-only, which
is the safe direction.

## Fail-open guarantees

No date-extraction fault propagates into a stage worker loop. Malformed JSON,
unexpected item types, missing `repository` blocks, repositories present but
without any usable `pushed_at`/`updated_at` (`missing_date` — the prevailing
production case since the September 2026 probe, design D7), undecodable bytes
and unparseable datetimes all yield NULL fields plus a counted warning
(once-per-kind log). Counters are exposed via
`search.client.get_date_parse_stats()`.

## Registry merge policy

Metadata reaches the registry through `StageOutput.link_metadata` →
`Pipeline._handle_stage_output` → `Registry.record_metadata`, and is merged with
`COALESCE(excluded, links)` semantics:

- a NULL observation **never** erases a known value;
- a non-NULL observation **replaces** the stored one (dates grow monotonically).

## Metrics interpretation

| Metric | Formula | Healthy signal |
|--------|---------|----------------|
| `date_fill_rate_api` | API items with a usable `repo_pushed_at` ÷ API items processed | ≈ 1.0; well below 1.0 suggests an API schema change |
| `date_fill_rate_web` | gathered pages with a `file_commit_date` ÷ pages gathered | record the observed baseline; a collapse towards 0 indicates GitHub blob-layout drift |

Both are visible per run in `PipelineStatus.date_metrics` (see
`docs/specs/registry_flags_metrics.md`). Threshold alerting is intentionally
left to operators, not coded.
