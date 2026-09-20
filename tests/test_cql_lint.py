"""Config-time lint for transport-incompatible query syntax (fix-silent-losses).

Traceability: config-query-lint-S1, S2, S3, S4.

Evidence base (rule: live-captured sample): probe 2026-09-20,
api.github.com/search/code, Bearer auth - q='"sk-" AND content:"llm"' ->
HTTP 200 {"total_count":0,"incomplete_results":false,"items":[]}, while
q='"sk-"' -> total_count=46006272 (fixture recorded in plan_agr.md §3.1/3.3).

RED state notes: S1/S3/S4 fail pre-fix (no lint rule / no constant list).
S2 passes trivially pre-fix and is a false-positive guard.
"""

import contextlib

from config.schemas import Config, StageConfig, TaskConfig
from config.validator import ConfigValidator
from core.models import Condition, Patterns

KEY_PATTERN = "sk-x"


def _provider(name, use_api, queries):
    return TaskConfig(
        name=name,
        enabled=True,
        provider_type="openai_like",
        use_api=use_api,
        stages=StageConfig(search=True, gather=True, check=True, inspect=True),
        patterns=Patterns(key_pattern=KEY_PATTERN),
        conditions=[Condition(query=q, patterns=Patterns(key_pattern=KEY_PATTERN)) for q in queries],
    )


def _warnings(tasks):
    validator = ConfigValidator()
    with contextlib.suppress(ValueError):
        validator.validate(Config(tasks=tasks))
    return list(validator.warnings)


# ---------------------------------------------------------------------------
# config-query-lint-S1
# ---------------------------------------------------------------------------
def test_s1_api_provider_with_content_qualifier_is_warned():
    warnings = _warnings([_provider("deepseek", True, ['/sk-x/ AND content:"llm"'])])

    hits = [w for w in warnings if "content" in w.lower()]
    assert hits, f"expected a web-only-qualifier warning, got: {warnings}"
    assert any("deepseek" in w for w in hits)
    # Warning names the condition index so the operator can find it.
    assert any("1" in w for w in hits)


# ---------------------------------------------------------------------------
# config-query-lint-S2 (false-positive guard)
# ---------------------------------------------------------------------------
def test_s2_web_provider_with_same_query_is_not_warned():
    warnings = _warnings([_provider("deepseek-web", False, ['/sk-x/ AND content:"llm"'])])

    assert not [w for w in warnings if "content" in w.lower() and "deepseek-web" in w]


# ---------------------------------------------------------------------------
# config-query-lint-S3
# ---------------------------------------------------------------------------
def test_s3_every_occurrence_reported_separately():
    warnings = _warnings(
        [
            _provider("prov-a", True, ['/a/ AND content:"x"', "/b/"]),
            _provider("prov-b", True, ['/c/ AND content:"y"']),
        ]
    )

    hits = [w for w in warnings if "content" in w.lower()]
    assert len(hits) >= 2, f"each offending condition must be reported, got: {warnings}"
    assert any("prov-a" in w for w in hits)
    assert any("prov-b" in w for w in hits)


# ---------------------------------------------------------------------------
# config-query-lint-S4
# ---------------------------------------------------------------------------
def test_s4_new_qualifier_extends_lint_without_validator_code_change(monkeypatch):
    import constant.search as search_constants

    existing = tuple(getattr(search_constants, "WEB_ONLY_QUALIFIERS", ("content:",)))
    monkeypatch.setattr(search_constants, "WEB_ONLY_QUALIFIERS", existing + ("foobar:",), raising=False)

    warnings = _warnings([_provider("prov-c", True, ['/z/ AND foobar:"q"'])])

    assert any("foobar" in w.lower() for w in warnings), (
        f"lint must read the constant list dynamically, got: {warnings}"
    )
