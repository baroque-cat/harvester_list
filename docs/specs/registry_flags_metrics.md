# Registry Feature Flags & Metrics Dictionary (Phase-0)

**Owning change:** `add-link-registry`
**Status:** `registry.enabled` is implemented here. All skip/early-stop/check
flags and their metrics are **defined now, emitted by later changes**. They are
documented here so later designs share one vocabulary and one trust model.

## Feature-flag matrix

Flags are additive and default to the safest (off) state. "shadow" means the
decision is computed and logged but not applied.

| Flag | Values | Default | Effect | Owner | Reads registry? |
|------|--------|---------|--------|-------|-----------------|
| `registry.enabled` | `true` / `false` | `false` | Record links, coverage and runs. No behavior change. | `add-link-registry` | No |
| `skip_known` | `off` / `shadow` / `on` | `off` | Skip acquisition for links already `gathered_ok` for the provider + `patterns_hash`. | `add-gather-skip` | Yes |
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
| `skipped_known` | Acquisition tasks not enqueued because the link was already `gathered_ok`. | `add-gather-skip` |
| `regathered_total` | Links re-gathered despite a prior `gathered_ok` row. | `add-gather-skip` |
| `regathered_expired` | Re-gathers triggered by a TTL/date rule. | `add-gather-skip` |
| `early_stop_pages_saved` | Search pages not fetched because of an early stop. | `add-search-early-stop` |
| `early_stop_triggered` | Searches where an early stop fired. | `add-search-early-stop` |
| `check_skipped_by_status` | Checks skipped, labelled by the conclusive status that justified the skip (`valid`, `invalid`, `no_quota`, `wait_check`). | `add-key-ledger` |
| `date_fill_rate_repo` | Fraction of links with a populated `repo_pushed_at`. | `add-date-extraction` |
| `date_fill_rate_file` | Fraction of links with a populated `file_commit_date`. | `add-date-extraction` |

## Storage mapping

- `novel_links_per_run` is derivable from `links.first_seen_ts` within a run
  window once `runs.started_at`/`runs.finished_at` are journaled (this change).
- `skipped_known` and the `regathered_*` counters rely on `link_coverage`
  (`url_hash`, `provider`, `patterns_hash`) plus `links.visit_status`.
- `check_skipped_by_status` relies on the `keys` table; this change only creates
  the table and migration-imports existing keys. `add-key-ledger` owns writes.
