"""L3-rest (wave 6, slice 6.0) — contextual ``user_session=`` redaction.

Canon: 02_critical_fixes.md §2.4 note 1 (2026-10-29 audit): the real L3 rest
is ONLY the ``user_session=`` contextual pattern in the shared dictionary
(``tools/patterns.py``) plus a dictionary pin.  All other secret shapes
(AIza/sk-/sk-proj-/anthropic/gsk_/stab_/ghp_{36}/github_pat_/gho_{36}) were
already covered and pinned (test_roi_redaction S1..S6).

Why this matters: GitHub web sessions are full-account cookies consumed as
``Cookie: user_session={session}`` (search/client.py).  A dead/expired session
produces sign-in pages whose diagnostics historically interpolated header
values; every sink (five log formatters via ``_redact_line``, purge/restore/
maintain receipts via ``redact_api_keys_in_text``) shares ONE dictionary, so a
single contextual pattern closes all of them.

The credential below is SYNTHETIC and shaped like a session cookie; no real
material is used, logged or asserted on (house precedent: test_roi_redaction).

Expected RED at creation (pattern absent from the dictionary):
* P1 dictionary pin — no ``user_session=`` entry in API_KEY_PATTERNS;
* P2 functional — Cookie-header form publishes the raw value;
* P3 sink chain — _redact_line (all formatters delegate to it) publishes it;
* P5 contextual variants — query-param / JSON-escaped forms publish it.
P4 (no false positives) is insurance: passes before and after.
"""

from __future__ import annotations

import logging

from tools.patterns import API_KEY_PATTERNS, redact_api_keys_in_text

# Synthetic, obviously-fake value; 39 chars of [A-Za-z0-9_] after '='.
FAKE_SESSION = "SYNTHETIC_TEST_VALUE_0000000000abcdefg"
COOKIE_LINE = f"Cookie: user_session={FAKE_SESSION}; path=/; HttpOnly"


def test_p1_dictionary_contains_user_session_pattern() -> None:
    """P1 (dictionary pin): the shared dictionary carries a contextual
    ``user_session=`` pattern (canon: 02 §2.4 L3-rest)."""
    assert any(
        "user_session=" in p for p in API_KEY_PATTERNS
    ), f"no contextual user_session= pattern in dictionary: {API_KEY_PATTERNS}"


def test_p2_cookie_header_value_is_masked() -> None:
    """P2: redact_api_keys_in_text masks the session VALUE in Cookie-header
    form; the raw value must not survive in any substring."""
    out = redact_api_keys_in_text(COOKIE_LINE)
    assert FAKE_SESSION not in out, f"raw session value survived: {out!r}"
    # Context stays recognizable for operators (key name prefix survives in
    # some masked form), but the entropy body is gone.
    assert "SYNTHETIC_TEST" not in out, f"value body survived: {out!r}"


def test_p3_sink_chain_redact_line_masks_session() -> None:
    """P3: the sink chain used by ALL five formatters (_redact_line ->
    redact_api_keys_in_text) masks the session value."""
    import tools.logger as lg

    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1,
        msg=COOKIE_LINE, args=None, exc_info=None,
    )
    out = lg._redact_line(COOKIE_LINE, record)
    assert FAKE_SESSION not in out, f"raw session value survived sink chain: {out!r}"


def test_p4_no_false_positives() -> None:
    """P4 (insurance): unrelated text and uncontextualized alphanumerics are
    NOT altered by the new contextual pattern."""
    plain = "ordinary log line with numbers 12345678901234567890 and words"
    assert redact_api_keys_in_text(plain) == plain
    # A bare 48-char token WITHOUT the user_session= context must be untouched
    # by THIS pattern (other dictionary patterns may still apply to sk-/ghp_
    # shapes; this one is purely alphanumeric with no key prefix).
    bare = "value=AbCdEf0123456789AbCdEf0123456789AbCdEf0123456789 end"
    assert redact_api_keys_in_text(bare) == bare


def test_p5_contextual_variants_are_masked() -> None:
    """P5: query-param and JSON-escaped forms of user_session= are masked
    (the pattern is contextual on the key name, not on the wrapper)."""
    variants = [
        f"https://github.com/search?q=x&user_session={FAKE_SESSION}&p=2",
        f'{{"headers": {{\\"Cookie\\": \\"user_session={FAKE_SESSION}\\"}}}}',
        f"user_session={FAKE_SESSION}",
    ]
    for text in variants:
        out = redact_api_keys_in_text(text)
        assert FAKE_SESSION not in out, f"variant leaked: {text!r} -> {out!r}"
