# provider-refusal-taxonomy Specification

## Purpose

Defines how a capacity refusal encountered while talking to an LLM provider is recognised,
signalled, bounded, counted and surfaced — so that "the provider told us to slow down" can never be
mistaken for "this task failed" or "this provider has no models".

## Requirements

### Requirement: Capacity refusals are recognised by wire signal, not by status code alone

A refusal SHALL be classified as a *capacity* refusal when the response carries a recognised
throttling signal — an HTTP 429, or an HTTP 403 whose headers or body carry a rate-limit / abuse
marker — and as an *authentication* failure otherwise. The contract is opportunistic: a published
resumption time (`Retry-After`, a rate-limit reset timestamp, or a wait expressed in the body) SHALL
be used when present, and when absent the system SHALL fall back to the pre-existing transient
classification rather than inventing a wait. Recognition SHALL be a single rule shared by every
outbound surface, so the same signal cannot yield two different verdicts. The recognised marker
vocabulary SHALL have exactly one exported definition, consumed by every surface that reads it, so a
vocabulary change is one edit and two surfaces cannot drift apart.

#### Scenario: PRT-S1 — a throttled call carrying a published wait defers with that wait
- **WHEN** a provider call receives HTTP 429 with `Retry-After: 30`
- **THEN** the call raises the typed deferral signal carrying a 30 s wait and the reason class
  `RATE_LIMITED`, and it is not retried blindly in the transport

#### Scenario: PRT-S2 — a 403 carrying a limit marker is a capacity refusal, not an auth failure
- **WHEN** a provider call receives HTTP 403 whose body contains a rate-limit marker
- **THEN** it is classified as a capacity refusal (never as "Authentication failed"), and the
  classification is identical to the one the gather surface gives the same response

#### Scenario: PRT-S3 — a 403 with no limit marker stays an authentication failure
- **WHEN** a provider call receives HTTP 403 with no throttling marker in headers or body
- **THEN** it is classified as an authentication failure exactly as before this capability existed,
  and no deferral is raised

#### Scenario: PRT-S4 — a refusal with no published wait keeps the legacy transient path
- **WHEN** a provider call receives HTTP 429 with no `Retry-After`, no reset timestamp and no wait
  in the body
- **THEN** it remains a retryable transient error handled by the existing retry policy, and no
  deferral is raised

#### Scenario: PRT-S5 — one recognition rule, two surfaces, no divergence
- **WHEN** the same throttled response is presented to the gather surface and to the provider
  surface
- **THEN** both classify it the same way and both derive the same wait, and the gather surface's
  previously specified behavior is unchanged

#### Scenario: PRT-S15 — a marked 403 with no published wait keeps the legacy transient exception
- **WHEN** a provider call receives HTTP 403 whose body carries a throttling marker but which
  publishes no resumption time in either its headers or its body
- **THEN** it raises the pre-existing retryable transient error and never an authentication failure,
  the transport's retry count is the one the legacy transient path produced, and the transient
  refusal counter advances by one

#### Scenario: PRT-S16 — the marker vocabulary has exactly one definition
- **WHEN** the refusal classifier and the credential-cooling content detector are each asked whether
  a body carries a throttling marker
- **THEN** both answer from one shared exported vocabulary, their verdicts agree on every marker and
  non-marker body, and no module keeps a second private copy of the limit markers

### Requirement: A published wait is handed to the stage, never slept in the transport

The transport SHALL NOT sleep for a published resumption time. It SHALL publish the wait on the
typed signal so the stage performs a bounded wait and defers the task through the durable queue.
Any wait SHALL be clamped by a configured cap that is validated as strictly below the queue
visibility window, because no claim-renewal API exists. The cap that clamps a refusal SHALL be the
cap of the surface that produced it: a refusal raised while talking to a model provider is bounded
by the provider ceiling, and a refusal raised while talking to the code-hosting surface is bounded
by that surface's ceiling. A configured ceiling that a ceiling from an unrelated section silently
overrides SHALL NOT be presented to the operator as the bound, and the ceiling reported with a
deferral SHALL be the one that governed. The typed signal SHALL be neither retried by
the retry policy nor absorbed by a fail-open wrapper. Every shipped provider transport SHALL let the
signal escape, including a transport that wraps its own outbound call in a broad exception handler or
that iterates over several publishers.

