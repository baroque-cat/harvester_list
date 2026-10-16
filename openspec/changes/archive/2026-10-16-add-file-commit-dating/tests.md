# Test Plan

Scenario IDs are assigned per capability in delta document order and are stable (append-only).
`gather-transport` continues its own global S-numbering (main spec ends at S29), so its new
scenarios are `gather-transport-S30`…`-S35` rather than restarting at S1.

## Traceability

| Scenario ID | Spec | Requirement | Scenario | Test file | Status |
|-------------|------|-------------|----------|-----------|--------|
| `file-commit-dating-S1` | `specs/file-commit-dating/spec.md` | Per-file commit date is acquired opportunistically and never fabricated | Date captured for a single file | `tests/test_fcd_fetch.py` | GREEN |
| `file-commit-dating-S2` | `specs/file-commit-dating/spec.md` | Per-file commit date is acquired opportunistically and never fabricated | Response without a usable date degrades to no evidence | `tests/test_fcd_fetch.py` | GREEN |
| `file-commit-dating-S3` | `specs/file-commit-dating/spec.md` | Per-file commit date is acquired opportunistically and never fabricated | Repository push date is never substituted for a file date | `tests/test_fcd_stage_integration.py` | GREEN |
| `file-commit-dating-S4` | `specs/file-commit-dating/spec.md` | Per-file commit date is acquired opportunistically and never fabricated | A commit date may predate repository creation | `tests/test_fcd_fetch.py` | GREEN |
| `file-commit-dating-S5` | `specs/file-commit-dating/spec.md` | Two interchangeable acquisition transports with bounded batching | REST form dates one file per request | `tests/test_fcd_fetch.py` | GREEN |
| `file-commit-dating-S6` | `specs/file-commit-dating/spec.md` | Two interchangeable acquisition transports with bounded batching | Batched form dates several files in one request | `tests/test_fcd_fetch.py` | GREEN |
| `file-commit-dating-S7` | `specs/file-commit-dating/spec.md` | Two interchangeable acquisition transports with bounded batching | Batch ceiling is enforced by configuration validation | `tests/test_fcd_config.py` | GREEN |
| `file-commit-dating-S8` | `specs/file-commit-dating/spec.md` | Two interchangeable acquisition transports with bounded batching | Transport switch preserves recorded semantics | `tests/test_fcd_fetch.py` | GREEN |
| `file-commit-dating-S9` | `specs/file-commit-dating/spec.md` | Dating cannot starve search | Batched dating does not consume the code-search budget | `tests/test_fcd_fetch.py` | GREEN |
| `file-commit-dating-S10` | `specs/file-commit-dating/spec.md` | Dating cannot starve search | Per-run ceiling stops dating quietly | `tests/test_fcd_stage_integration.py` | GREEN |
| `file-commit-dating-S11` | `specs/file-commit-dating/spec.md` | Dating cannot starve search | Search throughput is unchanged when dating is disabled | `tests/test_fcd_pipeline_wiring.py` | GREEN |
| `file-commit-dating-S12` | `specs/file-commit-dating/spec.md` | Failures are typed signals, including errors carried inside a successful status | Errors inside a successful status are classified, not swallowed | `tests/test_fcd_fetch.py` | GREEN |
| `file-commit-dating-S13` | `specs/file-commit-dating/spec.md` | Failures are typed signals, including errors carried inside a successful status | Point or quota exhaustion defers without burning attempts | `tests/test_fcd_fetch.py` | GREEN |
| `file-commit-dating-S14` | `specs/file-commit-dating/spec.md` | Failures are typed signals, including errors carried inside a successful status | Absent repository or path drops loudly once and suppresses retries | `tests/test_fcd_store.py` | GREEN |
| `file-commit-dating-S15` | `specs/file-commit-dating/spec.md` | Failures are typed signals, including errors carried inside a successful status | An unrecognised error cause stays transient | `tests/test_fcd_fetch.py` | GREEN |
| `file-commit-dating-S16` | `specs/file-commit-dating/spec.md` | Cached evidence has a bounded lifetime and suppresses dead targets | Fresh cache entry issues no request | `tests/test_fcd_store.py` | GREEN |
| `file-commit-dating-S17` | `specs/file-commit-dating/spec.md` | Cached evidence has a bounded lifetime and suppresses dead targets | Expired entry is revalidated conditionally | `tests/test_fcd_store.py` | GREEN |
| `file-commit-dating-S18` | `specs/file-commit-dating/spec.md` | Cached evidence has a bounded lifetime and suppresses dead targets | Gone target suppresses further attempts | `tests/test_fcd_store.py` | GREEN |
| `file-commit-dating-S19` | `specs/file-commit-dating/spec.md` | Cached evidence has a bounded lifetime and suppresses dead targets | A different observed revision invalidates the cached entry | `tests/test_fcd_store.py` | GREEN |
| `file-commit-dating-S20` | `specs/file-commit-dating/spec.md` | Inert until explicitly enabled, rollback by configuration flag | Disabled by default is indistinguishable from before the change | `tests/test_fcd_pipeline_wiring.py` | GREEN |
| `file-commit-dating-S21` | `specs/file-commit-dating/spec.md` | Inert until explicitly enabled, rollback by configuration flag | Enabling does not alter harvest output | `tests/test_fcd_stage_integration.py` | GREEN |
| `file-commit-dating-S22` | `specs/file-commit-dating/spec.md` | Inert until explicitly enabled, rollback by configuration flag | Invalid configuration fails loudly | `tests/test_fcd_config.py` | GREEN |
| `file-commit-dating-S23` | `specs/file-commit-dating/spec.md` | Scope of dating is configurable and defaults to files with findings | Default scope dates only files where a credential was found | `tests/test_fcd_stage_integration.py` | GREEN |
| `file-commit-dating-S24` | `specs/file-commit-dating/spec.md` | Scope of dating is configurable and defaults to files with findings | Widening scope increases the dated population | `tests/test_fcd_stage_integration.py` | GREEN |
| `file-commit-dating-S25` | `specs/file-commit-dating/spec.md` | Dating coverage is observable per run | Counters rendered on the status surface | `tests/test_fcd_render.py` | GREEN |
| `file-commit-dating-S26` | `specs/file-commit-dating/spec.md` | Dating coverage is observable per run | Zero-evidence run is distinguishable from a disabled run | `tests/test_fcd_render.py` | GREEN |
| `date-extraction-S1` | `specs/date-extraction/spec.md` | File commit date from gathered blob pages | Date captured from blob HTML | `tests/test_date_extraction.py` | GREEN (pre-existing) |
| `date-extraction-S2` | `specs/date-extraction/spec.md` | File commit date from gathered blob pages | Layout without markers yields NULL safely | `tests/test_date_extraction.py` | GREEN (pre-existing) |
| `date-extraction-S3` | `specs/date-extraction/spec.md` | File commit date from gathered blob pages | Live-captured blob page yields NULL safely | `tests/test_date_extraction.py` | GREEN (pre-existing) |
| `date-extraction-S4` | `specs/date-extraction/spec.md` | File commit date from gathered blob pages | Plain-content transport yields NULL without a parse attempt | `tests/test_date_extraction.py` | GREEN (pre-existing) |
| `date-extraction-S5` | `specs/date-extraction/spec.md` | File commit date from gathered blob pages | Transport switch does not worsen the fill rate | `tests/test_date_extraction.py` | GREEN (pre-existing) |
| `date-extraction-S6` | `specs/date-extraction/spec.md` | File commit date from gathered blob pages | Session-authenticated page also yields NULL | `tests/test_date_extraction.py` | GREEN |
| `date-extraction-S7` | `specs/date-extraction/spec.md` | File commit date from gathered blob pages | Markup silence does not block API-sourced evidence | `tests/test_date_flow.py` | GREEN |
| `date-extraction-S8` | `specs/date-extraction/spec.md` | Transport asymmetry enforced | Web search produces no dates | `tests/test_date_extraction.py` | GREEN (pre-existing) |
| `date-extraction-S9` | `specs/date-extraction/spec.md` | Transport asymmetry enforced | Web-discovered link is still dateable | `tests/test_date_flow.py` | GREEN |
| `date-extraction-S10` | `specs/date-extraction/spec.md` | Non-regressing delivery into the registry | Known date survives later NULL observation | `tests/test_date_flow.py` | GREEN (pre-existing) |
| `date-extraction-S11` | `specs/date-extraction/spec.md` | Non-regressing delivery into the registry | Fresher date replaces older | `tests/test_date_flow.py` | GREEN (pre-existing) |
| `date-extraction-S12` | `specs/date-extraction/spec.md` | Non-regressing delivery into the registry | Stale cached date does not regress a newer recorded date | `tests/test_date_flow.py` | GREEN |
| `link-registry-S1` | `specs/link-registry/spec.md` | Persistent global registry storage | Registry persists across restarts | `tests/test_registry_core.py` | GREEN (pre-existing) |
| `link-registry-S2` | `specs/link-registry/spec.md` | Persistent global registry storage | Registry is shared across provider-set changes | `tests/test_registry_core.py` | GREEN (pre-existing) |
| `link-registry-S3` | `specs/link-registry/spec.md` | Persistent global registry storage | Schema bootstrap on empty workspace | `tests/test_registry_core.py` | RED (table list extended) |
| `link-registry-S4` | `specs/link-registry/spec.md` | Persistent global registry storage | Database at an earlier schema version migrates in place | `tests/test_registry_migration.py` | GREEN |
| `link-registry-S5` | `specs/link-registry/spec.md` | Persistent global registry storage | Schema migration is idempotent on reopen | `tests/test_registry_migration.py` | GREEN |
| `link-registry-S6` | `specs/link-registry/spec.md` | Persistent global registry storage | Older build reads a newer database | — | MANUAL |
| `link-registry-S7` | `specs/link-registry/spec.md` | Link lifecycle recording | First discovery inserts | `tests/test_registry_core.py` | GREEN (pre-existing) |
| `link-registry-S8` | `specs/link-registry/spec.md` | Link lifecycle recording | Re-discovery updates only recency fields | `tests/test_registry_core.py` | GREEN (pre-existing) |
| `link-registry-S9` | `specs/link-registry/spec.md` | Link lifecycle recording | Gather outcome is recorded | `tests/test_registry_core.py` | GREEN (pre-existing) |
| `link-registry-S10` | `specs/link-registry/spec.md` | Link lifecycle recording | Revalidation token is stored with its transport | `tests/test_registry_core.py` | GREEN |
| `link-registry-S11` | `specs/link-registry/spec.md` | Link lifecycle recording | Absent token leaves the row usable | `tests/test_registry_core.py` | GREEN |
| `gather-transport-S30` | `specs/gather-transport/spec.md` | Unchanged content is not downloaded again | S30 unchanged content short-circuits the download | `tests/test_gt_transport.py` | GREEN |
| `gather-transport-S31` | `specs/gather-transport/spec.md` | Unchanged content is not downloaded again | S31 unchanged content preserves prior findings and advances recency | `tests/test_gt_transport.py` | GREEN |
| `gather-transport-S32` | `specs/gather-transport/spec.md` | Unchanged content is not downloaded again | S32 a token is never replayed across transports | `tests/test_gt_transport.py` | GREEN |
| `gather-transport-S33` | `specs/gather-transport/spec.md` | Unchanged content is not downloaded again | S33 first gathering sends no conditional header | `tests/test_gt_transport.py` | GREEN |
| `gather-transport-S34` | `specs/gather-transport/spec.md` | Unchanged content is not downloaded again | S34 changed content falls through to a full fetch | `tests/test_gt_transport.py` | GREEN |
| `gather-transport-S35` | `specs/gather-transport/spec.md` | Unchanged content is not downloaded again | S35 an unusable revalidation answer degrades to a full fetch | `tests/test_gt_transport.py` | GREEN |

