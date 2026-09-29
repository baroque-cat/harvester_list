"""The typed signal escapes every shipped provider transport (PRT-S17).

RED at plan time for two of the five: `provider/vertex.py` calls `http_get` at
`:304` and `:342`, both inside a `try:` whose `except Exception` (`:325`, `:353`)
turns the deferral into `continue`; `provider/bedrock.py` re-raises it correctly
out of `_send_request` (`:240`, PFC-D10) but the calling `inspect` swallows it at
`:493-495` into `return []`.  In both cases the stage sees an empty model list,
i.e. "this provider has no models" - the outcome PRT-S8 forbids.

The predecessor's proposal asserted that "gemini and vertex have no wrapper, so
the exception reaches InspectStage's catch-all".  That is true for gemini and
false for vertex, so this pin covers all five transports and turns the claim into
a checked fact rather than an assertion.  `openai_like`'s subclasses (`azure`,
`openai`, `doubao`, `qianfan`) inherit `_fetch_models` and are covered by it;
`gooeyai` and `stabilityai` never call the module-level `http_get` at all.

No network and no cloud credentials: the subject is whether a broad handler
absorbs one typed exception, so the transport call is stubbed at the module
boundary and the credential plumbing is left real.
"""

import pytest

from core.enums import ErrorReason
from core.exceptions import RateLimitDeferral
from core.models import Condition

WAIT_S = 30.0


def _refusal():
    return RateLimitDeferral(
        "provider rate limit (HTTP 429)", wait_s=WAIT_S, reason=ErrorReason.RATE_LIMITED.name
    )


def _stub_http_get(monkeypatch, module):
    """Make the module's transport call raise the typed signal."""

    def _refuse(*args, **kwargs):
        raise _refusal()

    monkeypatch.setattr(module, "http_get", _refuse)


def _openai_like(monkeypatch):
    import provider.openai_like as mod
    from provider.openai_like import OpenAILikeProvider

    _stub_http_get(monkeypatch, mod)
    provider = OpenAILikeProvider(
        name="probe",
        base_url="http://127.0.0.1",
        completion_path="/v1/chat/completions",
        model_path="/v1/models",
        default_model="probe-model",
        conditions=[Condition(query="/sk-x/")],
    )
    return lambda: provider._fetch_models("http://127.0.0.1/v1/models", {})


def _anthropic(monkeypatch):
    import provider.anthropic as mod
    from provider.anthropic import AnthropicProvider

    _stub_http_get(monkeypatch, mod)
    provider = AnthropicProvider(conditions=[Condition(query="/sk-ant-/")])
    return lambda: provider._fetch_models("http://127.0.0.1/v1/models", {})


def _gemini(monkeypatch):
    import provider.gemini as mod
    from provider.gemini import GeminiProvider

    _stub_http_get(monkeypatch, mod)
    provider = GeminiProvider(conditions=[Condition(query="/AIza/")])
    return lambda: provider.inspect(token="dummy-token", address="", endpoint="")


def _vertex(monkeypatch):
    """The multi-publisher transport: a swallowed signal becomes `continue`, so
    all eight publishers are skipped and the fallback block swallows again."""
    import provider.vertex as mod
    from provider.vertex import VertexProvider

    _stub_http_get(monkeypatch, mod)
    provider = VertexProvider(conditions=[Condition(query="/ya29./")])
    return lambda: provider.inspect(token="dummy-token", address="global", endpoint="dummy-project")


def _bedrock(monkeypatch):
    """The request-signing transport: `_send_request` already re-raises, so the
    absorption happens one frame up, in `inspect`."""
    import provider.bedrock as mod
    from provider.bedrock import BedrockProvider

    _stub_http_get(monkeypatch, mod)
    provider = BedrockProvider(conditions=[Condition(query="/AKIA/")])
    return lambda: provider.inspect(token="dummy-secret", address="us-east-1", endpoint="AKIADUMMY")


TRANSPORTS = [
    ("openai_like", _openai_like),
    ("anthropic", _anthropic),
    ("gemini", _gemini),
    ("vertex", _vertex),
    ("bedrock", _bedrock),
]


@pytest.mark.parametrize("name,factory", TRANSPORTS, ids=[t[0] for t in TRANSPORTS])
def test_s17_every_shipped_transport_lets_the_signal_escape(monkeypatch, name, factory):
    """PRT-S17: no shipped transport may convert the typed deferral into an empty
    answer or into a silently skipped publisher."""
    call = factory(monkeypatch)

    with pytest.raises(RateLimitDeferral) as excinfo:
        call()

    assert excinfo.value.wait_s == pytest.approx(WAIT_S), f"{name}: the wait must travel intact"
    assert excinfo.value.reason == ErrorReason.RATE_LIMITED.name


def test_s17_the_census_of_transport_callers_is_complete():
    """Guard the claim above: if a new module starts calling the provider
    transport, this pin names it so the escape property is extended deliberately
    rather than by omission."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent / "provider"
    callers = sorted(
        path.stem for path in root.glob("*.py") if re.search(r"(?<!_)http_get\(", path.read_text())
    )
    assert callers == ["anthropic", "bedrock", "gemini", "openai_like", "vertex"], (
        f"the set of modules calling the provider transport changed: {callers}"
    )
