# gather-share Specification

## Purpose

Removes the last source of multiplication in the gather queue: four configured providers run the same query, discover almost the same files (measured pairwise URL-set overlap 0.86-0.99), and each discovery used to create its own acquisition task for byte-identical content (measured 9 700 queued rows for 3 027 distinct URLs, 3.204x, 68.8 % redundant). This capability shares the FETCH across providers while preserving every provider's attribution: one HTTP request satisfies all providers that discovered the link under identical extraction patterns, and each of them still receives its own coverage row, its own discovered-link record and its own validation tasks for any credential found. Sharing is run-scoped and deliberately not persisted — the durable queue serialises a task at enqueue time, so a beneficiary set cannot travel inside the task payload, and losing the set on restart fails open into extra work rather than into lost data. Sharing never spends another provider's rate-limit budget: the basket token count equals the number of distinct fetches.

## Requirements

### Requirement: Run-scoped fetch sharing

When `gather.share_fetch` is `on`, the search stage SHALL create at most one acquisition task per `(url_hash, patterns_hash, gather_transport)` per run. The first provider to claim a key owns the fetch; every later claimant for the same key becomes a beneficiary and SHALL NOT produce a task of its own. Identity of the extraction pattern set is an explicit precondition of sharing: a different `patterns_hash` or a different gather transport is a different key and is fetched separately.

#### Scenario: Two providers discovering the same file share one fetch

- **WHEN** provider A and provider B both discover the same URL under identical patterns and the same gather transport
- **THEN** exactly one acquisition task is created, owned by A, and B is recorded as a beneficiary

#### Scenario: Different patterns are never shared

- **WHEN** two providers discover the same URL but their `key_pattern` differs, so the `patterns_hash` differs
- **THEN** two acquisition tasks are created and neither claim suppresses the other

#### Scenario: Different gather transports are never shared

- **WHEN** the configured gather transport changes between two claims of the same URL and pattern set
- **THEN** both claims create tasks, because the fetched bytes are not known to be identical

### Requirement: Beneficiary attribution is preserved

A successful shared fetch SHALL deliver, for EVERY beneficiary including the owner: the discovered-link record, link metadata carrying that beneficiary's OWN search transport, and — when the file yields credentials — one validation task and one material result per beneficiary. The link row, its revalidation token and the gather outcome SHALL be written once by the owner, while a coverage row SHALL be written for each beneficiary so that no provider is left with a permanent coverage gap.

#### Scenario: Coverage is written for every beneficiary

- **WHEN** a shared fetch of one URL succeeds for three beneficiaries
- **THEN** the registry holds one `links` row with `visit_status='gathered_ok'` and three `link_coverage` rows, one per beneficiary provider

#### Scenario: Every beneficiary gets its own validation tasks

- **WHEN** a shared fetch extracts two credentials from one file and three providers are beneficiaries
- **THEN** six validation tasks are emitted, two per beneficiary, each naming its own provider, and three material results are recorded

#### Scenario: Metadata transport follows the beneficiary, not the owner

- **WHEN** the owner searches over the API and one beneficiary searches over the web
- **THEN** the owner's link metadata records transport `api` and that beneficiary's records `web`

### Requirement: Shared cost is paid once

Per-fetch side effects that consume a rate-limit budget or an external quota SHALL be executed once per fetched URL regardless of the beneficiary count. Sharing a fetch MUST NOT multiply per-file commit dating, repository enrichment or any other authenticated call, and MUST NOT let one provider ride on another provider's basket allocation.

#### Scenario: Dating fires once for a shared fetch

- **WHEN** a shared fetch with three beneficiaries triggers the per-file dating hook
- **THEN** the hook is invoked exactly once for that URL and the resulting date is delivered to all three beneficiaries

### Requirement: Three-mode rollout flag

