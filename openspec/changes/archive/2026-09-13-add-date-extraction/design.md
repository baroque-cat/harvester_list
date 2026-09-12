# Design: add-date-extraction

## Context

See proposal.md — Why. Constraints shaping the approach:

- Both API parse points (`search/client.py:741-754`, `:848-865`) currently build a plain `set` of `html_url` strings consumed by `SearchStage`; several callers iterate that set. Changing the return type would ripple through stage code.
- `collect()` already downloads the full blob HTML page for key extraction. The page was expected to embed commit datetimes in `<relative-time datetime="…">` custom elements — **invalidated by live probe, see D7**: current GitHub serves blob pages with client-side-rendered timestamps; the served HTML carries no `datetime` attributes. No DOM parser exists in the project (regex-only convention).
- The link registry (add-link-registry) reserves nullable columns `repo_pushed_at`, `repo_size_kb`, `file_commit_date` and accepts batched upserts from a single writer thread.
- Stage outputs flow through `StageOutput` (`stage/base.py:57-81`) into `Pipeline._handle_stage_output` — the established channel for stage→storage side data.

## Goals / Non-Goals

**Goals:**
- Capture repo freshness + size (API) and file commit date (gather) with **zero additional HTTP requests** whenever the payloads expose them.
- Keep every existing contract intact (URL sets, shard contents, task graph).
- Strictly fail-open parsing with drift observability (fill rates) — absence of data is a first-class, measured outcome, not an error.

**Non-Goals:**
- No exact per-file commit lookup via `GET /repos/{o}/{r}/commits?path=` (costs REST quota per file; reserved as a future targeted tool if ever needed).
- No REST-based repository metadata fetching (`GET /repos/{o}/{r}`): deliberately separated into the `add-repo-meta-enrichment` change with its own quota policy, cache table, and flag — this change stays zero-additional-requests.
- No date parsing from web search-results pages (deliberate asymmetry, see spec).
- No consumption of dates for decisions — gather-skip reads them in the next change.

## Decisions

**D1 — Parallel enrichment mapping instead of changing return types.**
API search functions keep returning the URL set and additionally populate a `Dict[url, LinkMetadata]` (dataclass: `repo_pushed_at: float|None`, `repo_size_kb: int|None`, `transport: str`) passed back via a second return value or an out-parameter on the client. Alternative rejected: returning rich objects everywhere — breaks `set` semantics (dedup by URL) used by callers and dedup logic in stages.

**D2 — MAX over all `relative-time` datetimes on the page.**
A blob page contains several relative-time elements (file header "last commit", sidebar entries) — when present at all (see D7). Taking the maximum biases freshness upward. Asymmetry of errors: overestimated date → `repo_pushed_at > gathered_ts` fires → extra re-gather (cheap, safe); underestimated date → false skip (missed leak, dangerous). Alternative rejected: first-match — depends on element order in a React-rendered page, brittle in the unsafe direction.

**D3 — Field preference chain.**
`pushed_at` (last push — the event that changes files) preferred; `updated_at` fallback (includes metadata-only changes — acceptable overestimate, same safe direction); else NULL. `size` is KB per GitHub API docs and feeds the future clone-size guard. Timestamps parsed with `datetime.strptime`/`fromisoformat` handling the `Z` suffix → epoch float.

**D4 — Non-regressing merge in the registry writer (amended wording, verification finding W3).**
Upsert semantics use `COALESCE(excluded.col, links.col)` for the three metadata columns: NULL never erases a known value; non-NULL replaces (dates grow monotonically in practice; replacement keeps the freshest observation). Implementation: a dedicated op-kind `_OP_METADATA` **inside the existing batched writer** (same queue, same writer thread, same transaction, executed last within the batch) with its own UPDATE-only SQL — not a separate write path. Metadata ops SHALL NOT insert rows for unknown `url_hash` (verification finding S2: an INSERT branch could fabricate `first_seen_ts` phantom links); affected-rows==0 is counted once per kind at debug level.

**D5 — Carrier: optional `link_metadata` field on StageOutput.**
Additive field ignored by stages/handlers that don't know it; `Pipeline._handle_stage_output` forwards it to the registry hook alongside `add_links`. Mirrors how `results`/`models` already travel.

