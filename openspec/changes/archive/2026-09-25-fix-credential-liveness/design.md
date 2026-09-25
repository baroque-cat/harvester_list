# Design: fix-credential-liveness

## Context

See `proposal.md` — Why. Verified code map (all line numbers checked against HEAD `904f9af` + the 2026-09-25 promotion-round config edits):

- `tools/credential.py:126-154` — `_get_available`: `while True` (137), spin `if wait <= 0: time.sleep(0.1); continue` (145-147), unbounded `time.sleep(wait)` (150-154). No deadline, no episode counter, no per-episode single log.
- `tools/state.py` — `GithubCredentialLimited` (36-44), `mark_limited` escalation MIN 60 → ×2 → MAX 900 (61-78), `mark_success` pops only expired entries (80-89), `all_cooling` (112-115), `next_wait` (117-121). Singleton `github_credential_state` (132).
- `search/client.py` — `_limit` bounded token sleep (314-335); `get_with_headers`/`get_with_status` content-check → `mark_credential_limited` (511-525) → `_extract_wait` reads `Retry-After` + `X-RateLimit-Reset` (647-666) then collapses everything into one `mark_limited`; broad detectors `please wait` / `try again later` in `is_rate_limited_content` (527-550) and `_is_http_rate_limited` (632-634); `_gather_credential` (~1185-1196) resolves the rest-transport token under `except Exception: pass` → None.
- `stage/definition.py:246-315` — search rotation loops: `while True` → `get_token()`/`get_session()`; `if not auth_token: return [], "", 0` (None conflated with honest empty); `except GithubCredentialLimited` → rotate again, each rotation able to sleep inside the claim.
- `storage/repo_meta.py` — the good pattern: non-blocking probe `tokens_cooling_down()` (49-64) + silent yield; `_next_credential` (493) can still sleep if cooling starts between probe and call.
- `manager/task.py:417-418` — startup capability probe `has_token = get_token() is not None`.
- R5.1 DEFER machinery available for reuse: `RateLimitDeferral` (typed sibling of `TransientFetchError`, NOT a subclass), `stage.defer_task()` seam (bypasses the dedup gate, preserves `attempts`/`created_at`/`dedup_id`), worker-loop DEFER branch with `_defer_wait_cap()` bounded pause, `tasks_deferred` counter, no registry writes on defer, `max_age_hours` age-gate as ultimate ceiling.
- Spec conflict: main spec `failure-handling` requirement "Existing protections are preserved" pins "an all-credentials-cooling pool keeps blocking (waiting) rather than dropping tasks" with scenario "Full-pool cooldown waits, never drops". This change MODIFIES that scenario (delta spec) — the never-drops / never-completed-empty guarantees survive, unbounded blocking does not.

Constraints: stdlib only; parsers fail-open; behavioral changes ship behind flags with config-flip rollback; loud one-per-episode logging (degrade-once-per-kind house pattern); no in-task sleep may exceed `queue.visibility_timeout_s` (300 s) while `storage/task_queue.py` has no claim-renewal API.

## Goals / Non-Goals

**Goals:**
- Every sleep in the credential path has a deadline computed BEFORE the sleep, a named reason, an episode counter, and one log line per episode.
- Credential exhaustion becomes a third task outcome (DEFER) — not an infinite wait, not a drop, not a dishonest empty result.
- A proven-working credential is released from cooldown immediately; a false positive can no longer bench a healthy key for 900 s.
- Primary vs secondary limits produce different reactions from signals ALREADY read off the wire.
- Config stops lying: dead adaptive coefficients removed with a graceful acceptance period.

**Non-Goals:**
- Claim-renewal/heartbeat API for the sqlite queue (R5.1 D5 deferred it; the bounded-wait invariant makes it unnecessary for this layer).
- Cross-SERVICE subject escalation (secondary limits hit the subject, but a single global brake turns partial degradation into a full stop — Ф4; documented as follow-up candidate).
- Restoring `file_commit_date` extraction (separate task, plan Ф4/R5.1 D7).
- Changing `mark_limited`'s escalation schedule (60→120→240→480→900 on REPEATED failures stays; pinned by `test_aux_backoff_schedule_unchanged`).
- Touching aggregation, governor caps, or queue backends (cap raises stay blocked until this change + R5.1 gates pass).

