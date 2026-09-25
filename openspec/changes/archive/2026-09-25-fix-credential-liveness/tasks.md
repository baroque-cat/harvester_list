# Tasks: fix-credential-liveness

TDD order. Every automated scenario in `tests.md` must be GREEN before archive.
Design decisions are cited as Dn (see `design.md`). The live gate (group 9)
executes `runbook.md` from this change directory — it is written at planning
time and consolidates THIS change's acceptance with the still-open R5.1
operator-gated gates 8.1/8.2/8.3 in a single production-shaped run.

## 1. RED baseline + early live wire probe

- [x] 1.1 Run `python -m pytest tests/ -q --continue-on-collection-errors` and confirm the measured baseline: `277 passed, 7 errors` — all 7 errors are collection-time `ImportError`s for not-yet-existing symbols (`CredentialsExhausted`, `credential_liveness_metrics`, `CredentialLivenessConfig`, `_probe_github_credentials`). Record verbatim in `verification.md` §1. Any OTHER failure means collateral damage — stop and fix the plan, not the suite.
- [x] 1.2 External-contract rule (house): re-probe the GitHub wire signals this change classifies, BEFORE coding against them. With a token read at runtime from git-ignored `.secrets`: `GET https://api.github.com/rate_limit` and one `GET` of a small public file via REST contents; capture response headers (`x-ratelimit-limit/remaining/reset/resource/used`, presence/absence of `Retry-After` on a normal 200) into `/tmp/opencode/probe_cl_headers.py` output; record endpoint, date, auth mode, and exact header casing in `verification.md` §2 (provenance style of R5.1 fixtures `live_2026_09_gt_*`). Confirm `_wait_from_headers` (`search/client.py:647-666`) parses exactly these names. DO NOT provoke a secondary limit — no live sample of its body exists; the classifier keeps matching only the markers the code already matches (D5 wire-format caveat), and runbook step 5 captures real bodies only if they occur organically.

## 2. Configuration surface (S5, S6-config, S21, S22)

- [x] 2.1 Add `CredentialLivenessConfig` to `config/schemas.py` (precedent `GatherConfig`): `wait_mode: str = "bounded"` (enum bounded|blocking), `max_wait_s: float = 60.0`, `early_release: bool = True`, `emergency_threshold: int = 3`; loud `__post_init__` validation (enum, positivity). Wire as `Config.credential_liveness` with loader parsing (`config/loader.py`). (D2)
- [x] 2.2 Extend `ConfigValidator` (`config/validator.py`, model: `_validate_gather_config` :533-558): re-check enum/positivity; cross-section invariant — under `queue.backend: sqlite` and `wait_mode: bounded`, reject `max_wait_s >= queue.visibility_timeout_s` with an error naming both keys (S5); under `wait_mode: blocking` + sqlite emit a loud WARNING that the duplicate-execution hazard is deliberately re-accepted, and skip the numeric invariant (S6-config). (D2)
- [x] 2.3 Remove the dead adaptive coefficients (D9): delete `calculate_adjusted_rate` and fields `backoff_factor`, `recovery_factor`, `max_rate_multiplier`, `min_rate_multiplier` from `RateLimitConfig` (`core/models.py:236-269`), their parsing (`config/loader.py:522-525`) and validation (`config/validator.py:579`). In `_parse_rate_limits`, accept-and-ignore unknown legacy keys with exactly ONE loud WARNING per key per load naming it as ignored (graceful acceptance period — live operator configs must not crash) (S21, S22).

## 3. State layer: tools/state.py (S11, S12, S13, S23-collector)

