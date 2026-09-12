# Design: add-search-early-stop

## Context

See proposal.md — Why. Constraints:

- API pagination today: page 1 via `search_api_with_count` returns total; `_handle_first_page_results` (`stage/definition.py:224-296`) either refines (total > API_LIMIT) or spawns page tasks 2..min(ceil(total/per_page), API_MAX_PAGES=10). Each refined partition is a distinct deterministic query string (RefineEngine), i.e., a distinct result stream.
- `sort=indexed&order=desc` orders by index recency but is NOT strictly monotonic (timestamp clusters, GitHub-internal search shards); occasional novel items deep in a mostly-known stream are normal.
- The registry (changes 1–3) provides identities, lifecycle statuses, coverage, runs journal with degraded marker, and a read path proven under WAL concurrency.
- Web transport results are relevance-ordered — saturation there carries no information.

## Goals / Non-Goals

**Goals:**
- Stop deeper pagination per partition exactly when the frontier (boundary between new and already-researched content) is demonstrably behind us.
- Unify "known" with gather-skip semantics — one notion, one implementation.
- Make the false-stop price a measured number (`novel_after_stop`) before enforcement, and keep every failure mode biased toward full passes.

**Non-Goals:**
- No changes to RefineEngine internals (its determinism is exploited, not modified).
- No early termination of the refine fan-out itself (partition generation stays total-driven; stopped partitions simply don't paginate deeper).
- No web-transport detection (excluded at code level).

## Decisions

**D1 — "Known" = gather-skip decision, reused.**
A window result is known iff the conjunctive skip rule (status gathered_ok ∧ TTL ∧ ¬push-invalidation ∧ coverage) says skippable. Alternative rejected: bare `url_hash` existence — discovered-only or other-provider links still require work; stopping on them would silently drop research. Reuse keeps skip and stop economics consistent and reason codes attributable. **Alignment note (September 2026 amnesty):** spec R1 was tightened to match this decision — "known" is the FULL amended conjunction, including the gather TTL and the NULL-tolerant push condition (gather-skip spec amended after date-extraction D7 probes). Consequence: a fully TTL-aged corpus keeps windows novel → no stops → full passes, which is coherent — those pages represent real re-gather work, so traversing them is necessary, not waste.

**D2 — Ratio window evaluated at page boundaries.**
Tracker keeps a rolling deque of the last W=100 result identities per partition (W spans exactly one API page). After each page ≥ min_pages(2), compute known-ratio; stop iff ratio ≥ θ(0.9) ∧ trust gate. A consecutive-streak rule ("N known in a row") was rejected: `sort=indexed` interleaving makes streaks brittle in the UNSAFE direction (a streak can appear inside a cluster), while a ratio degrades safely (interleaved novels pull the ratio down → continue). All three parameters configurable (`early_stop.window/theta/min_pages`).

**D3 — Run-local trackers keyed by (provider, query).**
Plain dict in SearchStage state: key → {deque of window hashes, pages_fetched, hypothetical_stop_page, novel_after_stop counter}. Bounded by active partitions × W. Cross-run persistence unnecessary: frontiers are stable because RefineEngine regenerates identical query strings and the registry carries the memory.

**D4 — Trust gate + kill-switch implementation.**
Gate = `COUNT(links) ≥ min_trust` (default 1 000, configurable) ∧ `meta.migration_complete` present ∧ current run not degraded. The marker needs a home: this change adds an additive `meta(key TEXT PRIMARY KEY, value TEXT)` table (schema_version bump — permitted by change-1's additive-evolution rule); `tools/registry_migrate.py` gains writing `migration_complete=<ts>`. Kill-switch = the existing degraded flag: any registry error sets it (change-1 fail-open wrapper), and the detector consults it per evaluation — no separate mechanism.

**D5 — Shadow instrumentation shares the decision log.**
`would_stop_at` records append to `<workspace>/registry_decisions.jsonl` with `type='early_stop'` (partition query, page, window stats, ratio). `novel_after_stop`: links whose registry `first_seen_ts` falls within the current run AND that arrived after the hypothetical stop page for their partition. Promotion gate (task): `novel_after_stop ≈ 0` across ≥3 diverse staging query sets before default-on is even discussed.

**D6 — Index-backfill behavior is safe by construction.**
If GitHub re-indexes old content (backfill), those items lack recent `first_seen` novelty but ARE known in the registry → ratio stays high → stop fires correctly; if the registry was reset/migrated away, trust gate fails → full passes. Both directions verified by scenario tests.

## Risks / Trade-offs

- [False stop = missed leaks — highest-severity risk in roadmap] → conjunction of four independent gates (ratio, min_pages, trust, kill-switch) + shadow period with measured price + flag rollback.
- [Duplicate results across pages skew the window] → dedupe identities within the deque before ratio computation.
- [θ misconfiguration (too low)] → validator rejects θ outside [0.5, 1.0]; default conservative 0.9; docs explain trade-off.
- [Tracker state lost on crash] → harmless: trackers are per-run heuristics; worst case one extra full pass.
- [Interaction with gather-skip metrics] → shared decision engine emits skip reasons once; early-stop counters are separate; no double counting (stop prevents page fetches, skip prevents tasks from fetched pages).

## Migration Plan

1. Deploy `early_stop=off` (no-op).
2. Staging `shadow` across ≥3 diverse query sets (different providers/patterns); analyze `novel_after_stop`; require ≈0.
3. Production `on` with defaults; monitor `early_stop_fired` vs `novel_links_per_run` trend. Rollback: flag flip.

## Open Questions

- None blocking. (Tuning θ/W per provider from production metrics can happen later without spec changes — both are config.)