## Decisions

### D1. Typed `CredentialsExhausted`, never `None`
New exception in `tools/state.py` (sibling of `GithubCredentialLimited`): `CredentialsExhausted(Exception)` with `service`, `reason` (`"all_cooling" | "spin_anomaly" | "budget_spent"`), `wait_estimate_s` (best-known time until the earliest release, may be `None`), `episode_id`. Raised by `_get_available` when the bounded budget is spent.
- **Why not return None:** `stage/definition.py:252-260` treats falsy token as `return [], "", 0` — an honest-empty result. A None-return would silently complete tasks empty (violates failure-handling "never completed-empty") or require touching every caller's None-semantics. A typed exception is catchable exactly where deferral is possible.
- `get_token()`/`get_session()`/`get_credential()` propagate it; `RuntimeError("No credentials available")` for the MISCONFIGURED pool (no credentials at all) stays as-is — exhaustion ≠ misconfiguration.

### D2. Bounded selector + `credential_liveness:` config section
`_get_available` rewritten around a deadline:
```
budget = config.credential_liveness.max_wait_s          # default 60.0
deadline = time.monotonic() + budget
loop: probe pool → available credential → return it
      all cooling → wait = next_wait(...)
      wait is None or wait <= 0 → raise CredentialsExhausted("spin_anomaly")   # D3
      remaining = deadline - now; remaining <= 0 → raise CredentialsExhausted("budget_spent")
      sleep min(wait, remaining) — single sleep, computed before, logged once (WARNING, masked creds, episode counter++)
```
Config (`config/schemas.py` `CredentialLivenessConfig`, loud `__post_init__` + `ConfigValidator`, precedent `GatherConfig`):
- `wait_mode: bounded | blocking` (default **bounded**; `blocking` = byte-for-byte legacy selector for rollback — house doctrine: rollback is a config flip, never code removal).
- `max_wait_s: float = 60.0` — cross-section validator invariant: under `queue.backend: sqlite`, `max_wait_s < queue.visibility_timeout_s` (exact pattern of the R5.1 D5 check in `config/validator.py:533-558`). Under `blocking` mode the invariant becomes a loud WARNING (rollback deliberately re-accepts the duplicate-execution hazard; operator must see it).
- `early_release: bool = true` (D4).
- `emergency_threshold: int = 3` (D7).
- Defaults rationale: 60 s matches gather's `max_refusal_wait_s` (measured: real `Retry-After`s in the R5.1 live gate were 7-60 s); anything longer than the visibility window is structurally forbidden anyway.

### D3. The `wait <= 0` spin becomes fail-fast exhaustion
Today: `if wait <= 0: time.sleep(0.1); continue` — an eternal 10 Hz spin when `next_wait()` returns non-positive while everything is cooling (state anomaly). New: raise `CredentialsExhausted("spin_anomaly")` immediately (one WARNING). Rejected alternative — keep spinning under the deadline: a 0.1 s hot loop for up to 60 s burns CPU to re-discover the same anomaly; the anomaly is a state bug, and DEFER surfaces it loudly instead of hiding it in a spin.

### D4. `mark_success` releases cooldown immediately, behind a flag
`mark_success(service, credential)`: pop the entry unconditionally (when `credential_liveness.early_release` is true), increment an `early_releases` counter, at most one INFO per (service, credential) per release. `early_release: false` restores the expired-only pop byte-for-byte.
- **Why:** a successful request is direct evidence the credential works. Today one false-positive from the broad detectors benches a healthy key for the full escalated term, and with 4 tokens the all-cooling state (hence D1/D2 exhaustion) becomes reachable in normal operation — rotation makes things worse, not better (plan Ф6).
- **Flap risk** (release → immediate re-limit → re-release): acceptable and self-limiting — each re-limit re-escalates `last_wait` only if the entry survived; a fresh entry restarts at MIN 60 s. The real mitigation is D6 (narrowed detectors): fewer false positives means fewer wrongful benches AND less flapping.

