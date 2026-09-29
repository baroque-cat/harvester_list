# Design: fix-provider-refusal-counters

Predecessor: `openspec/changes/archive/2026-09-29-fix-provider-failure-classification/` (commit
`698bbf5`). Its decisions are cited below as **PFC-Dn** to keep the two numbering scopes apart; the
decisions in this file are **Dn** of this change.

## Context

The predecessor shipped the taxonomy but produced only four of its seven declared counters, and only
three of five shipped transports let the typed signal escape. Both gaps are against text that is
already normative, so this change is a conformance fix, not a redesign. The overriding constraint is
that the predecessor's behaviour was **proven live** (a 600 s soak at 8.53 req/s with zero stage
errors, plus a rollback drill and a `kill -9` durability drill). Nothing here may perturb it.

## Decisions

### D1 — Counting is strictly observational; no exception, message, retry count or deferral changes

The three missing producers are added as counter increments **only**. Every `raise` statement on
every path keeps its existing type, message and position, so the retry policy's decisions and the
number of wire hits are byte-identical to the shipped behaviour.

*Why:* the alternative — letting the new classes influence control flow — would re-open exactly the
question the predecessor settled with a live gate, and would invalidate its pins (PRT-S1…S5, S13) and
its measured soak. Observational counting delivers the whole benefit the requirement asks for (a
refusal is no longer indistinguishable from "no models") at zero behavioural risk.

*Rejected:* making `refusals_quota` suppress a deferral (a quota error cannot be waited out, so
deferring is arguably wrong). Rejected because it changes control flow on a path with **no captured
fixture** — the PFC-D11 live probe of all four production providers returned 401 `invalid_api_key`
only, and no quota refusal was ever observed on the wire. Inventing a control-flow rule from an
unobserved payload is precisely what PFC-D11 forbids. The conflict is resolved by precedence instead:
see D2.

*Pinned by:* PRT-S22 (identical exception type, message and wire-hit count before and after).

### D2 — One deterministic precedence, so "exactly one counted class" holds

Classification order inside the provider transport's HTTP-error handler, first match wins:

| # | Condition | Counter | Exception (unchanged) |
|---|---|---|---|
| 1 | capacity refusal **and** a published wait > 0 | `refusals_rate_limit` | `RateLimitDeferral(wait_s=min(published, cap))` |
| 2 | capacity refusal, no published wait, quota marker present | `refusals_quota` | `ConnectionError("Rate limit exceeded (HTTP {code})")` |
| 3 | capacity refusal, no published wait, no quota marker | `refusals_transient` | `ConnectionError("Rate limit exceeded (HTTP {code})")` |
| 4 | quota marker present (not a capacity refusal) | `refusals_quota` | falls through to the legacy branch for its status |
| 5 | `code in (401, 403)` | `refusals_auth` | `NetworkError("Authentication failed (HTTP {code})")` |
| 6 | `code >= 500` | `refusals_transient` | `ConnectionError("Server error (HTTP {code}): {reason}")` |
| 7 | anything else (404, 400, …) | *no refusal class* | unchanged |

Row 1 precedes rows 2–4 deliberately: a published wait is an instruction from the remote that waiting
will help, and honouring it is the capability's whole purpose. A quota marker on a response that also
publishes a wait is therefore counted as rate-limit, not quota. This makes the counter agree with the
action taken, which is what an operator reads the line for.

Row 4 keeps the existing exception for its status: a 403 carrying `exceeded_current_quota_error` and
no throttling marker still raises `NetworkError("Authentication failed (HTTP 403)")` — the *verdict*
is now visible in the counter even though the wire classification is untouched. Changing that
exception would break PRT-S3 and the predecessor's live-proven auth path.

*Rejected:* a quota-first precedence (rows 2/4 above row 1). It would produce the more precise label
for a hypothetical 429 carrying both signals, but it would also mean the counter says "quota" while
the system defers — a disagreement between the reported class and the observed action.

*Pinned by:* PRT-S18, PRT-S19, PRT-S20, PRT-S21.

### D3 — The quota vocabulary is transcribed from shipped `_judge` methods, not assumed

`QUOTA_MARKERS` in `tools/http_signals.py` is the union of the quota/billing markers already proven in
this codebase:

