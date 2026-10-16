## MODIFIED Requirements

### Requirement: Persistent global registry storage

The system SHALL maintain a single SQLite database file at `<workspace>/registry.sqlite` (configurable path), stored **outside** any `providers/<folder>/` directory, so that its contents survive restarts and changes of the enabled provider set. The database SHALL operate in WAL journal mode and SHALL be created automatically with the current schema version on first use. Schema evolution SHALL be **additive only**: a database created at an earlier version SHALL be migrated in place on open by an idempotent guard, migrating twice SHALL be equivalent to migrating once, and all pre-existing rows SHALL survive unchanged. Because migrations only add, a build that predates the current schema version SHALL still be able to read a database at the newer version, so rolling back the binary SHALL NOT require rolling back the data.

#### Scenario: Registry persists across restarts

- **WHEN** a run records links into the registry and the process exits gracefully, and a new run starts against the same workspace
- **THEN** all previously recorded rows are present in the database before any new writes occur

#### Scenario: Registry is shared across provider-set changes

- **WHEN** run A executes with provider `openai` enabled and records links, and run B executes with only provider `anthropic` enabled against the same workspace
- **THEN** run B observes the same registry file and the rows written during run A are intact and attributable via their coverage/provider columns

#### Scenario: Schema bootstrap on empty workspace

- **WHEN** the registry is enabled and no database file exists yet
- **THEN** the system creates the file with all tables (`links`, `link_coverage`, `keys`, `runs`, `repos`, `meta`, `file_meta`) and a recorded schema version, without failing the run

#### Scenario: Database at an earlier schema version migrates in place

- **WHEN** the registry opens a database whose recorded schema version predates the current one
- **THEN** the missing columns and tables are added, the recorded version becomes current, and every pre-existing row keeps its prior values

#### Scenario: Schema migration is idempotent on reopen

- **WHEN** an already-migrated database is opened again, or the migration guard runs twice in one process
- **THEN** no error is raised, no column is duplicated and the recorded schema version is unchanged

#### Scenario: Older build reads a newer database

- **WHEN** a build that does not know the newest columns opens a database at the newer schema version
- **THEN** reads and writes of the columns it does know succeed, and the unknown columns retain their values

### Requirement: Link lifecycle recording

The system SHALL record link discovery at the search stage (upsert with `first_seen_ts` on first insert, `last_seen_ts` refresh on re-observation, plus `transport` and `query_origin`) and gather outcome at the acquisition stage (`gathered_ts` and `visit_status ∈ {discovered, gathered_ok, failed}`). Links seen but never successfully gathered SHALL remain distinguishable from gathered ones; failed gathers SHALL be marked so later phases can prioritize retry. The registry SHALL additionally store, per link, the conditional-revalidation token returned by the transport that fetched it together with an identification of that transport, so that a later run can ask whether the content changed without downloading it again. A token SHALL only ever be replayed to the transport that produced it.

#### Scenario: First discovery inserts

- **WHEN** a search result URL not present in the registry is processed
- **THEN** a new `links` row is inserted with `visit_status='discovered'`, `first_seen_ts` and `last_seen_ts` set to the observation time, and `gathered_ts` NULL

#### Scenario: Re-discovery updates only recency fields

- **WHEN** a URL already present in the registry is observed again in a later run
- **THEN** `last_seen_ts` advances, `first_seen_ts` stays unchanged, and no duplicate row is created

#### Scenario: Gather outcome is recorded

- **WHEN** the acquisition stage completes fetching a registered link, successfully or not
- **THEN** the row's `visit_status` becomes `gathered_ok` with `gathered_ts` set on success, or `failed` on error, and a previously successful `gathered_ok` status is never downgraded to `discovered` by later re-observations

#### Scenario: Revalidation token is stored with its transport

- **WHEN** a link is gathered successfully and the response carries a revalidation token
- **THEN** the token is stored on that link's row together with the transport that produced it, replacing any token previously stored from a different transport

#### Scenario: Absent token leaves the row usable

- **WHEN** a link is gathered successfully but the response carries no revalidation token
- **THEN** the row records the gather outcome normally, stores no token, and later runs fetch it unconditionally rather than failing
