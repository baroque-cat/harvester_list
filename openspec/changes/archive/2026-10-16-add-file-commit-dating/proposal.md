## Why

The registry can already answer *"have we seen this file before"* (`links.first_seen_ts`, fill rate 1.0000 in the 2026-10-01 control run), but it cannot answer *"is this leak new or old"*, which is what the operator needs in order to (a) mark repositories that have leaked keys so other tools can later walk all their files and commits, (b) tell a fresh leak from a stale one on a re-run under the same or a different pattern, and (c) sort — and eventually stop — searching once only old repositories keep appearing. Both implemented date sources are dead by measurement, not by assumption: `/search/code` returns a trimmed `repository` object with no date fields, and the blob HTML page carries no date markup in **either** credential mode. Meanwhile a live probe established that GitHub does serve per-file commit dates over REST and over GraphQL, and that conditional requests make re-checking nearly free — none of which the code uses.

Live-captured evidence (all probes 2026-10-14, four target files across four distinct owners taken from the surviving gather queue of run `/var/tmp/opencode-p19-control_20261001T043139`; full matrix in `plan.md` II.21):

| Endpoint | Auth mode | Captured result |
|---|---|---|
| `GET /search/code?q=/sk-[a-zA-Z0-9]{32}/` | PAT | `200`; `item[0]` keys `[git_url, html_url, name, path, repository, score, sha, url]`; `repository.pushed_at`, `.updated_at`, `.created_at`, `.size` **all absent** |
| `GET /repos/{o}/{r}/commits?path={file}&per_page=1&sha={sha}` | PAT | `200` ×4; `committer.date` = `2021-05-29T14:24:14Z`, `2018-06-04T12:59:03Z`, `2011-06-30T19:18:15Z`, `2021-07-09T18:30:09Z`; 1 `core` unit each |
| `POST /graphql` `{… object(expression:"<sha>"){... on Commit{history(first:1,path:"<file>"){nodes{committedDate}}}}}` batched ×4 aliases | PAT | `200`; **`rateLimit.cost = 1`**, `remaining 4993`; dates byte-identical to the REST form |
| `POST /graphql` `... on Blob { committedDate }` | PAT | `200` with `errors` — field does not exist on `Blob` |
| `GET /repos/{o}/{r}` | PAT | `200` ×4; `pushed_at` present, plus `last-modified` and `etag` headers |
| blob HTML page `github.com/{o}/{r}/blob/{sha}/{path}` | anonymous | `200, 503, 503, 200` — stochastic refusal; `<relative-time`=0, `datetime=`=0, `committedDate`=0, `"pushedAt"`=0, `latestCommit`=0 |
| same | session cookie | `200` ×4 (192–239 KB); **the same five markers all zero** |
| `raw.githubusercontent.com/{o}/{r}/{sha}/{path}` | anonymous / PAT / session | `200` ×4 in all three modes, byte-identical bodies equal to the file size, identical `etag`, **no `last-modified` header in any mode**, no `x-ratelimit-*` at all |
| same + `If-None-Match: <etag>` | anonymous | **`304`, empty body**, 150–182 ms |
| `GET /repos/{o}/{r}/contents/{path}?ref={sha}` + `If-None-Match` | PAT | **`304`, empty body, `x-ratelimit-remaining` unchanged** (4994→4994, 4991→4991, 4988→4988, 4985→4985) — a conditional request consumes zero quota |
| `GET /settings/profile` | each of the 4 configured sessions | `200` ×4 — all web sessions alive |

Two consequences drive this change. First, repository-level `pushed_at` is **not** a proxy for file freshness: measured gaps against the file's own commit date were 11, 35, 311 and **4 561 days** (12.5 years), and the gap does not correlate with commit count (44 commits produced the 12.5-year gap, 110 commits produced 311 days). Second, cost is bounded by the *basket*, not the quota: 3 029 distinct files sit in 2 524 distinct repositories (1.20 files/repo), so at `ratelimits.github_api base_rate 0.15` ≈ 9 req/min shared with search, full file-level dating would take ~5.6 h and a single 300 s run reaches ~1.8 % of repositories. Reducing the **number of requests** (batching) and **eliminating unchanged ones** (conditional requests) is therefore the substance of the work, not the fetching itself.

## What Changes