- [x] 3.1 Add `CredentialsExhausted(Exception)` (sibling of `GithubCredentialLimited`): fields `service`, `reason` (`budget_spent | spin_anomaly | all_cooling`), `wait_estimate_s` (may be None). (D1)
- [x] 3.2 Add `kind` to `Cooldown` (default `"primary"`); `mark_limited(..., kind="primary")` stores it; escalation schedule UNCHANGED (pinned by `test_aux_backoff_schedule_unchanged`). (D5)
- [x] 3.3 `mark_success`: when the `_early_release_enabled()` seam resolves true (reads `Config.credential_liveness.early_release`, default true, fail-open on missing config), pop the entry unconditionally and bump `early_releases`; when false, keep the expired-only pop byte-for-byte (S11, S12). A released credential's next `mark_limited` restarts at MIN (fresh `Cooldown`) — no ladder memory across a success (S13). (D4)
- [x] 3.4 Add the `credential_liveness_metrics()` collector (module-level counters + snapshot dict, keys exactly: `exhausted_episodes`, `deferred_by_credentials`, `secondary_limit_incidents`, `early_releases`, `emergency_trips`, `blocking_mode_active`) and internal increment helpers (S23). One log line per episode, degrade-once-per-kind house pattern.

## 4. Bounded selector: tools/credential.py (S1–S4, S6-behavior, S9)

- [x] 4.1 Add the `_liveness_settings()` seam resolving the effective `CredentialLivenessConfig` (global config when wired, built-in defaults otherwise — fail-open, so unit tests can monkeypatch it). (D2/D11)
- [x] 4.2 Rewrite `_get_available` around a deadline (D2 pseudocode): probe pool → return available credential with no sleep (S1); all-cooling → `wait = next_wait(...)`; `wait <= 0` or undeterminable → raise `CredentialsExhausted(reason="spin_anomaly")` immediately, no spin (S4); `remaining = deadline - now`, `remaining <= 0` → raise `CredentialsExhausted(reason="budget_spent", wait_estimate_s=wait)` (S3); else ONE `sleep(min(wait, remaining))`, then re-probe; release within budget returns the credential (S2). Total sleep NEVER exceeds `max_wait_s` (monotonic clock). Falsy return stays reserved for the unconfigured pool (D1).
- [x] 4.3 Episode accounting: exactly one WARNING per wait episode (masked credentials via `mask_credential`), `exhausted_episodes` bumped once per raise; `wait_mode: blocking` restores the legacy unbounded loop byte-for-byte (both former paths) and sets `blocking_mode_active` (S6-behavior). (D2)
- [x] 4.4 Emergency state: `emergency_threshold` consecutive exhaustion episodes on one service → one loud ERROR per trip + `emergency_trips` increment; pipeline keeps running (fail-open); a successful credential acquisition resets the consecutive counter (S9). (D7)

## 5. Classifiers and reactions: search/client.py (S14, S16, S17, S18, S19, S20)

- [x] 5.1 `mark_credential_limited` classification per the D5 table, using signals ALREADY parsed by `_extract_wait`/`_wait_from_headers`: secondary markers (`secondary rate limit`, `abuse detection` in body or reason) → `kind="secondary"`: cool EVERY credential of the service pool (enumerate via `self.resource_provider` tokens/sessions for the service), one ERROR per episode, `secondary_limit_incidents` increment, raise `GithubCredentialLimited` as today (the stage DEFER branch provides the bounded pause). Header-anchored primary (`remaining: 0` + usable reset, or `Retry-After`) → `kind="primary"`: cool ONLY the credential in use until the indicated reset, clamped by existing bounds (S14). No usable header + no marker + 403/429 → do NOT cool anything: route to the transient-failure path (existing `ConnectionError`/requeue taxonomy) (S17 ambiguous row). Never cool pools of OTHER services (D5).
- [x] 5.2 Narrow `is_rate_limited_content` (S18, S19, S20): API branch patterns reduce to `rate limit`, `secondary rate limit`, `abuse detection` (drop `please wait`, `try again later`); WEB branch keeps the exact sentence `Search failed\. Please try again later\.` untouched. (D6)
- [x] 5.3 Narrow `_is_http_rate_limited` (:632-634) to `code == 429 or (code == 403 and re.search(r"rate limit|abuse detection", ..., re.I))` — drop `please wait` (S17). Narrow the R5.1 gather-side classifier (:1101-1113) consistently to `rate limit|abuse detection|secondary rate limit`; `_gather_is_secondary` markers unchanged. (D6)

