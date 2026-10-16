## Context

See `proposal.md` — Why for motivation and the captured probe matrix. This section records only the existing code shape that constrains the approach.

- A working precedent already exists for authenticated metadata fetching: `storage/repo_meta.py` performs `GET /repos/{o}/{r}`, stores `pushed_at`, `size_kb`, `default_branch`, **`etag`**, `fetched_at`, `gone` in the `repos` table (`storage/registry.py:107-117`, `PRIMARY KEY(owner, repo)`), sends `If-None-Match` (`:159`), treats `304` as success and re-propagates stored values (`:179-180`, `:571-573`), honours a TTL (`is_fresh`, `:266-271`), suppresses retries for taken-down repos via `gone`, counts `enrichment_304s` (`:461`), checks `tokens_cooling_down()` (`:49`) and accepts `200/304/404` as non-errors (`:540`). It is constructed in `manager/pipeline.py:143-158` with `auth=get_auth_provider()`, `client=client.get_github_client()`, `enabled=config.enrichment.enabled`, and is **off** (`EnrichmentConfig.enabled: bool = False`, `config/schemas.py:351`).
- Additive schema migration is an established pattern: `SCHEMA_VERSION = 4` (`storage/registry.py:40`), `_migrate_schema(conn)` (`:273-284`) applies idempotent `ALTER TABLE … ADD COLUMN` guards because `CREATE TABLE IF NOT EXISTS` does not add columns, then `PRAGMA user_version` is stamped (`:269-270`). Its own comment names the rule: *additive columns only*.
- `links` already carries both date columns (`repo_pushed_at`, `file_commit_date`, `storage/registry.py:69-71`) plus `owner`, `repo`, `path`, and is keyed by `url_hash`. Date writes go through a separate UPDATE using `COALESCE(?, col)` (`:204-206`, `:217`) fed by `update_link_metadata` (`:601-618`). Both columns measured **0.0000** filled in runs №3 and №4 (2026-10-01).
- `_service(url)` (`search/client.py:288-300`) tests `api.github.com` **first**, then `raw.githubusercontent.com`, then generic `github.com`. A GraphQL POST to `api.github.com/graphql` therefore falls into the `github_api` basket unless explicitly overridden.
- Refusal classification is header-driven (`tools/http_signals.py`; `_classify_credential_limit` needs `x-ratelimit-*` / `retry-after`). Bounded waiting and eventual drop already exist: `gather.max_refusal_wait_s: 60` and `credential_liveness.max_wait_s: 60` are both strictly below `queue.visibility_timeout_s: 300` (invariant held by `config/validator.py:583-587`); `max_retries_requeued: 3` leads to a loud drop with `tasks_dropped_max_retries` (`stage/base.py:363-366`) under `failure_handling: "strict"`; `queue.max_age_hours: 24` purges by age; a deferral does not consume the retry budget.
- An in-process or TTL memo cannot substitute for durable state here: duplicate copies of the same URL in the gather queue were measured **p50 5 986 / 6 693 seq positions apart** and ~850 s apart in wall clock at ~7 req/s (plan.md II.6.1, reproduced in run №3), so nothing stays hot long enough.

## Goals / Non-Goals

**Goals:**
- Make `links.file_commit_date` actually fill, from a source proven live, without changing any decision the pipeline makes today while the feature is off.
- Spend **fewer requests**, not fewer quota units: batching and conditional requests are the primary levers, because the binding constraint is the 9/min `github_api` basket shared with search.
- Route every new failure through the existing typed-signal machinery so bounded retry, deferral and loud drop govern it.
- Produce rendered evidence per run so coverage is readable without SQL.

**Non-Goals:**
- No change to query splitting or refine depth for either transport (excluded from the route by operator decision; the prefix-ladder measurement is recorded in `plan.md` II.21.1 as data only).
- No change to `keys.source_url_hash` overwrite semantics — the collapse of "same key seen in two files" into one ledger row is a real adjacent defect and is filed in `plan.md`, not fixed here.
- No parsing of web-search HTML for dates (prohibited by `docs/specs/date_extraction.md`, and the probe found zero date markers on that page anyway).
- No promotion of `enrichment.enabled`; repository-level dating stays a separate, still-disabled subsystem. This change deliberately does **not** turn it on, because the measured B−C gap (up to 4 561 days) shows repo-level `pushed_at` cannot answer "new leak or old leak".
- No new credential type and no secret storage change.

