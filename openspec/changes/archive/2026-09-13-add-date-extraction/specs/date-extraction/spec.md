# Delta Spec: date-extraction

## Purpose

Opportunistically captures repository freshness, repository size, and file-level last-commit dates from HTTP payloads the harvester already downloads (API search JSON, blob HTML pages), delivering them into the link registry without extra network requests and without ever breaking the pipeline on malformed **or absent** data. Live probing (September 2026, design D7) established that current GitHub payloads do not expose these fields at the free extraction points; this capability is therefore the always-on delivery channel and safe-degradation contract, while the actual supply of dates is restored by the `add-repo-meta-enrichment` change. Fill-rate metrics serve as drift detectors: they announce if GitHub ever restores the fields.

## ADDED Requirements

### Requirement: Repository metadata from API search results

For every item of an API code-search response the system SHALL extract `repository.pushed_at` (falling back to `repository.updated_at`, else NULL) and `repository.size` (KB, else NULL), keyed by the item's `html_url`, **when those fields are present in the payload**. Live probing (September 2026, design D7) established that current `/search/code` responses carry a trimmed `repository` object without date/size fields; the binding contract is safe degradation: fields present → parsed and delivered; absent or malformed → NULL fields plus a counted warning, never an exception. Existing return contracts (sets of URLs) SHALL remain unchanged; enrichment SHALL be delivered as a parallel mapping. The parser SHALL remain in production as a dormant trap: if GitHub restores the fields, dates resume flowing with zero code changes.

#### Scenario: Metadata extracted from well-formed items

- **WHEN** an API search response item contains `html_url` and `repository` with `pushed_at` and `size`
- **THEN** the enrichment mapping contains that URL with parsed epoch timestamp and integer size, and the returned URL set is identical to the pre-change behavior

#### Scenario: Missing repository object degrades to NULL

- **WHEN** an API search response item lacks the `repository` object or its date fields
- **THEN** the URL is still returned in the result set and its metadata entry carries NULL date/size with a counted warning

#### Scenario: pushed_at preferred over updated_at

- **WHEN** an item's repository carries both `pushed_at` and `updated_at`
- **THEN** the recorded freshness timestamp equals `pushed_at`

#### Scenario: Live-captured trimmed payload yields NULLs across the board

- **WHEN** the parser processes a live-captured (September 2026) API search response whose `repository` objects lack `pushed_at`/`updated_at`/`size`
- **THEN** every item produces a NULL metadata entry with counted warnings, the URL set matches pre-change behavior, and `date_fill_rate_api` reports 0.0 for that run

### Requirement: File commit date from gathered blob pages

During acquisition the system SHALL extract ISO datetimes from `<relative-time … datetime="…">` elements in the already-downloaded blob HTML **when such markup is present** and record the **maximum** of all matches as `file_commit_date` (conservative bias: overestimating freshness causes at most an extra re-gather, underestimating could cause a false skip). Live probing (September 2026, design D7) established that current GitHub serves blob pages with client-side-rendered timestamps — the served HTML carries no `datetime` attributes. Zero matches SHALL yield NULL without exception; this is the prevailing production case, not an edge case.

#### Scenario: Date captured from blob HTML

- **WHEN** a gathered blob page contains one or more `relative-time` elements with `datetime` attributes
- **THEN** `file_commit_date` equals the latest of those datetimes

#### Scenario: Layout without markers yields NULL safely

- **WHEN** a gathered page contains no `relative-time` elements (e.g., layout changed or non-blob page)
- **THEN** `file_commit_date` is NULL, a warning counter increments, and key extraction proceeds unaffected

#### Scenario: Live-captured blob page yields NULL safely

- **WHEN** acquisition processes a live-captured (September 2026) blob HTML page whose timestamps are rendered client-side
- **THEN** `file_commit_date` is NULL, a warning counter increments, key extraction proceeds unaffected, and `date_fill_rate_web` reports 0.0 for that run

### Requirement: Transport asymmetry enforced

The system SHALL NOT attempt date extraction from web **search-results** HTML; for the web transport, dates SHALL arrive only at gather stage. This keeps the fragile search-page parsing surface unchanged.

#### Scenario: Web search produces no dates

- **WHEN** search runs with the web transport (`use_api=false`)
- **THEN** no `repo_pushed_at`/`repo_size_kb` values are written at search stage, and discovered links carry NULL metadata until gathered

### Requirement: Non-regressing delivery into the registry

Extracted metadata SHALL reach the registry through the stage-output channel and SHALL be merged into `links` rows under a non-regression policy: a NULL value never overwrites a known value; a non-NULL value replaces an older one.

#### Scenario: Known date survives later NULL observation

- **WHEN** a link already has `repo_pushed_at` from an API run and is later re-observed via the web transport (no metadata)
- **THEN** the stored `repo_pushed_at` remains unchanged

#### Scenario: Fresher date replaces older

- **WHEN** a re-observation carries a `repo_pushed_at` newer than the stored value
- **THEN** the stored value is updated to the newer timestamp

### Requirement: Fill-rate observability

The system SHALL expose per-run metrics `date_fill_rate_api` (share of API search items that yielded a usable date) and `date_fill_rate_web` (share of gathered pages that yielded a `file_commit_date`), visible in run statistics. The rates SHALL double as drift detectors: a sustained rise from the documented 0.0 baseline (design D7) signals that GitHub restored the fields or that `add-repo-meta-enrichment` began feeding the columns.

#### Scenario: Rates computed over a run

- **WHEN** a run processes 100 API items (95 with dates) and gathers 50 pages (40 with dates)
- **THEN** reported rates are 0.95 and 0.80 respectively

### Requirement: Fail-open parsing

No date-extraction fault (malformed JSON, undecodable HTML, unexpected types) SHALL propagate into stage worker loops; faults SHALL be logged at warning level once per kind and counted.

#### Scenario: Garbage payload does not break search or gather

- **WHEN** the API returns a structurally invalid item or the blob page bytes are undecodable
- **THEN** the affected record gets NULL metadata, the warning counter increments, and the run continues to completion
