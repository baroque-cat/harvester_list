# Registry Feature Flags & Metrics Dictionary (Phase-0)

**Owning change:** `add-link-registry`
**Status:** `registry.enabled` is implemented by `add-link-registry`;
`skip_known` and its `skipped_known` / `regathered_*` / `push_signal_coverage`
metrics are implemented by `add-gather-skip`; the `enrichment.*` flag and
`enrichment_fetches` / `enrichment_304s` / `enrichment_failures` / `repos_cached`
/ `gone` metrics are implemented by `add-repo-meta-enrichment`. The remaining
early-stop/check flags and their metrics are **defined now, emitted by later
changes**, so later designs share one vocabulary and one trust model.

## Feature-flag matrix

Flags are additive and default to the safest (off) state. "shadow" means the
decision is computed and logged but not applied.

| Flag | Values | Default | Effect | Owner | Reads registry? |
|------|--------|---------|--------|-------|-----------------|
| `registry.enabled` | `true` / `false` | `false` | Record links, coverage and runs. No behavior change. | `add-link-registry` | No |
| `skip_known` | `off` / `shadow` / `on` | `off` | Skip acquisition for links already `gathered_ok` for the provider + `patterns_hash` when the four-condition rule holds. | `add-gather-skip` | Yes |
| `enrichment.enabled` | `true` / `false` | `false` | Fetch and TTL-cache repository metadata (`pushed_at`/`size_kb`/`default_branch`/ETag) and merge it into `links`. `off` is byte-for-byte inert. | `add-repo-meta-enrichment` | Yes (cache lives in `repos`) |
| `early_stop` | `off` / `shadow` / `on` | `off` | Stop paging a search once enough known links dominate the page. | `add-search-early-stop` | Yes |
| `check_skip` | `off` / `on` | `off` | Skip re-validating keys whose last check is fresh and conclusive. | `add-key-ledger` | Yes |
| `recheck_cron` | `off` / `on` | `off` | Schedule periodic re-checks of previously-seen keys. | `add-key-ledger` | Yes |

Trust gate: a run whose registry was **degraded** (write failure, queue overflow,
corrupt open) must not be trusted as a source of "known" counts. Skip/early-stop
flags therefore consult `runs.degraded` for the run that produced the data.

## Metrics dictionary

Metric names are stable; their emitting change is annotated. All are counters
per run unless noted.

| Metric | Meaning | Emitted by |
|--------|---------|------------|
| `registry_dropped` | Operations dropped because the bounded writer queue was full (also marks the run degraded). Logged at `Registry.stop()` and exposed by `Registry.get_stats()`. | `add-link-registry` |
| `novel_links_per_run` | Links observed in a run with no prior `links` row. | `add-link-registry` (foundation) / `add-gather-skip` (reporting) |
| `skipped_known` | Acquisition tasks not enqueued because the link was already `gathered_ok` for the current provider/patterns and no condition failed. | `add-gather-skip` |
| `regathered_changed` | Re-gathers attributed to condition 3: non-NULL `repo_pushed_at` later than `gathered_ts` + 60 s grace. | `add-gather-skip` |
| `regathered_coverage_gap` | Re-gathers attributed to condition 4: no `link_coverage` row for the current `(provider, patterns_hash)`. | `add-gather-skip` |
| `regathered_ttl_expired` | Re-gathers attributed to condition 2: `gathered_ts` older than `skip.gather_ttl_hours`. | `add-gather-skip` |
| `regathered_failed_retry` | Re-gathers attributed to condition 1: last visit status was `failed` (never skipped). | `add-gather-skip` |
| `push_signal_coverage` | **Rate** (not a counter): share of evaluated already-known links whose skip decision carried non-NULL `repo_pushed_at` evidence. 0.0 is the documented pre-enrichment baseline. | `add-gather-skip` |
| `gathered_ts_missing` | **Diagnostic**: `gathered_ok` rows seen without `gathered_ts` (external mutation/corruption — the writer always stamps it). Fail-open applies; warned once per process. | `add-gather-skip` |
| `early_stop_pages_saved` | Search pages not fetched because of an early stop. | `add-search-early-stop` |
| `early_stop_triggered` | Searches where an early stop fired. | `add-search-early-stop` |
| `check_skipped_by_status` | Checks skipped, labelled by the conclusive status that justified the skip (`valid`, `invalid`, `no_quota`, `wait_check`). | `add-key-ledger` |
| `date_fill_rate_api` | Share of API search items that yielded a usable `repo_pushed_at` (documented **0.0 baseline**: the September 2026 live probe found trimmed `repository` objects without date/size fields — design D7; a sustained rise signals GitHub restored the fields or `add-repo-meta-enrichment` began feeding). Exposed per run in `PipelineStatus.date_metrics`. | `add-date-extraction` |
| `date_fill_rate_web` | Share of gathered blob pages that yielded a `file_commit_date` (documented **0.0 baseline**: served blob HTML renders `<relative-time>` timestamps client-side — design D7; a sustained rise signals restored server-side markers). Exposed per run in `PipelineStatus.date_metrics`. | `add-date-extraction` |
| `metadata_noops` | Date-metadata updates that matched no known link (the UPDATE-only merge never inserts phantom rows); logged once at debug level. Exposed by `Registry.get_stats()`. | `add-date-extraction` |
| `enrichment_fetches` | Completed repository-metadata HTTP exchanges (`200`/`304`/`404`) issued for stale-or-missing cache entries. One per unique `(owner, repo)` per TTL window. | `add-repo-meta-enrichment` |
| `enrichment_304s` | Subset of `enrichment_fetches` answered `304 Not Modified` (ETag conditional refresh). Zero rate-limit cost, touch-only `fetched_at` bump, no link merge. | `add-repo-meta-enrichment` |
| `enrichment_failures` | Failed fetches (network/5xx/credential exhaustion) that degraded fail-open; warned once per enricher. | `add-repo-meta-enrichment` |
| `repos_cached` | Successful `200` fetches that replaced/cached a `repos` row and re-triggered the link-column merge. | `add-repo-meta-enrichment` |
| `gone` | Repositories answered `404` and marked `repos.gone=1`; retries are suppressed within TTL and existing link rows are preserved. | `add-repo-meta-enrichment` |
| `cooling_skips` | **Diagnostic**: enrichment encounters yielded without requests because every API token was cooling down (non-blocking probe; transient — resumes in-run when a token recovers). Logged once at info level. | `add-repo-meta-enrichment` |

