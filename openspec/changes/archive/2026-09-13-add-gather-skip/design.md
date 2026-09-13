# Design: add-gather-skip

## Context

See proposal.md — Why. Constraints:

- Single choke point exists: `SearchStage._search_worker` creates one AcquisitionTask per hit right after `output.add_links` (`stage/definition.py:114-127`); both transports funnel through it.
- Registry writes go through one writer thread (add-link-registry D2). WAL mode permits concurrent readers while the writer holds its transaction — readers must NOT queue through the writer (search worker latency).
- Dates may be NULL (web transport before gather; API outages): condition 3 must tolerate NULL safely.
- Search worker count defaults to 1 thread but is configurable; the read path must be thread-safe regardless.

## Goals / Non-Goals

**Goals:**
- Eliminate redundant GET+check cascades for unchanged, already-researched files in both transports.
- Make the false-skip cost measurable BEFORE enforcement (shadow mode + decision log joinable with shard outcomes).
- Guarantee: missing registry rows, errored lookups, and failed gathers ⇒ re-gather (errors bias toward work, never toward silence). Nullable *evidence* fields (`repo_pushed_at`) are exempt from this veto: their absence degrades precision to TTL-bounded staleness (D3), it does not disable skipping.

**Non-Goals:**
- No skipping of search itself (early-stop is a separate change with its own risk gate).
- No check-level dedup (key ledger change owns provider-call economics).
- No automatic TTL tuning; defaults are static config.

## Decisions

**D1 — Read-only connections, not writer-thread queries.**
Each search worker opens `sqlite3.connect("file:<path>?mode=ro", uri=True)` lazily and keeps it for the run; WAL guarantees snapshot-consistent reads beside the writer. Alternative rejected: routing lookups through the writer thread's queue — head-of-line blocking behind batch writes and awkward timeout semantics inside a worker loop.

**D2 — Batched page-level lookups.**
A search page yields ≤100 URLs (API) / ≤20 (web). One `SELECT … WHERE url_hash IN (…)` plus one coverage `SELECT` per page builds a decision map; per-link queries rejected (round-trip overhead × threads).

**D3 — Condition order and reason attribution.**
Evaluate cheapest-first: visit_status → coverage row → TTL → pushed_at invalidation. First failing condition names the `regathered_*` metric. Order does not affect the decision (conjunction) but stabilizes attribution semantics for ops dashboards. NULL `repo_pushed_at` ⇒ condition 3 passes vacuously (no evidence of change) — TTL remains the backstop; this is the documented TTL-only degradation mode. **Amendment (September 2026):** live probes during the date-extraction amnesty (its design D7) established that NULL `repo_pushed_at` is the *prevailing production state* — not an edge case — until `add-repo-meta-enrichment` lands. Vacuous pass is therefore the normal path; the new `push_signal_coverage` metric (share of known-link decisions backed by real push evidence) makes evidence quality visible so operators know exactly how much of the fleet runs TTL-only. The original spec wording ("any datum missing/NULL → task created") over-generalized the veto beyond this design's intent and was amended to match: the veto applies to missing registry rows / `gathered_ts` / errored lookups, never to nullable evidence fields.

**D4 — TTL default 168 h (7 days), config `skip.gather_ttl_hours`.**
Rationale: weekly cadence bounds staleness for repos where dates are unavailable (web transport), while active repos are covered far more precisely by push invalidation. Resolves plan open question #1 (gather part). Key-status TTLs belong to the key-ledger change.

**D5 — Shadow decision log: append-only JSONL `<workspace>/registry_decisions.jsonl`.**
One line per would-skip: `{ts, run_id, url_hash, provider, conditions:{status,ttl,push,coverage}, decision}`. Offline analysis joins it with `material`/`valid` shards written in the same run to compute the empirical false-skip price: keys extracted from URLs that shadow marked as skippable. Promoting `shadow→on` REQUIRES this price ≈ 0 over a staging period (verification task), mirroring the early-stop promotion gate. Logger-based shadow rejected: unstructured, rotated, hard to join.

**D6 — Flag plumbing follows existing conventions.**
`skip_known` parsed in loader, validated to the enum {off, shadow, on}, default off; rollback = config flip, no code removal (plan principle 2).

**D7 — Links audit trail unaffected.**
`output.add_links` continues to record EVERY search hit into `shards/links/*` regardless of skipping — the shard stays a complete discovery log; only task creation is suppressed. (Keeps add-link-registry's migration source semantics intact for future runs.)

## Risks / Trade-offs

- [False skip of a changed file] → protection layers: status + TTL + coverage always; push invalidation whenever dates are available (see `push_signal_coverage` — under current GitHub reality most decisions run TTL-only until enrichment lands); shadow-period measurement gates enforcement; failed/suspicious data always re-gathers. Worst-case miss window in TTL-only mode = gather TTL (default 7 d).
- [Canonicalization bug marks unseen file as known] → frozen canon with golden tests (change 1); shadow period would surface it as keys-from-skipped-URLs in the join.
- [Clock/timezone skew between `gathered_ts` (local epoch) and `repo_pushed_at` (GitHub ISO→epoch)] → both normalized to UTC epoch floats at write time; comparison adds a small grace (design note: treat `pushed_at > gathered_ts + 60s` as changed) to absorb rounding.
- [WAL reader on stale snapshot misses just-written rows] → harmless: a missed row means "unknown" ⇒ task created (safe direction).
- [Decision-log growth in shadow mode] → one JSONL line per candidate; bounded by search volume; rotation via existing log conventions, documented.

## Migration Plan

1. Deploy with `skip_known=off` (no-op).
2. Staging: `shadow` for ≥1 representative run cycle; execute task 5.3 join analysis; require false-skip price ≈ 0.
3. Production: `on`. Rollback: flip flag back; decision log and metrics persist for postmortem.

## Open Questions

- None blocking. (Whether to lower gather TTL for wait_check-heavy providers can be answered later from metrics without spec changes.)
