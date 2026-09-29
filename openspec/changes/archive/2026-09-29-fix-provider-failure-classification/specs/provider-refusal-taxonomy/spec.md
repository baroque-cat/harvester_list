# provider-refusal-taxonomy delta

## Purpose

Defines how a capacity refusal encountered while talking to an LLM provider is recognised,
signalled, bounded, counted and surfaced — so that "the provider told us to slow down" can never be
mistaken for "this task failed" or "this provider has no models".

## ADDED Requirements

### Requirement: Capacity refusals are recognised by wire signal, not by status code alone

A refusal SHALL be classified as a *capacity* refusal when the response carries a recognised
throttling signal — an HTTP 429, or an HTTP 403 whose headers or body carry a rate-limit / abuse
marker — and as an *authentication* failure otherwise. The contract is opportunistic: a published
resumption time (`Retry-After`, a rate-limit reset timestamp, or a wait expressed in the body) SHALL
be used when present, and when absent the system SHALL fall back to the pre-existing transient
classification rather than inventing a wait. Recognition SHALL be a single rule shared by every
outbound surface, so the same signal cannot yield two different verdicts.

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

### Requirement: A published wait is handed to the stage, never slept in the transport

The transport SHALL NOT sleep for a published resumption time. It SHALL publish the wait on the
typed signal so the stage performs a bounded wait and defers the task through the durable queue.
Any wait SHALL be clamped by a configured cap that is validated as strictly below the queue
visibility window, because no claim-renewal API exists. The typed signal SHALL be neither retried by
the retry policy nor absorbed by a fail-open wrapper.

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

### Requirement: Refusals are counted by class and are never silent

Every provider-call outcome SHALL land in exactly one counted class: an answer, a genuine empty
answer, or a refusal carrying a reason class (rate-limit, quota, authentication, transient, or
starvation by our own provider basket). A refusal SHALL never be recorded as "the provider has no
models", and a worker SHALL never consume a task without recording an outcome. The published
counters SHALL be rendered in detailed status and SHALL be registered in the surface-completeness
set, so publishing a counter nobody can read is a test failure.

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

### Requirement: Rollback is a configuration flip

The classification SHALL be governed by one boolean configuration value, defaulted to the fixed
behavior. Setting it to false SHALL restore the legacy classification byte-for-byte — including the
failure report to the adaptive budget and the consumption of a retry attempt — so that a
half-rollback cannot keep the destructive half. A non-boolean value SHALL be rejected loudly at load
time rather than coerced.

#### Scenario: PRT-S13 — the legacy classification returns with the flag
- **WHEN** the flag is false and a provider basket starves a task
- **THEN** the task is re-enqueued through the bounded-retry path with its attempt count
  incremented, the legacy error classification is used, and the own-basket deferral counter stays
  at zero

#### Scenario: PRT-S14 — a non-boolean flag is rejected loudly
- **WHEN** the flag is configured as a string
- **THEN** configuration loading fails with an error naming the value, and no truthy coercion is
  applied