### D5. Primary vs secondary limit kinds, derived from signals already read
`GithubCredentialState.mark_limited(service, credential, wait=None, kind="primary")` gains `kind: "primary" | "secondary"` (stored on the `Cooldown`, surfaced in logs/metrics; escalation schedule unchanged).
`mark_credential_limited` classifies BEFORE calling it:
| Signal (already parsed) | Kind | Reaction |
|---|---|---|
| `X-RateLimit-Remaining: 0` + usable `X-RateLimit-Reset` | primary | cool THIS credential until reset (`wait = reset - now`, clamped to [MIN, MAX] as today); selector budget (D2) caps actual sleeping — the task defers instead of holding the claim |
| `Retry-After: N` present, N ≤ max_wait_s | primary | respect N as the cooldown wait |
| `Retry-After: N`, N > max_wait_s, or reset far in the future | primary | cool credential, do NOT sleep it out: exhaustion → DEFER (task returns when capacity returns) |
| body/reason matches `secondary rate limit` or `abuse detection` | **secondary** | cool the WHOLE service pool (`mark_limited` for every known credential of the service, wait = `Retry-After` if given else MIN), loud ERROR once per episode, `secondary_limit_incidents` counter, stage-level pause via the existing DEFER pause — stop generating traffic; pressure worsens subject-scoped limits |
| 403/429 with NO usable header and NO secondary marker | ambiguous | treat as transient task failure (existing bounded requeue, counted) — NOT a credential limit. Never bench keys on a guess |
- **Why not one global brake:** budgets are separate and separately observable (`core` 5000/h, `code_search` 10/min, `search` 30/min — measured from GitHub `/rate_limit` on 2026-09-25); a shared brake turns one tripped surface into a full stop (Ф4). Cross-service subject escalation stays a documented follow-up.
- Wire-format caveat (house rule): the secondary-marker phrases are the ones the code ALREADY matches (`is_rate_limited_content` patterns, `client.py:527-550`); no live-captured secondary-limit body exists in repo logs (grep of `logs/` finds test-run lines only). No NEW phrase is invented; the runbook's organic-observation step captures real bodies if they occur.

### D6. Narrowed content detectors
- `is_rate_limited_content` (API branch): remove `please wait` and `try again later`; keep `rate limit`, `secondary rate limit`, `abuse detection`. WEB branch keeps its exact `Search failed\. Please try again later\.` (a verified full-sentence GitHub string, not a fragment).
- `_is_http_rate_limited`: `code == 429 or (code == 403 and re.search(r"rate limit|abuse detection", reason))` — drop `please wait`.
- R5.1's gather-side classifier (`client.py:1101-1113`, `403 + rate limit|abuse detection|please wait|try again later`): narrow to `rate limit|abuse detection|secondary rate limit` for consistency; `_gather_is_secondary` markers unchanged.
- **Trade-off:** a real secondary limit wrapped in novel polite phrasing is missed → costs a bounded requeue cycle (counted), never a silent success. Missing a limit is cheaper than benching healthy keys on every polite 403.

