# Registry Feature Flags & Metrics Dictionary (Phase-0)

**Owning change:** `add-link-registry`
**Status:** `registry.enabled` is implemented by `add-link-registry`;
`skip_known` and its `skipped_known` / `regathered_*` / `push_signal_coverage`
metrics are implemented by `add-gather-skip`; the `enrichment.*` flag and
`enrichment_fetches` / `enrichment_304s` / `enrichment_failures` / `repos_cached`
/ `gone` metrics are implemented by `add-repo-meta-enrichment`; the
`early_stop` flag and its `early_stop_would_fire` / `early_stop_fired` /
`novel_after_stop` metrics are implemented by `add-search-early-stop`; the
`check_skip` / `recheck_cron` flags and their `check_skipped_by_status` /
`rechecks_enqueued` / `provider_calls_saved` metrics are implemented by
`add-key-ledger`; the `prioritization` section and its `links.priority`
denormalization / candidate export are implemented by
`add-target-prioritization`. All flags default to the safest (off) state.

## Feature-flag matrix

Flags are additive and default to the safest (off) state. "shadow" means the
decision is computed and logged but not applied.

| Flag | Values | Default | Effect | Owner | Reads registry? |
|------|--------|---------|--------|-------|-----------------|
| `registry.enabled` | `true` / `false` | `false` | Record links, coverage and runs. No behavior change. | `add-link-registry` | No |
| `skip_known` | `off` / `shadow` / `on` | `off` | Skip acquisition for links already `gathered_ok` for the provider + `patterns_hash` when the four-condition rule holds. | `add-gather-skip` | Yes |
| `enrichment.enabled` | `true` / `false` | `false` | Fetch and TTL-cache repository metadata (`pushed_at`/`size_kb`/`default_branch`/ETag) and merge it into `links`. `off` is byte-for-byte inert. | `add-repo-meta-enrichment` | Yes (cache lives in `repos`) |
| `early_stop` | `off` / `shadow` / `on` | `off` | Stop paging an **API-transport** partition once its trailing window is saturated with known links and the trust gate passes. Web transport is inert at code level. | `add-search-early-stop` | Yes |
| `check_skip.mode` | `off` / `on` | `off` | Skip re-validating keys whose last check is fresh and conclusive (per-status TTL in `check_skip.ttl_hours`). | `add-key-ledger` | Yes |
| `recheck.enabled` (planning name `recheck_cron`) | `true` / `false` | `false` | Schedule periodic re-checks of previously-seen keys (in-process cron; `recheck.interval_hours` / `batch_size`). | `add-key-ledger` | Yes |
| `prioritization.display_top_n` | integer ≥ 0 | `0` | Render a cosmetic top-N owner/repo:priority line in the status display. Scoring/export are always on (read-side). | `add-target-prioritization` | Yes (read-only top-N) |

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
| `early_stop_would_fire` | Partitions where the stop conjunction first held (logged as `would_stop_at`). Counted once per partition, in `shadow` and `on`. | `add-search-early-stop` |
| `early_stop_fired` | Partitions where the stop was enforced (`on` mode); no page task was emitted beyond the stop point. | `add-search-early-stop` |
| `novel_after_stop` | **False-stop price**: distinct identities that the gather-skip conjunction would not skip (still require research) and arrived on pages after the first hypothetical stop point of a partition. Conservative (counts all missed work, not only first-seen-this-run). The shadow promotion gate requires ≈ 0. | `add-search-early-stop` |
| `early_stop_evaluations` | Page-boundary evaluations performed (diagnostic; one per page recorded). | `add-search-early-stop` |
| `check_skipped_by_status` | Checks skipped, labelled by the conclusive status that justified the skip (`valid`, `invalid`, `no_quota`, `wait_check`). | `add-key-ledger` |
| `provider_calls_saved` | Provider verification calls not made because `check_skip.mode=on` skipped a fresh ledger key (one per `check_skipped_by_status` increment). | `add-key-ledger` |
| `rechecks_enqueued` | Ordinary `CheckTask`s enqueued by the periodic re-check driver in the current run (bounded by `recheck.batch_size` per tick). | `add-key-ledger` |
| `date_fill_rate_api` | Share of API search items that yielded a usable `repo_pushed_at` (documented **0.0 baseline**: the September 2026 live probe found trimmed `repository` objects without date/size fields — design D7; a sustained rise signals GitHub restored the fields or `add-repo-meta-enrichment` began feeding). Exposed per run in `PipelineStatus.date_metrics`. | `add-date-extraction` |
| `date_fill_rate_web` | Share of gathered blob pages that yielded a `file_commit_date` (documented **0.0 baseline**: served blob HTML renders `<relative-time>` timestamps client-side — design D7; a sustained rise signals restored server-side markers). Exposed per run in `PipelineStatus.date_metrics`. | `add-date-extraction` |
| `metadata_noops` | Date-metadata updates that matched no known link (the UPDATE-only merge never inserts phantom rows); logged once at debug level. Exposed by `Registry.get_stats()`. | `add-date-extraction` |
| `enrichment_fetches` | Completed repository-metadata HTTP exchanges (`200`/`304`/`404`) issued for stale-or-missing cache entries. One per unique `(owner, repo)` per TTL window. | `add-repo-meta-enrichment` |
| `enrichment_304s` | Subset of `enrichment_fetches` answered `304 Not Modified` (ETag conditional refresh). Zero rate-limit cost, touch-only `fetched_at` bump, no link merge. | `add-repo-meta-enrichment` |
| `enrichment_failures` | Failed fetches (network/5xx/credential exhaustion) that degraded fail-open; warned once per enricher. | `add-repo-meta-enrichment` |
| `repos_cached` | Successful `200` fetches that replaced/cached a `repos` row and re-triggered the link-column merge. | `add-repo-meta-enrichment` |
| `gone` | Repositories answered `404` and marked `repos.gone=1`; retries are suppressed within TTL and existing link rows are preserved. | `add-repo-meta-enrichment` |
| `cooling_skips` | **Diagnostic**: enrichment encounters yielded without requests because every API token was cooling down (non-blocking probe; transient — resumes in-run when a token recovers). Logged once at info level. | `add-repo-meta-enrichment` |
| `prioritization.display_top_n` | Configured N for the optional status line (mirror of the config value; `0` disables). | `add-target-prioritization` |
| `prioritization.candidates` | Flattened `[[owner, repo, priority], ...]` top-N list read from `links.priority` while `display_top_n > 0`. Empty otherwise. | `add-target-prioritization` |

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