## Decisions

**D1 — New module `storage/file_meta.py`, not an extension of `RepoMetaStore`.**
The cache key granularity differs (`owner, repo, path` vs `owner, repo`) and so does the endpoint. Extending `repos` would require changing its primary key, which is a destructive migration and violates the additive-only rule. Alternative rejected: a generic key/value cache in the existing `meta` table — it has no schema for TTL/etag/gone and would push parsing into callers.

**D2 — Cache key `(owner, repo, path)` plus a `resolved_sha` column; sha mismatch is a cache miss.**
A blob URL carries a sha, and the same path can be observed at different shas across runs. Keying by `(owner, repo, path, sha)` would duplicate rows for one logical file and inflate the table beyond the measured 3 029 distinct files. Keying by path alone and storing the sha it was resolved against keeps one row per file while staying correct: if the observed sha differs, the entry is treated as stale and re-fetched. Table: `file_meta(owner, repo, path, resolved_sha, committed_date, etag, fetched_at, gone, PRIMARY KEY(owner, repo, path))`. Alternative rejected: no cache at all — at 1 request per file and a 9/min shared basket, re-dating on every run is what makes the feature unusable.

**D3 — Two transports behind one interface: `file_dating.transport: rest | graphql`, default `rest`.**
REST (`GET /repos/{o}/{r}/commits?path={file}&per_page=1&sha={sha}`) is the reference implementation: one file per request, 1 `core` unit, response shape captured 2026-10-14. GraphQL batches N files into one POST with N aliases; measured `rateLimit.cost = 1` for N=4 across four repositories, with dates byte-identical to REST. Default is `rest` because its cost model is fully measured, while GraphQL cost scaling with N is **not** (open item, answered by the field gate, not by assumption). Alternative rejected: GraphQL-only — it would make the first live measurement depend on the least-understood path, and REST remains the fallback when the GraphQL pool is exhausted. Alternative rejected: REST-only — it forfeits the only lever that reduces request *count*, which is the binding constraint.

**D4 — GraphQL rides a new basket `ratelimits.github_graphql`; it must not fall into `github_api`.**
Because `_service()` matches `api.github.com` first, an explicit service-type override for the `/graphql` path is required. Rationale: `github_api` is `base_rate 0.15` ≈ 9 req/min **shared with code search**; measured, full file-level dating through it costs ~5.6 h and a 300 s run reaches ~1.8 % of repositories, i.e. it would starve search rather than augment it. The `graphql` pool was measured completely idle (`remaining 5000/5000`). Alternative rejected: reusing `github_api` — automatic inheritance of rotation and throttling is real but worthless if the budget is contended.

**D5 — "HTTP 200 carrying `errors`" is classified into the existing typed signals; no new exception class.**
GraphQL reports schema, permission and point-exhaustion problems inside a `200` body (captured: `Blob.committedDate` returned `200` with `errors[].path` pointing at the offending field). Mapping: a recognised rate-limit/points extension → the credential-limit signal; a recognised permission/absent-resource extension → the permanent-failure signal; anything unrecognised → `TransientFetchError`. Under no circumstance may an errors-bearing response be returned as an empty success. Rationale: the deferral, bounded-retry and loud-drop machinery keys off these types, so a new class would leave the path ungoverned. Alternative rejected: a dedicated `GraphQLError` exception — every call site would have to learn it, and the existing counters would not see it. This decision exists because the same silent-empty class was demonstrated live on a different surface: anonymous web search returned `200`, 172 857 bytes and **zero** result links with no error.

