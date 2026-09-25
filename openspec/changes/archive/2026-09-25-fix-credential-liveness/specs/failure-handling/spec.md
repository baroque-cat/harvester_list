## MODIFIED Requirements

### Requirement: Existing protections are preserved
Credential-cooldown mechanics SHALL remain intact where they are proven: rate-limit signals (HTTP 429/403 and soft-block content) keep rotating credentials through the existing bounded-backoff channel (60→900s escalation) and MUST NOT be double-handled as generic transient failures; NDJSON shard formats and `recover_tasks()` replay semantics remain unchanged. The all-credentials-cooling pool reaction is amended by `credential-liveness`: the pool waits only up to the configured credential budget (`credential_liveness.max_wait_s`, cross-validated below the durable-queue visibility timeout) and then DEFERS the task through the durable deferral seam — attempts unchanged, counted, nothing written to the registry — rather than blocking indefinitely inside a claimed task. A task SHALL never be dropped or completed-empty by this mechanism; the ultimate ceiling on deferral cycling remains the queue's existing `max_age_hours` purge with its loud accounting.

#### Scenario: Cooldown rotation unchanged under the new contract
- **WHEN** a credential hits a rate limit during search in strict mode
- **THEN** the rotation loop exchanges the credential and proceeds exactly as before this change, with backoff escalating per the existing schedule, and the event is not counted as a failure-empty requeue

#### Scenario: Full-pool cooldown defers, never drops
- **WHEN** every pooled credential is cooling down while a search task needs one
- **THEN** the task waits at most the configured credential budget for the earliest cooldown release; if a credential frees within the budget the task proceeds with it, otherwise the task is deferred (attempts/created_at/dedup identity preserved, `tasks_deferred` counted, stage paused within the bounded defer cap) and is never dropped, never completed-empty, and never slept out inside its queue claim beyond the budget

#### Scenario: Restart recovery unaffected
- **WHEN** the process restarts after runs that included transient failures
- **THEN** queue-file and shard-based recovery replays the same task sets as before this change (formats untouched)