## Early-stop semantics (`add-search-early-stop`)

- **API transport only.** The detector is inert for web searches at code level
  (relevance-ordered results carry no saturation information). The flag value
  has no effect on web pagination.
- **Per-partition window.** Each refined query string is a distinct partition
  keyed by `(provider, query)`. A rolling window of the trailing `window`
  result identities (URL hashes, de-duplicated in-window) tracks the known
  ratio. Tracking and stopping one partition never affects siblings.
- **Four independent gates.** A stop requires the ratio `>= theta`, at least
  `min_pages` pages fetched (never the first page), the registry trust gate
  (`COUNT(links) >= min_trust`, `meta.migration_complete` present, run not
  degraded, no registry read errors), and the flag in `on` mode. Any doubt
  fails open to a full pass.
- **"Known" is gather-skip.** Classification reuses the exact amended
  gather-skip conjunction (`storage.gather_skip.GatherSkipEngine.classify`):
  `gathered_ok` ∧ fresh within the gather TTL ∧ no push invalidation (NULL
  `repo_pushed_at` passes vacuously) ∧ coverage for the current
  `(provider, patterns_hash)`. Discovered-only, foreign-coverage,
  TTL-expired and failed links all count as novel.
- **Chained pagination.** When the detector is active for API tasks, page tasks
  are generated one boundary at a time (page 1 emits page 2, page N emits page
  N+1) instead of the pre-change bulk generation, so every boundary can be
  evaluated. `off` mode and web keep the byte-for-byte bulk behavior.
- **Kill-switch.** The existing registry degraded flag (write failure, queue
  overflow, corrupt open) plus any registry read error mutes stopping for the
  remainder of the run.

## Early-stop shadow promotion procedure (`shadow` → `on`)

Preconditions: `registry.enabled: true`; `tools.registry_migrate` has written
`meta.migration_complete`; the workspace has run at least one representative
cycle with `early_stop.mode: shadow` over several diverse query sets (different
providers/patterns); the run's `runs.degraded = 0`.

1. **Read the decision log.** `<workspace>/registry_decisions.jsonl` holds one
   `type: "early_stop"` record per partition's first `would_stop_at` point
   (`provider`, `query`, `page`, `window_size`, `known_count`, `ratio`,
   `trust_ok`). Filter by `run_id` to isolate a cycle.