#### Scenario: PRT-S6 — the wait travels on the signal instead of blocking the transport
- **WHEN** a provider call raises the typed deferral carrying a 30 s wait
- **THEN** the transport performed no additional sleep for that wait, and the stage applies a
  bounded wait before deferring the task

#### Scenario: PRT-S7 — the wait is capped below the visibility window
- **WHEN** a provider publishes a resumption time larger than the configured cap
- **THEN** the wait actually applied is the cap, and a configuration in which the cap is not
  strictly below the queue visibility window is rejected loudly at load time

#### Scenario: PRT-S8 — the signal is neither retried nor swallowed
- **WHEN** the typed deferral propagates out of a provider call that is wrapped in a fail-open
  handler with a default empty result
- **THEN** the signal escapes the wrapper and the retry policy instead of being converted into an
  empty answer or retried, and the retry policy's decision is based on the signal's type rather than
  on words in its message

#### Scenario: PRT-S17 — every shipped provider transport lets the signal escape
- **WHEN** the typed deferral is raised inside a provider transport that wraps its own outbound call
  in a broad exception handler — the multi-publisher transport and the request-signing transport
- **THEN** the signal escapes the handler rather than being converted into an empty answer or into a
  silently skipped publisher, and it does so for every shipped provider transport

#### Scenario: PRT-S26 — a provider refusal is bounded by the provider ceiling, not by another surface's
- **WHEN** a stage that talks to a model provider defers on a published wait larger than the provider
  ceiling, while the ceiling configured for the code-hosting surface is lower still
- **THEN** the wait actually applied is the provider ceiling, and the deferral record reports the
  provider ceiling as the bound that governed rather than the unrelated lower one

#### Scenario: PRT-S27 — a code-hosting-surface refusal keeps its own ceiling
- **WHEN** a stage that talks to the code-hosting surface defers on a published wait larger than that
  surface's ceiling
- **THEN** the wait actually applied is that surface's ceiling, unchanged by the provider-surface
  amendment

#### Scenario: PRT-S28 — the applied wait is unchanged while the two ceilings are equal
- **WHEN** both ceilings hold their shipped default value and a provider publishes a wait larger than
  either
- **THEN** the wait actually applied is the same one that was applied before the ceiling became
  surface-aware, and it remains strictly below the queue visibility window

### Requirement: Refusals are counted by class and are never silent

Every provider-call outcome SHALL land in exactly one counted class: an answer, a genuine empty
answer, or a refusal carrying a reason class (rate-limit, quota, authentication, transient, or
starvation by our own provider basket). A refusal SHALL never be recorded as "the provider has no
models", and a worker SHALL never consume a task without recording an outcome. The published
counters SHALL be rendered in detailed status and SHALL be registered in the surface-completeness
set, so publishing a counter nobody can read is a test failure. Every declared refusal class SHALL
have a producer in shipped code: a counter that is declared, rendered and registered but never
incremented does not satisfy this requirement. Counting SHALL be strictly observational — it SHALL
NOT change the exception type, the message, the retry count or the deferral decision of any path that
existed before this capability. A refusal class SHALL be recognised opportunistically from markers
already proven in the codebase; when no marker is present the outcome SHALL fall through to the
pre-existing classification untouched. A refusal whose exception is swallowed by a fail-open wrapper
SHALL still be recorded as a refusal at the stage: the empty-answer counter is reserved for genuine
empty answers. The refusal classes describe **our own** provider credentials being turned away; the
outcome of testing a credential harvested from a public repository stays on the check surface's own
reason classification, because in a normal run those rejections are the majority of all provider
calls and folding them in would drown the signal an operator reads this line for.

#### Scenario: PRT-S9 — per-class counters distinguish refusal from empty answer
- **WHEN** one provider call is refused with a published wait and another returns an empty model list
- **THEN** the refusal counter for its class and the empty-answer counter each advance by one, and
  neither outcome is counted as a task error

#### Scenario: PRT-S10 — starvation by our own basket is counted apart from a remote refusal
- **WHEN** a task is deferred because our own provider basket had no token
- **THEN** the dedicated own-basket counter advances and the remote rate-limit counter does not, and
  nothing is reported to the adaptive budget because no request was issued

#### Scenario: PRT-S11 — the refusal surface is rendered and registered
- **WHEN** detailed status is rendered after refusals have been counted
- **THEN** one line shows the per-class counters, the line is absent when the surface is empty, and
  the surface is a member of the registered rendered-surface set