### D7. Search stage routes exhaustion into the EXISTING R5.1 DEFER seam
`_execute_first_page_search` / `_execute_page_search` rotation loops:
- `except CredentialsExhausted` → raise `RateLimitDeferral(f"credentials exhausted ({e.reason}), service={e.service}")` — the worker-loop DEFER branch (R5.1) does the rest: `defer_task`, `tasks_deferred++`, bounded stage pause, no registry writes, attempts untouched. NO new stage machinery.
- `except GithubCredentialLimited` → keep rotating. Termination is now structural: each rotation can only cool a DIFFERENT credential (a success exits the loop; a repeated failure on the same credential escalates its cooldown), so at most `pool_size` rotations end in D1 exhaustion. No artificial rotation cap (a cap would drop tasks the pool could still serve after a pause).
- `if not auth_token: return [], "", 0` stays ONLY for the genuinely-unconfigured pool (get_token returns None when the pool is empty by configuration) — exhaustion now arrives as an exception and can never reach this branch.
- **Rejected alternative — repo_meta-style pre-probe in search:** enrichment may silently skip (its spec says so); search MUST NOT silently skip work — deferral is the honest outcome, and the probe would race the claim anyway.
- Emergency state: `emergency_threshold` consecutive CredentialsExhausted episodes on one service → loud ERROR (once per trip), `credential_emergency_trips` counter, surfaced on PipelineStatus. It does NOT stop the pipeline (fail-open doctrine: better defer forever within the age-gate than die); `max_age_hours` purge remains the ultimate ceiling with its existing loud accounting.

### D8. Peripheral callers
- `storage/repo_meta.py::_next_credential`: catch `CredentialsExhausted` → same path as "all cooling" today (`_note_cooling(); return None` → silent yield). Spec `repo-meta-enrichment` behavior ("silently self-disable when no usable token exists") is preserved — no delta needed.
- `search/client.py::_gather_credential` (rest transport): the `except Exception: pass` → None fallback must NOT swallow `CredentialsExhausted` (None silently downgrades the rest fetch to credential-less 401/403 churn). Re-raise it before the broad catch; `fetch_gather_content`'s rest path translates it into `RateLimitDeferral` (its refusal taxonomy already defers on limits).
- `manager/task.py:417-418` startup probe: wrap in try/except `CredentialsExhausted` → `has_token = False` with one WARNING. Startup must neither sleep nor crash; the probe asks "is a token configured and not cooling", not "wait for one".

### D9. Dead adaptive coefficients: REMOVE, do not revive
- Delete `calculate_adjusted_rate` and the four fields (`backoff_factor`, `recovery_factor`, `max_rate_multiplier`, `min_rate_multiplier`) from `RateLimitConfig` (`core/models.py:236-269`), their parsing (`config/loader.py:522-525`) and validation (`config/validator.py:579`).
- Loader acceptance period: unknown keys inside any `rate_limit:`/`ratelimits:` block emit ONE loud WARNING per key per load ("ignored; adaptive behavior is governed by TokenBucket.adjust_rate") and are dropped. Operator configs must not crash — `config.yaml` is live production and carries these keys today.
- Fix the lying comments in `config.yaml` + `examples/config-full.yaml` + README: the real model is `TokenBucket.adjust_rate` (`core/models.py:721-746`): ×0.5 after 3 consecutive failures (floor 0.1×base), ×1.1 after 10 consecutive successes (cap 2×base), per named bucket.
- **Why remove over revive:** reviving `calculate_adjusted_rate` (success-ratio based) would run TWO competing adaptive models over the same buckets and change live rates — a behavior change nobody measured. Removal makes config honest; the live model stays pinned by existing tests.

### D10. Observability: `credential_metrics` block on PipelineStatus
Follow the `refine_metrics`/`aggregation_metrics` precedent (not StageMetrics — episodes are pipeline-wide, not per-stage):
`credential_metrics = { exhausted_episodes, deferred_by_credentials, secondary_limit_incidents, early_releases, emergency_trips, blocking_mode_active }` — counters incremented at the episode sites (D2/D5/D7), surfaced read-only through PipelineStatus, zero new dependencies. `tasks_deferred` (R5.1, per-stage) remains the authoritative per-stage deferral count; `deferred_by_credentials` is the pipeline-wide attribution slice.

