"""Extraction fidelity pins (E1-E6).

Traceability: operator directive 2026-11-04 — pin completeness of local key
extraction from downloaded content and the AND-semantics contract of
contextual patterns.

Product surfaces under pin:
- ``search/client.py:3473-3496`` — ``extract(text, regex)``: trim both ends,
  ``re.findall`` over the full string, set-dedup, str/tuple/list group handling.
- ``search/client.py:3318-3469`` — ``collect(key_pattern, ..., text=...)``:
  AND-semantics for address/endpoint/model patterns (if pattern is non-empty
  after trim and yields no matches → return [] even when keys exist);
  ``itertools.product`` combinatorics for candidate generation.
- ``stage/definition.py:887-897`` — ``SearchStage._extract_keys_from_content``:
  delegates to ``client.collect`` with task patterns.
- ``stage/definition.py:927-1103`` — ``AcquisitionStage._acquisition_worker``:
  real gather path through ``client.collect``; services reach ``StageOutput``
  via ``add_task``/``add_result``.

Harness: all tests offline. E1-E5 call ``extract``/``collect`` directly with
``text=``. E6 drives ``AcquisitionStage`` with ``fetch_gather_content``
monkeypatched (the single transport seam) so real ``collect``/``extract`` run.
No real network. All fixture values are obviously synthetic (contain
SYNTHETIC/TEST/FIXTURE in the token body, form ``AKIA[0-9A-Z]{16}``).
"""

import pytest

from core.models import Patterns, Service
from search import client as search_client
from search.client import collect, extract


# ---------------------------------------------------------------------------
# Shared synthetic fixtures
# ---------------------------------------------------------------------------

# All keys are form AKIA[0-9A-Z]{16} — 20 chars total, 16 after AKIA.
KEY_A = "AKIASYNTHETICKEY1234"  # SYNTHETIC + KEY + 1234
KEY_B = "AKIATESTEXAMPL567890"  # TEST + EXAMPL + 567890
KEY_C = "AKIAFIXTURESAMPLE901"  # FIXTURE + SAMPLE + 901

ADDR_1 = "https://api.SYNTHETIC.example.com"
ADDR_2 = "https://api.TEST.example.com"
ENDPOINT_1 = "/v1/SYNTHETIC"
MODEL_1 = "model-TEST-gpt4"


# ---------------------------------------------------------------------------
# E1: completeness — N embedded keys → exactly N services
# ---------------------------------------------------------------------------
def test_e1_completeness_n_keys_n_services():
    """WHEN content carries 3 distinct synthetic keys AND non-empty
    address/endpoint/model patterns each match in the content
    THEN collect returns exactly 3 Services (one per unique key) and the
    set of extracted keys equals the set of embedded keys.

    Contract: search/client.py:3433-3469 — extract returns list(set(...)),
    itertools.product(keys, addresses, endpoints, models) with one match each
    for address/endpoint/model yields |keys| candidates.
    """
    content = (
        f"some text {KEY_A} more text\n"
        f"address: {ADDR_1}\n"
        f"endpoint: {ENDPOINT_1}\n"
        f"model: {MODEL_1}\n"
        f"also found {KEY_B} here\n"
        f"and {KEY_C} at the end"
    )
    services = collect(
        key_pattern=r"AKIA[0-9A-Z]{16}",
        address_pattern=r"https://api\.[A-Z]+\.[a-z]+\.com",
        endpoint_pattern=r"/v1/[A-Z]+",
        model_pattern=r"model-[A-Z]+-[a-z0-9]+",
        text=content,
    )
    assert len(services) == 3
    found_keys = {s.key for s in services}
    assert found_keys == {KEY_A, KEY_B, KEY_C}
    # All share the same address/endpoint/model (single match each)
    for s in services:
        assert s.address == ADDR_1
        assert s.endpoint == ENDPOINT_1
        assert s.model == MODEL_1


