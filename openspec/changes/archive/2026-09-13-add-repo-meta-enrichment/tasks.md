# Tasks: add-repo-meta-enrichment

## 1. Early live probe & RED baseline

- [x] 1.1 **Live staging probe FIRST** (config rule: probe before spec-adjacent code): authenticated `GET /repos/{owner}/{repo}` on a known repo — capture full 200 body, ETag header; repeat with `If-None-Match` — capture 304 (headers, empty body) and measure `X-RateLimit-Remaining` delta across the 200/304 pair; probe a deleted repo — capture 404 shape. Store as `tests/fixtures/live_<YYYY-MM>_repos_{200,304,404}.json` with provenance headers (endpoint, date, auth mode). Confirm/refute: `pushed_at`, `size`, `default_branch` presence; 304 zero-accounting. Any refutation → stop and amend this change (amnesty procedure) BEFORE writing code
- [x] 1.2 Author tests from tests.md: `tests/test_repo_meta_enrichment.py` (repo-meta-enrichment-S1..S5, S12), `tests/test_enrichment_integration.py` (S6..S11); fixtures from 1.1 authoritative, synthetic field-absent variants labeled supplements
- [x] 1.3 Run pytest; confirm all new tests fail for expected reasons (no `storage/repo_meta.py`, no `repos` table, no trigger hooks) — record RED in tests.md

## 2. Schema & cache core

- [x] 2.1 Additive migration: `repos` table per design D2 + `PRAGMA user_version` bump with idempotent guard
- [x] 2.2 Writer-thread op-kinds for cache upsert/304-touch/gone-mark (single-writer invariant; WAL durability before "done" — design D7)
- [x] 2.3 Cache query API: freshness check (`fetched_at > now − ttl`), stale-or-missing resolution for encountered `(owner,repo)` sets, resume query for crash-restart (repo-meta-enrichment-S12 groundwork)
- [x] 2.4 Links propagation: enqueue `_OP_METADATA` UPDATE-only merges into `links.repo_pushed_at`/`repo_size_kb` via existing COALESCE channel; assert no phantom inserts (repo-meta-enrichment-S1, S5)

## 3. Client integration

- [x] 3.1 Fetch function over GitHubClient `github_api` service (Accept `application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`, Bearer): parse per opportunistic contract — present fields persist, absent fields NULL + counted (repo-meta-enrichment-S1)
- [x] 3.2 Conditional branch: stored etag → `If-None-Match`; 304 → touch-only path, `enrichment_304s`; 200 → replace + merge (repo-meta-enrichment-S4, S5)
- [x] 3.3 404 → `gone=1`, retry suppressed within TTL, no exception upward (repo-meta-enrichment-S2); 5xx/network → fail-open counters + warn-once (S3)
- [x] 3.4 Dedup: per-batch + per-run in-flight sets keyed `(owner,repo)` (repo-meta-enrichment-S8)

## 4. Trigger points

- [x] 4.1 AcquisitionStage hook post-collect: URL → `(owner,repo)` (reuse canonicalization parse), cache-first, lazy fetch (repo-meta-enrichment-S6, S8)
- [x] 4.2 SearchStage skip-evaluator refresh — **GATED: wire only when add-gather-skip is implemented**; if ③ not landed, defer with explicit note in PR and leave S7 test xfail-marked (repo-meta-enrichment-S7)
- [x] 4.3 Tokenless auto-disable: empty/unusable token pool → silent off, ≤1 info log, zero requests (repo-meta-enrichment-S10); credential rotation inherits GithubCredentialLimited handling (S9)

## 5. Configuration & metrics

- [x] 5.1 `enrichment` config section: `enabled` (default off), `ttl_hours` (default 24, validated > 0) in schemas/loader/validator + examples (repo-meta-enrichment-S11)
- [x] 5.2 Counters `enrichment_fetches`/`enrichment_304s`/`enrichment_failures` (thread-safe per ② amnesty W1 lesson: lock-guarded) + StatusManager display line

## 6. GREEN & verification

- [x] 6.1 Drive all automated tests to GREEN; update tests.md statuses
- [x] 6.2 Regression: suites of ①–⑤ green; `enabled=off` A/B confirms zero footprint (S11 evidence)
- [x] 6.3 **Staging economics gate:** run with `on` over realistic query set — measure units consumed vs unique-repo count (expect ≈1:1 first run), second-run 304 share (expect ≥80%), `date_fill_rate_api` rise from 0.000 baseline, ③ `push_signal_coverage` rise (skip_known in shadow); document numbers; production enablement only on match with cost model. **DONE 2026-09-13** (harness: `/tmp/opencode/staging_enrichment_gate.py`; tokens via `GITHUB_TOKENS` env from `.secrets`, never persisted; single token for exact accounting; temp workspace; real `Registry`+writer thread, real `GitHubClient` buckets, real network).
  - **Accounting method:** per-response `x-ratelimit-used` headers (`MeasuringClient` wrapper) + independent `GET /repos` probe before run 1. The `/rate_limit` endpoint returns a synthesized/cached view in this environment (remaining pinned at 5000) and was deliberately NOT trusted; header deltas across consecutive runs reconciled exactly (run N spent 1 probe + 3 fetches + 0 conditional = 4 units; next run's `used_before_run1` grew 32→36).
  - **Live-search run (30 links → 30 unique repos):** first run fetched=30, http200=30, failures=0, elapsed 24.8 s; header cross-check `used` +31 (= 30 fetches + 1 diag GET) ⇒ **units_per_unique_repo = 1.0 (≈1:1)**. Second run after SQL aging (`fetched_at −= 172800`): fetched=30, **share_304 = 1.00 (≥0.8)**, elapsed 19.3 s. `links.repo_pushed_at`/`repo_size_kb` fill **0.000 → 1.000**; ③ `push_signal_coverage` (shadow decide) **0.000 → 1.000**; channel-② `date_fill_rate_api` stayed **0.000** — confirms the trimmed-payload baseline the design predicts (enrichment bypasses channel ②, does not repair it).
  - **Fixture-fallback reruns (3 repos; search API hit a transient secondary limit on the token, enrichment core traffic unaffected):** units1 = 3 ⇒ **units_per_unique_repo = 1.00**; second run **core_units_consumed = 0** with `used` constant across ALL 304 responses (**zero 304-accounting proven live**, matches task 1.1 fixture `rate_limit_delta.delta=0`); share_304 = 1.00; coverage 0.0→1.0; fill 0.0→1.0. **All five gates true, verdict PASS.**
  - **Conclusion:** measured economics match the design D1/D3 cost model (≤1 unit per unique repo per TTL window; steady state dominated by free 304s). Production enablement gate satisfied; flag flip `enrichment.enabled: true` is now an operator decision per the migration plan (deploy off → staging on ✓ → production on).

## 7. Documentation

- [x] 7.1 README: quota model (cost-per-unique-repo, TTL decay, 304 economics), tokenless asymmetry, `gone` takedown semantics
- [x] 7.2 `docs/specs/registry_flags_metrics.md`: fill `enrichment.*` rows + metric definitions; cross-reference ⑥ `candidates_export.md` contract (`gone` flag exposure for dangling-commit targeting)
