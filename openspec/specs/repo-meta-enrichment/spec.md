# repo-meta-enrichment Specification

## Purpose

Restores the repository-level evidence supply killed by the September 2026 probe (trimmed search-API `repository` objects; client-rendered blob timestamps) through a TTL-cached, ETag-conditioned REST acquisition channel: gives gather-skip condition 3 real push-invalidation teeth, reactivates prioritization components W4/W5, and provides the future deep-scan extension with size guards and takedown signals — at a controlled, decaying quota cost of at most one request per unique repository per TTL window.

## Requirements

### Requirement: Repository metadata acquisition

When enrichment is enabled and at least one API credential is available, the system SHALL fetch repository metadata via authenticated REST `GET /repos/{owner}/{repo}` at most once per unique `(owner, repo)` per TTL window, persisting `pushed_at`, `size_kb`, `default_branch`, `etag`, `fetched_at` into the `repos` cache through the registry writer thread. When the response carries the corresponding fields (opportunistic contract — field presence is GitHub's decision, not ours), values SHALL propagate into `links.repo_pushed_at`/`repo_size_kb` for that repository's links via the non-regression COALESCE merge (UPDATE-only; never fabricates link rows). HTTP 404 SHALL mark `gone=1` without exception and suppress retries within the TTL window. Any other failure SHALL degrade fail-open: NULL propagation, counted warning, pipeline continues unblocked.

#### Scenario: Successful fetch populates cache and link columns

- **WHEN** a novel repository's blob URL is gathered and `GET /repos/{o}/{r}` returns 200 with `pushed_at` and `size` fields present
- **THEN** a `repos` row persists with those values plus etag and fetched_at, and all `links` rows of that repository carry non-NULL `repo_pushed_at`/`repo_size_kb` after merge

#### Scenario: Taken-down repository marks gone

- **WHEN** the REST call returns 404 for a cached or novel repository
- **THEN** its `repos` row records `gone=1`, no exception reaches the worker loop, and further encounters within the TTL window issue no requests

#### Scenario: Transient failure degrades fail-open

- **WHEN** the REST call returns 5xx or raises a network error
- **THEN** `enrichment_failures` increments, a warning is logged once per batch, link date columns remain NULL, and gathering proceeds normally

### Requirement: Conditional refresh economics

Cache entries older than the configured TTL SHALL be refreshed with `If-None-Match: <stored etag>`. A `304 Not Modified` response SHALL bump `fetched_at` while preserving all stored values and SHALL require no merge work; a `200` response SHALL replace `pushed_at`/`size_kb`/`default_branch`/`etag` and re-trigger link-column merge. Zero rate-limit accounting of 304s is documented GitHub behavior whose live confirmation was an early implementation task (probe, completed September 2026: `rate_limit_delta = 0`), not a spec guarantee.

#### Scenario: Unchanged repository refreshes via 304

- **WHEN** a TTL-expired entry is conditionally refreshed and the server answers 304
- **THEN** stored values and etag are unchanged, `fetched_at` advances, `enrichment_304s` increments, and no links merge is performed

#### Scenario: Changed repository replaces values

- **WHEN** a conditional refresh answers 200 with a newer `pushed_at`
- **THEN** cache fields and etag are replaced and the repository's link columns update through the merge channel

### Requirement: Cache-first lazy triggering

Every enrichment need SHALL consult the cache first; entries fresh within TTL SHALL be served with zero network requests. Fetches trigger lazily at two points: (a) AcquisitionStage encountering a repository that is uncached or stale; (b) the gather-skip evaluator encountering stale cache for candidate links (wired against the landed add-gather-skip evaluator, under a bounded per-batch wall-clock budget). Duplicate encounters of the same repository within a batch, page, or run SHALL coalesce into a single fetch.

#### Scenario: Fresh cache serves offline

- **WHEN** every repository on a processed page has a cache entry younger than TTL
- **THEN** zero enrichment HTTP requests occur during that page's processing

#### Scenario: Stale cache at skip evaluation triggers one shared refresh

- **WHEN** a skip-evaluation batch contains five links belonging to one stale-cache repository
- **THEN** exactly one conditional refresh is issued and its outcome serves all five decisions; if the refresh fails, decisions proceed fail-open (NULL push evidence passes condition 3 vacuously)

#### Scenario: Duplicate encounters fetch once

- **WHEN** the same novel repository appears across 30 links on two pages within one run
- **THEN** exactly one fetch for it is recorded in `enrichment_fetches`

### Requirement: Credential and throttling inheritance

Enrichment requests SHALL ride the shared GitHubClient under service type `github_api`, inheriting per-credential adaptive token buckets, cooldown state, `GithubCredentialLimited` rotation, and server wait headers (`Retry-After`, `X-RateLimit-Reset`). When no usable token exists (web-only deployment, or all tokens cooling beyond search's own needs), enrichment SHALL silently self-disable: zero requests, NULL dates, pipeline proceeds, at most one informational log line.

#### Scenario: Rate-limited credential rotates like search

- **WHEN** an enrichment fetch hits a rate-limit signal (429/403-markers)
- **THEN** the credential is marked limited exactly as search does, and the fetch either retries with the next available credential or abandons fail-open without blocking the worker

#### Scenario: Tokenless deployment disables silently

- **WHEN** the token pool is empty and the harvester runs web-transport-only
- **THEN** zero enrichment requests are issued, date columns stay NULL, and no errors beyond one informational log are produced

### Requirement: Flag isolation and crash-safe durability

With `enrichment.enabled=off` (default) the subsystem SHALL be indistinguishable from pre-change behavior: zero extra requests, zero writes beyond the pre-existing ② channels, no metrics emitted. Every completed fetch SHALL persist through the writer thread before being considered done — the cache IS the work ledger: after crash and restart, remaining work is exactly the queryable stale-or-missing set, resumed idempotently with no duplicated fetches for persisted entries.

#### Scenario: Disabled flag means zero footprint

- **WHEN** a full staging run executes with `enrichment.enabled=off`
- **THEN** network request counts and registry writes are identical to the pre-change baseline (A/B) and no enrichment metrics appear

#### Scenario: Crash-resume completes without redo

- **WHEN** the process is killed mid-run after some repositories were persisted, then restarted
- **THEN** the resumed run fetches only stale-or-missing entries and previously persisted repositories produce zero new fetches within their TTL