# ---------------------------------------------------------------------------
# E2: dedup — same key 3 times → one Service per unique key
# ---------------------------------------------------------------------------
def test_e2_dedup_same_key_three_times():
    """WHEN the same key appears 3 times in the content
    THEN extract's set-dedup yields exactly 1 unique key, and with one
    address/endpoint/model the product is exactly 1 Service.

    Contract: search/client.py:3479 — ``items: set[str] = set()``;
    search/client.py:3494 — ``items.add(key)``.
    """
    content = f"{KEY_A} noise {KEY_A} more noise {KEY_A}"
    services = collect(
        key_pattern=r"AKIA[0-9A-Z]{16}",
        address_pattern=r"https://api\.[A-Z]+\.[a-z]+\.com",
        text=f"{content}\naddress: {ADDR_1}",
    )
    assert len(services) == 1
    assert services[0].key == KEY_A


# ---------------------------------------------------------------------------
# E3: combinatorics — 2 keys × 2 addresses × 1 endpoint × 1 model = 4
# ---------------------------------------------------------------------------
def test_e3_combinatorics_product():
    """WHEN 2 distinct keys and 2 distinct addresses match
    THEN itertools.product yields 2×2×1×1 = 4 candidates, and every
    (key, address) pair is present.

    Contract: search/client.py:3466 — ``itertools.product(keys, addresses,
    endpoints, models)``.
    """
    content = (
        f"keys: {KEY_A} and {KEY_B}\n"
        f"addresses: {ADDR_1} and {ADDR_2}\n"
        f"endpoint: {ENDPOINT_1}\n"
        f"model: {MODEL_1}"
    )
    services = collect(
        key_pattern=r"AKIA[0-9A-Z]{16}",
        address_pattern=r"https://api\.[A-Z]+\.[a-z]+\.com",
        endpoint_pattern=r"/v1/[A-Z]+",
        model_pattern=r"model-[A-Z]+-[a-z0-9]+",
        text=content,
    )
    assert len(services) == 4
    pairs = {(s.key, s.address) for s in services}
    assert pairs == {
        (KEY_A, ADDR_1),
        (KEY_A, ADDR_2),
        (KEY_B, ADDR_1),
        (KEY_B, ADDR_2),
    }


# ---------------------------------------------------------------------------
# E4: AND-semantics contract (documented, intentional)
# ---------------------------------------------------------------------------
class TestE4AndSemantics:
    """AND-semantics: a non-empty contextual pattern that fails to match
    rejects ALL candidates, even when keys were found.

    Contract: search/client.py:3441-3461 — for each of address/endpoint/model,
    if the pattern is non-empty after trim and yields no matches, return [].
    This is an intentional design choice: a key without context is useless.
    """

    CONTENT_WITH_KEY = f"found key {KEY_A} here"

    def test_e4a_address_pattern_no_match_returns_empty(self):
        """WHEN keys exist but address_pattern is non-empty and matches nothing
        THEN collect returns [] — the key is useless without an address.

        Contract: search/client.py:3441-3443.
        """
        services = collect(
            key_pattern=r"AKIA[0-9A-Z]{16}",
            address_pattern=r"https://nonexistent\.SYNTHETIC\.domain",
            text=self.CONTENT_WITH_KEY,
        )
        assert services == []

    def test_e4b_endpoint_pattern_no_match_returns_empty(self):
        """WHEN keys exist but endpoint_pattern is non-empty and matches nothing
        THEN collect returns [].

        Contract: search/client.py:3449-3451.
        """
        services = collect(
            key_pattern=r"AKIA[0-9A-Z]{16}",
            endpoint_pattern=r"/v1/NONEXISTENT_SYNTHETIC",
            text=self.CONTENT_WITH_KEY,
        )
        assert services == []

    def test_e4c_model_pattern_no_match_returns_empty(self):
        """WHEN keys exist but model_pattern is non-empty and matches nothing
        THEN collect returns [].

        Contract: search/client.py:3457-3459.
        """
        services = collect(
            key_pattern=r"AKIA[0-9A-Z]{16}",
            model_pattern=r"model-NONEXISTENT_SYNTHETIC",
            text=self.CONTENT_WITH_KEY,
        )
        assert services == []

    def test_e4d_empty_address_pattern_yields_candidates(self):
        """WHEN address_pattern is empty ("") and no addresses match
        THEN candidates ARE produced with address=="" — empty pattern means
        "no constraint", not "must find something".

        Contract: search/client.py:3444-3445 — ``if not addresses:
        addresses.append("")``.
        """
        services = collect(
            key_pattern=r"AKIA[0-9A-Z]{16}",
            address_pattern="",
            text=self.CONTENT_WITH_KEY,
        )
        assert len(services) == 1
        assert services[0].key == KEY_A
        assert services[0].address == ""


