#!/usr/bin/env python3

"""
Q1 pins: declarative error-parsing engine (provider/error_rules.py).

Tests cover:
- Priority ordering (higher priority wins)
- Both body forms (error.code vs code — compatible vs native dashscope)
- "$" fallback on non-JSON body
- Dot-path: missing/non-string/nested
- status_code=0 wildcard
- No match → None
- Invalid regex in compile → ValueError
- Validator: bad map_to, >64 rules, pattern>512, status<0
- enabled=false / absence → inertness
- __post_init__ mirror
"""

import pytest

from config.schemas import ErrorParsingConfig, ErrorRuleConfig, TaskConfig
from provider.error_rules import (
    MAX_PATTERN_LEN,
    MAX_RULES_PER_TASK,
    ErrorRule,
    compile_rules,
    evaluate,
)


# --- Engine pins ---


class TestPriorityOrdering:
    """Higher priority rule wins over lower priority when both match."""

    def test_higher_priority_wins(self):
        rules = compile_rules(
            [
                {"status_code": 400, "error_path": "error.code", "match_pattern": "Arrearage", "map_to": "no_quota", "priority": 100, "description": "low"},
                {"status_code": 400, "error_path": "error.code", "match_pattern": "Arrearage", "map_to": "no_quota", "priority": 50, "description": "lower"},
            ]
        )
        # Both match, but priority 100 is first after sort.
        body = '{"error": {"code": "Arrearage", "message": "overdue"}}'
        result = evaluate(rules, 400, body)
        assert result == "no_quota"
        # Verify ordering: highest priority first.
        assert rules[0].priority == 100
        assert rules[1].priority == 50

    def test_conflicting_map_to_higher_wins(self):
        """Two rules match same body but map to different reasons — higher priority wins."""
        rules = compile_rules(
            [
                {"status_code": 400, "error_path": "error.code", "match_pattern": "Arrearage", "map_to": "bad_request", "priority": 10, "description": "low"},
                {"status_code": 400, "error_path": "error.code", "match_pattern": "Arrearage", "map_to": "no_quota", "priority": 100, "description": "high"},
            ]
        )
        body = '{"error": {"code": "Arrearage"}}'
        result = evaluate(rules, 400, body)
        assert result == "no_quota"


class TestBodyForms:
    """Both dashscope body forms: compatible-mode (error.code) and native (code)."""

    def test_compatible_mode_error_code(self):
        """Compatible-mode: {"error": {"code": "Arrearage"}} — path error.code."""
        rules = compile_rules(
            [{"status_code": 400, "error_path": "error.code", "match_pattern": "Arrearage", "map_to": "no_quota", "priority": 100, "description": "test"}]
        )
        body = '{"error": {"code": "Arrearage", "message": "overdue"}}'
        assert evaluate(rules, 400, body) == "no_quota"

    def test_native_mode_code(self):
        """Native mode: {"code": "Arrearage"} — path code."""
        rules = compile_rules(
            [{"status_code": 400, "error_path": "code", "match_pattern": "Arrearage", "map_to": "no_quota", "priority": 95, "description": "test"}]
        )
        body = '{"code": "Arrearage", "message": "overdue", "request_id": "abc"}'
        assert evaluate(rules, 400, body) == "no_quota"

    def test_compatible_mode_error_message(self):
        """Compatible-mode: error.message path."""
        rules = compile_rules(
            [{"status_code": 400, "error_path": "error.message", "match_pattern": "Access denied", "map_to": "no_access", "priority": 80, "description": "test"}]
        )
        body = '{"error": {"code": "AccessDenied", "message": "Access denied. Please check permissions."}}'
        assert evaluate(rules, 400, body) == "no_access"

    def test_native_mode_message(self):
        """Native mode: message path."""
        rules = compile_rules(
            [{"status_code": 400, "error_path": "message", "match_pattern": "Access denied", "map_to": "no_access", "priority": 65, "description": "test"}]
        )
        body = '{"code": "AccessDenied", "message": "Access denied."}'
        assert evaluate(rules, 400, body) == "no_access"


class TestDollarFallback:
    """$ path = raw body text; works on non-JSON bodies too."""

    def test_dollar_on_non_json_body(self):
        """Non-JSON body with $ path — regex matches raw text."""
        rules = compile_rules(
            [{"status_code": 400, "error_path": "$", "match_pattern": ".*", "map_to": "bad_request", "priority": 0, "description": "generic-400"}]
        )
        body = "<html>Bad Request</html>"
        assert evaluate(rules, 400, body) == "bad_request"

    def test_dollar_on_json_body(self):
        """$ path on JSON body — matches the full JSON string."""
        rules = compile_rules(
            [{"status_code": 429, "error_path": "$", "match_pattern": ".*", "map_to": "rate_limited", "priority": 0, "description": "generic-429"}]
        )
        body = '{"error": "rate limit exceeded"}'
        assert evaluate(rules, 429, body) == "rate_limited"


