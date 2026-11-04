#!/usr/bin/env python3

"""
Q3 pins: qwen preset loads from examples/config-full.yaml via real
loader/validator, compiles, and matches expected rule count/priorities/map_to.
"""

import pytest
import yaml

from config.schemas import ErrorParsingConfig, ErrorRuleConfig, TaskConfig
from provider.error_rules import compile_rules, evaluate


class TestPresetLoads:
    """The qwen preset in examples/config-full.yaml loads and compiles."""

    @pytest.fixture
    def qwen_task(self):
        """Load the first task from examples/config-full.yaml."""
        with open("examples/config-full.yaml", "r") as f:
            data = yaml.safe_load(f)
        tasks = data.get("tasks", [])
        assert len(tasks) > 0, "config-full.yaml must have at least one task"
        task_data = tasks[0]
        ep_data = task_data.get("error_parsing")
        assert ep_data is not None, "first task must have error_parsing block"
        # Parse via schema (exercises __post_init__ coercion).
        ep = ErrorParsingConfig(
            enabled=ep_data.get("enabled", True),
            rules=ep_data.get("rules", []),
        )
        return ep

    def test_preset_enabled(self, qwen_task):
        assert qwen_task.enabled is True

    def test_preset_rule_count(self, qwen_task):
        """Preset has exactly 18 rules."""
        assert len(qwen_task.rules) == 18

    def test_preset_compiles(self, qwen_task):
        """All rules compile without error."""
        rule_dicts = [
            {
                "status_code": r.status_code,
                "error_path": r.error_path,
                "match_pattern": r.match_pattern,
                "map_to": r.map_to,
                "priority": r.priority,
                "description": r.description,
            }
            for r in qwen_task.rules
        ]
        compiled = compile_rules(rule_dicts)
        assert len(compiled) == 18

    def test_preset_priorities(self, qwen_task):
        """Priority set matches the operator's table."""
        priorities = sorted([r.priority for r in qwen_task.rules], reverse=True)
        expected = sorted([100, 95, 92, 90, 88, 86, 85, 84, 80, 75, 70, 65, 58, 56, 54, 20, 0, 0], reverse=True)
        assert priorities == expected

    def test_preset_map_to_all_valid(self, qwen_task):
        """All map_to values are valid ErrorReason values."""
        from core.enums import ErrorReason
        valid = {e.value for e in ErrorReason}
        for rule in qwen_task.rules:
            assert rule.map_to in valid, f"invalid map_to: {rule.map_to}"

    def test_preset_arrearage_matches(self, qwen_task):
        """Arrearage body → no_quota."""
        import json
        rule_dicts = [
            {
                "status_code": r.status_code,
                "error_path": r.error_path,
                "match_pattern": r.match_pattern,
                "map_to": r.map_to,
                "priority": r.priority,
                "description": r.description,
            }
            for r in qwen_task.rules
        ]
        compiled = compile_rules(rule_dicts)
        body = json.dumps({"error": {"code": "Arrearage", "message": "overdue"}})
        result = evaluate(compiled, 400, body)
        assert result == "no_quota"

    def test_preset_invalid_api_key_matches(self, qwen_task):
        """InvalidApiKey body → invalid_key."""
        import json
        rule_dicts = [
            {
                "status_code": r.status_code,
                "error_path": r.error_path,
                "match_pattern": r.match_pattern,
                "map_to": r.map_to,
                "priority": r.priority,
                "description": r.description,
            }
            for r in qwen_task.rules
        ]
        compiled = compile_rules(rule_dicts)
        body = json.dumps({"error": {"code": "InvalidApiKey", "message": "bad"}})
        result = evaluate(compiled, 400, body)
        assert result == "invalid_key"

    def test_preset_throttling_matches(self, qwen_task):
        """Throttling.AllocationQuota → rate_limited."""
        import json
        rule_dicts = [
            {
                "status_code": r.status_code,
                "error_path": r.error_path,
                "match_pattern": r.match_pattern,
                "map_to": r.map_to,
                "priority": r.priority,
                "description": r.description,
            }
            for r in qwen_task.rules
        ]
        compiled = compile_rules(rule_dicts)
        body = json.dumps({"error": {"code": "Throttling.AllocationQuota", "message": "quota"}})
        result = evaluate(compiled, 400, body)
        assert result == "rate_limited"