# ---------------------------------------------------------------------------
# E5: extract() unit pins
# ---------------------------------------------------------------------------
class TestE5ExtractUnit:
    """Unit-level pins for ``extract(text, regex)``.

    Contract: search/client.py:3473-3496.
    """

    def test_e5a_single_group_regex(self):
        """WHEN regex has one capture group THEN findall returns strings,
        and the same keys are extracted as with a bare pattern.

        Contract: search/client.py:3483-3484 — ``isinstance(x, str)``.
        """
        content = f"text {KEY_A} and {KEY_B}"
        result = extract(text=content, regex=r"(AKIA[0-9A-Z]{16})")
        assert set(result) == {KEY_A, KEY_B}

    def test_e5b_multi_group_tuple_regex(self):
        """WHEN regex has multiple groups THEN findall returns tuples,
        and all groups are flattened into the result set.

        Contract: search/client.py:3485-3486 — ``isinstance(x, (tuple, list))``.
        """
        content = f"{KEY_A} separator {ADDR_1}"
        result = extract(text=content, regex=r"(AKIA[0-9A-Z]{16}).*?(https://[^\s]+)")
        assert set(result) == {KEY_A, ADDR_1}

    def test_e5c_trim_whitespace(self):
        """WHEN a key is surrounded by whitespace/newlines
        THEN it is extracted trimmed.

        Contract: search/client.py:3492 — ``key = trim(word)``.
        """
        content = f"   \n  {KEY_A}  \n   "
        result = extract(text=content, regex=r"AKIA[0-9A-Z]{16}")
        assert result == [KEY_A]

    def test_e5d_no_match_returns_empty(self):
        """WHEN content has no matches for the regex THEN [].

        Contract: search/client.py:3479-3496 — empty set → empty list.
        """
        result = extract(text="no keys here at all", regex=r"AKIA[0-9A-Z]{16}")
        assert result == []

    def test_e5e_empty_regex_or_text_returns_empty(self):
        """WHEN regex or text is empty THEN [].

        Contract: search/client.py:3476-3477 — early return on empty.
        """
        assert extract(text="", regex=r"AKIA[0-9A-Z]{16}") == []
        assert extract(text="some content", regex="") == []
        assert extract(text="", regex="") == []


