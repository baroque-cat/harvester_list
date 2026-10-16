## 1. RED baseline

- [x] 1.1 Confirm no live pipeline run is active on the host (`AGENTS.md` §2: never run `pytest` while a gate is live) and that repository `logs/` and `data/` are empty
- [x] 1.2 Run the new and extended test files (`tests/test_fcd_store.py`, `test_fcd_fetch.py`, `test_fcd_config.py`, `test_fcd_pipeline_wiring.py`, `test_fcd_stage_integration.py`, `test_fcd_render.py`, plus the new cases in `test_date_extraction.py`, `test_date_flow.py`, `test_registry_migration.py`, `test_registry_core.py`, `test_gt_transport.py`) and record the failure count and each failure reason
- [x] 1.3 Verify every failure is a missing-behaviour / missing-field / missing-metric-key failure — no import errors, no fixture errors, no collection errors — and correct `tests.md` if any recorded RED reason differs from the observed one

## 2. Live-captured fixtures for external contracts (early, per schema rule)

- [x] 2.1 Re-run a minimal staging probe against GitHub for the two dating endpoints (REST `GET /repos/{o}/{r}/commits?path=&per_page=1&sha=` and batched `POST /graphql` with `Commit.history(first:1,path:)`), using credentials read at runtime and never written to disk, and save raw responses under `tests/fixtures/file_dating/`
- [x] 2.2 Give every fixture a provenance header naming endpoint, capture date, auth mode and HTTP status; label any synthetic vector explicitly as synthetic (schema rule: synthesized expectations are not evidence)
- [x] 2.3 Add the counter-example fixtures captured 2026-10-14: `... on Blob { committedDate }` returning `200` **with** `errors` (the field does not exist), and the session-authenticated blob page carrying zero date markers — both are pinned so a future refactor cannot silently re-assume them
- [x] 2.4 Record the etag divergence fixture: the same file returned `"0e402cf4…"` (64 hex) from `raw.githubusercontent.com` and `"fdeb21a0…"` (40 hex) from the contents API, which is why design D6 forbids cross-transport token replay
- [x] 2.5 Scan every committed fixture for credential **values** taken from the live config (not by filename glob — lesson И8) and confirm zero matches before the files enter the index

## 3. Registry schema migration (link-registry-S3, S4, S5, S10, S11)

- [x] 3.1 Raise `SCHEMA_VERSION` 4 → 5 in `storage/registry.py` and add `links.etag TEXT` plus `links.etag_transport TEXT` to the bootstrap DDL (additive only, design D10)
- [x] 3.2 Add the `file_meta` table to the bootstrap DDL: `owner, repo, path, resolved_sha, committed_date, etag, fetched_at, gone`, `PRIMARY KEY(owner, repo, path)` (design D2)
- [x] 3.3 Extend `_migrate_schema` with idempotent `PRAGMA table_info` guards for both the new columns and the new table, following the existing `keys.last_seen_ts` pattern
- [x] 3.4 Prove migration against a **copy** of the run-№4 registry at `user_version = 4` (3 055 links): row counts identical, all prior values intact, version stamped 5, reopening changes nothing
- [x] 3.5 Store and replace the revalidation token together with its transport; a successful gather with no token records normally and stores nothing

## 4. Per-file evidence store (file-commit-dating-S14, S16, S17, S18, S19)

- [x] 4.1 Create `storage/file_meta.py` modelled on `storage/repo_meta.py`: entry dataclass, TTL freshness check, `gone` suppression, conditional-revalidation token handling
- [x] 4.2 Implement cache lookup keyed `(owner, repo, path)` with `resolved_sha` comparison; a sha mismatch is a cache miss, not a hit (design D2)
- [x] 4.3 Implement `gone` marking on not-found outcomes so later runs issue no request for that target
- [x] 4.4 Keep the store fail-open: an absent or corrupt cache degrades to "no evidence" and never raises into a worker loop

## 5. REST dating fetcher and typed failure classification (file-commit-dating-S1, S2, S4, S5, S13, S15)

