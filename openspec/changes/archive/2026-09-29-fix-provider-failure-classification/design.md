# Design: fix-provider-failure-classification

## Context

See `proposal.md` — Why. The constraints that shape the approach:

- **Two seams already exist and are proven live.** `stage/base.py:662-719` implements a
  three-outcome worker contract: `RateLimitDeferral` → clamp (`_effective_defer_wait`) → optional
  stage pause → sleep → `defer_task` (identity preserved, nothing counted as an error); any other
  exception → retry policy → `attempts += 1` → requeue. R5.2 (`CredentialsExhausted`) and R5.3
  (`gather.defer_local_suppression`) both feed that seam. This change adds a third producer, not a
  fourth mechanism.
- **Invariant 6.** No sleep inside a claimed task may outlive the remaining visibility window
  (`queue.visibility_timeout_s` = 300, no claim-renewal API). `tools/retry.py::network_retry` already
  sleeps in the calling thread, so *any* new wait must be handed to the stage, not slept in
  `http_get`.
- **Invariant 1.** The basket is the provider. `check`'s baskets are per-provider
  (`get_service_name(task.provider)`, `base_rate 2.0`/`burst 10` each), unlike gather's single
  shared `github_raw` basket — this changes the pause calculus (D3).
- **Invariant 5 / RO-S4.** Policy is visible in config; a published `*_metrics` surface without a
  registered renderer fails the suite.
- **Three copies of the same wire-parsing logic already exist** in `search/client.py`
  (`:716-780` and `:1276-1327`). The defect is partly *divergence between copies*.
- **No provider error dialect has ever been captured.** The only probe evidence in this repository
  is a controlled probe of our own classifier (`127.0.0.1`, 2026-09-29, auth: none). Nothing may be
  hard-coded about `dashscope` / `dashscope-intl` / `maas.qwencloudapi` / `api.deepseek` bodies
  until captured (proposal, evidence row 3).

## Goals / Non-Goals

**Goals:**
- One recognition rule for "the remote told us to slow down", shared by the gather and provider
  surfaces, so the same signal can never yield two verdicts again.
- Every capacity refusal on the provider surface ends in exactly one of: a counted **deferral**, a
  counted **loud drop**, or a counted **answer** — never a silent `None` / `[]`.
- Per-class counters an operator can read during a run, with a renderer.
- A single-flag rollback that restores the legacy classification byte-for-byte.

