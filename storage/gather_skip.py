#!/usr/bin/env python3

"""
Registry-driven gather-skip decision engine (add-gather-skip).

Reads the (otherwise write-only) link registry through dedicated read-only
SQLite connections to decide whether a discovered link's acquisition task can
be suppressed.  Design constraints:

- Read-only connections (``file:...?mode=ro``, URI) are opened lazily per
  worker thread; WAL keeps them snapshot-consistent beside the writer thread.
- Lookups are batched per result page (``WHERE url_hash IN (...)``).
- The rule is a strict conjunction evaluated cheapest-first; the first
  failing condition names the ``regathered_*`` reason.
- The engine fails open: errored lookups and missing rows always yield tasks,
  never skips.  A NULL ``repo_pushed_at`` is nullable *evidence*, not missing
  data: it passes condition 3 vacuously (documented TTL-only degradation mode
  while ``add-repo-meta-enrichment`` has not landed).
- ``off`` mode performs no registry read at all.

The engine never enforces suppression itself: it reports ``skip`` decisions
and the stage applies them only when ``mode == "on"``.  See
``docs/specs/registry_flags_metrics.md`` for the flag/metric vocabulary.
"""

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from storage.registry import url_hash
from tools.logger import get_logger

logger = get_logger("storage")

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ON = "on"
VALID_MODES = (MODE_OFF, MODE_SHADOW, MODE_ON)

REGISTRY_FILENAME = "registry.sqlite"
DECISION_LOG_FILENAME = "registry_decisions.jsonl"

DEFAULT_TTL_HOURS = 168.0  # 7 days
DEFAULT_PUSH_GRACE_SECONDS = 60.0

REASON_CHANGED = "changed"
REASON_COVERAGE_GAP = "coverage_gap"
REASON_TTL_EXPIRED = "ttl_expired"
REASON_FAILED_RETRY = "failed_retry"
REGATHER_REASONS = (
    REASON_CHANGED,
    REASON_COVERAGE_GAP,
    REASON_TTL_EXPIRED,
    REASON_FAILED_RETRY,
)

STATUS_GATHERED_OK = "gathered_ok"
STATUS_FAILED = "failed"

_SECONDS_PER_HOUR = 3600.0

# Maximum bind parameters per IN-clause SELECT.  Mirrors storage/priority.py
# (900) and stays well below SQLite's upstream limit (32 766) to accommodate
# builds with lower limits (prod pyenv 3.11 is unmeasured).
_BIND_CHUNK_SIZE = 900


@dataclass
class SkipDecision:
    """Outcome of evaluating one discovered link against the registry.

    ``skip`` is the *computed* would-skip outcome; whether it is enforced is
    the stage's decision and depends on the engine mode.  ``reason`` names the
    first failing condition for ``regathered_*`` attribution (empty when the
    link is skipped, unseen or merely not-yet-gathered).
    """

    url: str
    url_hash: str
    known: bool = False
    skip: bool = False
    reason: str = ""
    push_evidence: bool = False
    conditions: Dict[str, Any] = field(default_factory=dict)


