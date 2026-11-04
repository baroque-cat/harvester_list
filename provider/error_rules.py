#!/usr/bin/env python3

"""
Declarative error-parsing rules engine.

Evaluates per-task rule sets against provider HTTP responses to map specific
error codes/messages to ErrorReason values.  The engine is byte-neutral when
no rules are configured: callers must check for None/empty before invoking.

Design: JSON-parse the body ONCE, walk dot-paths (<=5 segments, dict-only),
regex-match, sort by priority DESC, FIRST match wins.  Any exception inside
evaluate() fails open (returns None) with a loud-once WARNING.
"""

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Tuple

from tools.logger import get_logger
from tools.patterns import redact_api_keys_in_text

logger = get_logger("provider")

# Caps — prevent fat-finger configs from blowing up RAM/CPU.
MAX_RULES_PER_TASK = 64
MAX_PATTERN_LEN = 512

# Loud-once guard for evaluate() exceptions (house pattern: client.py:43-57).
_evaluate_warning_emitted = False


def _emit_evaluate_warning(msg: str) -> None:
    """Emit a loud-once WARNING for evaluate() internal exceptions."""
    global _evaluate_warning_emitted
    if not _evaluate_warning_emitted:
        _evaluate_warning_emitted = True
        logger.warning(msg)


@dataclass(frozen=True)
class ErrorRule:
    """A single compiled error-parsing rule.

    Attributes:
        status_code: HTTP status to match; 0 = wildcard (any status).
        error_path: Dot-path into the JSON body, or "$" for raw body text.
        pattern_source: Original regex string from config (for diagnostics).
        pattern: Compiled regex pattern.
        map_to: Target ErrorReason value string.
        priority: Higher wins; ties broken by config order (stable sort).
        description: Human-readable description of the rule.
    """

    status_code: int
    error_path: str
    pattern_source: str
    pattern: "re.Pattern[str]"
    map_to: str
    priority: int
    description: str


def compile_rules(rule_configs: Sequence[Dict[str, Any]]) -> Tuple[ErrorRule, ...]:
    """Compile a sequence of rule config dicts into sorted ErrorRule tuples.

    Raises ValueError on invalid regex (with rule description for diagnostics).
    Sorts by priority DESC (stable — ties preserve config order).
    """
    rules = []
    for idx, cfg in enumerate(rule_configs):
        status_code = int(cfg.get("status_code", 0))
        error_path = str(cfg.get("error_path", "$"))
        pattern_source = str(cfg.get("match_pattern", ""))
        map_to = str(cfg.get("map_to", ""))
        priority = int(cfg.get("priority", 0))
        description = str(cfg.get("description", ""))

        # Compile regex — fail fast with rule identity in the message.
        try:
            compiled = re.compile(pattern_source)
        except re.error as e:
            rule_id = description or f"rule#{idx}"
            raise ValueError(
                f"invalid regex in error rule {rule_id!r}: {e}"
            ) from e

        rules.append(
            ErrorRule(
                status_code=status_code,
                error_path=error_path,
                pattern_source=pattern_source,
                pattern=compiled,
                map_to=map_to,
                priority=priority,
                description=description,
            )
        )

    # Stable sort by priority DESC — ties preserve original config order.
    rules.sort(key=lambda r: -r.priority)
    return tuple(rules)


def _resolve_dot_path(data: Dict[str, Any], path: str) -> Optional[str]:
    """Walk a dot-path (<=5 segments) into a dict; return str or None.

    Only dict indexing is supported.  Missing keys → None.
    Non-string values are coerced via str().
    """
    segments = path.split(".")
    if len(segments) > 5:
        return None

    current: Any = data
    for seg in segments:
        if not isinstance(current, dict):
            return None
        if seg not in current:
            return None
        current = current[seg]

    # Coerce to string if needed.
    if isinstance(current, str):
        return current
    return str(current)


def evaluate(
    rules: Tuple[ErrorRule, ...], code: int, body: str
) -> Optional[str]:
    """Evaluate rules against an HTTP status code and response body.

    Returns the map_to string of the first matching rule, or None if no rule
    matches.  Any internal exception fails open (returns None) with a
    loud-once WARNING — secrets/bodies are never logged.
    """
    if not rules:
        return None

    try:
        # Parse JSON body once — non-JSON → data=None (paths won't match).
        data: Optional[Dict[str, Any]] = None
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                data = parsed
        except (json.JSONDecodeError, ValueError):
            data = None

        for rule in rules:
            # Status code filter: 0 = wildcard.
            if rule.status_code != 0 and rule.status_code != code:
                continue

            # Resolve text to match against.
            if rule.error_path == "$":
                text = body
            else:
                if data is None:
                    continue
                text = _resolve_dot_path(data, rule.error_path)
                if text is None:
                    continue

            # Regex match — no forced flags (inline (?i) allowed).
            if rule.pattern.search(text):
                return rule.map_to

        return None

    except Exception as e:
        # Fail open — log a loud-once WARNING with no secrets/body.
        _emit_evaluate_warning(
            f"error_rules evaluate() exception ({type(e).__name__}); "
            f"failing open (no rule applied)"
        )
        return None