| Source | Markers taken |
|---|---|
| `provider/openai_like.py:123` (403) | `exceeded_current_quota_error`, `insufficient_user_quota`, `(额度\|余额)(不足\|过低)` |
| `provider/openai_like.py:127` (429) | `insufficient_quota`, `billing_not_active`, `欠费`, `请充值`, `recharge` |
| `provider/anthropic.py:151` | `credit balance is too low` |
| `provider/gemini.py:72` (429) | `Quota exceeded for quota metric` |

Deliberately **excluded**, with reasons:

- `quota` and `billing` as bare words (`provider/vertex.py:158`). They are safe in vertex's
  status-scoped 403 branch, where the alternative verdict is `NO_ACCESS`; at transport level a bare
  `quota` would match any body that merely mentions the word (a pricing page, a documentation string,
  an unrelated error message) and mislabel an authentication refusal.
- `Billing` and `purchase` as bare words (`provider/anthropic.py:151`), same reasoning.
- `RESOURCE_EXHAUSTED` (`provider/gemini.py:72`). On the Gemini surface this is the *exhaustion* code
  used for both quota and rate limit; at transport level a 429 carrying it is already a capacity
  refusal, so importing it under quota would steal rate-limit outcomes into the quota class.

*Why this satisfies PFC-D11 ("no rule without a fixture"):* these strings are not a newly assumed
provider dialect — they are the codebase's own existing recognition vocabulary, shipped and exercised
on the `check` surface. Transcribing them moves knowledge that is already in the repository into the
one shared place. No live quota fixture exists (the PFC-D11 probe observed only 401), so every quota
fixture in `tests.md` is labeled a **synthetic supplementary unit vector**, as the `tasks` rule
requires, and the rule is written as an opportunistic contract: marker present → count as quota;
marker absent → fall through untouched.

*Rejected:* leaving `refusals_quota` inert and narrowing the requirement text instead. Rejected
because the classes are the operator's whole diagnostic payload — "the provider is out of credit" and
"the provider throttled us" call for opposite responses (rotate credentials vs. slow down), and the
vocabulary to tell them apart already exists in five places in this repository.

### D4 — The new producers ride the existing rollback flag

All counting, new and old, is gated by `provider.classify_refusals`. With the flag false the
transport takes the legacy branches and increments nothing.

*Why:* PRT-S13 promises rollback restores the pre-capability behaviour byte-for-byte. A counter that
keeps moving after a rollback would be a half-rollback — the exact failure mode the requirement names.
No new flag is introduced: one switch, one meaning.

*Pinned by:* PRT-S23.

### D5 — Counting lives in the module-level `http_get`, which is the provider-only transport

Verified by call-site census: module-level `http_get` (`search/client.py:808`) is called only by
`provider/openai_like.py:143`, `provider/anthropic.py:174`, `provider/gemini.py:103`,
`provider/vertex.py:304,342`, `provider/bedrock.py:218` (and by tests). GitHub traffic goes through
the separate `GitHubClient._http_get` (`search/client.py:638-699`), which has its own `HTTPError`
handler and never calls the module-level function.

*Why this matters:* the counters are named `refusals_*` on a surface rendered as `ProviderRefusals`.
If GitHub responses reached them, a GitHub 401 would be counted as a provider authentication refusal
and the line would be meaningless. The census proves that cannot happen, and the fact is recorded
here so a future refactor that merges the two transports is a visible spec-breaking act rather than a
silent one.

*Rejected:* counting in `InspectStage` instead. The stage sees only the exception that survived the
provider wrapper — after `@handle_exceptions` that is `[]`, with the status code and body already
discarded. The class is only knowable where the wire response is.

### D6 — Signal escape is restored by re-raising before the broad handler, not by removing it

`except RateLimitDeferral: raise` is inserted immediately before the existing `except Exception` at
`provider/vertex.py:325`, `provider/vertex.py:353` and `provider/bedrock.py:493`.

*Why:* the broad handlers are the repository's fail-open contract ("parsers are strictly fail-open …
never an exception into worker loops", `openspec/config.yaml → context`). Removing them would change
every other failure mode on those transports. Re-raising one typed signal is the same surgical move
PFC-D10 already made in `bedrock._send_request` (`:240`) and PFC-D14 in `_check_worker`; it completes
D10 rather than revising it.

*Note on `gemini`:* no change needed — `http_get` at `provider/gemini.py:103` is outside any `try`,
and the `except:` at `:111` wraps only `json.loads`. The predecessor's claim was correct for `gemini`
and wrong for `vertex`; this change corrects the code and pins all five transports so the claim
becomes checked rather than asserted.

