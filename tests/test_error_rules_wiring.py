#!/usr/bin/env python3

"""
Q2 pins: error-parsing wiring (base.py hook + manager/task.py plumbing).

Tests cover:
- Real OpenAILikeProvider with rules in extras, mock chat seam
- Arrearage (400, both forms) → NO_QUOTA
- InvalidApiKey (400) → INVALID_KEY
- Access denied (400 message) → NO_ACCESS
- APIConnectionError (400) → NETWORK_ERROR
- Throttling.AllocationQuota (400) → RATE_LIMITED
- Generic 400 → BAD_REQUEST (rule 8)
- 200 healthy → success (regression)
- Without rules → byte-identical to old _judge (regression)
- Masking: CheckResult.message doesn't contain synthetic secret
- Plumbing: TaskConfig(error_parsing=...) → kwargs["error_rules"] present
"""

import json
from unittest.mock import patch

import pytest

from core.enums import ErrorReason
from core.models import CheckResult, Condition, Patterns
from config.schemas import (
    ApiConfig,
    ErrorParsingConfig,
    ErrorRuleConfig,
    StageConfig,
    StorageConfig,
    TaskConfig,
)
from provider.error_rules import compile_rules
from provider.openai_like import OpenAILikeProvider


@pytest.fixture(autouse=True)
def _mock_user_agent():
    """Mock get_user_agent to avoid config dependency in tests."""
    with patch("provider.openai_like.get_user_agent", return_value="test-agent"):
        yield


# --- Helpers ---

# Minimal qwen-style rule set for testing (subset of the full preset).
_QWEN_RULES = compile_rules(
    [
        {"status_code": 400, "error_path": "error.code", "match_pattern": "Arrearage", "map_to": "no_quota", "priority": 100, "description": "arrearage-compat"},
        {"status_code": 400, "error_path": "error.code", "match_pattern": "InvalidApiKey", "map_to": "invalid_key", "priority": 90, "description": "invalid-key-compat"},
        {"status_code": 400, "error_path": "error.message", "match_pattern": "Access denied", "map_to": "no_access", "priority": 80, "description": "access-denied-msg"},
        {"status_code": 400, "error_path": "code", "match_pattern": "Arrearage", "map_to": "no_quota", "priority": 95, "description": "arrearage-native"},
        {"status_code": 400, "error_path": "code", "match_pattern": "InvalidApiKey", "map_to": "invalid_key", "priority": 85, "description": "invalid-key-native"},
        {"status_code": 400, "error_path": "code", "match_pattern": "APIConnectionError", "map_to": "network_error", "priority": 75, "description": "conn-error"},
        {"status_code": 400, "error_path": "message", "match_pattern": "Access denied", "map_to": "no_access", "priority": 65, "description": "access-denied-native"},
        {"status_code": 400, "error_path": "error.code", "match_pattern": "(?i)Throttling\\.AllocationQuota", "map_to": "rate_limited", "priority": 88, "description": "throttle-compat"},
        {"status_code": 400, "error_path": "$", "match_pattern": ".*", "map_to": "bad_request", "priority": 0, "description": "generic-400"},
        {"status_code": 429, "error_path": "$", "match_pattern": ".*", "map_to": "rate_limited", "priority": 0, "description": "generic-429"},
    ]
)


def _make_provider(rules=None):
    """Create an OpenAILikeProvider with optional error_rules in extras."""
    conditions = [Condition(patterns=Patterns(key_pattern="sk-[a-z]+"), query="test")]
    kwargs = {
        "name": "test-qwen",
        "base_url": "https://dashscope.example.com/compatible-mode/v1",
        "default_model": "qwen-turbo",
    }
    if rules is not None:
        kwargs["error_rules"] = rules
    return OpenAILikeProvider(conditions=conditions, **kwargs)


# --- Wiring tests ---


