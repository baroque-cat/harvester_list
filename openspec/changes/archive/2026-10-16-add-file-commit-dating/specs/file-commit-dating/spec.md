## Purpose

Supplies per-file freshness evidence — the date of the last commit that touched one specific path in one specific repository — so that a later run can tell a newly leaked credential from one already recorded, and so repositories that have leaked can be dated, sorted and eventually used to stop searching. Acquisition is budgeted and opportunistic: the absence of evidence must never be reported as a date.

## ADDED Requirements

### Requirement: Per-file commit date is acquired opportunistically and never fabricated

The system SHALL obtain the last commit date of an individual file from the code-hosting API and SHALL record it against that file's registry entry, normalised to UTC epoch seconds. When a response carries no usable date for the requested path, the system SHALL record no date, SHALL preserve any previously stored value, and SHALL count the absence. It SHALL NOT substitute a repository-level push date, a discovery timestamp, or any default value.

#### Scenario: Date captured for a single file
- **WHEN** a file is submitted for dating and the response carries a committer date for that path
- **THEN** the file's recorded commit date equals that value in UTC epoch seconds and a filled counter increments

#### Scenario: Response without a usable date degrades to no evidence
- **WHEN** the response is well formed but contains no commit for that path, or the date field is absent, empty or unparseable
- **THEN** no date is written, any previously stored value survives unchanged, and an absence counter increments

#### Scenario: Repository push date is never substituted for a file date
- **WHEN** a file's own commit date cannot be obtained while the enclosing repository's push date is known
- **THEN** the file's commit date remains unset rather than being filled from the repository value

#### Scenario: A commit date may predate repository creation
- **WHEN** the returned commit date is earlier than the repository's creation date because history was imported from another system
- **THEN** the value is recorded as-is, is not treated as an error, and no consumer assumes ordering against repository creation

### Requirement: Two interchangeable acquisition transports with bounded batching

The system SHALL support a one-file-per-request REST form and a batched GraphQL form behind a single configuration switch, and the choice of transport SHALL NOT change the meaning or units of the recorded value. Batch size SHALL be configurable with an enforced upper bound rejected loudly at load time.

#### Scenario: REST form dates one file per request
- **WHEN** dating runs under the REST transport
- **THEN** each request concerns exactly one path and its result is recorded independently of every other request

#### Scenario: Batched form dates several files in one request
- **WHEN** dating runs under the batched transport with more than one file pending
- **THEN** several files are resolved in a single request, each alias result is attributed to its own file, and one alias failing does not discard the others

#### Scenario: Batch ceiling is enforced by configuration validation
- **WHEN** the configured batch size is not a positive integer within the supported bound
- **THEN** configuration loading fails loudly with a message naming the field, and no run starts

#### Scenario: Transport switch preserves recorded semantics
- **WHEN** the same file is dated under each transport in turn
- **THEN** both produce the same recorded value for the same underlying commit

### Requirement: Dating cannot starve search

Batched dating traffic SHALL ride its own rate-limit budget, separate from the code-search budget, and dating SHALL additionally honour a per-run request ceiling. Exhausting either SHALL stop dating without failing the run, without dropping tasks and without reducing search throughput.

#### Scenario: Batched dating does not consume the code-search budget
- **WHEN** dating issues requests under the batched transport
- **THEN** the code-search budget's remaining allowance is unchanged by those requests

#### Scenario: Per-run ceiling stops dating quietly
- **WHEN** the number of dating requests in one run reaches the configured ceiling
- **THEN** further dating is skipped for that run, a budget-stopped counter increments, and the run still completes successfully

#### Scenario: Search throughput is unchanged when dating is disabled
- **WHEN** two otherwise identical runs execute with dating disabled and enabled
- **THEN** the disabled run's search request count and admitted-query figures are not reduced by the enabled run's dating traffic beyond the configured budget

### Requirement: Failures are typed signals, including errors carried inside a successful status

A response whose HTTP status indicates success but whose body reports errors SHALL be classified into the same typed signals used for ordinary refusals and permanent failures, and SHALL NOT be returned as an empty successful result. Classification SHALL drive the existing bounded-wait, deferral, retry-budget and loud-drop behaviour.

