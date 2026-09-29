## Why

Four cheap, independent defects share one theme: **what a run writes to an operator-visible sink is not trustworthy.**

1. Console output is not redacted. Live gates R8 and R9 each found 7–8 harvested third-party `sk-` keys in a 40–56 MB `console.log`; under the shipped systemd unit that text goes to `journalctl`.
2. A daemon worker can still write to stdout while the interpreter finalizes it — a recovery run after `kill -9` died with `Fatal Python error: _enter_buffered_busy` and **exit 134** although queue state was byte-identical and reclaim was complete.
3. Every pytest run creates `logs/` in the repository root, and the absence of any pytest configuration also blocks reproducing a measurement at a parent commit outside the main tree.
4. `provider.max_refusal_wait_s` does not govern: the stage re-clamps a provider refusal with `gather.max_refusal_wait_s`, so raising the provider cap silently changes nothing.

Each one pollutes the evidence of the next large change (R3), whose gates are long soaks with large console captures and a `kill -9` drill.

## What Changes

- **One redaction layer at every sink.** Every formatter redacts the *final rendered line*, and fails **closed**. Today coverage is split across four formatters: `ColoredFormatter` (console) and `JSONFormatter` do not redact at all, `FileFormatterWithRedaction` redacts but fails **open** — on a redaction error it returns the original text, i.e. leaks the very secret it exists to hide.
- **The inert `RedactionFilter` attachment is removed.** It sits on the root logger, which sees nothing: loggers from `setup_logger` set `propagate = False`, and `Logger.callHandlers` walks ancestors for *handlers* only, never for *filters*. The semantics are pinned so the attachment cannot be re-added in the belief that it works.
- **The only known emitter stops emitting the secret.** `search/client.py:1862` interpolates the whole `headers` dict into a failure message; it keeps header **names** for diagnosis and drops the values.
- **Nothing is written to a sink after finalization begins**, and the run exits with an honest status instead of 134 on intact data.
- **Teardown noise is aggregated**: a discard after shutdown is counted and reported once per stage, not once per task.
- **The log directory becomes injectable** (`_setup_file_handlers` respects a preset instead of unconditionally assigning `Path("logs")`), and pytest is configured so a test run never writes into the repository's `logs/` and collects only `tests/`.
- **The refusal-wait cap is chosen by surface**: GitHub-facing stages (`search`, `gather`) keep `gather.max_refusal_wait_s`; provider-facing stages (`check`, `inspect`) use `provider.max_refusal_wait_s`. **No behaviour change under shipped defaults** — both are `60.0`.

No item is a breaking change; no configuration key is added, removed or renamed.

## Capabilities

### New Capabilities
- `run-output-integrity`: what a run may write to an operator-visible sink (stdout, log files) and when. Credential material is redacted at every sink by a layer that fails closed and is attached where it actually sees records; nothing is written after the run begins finalizing a sink; the run exits with an honest status; teardown noise is aggregated rather than per-record; a test run scopes its sink to its own workspace and never to the repository's.

### Modified Capabilities
- `provider-refusal-taxonomy`: Requirement 2, *"A published wait is handed to the stage, never slept in the transport"*. The requirement says a wait is clamped by "a configured cap"; in the shipped code two caps apply and the one that governs a provider refusal belongs to the **gather** section. The requirement is amended so the governing cap is the one belonging to the surface that produced the refusal — a configuration key that cannot govern is a lie in the operator's face, and invariant 5 says policy must be visible in configuration rather than implied.

## Impact

**Code**
- `tools/logger.py` — formatter redaction coverage and uniform fail-closed semantics; removal of the inert root-logger filter; suppression of writes after finalization begins; injectable logs directory.
- `search/client.py:1862` — `chat()` failure message no longer interpolates header values.
- `stage/base.py` — `_defer_wait_cap()` becomes surface-aware; `put_task` discard path aggregated; `stop()` marks teardown.
- `stage/definition.py` — `CheckStage` and `InspectStage` declare the provider surface.
- `tests/conftest.py` plus a new `pytest.ini` — test-run log isolation and collection scope.

**Not touched**
- `queue.backend`, the retry policy, the refusal-classification precedence (R9 D2), the metric renderers and `run-observability`, `RateLimitDeferral` and its transport producers, `check()`/`_judge()` and everything on the harvested-credential surface.

**Dependencies / systems**
- No new dependency. No wire-format assumption, so no live probe is required for the redaction or shutdown items; the credential material cited above comes from captures already taken (gate R8 row B5, 2026-09-28; gate R9 both runs, 2026-09-29).
- Prerequisite for installing `examples/harvester.service` (plan II.11.1): until console output is redacted, the unit's journal accumulates third-party live secrets from the first run.
- Unblocks the "measure at the parent commit before implementing" methodology that R3 requires (plan II.4, and lesson Р1 in `plan_races.md`).

**Rollback** — each item is independent and revertible on its own. The cap selection is behaviour-identical under shipped defaults, so reverting it changes nothing observable unless an operator has set the two caps apart.