Sharing SHALL be controlled by `gather.share_fetch` with values `off` (default), `shadow` and `on`. In `off` mode the ledger SHALL NOT be consulted at all and behaviour SHALL be indistinguishable from the pre-change implementation. In `shadow` mode would-be suppressions SHALL be counted while every task is still created and no beneficiary state is registered. In `on` mode suppression is enforced. A value outside the three SHALL be rejected loudly with the identical message from both validation sites and SHALL NOT be coerced.

#### Scenario: Off mode is inert

- **WHEN** the pipeline runs with `share_fetch=off` over links discovered by several providers
- **THEN** every discovery creates its own acquisition task and the ledger records no claims

#### Scenario: Shadow mode counts without acting

- **WHEN** the pipeline runs with `share_fetch=shadow` and two providers discover the same URL
- **THEN** both acquisition tasks are created, `would_suppress` increments, and `suppressed` stays zero

#### Scenario: An invalid mode is rejected identically by both validators

- **WHEN** `gather.share_fetch` is set to a value outside `off`, `shadow`, `on`
- **THEN** direct construction raises and configuration validation reports the same message `gather.share_fetch must be one of: off, shadow, on`

### Requirement: Fail-open under loss, failure and pressure

Sharing SHALL never cause work to be lost. A deferral SHALL neither consume nor discard the beneficiary set, leaving the deferred task able to satisfy every claimant when it eventually runs. A recorded gather failure SHALL discard the set so that a later discovery creates a fresh task. A beneficiary set lost to a process restart SHALL leave the queued task serving its own provider exactly as before. A ledger that has reached its tracked-key cap SHALL refuse to share rather than drop work, and every one of these paths SHALL be counted and SHALL let the run continue.

#### Scenario: A deferral preserves the beneficiary set

- **WHEN** the shared fetch is deferred by a rate-limit signal while three providers are beneficiaries
- **THEN** no gather outcome and no coverage row is written, the entry survives with all three providers, and the retried fetch still serves all of them

#### Scenario: A failed shared fetch releases its beneficiaries

- **WHEN** the shared fetch fails terminally
- **THEN** the entry is discarded, the discarded beneficiary count is reported, and no coverage row is written for any provider

#### Scenario: Restart loses the ledger but not the work

- **WHEN** a queued acquisition task completes after a process restart that emptied the ledger
- **THEN** the task serves its own provider with one link record, one gather outcome and no coverage fan-out, exactly as it would have before sharing existed

#### Scenario: Cap pressure degrades to independent fetches

- **WHEN** the ledger already tracks its maximum number of keys and a new key is claimed
- **THEN** the claim does not suppress, the caller creates its own task, and the refusal is counted

#### Scenario: Cap is configurable via gather.share_ledger_max_tracked

- **WHEN** the operator sets `gather.share_ledger_max_tracked` to a value different from the default 50 000
- **THEN** the ledger enforces that value as its tracked-key cap (and in shadow mode, its seen-set cap), and the `ledger_full` counter increments on refusal

#### Scenario: Shadow seen-set is bounded by the same cap

- **WHEN** the ledger runs in shadow mode and the seen-set reaches `share_ledger_max_tracked`
- **THEN** further unique keys increment `ledger_full` without growing the seen-set, and re-claims of already-seen keys still increment `would_suppress`

### Requirement: Sharing is observable

Per-run counters SHALL be published as a dedicated metrics surface and rendered only when the mode is not `off`, reporting at least the mode, the number of claims, the number of suppressed tasks, the number of fetches that served more than one provider, the number of extra coverage attributions delivered, the number of abandoned attributions, the number of currently tracked keys and the number of cap refusals.

#### Scenario: The share line reports a shared run

- **WHEN** a run with `share_fetch=on` suppressed 6 600 duplicate tasks and delivered 6 600 extra attributions
- **THEN** the status output contains one line reporting `mode=on`, `suppressed=6600` and `extra=6600`

#### Scenario: Nothing is rendered while sharing is off

- **WHEN** the pipeline runs with `share_fetch=off`
- **THEN** no share line appears in the status output