*Examined and deliberately not changed — `bedrock.check`.* Its broad handler (`provider/bedrock.py:438`)
looks like the same hole, and a `except RateLimitDeferral: raise` there was written and then removed as
**unreachable code**. `check` calls `_send_request("POST", …)`, and the POST branch never produces a
typed refusal: it calls `request()` directly rather than `http_get`, converts an `HTTPError` into a
`(code, message)` pair at `:233-234`, and its own inner `except Exception` at `:235-236` sits *inside*
the frame that PFC-D10's re-raise at `:240` guards. So the check surface classifies through
`_parse_response` → `_judge` → `CheckResult`, which is the boundary D11 states, not a gap in it.
Adding a clause that cannot execute would be exactly the kind of unpinned, unverifiable claim this
change exists to remove. The GET branch (`inspect`, `:470`) is the one that reaches `http_get`, and
that is the one now fixed.

*Rejected:* wrapping at the `InspectStage` level (catching the swallowed case after the fact). The
information needed to classify is already destroyed by then; and it would make the stage responsible
for every provider's internal error handling.

### D7 — One exported marker vocabulary; the GitHub-web-only marker stays local

`tools/http_signals.py` exports `LIMIT_MARKERS` and `QUOTA_MARKERS` as public compiled patterns.
`GitHubClient.is_rate_limited_content` (`search/client.py:615-636`) drops its private
`["rate limit", "secondary rate limit", "abuse detection"]` list and searches `LIMIT_MARKERS`
instead. `_LIMIT_MARKERS` is kept as a module-private alias so the existing internal references and
the predecessor's pins keep working.

The `SERVICE_TYPE_GITHUB_WEB` branch's `Search failed\. Please try again later\.` marker stays in
`search/client.py`. It answers a different question (is this HTML page a soft rate-limit page?) on a
different payload (rendered HTML, no status code), and it is not a throttling marker in the refusal
taxonomy's sense. Moving it into the shared vocabulary would widen `is_capacity_refusal` and change
refusal classification — a behaviour change this design forbids (D1).

*Behaviour check:* the three patterns being replaced are `rate limit`, `secondary rate limit`,
`abuse detection`; `secondary rate limit` is already subsumed by `rate limit`, so the alternation is
semantically identical and the credential-cooling verdicts pinned by `tests/test_cl_detectors.py:145,
163, 173` are unchanged. The JSON `message` extraction that precedes the match is untouched.

*Pinned by:* PRT-S16.

### D8 — The flaky harness had a real race, not merely an invisible symptom

`tests/test_gt_stage_integration.py::test_s18_a_deferred_task_is_never_executed_twice` failed roughly
one full-suite run in eight with `assert 0 >= 2` — the victim task never executed at all. Narrowing
the diagnosis found **two** harness defects, and the first is the cause:

1. **A race on the shared SQLite connection.** Both consumer threads read
   `stage.queue._conn.execute("SELECT COUNT(*) … state='claimed'")` directly, *without* holding the
   queue's condition, while the other consumer can be inside `_claim_locked()` on that same
   connection (`SqliteTaskQueue` opens it with `check_same_thread=False`, `storage/task_queue.py:108`,
   and every queue method serialises on `self._cond`). Concurrent use of one connection from two
   threads raises an unhandled `sqlite3` error at that line, which is outside any `try`, so the thread
   dies. If both die before the victim is claimed, `executions` never contains it and the assertion
   reports `0 >= 2` — with the real error visible only as a thread traceback on stderr.
   **Fix:** take the snapshot under `with stage.queue._cond:`.
2. **A stop condition that swallowed everything.** `stage.queue.get(timeout=0.2)` raises
   `queue.Empty` on timeout (`storage/task_queue.py::get`), but the harness caught bare `Exception`
   and returned, so any transient queue error ended the consumer silently. **Fix:** catch
   `queue.Empty` only; anything else propagates.
3. **An assertion inside a broad handler.** `assert stage.defer_task(t) is True` sat in the same
   `try` as `except Exception: pass`, and `AssertionError` *is* an `Exception`, so a failed deferral
   was dropped along with the task. **Fix:** capture the result and assert it after the handler.

