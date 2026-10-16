# Date Extraction (free freshness metadata)

**Owning change:** `add-date-extraction`
**Depends on:** `add-link-registry` (reserved columns `repo_pushed_at`,
`repo_size_kb`, `file_commit_date`)

The harvester captures freshness data **for free** from HTTP payloads it
already downloads — no extra requests, no extra quota. The values land in the
link registry and are the invalidation signal the later gather-skip change
reads (`repo_pushed_at > gathered_ts` ⇒ re-gather).

## Transport asymmetry (by design)

| Transport | Where dates are captured | What is captured |
|-----------|--------------------------|------------------|
| API (`use_api: true`) | search stage, from the code-search JSON | `repository.pushed_at` (fallback `updated_at`) → `repo_pushed_at`; `repository.size` → `repo_size_kb` |
| Web (`use_api: false`) | gather stage only, from the blob HTML | `file_commit_date` (MAX of `<relative-time datetime="…">`) |

Web **search-results** HTML is deliberately **not** parsed for dates: that
surface is brittle (it changes often) and parsing it would widen the fragile
area for no required gain. Consequently a link discovered via the web transport
carries NULL date metadata until it is gathered.

## Field preference and parsing

- `repository.pushed_at` is preferred over `updated_at`: a push is the event
  that changes file contents, whereas `updated_at` also moves on metadata-only
  edits — an acceptable overestimate in the safe direction.
- All timestamps are normalised to UTC epoch seconds (`Z` suffix handled).
- `repository.size` is recorded as-is (KB, per GitHub API docs).

## MAX-bias rationale for `file_commit_date`

A blob page contains several `relative-time` elements (file header, sidebar,
related entries). The extractor records the **maximum** of all matches. This
biases the recorded date upward on purpose because the errors are asymmetric:

- **Overestimate** → `repo_pushed_at`/`file_commit_date` looks newer than the
  gather → at most one extra re-gather (cheap, safe).
- **Underestimate** → a changed file can be falsely skipped (missed findings —
  dangerous).

When no elements match (layout drift, non-blob page), the value degrades to
NULL and a counter increments; gather-skip then falls back to TTL-only, which
is the safe direction.

## Fail-open guarantees

No date-extraction fault propagates into a stage worker loop. Malformed JSON,
unexpected item types, missing `repository` blocks, repositories present but
without any usable `pushed_at`/`updated_at` (`missing_date` — the prevailing
production case since the September 2026 probe, design D7), undecodable bytes
and unparseable datetimes all yield NULL fields plus a counted warning
(once-per-kind log). Counters are exposed via
`search.client.get_date_parse_stats()`.

## Registry merge policy

Metadata reaches the registry through `StageOutput.link_metadata` →
`Pipeline._handle_stage_output` → `Registry.record_metadata`, and is merged with
`COALESCE(excluded, links)` semantics:

- a NULL observation **never** erases a known value;
- a non-NULL observation **replaces** the stored one (dates grow monotonically).

## Metrics interpretation

| Metric | Formula | Healthy signal |
|--------|---------|----------------|
| `date_fill_rate_api` | API items with a usable `repo_pushed_at` ÷ API items processed | ≈ 1.0; well below 1.0 suggests an API schema change |
| `date_fill_rate_web` | gathered pages with a `file_commit_date` ÷ pages gathered | record the observed baseline; a collapse towards 0 indicates GitHub blob-layout drift |

Both are visible per run in `PipelineStatus.date_metrics` (see
`docs/specs/registry_flags_metrics.md`). Threshold alerting is intentionally
left to operators, not coded.

## Measured source availability (live probe, 2026-10-14)

The table above documents what the code *parses*. A live four-target probe established
what GitHub actually *returns* today; full matrix and method in `plan.md` II.21.