## 6. Stage + peripheral wiring (S7, S8, S10, S15, S24, S25, S26)

- [x] 6.1 `stage/definition.py` search rotation loops (`_execute_first_page_search` :245-275, `_execute_page_search` :286-315): catch `CredentialsExhausted` → raise `RateLimitDeferral(f"credentials exhausted ({reason}), service={service}")` — the existing R5.1 worker-loop DEFER branch (`stage/base.py:686-698`) does the rest: `defer_task` (attempts/created_at/dedup preserved, S10), `tasks_deferred` increment, bounded pause, no registry writes (S7); no StageOutput, `total_processed` untouched (S8). Keep `except GithubCredentialLimited` rotation (structural termination: each rotation cools a different credential → at most pool_size rotations → exhaustion; D7). `if not auth_token: return [], "", 0` stays ONLY for the unconfigured pool. Increment `deferred_by_credentials` at the raise site.
- [x] 6.2 `storage/repo_meta.py::_next_credential` (:484-495): catch `CredentialsExhausted` explicitly BEFORE the broad `except Exception` → same silent-yield path as all-cooling (`_note_cooling()`; return None) and do NOT trip the permanent tokenless latch (S25). Enrichment spec behavior preserved — no delta needed. (D8)
- [x] 6.3 `search/client.py::_gather_credential` (~:1185-1196): re-raise `CredentialsExhausted` before the `except Exception: pass` fallback; in `fetch_gather_content`'s rest path translate it into `RateLimitDeferral` (existing refusal taxonomy) — NO credential-less REST request (S26). (D8)
- [x] 6.4 `manager/task.py`: extract the startup capability probe (:415-425) into module-level `_probe_github_credentials() -> (has_token, has_session)`; catch `CredentialsExhausted` → `(False, False)`-style degraded answer with ONE WARNING; never sleep, never raise (S24). Call site behavior for healthy pools unchanged. (D8)
- [x] 6.5 Surface: add `credential_metrics: Dict[str, Any] = field(default_factory=dict)` to `PipelineStatus` (`core/metrics.py:186+`, refine_metrics precedent) and populate it from `credential_liveness_metrics()` wherever the pipeline assembles status (S23).

## 7. Docs, examples, live config

- [x] 7.1 `examples/config-full.yaml`: add the `credential_liveness:` section with honest comments; REMOVE the four dead coefficient keys everywhere; rewrite per-provider `rate_limit:` comments to describe the real model (`TokenBucket.adjust_rate`: ×0.5 after 3 consecutive failures floor 0.1×base; ×1.1 after 10 consecutive successes cap 2×base). README: credential-liveness section (bounded waits, DEFER-on-exhaustion, early release, primary vs secondary, rollback flags) + corrected adaptive-rate description. (D9/D2)
- [x] 7.2 Live `config.yaml` (git-ignored — backup FIRST to `/tmp/opencode/` with md5 recorded, promotion-round discipline): add explicit `credential_liveness:` section (defaults, visible per R0 doctrine); remove dead `rate_limit:` coefficient keys and their false comments. Verify `ConfigLoader("config.yaml").load()` passes with zero errors and record effective values in `verification.md`.

## 8. GREEN + validation

- [x] 8.1 Drive every automated scenario in `tests.md` to GREEN: `python -m pytest tests/ -q` → 0 failed, 0 errors; the pre-existing 277 stay passing (no tracked test weakened; the only replaced pin is `test_s16_full_pool_cooldown_waits_never_drops` → `test_fh_s2_full_pool_cooldown_defers_never_drops`, deliberate per the amended failure-handling requirement). Update `tests.md` Status column RED→GREEN and tick the scenario boxes.
- [x] 8.2 `openspec validate --change fix-credential-liveness` (and `--strict` if available) passes; record command output in `verification.md`.
- [x] 8.3 Write `verification.md` (evidence style of the archived R5.1/R2 verifications): §1 RED baseline verbatim, §2 live header probe with provenance, §3 GREEN suite numbers, §4 implementation deviations recorded as new numbered decisions/amendments in `design.md` — never silent edits.

