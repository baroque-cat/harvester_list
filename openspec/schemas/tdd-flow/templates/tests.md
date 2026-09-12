# Test Plan

<!-- Derived mechanically from the delta specs under specs/. One spec scenario
     = exactly one automated test case, or one Manual entry with a reason.
     Scenario IDs (<capability>-S<n>) are stable: never renumber, append only. -->

## Traceability

| Scenario ID | Spec | Requirement | Scenario | Test file | Status |
|-------------|------|-------------|----------|-----------|--------|
| `<capability>-S1` | `specs/<capability-path>/spec.md` | <requirement name> | <scenario name> | <test file path> | RED |

<!-- Status values: RED (written, failing) -> GREEN (passing). Manual entries
     keep status MANUAL. -->

## Automated

### File: <path/to/file.test.ts>

Describe: <describe block name>

<!-- If the module under test does not exist yet, say so here: the import
     failure is the expected RED state. -->

- [ ] `<capability>-S1` — it("<scenario name>") <!-- WHEN <trigger> THEN <observable outcome> -->
- [ ] `<capability>-S2` — it("<scenario name>") <!-- WHEN <trigger> THEN <observable outcome> -->

## Manual

<!-- Scenarios that cannot be automated (visual checks, third-party flows,
     hardware, ...). One line per scenario ID with the reason. -->

- `<capability>-S3` — <why this needs human/E2E verification>
