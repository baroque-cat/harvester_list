"""ROI-S1..S6 — run-output-integrity: credential material never reaches a sink.

Change ``fix-run-output-hygiene``.  One test per scenario, in the order the
scenarios appear in ``specs/run-output-integrity/spec.md``.

Expected RED at creation, and why:

* the console renderer does not redact at all, so S1/S2/S3 publish the payload;
* the JSON renderer does not redact either, so S4 names it as an offender;
* the logging setup still parks a redaction filter on the ancestor logger, where
  it can never fire, so S5 fails on the source inspection;
* ``chat()`` still interpolates whole header values, so S6 finds the payload.

The credential below is synthetic and shaped like a harvested key; no real
material is used, logged or asserted on.
"""

from __future__ import annotations

import inspect
import io
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

import search.client as sc
import tools.logger as lg
from tools.logger import get_logger

SECRET = "sk-ROIprobeSYNTHETIC0000000000000"
AUTH_VALUE = f"Bearer {SECRET}"


def _record(message: str, level: int = logging.WARNING) -> logging.LogRecord:
    return logging.LogRecord(
        name="roi.probe",
        level=level,
        pathname="search/client.py",
        lineno=1862,
        msg=message,
        args=(),
        exc_info=None,
    )


def _console_handler(logger: logging.Logger) -> logging.StreamHandler:
    """The real console sink the logging layer attaches, not a test double."""
    for handler in logger.handlers:
        if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
            return handler
    raise AssertionError("the logging layer attached no console handler")


