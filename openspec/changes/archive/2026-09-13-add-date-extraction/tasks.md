# Tasks: add-date-extraction

## 1. RED baseline

- [x] 1.1 Capture sanitized fixtures into `tests/fixtures/`: one API code-search JSON page (incl. an item without `repository`), one blob HTML page with several `<relative-time datetime>` elements, one page without markers, one garbage payload <!-- AMNESTY NOTE: originally synthesized, not live-captured — process gap per design D6/D7; superseded by task 7.5 -->
- [x] 1.2 Author tests from tests.md: `tests/test_date_extraction.py` (date-extraction-S1..S5, S10), `tests/test_date_flow.py` (S6..S9); assertions encode spec THEN clauses
- [x] 1.3 Run pytest; confirm all new tests fail for expected reasons (missing helpers/fields) — record RED in tests.md

## 2. Client parsers (`search/client.py`)

- [x] 2.1 Add `LinkMetadata` dataclass + enrichment mapping to both API parse points (`:741-754`, `:848-865`): read `repository.pushed_at` → fallback `updated_at` → NULL; `repository.size`; keep URL-set contracts unchanged (date-extraction-S1, S2, S3)
- [x] 2.2 Add `_extract_file_commit_date(html)` helper: regex `<relative-time[^>]*datetime="([^"]+)"`, ISO parse, return MAX of matches or NULL; call from `collect()` path (~`:1080-1124`) (date-extraction-S4, S5)
- [x] 2.3 Wrap all new parsing in fail-open guards with once-per-kind warnings and counters (date-extraction-S10)

## 3. Delivery & registry merge

- [x] 3.1 Extend `StageOutput` with optional `link_metadata` carrier (`stage/base.py:57-81` pattern); populate in SearchStage (API transport only) and AcquisitionStage workers (date-extraction-S6)
- [x] 3.2 Forward metadata in `Pipeline._handle_stage_output` to the registry writer hook
- [x] 3.3 Implement non-regressing merge for `repo_pushed_at`/`repo_size_kb`/`file_commit_date` in `storage/registry.py` batched upsert (`COALESCE(excluded.col, links.col)` semantics) (date-extraction-S7, S8)

## 4. Metrics

- [x] 4.1 Add `date_fill_rate_api` / `date_fill_rate_web` counters (items with usable date ÷ items processed) and surface them in run stats/StatusManager output (date-extraction-S9) <!-- StatusManager rendering completed by task 7.2 (verification finding W2) -->

## 5. GREEN & verification

- [x] 5.1 Drive all automated tests to GREEN; update tests.md statuses
- [x] 5.2 Regression: rerun add-link-registry suite — write-only behavior and shard outputs unaffected
- [x] 5.3 Staging probe against live GitHub EXECUTED (September 2026): observed `date_fill_rate_api = 0.000` (20 items), `date_fill_rate_web = 0.000` (2 authenticated blob pages); root causes documented in design D7; 0.0/0.0 recorded as the documented baseline (not a failure) — original expectation "≈1.0" invalidated by probe

## 6. Documentation

- [x] 6.1 README + `docs/specs/registry_flags_metrics.md`: document transport asymmetry (API dates at search stage; web dates at gather stage only) and fill-rate metrics interpretation
- [x] 6.2 Note the MAX-bias rationale (overestimate → safe re-gather) next to the canon docs for future maintainers

## 7. Amnesty fixes (verification findings C1, W1–W3, S1–S2)

- [x] 7.1 W1+S1: add `threading.Lock` to `DateFillMetrics` (`core/metrics.py`); wrap `record_api`/`record_web`/rate computation; call `reset_date_parse_stats()` at metrics construction so module-level parse counters are per-run, not process-lifetime
- [x] 7.2 W2: render `Dates: api=<rate> (<n>/<m>), web=<rate> (<n>/<m>)` line in `state/display.py::_format_pipeline_section` when `status.pipeline.date_metrics` is non-empty (completes task 4.1)
- [x] 7.3 S2: make `_METADATA_SQL` UPDATE-only (drop the INSERT branch fabricating `first_seen_ts` for unknown `url_hash`); affected-rows==0 → once-per-kind debug counter (design D4 amended wording)
- [x] 7.4 W3: design.md D4 wording updated to "op-kind inside the existing batched writer" — doc-only, code retained as-is
- [x] 7.5 Capture LIVE fixtures with provenance headers (endpoint, capture date, auth mode): trimmed API search JSON page + client-rendered blob HTML page → `tests/fixtures/live_2026_09_*`; relabel existing synthetic fixtures as unit vectors (design D6)
- [x] 7.6 Author tests for date-extraction-S11/S12 against the live-captured fixtures — regression pins: pass immediately since degradation paths are already implemented (no RED phase expected; note in tests.md) <!-- one gap fixed for spec conformance: `extract_link_metadata` now counts `missing_date` when repository exposes no usable date field (spec: absent → NULL + counted warning); design D7.1 note amended -->
- [x] 7.7 Full suite green (existing 36 + new pins); update tests.md statuses; then openspec-verify-change → archive
