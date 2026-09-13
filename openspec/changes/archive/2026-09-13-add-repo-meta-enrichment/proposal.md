# Proposal: add-repo-meta-enrichment

## Why

The September 2026 amnesty probe (add-date-extraction design D7) killed both free date sources: live `/search/code` responses carry a **trimmed** `repository` object (no `pushed_at`/`updated_at`/`size`), and blob-page timestamps are **client-side rendered** (server HTML contains no file-level dates). Consequences in production: `links.repo_pushed_at`/`repo_size_kb` stay NULL; gather-skip condition 3 degrades to TTL-only (worst-case miss window = full 7-day TTL); target-prioritization components W4 (freshness) and W5 (size penalty) compute neutral zeros; the future clone/TruffleHog extension loses its size guard.

The only stable, documented source of repository-level metadata is authenticated REST `GET /repos/{owner}/{repo}`. Its cost is **controlled and decaying**: at most 1 request per *unique repository* per TTL window (not per link or file); conditional refresh via `If-None-Match` makes unchanged repositories near-free — a `304 Not Modified` consumes zero rate-limit units (documented GitHub behavior; binding confirmation is the early live probe, task 1.1, per config rule "probe before spec"); repeat runs inside the TTL window make **zero** requests because the cache itself is the work ledger. Worked example: a run over 10 000 links spanning 2 000 repos costs ~2 000 GETs on first run (~40% of a single token's 5 000/h budget, spread across the credential pool and adaptive buckets), ~200 paid units on a next-day run (~90% answered 304), and 0 on same-day reruns — versus up to 10 000 blob-page GETs the gather stage performs anyway (≈20% first-run overhead, then decay).

Deliberately separated from add-date-extraction (which remains the opportunistic zero-cost channel and drift detector): enrichment is a **network subsystem with controlled cost** and deserves its own flag, quota accounting, and failure modes.

## What Changes

- **Additive `repos` cache table** + `PRAGMA user_version` bump:
  `repos(owner TEXT, repo TEXT, pushed_at REAL, size_kb INTEGER, default_branch TEXT, etag TEXT, fetched_at REAL, gone INTEGER DEFAULT 0, PRIMARY KEY(owner, repo))`.
- **Fetcher rides the existing GitHubClient `github_api` service type** — inheriting per-credential adaptive TokenBuckets, `GithubCredentialState` cooldowns [60,900]×2, `GithubCredentialLimited` rotation, and `Retry-After`/`X-RateLimit-Reset` handling. Shares the bucket with search deliberately: a rate-limited token must not be hammered from two subsystems.
- **Propagation into `links.repo_pushed_at`/`repo_size_kb` through the existing COALESCE non-regression merge pattern** (② plumbing reuse as a repo-scoped UPDATE-only `_OP_REPO_LINKS` op keyed by `(owner,repo)` — never inserts phantom link rows).
- **Conditional refresh economics:** cached entry older than TTL → `If-None-Match: <etag>`; `304` bumps `fetched_at` and preserves values; `200` replaces `pushed_at`/`size_kb`/`default_branch`/`etag`.
- **Lazy TTL-gated triggers:** (a) AcquisitionStage — novel or stale-cache repos encountered at gather; (b) SearchStage skip-evaluator — candidates whose cache entry is stale (wired **after** add-gather-skip lands; gated task 4.2). Deduplication per `(owner, repo)` within batch/page/run.
- **404 → `gone=1` takedown signal:** no exception, no retry within TTL; exposed for future dangling-commit targeting and the ⑥ export contract.
- **Fail-open everywhere:** transient failures → NULL propagation + counted warning, pipeline never blocks; **tokenless (web-only) deployments silently disable enrichment** — zero requests, zero cost, documented asymmetry.
- **Config:** `enrichment.enabled` (default **off**), `enrichment.ttl_hours` (default 24) via standard schemas/loader/validator path.
- **Durability — cache as work ledger:** every completed fetch persists immediately through the writer thread (WAL survives `kill -9`); remaining work is exactly the queryable set `fetched_at IS NULL OR fetched_at < now − ttl`; resume is idempotent; there is no separate queue to lose.
- **Metrics:** `enrichment_fetches`, `enrichment_304s`, `enrichment_failures`; fill-rate rise attributable via existing `date_fill_rate_*` counters and ③'s `push_signal_coverage`.

## Capabilities

### New Capabilities
- `repo-meta-enrichment`: TTL-cached, ETag-conditioned REST acquisition of repository metadata (`pushed_at`, `size_kb`, `default_branch`, liveness) for unique repositories encountered by the pipeline, with credential-inherited throttling, fail-open degradation, crash-safe cache-as-ledger durability, and propagation into the link registry through the established non-regression merge.

### Modified Capabilities
<!-- none: ② specs untouched (its parsers remain dormant traps); ③ gains precision through DATA (non-NULL repo_pushed_at), not requirement changes — its amended NULL-tolerant spec already defines both regimes -->

## Impact

- **New code:** `storage/repo_meta.py` (cache core + trigger API), fetch function in `search/client.py` or thin wrapper module, hooks in `stage/definition.py` (AcquisitionStage; SearchStage gated), config section, metrics counters, display line.
- **Depends on:** add-link-registry (writer thread, schema evolution rules, fail-open conventions), add-date-extraction (COALESCE merge channel, `_OP_METADATA` op-kind, fill-rate metrics).
- **Feeds:** add-gather-skip (condition 3 gets teeth; `push_signal_coverage` rises from 0), add-search-early-stop (indirectly — known-semantics unchanged), add-target-prioritization (W4/W5 alive), future extension project (size guard + `gone` takedown list).
- **Quota model:** bounded by unique-repo count × TTL windows; cold-start burst spread by adaptive buckets and pool rotation; steady-state ≈ free (304-dominant). Rollback: flag flip to `off` — zero footprint guaranteed by S11.
- **Known inherited weaknesses (explicit):** the shared client does not mark credentials on plain 401/403 and only logs dead sessions; fail-open covers both (NULL dates, counted warnings) — recorded here so verification is not surprised.