New automated cases: **35**. Pre-existing cases re-pinned by this delta: **13**. Manual: **1**.

## Automated

### File: `tests/test_fcd_store.py`

Describe: `storage/file_meta.py` — the per-file evidence cache: key `(owner, repo, path)` with `resolved_sha`, TTL freshness, conditional-revalidation token, `gone` suppression, revision invalidation. Fixtures use a temporary SQLite registry; no network.

- `file-commit-dating-S14` absent repository or path marks the entry gone and suppresses retries
- `file-commit-dating-S16` fresh entry issues no request
- `file-commit-dating-S17` expired entry is revalidated conditionally and a `304` preserves the stored value
- `file-commit-dating-S18` gone target suppresses further attempts
- `file-commit-dating-S19` a different observed revision invalidates the cached entry

### File: `tests/test_fcd_fetch.py`

Describe: `search/client.py` dating fetchers — REST `commits?path=` and batched GraphQL — plus classification of errors carried inside a `200`. All external shapes come from **live-captured fixtures** (see group 2); synthetic vectors are labelled as such.

- `file-commit-dating-S1` committer date captured and normalised to UTC epoch seconds
- `file-commit-dating-S2` well-formed response without a usable date yields no evidence and counts an absence
- `file-commit-dating-S4` commit date earlier than repository creation is recorded as-is
- `file-commit-dating-S5` REST form issues exactly one request per path
- `file-commit-dating-S6` batched form resolves several aliases in one request; one failing alias does not discard the others
- `file-commit-dating-S8` both transports yield the same recorded value for the same commit
- `file-commit-dating-S9` batched traffic does not draw from the code-search budget
- `file-commit-dating-S12` success status carrying `errors` is classified, never returned as empty success
- `file-commit-dating-S13` allowance exhaustion defers with a bounded wait and does not consume the retry budget
- `file-commit-dating-S15` unrecognised error cause is treated as transient

