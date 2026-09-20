"""Wire-query fingerprint single source of truth (add-search-aggregation).

Traceability: search-aggregation-S1, S2, S3 (+ auxiliary planner/runtime
equality pin from design D2).

Harness: pure-function tests against the planned module ``search/querykey.py``
(``wire_query``, ``fingerprint``). The module does not exist yet - the import
failure IS the expected RED state.

Contract notes:
- API transport: wire form = RefineEngine.clean_regex output when non-empty
  (exactly today's SearchStage._preprocess_query semantics, relocated).
- Web transport: wire form = raw query.
- fingerprint = sha256("<api|web>|<wire_query>") hex - pinned as a golden
  vector so planning and runtime keys can never drift apart silently.
"""

import hashlib

import pytest

from search.querykey import fingerprint, wire_query  # RED driver: module absent pre-fix


RAW_REGEX_QUERY = "/sk-[a-zA-Z0-9]{32}/"
ITS_API_WIRE_FORM = '"sk-"'  # clean_regex keeps only fixed literals (verified 2026-09-20)


# ---------------------------------------------------------------------------
# search-aggregation-S1
# ---------------------------------------------------------------------------
def test_s1_api_fingerprint_reflects_preprocessed_wire_form():
    """WHEN two raw queries converge to the same API wire query THEN one identity."""
    # clean_regex('/sk-[a-zA-Z0-9]{32}/') -> '"sk-"' and '"sk-"' passes through stable
    # (stability pinned by the fix-refine-query-split proposal evidence).
    assert wire_query(RAW_REGEX_QUERY, True) == ITS_API_WIRE_FORM
    assert wire_query(ITS_API_WIRE_FORM, True) == ITS_API_WIRE_FORM
    assert fingerprint(RAW_REGEX_QUERY, True) == fingerprint(ITS_API_WIRE_FORM, True)

    # Web transport keeps the raw query verbatim.
    assert wire_query(RAW_REGEX_QUERY, False) == RAW_REGEX_QUERY


# ---------------------------------------------------------------------------
# search-aggregation-S2
# ---------------------------------------------------------------------------
def test_s2_transport_separation_is_absolute():
    """WHEN the same raw query runs under api and web THEN fingerprints differ."""
    assert fingerprint(RAW_REGEX_QUERY, True) != fingerprint(RAW_REGEX_QUERY, False)
    assert fingerprint(ITS_API_WIRE_FORM, True) != fingerprint(ITS_API_WIRE_FORM, False)


# ---------------------------------------------------------------------------
# search-aggregation-S3
# ---------------------------------------------------------------------------
def test_s3_fingerprint_determinism_and_golden_vector():
    """WHEN fingerprinted repeatedly THEN identical, matching the pinned sha256 contract."""
    f1 = fingerprint(RAW_REGEX_QUERY, False)
    f2 = fingerprint(RAW_REGEX_QUERY, False)
    assert f1 == f2

    golden = hashlib.sha256(f"web|{RAW_REGEX_QUERY}".encode("utf-8")).hexdigest()
    assert f1 == golden

    golden_api = hashlib.sha256(f"api|{ITS_API_WIRE_FORM}".encode("utf-8")).hexdigest()
    assert fingerprint(RAW_REGEX_QUERY, True) == golden_api


# ---------------------------------------------------------------------------
# Auxiliary pin (design D2): the stage preprocessor must delegate to the helper
# ---------------------------------------------------------------------------
def test_aux_stage_preprocess_delegates_to_single_source():
    """SearchStage._preprocess_query and wire_query must agree byte-for-byte."""
    from config.schemas import Config, StageConfig, TaskConfig
    from core.models import Patterns
    from stage.base import StageResources
    from stage.definition import SearchStage

    class FakeAuth:
        def get_session(self):
            return "s"

        def get_token(self):
            return "t"

        def get_user_agent(self):
            return "ua"

    provider = "prov-x"
    resources = StageResources(
        limiter=None,
        providers={},
        config=Config(),
        task_configs={
            provider: TaskConfig(
                name=provider,
                enabled=True,
                provider_type="openai_like",
                use_api=True,
                stages=StageConfig(search=True, gather=True, check=True, inspect=True),
                patterns=Patterns(key_pattern="sk-x"),
            )
        },
        auth=FakeAuth(),
    )
    stage = SearchStage(resources, lambda out: None)

    for use_api in (True, False):
        assert stage._preprocess_query(RAW_REGEX_QUERY, use_api) == wire_query(RAW_REGEX_QUERY, use_api)