- **New per-file dating mechanism ("source C")**: fetch the last commit date of a specific file via `GET /repos/{o}/{r}/commits?path={file}&per_page=1`, cache it in a new `file_meta` store modelled on the existing `RepoMetaStore` (ETag, TTL, `gone` suppression), and propagate it into the already-existing `links.file_commit_date` column. Inert until an operator opts in (`file_dating.enabled: false` by default), following the house doctrine that a new subsystem is byte-for-byte inert until enabled.
- **Batched GraphQL transport for the same evidence**: `file_dating.transport: rest | graphql`, where `graphql` issues one `Commit.history(first:1, path:)` query with N aliases. Measured cost was 1 point for 4 files across 4 repositories against 4 `core` units for the REST form. GraphQL rides a **separate basket** (`github_graphql`) so it does not compete with code search for the 9/min `github_api` budget; the currently idle `graphql` pool (5000/h, measured `remaining 5000/5000`) is used instead.
- **"HTTP 200 with errors" becomes a typed signal.** GraphQL reports schema and permission problems inside a `200` body. Today that class of response is invisible to `tools/http_signals.py`, which is header-driven. This change makes an errors-bearing 200 raise the same typed refusal/transient signals as a 4xx, so the existing bounded-retry, deferral and loud-drop machinery governs it instead of being bypassed. This closes, for the new path, the silent-degradation class demonstrated live by the anonymous web-search probe (`200`, 172 857 bytes, **zero** result links, no error).
- **Conditional re-gather**: store the response ETag per link (additive registry migration, `SCHEMA_VERSION` 4 → 5) and send `If-None-Match` when re-fetching a file the registry has already gathered. A `304` means "unchanged": no download, no re-extraction, `visit_status` stays `gathered_ok`, `last_seen_ts` still advances. Measured: 304 works anonymously on `raw` (~150 ms, 0 bytes) and costs zero quota on the contents API.
- **Monotonic date writes**: `links.file_commit_date` and `links.repo_pushed_at` updates become `MAX(existing, new)` instead of the current `COALESCE(new, existing)`, so a later observation can never regress a recorded date backwards.
- **Rendered evidence**: the status surface gains the dating figures (requests, 304s, fill rate, batch count, GraphQL cost, refusals) so a run states its own coverage instead of requiring SQL inspection.
- Non-goals, recorded explicitly: no change to the query-splitting algorithm for either transport (excluded from the route by operator decision); no change to `keys.source_url_hash` overwrite semantics (adjacent defect, filed separately in `plan.md`); no parsing of web-search HTML for dates (prohibited by `docs/specs/date_extraction.md` and confirmed fruitless by probe).

## Capabilities

### New Capabilities
- `file-commit-dating`: acquisition, caching, batching and propagation of per-file freshness evidence (last commit date of a specific path), including its budget, its transport selection, its failure taxonomy and its inert-by-default rollout.

### Modified Capabilities
- `date-extraction`: the authority order for `file_commit_date` changes — the blob-HTML `<relative-time datetime="…">` source is demoted to a documented dead end (measured zero markers in both credential modes) and the API per-file source becomes authoritative; the repository-level `pushed_at` is explicitly declared insufficient for distinguishing a new leak from an old one.
- `link-registry`: additive schema migration to `SCHEMA_VERSION` 5 storing a per-link `etag`, idempotent for databases created at earlier versions; monotonic (`MAX`) semantics for the two date columns.
- `gather-transport`: a `304 Not Modified` outcome on the raw transport becomes a first-class successful result that short-circuits download and extraction while preserving accounting.

## Impact

- **Code**: new `storage/file_meta.py` (store + fetcher, mirroring `storage/repo_meta.py`); `storage/registry.py` (additive `_migrate_schema` guard, `links.etag`, monotonic date updates, `SCHEMA_VERSION` 5); `search/client.py` (commits-endpoint fetch, GraphQL POST, errors-in-200 classification, `If-None-Match` on the raw path, `_service()` mapping for `api.github.com/graphql`); `config/schemas.py`, `config/loader.py`, `config/validator.py` (new `FileDatingConfig`, mirrored strict validation, new `github_graphql` basket); `manager/pipeline.py` (construction and wiring, inert when disabled); `stage/definition.py` (call sites in search and gather, budget accounting); `state/display.py` (rendered metrics); `tools/http_signals.py` only if the errors-in-200 predicate belongs there rather than in the client.
- **Data**: one additive column and one new table in `<workspace>/registry.sqlite`; no rewrite of existing rows; registries at `user_version` ≤ 4 migrate in place on open. Existing shard NDJSON output is untouched.
- **Configuration**: new `file_dating:` section (default off) and a new `ratelimits.github_graphql` basket; `examples/config-full.yaml`, `README.md` and `docs/specs/date_extraction.md` updated. Rollback is a flag flip (`file_dating.enabled: false`) with no code change, matching every preceding subsystem.
- **Credentials and quotas**: dating consumes the existing rotating PAT pool through the existing basket machinery; the GraphQL path consumes the separate, currently idle `graphql` pool. No new credential type, no new secret storage. The gather conditional path stays **anonymous** — the probe established that credentials add nothing on `raw`.
- **Field verification**: requires live runs against GitHub with the production-shaped config, so the gate protocol follows `AGENTS.md` (run roots under `/var/tmp`, no `pytest` while a gate is active, credentials masked, launches wrapped in `timeout -k 30`).
