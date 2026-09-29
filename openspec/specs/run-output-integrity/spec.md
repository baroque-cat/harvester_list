# run-output-integrity Specification

## Purpose

Defines what a run may write to an operator-visible sink — the console and its log files — and when: credential material is redacted at every sink by a layer that fails closed and is attached where records actually pass, nothing is written once the run has begun closing a sink, a clean stop reports an honest exit status, work discarded during teardown stays loud and counted, and a test run writes to its own sink instead of the repository's.

## Requirements

### Requirement: Every operator-visible sink redacts credential material

Every sink that carries run output to an operator — the console, and every log file in every
structured or unstructured mode the logging layer offers — SHALL redact credential material from the
final rendered line before that line is written. Redaction SHALL be applied to the rendered line
rather than to the message alone, so material carried by an attached traceback is covered on the same
terms as material in the message. Redaction SHALL fail closed: when it cannot be performed, the line
SHALL be replaced by a marker that keeps the record's level, its origin logger and its source
location and omits the payload. Completeness SHALL be enforced by inspecting the logging layer rather
than by maintaining a list of the sinks that happen to be covered, so that a new sink cannot ship
unredacted.

#### Scenario: ROI-S1 a credential logged to the console never reaches it
- **WHEN** a record whose message contains a harvested credential is emitted through a category
  logger while the console sink is captured
- **THEN** the captured console text contains no form of that credential, masked or unmasked, while
  the rest of the line renders normally

#### Scenario: ROI-S2 a credential inside an attached traceback is redacted too
- **WHEN** a record is emitted with exception information whose traceback text contains a credential
- **THEN** the rendered line reaching the sink carries the traceback with the credential redacted,
  demonstrating that redaction covers what the rendering adds and not only the message

#### Scenario: ROI-S3 a redaction failure publishes a marker, not the payload
- **WHEN** redaction of a rendered line raises instead of returning a redacted line
- **THEN** the sink receives a marker that names the record's level, origin logger and source
  location and omits the payload, and no part of the unredacted line is written

#### Scenario: ROI-S4 no sink in the logging layer can ship without redaction
- **WHEN** the renderers defined by the logging layer are enumerated and each is given a line
  containing a credential
- **THEN** every one of them redacts it, so that adding a renderer without redaction is a test
  failure rather than a silent gap

### Requirement: Redaction is attached where records actually pass, and secrets are not emitted

A redaction mechanism SHALL be attached at a point the run's records actually traverse. A filter
attached to an ancestor logger that the run's records never reach — because the emitting logger does
not propagate, and because propagation consults ancestor handlers and never ancestor filters — SHALL
NOT be counted as coverage, and that semantics SHALL be pinned so the arrangement cannot be
reintroduced in the belief that it works. Independently of redaction, a diagnostic message SHALL NOT
interpolate a credential value it does not need: where a request's headers are worth reporting, their
names SHALL be reported and their values omitted.

#### Scenario: ROI-S5 a filter on the ancestor logger never sees a pipeline record
- **WHEN** a filter that records what it sees is attached to the process's top-level logger and a
  record is emitted through a category logger of the run
- **THEN** the filter saw nothing, establishing that redaction placed there cannot protect any sink

#### Scenario: ROI-S6 a failed request reports header names and no header value
- **WHEN** an outbound request fails and the failure is logged with its status and its headers
- **THEN** the record names the headers that were sent and carries no header value, so the credential
  is absent from the message before any redaction is applied

### Requirement: Nothing is written to a sink after the run has begun closing it

Once the run has begun finalizing its output — flushing what was already written and then closing the
sinks — a subsequent record SHALL produce no write to any sink, whichever thread emits it. Output
produced before finalization began SHALL still be flushed, so suppression SHALL NOT cost the run its
final report. A worker thread that outlives the bounded join SHALL NOT change the run's exit status: a
run that stopped cleanly and preserved its durable state SHALL report success, and its survivors SHALL
still be reported loudly as survivors.

#### Scenario: ROI-S7 a record emitted after finalization began produces no write
- **WHEN** finalization of the logging layer has begun and a thread emits a record to a sink that was
  open before it
- **THEN** the sink receives nothing further and the emission raises nothing into the emitting thread

#### Scenario: ROI-S8 output produced before finalization is still flushed
- **WHEN** records are emitted before finalization begins and finalization then runs
- **THEN** every one of those records is present in its sink, so suppression applies only to what
  arrives afterwards

#### Scenario: ROI-S9 a surviving worker does not turn a clean stop into a failure status
- **WHEN** a run is stopped within its budget, one worker thread outlives the bounded join and emits
  records afterwards, and the process then exits
- **THEN** the process reports a success status rather than dying on a write to a closing output
  stream, and the survivor was reported as a survivor

### Requirement: Work discarded during teardown is loud once and counted always

A task discarded because its stage has stopped accepting work SHALL be reported once per stage, naming
the condition and stating that further discards are counted rather than logged, and the running total
SHALL be reported with that stage's stop-completion record. Discarding SHALL NOT become silent: the
total SHALL be obtainable after the run, and the change SHALL reduce the number of identical lines
without reducing the information.

#### Scenario: ROI-S10 the first teardown discard is reported and later ones are counted
- **WHEN** a stage that has stopped accepting work is handed several tasks in succession
- **THEN** exactly one report names the condition and states that further discards are counted, the
  remaining discards produce no further report, and every one of them is counted

#### Scenario: ROI-S11 the discard total is reported with the stop-completion record
- **WHEN** a stage finishes stopping after having discarded tasks
- **THEN** its stop-completion record carries the total number discarded, distinguishing a graceful
  stop from one that left survivors

### Requirement: A test run writes to its own sink and collects only its own tests

The location of the run's log sink SHALL be resolvable from a value supplied by the caller rather than
fixed to a path relative to the process working directory, while an unsupplied value SHALL keep the
shipped default unchanged. A test run SHALL therefore write no log file into the repository, and test
collection SHALL be scoped so that a package module at the repository root is not collected as a test —
the condition that otherwise makes a measurement irreproducible at another commit outside the main
working tree.

#### Scenario: ROI-S12 a supplied sink location survives logging setup and is used
- **WHEN** a sink location is supplied before logging is set up and a record is then emitted
- **THEN** the log file is created under the supplied location and not under a path relative to the
  working directory, and an unsupplied location still resolves to the shipped default

#### Scenario: ROI-S13 a test run leaves no log file in the repository
- **WHEN** the test suite has run to completion
- **THEN** the repository holds no log directory or log file created by that run, so operator journals
  cannot be confused with test output

#### Scenario: ROI-S14 collection from the repository root does not include the root package module
- **WHEN** test collection is performed from the repository root
- **THEN** the package module at the repository root is not among the collected items, and the
  collected set is the tests directory
