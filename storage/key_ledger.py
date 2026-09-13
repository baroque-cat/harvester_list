#!/usr/bin/env python3

"""
Persistent key ledger decision engine (add-key-ledger).

The registry ``keys`` table is a persistent memory of credential identities and
their last verification status.  When ``check_skip=on``, the CheckStage consults
this engine *before* invoking a provider: a known key whose last real re-check is
fresher than the TTL configured for its status is skipped (no provider call, no
duplicate shard record); everything else is checked exactly as before.

Design constraints (see openspec/changes/add-key-ledger/design.md):

- Identity is ``key_hash = sha256("provider|key|address|endpoint")`` (D1),
  mirroring the in-memory CheckTask dedup id.
- Only hashes and masked references are ever persisted; the full secret never
  reaches the registry, its WAL, logs or metrics (D2/D7).
- Lookups use dedicated read-only SQLite connections (``file:...?mode=ro``)
  opened lazily per worker thread; WAL keeps them snapshot-consistent beside the
  writer thread (same pattern as add-gather-skip).
- Fail-open: any lookup error degrades to "check it", warns once per error kind,
  and marks the run degraded.  A skip is never derived from an errored lookup.
- ``off`` mode performs no registry read at all.
"""

import hashlib
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from tools.logger import get_logger

logger = get_logger("storage")

MODE_OFF = "off"
MODE_ON = "on"
VALID_MODES = (MODE_OFF, MODE_ON)

REGISTRY_FILENAME = "registry.sqlite"

# Per-status freshness windows (design D3).  wait_check means "provider refused
# temporarily -- retry soon"; no_quota changes on recharge; invalid can resurrect
# via rotation but rarely; valid keys are empirically long-lived.
DEFAULT_TTL_HOURS: Dict[str, float] = {
    "wait_check": 12.0,
    "no_quota": 72.0,
    "invalid": 168.0,
    "valid": 336.0,
}
STATUSES = ("valid", "wait_check", "invalid", "no_quota")

REASON_UNKNOWN = "unknown"
REASON_FRESH = "fresh"
REASON_EXPIRED = "expired"

_SECONDS_PER_HOUR = 3600.0


def key_hash(provider: str, key: str, address: str = "", endpoint: str = "") -> str:
    """Deterministic identity hash ``sha256("provider|key|address|endpoint")``.

    Unsalted on purpose: this is a join key, not a secret-protection mechanism
    (the registry is a local file; masking handles exposure).  Address/endpoint
    are part of the identity because the same secret against different endpoints
    legitimately has different statuses.
    """
    payload = f"{provider or ''}|{key or ''}|{address or ''}|{endpoint or ''}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def mask_key(key: str) -> str:
    """Masked reference ``<first6>…<last4>`` (useless for reconstruction).

    Short strings (<= 10 chars) are masked entirely rather than joined, so a
    complete short secret can never be reconstructed from the stored reference.
    """
    text = key or ""
    if not text:
        return ""
    if len(text) <= 16:
        return "…"
    return f"{text[:6]}…{text[-4:]}"


@dataclass
class KeyCheckRequest:
    """One credential up for verification."""

    provider: str
    key: str
    address: str = ""
    endpoint: str = ""
    source_url_hash: str = ""


@dataclass
class KeyDecision:
    """Outcome of consulting the ledger for one credential.

    ``skip`` is the enforced decision: True only when the key is known, its
    stored status is conclusive, and its ``last_recheck_ts`` is within the TTL
    configured for that status.
    """

    key_hash: str
    provider: str = ""
    status: Optional[str] = None
    known: bool = False
    skip: bool = False
    reason: str = REASON_UNKNOWN
    last_recheck_ts: Optional[float] = None
    ttl_hours: float = 0.0