### D11. Test-file layout (for tests.md alignment)
New pins live in `tests/test_cl_*.py` (credential-liveness): `test_cl_config.py` (schema/validator/cross-invariant, dead-key acceptance), `test_cl_selector.py` (bounded waits, both former infinite paths, episode accounting, blocking-mode parity, emergency trips), `test_cl_state.py` (early release, rollback flag, escalation ladder preserved), `test_cl_detectors.py` (primary/secondary reactions, narrowed classifiers, ambiguous-text-as-transient), `test_cl_stage_defer.py` (search rotation → DEFER, never-empty, age-gate preservation, far-reset integration, `credential_metrics` surface), `test_cl_periphery.py` (startup probe, repo_meta silent yield without tokenless latch, rest-transport gather deferral). The obsolete pin `test_s16_full_pool_cooldown_waits_never_drops` is REPLACED inside `test_fh_protections.py` by `test_fh_s2_full_pool_cooldown_defers_never_drops` (it guards a failure-handling scenario that the delta spec rewrites — the replacement pin stays in that file next to its sibling protections; part 1 keeps the within-budget wait assertions verbatim, part 2 pins the beyond-budget typed exhaustion). `test_aux_backoff_schedule_unchanged` survives untouched.

## Risks / Trade-offs

- [Deferred tasks cycle under a long global limit] → age-gate `max_age_hours = 24` purges with loud accounting (existing); stage pause keeps the cycle cheap (no payload rewrite; 537 B/row measured); `deferred_by_credentials` makes cycling visible.
- [Early release + still-broad detectors could flap a credential] → detectors narrow in the SAME change (D6); flap is self-limiting (fresh entry restarts at MIN); `early_release: false` rollback.
- [Narrowed detectors miss a novel GitHub phrasing for a real limit] → failure mode is a counted bounded requeue, never a silent success; runbook organic-capture step records real bodies for future detector updates; never deliberately provoke (R5.1 gate 8.3 discipline).
- [Secondary reaction cools the whole pool → throughput drop] → intended (stop the pressure on a subject-scoped limit); loud ERROR + counter + emergency trips distinguish it from misconfiguration.
- [`wait_mode: blocking` rollback re-arms duplicate execution under sqlite] → validator emits a loud WARNING in exactly that combination; drill covered in the runbook.
- [Search DEFER under memory backend (no durable defer history)] → `defer_task` seam is backend-agnostic (R5.1 pins exist for both); memory rollback re-accepts pre-R2 durability, unchanged doctrine.
- [Startup probe behavior change breaks deployments with all-cooling pools] → probe now fails fast with a WARNING instead of hanging boot — strictly better; pinned by a test.

## Migration Plan

1. Ship code + tests behind config defaults (`wait_mode: bounded`, `early_release: true`) — defaults are the NEW safe behavior (precedent: R1 shipped governor `on` by default because ungoverned was proven harmful; here unbounded sleep under sqlite is proven duplicate-execution).
2. Executor adds an explicit `credential_liveness:` section to live `config.yaml` (git-ignored; backup first — R0/promotion-round discipline) and removes dead `rate_limit:` keys from it and from `examples/config-full.yaml`.
3. Live gate: run `runbook.md` (group 9) — consolidated soak closing THIS change's acceptance AND R5.1's open gates 8.1/8.2/8.3 in one production-shaped run.
4. Rollback: `credential_liveness.wait_mode: blocking` and/or `credential_liveness.early_release: false` — config flips, no code removal. Detector narrowing and dead-coefficient removal are not flag-gated (they cannot cause unbounded waits; their worst case is a counted requeue).

## Open Questions

- Should secondary-limit pool cooling use `Retry-After` when GitHub provides it on 403 (it sometimes does)? — resolved conservatively in D5 (use it when present, else MIN); if the runbook's organic capture shows different real bodies, amend as a new numbered decision, never a silent edit.

## Implementation deviations (append-only, never silent edits)

### D12. Break the config↔tools circular import so the config logger is captureable
The plan assumed the existing lazy `get_logger("config")` calls; pytest's caplog only
attaches its handler to non-propagating loggers that already exist at test setup, so the
lazily-created `config` logger could not be observed by `test_cl_config.py` S6/S21. Root
cause: `tools/coordinator.py` imported `from config import get_config` at module level,
closing the cycle `config/__init__ → config.loader → (tools) → config`. Fix: move that
import inside `ResourceManager.__init__` (its only use), and make `config/validator.py`
and `config/loader.py` create `logger = get_logger("config")` at module import. No
behavior change; S6/S21 now capture. (Files: `tools/coordinator.py`, `config/validator.py`,
`config/loader.py`.)