# ---------------------------------------------------------------------------
# E6: stage-level e2e through REAL collect/extract
# ---------------------------------------------------------------------------
def test_e6_stage_e2e_real_collect(workspace, monkeypatch):
    """WHEN AcquisitionStage processes a task whose URL returns content with
    2 synthetic keys and matching contextual patterns
    THEN the real ``collect``/``extract`` run (only ``fetch_gather_content``
    is monkeypatched at the transport seam) and services reach the stage
    output: check tasks are created and material results are recorded.

    Harness boundary: ``fetch_gather_content`` is the single mocked seam
    (precedent: tests/test_fh_gather_fidelity.py). Everything below —
    ``collect``, ``extract``, ``_acquisition_worker``, ``StageOutput`` —
    runs for real. We do NOT mock ``client.collect`` (contrast with
    test_r7_stage_integration.py which uses ``fake_collect`` — our goal is
    the opposite: exercise the real extraction path).

    Contract: stage/definition.py:949-960 (real collect call),
    stage/definition.py:1045-1052 (services → check tasks + material results).
    """
    import os
    import sqlite3

    from config.schemas import Config, StageConfig, TaskConfig
    from core.models import Patterns
    from stage.base import StageResources
    from stage.definition import AcquisitionStage
    from stage.factory import TaskFactory
    from storage.registry import Registry, url_hash

    PROVIDER = "SYNTHETIC_provider"
    URL = "https://github.com/SYNTHETIC/test/blob/main/leak.py"
    KEY_PATTERN = r"AKIA[0-9A-Z]{16}"

    content = (
        f"leaked keys: {KEY_A} and {KEY_B}\n"
        f"address: {ADDR_1}\n"
        f"endpoint: {ENDPOINT_1}\n"
        f"model: {MODEL_1}"
    )

    # Mock only the transport seam — real collect/extract run.
    def fake_fetch(url="", **kwargs):
        return content

    monkeypatch.setattr(search_client, "fetch_gather_content", fake_fetch)

    # Minimal registry setup (precedent: test_fh_gather_fidelity.py).
    class _FastRegistryConfig:
        enabled = True
        batch_size = 1
        flush_interval = 0.05
        queue_size = 100000
        path = ""

    registry = Registry(str(workspace), config=_FastRegistryConfig(), enabled=True)
    assert registry.start() is True

    try:
        cfg = Config()
        cfg.pipeline.failure_handling = "strict"
        cfg.gather.transport = "raw"
        resources = StageResources(
            limiter=None,
            providers={},
            config=cfg,
            task_configs={
                PROVIDER: TaskConfig(
                    name=PROVIDER,
                    enabled=True,
                    provider_type="openai_like",
                    use_api=False,
                    stages=StageConfig(search=True, gather=True, check=True, inspect=True),
                    patterns=Patterns(
                        key_pattern=KEY_PATTERN,
                        address_pattern=r"https://api\.[A-Z]+\.[a-z]+\.com",
                        endpoint_pattern=r"/v1/[A-Z]+",
                        model_pattern=r"model-[A-Z]+-[a-z0-9]+",
                    ),
                )
            },
            auth=None,
            registry=registry,
        )

        captured_outputs = []
        stage = AcquisitionStage(
            resources, lambda out: captured_outputs.append(out), thread_count=1, max_retries=2,
        )

        task = TaskFactory.create_acquisition_task(
            PROVIDER,
            URL,
            Patterns(
                key_pattern=KEY_PATTERN,
                address_pattern=r"https://api\.[A-Z]+\.[a-z]+\.com",
                endpoint_pattern=r"/v1/[A-Z]+",
                model_pattern=r"model-[A-Z]+-[a-z0-9]+",
            ),
        )

        output = stage.process_task(task)
        assert output is not None

        # Services reached the stage output seam: check tasks created.
        check_tasks = [t for t, stage_name in output.new_tasks if stage_name == "check"]
        assert len(check_tasks) == 2  # 2 keys × 1 address × 1 endpoint × 1 model

        # Material results recorded.
        material_results = [
            data for prov, rtype, data in output.results if rtype == "material"
        ]
        assert len(material_results) == 1
        assert len(material_results[0]) == 2  # 2 services

        # Links recorded.
        link_entries = [links for prov, links in output.links]
        assert any(URL in links for links in link_entries)
    finally:
        registry.stop()


# ---------------------------------------------------------------------------
# E7: skipped — blank gather payload is already pinned by fh_client_taxonomy S2
# and fh_gather_fidelity S7/S8. The defensive check at client.py:3413-3414 is
# only reachable with text="" (returns [] at :3418), and the URL path raises
# TransientFetchError inside fetch_gather_content itself (tested elsewhere).
# No additional pin needed — would duplicate existing coverage.
# ---------------------------------------------------------------------------
