## MODIFIED Requirements

### Requirement: Gather traffic is throttled under its own budget

Gather requests SHALL acquire rate-limit tokens before being issued and SHALL report their outcome afterwards, under a budget that is distinct from the code-search budget. The plain-content host SHALL be recognized as its own resource class; an unrecognized host SHALL NOT result in unthrottled traffic. Where the remote publishes remaining-budget headers the system SHALL use them; where it publishes none the system SHALL rely on its own budget and on counted refusals, and the absence of headers SHALL be observable rather than assumed away.

Only the outcome of a request that was actually issued SHALL be reported to the adaptive budget. A request **withheld** by the system's own budget SHALL NOT be reported as a failure: the adaptive budget is a model of remote tolerance, and feeding it an event in which no remote was contacted decays the very budget that produced the withholding, so throttling amplifies itself until the budget reaches its floor.

#### Scenario: S9 plain-content host resolves to a real budget
- **WHEN** a gather request is issued to the plain-content host
- **THEN** the request is attributed to a distinct named resource class, a limiter token is acquired before it is sent, and its success or failure is reported back to the adaptive budget

#### Scenario: S10 gathering does not consume the search budget
- **WHEN** gather traffic runs under the plain-content transport while code search runs concurrently
- **THEN** the code-search budget's remaining allowance is unaffected by the number of gather requests issued

#### Scenario: S11 authenticated transports keep their existing budgets
- **WHEN** the transport is set to the rendered-page host or to the REST contents endpoint
- **THEN** requests are attributed to the pre-existing web and API resource classes respectively, and the behavior of already-working authenticated callers is unchanged

#### Scenario: S22 a withheld request does not decay the budget
- **WHEN** a gather fetch is withheld because the local budget has no token available, so no request reaches the network
- **THEN** nothing is reported to the adaptive budget for that event: its effective rate is unchanged and its consecutive-failure tally does not advance, while outcomes of requests that were actually issued continue to be reported as before

#### Scenario: S23 sustained contention does not collapse the budget
- **WHEN** many gather workers contend for the same budget so that a large number of fetches are withheld in succession
- **THEN** the budget's effective rate never falls below its configured base rate as a result of those withholdings alone, and the number of withheld fetches is observable as a count rather than inferred from a degraded rate

### Requirement: Refusals are classified by signal and produce bounded counted outcomes

A gather refusal SHALL be classified by what the remote actually signalled, not by elapsed time. A refusal whose wait duration is published and finite SHALL be deferred; a refusal that is actor-scoped and publishes no resumption time SHALL be deferred together with a stage-level pause, a loud error and a dedicated emergency counter; a genuine authentication failure and a missing resource SHALL each end in a loud, counted drop after a single attempt. A rate-limit refusal delivered with a forbidden status code SHALL NOT be classified as an authentication failure. No refusal outcome SHALL be silent, and no refused task SHALL remain in circulation unbounded.

A fetch withheld by the system's **own** budget before any request is sent belongs to the deferral category as well: the wait SHALL be derived from that budget's own refill time and SHALL be bounded by the configured refusal cap, the stage that shares the budget SHALL pause instead of letting every worker claim and withhold in lockstep, the event SHALL be counted in a counter distinct from every remote-refusal counter, and it SHALL NOT be treated as a fault of the task. The classification of a local-budget withholding SHALL be switchable back to the legacy failure-empty treatment by configuration alone, with no code change.

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

#### Scenario: S24 own-budget withholding defers with a bounded wait and a stage pause
- **WHEN** a gather fetch cannot obtain a token from its own budget even after the scheduled wait, so no request is sent
- **THEN** the task is deferred with a wait derived from that budget's refill time and bounded by the configured refusal cap, the stage pauses for that interval so its other workers stop claiming into the same empty budget, and the task is neither dropped nor completed empty

#### Scenario: S25 own-budget withholding is counted apart from remote refusals
- **WHEN** a run contains both remote rate-limit refusals and withholdings by the system's own budget
- **THEN** the two are reported as separate counts, so an operator can tell "the remote refused us" from "we refused ourselves" without reading log lines

#### Scenario: S26 legacy classification is a configuration flip
- **WHEN** an operator disables local-budget deferral in configuration and restarts
- **THEN** a withheld gather fetch is classified as a retryable failure-empty exactly as before this capability existed, with no code change and no data migration

### Requirement: Payload handling is economical, bounded and observable

Extraction SHALL operate on the transported payload with unchanged pattern semantics, and switching to a plainer payload SHALL NOT reduce the set of values extracted from the same file. Because a plain-content response has no practical upper bound, payloads SHALL be read with a byte cap: an oversized payload SHALL be truncated, counted, and its retained prefix SHALL still be searched rather than discarded. Per-transport volume, latency, refusal-class and deferral figures SHALL be exposed for operators.

Latency SHALL be exposed as a sample count together with the 50th and 99th percentiles in milliseconds per transport, derived from a distribution whose memory does not grow with the number of requests, and reported conservatively so a published percentile never understates the measured one. Exposure SHALL mean reachable from operator status output, not merely present in an internal structure.

#### Scenario: S19 plainer payload loses no findings
- **WHEN** the same real file is fetched through the rendered-page transport and through the plain-content transport and both payloads are passed to the project's own extraction routine
- **THEN** the set of values extracted from the plain payload is a superset of the set extracted from the rendered payload

#### Scenario: S20 oversized payload is truncated and still searched
- **WHEN** a gathered payload exceeds the configured byte cap
- **THEN** reading stops at the cap, a truncation counter increments, the retained prefix is still searched for matches, and the task is not treated as a fetch failure

#### Scenario: S21 transport economics are observable
- **WHEN** a run completes with gather traffic on any transport
- **THEN** status reporting exposes bytes and request counts per transport, refusals split by classification, deferrals, revision fallbacks and truncations

#### Scenario: S27 latency percentiles are published per transport
- **WHEN** a run completes with gather traffic on any transport
- **THEN** status reporting exposes, for that transport, the number of timed fetches and their 50th and 99th percentile latency in milliseconds, and the mean bytes per fetched file is computable from the published byte and request counts

#### Scenario: S28 latency summary costs bounded memory
- **WHEN** the number of gathered files grows by orders of magnitude between two runs
- **THEN** the memory held by the latency summary is unchanged, because it accumulates into a fixed set of millisecond bands rather than retaining per-request samples

#### Scenario: S29 a withheld fetch is not timed as a slow request
- **WHEN** a gather fetch is withheld by the system's own budget and never reaches the network
- **THEN** the latency summary's sample count does not advance for it, so published percentiles describe real network fetches only