### D13. `_github_client` defaults to a credential-less `GitHubClient()`
`test_cl_periphery.py` S26 injects a raising provider via
`monkeypatch.setattr(search_client._github_client, "resource_provider", ...)`, which
requires the module global to be an object (it was `None` before `init_github_client`).
A default `GitHubClient()` has `resource_provider=None`, so every production path behaves
exactly as before (gather stays anonymous until a provider is wired); only the injectable
seam changes. `init_github_client` still replaces it.

### D14. `_search_worker` re-raises `RateLimitDeferral`
The D7 translation is raised from inside `_execute_first_page_search` /
`_execute_page_search`. Its credential acquisition runs before the search call, and the
worker's catch-all (`except Exception → return None`) would otherwise swallow the
deferral and silently complete the task empty. Added the explicit
`except RateLimitDeferral: raise` before the transient/catch-all branches (mirrors the
acquisition worker's existing guard).

### D15. Bounded-wait accounting is cumulative, not wall-clock
The selector tracks `spent += sleep_for` and raises once `budget - spent <= 0`, instead of
re-reading a monotonic deadline. Requirement: total in-task sleep must never exceed
`max_wait_s` even when `time.sleep` is a no-op (the S15 integration test patches sleep but
not the clock). Equivalent under a real clock, and strictly safer under a stalled/patched
one. Waits log exactly once per episode (flag, not per iteration), matching S2.

### D16. CLI command spelling
This OpenSpec build does not accept `validate --change <name>`; the working commands are
`openspec validate <name> --type change` and `... --strict`. Used verbatim in
`verification.md` §3.

### D17. Emergency trip semantics
One loud ERROR and one `emergency_trips` increment per *ongoing* emergency (the first
episode whose consecutive streak reaches the threshold); further episodes in the same
uninterrupted streak keep raising but do not re-trip. Any successful credential
acquisition resets the streak and re-arms the trip. Matches S9's "exactly one ERROR per
trip".

### D18. Typed exhaustion must survive the production auth chain (LIVE-GATE FINDING)
Found by the live gate (task 9.1), not by the unit suite: production wires stages to
`core.auth.GithubAuthProvider`, whose providers are `tools.coordinator.get_token/get_session`.
BOTH layers wrapped the call in `except Exception: return None`:

- `tools/coordinator.py::ResourceManager.get_token/get_session`
- `core/auth.py::GithubAuthProvider.get_token/get_session`

So `CredentialsExhausted` never reached `stage/definition.py`; the stage saw a falsy token and
took the `return [], "", 0` branch — a **silently completed-empty search task**, exactly the
outcome the `credential-liveness` and `failure-handling` specs forbid (S8). The same swallow
would (a) hand `storage/repo_meta.py::_next_credential` a `None` and trip its **permanent
tokenless latch** (violating S25, since exhaustion is transient), and (b) make
`search/client.py::_gather_credential` fall back to an **anonymous REST request** guaranteed to
fail (violating S26).

**Fix:** both layers now re-raise `CredentialsExhausted` before the broad catch
(`except CredentialsExhausted: raise` in the coordinator; a lazy-import
`_is_typed_exhaustion()` guard in `core/auth.py`, lazy because `core` must not import `tools`
at module level: `core/__init__ → core.auth → tools.state → tools/__init__ → config →
core.models` closes a cycle). `None` again means only "no credential configured".
Pinned by `tests/test_cl_periphery.py::test_s24c_exhaustion_propagates_through_the_production_auth_chain`
(appended under the existing S24/S25/S26 rows — scenario IDs are append-only).

**Why the unit suite missed it:** every automated scenario injects a fake auth/provider that
raises directly, so the intermediate swallow layers were never exercised. Lesson recorded: the
live gate is the only place the real wiring is assembled.

### D19. The startup capability probe must never wait out a cooldown (LIVE-GATE FINDING)
Also found by the live gate. With D18 fixed, `_probe_github_credentials()` reached the real
bounded selector - and the selector *waited the full budget* before reporting exhaustion
(measured live: `sleeps=[2.0]` with `max_wait_s: 2.0`, i.e. **60 s at the production value**).
Spec `credential-liveness-S24` requires startup to complete with at most one WARNING and
"no startup sleep occurs"; task 6.4 says "never sleep, never raise".

**Fix:** `tools/credential.py` gained a zero-budget override seam:

- `_resolve_liveness_settings()` - the old config/default resolution, unchanged semantics.
- `_settings_override` + `_FailFastSettings(base)` - `wait_mode="bounded"`, `max_wait_s=0.0`,
  other fields inherited. Pinning `bounded` matters: under a `blocking` rollback config the
  probe would otherwise block boot forever.
- `fail_fast_liveness()` context manager installs/removes the override (module-level, single
  thread at startup).
- `_liveness_settings()` returns the override when set, else resolves from config. Tests that
  monkeypatch `_liveness_settings` wholesale are unaffected.

`manager/task.py::_probe_github_credentials()` now wraps its two calls in
`with fail_fast_liveness():`. With a zero budget the selector raises `budget_spent` immediately
(`wait_estimate_s` still carries the real cooldown), so the probe is instantaneous, still
returns `(False, False)` with exactly one WARNING, and the episode is still counted in
`exhausted_episodes` (honest: the pool *is* exhausted).

Pinned by `tests/test_cl_periphery.py::test_s24d_capability_probe_never_waits_out_a_cooldown`.

### D20. Credential counters rendered at shutdown (live-gate evidenceability)
The spec's observability requirement is satisfied programmatically (`PipelineStatus.credential_metrics`,
S23). But the runbook's acceptance rows A9-A13 cite `credential_metrics` as their evidence
source, and **nothing in the app ever prints it** - `main.py`'s final status logs only runtime
and monitoring stats, and `state/display.py` renders just the stage table (the same is true of
`refine_metrics` / `aggregation_metrics` / `gather_transport_metrics`, a pre-existing pattern).
A real run therefore cannot evidence those rows.