#### Scenario: PRT-S12 — no credential material in the refusal path
- **WHEN** a refusal is logged and counted
- **THEN** neither the log line nor any counter contains a token, key or cookie, and the task is
  identified by its hashed identifier rather than by its raw payload

#### Scenario: PRT-S18 — a quota refusal is counted as quota and keeps its legacy exception
- **WHEN** a provider call receives a refusal whose body carries a recognised quota or billing marker
  — an HTTP 403 carrying an exhausted-quota marker, or an HTTP 429 carrying an insufficient-quota
  marker with no published wait
- **THEN** the quota refusal counter advances by one, the empty-answer counter does not, and the
  exception type and message are exactly the ones the legacy path produced for that status

#### Scenario: PRT-S19 — an authentication refusal is counted as auth and keeps its legacy exception
- **WHEN** a provider call receives HTTP 401, or HTTP 403 carrying neither a throttling marker nor a
  quota marker
- **THEN** the authentication refusal counter advances by one, the empty-answer counter does not, no
  deferral is raised, and the exception type and message are unchanged — "exactly as before" in
  PRT-S3 constrains the exception, while the counter is additive

#### Scenario: PRT-S20 — a transient refusal is counted as transient
- **WHEN** a provider call receives HTTP 429 with no published wait and no quota marker, or any
  HTTP 5xx
- **THEN** the transient refusal counter advances by one and the empty-answer counter does not

#### Scenario: PRT-S21 — exactly one class per outcome, and counting never changes behaviour
- **WHEN** the transport is presented with a published-wait refusal that also carries a quota marker,
  a resource-not-found refusal, a generic client error, and a genuine empty answer
- **THEN** the published-wait refusal defers and counts as rate-limit because a published wait
  outranks a quota marker, the resource-not-found and generic client outcomes count no refusal class
  at all, the genuine empty answer counts only as an empty answer, and no outcome advances more than
  one refusal counter

#### Scenario: PRT-S22 — counting is observational and never re-classifies a legacy path
- **WHEN** the same set of refusal statuses is presented to the transport before and after the
  refusal classes gain producers
- **THEN** every exception type, message and wire-hit count is identical, and only the counters differ

#### Scenario: PRT-S24 — a refusal the fail-open wrapper swallows is still not an empty answer
- **WHEN** a provider call is refused with a status that is not deferrable, so the fail-open wrapper
  still returns an empty model list to the stage
- **THEN** the refusal carries its class counter, the stage records the outcome as a refusal rather
  than as an empty answer, the empty-answer counter stays at zero, and no task error is counted

#### Scenario: PRT-S25 — a harvested credential's failure is not a provider refusal
- **WHEN** a credential that was *harvested from a public repository* is tested against its provider
  and the provider rejects it
- **THEN** no refusal counter advances, because the refusal surface describes our own provider
  credentials being turned away, and the harvested credential's outcome stays on the check surface's
  own reason classification where it was already counted

### Requirement: Rollback is a configuration flip

The classification SHALL be governed by one boolean configuration value, defaulted to the fixed
behavior. Setting it to false SHALL restore the legacy classification byte-for-byte — including the
failure report to the adaptive budget and the consumption of a retry attempt — so that a
half-rollback cannot keep the destructive half. A non-boolean value SHALL be rejected loudly at load
time rather than coerced. With the flag false no refusal class SHALL be counted at all: rollback
restores the pre-capability surface, counters included.

#### Scenario: PRT-S13 — the legacy classification returns with the flag
- **WHEN** the flag is false and a provider basket starves a task
- **THEN** the task is re-enqueued through the bounded-retry path with its attempt count
  incremented, the legacy error classification is used, and the own-basket deferral counter stays
  at zero

#### Scenario: PRT-S14 — a non-boolean flag is rejected loudly
- **WHEN** the flag is configured as a string
- **THEN** configuration loading fails with an error naming the value, and no truthy coercion is
  applied

#### Scenario: PRT-S23 — with the flag off no refusal class is counted
- **WHEN** the flag is false and the transport meets an authentication refusal, a quota refusal and a
  transient refusal
- **THEN** every refusal counter stays at zero, the legacy exception types and messages are used, and
  the retry behaviour is unchanged