**Non-Goals (design-level boundaries beyond the proposal's):**
- No change to `provider.check()` or any `_judge()` verdict mapping. The refusal taxonomy lives on
  the *transport* path (`http_get`, `_fetch_models`, `inspect`, basket starvation), never on the
  verdict path.
- No new retry/backoff policy. `network_retry`'s attempt count and interval are untouched; we only
  stop it from swallowing or hammering a typed signal.
- No provider-specific dialect table shipped in this change. Dialect rules arrive only as
  fixture-backed additions after the live probe (D11).
- No change to `stage/base.py`'s worker loop or to the `failure_handling` mode policy.

## Decisions

### D1 — One shared wire-signal module; `search/client.py` keeps delegating wrappers
New `tools/http_signals.py` exposing `is_capacity_refusal(status, text)`, `wait_from_headers(headers)`
and `wait_from_content(content)`. The three existing implementations become thin delegations, so
their private names, signatures and every existing pin keep working.
*Why:* the defect is divergence between copies; a fourth copy in `provider/` would institutionalise
it. *Alternatives rejected:* put it in `core/` — `core` has no HTTP dependency today and would gain
one for two regexes; leave the copies and add provider-local parsing — that is the status quo that
produced 403-as-auth on one surface and 403-as-limit on the other.

### D2 — Reuse `RateLimitDeferral` as the typed signal, extended additively
`core/exceptions.py:55` already carries `wait_s` and `stage_pause` and is already handled by the
worker loop. Extend `__init__` with `reason: Optional[str] = None` and `provider: Optional[str] = None`
(defaults preserve every existing call site and pin). `reason` takes an `ErrorReason` name
(`core/enums.py:51-73`) so counters are keyed by an existing vocabulary rather than a new one.
*Why:* zero new handler branches; the R5.2/R5.3 precedent means the deferral path is already
gate-proven (8 040 deferrals, `SUM(attempts)=0`). *Alternative rejected:* a new
`ProviderRefusal` exception — a richer name, but it would need a new branch in the worker loop and
in the `failure_handling` mode policy, and every stage that catches `RateLimitDeferral` today would
have to learn a second class.

### D3 — `stage_pause=False` for provider-basket starvation, and no in-worker sleep at all
`stage/definition.py:720-738` currently sleeps `wait_time` unclamped inside the claim and then
raises a failure-empty. Replacement: on a failed second `acquire`, raise
`RateLimitDeferral(wait_s=min(1/base_rate, cap), stage_pause=False, reason=RATE_LIMITED,
provider=<name>)` immediately — no sleep in the worker.
*Why `False`:* R5.3 chose `True` because eight gather workers share **one** basket. Here the basket
is **per provider** (invariant 1) and four check threads may be serving four different providers; a
stage-wide pause would idle three healthy providers to protect one starved basket. *Why no sleep:*
the sleep is the invariant-6 exposure, and it is pointless — the deferral already removes the task
from the claim, and the stage handler performs the (clamped) wait. *Alternative rejected:*
`stage_pause=True` for symmetry with R5.3 — wrong granularity, and it would measurably slow the
whole check stage.

### D4 — A published wait defers; an unpublished one keeps the legacy transient path
With the flag on, `http_get` raises the typed signal **only** when it can read a wait
(`Retry-After`, `X-RateLimit-Reset`, or a body marker). A 429 with no published wait stays a
retryable transient exactly as today.
*Why:* the measured harm is (a) 3 blind attempts against a server that told us when to come back
and (b) a 403-with-limit-text read as an auth failure and never retried at all. Both are fixed by
reading the wait. Turning *every* 429 into a deferral would change behavior we have no evidence is
harmful, and would move retries out of the decorator that exists to perform them. *Alternative
rejected:* defer on any 429 — larger blast radius, no measured benefit, and it would make the
rollback flag cover two behavior changes at once.

### D5 — The typed signal must be non-retryable and non-swallowable, explicitly
Two guards, both pinned: `tools/retry.py::should_retry_error` gains an explicit non-retryable class
check for `RateLimitDeferral` (today a message containing "rate limit" would make it retryable by
*text* — a fragile coupling this change must not inherit), and `@handle_exceptions` on
`_fetch_models` gains `exclude=(RateLimitDeferral,)`, using the parameter R5.2 already added for
exactly this purpose. No decorator rewrite.
*Why explicit:* relying on message wording to control retry behavior is how the current code
retries 429 and not 403. *Alternative rejected:* rewording the exception message to dodge the text
match — invisible to a reader and one log-message edit away from breaking.

### D6 — New surface `provider_refusal_metrics`, rendered as `ProviderRefusals:`
A declared key tuple with a loud `_stat_inc`-style guard (R5.3 D12 pattern): `refusals_rate_limit`,
`refusals_quota`, `refusals_auth`, `refusals_transient`, `deferred_provider_budget`,
`inspect_refused`, `inspect_empty_answers`. Added to `PipelineStatus`, wired in `manager/pipeline.py`,
registered in `state/display.py::_RENDERED_METRIC_SURFACES` with a `_format_provider_refusal_metrics_line`
static reading an explicit allowlist and degrading malformed figures fail-open.
*Why a new surface rather than extending `credential_metrics`:* that surface is the GitHub
credential pool's (R5.2) and its six keys are pinned by the D20 shutdown line; overloading it would
mix two pools in one line. *Naming:* `provider_refusal_metrics`, not `provider_metrics`, because
`state/display.py` already has `_format_provider_section` for the per-provider result table — a
near-identical name there would invite confusion.

### D7 — Rollback flag in a new `provider:` config section
`provider.classify_refusals: bool = True` and `provider.max_refusal_wait_s: float = 60.0`, mirroring
`gather:` exactly: parsed in `config/loader.py`, validated in both `GatherConfig`-style
`__post_init__` and `ConfigValidator`, non-boolean rejected loudly, and `max_refusal_wait_s`
required to be strictly below `queue.visibility_timeout_s` under `queue.backend: sqlite` (the same
check gather already has). `false` restores the legacy classification **including** the failure
report and the burned attempt.
*Why a new section:* the flag governs the provider surface, and stuffing it into `gather:` would
imply gather scope. *Alternative rejected:* a top-level key — the house pattern is one section per
concern with its own validator.

### D8 — `InspectStage` returns an outcome on every path
`_inspect_worker` keeps ERROR + `None` for an **unknown provider** (a configuration fault, not
capacity), but a refusal propagates as the typed signal (→ DEFER, counted `inspect_refused`) and a
genuine empty answer returns an output counted `inspect_empty_answers`.
*Why:* today the catch-all at `:933-935` makes "the provider refused" and "the task was handled"
indistinguishable in every counter, which is why Ф7 has no live occurrence count at all.

### D9 — The error path stops rendering the task object
`:933-935` logs `task: {task}` while `_generate_id` (`:886-895`) deliberately hashes the same
secret because "task ids are rendered in log lines". Switch to the hashed id; never log a token.
*Why:* `AGENTS.md` §3 and the R5.2/R5.3 gate hygiene both require masking; this line is a standing
leak risk in exactly the place we are about to make louder.

### D10 — `bedrock._send_request` stops rewriting 403 into 500
The GET path propagates the typed signal instead of returning `(500, str(e))`. Its `check()` path
(`:419-425`, a direct `request("POST")`) is untouched — it never went through `http_get`.

### D11 — No provider dialect is assumed; the probe produces fixtures
The shipped predicate recognises only: status 429; status 403 carrying the limit markers already
proven on the GitHub surface (`rate limit|abuse detection|secondary rate limit`); and a published
wait from headers or body. The first task probes the four configured providers with the smallest
request that answers the format question, and **any** additional dialect rule must land with its
captured fixture and its provenance (endpoint, date, auth mode) cited in `verification.md`.
*Why:* the artifact rule is "probe before spec — synthesized expectations are not evidence", and
this repository has been burned once already by assuming a third-party payload shape (the
date-extraction amnesty, September 2026).

### D12 — Harness determinism defects in the new tests, fixed before implementing (deviation)

Task 1.2 named one harness defect (`test_pfc_render.py::test_s12…`'s stub provider
was rejected by `InspectStage`'s `isinstance(provider, IProvider)` lookup, so the
test exercised the unknown-provider path instead of the error path). Fixing it
exposed two more of the same class in `tests/test_pfc_check_starve.py`, invisible
in the RED baseline because that file errored at collection:

1. `_RefusingProvider` did not subclass `IProvider` either (no `result`), so
   `_check_worker` returned at the provider lookup and never reached the basket.
2. `_starved_limiter` set `bucket.tokens = 0` but left `burst = 1` at
   `base_rate = 100`, i.e. one token per 10 ms. The real `TokenBucket.acquire`
   refills from `last_update`, so by the time a worker claimed the task the
   basket had a token and `check()` ran. The capacity is now pinned to `0`
   (`bucket.burst = 0`) while `rate` stays `100`, so the refill-derived wait is
   still ~10 ms but the starvation precondition is a fixture, not a race.
3. `test_fh4_s1…` asserted `tasks_deferred == 1`. An always-starved task is
   re-queued by `defer_task` and re-claimed immediately, and the worker's bounded
   sleep is not interruptible by `stop()`, so a second deferral can land while the
   stage joins. Relaxed to `>= 1`, matching the R5.3 gather precedent
   (`tests/test_tdm_stage_defer.py` pins `tasks_deferred >= 3`). Every semantic
   assertion — no failure-empty, no error, no requeue, no drop, no processed,
   identity and age preserved, empty registry — is unchanged.

*Why:* these are harness defects, not contract changes; the house rule is to fix
the test before implementing, and never to weaken an assertion to make it pass.
Only the count that measures scheduler timing was relaxed.

### D13 — The expected GREEN total is 377, not 371 (deviation)

`tasks.md` predicted 371 = 343 − 1 + 29, taking the RED collected count (29) as
the number of new tests. Two of the five new files were **collection errors**
under `--continue-on-collection-errors` (`test_pfc_config.py` failed at import on
the missing `ProviderConfig`; one `test_pfc_check_starve.py` case errored on a
missing fixture), so six tests were never counted. The new-file total is 35, and
343 − 1 + 35 = **377**, observed stable across three consecutive runs before and
after the gate. *Why it matters:* the arithmetic, not the coverage, was wrong —
the 17 spec scenarios are exactly the tests that turned green.

### D14 — `_check_worker` needs an explicit `except RateLimitDeferral: raise` (D3 consequence)

`CheckStage._check_worker` ended in `except TransientFetchError: raise` followed by
a catch-all `except Exception` that reports a limiter failure, logs and returns
`None`. `RateLimitDeferral` is a *sibling* of `TransientFetchError` (deliberately,
per its own docstring), so the new deferral fell into the catch-all: it was
reported to the adaptive budget as a failure and swallowed into a silent `None` —
reproducing the exact defect D3 exists to remove, one frame later. A propagation
branch was added ahead of the `TransientFetchError` one. *Why:* the seam in
`stage/base.py` can only honour a signal that reaches it; no classification
changed, and the legacy branch behind the flag is untouched.

### D15 — An all-zero refusal surface must render nothing (deviation, found by the live gate)

Gate row B2 requires the `ProviderRefusals:` line to be absent when a run has no
provider refusals. It was present from the first periodic block with every
counter at zero: `get_provider_refusal_stats()` returns the full declared key set
(zeros before any refusal), so the renderer's `if not metrics` guard can never
fire in a live process — only in a unit test that passes `{}` or omits the
attribute, which is exactly what the offline pins did. The renderer now computes
the declared figures first and returns `""` when all of them are zero, mirroring
the guard `_format_gather_transport_metrics_line` already had. Re-observed live
in B4: **0 of 11** periodic blocks rendered the line with the flag off while all
11 rendered `Gather:`, and **6 of 11** with the flag on (the first five pre-date
the first refusal). *Why it matters:* RO-S2's empty-surface rule is only
observable against a real all-zero surface, so no offline pin could catch it.

### D16 — Out-of-scope defect found by gate row B5: console logs are not redacted (reported, not fixed)

B5's scan found seven harvested `sk-…` keys in the 40 MB console capture, all
from the pre-existing `search/client.py::chat` error path, which interpolates the
full request `headers` dict. They reach **stdout in plaintext** because the
console handler is built with `ColoredFormatter` (`tools/logger.py:599-600`)
while the file handlers use `FileFormatterWithRedaction`; the `RedactionFilter`
is attached to the **root logger** (`:869-871`), which has no handlers, and
Python does not apply a logger's filters to records propagated from child
loggers. Verified live: one record is written to file as `Bearer sk-a1b...n4o5p6`
and to stdout in full. Operator credentials were **not** involved — the scan
found zero session/token hits outside the two run-root config copies, which were
deleted. *Why not fixed here:* this is the completion path, not the refusal path
that PRT-S12/D9 governs (that path is fixed and pinned), and fixing the logging
stack is a separate blast radius. Recommended follow-up: give the console handler
a redacting formatter, or attach `RedactionFilter` to handlers rather than to the
root logger.

## Risks / Trade-offs

- **[The typed signal reaches `_judge` and becomes a verdict]** → it is raised only on the transport
  path; `check()` and every `_judge()` are pinned unchanged, and the signal is not a subclass of
  anything `_judge` inspects (it maps codes and message text, not exception types).
- **[`network_retry` hammers or swallows the signal]** → D5's two explicit guards, each with its own
  pin; the text-based retryability of "rate limit" is replaced by a class check.
- **[`stage_pause=False` lets four threads churn one starved basket]** → churn is bounded by
  `base_rate` (the basket refills at 2.0/s), each deferral removes the task from the claim, and the
  churn ratio is a gate row (`defer/req`), measured the same way R5.3 measured gather's 3.19 → 0.93.
- **[Consolidating three parsers regresses gather]** → the 29 `gather-transport` scenarios stay
  green **untouched**; wrappers delegate rather than change behavior, and a pin asserts the gather
  classification of 403-with-limit-text is byte-identical before and after.
- **[Deleting an obsolete pin loses coverage]** → `test_s9_starved_check_requeues_instead_of_dropping`
  is replaced in the same pass by a stronger one (deferral, `SUM(attempts)` unchanged, identity and
  age preserved, counted separately), never merely removed.
- **[A provider whose refusal we cannot recognise stays silent]** → `inspect_empty_answers` counts
  the residual, so an unrecognised dialect shows up as an anomalous empty-answer rate instead of
  vanishing; the gate reads that counter.

## Migration Plan

1. Land the flag defaulting to `true` with the legacy path preserved verbatim behind `false`.
2. No data migration: a deferred task is a `pending` row in the durable queue; rows written by
   older versions are unaffected, and `attempts`/`created_at`/dedup identity are preserved by the
   existing `defer_task`.
3. Rollback = set `provider.classify_refusals: false` and restart. Both halves of the legacy
   behavior return together (classification **and** the burned attempt), so a half-rollback cannot
   keep the destructive half.
4. Promotion evidence: the gate rows in `runbook.md`, plus the organic-refusal capture that
   `AGENTS.md` §3 forbids us to provoke.

## Open Questions

None that would change the specs, the approach or the task breakdown. The provider dialect question
is deliberately deferred into D11's fixture-backed mechanism: the answer adds rules and fixtures, it
does not reshape the taxonomy.