**Fix (minimal, additive):** `tools/state.py::credential_metrics_summary()` renders the snapshot
as `key=value, ...` in collector declaration order, and `main.py`'s shutdown block logs one
`Credential liveness: ...` INFO line (wrapped in a defensive try/except so observability can
never break shutdown). No spec change: the required surface stays `PipelineStatus`; this is the
operator-facing rendering of the same data.
Pinned by `tests/test_cl_stage_defer.py::test_s23b_credential_metrics_summary_renders_every_counter`.

### D21. Dead-key WARNING deduped per load, not per occurrence (VERIFICATION FINDING)
Found while verifying requirement `Honest adaptive-rate configuration` against the code.
Spec S21: "each present key produces **exactly one** WARNING naming it as ignored" and the
requirement text says "exactly one loud WARNING **per key per load**". The implementation
warned once per key **per rate_limit block**: `_warn_dead_rate_limit_keys()` is called from
both `_parse_rate_limits()` (every global bucket) and `_parse_task_config()` (every task-level
`rate_limit:` block). The pre-fix live `config.yaml` carried those keys in 4 task blocks, i.e.
an operator with a stale config would see 4 WARNINGs per key (16 lines) per load - more than
the spec promises. The existing S21 pin could not catch it: it feeds a single block.

**Fix:** per-loader dedupe set `self._warned_dead_keys`, reset at the top of `load()` so the
"per load" semantics survive repeated loads on one instance, and never process-global (a fresh
loader warns again). Keys remain ignored with no behavioural effect either way.
Pinned by `tests/test_cl_config.py::test_s21b_dead_keys_warn_once_per_load_across_all_blocks`
(two global blocks + one task-level block in a single load → one WARNING per key; fresh loader
→ warns again).
