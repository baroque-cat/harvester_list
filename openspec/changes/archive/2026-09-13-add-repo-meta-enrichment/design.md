# Design: add-repo-meta-enrichment

## Context

See proposal.md — Why. Constraints:

- The amnesty probe (② design D7, September 2026) established: `/search/code` `repository` objects are trimmed; blob-page file timestamps are client-rendered. Both ② parsers remain as dormant traps; this change is the restored supply line, not a replacement of ②'s channel.
- Registry conventions are fixed: single writer thread, additive schema evolution via `user_version`, read-only connections for readers, fail-open degradation, UPDATE-only `_OP_METADATA` merge with COALESCE non-regression (② amended D4).
- GitHubClient infrastructure (per-credential TokenBuckets since commit 0c9c9dc with adaptive `adjust_rate`, `GithubCredentialState` cooldowns, `GithubCredentialLimited` rotation, wait-header extraction) is the mandatory transport — no parallel HTTP stack.
- Per config rule: every external-format assumption below references the planned live-captured fixture (task 1.1 probe, `tests/fixtures/live_*_repos_*`); the spec text itself is written as an opportunistic contract.

## Goals / Non-Goals

**Goals:**
- Non-NULL `repo_pushed_at`/`repo_size_kb` for actively encountered repositories at steady-state quota cost ≈ 304-dominant conditional refreshes.
- Crash-safe incremental completion with the cache as the only work state.
- Zero footprint when disabled; zero requests when tokenless.

**Non-Goals:**
- No file-level dates (`GET /repos/{o}/{r}/commits?path=` — alive but expensive; deferred until a consumer genuinely needs per-file precision; condition 3 does not).
- No offline `git log` dating (extension-project territory, post-clone).
- No Atom-feed (`commits.atom`) tokenless fallback — considered and DEFERRED: doubles the parsing surface for a park (web-only deployments) whose skip regime is TTL-only by design anyway.
- No warm-up sweep CLI (pre-populating cache for the whole registry before a run) — deferred; lazy triggers + rate limiter bound the cold-start burst adequately. Revisit from staging metrics.
- No GH Archive / force-push data (W3 component of ⑥; separate future change).

## Decisions

**D1 — Source: authenticated REST `GET /repos/{owner}/{repo}`.**
Rejected alternatives: `/search/repositories` (no cheaper per-repo, adds query-construction complexity); HTML repo-page scrape (client-rendered timestamps — the exact failure mode D7 of ② just proved; brittle); Atom `commits.atom` (tokenless-capable but deferred per Non-Goals). REST repo endpoint is the documented, versioned (`X-GitHub-Api-Version: 2022-11-28`) contract with stable `pushed_at`/`size`/`default_branch` fields — confirmed by probe before tests are authored (task 1.1).

**D2 — Additive `repos` table + `user_version` bump.**
```sql
CREATE TABLE repos(
  owner TEXT NOT NULL, repo TEXT NOT NULL,
  pushed_at REAL, size_kb INTEGER, default_branch TEXT,
  etag TEXT, fetched_at REAL NOT NULL,
  gone INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(owner, repo)
);
```
Separate from `links` deliberately: one row per repository vs many per repo; lifecycle (TTL refresh) differs; keeps `links` write path untouched. `links.repo_*` columns remain the denormalized consumption surface for ③/⑥ queries (no joins in hot paths).

**D3 — ETag mechanics and 304 economics.**
Store raw `ETag` response header verbatim; send as `If-None-Match` on refresh. 304 → touch `fetched_at` only (single-column UPDATE via writer queue; no merge op enqueued). 200 → full replace + enqueue a links-merge for that repo's rows through a dedicated repo-scoped writer op (`_OP_REPO_LINKS`: `UPDATE links SET repo_pushed_at/repo_size_kb = COALESCE(...) WHERE owner=? AND repo=?`) — the ② `_OP_METADATA` COALESCE non-regression pattern re-keyed by `(owner,repo)` instead of `url_hash`; UPDATE-only like its ② sibling, so unknown repositories affect zero rows and no phantom link rows can appear. The "304 costs zero rate-limit units" claim is GitHub-documented but treated as a hypothesis until the task-1.1 probe measures `X-RateLimit-Remaining` deltas across a 200/304 pair on staging credentials.