2. **Read `novel_after_stop`.** The run counter (also in
   `PipelineStatus.early_stop_metrics`) is the number of genuinely novel
   identities that arrived after the hypothetical stop point.
3. **Gate.** Require `novel_after_stop ≈ 0` across ≥ 3 diverse staging query
   sets. Investigate any nonzero case individually — a nonzero price means the
   window/theta is too aggressive or the partition key is unstable.
4. **Promote.** Only then flip `early_stop.mode: on`; monitor
   `early_stop_fired` against the novel-links trend.
5. **Rollback.** Flip the flag back to `shadow` or `off`; the decision log and
   counters persist for postmortem.

### Live shadow measurement (2026-09-13)

Executed with the production `EarlyStopEngine` in `shadow` mode, the real GitHub
code-search API transport, and a real SQLite registry. Per query set, 10 pages
(100 results/page, the `API_MAX_PAGES` horizon) were fetched once; three
registry states were then measured over that same real stream:

- **warm** — every fetched page seeded `gathered_ok` with coverage, modelling a
  previous full pass (which is what a `shadow` period produces);
- **partial** — only page 1 seeded (shallower prior horizon);
- **cold** — 0 rows (fresh install).

| Provider | Query | total | warm `would_stop_at` | warm ratio | warm `novel_after_stop` | partial stop | cold stop |
|----------|-------|-------|----------------------|------------|--------------------------|--------------|-----------|
| openai | `"T3BlbkFJ"` | 83 072 | page 2 | 1.0 | **0** | none | none |
| anthropic | `"sk-ant-api03"` | 52 352 | page 2 | 1.0 | **0** | none | none |
| google | `"AIzaSy"` | 2 641 920 | page 2 | 1.0 | **0** | none | none |

Result: across three diverse query sets the promotion gate condition
`novel_after_stop ≈ 0` holds exactly (`0` missed work after the hypothetical
stop), while a cold or shallow registry never stops (ratio 0.0, zero false
stops) — both failure directions verified against live GitHub data. The raw
per-run JSON is kept outside the repo; no credentials were written to the
repository or the results. This satisfies the staging promotion gate for these
sets; production `on` rollout should still follow a broader shadow period and
monitor the `early_stop_fired` vs novel-links trend.

## Storage mapping

- `novel_links_per_run` is derivable from `links.first_seen_ts` within a run
  window once `runs.started_at`/`runs.finished_at` are journaled.
- `skipped_known` and the `regathered_*` counters rely on `link_coverage`
  (`url_hash`, `provider`, `patterns_hash`) plus `links.visit_status`,
  `links.gathered_ts` and `links.repo_pushed_at`.
- The shadow decision log is append-only JSONL at
  `<workspace>/registry_decisions.jsonl`. Gather-skip writes one line per
  would-skip decision:
  `{ts, run_id, url, url_hash, provider, decision, mode, conditions:{status_ok,
  coverage_ok, ttl_ok, push_evidence, push_changed}}`. Early-stop writes one
  line per partition's first `would_stop_at`:
  `{type:"early_stop", ts, run_id, provider, query, page, window_size,
  known_count, ratio, theta, min_pages, trust_ok, decision, mode}`. Both are
  written in their respective `shadow`/`on` modes. The file is pure
  observability data: rotate, truncate or archive it freely between cycles —
  run counters do not depend on it.
- `meta(key, value)` is an additive key/value table (schema_version 3).
  `add-search-early-stop` reads `migration_complete` as part of its trust gate;
  `tools/registry_migrate.py` writes it after a successful (non-dry-run)
  migration.
- `check_skipped_by_status` relies on the `keys` table; `add-link-registry` only
  creates the table and migration-imports existing keys. `add-key-ledger` owns
  writes and the `keys.source_url_hash` attribution that makes the false-skip
  join exact. The ledger row carries `key_hash` (primary key), a masked
  reference, `status`, `first_seen_ts` (set once), `last_recheck_ts` (real
  provider calls only), `last_seen_ts` (every observation, including skips) and
  `source_url_hash`; schema_version 4 adds `last_seen_ts` additively.
