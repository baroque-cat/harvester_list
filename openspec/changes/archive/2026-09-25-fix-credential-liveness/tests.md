# Test Plan — fix-credential-liveness

Derived mechanically from the delta specs under `specs/`. One spec scenario =
exactly one automated test case, or one Manual entry with a reason. Scenario
IDs are stable: never renumber, append only.

**RED baseline (measured 2026-09-25, before any implementation):**

```
python -m pytest tests/ -q --continue-on-collection-errors
→ 277 passed, 7 errors in 42.35s
```

- The 277 passes are the untouched pre-existing suite (including the R5.1 set);
  the same run with the six new `test_cl_*.py` files and the edited
  `test_fh_protections.py` excluded also gives **277 passed, 0 failed** — no
  collateral damage from planning.
- All 7 errors are collection-time `ImportError`s for symbols that do not exist
  yet (`tools.state.CredentialsExhausted`, `tools.state.credential_liveness_metrics`,
  `config.schemas.CredentialLivenessConfig`, `manager.task._probe_github_credentials`).
  Per house TDD doctrine the import failure IS the expected RED state; every
  scenario below additionally asserts real THEN-clause behavior once imports resolve.
- Test functions authored: `test_cl_config.py` 4, `test_cl_selector.py` 6,
  `test_cl_state.py` 3, `test_cl_detectors.py` 6 (3 parametrized: ×6/×3/×3 cases),
  `test_cl_stage_defer.py` 5, `test_cl_periphery.py` 4, `test_fh_protections.py` 2
  (1 deliberately replaced pin + 1 survivor).
- **Stale-test disposition (operator directive: delete/replace, never weaken):**
  `test_s16_full_pool_cooldown_waits_never_drops` pinned the pre-change
  unbounded-blocking contract that the `failure-handling` delta spec rewrites.
  It is REPLACED by `test_fh_s2_full_pool_cooldown_defers_never_drops` — part 1
  keeps the old within-budget assertions verbatim (waits, proceeds, never
  drops), part 2 pins the new beyond-budget typed exhaustion.
  `test_aux_backoff_schedule_unchanged` survives untouched (escalation schedule
  is unchanged by this change).

## Traceability