class TestDotPathEdgeCases:
    """Dot-path: missing/absent, non-string coercion, nested paths."""

    def test_missing_path_returns_none(self):
        """Absent dot-path → skip rule."""
        rules = compile_rules(
            [{"status_code": 400, "error_path": "error.code", "match_pattern": "Arrearage", "map_to": "no_quota", "priority": 100, "description": "test"}]
        )
        body = '{"error": {"message": "something else"}}'
        assert evaluate(rules, 400, body) is None

    def test_non_string_coerced(self):
        """Non-string value at path → str() coercion, then regex match."""
        rules = compile_rules(
            [{"status_code": 400, "error_path": "error.code", "match_pattern": "42", "map_to": "bad_request", "priority": 10, "description": "test"}]
        )
        body = '{"error": {"code": 42}}'
        assert evaluate(rules, 400, body) == "bad_request"

    def test_nested_path(self):
        """Nested dot-path (3 segments)."""
        rules = compile_rules(
            [{"status_code": 400, "error_path": "error.details.code", "match_pattern": "deep", "map_to": "bad_request", "priority": 10, "description": "test"}]
        )
        body = '{"error": {"details": {"code": "deep_error"}}}'
        assert evaluate(rules, 400, body) == "bad_request"

    def test_path_too_deep_returns_none(self):
        """Path with >5 segments → None."""
        rules = compile_rules(
            [{"status_code": 400, "error_path": "a.b.c.d.e.f", "match_pattern": "x", "map_to": "bad_request", "priority": 10, "description": "test"}]
        )
        body = '{"a": {"b": {"c": {"d": {"e": {"f": "x"}}}}}}'
        assert evaluate(rules, 400, body) is None

    def test_non_dict_intermediate_returns_none(self):
        """Intermediate non-dict value → None."""
        rules = compile_rules(
            [{"status_code": 400, "error_path": "error.code", "match_pattern": "x", "map_to": "bad_request", "priority": 10, "description": "test"}]
        )
        body = '{"error": "not_a_dict"}'
        assert evaluate(rules, 400, body) is None


class TestStatusCodeWildcard:
    """status_code=0 matches any HTTP status."""

    def test_wildcard_matches_any_status(self):
        rules = compile_rules(
            [{"status_code": 0, "error_path": "$", "match_pattern": "rate", "map_to": "rate_limited", "priority": 10, "description": "test"}]
        )
        assert evaluate(rules, 200, '{"data": "rate limited"}') == "rate_limited"
        assert evaluate(rules, 429, '{"data": "rate limited"}') == "rate_limited"
        assert evaluate(rules, 500, '{"data": "rate limited"}') == "rate_limited"

    def test_specific_status_does_not_match_other(self):
        rules = compile_rules(
            [{"status_code": 400, "error_path": "$", "match_pattern": "rate", "map_to": "rate_limited", "priority": 10, "description": "test"}]
        )
        assert evaluate(rules, 429, '{"data": "rate limited"}') is None


class TestNoMatch:
    """No matching rule → None."""

    def test_no_match_returns_none(self):
        rules = compile_rules(
            [{"status_code": 400, "error_path": "error.code", "match_pattern": "Arrearage", "map_to": "no_quota", "priority": 100, "description": "test"}]
        )
        body = '{"error": {"code": "InvalidParameter"}}'
        assert evaluate(rules, 400, body) is None

    def test_empty_rules_returns_none(self):
        assert evaluate((), 400, '{"error": {}}') is None

    def test_non_json_body_with_path_returns_none(self):
        """Non-JSON body + non-$ path → skip (data=None, path unresolvable)."""
        rules = compile_rules(
            [{"status_code": 400, "error_path": "error.code", "match_pattern": "Arrearage", "map_to": "no_quota", "priority": 100, "description": "test"}]
        )
        body = "not json at all"
        assert evaluate(rules, 400, body) is None


class TestCompileErrors:
    """Invalid regex in compile → ValueError."""

    def test_invalid_regex_raises_valueerror(self):
        with pytest.raises(ValueError, match="invalid regex"):
            compile_rules(
                [{"status_code": 400, "error_path": "$", "match_pattern": "[invalid", "map_to": "bad_request", "priority": 0, "description": "bad-rule"}]
            )

    def test_invalid_regex_message_includes_rule_name(self):
        with pytest.raises(ValueError, match="bad-rule"):
            compile_rules(
                [{"status_code": 400, "error_path": "$", "match_pattern": "(unclosed", "map_to": "bad_request", "priority": 0, "description": "bad-rule"}]
            )


# --- Validator pins ---


