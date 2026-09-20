"""Search-syntax-aware query cleaning (fix-refine-query-split).

Traceability: query-refinement-S1..S6 (specs/query-refinement/spec.md).

Harness: pure-function tests against the EXISTING ``RefineEngine.clean_regex``
and the landed wire-identity seam ``search/querykey.py`` (commit 56b7781).
No network. Current outputs probe-verified 2026-09-20.

RED state note: unlike import-driven RED files, this one fails at the
assertion level - S1 vectors reproduce the recorded mangling
('""sk-" filename:" AND "env"') until the tokenizer fix lands. S2-S6 are
regression guards: GREEN now and REQUIRED to stay green after the fix
(design D3/D5 stability contract).
"""

import hashlib

from search.github.refine.engine import RefineEngine
from search.querykey import fingerprint, wire_query

ENGINE = RefineEngine.get_instance()


def _clean(query: str) -> str:
    return ENGINE.clean_regex(query=query)


# ---------------------------------------------------------------------------
# query-refinement-S1 (RED: mangled today, probe-verified garbage outputs)
# ---------------------------------------------------------------------------
def test_s1_mixed_literal_and_dotted_qualifier_passes_through():
    """WHEN a quote-leading part mixes a quoted literal with dotted qualifiers
    THEN tokens pass verbatim - no metacharacter parsing, no fragment
    re-quoting, no '" AND "' splicing."""
    # Today: '""sk-" filename:" AND "env"' (garbage -> total_count=0 on the API)
    assert _clean('"sk-" filename:.env') == '"sk-" filename:.env'


def test_s1_multiple_qualifiers_pass_through():
    """WHEN several dotted qualifiers follow a quoted literal THEN all verbatim."""
    # Today: '""sk-" filename:" AND "env path:" AND "github"'
    assert _clean('"sk-" filename:.env path:.github') == '"sk-" filename:.env path:.github'


def test_s1_quoted_literal_with_repo_qualifier_passes_through():
    """WHEN a common real-world form pairs a quoted literal with repo: THEN verbatim."""
    # Today: '""ghp_" repo:owner/name"'
    assert _clean('"ghp_" repo:owner/name') == '"ghp_" repo:owner/name'


def test_s1_unclassified_remnant_fails_open_verbatim():
    """WHEN the tokenizer cannot classify a remnant (unbalanced quote)
    THEN it is emitted verbatim (design D2 fail-open), never regex-parsed."""
    out = _clean('"sk- filename:.env')
    assert out == '"sk- filename:.env'
    assert '" AND "' not in out


# ---------------------------------------------------------------------------
# query-refinement-S2 (guard: regex extraction must survive unchanged)
# ---------------------------------------------------------------------------
def test_s2_regex_part_is_still_cleaned():
    """WHEN a genuine /regex/ part arrives THEN fixed strings are extracted and quoted
    exactly as before this change."""
    assert _clean("/sk-[0-9]{16}/") == '"sk-"'
    assert _clean("/sk-[a-zA-Z0-9]{32}/") == '"sk-"'


# ---------------------------------------------------------------------------
# query-refinement-S3 (guard: already-correct forms stay byte-identical)
# ---------------------------------------------------------------------------
def test_s3_already_correct_queries_stay_stable():
    """WHEN the query is already valid wire syntax THEN output equals input."""
    assert _clean('"sk-"') == '"sk-"'
    assert _clean("filename:.env") == "filename:.env"
    assert _clean('content:"llm"') == 'content:"llm"'
    # Bare-word quoting semantics preserved (probe 2026-09-20: 'AKIA' -> '"AKIA"').
    assert _clean("AKIA") == '"AKIA"'


# ---------------------------------------------------------------------------
# query-refinement-S4 (guard: regex+qualifier composition already correct today)
# ---------------------------------------------------------------------------
def test_s4_mixed_regex_and_qualifier_compose():
    """WHEN a regex part is combined with qualifier tokens THEN the regex is reduced
    to its quoted fixed string and qualifiers stay verbatim - byte-identical across
    the fix."""
    assert _clean("/sk-[a-zA-Z0-9]{32}/ AND filename:.env") == '"sk-" AND filename:.env'
    assert _clean('/sk-[a-zA-Z0-9]{32}/ AND content:"llm"') == '"sk-" AND content:"llm"'
    # A regex that yields no fixed strings disappears without leaving a dangling
    # operator (byte-identical to the pre-fix output 'filename:.env').
    assert _clean("/ab/ AND filename:.env") == "filename:.env"
    assert _clean("filename:.env AND /ab/") == "filename:.env"


# ---------------------------------------------------------------------------
# query-refinement-S5 (composition: delegation identity, structural)
# ---------------------------------------------------------------------------
def test_s5_fingerprint_follows_cleaning_automatically():
    """WHEN clean_regex output changes for a query THEN the API wire form and
    fingerprint follow with zero querykey code change; web stays raw verbatim."""
    mangled_today = '"sk-" filename:.env'

    # Delegation identity: API wire form IS the clean_regex output (non-empty branch).
    assert wire_query(mangled_today, True) == _clean(mangled_today)
    assert fingerprint(mangled_today, True) == hashlib.sha256(
        f"api|{_clean(mangled_today)}".encode("utf-8")
    ).hexdigest()

    # Web transport never cleans.
    assert wire_query(mangled_today, False) == mangled_today

    # Stable class: both sides agree on the pinned conversion.
    assert wire_query("/sk-[a-zA-Z0-9]{32}/", True) == '"sk-"'


# ---------------------------------------------------------------------------
# query-refinement-S6 (shared stability pins survive the fix)
# ---------------------------------------------------------------------------
def test_s6_shared_stability_pins_survive_the_fix():
    """WHEN the tokenizer fix lands THEN the search-aggregation golden vectors and
    the aux pin (_preprocess_query == wire_query) pass unchanged."""
    from config.schemas import Config, StageConfig, TaskConfig
    from core.models import Patterns
    from stage.base import StageResources
    from stage.definition import SearchStage

    raw = "/sk-[a-zA-Z0-9]{32}/"
    api_wire = '"sk-"'

    # Golden vectors shared with tests/test_sa_fingerprint.py (S1/S3 there).
    assert wire_query(raw, True) == api_wire
    assert wire_query(api_wire, True) == api_wire
    assert wire_query(raw, False) == raw
    assert fingerprint(raw, False) == hashlib.sha256(f"web|{raw}".encode("utf-8")).hexdigest()
    assert fingerprint(raw, True) == hashlib.sha256(f"api|{api_wire}".encode("utf-8")).hexdigest()

    # Aux pin: the stage preprocessor delegates to the single source.
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
        assert stage._preprocess_query(raw, use_api) == wire_query(raw, use_api)