| Scenario ID | Spec | Requirement | Scenario | Test file | Status |
|-------------|------|-------------|----------|-----------|--------|
| `credential-liveness-S1` | `specs/credential-liveness/spec.md` | Bounded credential waits | Available credential returns immediately | `tests/test_cl_selector.py` | GREEN |
| `credential-liveness-S2` | `specs/credential-liveness/spec.md` | Bounded credential waits | Short cooldown is waited out within budget | `tests/test_cl_selector.py` | GREEN |
| `credential-liveness-S3` | `specs/credential-liveness/spec.md` | Bounded credential waits | Long cooldown exhausts the budget | `tests/test_cl_selector.py` | GREEN |
| `credential-liveness-S4` | `specs/credential-liveness/spec.md` | Bounded credential waits | Non-positive wait anomaly fails fast instead of spinning | `tests/test_cl_selector.py` | GREEN |
| `credential-liveness-S5` | `specs/credential-liveness/spec.md` | Bounded credential waits | Cross-section invariant enforced at load time | `tests/test_cl_config.py` | GREEN |
| `credential-liveness-S6` | `specs/credential-liveness/spec.md` | Bounded credential waits | Blocking rollback mode restores legacy behavior with a loud caveat | `tests/test_cl_selector.py` + `tests/test_cl_config.py` | GREEN |
| `credential-liveness-S7` | `specs/credential-liveness/spec.md` | Exhaustion defers tasks, never drops or empties them | Search task defers on credential exhaustion | `tests/test_cl_stage_defer.py` | GREEN |
| `credential-liveness-S8` | `specs/credential-liveness/spec.md` | Exhaustion defers tasks, never drops or empties them | Exhaustion never produces an empty result | `tests/test_cl_stage_defer.py` | GREEN |
| `credential-liveness-S9` | `specs/credential-liveness/spec.md` | Exhaustion defers tasks, never drops or empties them | Consecutive full-bench trips raise a bounded emergency | `tests/test_cl_selector.py` | GREEN |
| `credential-liveness-S10` | `specs/credential-liveness/spec.md` | Exhaustion defers tasks, never drops or empties them | Deferred backlog remains bounded by the age gate | `tests/test_cl_stage_defer.py` | GREEN |
| `credential-liveness-S11` | `specs/credential-liveness/spec.md` | Early cooldown release on success | Successful request frees a benched credential | `tests/test_cl_state.py` | GREEN |
| `credential-liveness-S12` | `specs/credential-liveness/spec.md` | Early cooldown release on success | Rollback flag restores expired-only release | `tests/test_cl_state.py` | GREEN |
| `credential-liveness-S13` | `specs/credential-liveness/spec.md` | Early cooldown release on success | Repeated limits still escalate | `tests/test_cl_state.py` | GREEN |
| `credential-liveness-S14` | `specs/credential-liveness/spec.md` | Primary and secondary limits react differently | Primary quota exhaustion cools one credential until reset | `tests/test_cl_detectors.py` | GREEN |
| `credential-liveness-S15` | `specs/credential-liveness/spec.md` | Primary and secondary limits react differently | Reset far in the future defers instead of sleeping | `tests/test_cl_stage_defer.py` | GREEN |
| `credential-liveness-S16` | `specs/credential-liveness/spec.md` | Primary and secondary limits react differently | Secondary marker cools the whole service pool and escalates loudly | `tests/test_cl_detectors.py` | GREEN |
| `credential-liveness-S17` | `specs/credential-liveness/spec.md` | Primary and secondary limits react differently | Ambiguous refusal never benches a credential | `tests/test_cl_detectors.py` | GREEN |
| `credential-liveness-S18` | `specs/credential-liveness/spec.md` | Narrowed soft-block content detectors | Polite fragment alone is not a limit | `tests/test_cl_detectors.py` | GREEN |
| `credential-liveness-S19` | `specs/credential-liveness/spec.md` | Narrowed soft-block content detectors | Verifiable markers still detected | `tests/test_cl_detectors.py` | GREEN |
| `credential-liveness-S20` | `specs/credential-liveness/spec.md` | Narrowed soft-block content detectors | Web search soft-block string preserved | `tests/test_cl_detectors.py` | GREEN |
| `credential-liveness-S21` | `specs/credential-liveness/spec.md` | Honest adaptive-rate configuration | Dead keys warn once and are ignored | `tests/test_cl_config.py` | GREEN |
| `credential-liveness-S22` | `specs/credential-liveness/spec.md` | Honest adaptive-rate configuration | Clean config loads silently | `tests/test_cl_config.py` | GREEN |
| `credential-liveness-S23` | `specs/credential-liveness/spec.md` | Credential liveness observability | Counters readable from status | `tests/test_cl_stage_defer.py` | GREEN |
| `credential-liveness-S24` | `specs/credential-liveness/spec.md` | Peripheral credential consumers stay live | Startup with an all-cooling pool boots | `tests/test_cl_periphery.py` | GREEN |
| `credential-liveness-S25` | `specs/credential-liveness/spec.md` | Peripheral credential consumers stay live | Enrichment yields silently on exhaustion | `tests/test_cl_periphery.py` | GREEN |
| `credential-liveness-S26` | `specs/credential-liveness/spec.md` | Peripheral credential consumers stay live | Rest-transport gather defers instead of failing unauthenticated | `tests/test_cl_periphery.py` | GREEN |
| `failure-handling-FH2-S1` | `specs/failure-handling/spec.md` | Existing protections are preserved | Cooldown rotation unchanged under the new contract | `tests/test_fh_stage_modes.py` (existing canonical rotation pins) + `tests/test_fh_protections.py::test_aux_backoff_schedule_unchanged` | GREEN (must survive) |
| `failure-handling-FH2-S2` | `specs/failure-handling/spec.md` | Existing protections are preserved | Full-pool cooldown defers, never drops | `tests/test_fh_protections.py::test_fh_s2_full_pool_cooldown_defers_never_drops` | GREEN |
| `failure-handling-FH2-S3` | `specs/failure-handling/spec.md` | Existing protections are preserved | Restart recovery unaffected | Manual (see below) | MANUAL |

