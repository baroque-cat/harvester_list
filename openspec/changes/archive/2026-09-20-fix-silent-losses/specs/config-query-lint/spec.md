# Delta Spec: config-query-lint

## Purpose

Warns at configuration-validation time about query syntax that is known to be inert on the chosen transport — specifically web-only qualifiers (such as `content:`) inside conditions of `use_api: true` providers, which the GitHub code-search REST API silently treats as zero matches (live probe 2026-09-20, `api.github.com/search/code`, Bearer auth: `q="sk-" AND content:"llm"` → HTTP 200 `{"total_count":0,"incomplete_results":false,"items":[]}`, while `q="sk-"` → `total_count=46006272`).

## ADDED Requirements

### Requirement: Web-only qualifier warning under API transport
The configuration validator SHALL emit a warning — never a load-blocking error — when a condition query of a `use_api: true` provider contains a qualifier from the known web-only list (initially `content:`). The warning SHALL identify the provider name and condition index and explain the transport incompatibility. The configuration SHALL still load and the pipeline SHALL still run: third-party wire behavior is a moving target, so the lint stays advisory (present → warn; qualifier later supported by the API → warning merely stale, no breakage).

#### Scenario: API provider with content qualifier is warned
- **WHEN** a config declares a provider with `use_api: true` whose condition query contains `content:"..."`
- **THEN** validation emits a warning naming that provider and condition index and citing API-transport inertness, and the config loads successfully

#### Scenario: Web provider with the same query is not warned
- **WHEN** the identical condition query appears under a provider with `use_api: false`
- **THEN** no qualifier warning is emitted for it

#### Scenario: Every occurrence is reported separately
- **WHEN** multiple providers or multiple conditions carry web-only qualifiers under API transport
- **THEN** each occurrence produces its own warning entry (provider + condition index), none are collapsed or dropped

### Requirement: Data-driven qualifier list
The set of linted web-only qualifiers SHALL be maintained as configuration-level constant data, so extending the list (e.g., future GitHub web-search-only syntax) requires no change to validation logic.

#### Scenario: New qualifier extends linting without code change
- **WHEN** a new qualifier string is added to the web-only constant list
- **THEN** conditions containing it under `use_api: true` produce the same warning behavior with no validator logic modification
