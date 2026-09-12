# Proposal: add-search-early-stop

## Why

With a trustworthy registry, repeated API-transport runs re-walk pagination and RefineEngine partitions whose results are almost entirely known: `sort=indexed&order=desc` returns freshly-indexed files first, so once a run's sliding window over a partition's result stream is saturated with already-researched links, the frontier has been reached and deeper pages contain only re-runs of the past. Stopping there converts every repeat run from a full pass into delta collection — the single biggest quota saving on the API transport. This is also the **riskiest** mode in the whole roadmap (a false stop = missed leaks), which is why it ships last among search-side changes, gated by trust thresholds, a kill-switch, and a shadow period that measures the empirical price of a wrong stop (`novel_after_stop`) before enforcement.

## What Changes

- Frontier detector inside SearchStage pagination logic (`_handle_first_page_results` and page-task generation, `stage/definition.py:224-296`): a per-`(provider, query/partition)` sliding-window tracker over the API result stream.
- Stop rule — ALL must hold to stop requesting further pages of that partition:
  1. known-ratio ≥ θ (default 0.9) over the trailing window of W results (default 100);
  2. at least `min_pages` fetched for this partition (default 2 — never stop on the first page);
  3. registry trust gate passes: row count ≥ `min_trust`, migration completed, and zero registry errors during this run.
- "Known" reuses the gather-skip decision semantics (successfully gathered under current provider+patterns); anything else counts as novel — one unified notion across skip and stop.
- Kill-switch: any registry error during the run mutes early-stop until run end.
- **API transport only** (`use_api=true`); for the web transport the detector is disabled at code level (relevance-ordered results make saturation meaningless).
- Feature flag `early_stop: off | shadow | on` (default `off`). Shadow logs `would_stop_at` records and continues paginating; the run then computes `novel_after_stop` — how many genuinely new links appeared beyond the hypothetical stop point.
- Metrics: `early_stop_would_fire`, `early_stop_fired`, `novel_after_stop`, plus per-partition window snapshots in the shared JSONL decision log.

## Capabilities

### New Capabilities
- `search-early-stop`: frontier-based termination of API pagination per refined partition, with ratio-window detection, trust gating, transport restriction, three-mode rollout, and shadow measurement of false-stop cost.

### Modified Capabilities
<!-- none: gather-skip's requirements are untouched; this capability consumes its decision semantics without altering them -->

## Impact

- **Modified code:** `stage/definition.py` (pagination decision points), `storage/registry.py` (trust-gate queries: count, migration marker, degraded flag), config schemas/loader/validator (`early_stop` section: mode, window, theta, min_pages, min_trust), stats display.
- **Depends on:** add-link-registry (identities, runs journal, degraded marker), add-gather-skip (shared "known" decision semantics), add-date-extraction (not strictly required, but registry maturity metrics come from phases 1–3 operation).
- **Behavior:** default `off` — zero change. With `on` — fewer page fetches and fewer refined sub-query executions on repeat runs; first-ever runs against a fresh registry always do full passes (trust gate fails by construction).
- **Risk profile:** highest in the roadmap; mitigations are structural (θ+W+min_pages conjunction, trust gate, kill-switch, shadow promotion gate requiring `novel_after_stop ≈ 0` over diverse staging queries).