class TestWiringWithRules:
    """Provider with rules in extras: rules intercept before _judge."""

    def test_arrearage_compatible_mode(self):
        """400 + error.code=Arrearage → NO_QUOTA."""
        provider = _make_provider(_QWEN_RULES)
        body = json.dumps({"error": {"code": "Arrearage", "message": "overdue", "type": "billing"}})
        with patch("provider.base.chat", return_value=(400, body)):
            result = provider.check(token="sk-test1234567890")
        assert result.error_reason == ErrorReason.NO_QUOTA
        assert result.status_code == 400

    def test_arrearage_native_mode(self):
        """400 + code=Arrearage (native) → NO_QUOTA."""
        provider = _make_provider(_QWEN_RULES)
        body = json.dumps({"code": "Arrearage", "message": "overdue", "request_id": "abc"})
        with patch("provider.base.chat", return_value=(400, body)):
            result = provider.check(token="sk-test1234567890")
        assert result.error_reason == ErrorReason.NO_QUOTA

    def test_invalid_api_key_compatible(self):
        """400 + error.code=InvalidApiKey → INVALID_KEY."""
        provider = _make_provider(_QWEN_RULES)
        body = json.dumps({"error": {"code": "InvalidApiKey", "message": "bad key"}})
        with patch("provider.base.chat", return_value=(400, body)):
            result = provider.check(token="sk-test1234567890")
        assert result.error_reason == ErrorReason.INVALID_KEY

    def test_access_denied_message(self):
        """400 + error.message contains 'Access denied' → NO_ACCESS."""
        provider = _make_provider(_QWEN_RULES)
        body = json.dumps({"error": {"code": "AccessDenied", "message": "Access denied. Check permissions."}})
        with patch("provider.base.chat", return_value=(400, body)):
            result = provider.check(token="sk-test1234567890")
        assert result.error_reason == ErrorReason.NO_ACCESS

    def test_api_connection_error(self):
        """400 + code=APIConnectionError → NETWORK_ERROR."""
        provider = _make_provider(_QWEN_RULES)
        body = json.dumps({"code": "APIConnectionError", "message": "connection failed"})
        with patch("provider.base.chat", return_value=(400, body)):
            result = provider.check(token="sk-test1234567890")
        assert result.error_reason == ErrorReason.NETWORK_ERROR

    def test_throttling_allocation_quota(self):
        """400 + error.code=Throttling.AllocationQuota → RATE_LIMITED."""
        provider = _make_provider(_QWEN_RULES)
        body = json.dumps({"error": {"code": "Throttling.AllocationQuota", "message": "quota exceeded"}})
        with patch("provider.base.chat", return_value=(400, body)):
            result = provider.check(token="sk-test1234567890")
        assert result.error_reason == ErrorReason.RATE_LIMITED

    def test_generic_400_fallback(self):
        """400 + unrecognized body → BAD_REQUEST (rule 8, generic fallback)."""
        provider = _make_provider(_QWEN_RULES)
        body = json.dumps({"error": {"code": "SomeUnknownError", "message": "something"}})
        with patch("provider.base.chat", return_value=(400, body)):
            result = provider.check(token="sk-test1234567890")
        assert result.error_reason == ErrorReason.BAD_REQUEST

    def test_200_healthy_success(self):
        """200 + valid JSON response → success (regression)."""
        provider = _make_provider(_QWEN_RULES)
        body = json.dumps({"id": "chatcmpl-123", "choices": []})
        with patch("provider.base.chat", return_value=(200, body)):
            result = provider.check(token="sk-test1234567890")
        assert result.available is True

    def test_429_generic_rate_limited(self):
        """429 + any body → RATE_LIMITED (rule 9)."""
        provider = _make_provider(_QWEN_RULES)
        body = json.dumps({"error": "rate limit exceeded"})
        with patch("provider.base.chat", return_value=(429, body)):
            result = provider.check(token="sk-test1234567890")
        assert result.error_reason == ErrorReason.RATE_LIMITED


class TestWiringWithoutRules:
    """Without rules → byte-identical to old _judge behavior (regression)."""

    def test_400_without_rules_is_bad_request(self):
        """No rules: 400 → BAD_REQUEST (legacy _judge)."""
        provider = _make_provider(rules=None)
        body = json.dumps({"error": {"code": "Arrearage"}})
        with patch("provider.base.chat", return_value=(400, body)):
            result = provider.check(token="sk-test1234567890")
        assert result.error_reason == ErrorReason.BAD_REQUEST

    def test_401_without_rules_is_invalid_key(self):
        """No rules: 401 → INVALID_KEY (legacy _judge)."""
        provider = _make_provider(rules=None)
        with patch("provider.base.chat", return_value=(401, '{"error": "unauthorized"}')):
            result = provider.check(token="sk-test1234567890")
        assert result.error_reason == ErrorReason.INVALID_KEY

    def test_429_without_rules_is_rate_limited(self):
        """No rules: 429 → RATE_LIMITED (legacy _judge)."""
        provider = _make_provider(rules=None)
        with patch("provider.base.chat", return_value=(429, "rate limited")):
            result = provider.check(token="sk-test1234567890")
        assert result.error_reason == ErrorReason.RATE_LIMITED


