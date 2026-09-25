# Delta Spec: date-extraction

## MODIFIED Requirements

### Requirement: File commit date from gathered blob pages

During acquisition the system SHALL extract ISO datetimes from `<relative-time … datetime="…">` elements **when the transported payload carries such markup** and record the **maximum** of all matches as `file_commit_date` (conservative bias: overestimating freshness causes at most an extra re-gather, underestimating could cause a false skip). The extraction point is therefore transport-dependent: it applies to a rendered blob page, and a transport that delivers plain file content or a structured REST response carries no such markup, in which case `file_commit_date` SHALL be NULL by construction without any parse attempt and without exception. Live probing (September 2026, design D7; re-confirmed against a live blob page on 2026-09-25, where the marker census found `relative-time` present once but `datetime=` present zero times because the page is a client-side-rendered shell) established that current GitHub does not serve these attributes, so NULL is the prevailing production case on every transport, not an edge case. Zero matches SHALL yield NULL without exception. Fill-rate metrics SHALL remain in place unchanged as the drift detector that announces if GitHub ever restores the fields, and switching transport SHALL NOT worsen the observed NULL share.

#### Scenario: Date captured from blob HTML

- **WHEN** a gathered blob page contains one or more `relative-time` elements with `datetime` attributes
- **THEN** `file_commit_date` equals the latest of those datetimes

#### Scenario: Layout without markers yields NULL safely

- **WHEN** a gathered page contains no `relative-time` elements (e.g., layout changed or non-blob page)
- **THEN** `file_commit_date` is NULL, a warning counter increments, and key extraction proceeds unaffected

#### Scenario: Live-captured blob page yields NULL safely

- **WHEN** acquisition processes a live-captured (September 2026) blob HTML page whose timestamps are rendered client-side
- **THEN** `file_commit_date` is NULL, a warning counter increments, key extraction proceeds unaffected, and `date_fill_rate_web` reports 0.0 for that run

#### Scenario: Plain-content transport yields NULL without a parse attempt

- **WHEN** acquisition runs under a transport that delivers raw file bytes rather than a rendered page
- **THEN** `file_commit_date` is NULL, no markup parse is attempted, key extraction proceeds unaffected, and no exception reaches the worker loop

#### Scenario: Transport switch does not worsen the fill rate

- **WHEN** the same workload is gathered under the rendered-page transport and under the default plain-content transport
- **THEN** the proportion of gathered links carrying a non-NULL `file_commit_date` under the plain-content transport is not lower than under the rendered-page transport
