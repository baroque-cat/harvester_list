"""PRT-S26..S28 — provider-refusal-taxonomy Requirement 2: the governing ceiling.

Change ``fix-run-output-hygiene``, delta
``specs/provider-refusal-taxonomy/spec.md``.

The transport already clamps a published wait with ``provider.max_refusal_wait_s``.
The stage then clamps again with ``gather.max_refusal_wait_s`` for **every** stage,
so the ceiling that actually governs a provider refusal belongs to an unrelated
configuration section.  PRT-S26 is the fix; PRT-S27 and PRT-S28 are preservation
pins and are expected to be GREEN before the change — they exist so the fix cannot
be bought by moving the other surface's ceiling or by changing the shipped default.

Expected RED at creation: PRT-S26 only (provider-facing stages report the gather
ceiling).  PRT-S27 and PRT-S28 are declared up front as GREEN-before preservation
pins, not unexpected passes.
"""

from __future__ import annotations

import logging

import stage.base as sb
from stage.definition import AcquisitionStage, CheckStage, InspectStage, SearchStage
from tests.test_fh_stage_modes import _resources, _search_task

GATHER_CEILING = 10.0
PROVIDER_CEILING = 25.0
SHIPPED_DEFAULT = 60.0
PUBLISHED_WAIT = 3600.0

PROVIDER_FACING = (CheckStage, InspectStage)
CODE_HOSTING_FACING = (SearchStage, AcquisitionStage)


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - trivial
        self.messages.append(record.getMessage())


def _stage(cls, gather=GATHER_CEILING, provider=PROVIDER_CEILING):
    resources = _resources("strict")
    resources.config.gather.max_refusal_wait_s = gather
    resources.config.provider.max_refusal_wait_s = provider
    return cls(resources, lambda out: None, thread_count=1, max_retries=0)


def test_prt_s26_a_provider_refusal_is_bounded_by_the_provider_ceiling():
    """WHEN a stage that talks to a model provider defers on a published wait larger
    than the provider ceiling, while the ceiling configured for the code-hosting
    surface is lower still
    THEN the wait applied is the provider ceiling, and the deferral record reports
    the provider ceiling as the bound that governed."""
    for cls in PROVIDER_FACING:
        stage = _stage(cls)
        assert stage._defer_wait_cap() == PROVIDER_CEILING, (
            f"{cls.__name__} is bounded by the code-hosting ceiling, not the provider one"
        )
        assert stage._effective_defer_wait(PUBLISHED_WAIT) == PROVIDER_CEILING

    stage = _stage(CheckStage)
    capture = _Capture()
    sb.logger.addHandler(capture)
    try:
        assert stage.defer_task(_search_task(), wait_s=PROVIDER_CEILING) is True
    finally:
        sb.logger.removeHandler(capture)

    deferrals = [m for m in capture.messages if "deferred task on rate limit" in m]
    assert deferrals, f"no deferral record was emitted: {capture.messages}"
    assert f"cap {PROVIDER_CEILING:.1f}s" in deferrals[-1], (
        f"the deferral record does not name the ceiling that governed: {deferrals[-1]}"
    )
    assert f"cap {GATHER_CEILING:.1f}s" not in deferrals[-1], (
        "the deferral record names an unrelated surface's ceiling"
    )


def test_prt_s27_a_code_hosting_surface_refusal_keeps_its_own_ceiling():
    """WHEN a stage that talks to the code-hosting surface defers on a published wait
    larger than that surface's ceiling
    THEN the wait applied is that surface's ceiling, unchanged by the amendment.

    Preservation pin — GREEN before the change by design.
    """
    for cls in CODE_HOSTING_FACING:
        stage = _stage(cls)
        assert stage._defer_wait_cap() == GATHER_CEILING, f"{cls.__name__} changed surface"
        assert stage._effective_defer_wait(PUBLISHED_WAIT) == GATHER_CEILING


def test_prt_s28_the_applied_wait_is_unchanged_while_the_two_ceilings_are_equal():
    """WHEN both ceilings hold their shipped default value and a provider publishes a
    wait larger than either
    THEN the wait applied is the same one that was applied before the ceiling became
    surface-aware, and it remains strictly below the queue visibility window.

    Preservation pin — GREEN before the change by design.
    """
    for cls in PROVIDER_FACING + CODE_HOSTING_FACING:
        stage = _stage(cls, gather=SHIPPED_DEFAULT, provider=SHIPPED_DEFAULT)
        config = stage.resources.config
        assert config.gather.max_refusal_wait_s == SHIPPED_DEFAULT
        assert config.provider.max_refusal_wait_s == SHIPPED_DEFAULT
        assert stage._effective_defer_wait(PUBLISHED_WAIT) == SHIPPED_DEFAULT, cls.__name__
        assert stage._defer_wait_cap() < config.queue.visibility_timeout_s, (
            f"{cls.__name__}: invariant 6 — the ceiling must stay strictly below the "
            f"visibility window ({config.queue.visibility_timeout_s})"
        )