¹ RED at collection time only (import driver); their assertions encode behavior
that exists today and MUST keep passing after the detector narrowing — they are
regression guards, expected to flip GREEN as soon as the module imports resolve.

## Automated

### File: `tests/test_cl_config.py`

Describe: credential-liveness configuration surface (schema defaults, loud
validation, cross-section invariant, dead-key acceptance period). The
`CredentialLivenessConfig` symbol does not exist yet — the import failure is
the expected RED state.

- [x] `credential-liveness-S5` — test_s5_cross_section_invariant_and_loud_defaults
- [x] `credential-liveness-S6` (config half) — test_s6_config_blocking_mode_warns_loudly_under_sqlite
- [x] `credential-liveness-S21` — test_s21_dead_keys_warn_once_and_are_ignored
- [x] `credential-liveness-S21` (per-load dedupe) — test_s21b_dead_keys_warn_once_per_load_across_all_blocks
      (APPENDED during verification, design D21: the first pin fed a single block, so it
      could not see the per-occurrence warning that a stale config with 4 task-level
      `rate_limit:` blocks would emit. Scenario ID unchanged — append only.)
- [x] `credential-liveness-S22` — test_s22_clean_config_loads_silently

### File: `tests/test_cl_selector.py`

Describe: bounded credential selector (both former infinite paths removed,
deadline computed before every sleep, one WARNING + one counter per episode,
emergency trips, blocking-mode parity). `credentials` module seams
(`_liveness_settings`) do not exist yet — import/attr failure is the RED state.

- [x] `credential-liveness-S1` — test_s1_available_credential_returns_immediately
- [x] `credential-liveness-S2` — test_s2_short_cooldown_waited_out_within_budget
- [x] `credential-liveness-S3` — test_s3_long_cooldown_exhausts_the_budget
- [x] `credential-liveness-S4` — test_s4_non_positive_wait_anomaly_fails_fast
- [x] `credential-liveness-S6` (behavior half) — test_s6_blocking_mode_legacy_parity
- [x] `credential-liveness-S9` — test_s9_consecutive_full_bench_trips_raise_bounded_emergency

### File: `tests/test_cl_state.py`

Describe: cooldown-state amendments (early release behind flag, rollback
parity, escalation ladder preserved). `_early_release_enabled` seam does not
exist yet — RED state.

- [x] `credential-liveness-S11` — test_s11_successful_request_frees_benched_credential
- [x] `credential-liveness-S12` — test_s12_rollback_flag_restores_expired_only_release
- [x] `credential-liveness-S13` — test_s13_repeated_limits_still_escalate

### File: `tests/test_cl_detectors.py`

Describe: primary/secondary limit reactions and narrowed content detectors.
`credential_liveness_metrics` import is the file-level RED driver; S14/S16
additionally need the `kind` attribute and whole-pool reaction that do not
exist yet. S17–S20 run against existing classifier entry points — S18 fails
pre-fix because the broad patterns still match; S19/S20 are regression guards.

- [x] `credential-liveness-S14` — test_s14_primary_quota_exhaustion_cools_one_credential_until_reset
- [x] `credential-liveness-S16` — test_s16_secondary_marker_cools_whole_service_pool_and_escalates_loudly
- [x] `credential-liveness-S17` — test_s17_ambiguous_refusal_never_benches_a_credential (parametrized ×6)
- [x] `credential-liveness-S18` — test_s18_polite_fragment_alone_is_not_a_limit (parametrized ×3)
- [x] `credential-liveness-S19` — test_s19_verifiable_markers_still_detected (parametrized ×3, guard)
- [x] `credential-liveness-S20` — test_s20_web_search_soft_block_string_preserved (guard)

### File: `tests/test_cl_stage_defer.py`

Describe: stage-level deferral routing (search rotation → RateLimitDeferral →
R5.1 defer seam on the durable sqlite backend), never-empty guarantee, age-gate
preservation, far-reset integration, `credential_metrics` surface. Uses the
SearchStage + durable-queue harnesses of `test_fh_stage_modes.py` /
`test_gt_stage_integration.py`. No network: auth fake, search entry points
sentinel-patched.