*Why in this change:* the flake destroys the evidential value of a GREEN run (AGENTS.md §2 records the
same class of damage), and every re-run of the suite for this change would otherwise carry a ~5 %
chance of a false RED. The defect is **pre-existing and unrelated to the predecessor commit** —
attribution is proven in the proposal (0 changed lines in `AcquisitionStage`; `storage/task_queue.py`,
`stage/base.py`, `manager/queue.py`, `tests/conftest.py` and the test file byte-identical to
`20c15ce`) — so fixing it here is harness maintenance, not a behaviour change to product code. No
product code is touched, therefore no scenario is needed; the fix is verified by repeated runs
(30/30 targeted, 15/15 file-level, 5/5 full suite).

*Rejected:* marking the test `xfail`/`flaky`. That would hide a real defect and weaken a pin that
guards a durability property. Also rejected: giving each consumer its own connection, which would
change what the snapshot measures (the point is to observe claims *inside* the queue's serialisation).

### D9 — A thread-local refusal scope lets the stage tell a swallowed refusal from a genuine empty answer

Counting the class in the transport is necessary but not sufficient. The fail-open wrapper
`@handle_exceptions(default_result=[], exclude=(RateLimitDeferral,))` stays — it is the repository's
contract and removing it would turn every provider auth failure into a stage error — so a 401, a 503
or a quota refusal still reaches `InspectStage` as `[]`. With transport-only counting that outcome
advances `refusals_auth` **and** `inspect_empty_answers`: two counters for one outcome, and the
requirement's central sentence (*"A refusal SHALL never be recorded as 'the provider has no models'"*)
is still violated at the place an operator actually reads.

`search/client.py` therefore exposes a thread-local scope: `begin_refusal_scope()` before the provider
call and `end_refusal_scope()` after it, in a `finally` so it also clears on the raise path. Scopes
**nest** — the stage opens one around `provider.inspect` and the transport opens one inside it around
its own call — and `end_refusal_scope()` propagates the class it pops to the enclosing scope, so the
transport's answer is still readable by the stage that called it. `_provider_refusal_stat_inc` records
the class in the innermost open scope for the four `refusals_*` keys only (the `inspect_*` and
`deferred_*` keys are stage-level and must not pollute it). `InspectStage._inspect_worker` then routes
the outcome three ways: models present → an answer; models empty **and** a refusal class was counted →
`inspect_refused`; models empty and no refusal → `inspect_empty_answers`.

*Why thread-local:* the provider call is synchronous inside one worker thread, so the scope is exactly
as wide as the call and needs no lock. With `threads.inspect: 2` in the live config a counter-diff
approach would race between the two workers; carrying the class on the exception is impossible because
the wrapper discards the exception by design.

*Rejected:* also excluding `NetworkError`/`ConnectionError` from the wrapper (changes behaviour and
inflates `total_errors`, violating D1); counting nothing at the inspect dimension for a swallowed
refusal (violates "a worker SHALL never consume a task without recording an outcome"); widening
`IProvider.inspect` to return a richer type (five implementations plus four subclasses, for a fact the
transport already knows).

*On "exactly one counted class":* the counter set spans two dimensions — `refusals_*` is the **class**
of the refusal, established at the transport; `inspect_*` is the stage's **reading of the outcome**.
Exactly one counter advances per dimension, so a deferral legitimately advances `refusals_rate_limit`
and `inspect_refused` together. That is the shipped behaviour the predecessor's B1 soak measured
1:1:1, and PRT-S21 pins the per-dimension property rather than a global sum.

### D10 — One refusal per provider call, not per retry attempt (found during implementation)

The classification runs inside the `HTTPError` handler of the retried function, so a 503 that the
retry policy burns three attempts on classified three times and — with the counters incremented in
place — **counted three times**. `refusals_transient` would then be roughly three times the number of
refusals an operator actually experienced, and PRT-S21's "exactly one counted class" would hold per
attempt rather than per outcome. The requirement is written per *outcome*, so the implementation had
to be too.

The class therefore rides on the exception: `_tagged_refusal(error, refusal_class)` sets
`error.refusal_class` before the handler raises, and the counting moved out of the retried body into a
thin public `http_get` wrapper that increments once for whichever exception finally escapes. The
retried body became `_http_get_once` and is unchanged apart from the tagging.

*Why this shape:* the escaping exception **is** the call's outcome, so counting it needs no attempt
bookkeeping, no deduplication and no heuristic about which of several different classes seen across
attempts should win. It also keeps the wrapper's contract exact: with the flag off, or for a status
that is not a refusal, `refusal_class` is `None`, `_tagged_refusal` is a no-op and the wrapper adds
nothing but a `try/finally`.

*Rejected:* counting inside the handler and de-duplicating by thread-local (needs a scope boundary the
retried body cannot see, because `@network_retry` loops *inside* one call to the decorated function,
so consecutive attempts are sibling calls rather than nested ones); and counting per wire refusal
honestly, renaming the counters to say so (it would make `refusals_rate_limit` — never retried, always
1 — incomparable with `refusals_transient`, and the predecessor's B1 evidence established the 1:1:1
correspondence between counters and log lines that this would break).

*Measured effect* (`/tmp/opencode/prc_baseline.py`, before and after): every row keeps its exception
type, message and wire-hit count; the counter column goes from `{}` on seven rows to exactly one class
on each, `1` and not `3`, including the rows that take three attempts.

### D11 — The refusal surface is scoped to our own provider credentials

`refusals_*` counts what happens when **our** provider credentials are turned away, on the transport
that lists models (`http_get`). It deliberately does *not* count the outcome of testing a credential
harvested from a public repository, which is what the `check` surface does through `chat()`.

*Why:* in a normal run almost every harvested key is dead — that is the point of the harvester, and the
predecessor's B1 soak checked on the order of 400 of them in ten minutes. Folding those rejections
into `refusals_auth` would make the counter a measure of how many leaked keys are invalid and would
drown the one thing an operator needs from the `ProviderRefusals` line: *is our own pool being
refused?* The harvested-credential outcome is already classified where it belongs — `_judge` returns
`ErrorReason.INVALID_KEY` / `NO_QUOTA` / `RATE_LIMITED` / `NO_ACCESS`, which feeds
`credential_metrics`, the credential-liveness cooldowns and the adaptive budget.

This is a scope boundary, not a gap, so it is stated in the requirement text and pinned (PRT-S25)
rather than left to be rediscovered as a defect by the next verification pass.

*Rejected:* counting check-surface refusals too, on the argument that "every provider-call outcome"
includes them. Rejected because it makes the surface unusable for its stated purpose and double-counts
a fact the check surface already reports with finer granularity.

### D12 — Predecessor documentation is amended in place, with provenance

Two edits to the archived predecessor artifacts, both required by the `design` rule ("deviations …
are recorded as new numbered decisions … never silent edits contradicting shipped code"):

- **PFC-D17 (new)** records the 403-with-marker-and-no-published-wait deviation from predecessor
  task 4.1, its resolution in favour of PRT-S2, and the fact that it is now pinned by PRT-S15.
- **PFC-D6 (amended)** claimed the undeclared-counter guard rejects "exactly like `_gather_stat_inc`".
  It does not: `_gather_stat_inc` (`search/client.py:1107-1113`) logs a WARNING and returns, while
  `_provider_refusal_stat_inc` (`:1249-1256`) raises `ValueError`. The amendment states that the
  stricter behaviour is deliberate — an undeclared key is a programming error, not a degradation —
  which is what the existing pin `pytest.raises(ValueError, match="undeclared_key")` requires. The
  shipped code is correct; only the prose was wrong, so no code changes.

Both are appended/marked as amendments with this change's name as provenance rather than rewritten
silently.

## Risks

- **Counter noise on the auth path.** Every provider 401/403 now increments `refusals_auth`. With the
  live config's four providers this is the intended signal (the PFC-D11 probe produced exactly these),
  and the all-zero render guard (PFC-D15) keeps the line absent on a clean run.
- **Quota marker false positives.** Mitigated by D3's exclusion of bare `quota`/`billing`, and by D1:
  a false positive mislabels a counter and cannot change control flow.
- **`vertex`/`bedrock` now propagate a new exception type.** A deferral escaping `bedrock.inspect`
  reaches `InspectStage`, which already handles `RateLimitDeferral` (counts `inspect_refused` and
  re-raises) and the durable queue already defers on it. No new path is created; the two providers
  simply join the three that already behaved this way. Neither provider is used by the live config.

## Migration / rollback

No migration. Rollback is the existing flag: `provider.classify_refusals: false` restores the legacy
classification **and** silences every refusal counter (D4). The `vertex`/`bedrock` re-raises are not
flagged — they only affect a signal that the flag itself already suppresses at the source, so with
the flag off no `RateLimitDeferral` is ever raised and the new clauses are unreachable.
