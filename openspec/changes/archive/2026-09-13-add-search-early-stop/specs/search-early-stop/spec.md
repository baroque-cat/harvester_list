# Delta Spec: search-early-stop

## Purpose

Terminates API-transport pagination for a search partition once its result stream is demonstrably saturated with already-researched links, converting repeat runs into delta collection while guaranteeing — via ratio windows, minimum page floors, trust gates, kill-switches, and shadow measurement — that saturation is never confused with exhaustion when evidence is incomplete.

## ADDED Requirements

### Requirement: Frontier-based pagination stop

For an API-transport partition the system SHALL stop requesting further pages when ALL hold: the trailing window of W results (default 100) has known-ratio ≥ θ (default 0.9); at least `min_pages` (default 2) pages have been fetched for this partition; and the registry trust gate passes. A result counts as known only if the conjunctive gather-skip rule would skip it: successfully gathered (`gathered_ok`) under the current provider and patterns hash, within the gather TTL, and without positive push-invalidation evidence (NULL `repo_pushed_at` passes vacuously per the amended gather-skip spec). Everything else — including discovered-only, foreign-coverage, and TTL-expired links — counts as novel.

#### Scenario: Saturated window stops pagination

- **WHEN** pages 1–2 of a partition yield 100 results of which 97 are known (θ=0.9, W=100) and the trust gate passes
- **THEN** no page-3 task is created for that partition and `early_stop_fired` increments

#### Scenario: Never stops on the first page

- **WHEN** page 1 alone is fully saturated with known links
- **THEN** pagination continues to page 2 regardless (min_pages floor)

#### Scenario: Unsaturated window continues to the cap

- **WHEN** the trailing window's known-ratio stays below θ through all pages
- **THEN** pagination proceeds up to the existing API_MAX_PAGES cap exactly as before

#### Scenario: Discovered-only links count as novel

- **WHEN** window results include links present in the registry with `visit_status='discovered'` or gathered under another provider/patterns
- **THEN** those results count as novel in the ratio

#### Scenario: TTL-expired links count as novel

- **WHEN** window results include links whose `gathered_ts` exceeds the gather TTL (they will be re-gathered regardless of pagination depth)
- **THEN** those results count as novel in the ratio, keeping early-stop inert while genuine re-gather work remains upstream

### Requirement: Web transport exclusion

The early-stop detector SHALL be inert for web-transport searches irrespective of flag value; web pagination behavior SHALL remain byte-for-byte as pre-change.

#### Scenario: Web run with flag on paginates unchanged

- **WHEN** `early_stop=on` and a search task uses the web transport
- **THEN** page tasks are generated solely by the existing total/per-page logic and no window tracking occurs

### Requirement: Per-partition independence

Each refined partition (distinct query string produced by RefineEngine) SHALL maintain its own window and stop decision; stopping one partition SHALL NOT affect siblings.

#### Scenario: Mixed partitions act independently

- **WHEN** partition A's window saturates while partition B's stream is mostly novel
- **THEN** A stops paginating and B continues to its own cap or saturation

### Requirement: Trust gate and kill-switch

Early-stop SHALL evaluate only when the registry is trusted: total `links` rows ≥ `min_trust`, the one-time migration is marked complete, and the current run has recorded zero registry errors. Any registry error occurring during the run SHALL mute early-stop for the remainder of that run.

#### Scenario: Fresh registry forces full passes

- **WHEN** the registry has fewer rows than `min_trust` (e.g., first run after install)
- **THEN** no stop fires even on fully saturated windows

#### Scenario: Mid-run error mutes stopping

- **WHEN** a registry read or write error occurs mid-run and a later partition window saturates
- **THEN** pagination continues (stop muted) and the run is marked degraded

### Requirement: Three-mode flag with shadow measurement

`early_stop` SHALL accept `off` (default, detector never evaluates), `shadow` (decisions computed and logged, pagination always continues), and `on` (enforced). In shadow mode the system SHALL log `would_stop_at` records (partition, page, window stats) and SHALL compute `novel_after_stop` — the count of distinct links that the gather-skip conjunction would **not** skip (i.e., that still require research) and that arrived after the hypothetical stop point — quantifying the false-stop price conservatively (any missed work counts, regardless of when it was first seen).

#### Scenario: Shadow logs but never acts

- **WHEN** `early_stop=shadow` and a partition window saturates at page 2
- **THEN** a `would_stop_at` record is appended to the decision log, `early_stop_would_fire` increments, and pages 3+ are still fetched

#### Scenario: False-stop price is measured

- **WHEN** during a shadow run, 4 links first-seen this run appear on pages after a logged `would_stop_at` point for that partition
- **THEN** `novel_after_stop` for that partition equals 4
