#!/usr/bin/env python3

"""Wire-query fingerprint: single source of truth (add-search-aggregation).

Design D2: the value that gets put on the wire is produced in exactly one
place, so task planning and the runtime response-sharing cache can never
disagree about which queries are equal:

- API transport: ``RefineEngine.clean_regex`` output when it is non-empty,
  otherwise the raw query (GitHub's REST code search does not accept regex,
  so fixed literals are extracted before the request).
- Web transport: the raw query verbatim.

``fingerprint`` pins ``sha256("<api|web>|<wire_query>")`` so a fingerprint is
deterministic across calls and process restarts and always separates the two
transports.
"""

import hashlib

from search.github.refine.engine import RefineEngine

API_TRANSPORT = "api"
WEB_TRANSPORT = "web"


def wire_query(query: str, use_api: bool) -> str:
    """Return the exact query that is sent on the wire for a transport."""
    if not query:
        return query

    if use_api:
        keyword = RefineEngine.get_instance().clean_regex(query=query)
        if keyword:
            return keyword

    return query


def fingerprint(query: str, use_api: bool) -> str:
    """Return the deterministic, transport-aware identity of a wire query."""
    transport = API_TRANSPORT if use_api else WEB_TRANSPORT
    payload = f"{transport}|{wire_query(query, use_api)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