**D6 — Conditional re-gather is gated on prior evidence, not applied universally.**
Send `If-None-Match` only when the link already has `visit_status = 'gathered_ok'`, a stored etag **captured on the same transport**, and a fresh TTL. A `304` means unchanged: no download, no re-extraction, no shard write, `visit_status` stays `gathered_ok`, `last_seen_ts` advances, counter `gather_304` increments. Captured evidence: `304` with an empty body on `raw` **anonymously** in 150–182 ms, and `304` on the contents API with `x-ratelimit-remaining` unchanged (4994→4994, 4991→4991, 4988→4988, 4985→4985) — i.e. zero quota. The same-transport condition is mandatory because the two hosts issue different etags for the same file: `raw` returned `"0e402cf4…"` (64 hex) while contents returned `"fdeb21a0…"` (40 hex). Alternative rejected: conditional request on every gather — it spends a basket slot on files never seen before, which is most of them on a first run. Alternative rejected: in-memory memo — refuted by the measured seq/wall-clock spread of duplicates (see Context).

**D7 — Date writes become monotonic: `MAX(existing, new)` instead of `COALESCE(new, existing)`.**
The `304` path deliberately re-propagates stored values (`repo_meta.py:571-573`), so a stale cached date can be written after a newer one. `COALESCE` would let it win. Applies to `links.file_commit_date` and `links.repo_pushed_at`. Mirrors the existing `last_seen_ts = MAX(COALESCE(links.last_seen_ts, excluded.last_seen_ts), excluded.last_seen_ts)` idiom (`storage/registry.py:138`, `:158`). Alternative rejected: leaving `COALESCE` — a silent backwards date would corrupt exactly the "new leak or old leak" judgement this change exists to enable.

**D8 — Scope defaults to files where a key was actually found: `file_dating.scope: keyed | gathered | discovered`, default `keyed`.**
Measured population: 3 029 distinct files, 2 524 distinct repositories, 2 080 `gathered_ok` links, **225 keys**. Dating the `keyed` set serves the operator's stated purpose (mark repos that leaked, judge new vs old) at ~7 % of the request cost of `discovered`. Alternative rejected: `discovered` as default — 5.6 h through a contended basket for dates nobody consumes, since a file with no finding produces no key to date.

**D9 — Inert by default, budgeted, and independently rollbackable.**
`file_dating.enabled: false` by default (house doctrine: a new subsystem is byte-for-byte inert until an operator opts in, as with `EnrichmentConfig`, `SkipConfig`, `CheckSkipConfig`, `EarlyStopConfig`). Plus `file_dating.max_requests_per_run` (hard stop for the run, counted separately from the basket) and `file_dating.ttl_hours`. Rollback is a flag flip with no code change. Strict validation is mirrored in both places, as the codebase already does for `refine_governor.drop_wire_indistinguishable`: `__post_init__` raises for direct construction and `ConfigValidator` appends the identical message for loaded configs. Booleans are validated with `isinstance(x, bool)` and **never coerced**, because `bool("false") is True`.

**D10 — Additive migration to `SCHEMA_VERSION = 5`, idempotent, downgrade-safe.**
One new column `links.etag TEXT` and one new table `file_meta`, both applied through the existing `_migrate_schema` guard so registries created at version ≤ 4 migrate in place on open. Because the change is additive, older code reading a version-5 database simply ignores the extra column, so a rollback of the binary does not require a rollback of the data. Verification uses a **copy** of a real registry at `user_version = 4` (from run №4), never the original.

**D11 — Every external-format assumption is an opportunistic contract pinned to a captured fixture.**
Parsers read `committer.date` / `author.date` from a list response and `data.<alias>.object.history.nodes[0].committedDate` from GraphQL; each degrades to "no evidence" plus a counter when the shape differs, and never to a fabricated date. The captured counter-example is retained as a fixture: `... on Blob { committedDate }` returns `200` with `errors` — the field does not exist — which is precisely why the query text is pinned rather than composed dynamically. Similarly, `<relative-time datetime="…">` parsing stays in the tree but is documented as dead (zero markers in eight live responses across both credential modes).

**D12 — Metrics are additive only; nothing is renamed or removed (lesson И7).**
New counters: `file_dating_requests`, `file_dating_304s`, `file_dating_filled`, `file_dating_absent`, `file_dating_batches`, `file_dating_graphql_cost`, `file_dating_refusals`, `file_dating_budget_stopped`, `gather_304`. Rendered on the status surface next to the existing enrichment figures. Continuity matters because a renamed counter silently breaks comparison against the archived gates.