class TestMaskingPin:
    """CheckResult.message must not contain synthetic secrets."""

    def test_message_masks_sk_keys(self):
        """Body containing sk-... is redacted in CheckResult.message."""
        provider = _make_provider(_QWEN_RULES)
        # Body with a synthetic sk- key embedded.
        body = json.dumps({"error": {"code": "Arrearage", "message": "key sk-abcdefghijklmnopqrstuvwxyz1234567890ab is overdue"}})
        with patch("provider.base.chat", return_value=(400, body)):
            result = provider.check(token="sk-test1234567890")
        # The synthetic key must NOT appear in the message.
        assert "sk-abcdefghijklmnopqrstuvwxyz1234567890ab" not in result.message
        assert result.error_reason == ErrorReason.NO_QUOTA


class TestPlumbingPin:
    """TaskConfig(error_parsing=...) → kwargs["error_rules"] present and compiled."""

    def test_plumbing_compiles_rules(self):
        """ProviderFactory.create_provider with error_parsing → error_rules in kwargs."""
        from manager.task import ProviderFactory

        task_config = TaskConfig(
            name="test-qwen",
            enabled=True,
            provider_type="openai_like",
            api=ApiConfig(
                base_url="https://dashscope.example.com/compatible-mode/v1",
                default_model="qwen-turbo",
            ),
            conditions=[Condition(patterns=Patterns(key_pattern="sk-[a-z]+"), query="test")],
            error_parsing=ErrorParsingConfig(
                enabled=True,
                rules=[
                    ErrorRuleConfig(
                        status_code=400,
                        error_path="error.code",
                        match_pattern="Arrearage",
                        map_to="no_quota",
                        priority=100,
                        description="test-arrearage",
                    ),
                ],
            ),
        )

        provider = ProviderFactory.create_provider(task_config, task_config.conditions)
        # The provider should have error_rules in extras.
        assert "error_rules" in provider.extras
        assert len(provider.extras["error_rules"]) == 1
        assert provider.extras["error_rules"][0].map_to == "no_quota"

    def test_plumbing_no_error_parsing(self):
        """ProviderFactory.create_provider without error_parsing → no error_rules."""
        from manager.task import ProviderFactory

        task_config = TaskConfig(
            name="test-openai",
            enabled=True,
            provider_type="openai_like",
            api=ApiConfig(
                base_url="https://api.openai.com",
                default_model="gpt-4o-mini",
            ),
            conditions=[Condition(patterns=Patterns(key_pattern="sk-[a-z]+"), query="test")],
        )

        provider = ProviderFactory.create_provider(task_config, task_config.conditions)
        assert "error_rules" not in provider.extras

    def test_plumbing_disabled_error_parsing(self):
        """error_parsing with enabled=False → no error_rules in kwargs."""
        from manager.task import ProviderFactory

        task_config = TaskConfig(
            name="test-disabled",
            enabled=True,
            provider_type="openai_like",
            api=ApiConfig(
                base_url="https://dashscope.example.com/compatible-mode/v1",
                default_model="qwen-turbo",
            ),
            conditions=[Condition(patterns=Patterns(key_pattern="sk-[a-z]+"), query="test")],
            error_parsing=ErrorParsingConfig(
                enabled=False,
                rules=[
                    ErrorRuleConfig(
                        status_code=400,
                        error_path="$",
                        match_pattern=".*",
                        map_to="bad_request",
                        priority=0,
                        description="disabled-rule",
                    ),
                ],
            ),
        )

        provider = ProviderFactory.create_provider(task_config, task_config.conditions)
        assert "error_rules" not in provider.extras