class KeyLedger:
    """Read-only ledger lookups + per-status TTL skip rule.

    Thread-safe: each thread gets its own read-only connection and counters are
    guarded by a lock.  Every public call fails open.
    """

    def __init__(
        self,
        workspace: str,
        mode: str = MODE_OFF,
        ttl_hours: Optional[Dict[str, float]] = None,
        registry_path: Optional[str] = None,
        run_id: Optional[str] = None,
        clock: Optional[Callable[[], float]] = None,
        degraded_cb: Optional[Callable[[], None]] = None,
    ):
        self.workspace = str(workspace)
        self.mode = mode if mode in VALID_MODES else MODE_OFF
        self.ttl_hours: Dict[str, float] = {status: float(DEFAULT_TTL_HOURS[status]) for status in STATUSES}
        if ttl_hours:
            for status, hours in ttl_hours.items():
                try:
                    self.ttl_hours[str(status)] = float(hours)
                except (TypeError, ValueError):
                    continue
        self.registry_path = registry_path or os.path.join(self.workspace, REGISTRY_FILENAME)
        self.run_id = run_id
        self._clock = clock or time.time
        self._degraded_cb = degraded_cb

        self._local = threading.local()
        self._stat_lock = threading.Lock()
        self._conn_lock = threading.Lock()
        self._conns: set = set()
        self._closed = False

        self.check_skipped_by_status: Dict[str, int] = {status: 0 for status in STATUSES}
        self.provider_calls_saved = 0
        self.registry_reads = 0
        self.read_errors = 0
        self.read_error_warnings = 0
        self._logged_error_kinds: set = set()

    # ------------------------------------------------------------------
    # Capabilities
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """True when the engine may consult the ledger (mode == on)."""
        return self.mode == MODE_ON

    def has_read_errors(self) -> bool:
        """Thread-safe kill-switch probe: True once any lookup has errored."""
        with self._stat_lock:
            return self.read_errors > 0

    def ttl_for(self, status: Optional[str]) -> float:
        """TTL in seconds for a stored status (unknown status => 0 => expired)."""
        if status is None:
            return 0.0
        return self.ttl_hours.get(str(status), 0.0) * _SECONDS_PER_HOUR

    # ------------------------------------------------------------------
    # Decision path
    # ------------------------------------------------------------------

    def decide(self, requests: Sequence[Any], now: Optional[float] = None) -> List[KeyDecision]:
        """Evaluate a batch of credentials, one decision per request in order."""
        request_list = list(requests or [])
        if self.mode == MODE_OFF:
            return [self._check_only(req) for req in request_list]

        # Compute each identity once and reuse it for both the lookup and rule.
        pairs = [(req, self._digest(req)) for req in request_list]
        digests = {digest for _req, digest in pairs}
        try:
            rows = self._lookup(digests)
        except Exception as exc:  # fail-open in the safe direction
            self._record_read_error(exc, len(request_list))
            return [self._check_only(req) for req in request_list]

        moment = self._clock() if now is None else float(now)
        decisions: List[KeyDecision] = []
        for req, digest in pairs:
            decision = self._decide_one(req, digest, rows, moment)
            decisions.append(decision)
            self._account(decision)
        return decisions

    def to_stats(self) -> Dict[str, Any]:
        """Flatten per-run counters for run statistics / status display."""
        with self._stat_lock:
            return {
                "mode": self.mode,
                "check_skipped_by_status": dict(self.check_skipped_by_status),
                "provider_calls_saved": self.provider_calls_saved,
                "registry_reads": self.registry_reads,
                "read_errors": self.read_errors,
            }

    def close(self) -> None:
        """Close every read-only connection (never raises)."""
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

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Open (once per thread) a read-only connection to the registry."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            uri = Path(os.path.abspath(self.registry_path)).as_uri() + "?mode=ro"
            # check_same_thread=False lets the main thread close connections
            # owned by worker threads at shutdown; each is otherwise single-thread.
            conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=5.0)
            self._local.conn = conn
            with self._conn_lock:
                self._conns.add(conn)
        return conn

    def _lookup(self, digests: Sequence[str]) -> Dict[str, tuple]:
        """Batched lookup: ``key_hash -> (status, last_recheck_ts)``."""
        digest_list = list(dict.fromkeys(digests))
        if not digest_list:
            return {}
        conn = self._connect()
        with self._stat_lock:
            self.registry_reads += 1
        placeholders = ",".join("?" for _ in digest_list)
        rows = conn.execute(
            "SELECT key_hash, status, last_recheck_ts FROM keys "
            f"WHERE key_hash IN ({placeholders})",
            digest_list,
        ).fetchall()
        return {row[0]: (row[1], row[2]) for row in rows}

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

    def _digest(self, req: Any) -> str:
        if isinstance(req, KeyCheckRequest):
            return key_hash(req.provider, req.key, req.address, req.endpoint)
        return key_hash(
            getattr(req, "provider", ""),
            getattr(req, "key", ""),
            getattr(req, "address", ""),
            getattr(req, "endpoint", ""),
        )

    def _decide_one(self, req: Any, digest: str, rows: Dict[str, tuple], now: float) -> KeyDecision:
        provider = getattr(req, "provider", "") if not isinstance(req, KeyCheckRequest) else req.provider
        row = rows.get(digest)
        if row is None:
            return KeyDecision(key_hash=digest, provider=provider, known=False, skip=False, reason=REASON_UNKNOWN)

        status, last_recheck = row
        ttl_seconds = self.ttl_for(status)
        # NULL / inconsistent last_recheck_ts resolves to "check it".
        if last_recheck is None or status is None or ttl_seconds <= 0:
            return KeyDecision(
                key_hash=digest,
                provider=provider,
                status=status,
                known=True,
                skip=False,
                reason=REASON_EXPIRED,
                last_recheck_ts=last_recheck,
                ttl_hours=self.ttl_hours.get(str(status), 0.0),
            )

        fresh = (now - float(last_recheck)) <= ttl_seconds
        return KeyDecision(
            key_hash=digest,
            provider=provider,
            status=status,
            known=True,
            skip=bool(fresh),
            reason=REASON_FRESH if fresh else REASON_EXPIRED,
            last_recheck_ts=float(last_recheck),
            ttl_hours=self.ttl_hours.get(str(status), 0.0),
        )

    @staticmethod
    def _check_only(req: Any) -> KeyDecision:
        return KeyDecision(
            key_hash="",
            provider=getattr(req, "provider", "") if not isinstance(req, KeyCheckRequest) else req.provider,
            known=False,
            skip=False,
            reason=REASON_UNKNOWN,
        )

    # ------------------------------------------------------------------
    # Accounting / logging
    # ------------------------------------------------------------------

    def _account(self, decision: KeyDecision) -> None:
        if not decision.skip:
            return
        with self._stat_lock:
            status = str(decision.status)
            self.check_skipped_by_status[status] = self.check_skipped_by_status.get(status, 0) + 1
            self.provider_calls_saved += 1

    def _record_read_error(self, exc: Exception, key_count: int) -> None:
        kind = type(exc).__name__
        with self._stat_lock:
            self.read_errors += 1
            first_of_kind = kind not in self._logged_error_kinds
            if first_of_kind:
                self._logged_error_kinds.add(kind)
                self.read_error_warnings += 1
        if first_of_kind:
            logger.warning(
                f"[key-ledger] registry read failed ({kind}); "
                f"treating {key_count} keys as unknown (fail-open): {exc}"
            )
        self._reset_conn()
        if self._degraded_cb is not None:
            try:
                self._degraded_cb()
            except Exception:  # pragma: no cover - defensive
                pass