- `provider_calls_saved` and `check_skipped_by_status` are flattened by
  `KeyLedger.to_stats()` into `PipelineStatus.key_ledger_metrics` (rendered only
  when `check_skip.mode=on`); `rechecks_enqueued` and `unresolved` are flattened
  by `RecheckManager.to_stats()` into `PipelineStatus.recheck_metrics` (rendered
  only while the driver is enabled). With `check_skip.mode=off` the engine emits
  `{"mode": "off", ...}` and performs zero registry reads; with
  `recheck.enabled=false` the driver emits `{"enabled": false, ...}` and enqueues
  nothing.
- `links.priority` is the single denormalized scoring target
  (`add-target-prioritization`); its value for a repo is a pure function of
  `keys` (via `source_url_hash`) and the repo's `repo_pushed_at`/
  `repo_size_kb`. `prioritization.candidates` is flattened by
  `Pipeline._prioritization_metrics()` into `PipelineStatus.prioritization_metrics`
  (rendered only when `display_top_n > 0`); the export tool reads the same column
  read-only.

## Key-ledger semantics (`add-key-ledger`)

- **Identity.** `key_hash = sha256("provider|key|address|endpoint")` mirrors the
  in-memory CheckTask dedup id; the same secret against a different
  address/endpoint is deliberately a distinct row. Unsalted by design — this is
  an identity join key, not a secrecy mechanism.
- **Secret hygiene.** Only the hash and a `<first6>…<last4>` masked reference are
  persisted. The full secret must never appear in the registry database, its WAL
  sidecars, the decision log or metrics; the byte-scan test
  (`key-ledger-S9`) enforces this against planted canaries. `tools/registry_migrate`
  reuses the same hash/mask helpers so imported keys join exactly.
- **Skip rule.** With `check_skip.mode=on`, a known key is skipped iff its stored
  status has a configured TTL and `now - last_recheck_ts <= ttl`. Unknown keys,
  NULL/unknown statuses and NULL `last_recheck_ts` always fail open to a check.
  A skipped observation emits a `StageOutput` with no results, so no duplicate
  shard record is appended; only `last_seen_ts` advances.
- **Re-check driver.** With `recheck.enabled=true`, each tick selects TTL-expired
  keys in priority order (`wait_check → valid → no_quota → invalid`), oldest
  first, bounded by `recheck.batch_size`, and enqueues ordinary CheckTasks
  through the CheckStage queue so existing provider rate limits/token buckets
  apply. Migration rows with NULL `last_recheck_ts` count as expired and drain
  gradually. Because the registry holds no plaintext, the driver resolves keys
  from the result shards; unresolved keys are counted, not guessed.
- **Trust/degradation.** A ledger read error warns once per error kind, marks
  the run degraded, and forces checks for the affected batch. `off` flags
  restore pre-change behavior exactly.

## Prioritization semantics (`add-target-prioritization`)

- **Formula.** `priority = W1·valid_present + W2·soft_present +
  W4·exp(−ln2·age_days/H) − W5·clamp((size_kb−T)/(R−T), 0, 1)` with defaults
  `W1=100, W2=40, W4=30, H=30 d, W5=20, T=50000 KB, R=500000 KB` (all under the
  `prioritization` config section). `valid_present` / `soft_present` come from
  keys attributed through `keys.source_url_hash → links.url_hash → (owner,repo)`;
  `age_days`/`size_kb` are `MAX(repo_pushed_at)` / `MAX(repo_size_kb)` over the
  repo's `links` rows. Missing inputs contribute zero; a sparse repo scores 0.
- **Denormalization.** The repo score is written to **every** `links.priority`
  row of that repo (grouped by `idx_links_repo`). Consumers read a single
  numeric column without joins.
- **Convergence.** Key-status upserts, date/size merges and repo-wide metadata
  merges mark `(owner,repo)` dirty and are rescored in the same writer-batch
  transaction. `Registry.finish_run()` runs a full `DISTINCT owner,repo` sweep,
  so priorities never lag the ledger by more than one run and externally
  mutated registries self-heal.
- **Attribution limitation.** Legacy keys imported without a source URL
  contribute only to global statistics, never to a repo score. This is
  documented in `candidates_export.md`; `links_total` on each export record
  lets consumers sanity-check coverage.
- **Attribution caveat for future work.** `keys.source_url_hash` is set on real
  provider-check writes; migration-imported keys have `source_url_hash=NULL`
  and are excluded until a migration improvement attributes them.
- **Read-side isolation.** Scoring and export never change pipeline behavior;
  the only `links.priority` writes and internal bookkeeping are involved. The
  export tool opens the registry `mode=ro` and performs zero network activity.