class _Capture(logging.Handler):
    """Collects rendered messages of one logger, whatever its propagate setting."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - trivial
        self.messages.append(record.getMessage())


# ---------------------------------------------------------------------------
# ROI-S1 / ROI-S2 / ROI-S3 — the console sink
# ---------------------------------------------------------------------------


def test_roi_s1_a_credential_logged_to_the_console_never_reaches_it():
    """WHEN a record whose message contains a harvested credential is emitted
    through a category logger while the console sink is captured
    THEN the captured console text contains no form of that credential and the
    rest of the line renders normally."""
    logger = get_logger("roi-s1")
    console = _console_handler(logger)
    original, buffer = console.stream, io.StringIO()
    console.stream = buffer
    try:
        logger.warning("[chat] failed to request URL: %s, headers: {'authorization': '%s'}", "http://probe", AUTH_VALUE)
        rendered = buffer.getvalue()
    finally:
        console.stream = original

    assert SECRET not in rendered, "the console sink published a credential in clear"
    assert AUTH_VALUE not in rendered, "the console sink published an authorization value"
    assert "[chat] failed to request URL" in rendered, "the rest of the line must still render"


def test_roi_s2_a_credential_inside_an_attached_traceback_is_redacted_too():
    """WHEN a record is emitted with exception information whose traceback text
    contains a credential
    THEN the rendered line carries the traceback with the credential redacted.

    This is the case a record-level filter cannot cover: the traceback text is
    added by the renderer, not present in the message.
    """
    logger = get_logger("roi-s2")
    console = _console_handler(logger)
    original, buffer = console.stream, io.StringIO()
    console.stream = buffer
    try:
        try:
            raise ValueError(f"upstream rejected {AUTH_VALUE}")
        except ValueError:
            logger.exception("provider call failed")
        rendered = buffer.getvalue()
    finally:
        console.stream = original

    assert "Traceback" in rendered, "the traceback itself must survive redaction"
    assert SECRET not in rendered, "a credential inside the traceback reached the sink"


def test_roi_s3_a_redaction_failure_publishes_a_marker_not_the_payload(monkeypatch):
    """WHEN redaction of a rendered line raises instead of returning a redacted line
    THEN the sink receives a marker naming level, origin logger and source
    location, and no part of the unredacted line is written."""

    def _unavailable(_text):
        raise RuntimeError("redactor unavailable")

    monkeypatch.setattr(lg, "redact_api_keys_in_text", _unavailable)

    line = lg.FORMATTER.format(_record(f"headers contain {AUTH_VALUE}", level=logging.ERROR))

    assert SECRET not in line, "a redaction failure published the payload (fail-open)"
    assert "ERROR" in line, "the marker keeps the level"
    assert "roi.probe" in line, "the marker keeps the origin logger"
    assert "client.py" in line, "the marker keeps the source location"


# ---------------------------------------------------------------------------
# ROI-S4 — completeness is structural, not a maintained list
# ---------------------------------------------------------------------------


def test_roi_s4_no_sink_in_the_logging_layer_can_ship_without_redaction():
    """WHEN every renderer defined by the logging layer is given a line containing
    a credential
    THEN each of them redacts it, so a renderer added later without redaction is a
    test failure rather than a silent gap."""
    renderers = [
        obj
        for obj in vars(lg).values()
        if isinstance(obj, type)
        and obj.__module__ == lg.__name__
        and issubclass(obj, logging.Formatter)
        and obj is not logging.Formatter
    ]
    assert len(renderers) >= 4, f"expected the shipped renderers to be enumerated, got {renderers}"

    record = _record(f"authorization: {AUTH_VALUE}")
    offenders = sorted(
        cls.__name__ for cls in renderers if SECRET in cls("%(levelname)s %(message)s").format(record)
    )
    assert not offenders, f"renderer(s) publish credential material: {offenders}"


# ---------------------------------------------------------------------------
# ROI-S5 — attached where records actually pass
# ---------------------------------------------------------------------------


def test_roi_s5_a_filter_on_the_ancestor_logger_never_sees_a_pipeline_record():
    """WHEN a filter that records what it sees is attached to the process's
    top-level logger and a record is emitted through a category logger
    THEN the filter saw nothing — so redaction placed there cannot protect any
    sink, and the logging layer must not rely on it."""
    seen: list[str] = []

    class _Spy(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            seen.append(record.getMessage())
            return True

    root = logging.getLogger()
    previous = list(root.filters)
    root.addFilter(_Spy())
    try:
        logger = get_logger("roi-s5")
        logger.warning("a record from a category logger")

        assert logger.propagate is False, "category loggers do not propagate to the ancestor"
        assert seen == [], "an ancestor filter saw a record; the pinned semantics changed"
    finally:
        root.filters[:] = previous

    source = inspect.getsource(lg)
    assert "getLogger().addFilter" not in source, (
        "the logging layer still parks a filter on the ancestor logger, where it can never fire "
        "for a category logger's record"
    )


# ---------------------------------------------------------------------------
# ROI-S6 — the known emitter stops emitting the secret
# ---------------------------------------------------------------------------


class _ScriptedHandler(BaseHTTPRequestHandler):
    """Returns one scripted response so the failure path is deterministic."""

    script = (401, {}, "{}")

    def do_POST(self):  # noqa: N802 - the check surface posts
        status, headers, body = self.script
        payload = body.encode("utf-8")
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, str(value))
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # silence the request log
        pass


@pytest.fixture
def served(monkeypatch):
    """A loopback server plus an isolated session, so no proxy or env interferes."""
    isolated = requests.Session()
    isolated.trust_env = False
    monkeypatch.setattr(sc, "_HTTP_SESSION", isolated)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _ScriptedHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def serve(status: int, headers=None, body: str = "{}") -> str:
        _ScriptedHandler.script = (status, headers or {}, body)
        return f"http://127.0.0.1:{httpd.server_address[1]}/chat"

    try:
        yield serve
    finally:
        httpd.shutdown()
        httpd.server_close()
        isolated.close()


def test_roi_s6_a_failed_request_reports_header_names_and_no_header_value(served):
    """WHEN an outbound request fails and the failure is logged with its status
    and its headers
    THEN the record names the headers that were sent and carries no header value."""
    url = served(401, {}, '{"error":{"message":"invalid api key"}}')

    capture = _Capture()
    logger = get_logger("search")
    logger.addHandler(capture)
    try:
        code, _message = sc.chat(
            url=url,
            headers={"authorization": AUTH_VALUE, "x-probe-header": "1"},
            model="probe-model",
            retries=1,
            timeout=5,
        )
    finally:
        logger.removeHandler(capture)

    assert code == 401, "the check surface still classifies the outcome itself"
    text = "\n".join(capture.messages)
    assert "authorization" in text, "the header name is the diagnostic and stays"
    assert SECRET not in text, "a header value was interpolated into the log"
    assert AUTH_VALUE not in text, "an authorization value was interpolated into the log"
