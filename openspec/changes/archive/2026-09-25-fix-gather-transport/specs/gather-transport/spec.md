# Delta Spec: gather-transport

## Purpose

Governs how the gather stage obtains file content for key extraction: a config-selected transport, correct derivation of the transport-specific address from an already-discovered blob link, rate-limit isolation so gathering cannot starve code search, and refusal handling whose every outcome is bounded, classified and counted rather than silently abandoned.

## ADDED Requirements

### Requirement: Transport selection with loud configuration validation

The system SHALL support a `gather` configuration section selecting the content transport as one of `html`, `raw` or `rest`, together with positive numeric bounds for payload size and refusal waiting. With no `gather` section at all the transport SHALL resolve to `raw`. Selecting `html` SHALL reproduce the pre-change fetching behavior, and switching transports SHALL require no data migration in either direction. An unknown transport name or a non-positive numeric field SHALL fail configuration validation loudly at load time, naming the offending value.

#### Scenario: S1 absent section resolves to the measured-cheapest transport
- **WHEN** a configuration without any `gather` section is loaded
- **THEN** the resolved transport is `raw`, and a configuration that explicitly selects `html` fetches gathered content through the same anonymous rendered-page path used before this capability existed

#### Scenario: S2 unknown transport name fails validation loudly
- **WHEN** a configuration sets the gather transport to a value other than `html`, `raw` or `rest`
- **THEN** configuration loading or validation raises an error naming the offending value and the pipeline is not constructed

#### Scenario: S3 non-positive numeric gather fields fail validation loudly
- **WHEN** a configuration sets a gather payload-size bound or a gather refusal-wait bound to zero or a negative number
- **THEN** configuration loading or validation raises an error identifying the field

### Requirement: Transport address derived opportunistically from the discovered link

Gathered content SHALL be addressed by transforming the link produced at discovery time, and the transformation SHALL be opportunistic: when the link exposes a usable immutable revision the system SHALL use it, and when it does not the system SHALL fall back to a defined safe default, count the fallback and log it — never guessing silently and never raising into a worker loop. The task payload persisted in queues and snapshots SHALL continue to carry the discovered link unchanged, so a transport switch remains valid for already-persisted work. A content-hash field that identifies a file blob rather than a revision SHALL NOT be used as an address component.

#### Scenario: S4 immutable revision is used verbatim
- **WHEN** a discovered link carries a 40-character lowercase hexadecimal revision and a repository path
- **THEN** the derived address targets that exact revision and path on the plain-content host, and no fallback counter increments

#### Scenario: S5 unusable revision falls back loudly
- **WHEN** a discovered link's revision segment is not a 40-character hexadecimal string
- **THEN** the derived address targets the repository's default head instead, a dedicated fallback counter increments, and a warning names the affected link

#### Scenario: S6 blob content hash is never mistaken for a revision
- **WHEN** a discovery payload carries both a blob content hash and a separate revision reference for the same file
- **THEN** the derived address uses the revision reference, and using the blob content hash in its place is demonstrably rejected as an unresolvable address

#### Scenario: S7 unparseable link degrades safely
- **WHEN** the value to be gathered is not a recognizable repository blob link
- **THEN** no exception reaches the worker loop, the outcome is counted, and the task follows the existing failure accounting rather than hanging or crashing the stage

#### Scenario: S8 persisted tasks survive a transport switch
- **WHEN** tasks enqueued under one transport are recovered after the configured transport is changed and the process restarted
- **THEN** the recovered tasks are processed normally under the new transport with no rewrite of their stored payload

### Requirement: Gather traffic is throttled under its own budget

Gather requests SHALL acquire rate-limit tokens before being issued and SHALL report their outcome afterwards, under a budget that is distinct from the code-search budget. The plain-content host SHALL be recognized as its own resource class; an unrecognized host SHALL NOT result in unthrottled traffic. Where the remote publishes remaining-budget headers the system SHALL use them; where it publishes none the system SHALL rely on its own budget and on counted refusals, and the absence of headers SHALL be observable rather than assumed away.

#### Scenario: S9 plain-content host resolves to a real budget
- **WHEN** a gather request is issued to the plain-content host
- **THEN** the request is attributed to a distinct named resource class, a limiter token is acquired before it is sent, and its success or failure is reported back to the adaptive budget

#### Scenario: S10 gathering does not consume the search budget
- **WHEN** gather traffic runs under the plain-content transport while code search runs concurrently
- **THEN** the code-search budget's remaining allowance is unaffected by the number of gather requests issued