### File: `tests/test_fcd_config.py`

Describe: `config/schemas.py`, `config/loader.py`, `config/validator.py` — `FileDatingConfig` (`enabled`, `transport`, `scope`, `ttl_hours`, `max_requests_per_run`, `graphql_batch_size`) and the `github_graphql` basket. Strict `isinstance(bool)` checks, **no coercion**, mirrored messages in both validation sites.

- `file-commit-dating-S7` batch size outside the supported bound fails loading loudly
- `file-commit-dating-S22` non-boolean flag or non-positive numeric fails loudly from both a loaded file and direct construction

### File: `tests/test_fcd_pipeline_wiring.py`

Describe: `manager/pipeline.py` — construction, inertness, and the guarantee that enabling dating does not perturb search.

- `file-commit-dating-S11` search request count and admitted-query figures are unaffected by dating traffic beyond its budget
- `file-commit-dating-S20` absent configuration section behaves exactly as before the capability existed

### File: `tests/test_fcd_stage_integration.py`

Describe: `stage/definition.py` — scope selection, propagation into `links.file_commit_date`, per-run budget stop, and harvest-output neutrality.

- `file-commit-dating-S3` repository push date is never written into `file_commit_date`
- `file-commit-dating-S10` reaching the per-run ceiling skips further dating, counts the stop, and the run still succeeds
- `file-commit-dating-S21` extracted values, shard records and key rows are identical with dating off and on
- `file-commit-dating-S23` default scope submits only files where a credential was found
- `file-commit-dating-S24` widening scope grows the dated population while the ceiling still bounds requests