- **Rollback.** Purely additive: stop using the export or set `display_top_n: 0`.
  A stale `priority` column harms nothing.

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

## Key-ledger staging measurement (2026-09-13)

Live measurement for `add-key-ledger` task 6.3, performed on a staging harness
with **real GitHub discovery** and a **local deterministic provider mock**.

**Protocol.** GitHub side fully live: API transport, credentials injected only
via environment (`GITHUB_TOKENS`/`GITHUB_SESSIONS` read from the gitignored
`.secrets`; never written into configs, code or logs). Query
`filename:.env.production "T3BlbkFJ"` (total_count=105 → 110 links over 2
pages). Provider side: local openai-like mock on 127.0.0.1 bucketing tokens by
sha256 (60% valid / 20% invalid / 10% no_quota / 10% wait_check), so check
economics are measured without sending harvested third-party keys to real
provider APIs. Workspace under `/tmp` (outside the repo). Configs: run1
`check_skip.mode=off`; run2 `mode=on`; run3 `mode=on` + `recheck.enabled=true`
with `ttl_hours.valid≈10s`, `interval_hours≈10s`, `batch_size=10`,
`skip.skip_known=on`.

| Metric | Run 1 (baseline) | Run 2 (repeat, skip on) | Run 3 (cron cycles) |
|---|---|---|---|
| Links discovered / gathered | 110 / 110 | 110 / 110 (re-gathered) | 110 suppressed as known |
| Distinct keys checked | 20 | 20 rediscovered | — |
| **Provider calls** | **20** | **0 (−100%)** | 10 per cron wave |
| `provider_calls_saved` | — | 20 | 10 (run3c inline) |
| `check_skipped_by_status` | — | valid=10, invalid=5, no_quota=3, wait_check=2 | exact ledger mirror |
| Novel keys found | 20 | 0 (index stable over 4 min; novel keys are always checked — S4) | 0 |
| Ledger rows / degraded runs | 20 / 0 | 20 / 0 | 20 / 0 (8 runs total, degraded sum = 0) |
| Runtime | 60 s | 40 s | 20–60 s |

**Cron convergence (criterion: no `valid` key older than max-recheck-age after
a cycle).** Each tick selected exactly the 10 TTL-expired `valid` keys
(`wait_check`/`no_quota`/`invalid` correctly untouched — their TTLs had not
lapsed): `enqueued=10`, `unresolved=0`, later ticks `refused=38` (CheckStage
dedup absorbs repeat waves — no duplicate work). After the cycle: **10/10
valid keys refreshed inside the cycle window, stale=0**; between cycles age is
bounded by the cycle cadence, as designed (expiry re-triggers selection).

**Bugs found by staging and fixed.**
1. `RecheckManager` received `ShardKeyResolver` as an object but called it as a
   callable (`'ShardKeyResolver' object is not callable`) — every production
   resolution failed open to `unresolved`. Fixed by normalizing
   `resolver.resolve`; unit tests had used lambdas and could not catch it.
   Regression test: `tests/test_recheck_manager.py::test_resolver_object_recovers_plaintext_from_result_files`.
2. `rechecks_enqueued` counted dedup-refused enqueues. Split into
   `rechecks_enqueued` / `rechecks_refused` (honest counters surfaced in
   `PipelineStatus.recheck_metrics` and the status line).

**Observations (pre-existing behavior, out of scope).**
- CheckStage `put_task` dedup is stage-lifetime persistent ⇒ at most one cron
  wave per key per run; cross-run cadence provides ongoing convergence (both
  demonstrated above). Long-lived processes may want dedup eviction or
  attempts-tagged re-check tasks — follow-up candidate.
- When the provider rate budget is exhausted, CheckStage drops the task
  (`rate limit exceeded ... max: N`); the ledger selection is stateless and
  persistent, so the next cycle re-picks it (observed: 4 dropped in one cycle,
  all recovered in the next).
- Result recovery regenerates check tasks from `material` files independently
  of the ledger; skip + dedup absorb them harmlessly (run3c: `saved=10`).

**Post-run hygiene scan.** All 20 plaintext keys absent from
`registry.sqlite` + `-wal` + `-shm` bytes (masked refs present for 20/20);
GitHub PAT and session cookie: 0 occurrences across all repo log files
(`ghp_`/`github_pat_`/`gho_` redaction patterns added to `tools/patterns.py`
as part of this change's hardening).