#### Scenario: S11 authenticated transports keep their existing budgets
- **WHEN** the transport is set to the rendered-page host or to the REST contents endpoint
- **THEN** requests are attributed to the pre-existing web and API resource classes respectively, and the behavior of already-working authenticated callers is unchanged

### Requirement: Refusals are classified by signal and produce bounded counted outcomes

A gather refusal SHALL be classified by what the remote actually signalled, not by elapsed time. A refusal whose wait duration is published and finite SHALL be deferred; a refusal that is actor-scoped and publishes no resumption time SHALL be deferred together with a stage-level pause, a loud error and a dedicated emergency counter; a genuine authentication failure and a missing resource SHALL each end in a loud, counted drop after a single attempt. A rate-limit refusal delivered with a forbidden status code SHALL NOT be classified as an authentication failure. No refusal outcome SHALL be silent, and no refused task SHALL remain in circulation unbounded.

#### Scenario: S12 forbidden-with-rate-limit-marker is a deferral
- **WHEN** a gather request is refused with a forbidden status code whose body carries a rate-limit marker
- **THEN** the outcome is treated as a rate-limit deferral, the task is not abandoned after a single attempt, and the event is counted separately from authentication failures

#### Scenario: S13 genuine authentication failure drops loudly once
- **WHEN** a gather request is refused with an unauthorized status, or a forbidden status without any rate-limit marker
- **THEN** the task is dropped after one attempt with a warning and a dedicated counter increment, and it is not re-enqueued

#### Scenario: S14 missing resource drops loudly once
- **WHEN** a gather request returns not-found for the addressed revision or path
- **THEN** the task is dropped after one attempt with a counted outcome, and no further requests are issued for it in this run

#### Scenario: S15 finite quota exhaustion defers without burning attempts
- **WHEN** a gather request is refused with a published finite resumption time
- **THEN** the task returns to the pending state with its attempt counter unchanged, the wait is bounded by the configured cap, and a deferral counter increments

#### Scenario: S16 actor-scoped abuse refusal pauses the stage
- **WHEN** a gather request is refused as an abuse or secondary limit that publishes no resumption time
- **THEN** the stage pauses for a bounded interval instead of continuing to generate traffic, an error is logged, and an emergency counter increments

### Requirement: Refusal waits respect the durable queue visibility window

No gather worker SHALL remain asleep inside a claimed durable-queue row longer than that queue's visibility timeout, because an expired claim is re-delivered to another worker. A configuration whose refusal-wait cap is not strictly below the queue visibility timeout SHALL fail validation loudly, naming both values.

#### Scenario: S17 contradictory wait configuration is rejected
- **WHEN** the configured gather refusal-wait cap is greater than or equal to the configured queue visibility timeout
- **THEN** configuration validation fails with an error naming both values, and the pipeline is not constructed

#### Scenario: S18 a deferred task is never executed twice
- **WHEN** a gather task is deferred repeatedly under a durable queue backend while other workers continue consuming
- **THEN** the task is claimed by exactly one worker at a time, no claim expires while its worker is sleeping, and acknowledged work shows no duplicate execution

### Requirement: Payload handling is economical, bounded and observable

Extraction SHALL operate on the transported payload with unchanged pattern semantics, and switching to a plainer payload SHALL NOT reduce the set of values extracted from the same file. Because a plain-content response has no practical upper bound, payloads SHALL be read with a byte cap: an oversized payload SHALL be truncated, counted, and its retained prefix SHALL still be searched rather than discarded. Per-transport volume, latency, refusal-class and deferral figures SHALL be exposed for operators.

#### Scenario: S19 plainer payload loses no findings
- **WHEN** the same real file is fetched through the rendered-page transport and through the plain-content transport and both payloads are passed to the project's own extraction routine
- **THEN** the set of values extracted from the plain payload is a superset of the set extracted from the rendered payload

#### Scenario: S20 oversized payload is truncated and still searched
- **WHEN** a gathered payload exceeds the configured byte cap
- **THEN** reading stops at the cap, a truncation counter increments, the retained prefix is still searched for matches, and the task is not treated as a fetch failure

#### Scenario: S21 transport economics are observable
- **WHEN** a run completes with gather traffic on any transport
- **THEN** status reporting exposes bytes and request counts per transport, refusals split by classification, deferrals, revision fallbacks and truncations