**D4 — Trigger points, dedup, TTL gate.**
(a) AcquisitionStage worker, after successful collect: extract `(owner,repo)` from the task URL (already parsed for canonicalization — reuse), check cache freshness, enqueue fetch if stale/missing. (b) SearchStage skip-evaluator (gated task 4.2, wired only when ③ is implemented): before evaluating a page batch, resolve distinct stale repos within the batch and refresh them synchronously under a per-batch wall-clock budget (default 60 s, checked between repositories; an in-flight fetch always completes). Truncated work is not lost — the cache is the ledger, so the remaining stale-or-missing set is picked up by later triggers (fail-open). Dedup: per-batch set + per-run in-flight set keyed `(owner,repo)`; the writer-side upsert makes races harmless (last-writer-wins on identical data). TTL gate: `fetched_at > now − ttl_hours·3600` ⇒ serve from cache. Default TTL 24 h balances quota against push-invalidation latency (a repo pushed today is seen by tomorrow's run at latest; gather-TTL of ③ is 7 d, so 24 h refresh keeps condition-3 evidence ~7× fresher than the skip window it guards).

**D5 — Circularity resolution (skip needs fresh dates; fetching dates costs requests).**
The apparent loop — "need `repo_pushed_at` to decide skip, but want skip to avoid requests" — resolves economically: one metadata GET (or free 304) per *repository* replaces a blob GET + regex extraction + potential key-check cascade per *link*; repositories average multiple links, so amortized cost is a fraction of the work it prevents. Where cache is stale at decision time, ③'s amended semantics apply: NULL/stale push evidence passes condition 3 vacuously (TTL-only) — the decision never blocks on enrichment.

**D6 — Shared `github_api` bucket, deliberately.**
Enrichment competes with search for the same per-credential buckets. Alternative (separate `github_enrich` service type with own buckets) rejected: a token throttled by GitHub does not distinguish callers; double-spending it from two independent buckets invites secondary rate limits. Inherited adaptive `adjust_rate` (3 consecutive failures halve, 10 successes +10%, cap 2×) automatically backs off enrichment during contention. When **every** API token is cooling down, enrichment yields through a non-blocking probe (`tokens_cooling_down()`: zero requests, one info log, `cooling_skips` counter, transient — it resumes within the same run once a token recovers) instead of queueing behind `Credentials.get_token()`'s cooldown sleep, which remains search's own blocking policy. Priority note: search tasks are the product; enrichment fetches yield to pending search demand naturally because bucket waits are FIFO-ish per credential — acceptable, measured in staging (task 6.3).

**D7 — Durability: cache-as-work-ledger.**
No separate queue, journal, or checkpoint file: `repos.fetched_at` IS the progress record. Writer-thread persistence before "done" (WAL survives `kill -9`; change-1 S19 guarantees structure). Resume query: `SELECT ... WHERE fetched_at IS NULL OR fetched_at < now−ttl OR (owner,repo) NOT IN cache` over encountered repos — idempotent by construction. This mirrors the philosophy that made ①'s migration safe: state lives in the durable store, never in process memory.

**D8 — `gone` flag semantics.**
404 marks a takedown (deleted/privated repo — common after leak disclosure). Effects: no retry within TTL (quota respect); links rows preserved (audit log immutable); flag exposed in ⑥ export (deep-scan consumers treat `gone=1` as dangling-commit candidates — the repo's objects may still be fetchable by SHA, cf. TruffleHog force-push research) and in StatusManager counts. No auto-cleanup of links/keys — registry is memory, not garbage collector.

## Risks / Trade-offs

- [Quota contention with search on small token pools] → bounded unique-repo count, TTL decay, shared adaptive buckets, flag-off rollback; staging measurement gates production enablement (task 6.3).
- [Inherited client weaknesses: plain 401/403 do not mark credentials; dead sessions only logged] → fail-open covers (NULL + counters); explicitly recorded so verification does not classify known behavior as new defects.
- [Cold-start burst on first enabled run (every repo novel)] → spread by rate limiter across pool; optional warm-up sweep deferred (Non-Goals) — revisit only if staging shows pain.
- [Clock skew between hosts] → UTC epoch floats everywhere (convention since ②); TTL comparisons tolerate ±minutes.
- [`size` field semantics (KB, whole-repo incl. history) imprecise for clone-cost prediction] → adequate for W5 penalty banding; extension project may re-measure post-clone.
- [GitHub trims `/repos` payload someday (moving-target lesson)] → opportunistic contract: absent fields → NULL + drift visible via fill-rate metrics; same amnesty machinery applies.

## Migration Plan

1. Deploy `enrichment.enabled=off` (zero footprint, S11).
2. Early probe (task 1.1) on staging credential: capture live fixtures, confirm field presence + ETag/304 behavior + rate-limit accounting of 304s.
3. Staging `on`: observe quota headroom (`X-RateLimit-Remaining` trends), `date_fill_rate_api` rise, ③ `push_signal_coverage` rise (with skip_known in shadow), enrichment_304s share on second run.
4. Production `on` after numbers match the cost model (≈1 fetch/unique-repo/TTL; 304-dominant steady state).
5. Rollback: flip flag off; `repos` table remains harmlessly (additive schema).

## Open Questions

- Atom-feed fallback for tokenless parks — deferred (Non-Goals); reconsider only if web-only deployments become a primary use case.
- Warm-up sweep tool — deferred; staging metrics decide.
- Whether ⑥ export should rank `gone=1` repos above or below live ones for dangling scanning — extension-project decision; export exposes the flag either way.
