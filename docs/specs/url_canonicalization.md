# URL Canonicalization Canon (Phase-0)

**Owning change:** `add-link-registry`
**Implemented by:** `storage/registry.py::canonical_url` / `url_hash`

This document is normative. `canonical_url()` defines link identity for the
entire registry. Any change to these rules requires a spec update **and** a
full re-migration path, because a changed identity silently splits or merges
existing rows.

## Rules

1. **Scheme and host are lowercased.**
2. **The query string is discarded** (`?…`).
3. **The fragment is discarded** (`#…`), including GitHub line anchors
   (`#L10`, `#L42-L50`).
4. **A trailing slash is normalized away** (`https://github.com/o/r/` →
   `https://github.com/o/r`; the bare path `/` becomes empty).
5. **`owner/repo/path`/ref letter case is preserved exactly.** GitHub paths and
   refs are case-sensitive; `Config.env` and `config.env` are different files.
6. **The branch/ref segment is preserved.** Links to the same file on two
   branches are two different identities.
7. **Identity is `url_hash = sha256(canonical_url)`**, rendered as lowercase hex
   and used as the `links.url_hash` primary key.
8. **Transport does not affect identity.** The same blob URL discovered via the
   API and via the web maps to one row.

## Rationale: false-known vs false-novel asymmetry

A **false "known"** (two genuinely different files collapsing to one identity)
causes the harvester to silently skip a file it has never gathered — missing
real keys, with no audit trail. A **false "novel"** (one file splitting into two
identities) merely re-gathers a file once. The cost asymmetry is decisive: every
ambiguous normalization rule resolves toward *more* identities, never fewer.

This is why branch-agnostic normalization (collapsing the ref segment) is
explicitly **rejected**: it would make an unseen file on a renamed branch look
"known" and skip it. A branch rename instead yields a new identity and a safe
re-gather.

## Known limitations (out of canon)

The following normalizations are **deliberately not applied**. They are outside
the frozen canon, do not occur in GitHub search results, and changing any of
them would alter identities (requiring a spec update plus a full re-migration):

- Embedded credentials (`https://user:pass@github.com/...`) are not stripped
  from the authority.
- Explicit default ports (`https://github.com:443/...`) are not collapsed.
- Non-HTTP(S) schemes (`ftp://`, `file://`, …) are hashed as-is rather than
  rejected.
- Repeated path slashes (`https://github.com//o//r`) are not collapsed.

They are recorded here so future hardening moves identities apart, never
together (see the asymmetry rationale above).

## Golden vectors

Let `H(x) = sha256(canonical_url(x))` (hex).

| Input | `canonical_url(input)` | `url_hash` (first 16 hex) |
|-------|------------------------|---------------------------|
| `https://github.com/o/r/blob/main/a.py#L10` | `https://github.com/o/r/blob/main/a.py` | `ffab250b95f85734…` |
| `https://github.com/o/r/blob/main/a.py#L42-L50` | `https://github.com/o/r/blob/main/a.py` | `ffab250b95f85734…` |
| `https://github.com/o/r/blob/main/Config.env` | `https://github.com/o/r/blob/main/Config.env` | `ac0f6fdebabb2686…` |
| `https://github.com/o/r/blob/main/config.env` | `https://github.com/o/r/blob/main/config.env` | `4b085544592201de…` |
| `HTTPS://GitHub.com/o/r/blob/main/a.py?token=x` | `https://github.com/o/r/blob/main/a.py` | `ffab250b95f85734…` |
| `https://github.com/o/r/` | `https://github.com/o/r` | `97393e7e6b5ab8df…` |
| `https://github.com/o/r/blob/main/dir/` | `https://github.com/o/r/blob/main/dir` | `8ff1abb19b1f097a…` |

Invariants asserted by the automated suite:

- **Rows 1–2 share one hash** — `link-registry-S4` (line fragment does not change identity).
- **Rows 3–4 differ** — `link-registry-S5` (path case is significant).
- **Row 1 / row 5 share one hash** — transport/query-independent identity.
- **`link-registry-S6`** records one row via `transport='api'` then
  `transport='web'`, preserving `first_seen_ts` and updating `transport`.

## Ownership and evolution

| Concern | Owner |
|---------|-------|
| Canon rules + tests | `add-link-registry` (this change) |
| First reader of identity (`skip_known`) | `add-gather-skip` |
| Date extraction metadata (`repo_pushed_at`, …) | `add-date-extraction` |
| Prioritization by date + key status | `add-target-prioritization` |