### File: `tests/test_fcd_render.py`

Describe: `state/display.py` — rendered dating figures, allowlist style consistent with `_format_refine_metrics_line`.

- `file-commit-dating-S25` counters and fill rate appear in operator status output
- `file-commit-dating-S26` an enabled run with zero usable dates is visibly distinct from a disabled run

### File: `tests/test_date_extraction.py` (extended)

Describe: existing date-extraction suite; one new case pinned to the 2026-10-14 session-authenticated capture.

- `date-extraction-S6` live-captured authenticated blob page (HTTP 200, ~190–240 KB) yields NULL from markup

### File: `tests/test_date_flow.py` (extended)

Describe: end-to-end date delivery into the registry.

- `date-extraction-S7` markup silence does not block API-sourced evidence for the same path
- `date-extraction-S9` a web-discovered link is dateable through the per-file API path
- `date-extraction-S12` a stale propagated value never moves a recorded date backwards

### File: `tests/test_registry_migration.py` (extended)

Describe: additive migration to schema version 5.

- `link-registry-S4` a database at version 4 gains `links.etag` and `file_meta`, keeps every row, and stamps version 5
- `link-registry-S5` reopening an already-migrated database changes nothing and raises nothing

### File: `tests/test_registry_core.py` (extended)

Describe: registry bootstrap and link lifecycle.

- `link-registry-S3` bootstrap on an empty workspace creates the full table set including `file_meta`
- `link-registry-S10` a revalidation token is stored together with the transport that produced it and replaces a foreign one
- `link-registry-S11` a successful gather with no token records normally and stores nothing

### File: `tests/test_gt_transport.py` (extended)

Describe: conditional re-gather on the plain-content transport.

- `gather-transport-S30` unchanged answer transfers no payload, completes as success, increments the counter
- `gather-transport-S31` unchanged answer preserves prior findings and shard records, advances recency
- `gather-transport-S32` a token captured on another transport is not replayed; the fetch is unconditional
- `gather-transport-S33` a never-gathered link sends no precondition and stores any token returned
- `gather-transport-S34` changed content is processed exactly like an unconditional fetch
- `gather-transport-S35` an unusable conditional answer is classified by the ordinary rules and loses nothing

## Manual

| ID | Check | Why it cannot be automated |
|----|-------|---------------------------|
| `link-registry-S6` | A build predating schema version 5 opens a version-5 database and reads/writes the columns it knows | Requires executing a previous build; approximated in CI by `link-registry-S4`/`S5` (additive-only migration) but not equivalent |
| `FCD-F1` | Live arm A: `file_dating.enabled: false` — production-shaped 300 s run, baseline for every figure below | Needs real GitHub traffic and the production credential pool |
| `FCD-F2` | Live arm B: `enabled: true, scope: keyed, transport: rest` — fill rate of `file_commit_date`, requests, refusals, `degraded` runs, search throughput versus arm A | Same |
| `FCD-F3` | Live arm C: `transport: graphql` — batches issued, measured `rateLimit.cost` at batch sizes 4 / 20 / 50, confirmation that the `code_search` allowance is untouched | Same; cost scaling is the open question design D3 defers to the gate |
| `FCD-F4` | Conditional re-gather in the field — `gather_304` share, bytes saved against arm A, and verification that no findings were lost on unchanged files | Same |
| `FCD-F5` | Registry migration on a **copy** of the run-№4 registry (`user_version = 4`, 3 055 links) — row counts identical after migration, query plans unchanged | Requires the durable artefact from a previous live gate |
| `FCD-F6` | Hygiene and security scan of all gate evidence by credential **value** (not filename glob), `/tmp` returned to its pre-task level, no leftover config-shaped copies | Operational, per `AGENTS.md` §1/§3 and lesson И8 |