**D13 — Credentials and privacy.**
Dating requests carry rotating PATs through the existing provider; the conditional gather path stays **anonymous** because the probe established that neither a PAT nor a session cookie changes the `raw` response in any way. Responses contain no credentials, and file paths in query strings are not secrets; nevertheless all gate evidence is produced under the `AGENTS.md` §3 masking discipline and scanned by credential **value**, not by filename glob (lesson И8: a `.bak-<ts>` copy survived a `*.yaml` cleanup check).

## Risks / Trade-offs

- **[GraphQL cost scaling with N is unmeasured]** → the field gate measures `rateLimit.cost` at N = 4, 20, 50 before any default batch size is raised; `file_dating.graphql_batch_size` has a validator-enforced ceiling; `rest` remains the default transport.
- **[Dating starves search]** → separate basket (D4) **and** a per-run request budget (D9) **and** `scope: keyed` by default (D8); the gate measures search throughput with dating on versus off and treats a regression as a failed gate.
- **[`commits?path=` on a very large repo may be slow or return an unexpected shape]** → `per_page=1`, the standard request timeout, and any non-list or empty response counts as `file_dating_absent` rather than as a date; a `404` marks the cache entry `gone` exactly as the repo store does, suppressing retries.
- **[Commit dates can predate repository creation]** — measured: `asterisk-chan-dongle::INSTALL` has `committer.date` 2011-06-30 against `created_at` 2015-04-11, because the history was imported from SVN. → Documented as expected behaviour; no consumer may assume `file_commit_date >= created_at`. This is a feature for the operator's purpose: it dates the content, not the GitHub account.
- **[An etag captured on one transport replayed against another]** → D6 stores the transport with the etag and refuses cross-transport reuse; a scenario pins it.
- **[A `304` mistaken for a successful re-gather that should have produced findings]** → a `304` writes no shard record and produces no key rows; it only advances recency. Pinned by scenario.
- **[Repository-level `pushed_at` misread as file freshness]** → the demotion is written into the `date-extraction` spec delta with the measured 11 / 35 / 311 / 4 561-day gaps, so a future reader cannot re-introduce the substitution.
- **[Schema migration on a live production registry]** → additive only, verified against a copy at `user_version = 4`, and the binary rollback path is data-compatible (D10).
- **[Trade-off accepted]** `scope: keyed` means files with no finding stay undated, so "sort and stop when only old repos appear" operates on the dated subset only. Widening to `gathered` or `discovered` is a config change, not a code change, once the gate has priced it.

## Migration Plan

1. Land the schema migration and the store with `file_dating.enabled: false`; verify a copy of the run-№4 registry opens at `user_version = 5` with all pre-existing rows intact and identical query plans.
2. Land the REST fetcher, propagation into `links.file_commit_date`, monotonic date writes and the rendered counters. Suite green; feature still inert.
3. Land the conditional re-gather path (`links.etag`, `If-None-Match`, `304` short-circuit) behind the same flag family.
4. Land the GraphQL transport with its own basket and the errors-in-200 classification. Still default `rest`.
5. **Field gate** (live, production-shaped config, `AGENTS.md` hygiene): arm A `enabled: false`, arm B `enabled: true, scope: keyed, transport: rest`, arm C `enabled: true, scope: keyed, transport: graphql`. Measure fill rate, 304 ratio, bytes saved, GraphQL cost at three batch sizes, search throughput delta, refusals, `degraded` runs. Promote defaults only on evidence.
6. Rollback at any stage: `file_dating.enabled: false` (and `gather` conditional reuse disabled by the same family). No data rollback needed.

## Open Questions

- Optimal `graphql_batch_size` — answered by gate step 5, does not change any spec (the spec states batching with a configured size and an enforced ceiling).
- Whether `file_dating` should later subsume `enrichment` into one freshness subsystem — deferred; both remain independently flaggable, and merging them would change two shipped specs at once.

## Amendments recorded after the live field gate (2026-10-16)