- [x] 5.1 Add the `commits?path=&per_page=1&sha=` fetch to `search/client.py`, reading `committer.date` with `author.date` fallback, normalising to UTC epoch seconds
- [x] 5.2 Treat a non-list, empty-list, missing-field or unparseable response as **no evidence** plus an absence counter — never as a date, and never as a fabricated default
- [x] 5.3 Route refusals through the existing typed signals so bounded wait, deferral and the retry budget behave exactly as they do for search; a deferral must not consume an attempt
- [x] 5.4 Accept a commit date earlier than repository creation without special-casing it (measured: 2011-06-30 file date against 2015-04-11 `created_at`, imported SVN history)

## 6. Batched GraphQL transport and its own basket (file-commit-dating-S6, S8, S9, S12)

- [x] 6.1 Add a `github_graphql` service type and an explicit override in `_service()` so `api.github.com/graphql` does **not** fall into the `github_api` basket (design D4; `_service()` matches `api.github.com` first)
- [x] 6.2 Add `ratelimits.github_graphql` to the schema, loader, validator and defaults, remembering that a non-empty `ratelimits` section suppresses schema defaults and an unknown bucket silently allows everything
- [x] 6.3 Implement the aliased batch query pinned to the captured fixture; attribute each alias result to its own file and let one failing alias leave the others intact
- [x] 6.4 Classify a `200`-with-`errors` body into the existing typed signals: recognised allowance exhaustion → deferral, recognised permission/absent → permanent, unrecognised → transient; never return an empty success (design D5)
- [x] 6.5 Read the reported cost from the response's rate-limit field and expose it as a counter, so the gate can price batch sizes instead of assuming

## 7. Configuration surface (file-commit-dating-S7, S22)

- [x] 7.1 Add `FileDatingConfig` to `config/schemas.py`: `enabled: bool = False`, `transport: str = "rest"`, `scope: str = "keyed"`, `ttl_hours: float = 24.0`, `max_requests_per_run: int`, `graphql_batch_size: int` with an enforced ceiling
- [x] 7.2 Validate strictly in `__post_init__` — `isinstance(x, bool)` with **no coercion** (`bool("false") is True`), positive numerics, known transport and scope names
- [x] 7.3 Mirror every message verbatim in `config/validator.py` and parse the section in `config/loader.py` with defaults matching the schema
- [x] 7.4 Confirm an absent `file_dating:` section resolves to fully inert defaults

## 8. Wiring and inertness (file-commit-dating-S20, S11)

- [x] 8.1 Construct the dating components in `manager/pipeline.py` next to the enrichment wiring, passing `auth=get_auth_provider()`, the shared client and `enabled=config.file_dating.enabled`
- [x] 8.2 Guarantee that with the flag off no request is issued, no date column is written by this subsystem, and run output is byte-for-byte what it was before
- [x] 8.3 Prove search figures are unperturbed: with dating on, search request count and admitted-query metrics stay within the configured dating budget

## 9. Stage integration, scope and monotonic delivery (file-commit-dating-S3, S10, S21, S23, S24; date-extraction-S7, S9, S12)

- [x] 9.1 Implement scope selection `keyed | gathered | discovered`, default `keyed` — submit only files where a credential was found unless widened (design D8)
- [x] 9.2 Enforce `max_requests_per_run`: on reaching the ceiling skip further dating, increment a budget-stopped counter, and still complete the run successfully
- [x] 9.3 Propagate results through the existing stage-output channel into `links.file_commit_date`; never write a repository-level `pushed_at` into that column
- [x] 9.4 Change the date UPDATEs in `storage/registry.py` from `COALESCE(?, col)` to a monotonic `MAX` form for `file_commit_date` and `repo_pushed_at`, so a re-propagated cached value cannot move a date backwards (design D7)
- [x] 9.5 Verify harvest neutrality: extracted values, shard NDJSON records and key rows identical with dating off and on
- [x] 9.6 Confirm a web-discovered link is dateable through the API path, and that markup silence does not block API-sourced evidence

## 10. Conditional re-gather (gather-transport-S30 … S35)

- [x] 10.1 Send `If-None-Match` on the plain-content path only when the link is `gathered_ok`, holds a token captured **on that same transport**, and the TTL has expired (design D6)
- [x] 10.2 Treat `304` as a successful gather outcome with no payload: skip extraction, append no shard record, produce no key rows, preserve prior findings, advance `last_seen_ts`, increment `gather_304`
- [x] 10.3 Refuse cross-transport token replay and replace a stored token with one from the transport actually used
- [x] 10.4 Send no precondition for a never-gathered link; store any token returned
- [x] 10.5 Fall through to the ordinary full-fetch path on any answer other than `304`, and classify an unusable conditional answer by the existing refusal rules so nothing is silently lost
- [x] 10.6 Keep the conditional path **anonymous** — the probe established that neither a PAT nor a session cookie changes a `raw` response