class TestValidatorPins:
    """Validator catches bad configs."""

    def test_invalid_map_to_is_error(self):
        from config.validator import ConfigValidator
        from config.schemas import Config

        config = Config()
        config.tasks = [
            TaskConfig(
                name="test-task",
                enabled=True,
                provider_type="openai_like",
                error_parsing=ErrorParsingConfig(
                    enabled=True,
                    rules=[
                        ErrorRuleConfig(
                            status_code=400,
                            error_path="$",
                            match_pattern=".*",
                            map_to="NOT_A_REAL_REASON",
                            priority=0,
                            description="bad-map",
                        )
                    ],
                ),
            )
        ]
        # Need at least one condition for _validate_task to not add other errors.
        from core.models import Condition, Patterns
        config.tasks[0].conditions = [Condition(patterns=Patterns(key_pattern="test"), query="test")]

        v = ConfigValidator()
        v._validate_error_parsing_config(config)
        assert any("NOT_A_REAL_REASON" in e for e in v.errors)

    def test_too_many_rules_is_error(self):
        from config.validator import ConfigValidator
        from config.schemas import Config

        config = Config()
        rules = [
            ErrorRuleConfig(status_code=0, error_path="$", match_pattern="x", map_to="unknown", priority=i, description=f"r{i}")
            for i in range(MAX_RULES_PER_TASK + 1)
        ]
        config.tasks = [
            TaskConfig(
                name="test-task",
                enabled=True,
                provider_type="openai_like",
                error_parsing=ErrorParsingConfig(enabled=True, rules=rules),
            )
        ]
        from core.models import Condition, Patterns
        config.tasks[0].conditions = [Condition(patterns=Patterns(key_pattern="test"), query="test")]

        v = ConfigValidator()
        v._validate_error_parsing_config(config)
        assert any("exceeds maximum" in e for e in v.errors)

    def test_pattern_too_long_is_error(self):
        from config.validator import ConfigValidator
        from config.schemas import Config

        config = Config()
        config.tasks = [
            TaskConfig(
                name="test-task",
                enabled=True,
                provider_type="openai_like",
                error_parsing=ErrorParsingConfig(
                    enabled=True,
                    rules=[
                        ErrorRuleConfig(
                            status_code=400,
                            error_path="$",
                            match_pattern="x" * (MAX_PATTERN_LEN + 1),
                            map_to="bad_request",
                            priority=0,
                            description="long-pattern",
                        )
                    ],
                ),
            )
        ]
        from core.models import Condition, Patterns
        config.tasks[0].conditions = [Condition(patterns=Patterns(key_pattern="test"), query="test")]

        v = ConfigValidator()
        v._validate_error_parsing_config(config)
        assert any("exceeds maximum" in e and "match_pattern" in e for e in v.errors)

    def test_negative_status_code_is_error(self):
        """ErrorRuleConfig(status_code=-1) → ValueError at schema level (mirrored by validator)."""
        # The __post_init__ catches it first (schema-level guard).
        with pytest.raises(ValueError, match="status_code must be a non-negative integer"):
            ErrorRuleConfig(
                status_code=-1,
                error_path="$",
                match_pattern=".*",
                map_to="bad_request",
                priority=0,
                description="neg-status",
            )


# --- Schema / inertness pins ---


class TestSchemaInertness:
    """enabled=false / absence → inert."""

    def test_absent_error_parsing_is_none(self):
        """TaskConfig without error_parsing → None (inert)."""
        tc = TaskConfig(name="test", provider_type="openai_like")
        assert tc.error_parsing is None

    def test_enabled_false_preserved(self):
        """ErrorParsingConfig(enabled=False) preserves the flag."""
        ep = ErrorParsingConfig(enabled=False, rules=[])
        assert ep.enabled is False

    def test_enabled_non_bool_raises(self):
        """ErrorParsingConfig(enabled="yes") → ValueError."""
        with pytest.raises(ValueError, match="enabled must be a boolean"):
            ErrorParsingConfig(enabled="yes")

    def test_post_init_coerces_dicts_to_rule_configs(self):
        """ErrorParsingConfig with raw dicts → coerced to ErrorRuleConfig."""
        ep = ErrorParsingConfig(
            enabled=True,
            rules=[{"status_code": 400, "error_path": "$", "match_pattern": ".*", "map_to": "bad_request", "priority": 0, "description": "test"}],
        )
        assert len(ep.rules) == 1
        assert isinstance(ep.rules[0], ErrorRuleConfig)
        assert ep.rules[0].status_code == 400

    def test_post_init_status_code_negative_raises(self):
        """ErrorRuleConfig(status_code=-1) → ValueError."""
        with pytest.raises(ValueError, match="status_code must be a non-negative integer"):
            ErrorRuleConfig(status_code=-1)
