# Candidate Export Contract

**Owning change:** `add-target-prioritization`
**Producer:** `tools/export_candidates.py`
**Status:** stable. `schema_version` `"1.0"`.

This document is the handoff contract between the harvester (producer) and any
downstream deep-scan consumer (for example the future clone + TruffleHog +
dangling-commits extension project). The export orders the harvested corpus by
expected deep-scan value and is deliberately small, deterministic and
versioned: **stability matters more than richness.**

## Purpose

After discovery, date extraction and key verification the registry holds enough
evidence to answer *"which repositories deserve deep scanning first?"*. The
export aggregates that evidence per repository, ordered by a configurable
priority score, and publishes it as newline-delimited JSON (default) or CSV.

## Command line

```
python -m tools.export_candidates --workspace PATH \
    [--format ndjson|csv] [--csv] [--min-priority N] [--limit N] \
    [--sample-links N] [--registry PATH]
```

| Option | Default | Meaning |
|--------|---------|---------|
| `--workspace` | *(required)* | Workspace directory; the registry is `<workspace>/registry.sqlite`. Required by design D4 — an export is a handoff artifact, so the target must be named explicitly. |
| `--registry` | *(auto)* | Explicit registry database path (advanced; wins over workspace). |
| `--format` | `ndjson` | Output format. |
| `--csv` | off | Shorthand for `--format csv` (the spec's CSV switch). |
| `--min-priority` | *(none)* | Drop records whose `priority` is below the cutoff. |
| `--limit` | *(none)* | Emit at most N records (after filtering and sorting). |
| `--sample-links` | `5` | Maximum sampled link URLs per record (`0` disables samples). |

The tool opens the registry with SQLite's `mode=ro` URI, performs no writes and
no network access, and streams output to stdout.

Library use:

```python
from tools.export_candidates import export_candidates

export_candidates("./data", fmt="ndjson", min_priority=50, limit=100, out=stream)
```

## Record fields (`schema_version` `"1.0"`)

The field set is **frozen by the spec scenario test**. Additions, removals or
semantic changes require a `schema_version` bump.

| Field | Type | Meaning |
|-------|------|---------|
| `schema_version` | string | Contract version, currently `"1.0"`. |
| `owner` | string | GitHub owner (case preserved). |
| `repo` | string | GitHub repository (case preserved). |
| `priority` | number | Denormalized repository score (`links.priority`), see below. |
| `best_status` | string | Highest-ranked observed key status: `valid` > `wait_check` > `no_quota` > `invalid` > `none`. |
| `status_counts` | object | Counts of attributed ledger keys by status (`{}` when none). |
| `repo_pushed_at` | number \| null | Epoch seconds of the newest known push time; `null` when unknown. |
| `repo_size_kb` | number \| null | Largest known repository size in KB; `null` when unknown. |
| `links_total` | integer | Number of `links` rows attributed to the repository. |
| `sample_links` | array[string] | Up to `--sample-links` canonical link URLs, deterministic (`url_hash` order). |
| `gone` | *(not present)* | Takedown info is not part of v1; consumers may read `repos.gone` directly (advanced). |

### Ordering and determinism

Records are sorted by:

1. `priority` descending;
2. `repo_pushed_at` descending, with `null` last;
3. `owner` ascending, then `repo` ascending.

Given the same registry state and flags, the output is byte-stable. Within a
record, `sample_links` is ordered by `url_hash`; `status_counts` keys are
serialized sorted in CSV.

### CSV variant

The CSV header is exactly `schema_version,owner,repo,priority,best_status,
status_counts,repo_pushed_at,repo_size_kb,links_total,sample_links`.
`status_counts` and `sample_links` are compact JSON strings; `null`
`repo_pushed_at`/`repo_size_kb` are empty cells. Row order and values are
equivalent to NDJSON.

## Priority formula (`schema_version` `"1.0"` semantics)

```
priority = W1*valid_present + W2*soft_present
           + W4*exp(-ln2 * age_days / half_life_days)
           - W5*clamp((size_kb - threshold_kb) / (ramp_kb - threshold_kb), 0, 1)
```

| Symbol | Attribute | Default | Meaning |
|--------|-----------|---------|---------|
| W1 | `prioritization.w1` | 100 | A key with status `valid` is attributed to the repo. |
| W2 | `prioritization.w2` | 40 | A key with status `wait_check`/`no_quota` is attributed. |
| W4 | `prioritization.w4` | 30 | Peak freshness weight. |
| H | `prioritization.half_life_days` | 30 | Freshness half-life, days. |
| W5 | `prioritization.w5` | 20 | Maximum size penalty. |
| T | `prioritization.threshold_kb` | 50000 | Size (KB) below which no penalty applies (~50 MB). |
| R | `prioritization.ramp_kb` | 500000 | Size (KB) at/above which the penalty is capped (~500 MB). |

Missing inputs degrade neutrally: `null` dates/sizes and absent keys contribute
zero, never an error, and the score for a completely sparse repository is `0`.

### Score convergence

Scores are denormalized into **every** `links.priority` row of the repository
and kept converged with the ledger:

- **Event-driven.** Key-status upserts, date/size merges and repo-wide metadata
  merges mark the owning repository dirty; the next writer batch recomputes and
  persists its score (within one batch interval).
- **Run-finish sweep.** When a run finishes, every `(owner, repo)` is recomputed,
  healing any mutation that bypassed dirty-marking (migration imports, manual
  edits). Scores never lag the ledger by more than one run.

Outside a run the `links.priority` column simply goes stale; the export reads
the stored value. Run the pipeline once (or a sweep) before a fresh export.

### Key → repo attribution

Attribution is `keys.source_url_hash → links.url_hash → (owner, repo)`.
**Documented limitation (design D2):** legacy keys imported without a source URL
hash contribute only to global statistics, never to a repository score. Such
records therefore show a lower `status_counts` than the workspace-wide ledger
would suggest; `links_total` lets consumers sanity-check coverage.

## Consumer guidance

- **Re-rank offline.** All inputs except `best_status`/`status_counts` are raw
  numbers. A consumer can re-score with its own weights/policy without touching
  the database — the export is a snapshot, not a scheduler.
- **Treat unknown fields as forward-compatible.** Ignore fields you do not
  recognize; only a `schema_version` bump signals breaking change.
- **`best_status = "none"`** means no ledger key was attributable to the repo —
  not that the repo contains no secrets. Priority still reflects freshness/size.
- **Advanced: direct SQLite.** A consumer that needs the full link set can open
  `<workspace>/registry.sqlite` read-only and join on `links.owner/repo`
  (`idx_links_repo`). This bypasses the contract's stability guarantee; prefer
  the export unless the sampled URLs are insufficient. Example:

  ```sql
  SELECT url, owner, repo, priority, repo_pushed_at, repo_size_kb
  FROM links
  WHERE owner = ? AND repo = ?
  ORDER BY url_hash;
  ```

## Versioning policy

1. `schema_version` is a string `"MAJOR.MINOR"`.
2. **Adding** a field: bump MINOR; existing consumers keep working (unknown
   fields ignored).
3. **Removing or changing** a field, its type or its meaning, or changing the
   ordering: bump MAJOR and document the migration.
4. `priority` semantics are pinned to the defaults above; changing the default
   weights is a MAJOR change to the field's meaning even though the record shape
   is unchanged.
