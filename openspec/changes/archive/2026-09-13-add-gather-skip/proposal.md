# Proposal: add-gather-skip

## Why

Gather is the economic core of the pipeline: every search hit costs an HTTP GET of the blob page plus the whole downstream check cascade. Today every restart re-fetches every link, because dedup lives only in bounded in-memory sets. With the link registry (add-link-registry) carrying identities, lifecycle statuses, and dates (add-date-extraction), the harvester can now **stop paying for files it has already successfully researched and that have not changed** — in both transports (web search pagination is capped at 5 pages anyway; the expensive part is exactly gather+check). This is the first change that alters behavior, so it ships behind a three-position flag with a shadow mode that measures the empirical cost of a false skip before skipping becomes real.

## What Changes

- New decision point in `SearchStage._search_worker` immediately before `create_acquisition_task` (`stage/definition.py:114-127`) — a single choke point covering both API and web transports.
- Registry **read path**: read-only SQLite connections (WAL permits concurrency with the writer thread), batched lookups (`WHERE url_hash IN (…)`) per result page.
- Skip rule — a strict conjunction, skip only if ALL hold:
  1. `visit_status = 'gathered_ok'` (never skip `discovered` or `failed`);
  2. `gathered_ts` within TTL (`skip.gather_ttl_hours`, default 168 h = 7 days);
  3. NOT (`repo_pushed_at > gathered_ts`) — fresh-push invalidation;
  4. a `link_coverage` row exists for `(url_hash, current provider, current patterns_hash)` — provider/pattern-switch awareness.
- Any read error or missing data ⇒ treated as unknown ⇒ task IS created (fail-open in the safe direction: absence of evidence never causes a skip).
- Feature flag `skip_known: off | shadow | on` (default `off`): shadow logs every would-skip decision to a structured JSONL decision log while still creating tasks; `on` enforces skips.
- Metrics: `skipped_known`, `regathered_changed`, `regathered_coverage_gap`, `regathered_ttl_expired`, `regathered_failed_retry`, attributed by the first failing condition.
- Config section `skip` in schemas/loader/validator + example configs.

## Capabilities

### New Capabilities
- `gather-skip`: registry-driven suppression of redundant acquisition tasks under a conservative four-condition rule, with three-mode rollout (off/shadow/on), reason-attributed metrics, and fail-open reads.

### Modified Capabilities
<!-- link-registry requirements are unchanged: this change only READS the registry; its write-only guarantee ("no reads influence decisions") is scoped to the add-link-registry change itself and is not part of that capability's ongoing contract -->

## Impact

- **Modified code:** `stage/definition.py` (decision point in SearchStage), `stage/base.py` (StageResources field), `config/schemas.py` / `loader.py` / `validator.py`, `examples/config-full.yaml`, `manager/pipeline.py` + `core/metrics.py` + `state/display.py` (engine wiring, run stats, display). `storage/registry.py` stays untouched — the write-only facade is preserved; the read path lives entirely in the new decision unit.
- **New code:** `storage/gather_skip.py` — skip-decision engine (read-only connections, conjunctive rule, counters) incl. the shadow decision-log writer (JSONL under `<workspace>/`).
- **Depends on:** add-link-registry (identities, statuses, coverage), add-date-extraction (`repo_pushed_at` invalidation signal; without dates condition 3 simply never fires — TTL-only degradation, safe).
- **Behavior:** with default `off` — zero change. With `on` — fewer AcquisitionTasks and fewer downstream CheckTasks for unchanged known files; shard `links` records continue to be written for every search hit (audit log unaffected).