- [x] `credential-liveness-S7` — test_s7_search_task_defers_on_credential_exhaustion
- [x] `credential-liveness-S8` — test_s8_exhaustion_never_produces_an_empty_result
- [x] `credential-liveness-S10` — test_s10_deferral_never_extends_task_lifetime
- [x] `credential-liveness-S15` — test_s15_reset_far_in_the_future_defers_instead_of_sleeping
- [x] `credential-liveness-S23` — test_s23_counters_readable_from_status

### File: `tests/test_cl_periphery.py`

Describe: peripheral consumers (startup probe seam, repo_meta silent yield
without tokenless latch, rest-transport gather deferral). No network: HTTP
layer sentinel-patched.

- [x] `credential-liveness-S24` — test_s24_startup_probe_with_all_cooling_pool_boots (+ healthy-path companion test_s24b)
- [x] `credential-liveness-S25` — test_s25_enrichment_yields_silently_on_exhaustion
- [x] `credential-liveness-S26` — test_s26_rest_transport_gather_defers_instead_of_failing_unauthenticated
- [x] `credential-liveness-S24/S25/S26` (production chain) — test_s24c_exhaustion_propagates_through_the_production_auth_chain
      (APPENDED by the live gate, design D18: the real chain
      stage → `GithubAuthProvider` → `tools.coordinator.get_token` → `Credentials`
      swallowed the typed exhaustion into `None` at BOTH intermediate layers,
      which would have completed search tasks empty, tripped repo_meta's
      tokenless latch, and downgraded the rest transport to anonymous. Pin
      guards the propagation; scenario IDs unchanged — append only.)

### File: `tests/test_fh_protections.py` (edited in place)

Describe: preserved protections, amended by this change. The obsolete
unbounded-blocking pin is replaced (never weakened) per the `failure-handling`
delta spec; the backoff-schedule guard survives untouched.

- [x] `failure-handling-FH2-S2` — test_fh_s2_full_pool_cooldown_defers_never_drops
- [x] `failure-handling-FH2-S1` (aux half) — test_aux_backoff_schedule_unchanged (existing, GREEN, must survive)

## Manual

- [x] `failure-handling-FH2-S3` — Restart recovery unaffected: needs a full
  application boot/shutdown cycle over real queue files; carried as Manual
  since the original failure-handling change (its S17), and now folded into the
  consolidated live gate in `runbook.md` (restart step of the soak).
  **EXECUTED 2026-09-25** — twice: graceful restart over the soak workspace
  (`[gather] reclaimed 8 orphaned claimed task(s)` == in-flight gather threads,
  no loss) and a deliberate `kill -9` (`reclaimed 1` search + `8` gather ==
  in-flight, queue states intact across the kill). Evidence: `verification.md`
  §5.5 / §5.7, `/var/tmp/opencode-cl-gate/*/evidence/PHASE2.txt`, `PHASE3.txt`.
- [x] `credential-liveness-S6` (live half) — Blocking-mode rollback DRILL on the
  production-shaped sqlite workspace (flip `wait_mode: blocking`, restart,
  observe the loud WARNING and legacy behavior, flip back): requires a live
  run with real credentials; step 6 of `runbook.md`.
  **EXECUTED 2026-09-25** — `--validate` exit 0 with the loud
  duplicate-execution-hazard WARNING; 60 s blocking run exit 0 with
  `blocking_mode_active=1` rendered at shutdown; flipped back → 60 s run exit 0
  with `blocking_mode_active=0`. Evidence: `verification.md` §5.7.
- [ ] Organic secondary-limit observation (D5/S16 real-world confirmation): NEVER
  provoked; if a real secondary limit occurs during the soak, `runbook.md`
  step 5 records the captured body and verifies the whole-pool reaction,
  single ERROR, counter, and stage pause.
  **NOT OBSERVED 2026-09-25** — zero secondary/abuse events across all live runs
  (soak #1 600 s, soak #2, drills); the pool stayed healthy
  (`X-RateLimit-Remaining: 5000`, no 429/403 at all). Step 5 therefore did not
  apply; the secondary path stays evidenced by its fixture-based pins
  (S16) built on the live-captured header set (§2). Remains open by design -
  it can only be closed by a real organic window.