class GatherSkipEngine:
    """Read-only registry lookups + conjunctive skip rule.

    Thread-safe: each thread gets its own read-only connection and counters
    are guarded by a lock.  Every public call fails open.
    """

    def __init__(
        self,
        workspace: str,
        mode: str = MODE_OFF,
        ttl_hours: float = DEFAULT_TTL_HOURS,
        registry_path: Optional[str] = None,
        run_id: Optional[str] = None,
        grace_seconds: float = DEFAULT_PUSH_GRACE_SECONDS,
        clock=None,
        log_path: Optional[str] = None,
        decision_log_max_bytes: int = 67_108_864,
        decision_log_keep_rotated: int = 2,
    ):
        self.workspace = str(workspace)
        self.mode = mode if mode in VALID_MODES else MODE_OFF
        self.ttl_hours = float(ttl_hours)
        self.grace_seconds = float(grace_seconds)
        self.registry_path = registry_path or os.path.join(self.workspace, REGISTRY_FILENAME)
        self.log_path = log_path or os.path.join(self.workspace, DECISION_LOG_FILENAME)
        self.run_id = run_id
        self._clock = clock or time.time
        self.decision_log_max_bytes = int(decision_log_max_bytes)
        self.decision_log_keep_rotated = int(decision_log_keep_rotated)

        self._local = threading.local()
        self._stat_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._conn_lock = threading.Lock()
        self._conns: set = set()
        self._log_handle = None
        self._log_bytes = 0  # in-memory byte counter for rotation threshold
        self._closed = False

        self.skipped_known = 0
        self.evaluated_known = 0
        self.push_evidence_count = 0
        self.registry_reads = 0
        self.read_errors = 0
        self.read_error_warnings = 0
        self.gathered_ts_missing = 0
        self._regathered: Dict[str, int] = {reason: 0 for reason in REGATHER_REASONS}
        self._logged_error_kinds: set = set()
        self.chunk_warnings = 0
        self.decision_log_rotations = 0

    # ------------------------------------------------------------------
    # Capabilities
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """True when the engine may consult the registry (shadow/on)."""
        return self.mode != MODE_OFF

    @property
    def enforce(self) -> bool:
        """True when computed skips must actually suppress tasks."""
        return self.mode == MODE_ON

    @property
    def push_signal_coverage(self) -> float:
        """Share of evaluated known links backed by real push evidence."""
        with self._stat_lock:
            if self.evaluated_known <= 0:
                return 0.0
            return self.push_evidence_count / self.evaluated_known

    @property
    def read_error_kinds(self) -> set:
        """The set of error kinds that have already been warned about."""
        with self._stat_lock:
            return set(self._logged_error_kinds)

    def has_read_errors(self) -> bool:
        """Thread-safe kill-switch probe: True once any lookup has errored."""
        with self._stat_lock:
            return self.read_errors > 0

    # ------------------------------------------------------------------
    # Decision path
    # ------------------------------------------------------------------

    def decide(
        self,
        urls: Sequence[str],
        provider: str,
        patterns_hash: str,
        now: Optional[float] = None,
    ) -> List[SkipDecision]:
        """Evaluate a page of URLs, returning one decision per URL in order."""
        url_list = [url for url in (urls or []) if url]
        if self.mode == MODE_OFF or not url_list:
            return [self._unknown(url) for url in url_list]

        try:
            links, covered = self._lookup(url_list, provider, patterns_hash)
        except Exception as exc:  # fail-open in the safe direction
            self._record_read_error(exc, len(url_list))
            return [self._unknown(url) for url in url_list]

        moment = self._clock() if now is None else float(now)
        decisions: List[SkipDecision] = []
        for url in url_list:
            decision = self._decide_one(url, links, covered, moment)
            decisions.append(decision)
            self._account(decision)
            if decision.skip:
                self._log_decision(decision, provider)
        return decisions

    def classify(
        self,
        urls: Sequence[str],
        provider: str,
        patterns_hash: str,
        now: Optional[float] = None,
    ) -> List[SkipDecision]:
        """Read-only classification with no counters and no decision logging.

        Consumers such as early-stop frontier detection need the exact same
        conjunctive "known" rule as gather-skip but must not double-count skip
        decisions or append duplicate skip records.  Fails open exactly like
        ``decide``: errored lookups and missing rows yield non-skipped
        decisions.
        """
        url_list = [url for url in (urls or []) if url]
        if not url_list:
            return []
        try:
            links, covered = self._lookup(url_list, provider, patterns_hash)
        except Exception as exc:  # fail-open in the safe direction
            self._record_read_error(exc, len(url_list))
            return [self._unknown(url) for url in url_list]
        moment = self._clock() if now is None else float(now)
        return [self._decide_one(url, links, covered, moment) for url in url_list]

    def to_stats(self) -> Dict[str, Any]:
        """Flatten per-run counters for run statistics / status display."""
        with self._stat_lock:
            coverage = self.push_evidence_count / self.evaluated_known if self.evaluated_known else 0.0
            return {
                "mode": self.mode,
                "skipped_known": self.skipped_known,
                "regathered_changed": self._regathered[REASON_CHANGED],
                "regathered_coverage_gap": self._regathered[REASON_COVERAGE_GAP],
                "regathered_ttl_expired": self._regathered[REASON_TTL_EXPIRED],
                "regathered_failed_retry": self._regathered[REASON_FAILED_RETRY],
                "evaluated_known": self.evaluated_known,
                "push_evidence": self.push_evidence_count,
                "push_signal_coverage": coverage,
                "registry_reads": self.registry_reads,
                "read_errors": self.read_errors,
                "gathered_ts_missing": self.gathered_ts_missing,
            }

    def close(self) -> None:
        """Close every read-only connection and the decision log (never raises).

        Connections are opened per worker thread; they are tracked so this
        method (called from the pipeline's main thread at shutdown) can close
        all of them.  It must run only after the search stage has stopped, so
        no worker is using a connection at that point.
        """
        self._closed = True
        self._reset_conn()
        with self._conn_lock:
            conns = list(self._conns)
            self._conns.clear()
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass
        with self._log_lock:
            if self._log_handle is not None:
                try:
                    self._log_handle.close()
                except Exception:
                    pass
                self._log_handle = None

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Open (once per thread) a read-only connection to the registry."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            uri = Path(os.path.abspath(self.registry_path)).as_uri() + "?mode=ro"
            # check_same_thread=False is required so the main thread can close
            # connections owned by worker threads at shutdown (see close()).
            # Each connection is otherwise used only by its creating thread.
            conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=5.0)
            self._local.conn = conn
            with self._conn_lock:
                self._conns.add(conn)
        return conn

    def _lookup(self, urls: Sequence[str], provider: str, patterns_hash: str):
        """Batched page lookup: link rows + coverage rows for the current run.

        Chunks the IN-clause to stay below the SQLite bind-variable limit
        (C3).  The coverage query carries 2 extra bind params (provider,
        patterns_hash), so the IN-chunk is capped at ``_BIND_CHUNK_SIZE - 2``
        to keep the total ≤ _BIND_CHUNK_SIZE.  Results from all chunks are
        merged identically to a single unchunked query.
        """
        # De-duplicate hashes so a repeated URL does not bloat the IN clause.
        hashes = list(dict.fromkeys(url_hash(url) for url in urls))
        conn = self._connect()
        with self._stat_lock:
            self.registry_reads += 1

        links: Dict[str, tuple] = {}
        covered: set = set()

        # The coverage query has 2 extra params, so cap the IN-chunk accordingly.
        in_chunk = _BIND_CHUNK_SIZE - 2
        num_chunks = (len(hashes) + in_chunk - 1) // in_chunk if hashes else 0
        if num_chunks > 1:
            with self._stat_lock:
                self.chunk_warnings += 1
                first = self.chunk_warnings == 1
            if first:
                logger.warning(
                    f"[gather-skip] lookup chunked into {num_chunks} queries "
                    f"({len(hashes)} hashes > {in_chunk}); "
                    "further occurrences are counted in 'chunk_warnings'."
                )

        for start in range(0, len(hashes), in_chunk):
            chunk = hashes[start : start + in_chunk]
            placeholders = ",".join("?" for _ in chunk)
            link_rows = conn.execute(
                "SELECT url_hash, visit_status, gathered_ts, repo_pushed_at "
                f"FROM links WHERE url_hash IN ({placeholders})",
                chunk,
            ).fetchall()
            coverage_rows = conn.execute(
                "SELECT url_hash FROM link_coverage "
                f"WHERE provider = ? AND patterns_hash = ? AND url_hash IN ({placeholders})",
                [provider, patterns_hash, *chunk],
            ).fetchall()
            for row in link_rows:
                links[row[0]] = (row[1], row[2], row[3])
            covered.update(row[0] for row in coverage_rows)

        return links, covered

    def _reset_conn(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            return
        self._local.conn = None
        with self._conn_lock:
            self._conns.discard(conn)
        try:
            conn.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Rule
    # ------------------------------------------------------------------

    def _decide_one(self, url, links, covered, now) -> SkipDecision:
        digest = url_hash(url)
        row = links.get(digest)
        if row is None:
            return SkipDecision(
                url=url,
                url_hash=digest,
                known=False,
                skip=False,
                conditions=self._snapshot(None, None, None, False, now),
            )

        status, gathered_ts, pushed = row
        covered_ok = digest in covered
        conditions = self._snapshot(status, gathered_ts, pushed, covered_ok, now)
        push_evidence = conditions["push_evidence"]

        # Cheapest-first (design D3): status -> coverage -> TTL -> push.
        if status == STATUS_FAILED:
            reason = REASON_FAILED_RETRY
        elif status != STATUS_GATHERED_OK or gathered_ts is None:
            # First gather (or missing date): task created, no regather reason.
            if status == STATUS_GATHERED_OK and gathered_ts is None:
                # Data-integrity anomaly: the writer always stamps gathered_ts
                # on success, so a NULL here means external/corrupt state.
                self._note_missing_gathered_ts(digest)
            reason = ""
        elif not covered_ok:
            reason = REASON_COVERAGE_GAP
        elif not conditions["ttl_ok"]:
            reason = REASON_TTL_EXPIRED
        elif conditions["push_changed"]:
            reason = REASON_CHANGED
        else:
            return SkipDecision(
                url=url,
                url_hash=digest,
                known=True,
                skip=True,
                reason="",
                push_evidence=push_evidence,
                conditions=conditions,
            )

        return SkipDecision(
            url=url,
            url_hash=digest,
            known=True,
            skip=False,
            reason=reason,
            push_evidence=push_evidence,
            conditions=conditions,
        )

    @staticmethod
    def _unknown(url: str) -> SkipDecision:
        return SkipDecision(
            url=url,
            url_hash=url_hash(url),
            known=False,
            skip=False,
            conditions={
                "status": None,
                "status_ok": False,
                "coverage_ok": False,
                "ttl_ok": False,
                "push_evidence": False,
                "push_changed": False,
            },
        )

    def _snapshot(self, status, gathered_ts, pushed, covered_ok, now) -> Dict[str, Any]:
        push_evidence = pushed is not None
        push_changed = bool(
            push_evidence
            and gathered_ts is not None
            and float(pushed) > float(gathered_ts) + self.grace_seconds
        )
        ttl_ok = bool(
            gathered_ts is not None
            and (now - float(gathered_ts)) <= self.ttl_hours * _SECONDS_PER_HOUR
        )
        return {
            "status": status,
            "status_ok": status == STATUS_GATHERED_OK,
            "coverage_ok": bool(covered_ok),
            "ttl_ok": ttl_ok,
            "push_evidence": push_evidence,
            "push_changed": push_changed,
        }

    # ------------------------------------------------------------------
    # Accounting / logging
    # ------------------------------------------------------------------

    def _account(self, decision: SkipDecision) -> None:
        with self._stat_lock:
            if decision.known:
                self.evaluated_known += 1
                if decision.push_evidence:
                    self.push_evidence_count += 1
            if decision.skip:
                self.skipped_known += 1
            elif decision.reason in REGATHER_REASONS:
                self._regathered[decision.reason] += 1

    def _log_decision(self, decision: SkipDecision, provider: str) -> None:
        payload = {
            "ts": time.time(),
            "run_id": self.run_id,
            "url": decision.url,
            "url_hash": decision.url_hash,
            "provider": provider,
            "conditions": decision.conditions,
            "decision": "skip",
            "mode": self.mode,
        }
        try:
            with self._log_lock:
                if self._closed:
                    return
                if self._log_handle is None:
                    # Line-buffered: each decision is durable on its own line.
                    self._log_handle = open(self.log_path, "a", encoding="utf-8", buffering=1)
                    # Seed the in-memory byte counter from the file's current size.
                    try:
                        self._log_bytes = os.path.getsize(self.log_path)
                    except OSError:
                        self._log_bytes = 0
                line = json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n"
                line_bytes = len(line.encode("utf-8"))
                # Check rotation threshold before writing.
                if self._log_bytes + line_bytes >= self.decision_log_max_bytes:
                    # Close the handle before rotation.
                    try:
                        self._log_handle.close()
                    except Exception:
                        pass
                    self._log_handle = None
                    # Perform rotation (fail-open).
                    from tools.jsonl_rotate import maybe_rotate_jsonl
                    rotated = maybe_rotate_jsonl(
                        self.log_path,
                        max_bytes=self.decision_log_max_bytes,
                        keep_n=self.decision_log_keep_rotated,
                    )
                    if rotated:
                        with self._stat_lock:
                            self.decision_log_rotations += 1
                        logger.info(
                            f"[gather-skip] rotated decision log after {self._log_bytes} bytes"
                        )
                    self._log_bytes = 0
                    # Reopen handle (lazy; will be opened on next write or now).
                    self._log_handle = open(self.log_path, "a", encoding="utf-8", buffering=1)
                self._log_handle.write(line)
                self._log_bytes += line_bytes
        except Exception as exc:  # decision log is observability, never fatal
            logger.debug(f"[gather-skip] decision log write failed: {exc}")

    def _record_read_error(self, exc: Exception, link_count: int) -> None:
        kind = type(exc).__name__
        with self._stat_lock:
            self.read_errors += 1
            first_of_kind = kind not in self._logged_error_kinds
            if first_of_kind:
                self._logged_error_kinds.add(kind)
                self.read_error_warnings += 1
        if first_of_kind:
            logger.warning(
                f"[gather-skip] registry read failed ({kind}); "
                f"treating {link_count} links as unknown: {exc}"
            )
        self._reset_conn()

    def _note_missing_gathered_ts(self, url_hash_value: str) -> None:
        """Count (and warn once about) `gathered_ok` rows missing `gathered_ts`.

        The writer always stamps `gathered_ts` on success, so this state means
        external mutation or corruption. Fail-open still applies: the link is
        treated as unknown and re-gathered.
        """
        with self._stat_lock:
            self.gathered_ts_missing += 1
            first = self.gathered_ts_missing == 1
        if first:
            logger.warning(
                "[gather-skip] registry anomaly: 'gathered_ok' link without gathered_ts "
                f"(url_hash={url_hash_value[:12]}...); treated as unknown (fail-open). "
                "Further occurrences are counted in the 'gathered_ts_missing' stat."
            )
