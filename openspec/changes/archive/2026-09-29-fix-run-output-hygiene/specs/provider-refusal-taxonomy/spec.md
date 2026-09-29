## MODIFIED Requirements

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