| Source | Transport / credential | Result |
|---|---|---|
| `repository.pushed_at` from `/search/code` | API + PAT | **absent** — the `repository` object is trimmed (`pushed_at`, `updated_at`, `created_at`, `size` all missing); item keys are `git_url, html_url, name, path, repository, score, sha, url`. The parser is kept as a dormant trap |
| `GET /repos/{owner}/{repo}` | API + PAT | **works**, 1 `core` unit: `pushed_at`, `updated_at`, `created_at`, `size`, `default_branch`; carries `last-modified` and `etag` headers. This is the enrichment path (`enrichment.enabled`) |
| `GET /repos/{o}/{r}/commits?path=<file>&per_page=1` | API + PAT | **works**, 1 `core` unit: `committer.date` of the last commit touching that file. Not implemented anywhere |
| GraphQL `Blob.committedDate` | API + PAT | **field does not exist** (query returns `errors`) |
| GraphQL `Commit.history(first:1,path:)` | API + PAT | **works**; a batch of 4 files across 4 repositories cost `rateLimit.cost = 1` and matched the REST dates byte-for-byte. Not implemented |
| `<relative-time datetime="…">` on the blob page | HTML, anonymous **and** session cookie | **zero occurrences** of `relative-time`, `datetime=`, `committedDate`, `"pushedAt"`, `latestCommit` in all 8 responses (192–247 KB each). Anonymous additionally returned **503 on 2 of 4** targets; under a session cookie all 4 returned 200 |
| `Last-Modified` on `raw.githubusercontent.com` | raw: anonymous / PAT / session | **header absent in all three modes**; bodies byte-identical and equal to the file size. Credentials add nothing on this transport |
| `Last-Modified` on the contents API | rest: anonymous (limit 60/h) and PAT (limit 5000/h) | **present**, but equals the repository push time, not the file's commit date. The code never reads it |
| Conditional request (`If-None-Match`) | raw anonymous, contents + PAT | **304 with an empty body**; on the contents API the 304 consumed **zero** `core` quota (`x-ratelimit-remaining` unchanged). Usable as a cheap "did the file change" check |

Two consequences worth stating explicitly:

- Repository-level `pushed_at` is **not** a proxy for file freshness. Measured gaps
  between `pushed_at` and the file's own `committer.date`: 11, 35, 311 and **4 561 days**
  (12.5 years). The gap does not correlate with the repository's commit count (44 commits
  produced the 12.5-year gap; 110 commits produced 311 days) — it depends on whether recent
  activity touched that particular file.
- Web search results carry no dates either, which confirms the design decision above not to
  parse that surface. The legacy `github.com/search?type=code` endpoint itself is alive under
  a session cookie (125 result links observed) and returns an empty shell anonymously, without
  an error — so an expired session degrades silently rather than failing loudly.

## Authority order for `file_commit_date` (add-file-commit-dating)

Since the `file-commit-dating` capability, the sources of `file_commit_date`
are ordered by authority:

1. **Per-file API evidence** — the last commit that touched the path, obtained
   from `GET /repos/{o}/{r}/commits?path=&per_page=1` (`rest`) or from a batched
   GraphQL `Commit.history(first: 1, path: …)` query. Authoritative, and the only
   source measured to return a value (2026-10-14: four of four targets, both
   transports agreeing byte-for-byte).
2. **Blob-page markup** — `<relative-time datetime="…">` parsing, retained in the
   tree **only as a drift detector**. Eight live responses across both credential
   modes carried zero occurrences of every date marker, so this source yields NULL
   by construction today; if GitHub ever restores the attributes the fill-rate
   metric announces it.
3. **Repository `pushed_at`** — never a substitute for a file date. It remains a
   separate column (`links.repo_pushed_at`) fed by `enrichment`, and the two must
   not be conflated: measured gaps reached 4 561 days.

Delivery into the registry is monotonic for both date columns: a NULL never
overwrites a known value, and a known value is replaced only by a strictly newer
one. This matters because a conditional `304` refresh deliberately re-propagates
stored values, which under the previous `COALESCE(new, existing)` merge could have
moved a recorded date backwards.