## 9. Live gate — execute `runbook.md` (operator-gated)

- [x] 9.1 Execute `runbook.md` (this change directory) AFTER code+tests are green: the consolidated production-shaped live gate closing THIS change's acceptance AND R5.1's open gates 8.1 (full soak), 8.2 (rollback drill), 8.3 (secondary-limit accounting, organic only). Temp workspace under `/tmp`, credentials read at runtime from git-ignored `.secrets`/`config.yaml`, project `logs/` and `data/` untouched, nothing secret-bearing committed (tokens/cookies never in evidence, log excerpts redacted).
      DONE 2026-09-25 (steps 0–8, evidence under `/var/tmp/opencode-cl-gate/*/evidence/`).
      Two corrections to this line's assumptions, both recorded in `verification.md` §5:
      the workspace had to move `/tmp` → `/var/tmp` (a 600 s soak writes ~2.5 GB and
      `/tmp` is a 3.9 GB tmpfs — §5.1, `AGENTS.md` §1), and project `logs/*.log` is
      written by the **pytest** runs (git-ignored), never by a gate run; project `data/`
      mtime `1789245236` unchanged throughout (§5.9).
- [x] 9.2 Record every runbook measurement, command, and outcome in `verification.md` §5 (per-step accept/fail against the runbook's acceptance table). Any acceptance failure → amend code/specs as a new numbered decision, re-run affected steps; never edit shipped evidence.
      DONE 2026-09-25: `verification.md` §5 (5.1–5.11) records every run, command,
      counter and queue state. Gate verdict PASS: 12 of 14 acceptance rows PASS
      (A9 via a live probe on real credentials, not via the soak), A1/A2 NOT MEASURED
      (R5.1 transport metrics whose source dict is never rendered — O3/D20 follow-up).
      Four live findings were amended as new numbered decisions instead of touching
      shipped evidence: **D18** (auth chain swallowed the typed exhaustion — CRITICAL),
      **D19** (startup probe slept the whole budget), **D20** (counters were never
      rendered, so A9–A13 were unexecutable as written), **D21** (dead-key WARNING per
      occurrence rather than per load, contra S21). Runbook itself was amended for
      `/var/tmp` sizing and hardened launch snippets after the tmpfs incident (§5.1);
      the operating rule now lives in `AGENTS.md`.
- [x] 9.3 Handoff: update `plan.md` (R5.2 status, gate outcomes, next change R3 `add-aggregation-locality-v2`; governor-cap raises stay BLOCKED until this change + R5.1 gates are promoted); `openspec` sync-specs (new main spec `credential-liveness`, delta applied to `failure-handling`) and archive the change.
      DONE 2026-09-25: `plan.md` updated (new "Раунд реализации и живого гейта" entry; R5.1 gates 8.1/8.2 marked CLOSED and 8.3 left open by nature; R5.2 marked implemented+archived with implementation status and D12–D21; dependency chain ✅R5.1 → ✅R5.2 → R3; governor-cap paragraph now records that the formal condition is met but observation **O2** — gather saturating the local `github_raw` bucket at 8 threads — is a new precondition; итог rewritten). Specs synced: created `openspec/specs/credential-liveness/spec.md` (8 requirements / 26 scenarios) and applied the MODIFIED requirement to `openspec/specs/failure-handling/spec.md` ("Full-pool cooldown waits, never drops" → "defers, never drops"); `openspec validate --specs --strict` → 15/15 valid. Change archived to `openspec/changes/archive/2026-09-25-fix-credential-liveness/`.