**D6 — Fixtures + drift monitoring (amended: provenance rule, verification finding C1).**
Fixtures for external-contract scenarios MUST be live-captured samples with provenance headers (endpoint, capture date, auth mode); synthetic fixtures are supplementary unit vectors and must be labeled as such. Process gap found during verification: the original fixture set was synthesized around the *expected* payload shape ("well-formed" items carrying `pushed_at`), so the offline suite validated against an imagined contract while production returned none. Amnesty task 7.5 adds live-captured samples (trimmed API JSON page, client-rendered blob HTML) as the authoritative fixtures; the synthetic ones remain as labeled extraction-path unit vectors. Production drift surfaces via `date_fill_rate_*` (visible in StatusManager stats — verification finding W2; threshold alerting left to ops, not coded).

**D7 — Live probe findings (September 2026 staging, amnesty record).**
Empirical results with real credentials (secrets read at runtime, never printed/committed):
1. `GET /search/code` returns a **trimmed `repository` object** — `pushed_at`, `updated_at`, `size` are absent (only URL templates, `full_name`, `owner`, `private`, etc.). Observed `date_fill_rate_api = 0.000` over 20 items, warnings empty. Full repository objects (with dates/size) are served only by `GET /repos/{o}/{r}` and `/search/repositories`. (Amnesty task 7.6 added the `missing_date` counter, so field-absence — not only a missing `repository` block — is now explicit in `get_date_parse_stats()`; that is why the live-captured fixture pin can assert a counted warning.)
2. Served blob HTML contains **no `<relative-time datetime>` attributes**: `relative-time-element` is loaded as a JS module (`<link rel="modulepreload">`) and timestamps render client-side. The only ISO date in markup is the repository `createdAt` from embedded JSON — useless for invalidation. Observed `date_fill_rate_web = 0.000` over 2 authenticated public blob pages.
Consequences (all recorded across the roadmap): parsers stay as dormant traps (zero cost, auto-resume if GitHub restores fields); date supply moves to `add-repo-meta-enrichment` (cached REST, ETag economics); downstream consumers treat NULL dates as the normal state — this drove the `add-gather-skip` spec amendment (NULL `repo_pushed_at` passes condition 3 vacuously; TTL-only degradation is the prevailing mode, per that change's design D3) and its `push_signal_coverage` metric. Baselines 0.000/0.000 are documented expectations, not failures.

## Risks / Trade-offs

- [GitHub redesign removes/renames `relative-time`] → **materialized** (D7.2): dates NULL; skip logic degrades to TTL-only, which is safe; fill-rate metric makes the state visible within one run.
- [MAX bias picks an unrelated newer date (e.g., embedded repo-level widget)] → extra re-gathers only; bounded by gather-TTL economics of the next phase. Currently moot (no markers served) but retained for the dormant-trap path.
- [API schema assumption — **materialized**] → the `repository` object in `/search/code` proved trimmed despite version pinning (`X-GitHub-Api-Version: 2022-11-28`); parsers degrade to NULL (scenarios S2/S11); supply restored via `add-repo-meta-enrichment`. Lesson generalized into `openspec/config.yaml` rules (probe-before-spec).
- [Enrichment mapping memory on huge result sets] → bounded by page sizes (≤100 items/page); negligible.
- [Fill-rate counters under concurrency — verification finding W1] → `DateFillMetrics` guards increments with a `threading.Lock` (project `stats_lock` pattern); module-level parse-warning counters reset at metrics construction (finding S1).

## Migration Plan

Pure additive parsing behind no flag (fail-open by construction): deploy → observe `date_fill_rate_api` / `date_fill_rate_web` — **documented baseline is 0.0/0.0 (D7)**; rates rise only when GitHub restores the fields or when `add-repo-meta-enrichment` begins feeding the columns. Registry columns stay NULL until then; all consumers tolerate NULL by spec. Rollback = revert commit; NULL columns are tolerated by all consumers.

## Open Questions

- None blocking. Resolved by D7: "are the free sources sufficient?" — NO; repo-level REST enrichment is spun off as `add-repo-meta-enrichment`. Exact-commit-date lookup (`/commits?path=`) remains deferred; revisit only if repo-level `pushed_at` proves too coarse in production skip metrics.