#### Scenario: Errors inside a successful status are classified, not swallowed
- **WHEN** a batched dating response returns a success status together with a body reporting errors
- **THEN** the affected files receive no date, the condition is classified as a refusal or a transient failure according to its reported cause, and no caller observes an empty success

#### Scenario: Point or quota exhaustion defers without burning attempts
- **WHEN** dating is refused because the account's allowance for that endpoint is exhausted
- **THEN** the work is deferred with a bounded wait, the retry budget is not consumed, and a refusal counter increments

#### Scenario: Absent repository or path drops loudly once and suppresses retries
- **WHEN** the target repository or path is reported as not found
- **THEN** the attempt is counted as a permanent outcome, the cache entry records the target as gone so later runs do not retry it, and no infinite requeue follows

#### Scenario: An unrecognised error cause stays transient
- **WHEN** the reported error cause is not one the system recognises
- **THEN** it is treated as transient so bounded retry governs it, and it is never silently treated as "no such date"

### Requirement: Cached evidence has a bounded lifetime and suppresses dead targets

Dating results SHALL be cached per file with a configurable lifetime, a conditional-revalidation token, and a marker for targets reported gone. A fresh cache entry SHALL issue no request; an expired entry SHALL be revalidated conditionally where the endpoint supports it.

#### Scenario: Fresh cache entry issues no request
- **WHEN** a file is submitted for dating and its cached evidence is younger than the configured lifetime
- **THEN** no network request is made and the cached value is propagated

#### Scenario: Expired entry is revalidated conditionally
- **WHEN** cached evidence has expired and a revalidation token is held
- **THEN** the request is sent conditionally, an unchanged answer preserves the stored value at negligible transfer cost, and a changed answer replaces it

#### Scenario: Gone target suppresses further attempts
- **WHEN** a repository or path was previously reported not found
- **THEN** later runs skip it without a request until the suppression is deliberately cleared

#### Scenario: A different observed revision invalidates the cached entry
- **WHEN** the same path is observed at a revision other than the one the cached evidence was resolved against
- **THEN** the cached entry is treated as stale and re-fetched rather than reused

### Requirement: Inert until explicitly enabled, rollback by configuration flag

The subsystem SHALL be disabled by default so that a deployment that does not opt in behaves exactly as before, and enabling or disabling it SHALL require no code change. Enabling SHALL NOT alter harvested findings.

#### Scenario: Disabled by default is indistinguishable from before the change
- **WHEN** no configuration section for dating is present
- **THEN** no dating request is issued, no date column is written by this subsystem, and run output is byte-for-byte what it was before the capability existed

#### Scenario: Enabling does not alter harvest output
- **WHEN** the same workload runs with dating disabled and enabled
- **THEN** extracted values, shard contents and key rows are identical, and only date columns and counters differ

#### Scenario: Invalid configuration fails loudly
- **WHEN** a dating flag is not a boolean, or a numeric field is not positive
- **THEN** configuration loading fails with a message naming the field, whether the value arrived through a loaded file or through direct construction

### Requirement: Scope of dating is configurable and defaults to files with findings

The population submitted for dating SHALL be selectable among files where a credential was found, files successfully gathered, and all discovered files, defaulting to the narrowest set. Widening the scope SHALL be a configuration change requiring no code change.

#### Scenario: Default scope dates only files with findings
- **WHEN** dating runs with default configuration over a workload containing files with and without extracted credentials
- **THEN** only files where a credential was found are submitted for dating

#### Scenario: Widening scope increases the dated population
- **WHEN** the scope is widened to all successfully gathered files
- **THEN** the dated population grows accordingly and the per-run ceiling still bounds request count

### Requirement: Dating coverage is observable per run

Each run SHALL expose dating figures sufficient to judge coverage without inspecting the database: requests issued, unchanged revalidations, values filled, absences, batches, batched-transport cost, refusals and budget stops, together with the fill rate over the dated population. Counter names, once published, SHALL NOT be renamed.

#### Scenario: Counters rendered on the status surface
- **WHEN** a run completes with dating enabled
- **THEN** operator status output shows the dating counters and the fill rate for that run

#### Scenario: Zero-evidence run is distinguishable from a disabled run
- **WHEN** dating is enabled but every response lacked a usable date
- **THEN** the fill rate reports zero while the request and absence counters report the work actually performed, so the run is visibly distinct from one where dating never ran
