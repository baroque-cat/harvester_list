"""Operator rendering of the new surface, and no credential in any of it
(PRT-S11, PRT-S12).

RED at plan time: `PipelineStatus` has no `provider_refusal_metrics` field, the
renderer has no `ProviderRefusals:` line, `_RENDERED_METRIC_SURFACES` does not
list it (so the RO-S4 completeness pin fails too), and `InspectStage`'s error
path logs `task: {task}` - which renders the harvested secret in plaintext even
though `_generate_id` deliberately hashes it.

House conventions: assert on the **returned** lines of `_format_pipeline_section`
(only the compact pin needs `caplog`), and build any fake credential from pieces
so this file holds no token-shaped literal.
"""

import dataclasses
import logging

import pytest

from core.metrics import PipelineStatus, StageMetrics
from state.display import StatusDisplayEngine, _RENDERED_METRIC_SURFACES, get_display_config
from state.enums import DisplayMode, StatusContext
from state.models import SystemStatus

REFUSALS = {
    "refusals_rate_limit": 7,
    "refusals_quota": 2,
    "refusals_auth": 1,
    "refusals_transient": 4,
    "deferred_provider_budget": 11,
    "inspect_refused": 3,
    "inspect_empty_answers": 5,
}

# Assembled from pieces: this file must not contain a token-shaped literal.
_SECRET = "sk-" + "A" * 32
_COOKIE = "_gh_" + "sess" + "=" + "B" * 24


def _status(**surfaces):
    surfaces.setdefault("stages", {"check": StageMetrics(name="check", workers=4)})
    return SystemStatus(pipeline=PipelineStatus(**surfaces))


def _config(mode=DisplayMode.DETAILED):
    return get_display_config(StatusContext.APPLICATION, mode)


def _section(status, mode=DisplayMode.DETAILED):
    return StatusDisplayEngine()._format_pipeline_section(status, _config(mode))


def _line(lines, prefix):
    return [ln for ln in lines if ln.startswith(prefix)]


# ---------------------------------------------------------------------------
# PRT-S11
# ---------------------------------------------------------------------------
def test_s11_the_surface_is_rendered_with_every_declared_key():
    lines = _section(_status(provider_refusal_metrics=dict(REFUSALS)))
    rendered = _line(lines, "ProviderRefusals:")

    assert len(rendered) == 1, f"exactly one line, got {rendered}"
    for key, value in REFUSALS.items():
        assert str(value) in rendered[0], f"{key}={value} is not readable in: {rendered[0]}"


@pytest.mark.parametrize("surface", [{}, None])
def test_s11_empty_or_absent_surface_renders_nothing(surface):
    kwargs = {} if surface is None else {"provider_refusal_metrics": surface}
    assert _line(_section(_status(**kwargs)), "ProviderRefusals:") == []


def test_s11_the_surface_is_registered_for_completeness():
    """RO-S4: a published surface without a renderer is a defect, and the pin
    enumerates the dataclass - so the registry must list this one too."""
    assert "provider_refusal_metrics" in _RENDERED_METRIC_SURFACES

    published = {f.name for f in dataclasses.fields(PipelineStatus) if f.name.endswith("_metrics")}
    assert set(_RENDERED_METRIC_SURFACES) == published


def test_s11_unexpected_keys_are_ignored_and_malformed_figures_degrade():
    widened = dict(REFUSALS)
    widened["operator_token"] = _SECRET
    widened["session_cookie"] = _COOKIE
    baseline = _line(_section(_status(provider_refusal_metrics=dict(REFUSALS))), "ProviderRefusals:")[0]
    rendered = _line(_section(_status(provider_refusal_metrics=widened)), "ProviderRefusals:")[0]
    assert rendered == baseline, "an undeclared key must not widen the output"
    assert _SECRET not in rendered and _COOKIE not in rendered

    broken = dict(REFUSALS)
    broken["refusals_rate_limit"] = None
    broken["refusals_quota"] = "not-a-number"
    broken["refusals_auth"] = float("nan")
    broken["refusals_transient"] = float("inf")
    del broken["deferred_provider_budget"]
    degraded = _line(_section(_status(provider_refusal_metrics=broken)), "ProviderRefusals:")[0]
    assert "nan" not in degraded.lower() and "inf" not in degraded.lower()
    assert "not-a-number" not in degraded


def test_s11_compact_output_is_unchanged(caplog):
    with caplog.at_level(logging.INFO):
        StatusDisplayEngine()._render_compact(_status(provider_refusal_metrics=dict(REFUSALS)), _config(DisplayMode.COMPACT))
    assert "ProviderRefusals:" not in caplog.text


# ---------------------------------------------------------------------------
# PRT-S12
# ---------------------------------------------------------------------------
def test_s12_a_refusal_is_logged_by_hash_and_never_by_secret(caplog):
    """The inspect error path must identify the task by the same hash
    `_generate_id` produces; today it interpolates the whole task object."""
    from core.models import CheckResult, Patterns, ResultStorage, Service
    from core.types import IProvider
    from stage.definition import InspectStage
    from stage.factory import TaskFactory
    from tests.test_fh_stage_modes import _resources

    class _Refusing(IProvider):
        """Satisfies the real provider lookup contract so the worker exercises the
        *error path* rather than the unknown-provider configuration path."""

        name = "probe"
        conditions = []

        @property
        def result(self):
            return ResultStorage()

        def get_patterns(self):
            return Patterns(key_pattern="sk-x")

        def check(self, token, address="", endpoint="", model="", **kwargs):
            return CheckResult(available=False)

        def inspect(self, token, address="", endpoint="", **kwargs):
            raise RuntimeError("Authentication failed (HTTP 403)")

    provider_name = "probe"
    resources = _resources("strict", providers={provider_name: _Refusing()})
    stage = InspectStage(resources, lambda out: None, thread_count=1, max_retries=2)
    task = TaskFactory.create_inspect_task(
        provider_name,
        Service(address="https://api.example", endpoint="/v1", key=_SECRET, model="m"),
    )

    expected_hash = stage._generate_id(task)

    with caplog.at_level(logging.ERROR):
        assert stage.process_task(task) is None

    logged = "\n".join(rec.getMessage() for rec in caplog.records)
    assert logged, "the refusal must still be logged loudly"
    assert _SECRET not in logged
    assert _COOKIE not in logged
    assert "https://api.example" not in logged
    assert expected_hash in logged, "the task is identified by its hashed id"

    # The counters carry no credential material either.
    for key in REFUSALS:
        assert _SECRET not in key and _COOKIE not in key