## 11. Observability (file-commit-dating-S25, S26; date-extraction-S6)

- [x] 11.1 Add the counters from design D12 (`file_dating_requests`, `_304s`, `_filled`, `_absent`, `_batches`, `_graphql_cost`, `_refusals`, `_budget_stopped`, `gather_304`) — additive only, nothing renamed (lesson И7)
- [x] 11.2 Render them on the status surface in the existing allowlist style next to the enrichment figures, including the fill rate over the dated population
- [x] 11.3 Make an enabled run with zero usable dates visibly distinct from a disabled run (requests and absences non-zero, fill rate 0.0)
- [x] 11.4 Pin the 2026-10-14 session-authenticated blob page as a NULL-yielding fixture in `tests/test_date_extraction.py`

## 12. Suite, documentation and examples

- [x] 12.1 Drive every automated scenario in `tests.md` to GREEN and flip its row from RED
- [x] 12.2 Run the full suite three times consecutively; require an identical pass count, empty repository `logs/` after each run (§4 invariant) and untouched `data/`
- [x] 12.3 Add the `file_dating:` section and `ratelimits.github_graphql` to `examples/config-full.yaml` with comments citing the measured prices and the rollback flag
- [x] 12.4 Update `README.md` (new section, metrics paragraph, rollback statement) and `docs/specs/date_extraction.md` (authority order; the HTML source documented as measured-dead in both credential modes)
- [x] 12.5 Record the reconciliation between the expected and actual suite size, and any deviation from these tasks, in `verification.md`

## 13. Field verification (FCD-F1 … FCD-F6)

- [x] 13.1 Prepare a run root under `/var/tmp/opencode-fcd-gate_<TS>` (never `/tmp`), `cd` into it before launching so `global.workspace` and `logs/` resolve there, and reference the live `config.yaml` **in place** rather than copying it (a `.bak` copy once survived a glob-based cleanup check)
- [x] 13.2 Arm A — `file_dating.enabled: false`, 300 s production-shaped run wrapped in `timeout -k 30 600`, launched from a script file; capture the final status block, queue states and harvest totals as the baseline
- [x] 13.3 Arm B — `enabled: true, scope: keyed, transport: rest`; measure `file_commit_date` fill rate over the dated population, requests, absences, refusals, `degraded` runs, and search throughput versus arm A
- [x] 13.4 Arm C — `transport: graphql`; measure batches, the reported cost at batch sizes 4 / 20 / 50, and verify by header that the `code_search` allowance is untouched (answers the open question in design D3)
- [x] 13.5 Measure conditional re-gather in the field: `gather_304` share, bytes saved against arm A, and confirm no findings were lost on unchanged files
- [x] 13.6 Migrate a **copy** of the run-№4 registry (`user_version = 4`, 3 055 links) with the shipped build and compare row counts, values and query plans
- [x] 13.7 Hygiene: distil evidence into small text files before deleting anything, scan all artefacts by credential **value**, destroy any arm config inside the script, verify no leftover config-shaped files, and confirm `df -h /tmp` returned to its pre-task level
- [x] 13.8 Sequence the gate and the suite — never concurrently — and record the clean post-gate suite run in `verification.md`

## 14. Closure

- [x] 14.1 Write `verification.md`: summary table, RED baseline, GREEN reconciliation, scenario-to-evidence mapping for all 35 new automated cases plus the 13 re-pinned ones, the field-gate arms with their measured figures, deviations, and hygiene/security attestation
- [x] 14.2 Sync the delta specs into `openspec/specs/` and run `openspec validate --specs --strict` plus `openspec validate add-file-commit-dating --type change --strict`
- [x] 14.3 Update `plan.md`: close register positions P1-adjacent dating items, record the measured GraphQL cost scaling, and note any position whose status this change alters; add the corresponding lesson to `plan_races.md` if a measurement refuted an expectation
- [x] 14.4 Confirm `openspec instructions apply` reports all tasks done, then archive with the correct current date and commit in English following the repository's conventional-commit house style