The first gate ran four sequential 300 s arms against the production-shaped
configuration (arm A inert, arm B `rest`, arm B2 a second run over arm B's
workspace, arm C `graphql`). All four exited 0, reported `Overall Status:
Healthy` with `Critical Alerts: 0`, and produced no banned signature
(`Traceback`, `Logging error`, `lost task`, `secondary rate limit`,
`Disk quota exceeded`, HTTP 429/403). The gate nevertheless refuted four
implementation assumptions and exposed one doctrine gap. Each is recorded here
as a numbered amendment rather than folded silently into D1–D13.

**D14 — Link propagation must be keyed by URL, not by `(owner, repo, path)`.**
Arm B filled 73 `file_meta` rows (`filled=40`, `fill=1.0000`) while
`links.file_commit_date` stayed **0.0000**. Cause: `parse_github_url()` stores
`links.path` *with* the `blob/<revision>/` (or `<revision>/`) prefix, whereas
`file_meta.path` holds the clean path from `_parse_blob_link()`, so the
`(owner, repo, path)` UPDATE matched zero rows. Amendment: dates reach `links`
through the existing URL-keyed channel (`record_metadata` → `_METADATA_SQL`,
keyed by `url_hash`, monotonic per D7), exposed as
`FileDater.date_urls_map(urls) -> Dict[url, Optional[float]]`. The
`(owner, repo, path)` propagation stays in the tree as a cache-side helper only.
Rejected alternative: storing the prefixed form in `file_meta` — it would make
the cache key revision-dependent and defeat D2's one-row-per-file bound.

**D15 — Scope must be enforced at the call site, not inside the dater.**
Arm C dated 100 files that produced no findings while `scope="keyed"`,
contradicting scenario `file-commit-dating-S23`. The hook passed the whole
discovered population and delegated the decision to an orchestrator that did not
filter. Amendment: the search-stage hook fires only for `scope="discovered"`;
the gather hooks fire only for `keyed` (inside the branch where services were
found) and `gathered` (any successful gather).

**D16 — Dating is bounded by wall clock, not only by request count.**
Arm C completed **one** search task in 300 s (`Aggregation: misses=1` versus 37,
gather never ran, console 421 lines versus 6 959) because `threads.search = 1`
and dating was synchronous: 25 batches through a `github_graphql` basket at
`base_rate 0.2` cost ~5 s each. Amendment: `date_urls`/`date_urls_map` take a
`budget` parameter checked between files and between batches, defaulting to
`DEFAULT_DATING_BUDGET_SECONDS = 5.0`, mirroring
`RepoMetaEnricher.enrich_urls(budget=…)`; truncation counts
`file_dating_time_stopped` and loses nothing because the cache is the ledger.
The example basket rate also moves 0.2 → 0.5 s⁻¹ (30/min, still far below the
measured idle 5 000 points/h pool). This is the concrete answer to the open
question in D3 about batch cost: measured `cost=25` for `batches=25`
(`filled=100`), i.e. **one point per batch of four**, reproducing the fixture.

**D17 — The published request counter must be transport-independent.**
Arm C rendered `req=0` despite 25 POSTs because `to_stats()` read the client-side
stat that only the REST path incremented. Amendment: `file_dating_requests`
reports `self._requests_this_run`, and the batch fetcher increments the
client-side counter once per POST so both sources agree. Counting one HTTP
request per *batch* is correct semantics — arm C's 25 batches against a ceiling
of 40 legitimately produced `budget_stop=0`.

**D18 — Conditional re-gather ships behind its own flag and must be rendered.**
Two gaps: `gather_304` existed in the stats but no renderer exposed it (violating
the project's observability convention that a published surface nobody renders is
a defect), and the behaviour had no rollback switch. Amendment: the `Gather:` line
gains `304=<n>`, the `Dating:` line gains `cached=<n>`, and a new
`gather.conditional_refetch: bool = True` gates whether any `If-None-Match`
precondition is sent (strict `isinstance(bool)`, mirrored messages, no coercion).
Setting it false restores pre-change behaviour byte-for-byte while token storage
may continue. Precedent for defaulting such a guard to true with a documented
rollback is `refine_governor.drop_wire_indistinguishable` (R11).