`skip_known`, the four `regathered_*` counters and `push_signal_coverage` are
flattened by `GatherSkipEngine.to_stats()` into `PipelineStatus.skip_metrics`
and rendered on the status display only when the mode is `shadow` or `on`.

`enrichment.*` counters are flattened by `RepoMetaEnricher.to_stats()` into
`PipelineStatus.enrichment_metrics` and rendered only when the flag is enabled.
With `enrichment.enabled=false` the enricher emits no metrics (`{"enabled":
false}`) and performs zero requests or `repos` writes.

## Enrichment semantics (`add-repo-meta-enrichment`)

- **Cache-first / TTL.** The `repos` table is both cache and work ledger. A row
  fresh within `enrichment.ttl_hours` is served with zero requests; only
  stale-or-missing `(owner, repo)` pairs are fetched. Duplicate encounters
  within a batch/run coalesce to one fetch.
- **Conditional economics.** A stale refresh sends `If-None-Match: <stored
  etag>`; a `304` bumps `fetched_at` only and enqueues no link merge. A `200`
  replaces all cached values and merges `pushed_at`/`size_kb` into every
  `links` row of that repository through the UPDATE-only COALESCE channel.
- **Takedowns (`gone`).** A `404` sets `repos.gone=1`, suppresses retries within
  TTL, and preserves existing link rows. The `gone` flag is the forward
  contract for candidate export (change ⑥ `candidates_export.md`): dangling
  commits in taken-down repositories become explicitly targetable rather than
  silently lost.
- **Tokenless asymmetry.** Without a usable `github_api` token the enricher
  silently self-disables (zero requests, ≤1 info log, `disabled_tokenless`
  surfaced); web-only deployments keep working exactly as before.

## Storage mapping

- `novel_links_per_run` is derivable from `links.first_seen_ts` within a run
  window once `runs.started_at`/`runs.finished_at` are journaled.
- `skipped_known` and the `regathered_*` counters rely on `link_coverage`
  (`url_hash`, `provider`, `patterns_hash`) plus `links.visit_status`,
  `links.gathered_ts` and `links.repo_pushed_at`.
- The shadow decision log is append-only JSONL at
  `<workspace>/registry_decisions.jsonl`, one line per would-skip decision:
  `{ts, run_id, url, url_hash, provider, decision, mode, conditions:{status_ok,
  coverage_ok, ttl_ok, push_evidence, push_changed}}`, written in `shadow` and
  `on` modes. The file is pure observability data: rotate, truncate or archive
  it freely between cycles — run counters do not depend on it.
- `check_skipped_by_status` relies on the `keys` table; `add-link-registry` only
  creates the table and migration-imports existing keys. `add-key-ledger` owns
  writes and the `keys.source_url_hash` attribution that makes the false-skip
  join exact.

## Gather-skip shadow promotion procedure (`shadow` → `on`)

Preconditions: `registry.enabled: true`; the workspace has run at least one
representative full cycle with `skip.skip_known: shadow`; the run's
`runs.degraded = 0`.

1. **Read the decision log.** `<workspace>/registry_decisions.jsonl` holds one
   JSON object per would-skip decision for the run(s). Take the `url_hash` set
   (filter by `run_id` when isolating a cycle).
2. **Join against outcomes.** `material`/`valid` shard rows do not yet carry a
   source URL (that arrives with `add-key-ledger`'s `keys.source_url_hash`), so
   the conservative comparison is an A/B over the same workspace: snapshot it,
   re-run the identical search with `skip.skip_known: off`, and diff the
   `material`/`valid` key sets.
3. **Compute the false-skip price.**
   `false_skip_price = (# keys found only in the off run at would-skip URLs) /
   (# would-skip URLs)`.
4. **Gate.** Require `false_skip_price ≈ 0` across the staging period (and
   inspect any nonzero cases individually — a nonzero price means condition
   attribution or the identity canon needs fixing before enforcement). Repeat
   the gate on a larger representative cycle before production rollout — while
   `push_signal_coverage` ≈ 0 the measured price reflects the TTL-only regime
   only. Only then flip `skip.skip_known: on`.
5. **Rollback.** Flip back to `off`; the decision log and counters persist for
   postmortem. Note that while `push_signal_coverage` is near 0.0, the price
   measured is precisely the TTL-only regime's cost.
