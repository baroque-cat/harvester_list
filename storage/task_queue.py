#!/usr/bin/env python3

"""
Durable SQLite-backed task queue (fix-queue-persistence-under-load).

``SqliteTaskQueue`` is a duck-typed replacement for the in-memory
``queue.Queue`` surface used by pipeline stages::

    put(obj, timeout=None, *, dedup_id="", created_at=None, attempts=0)
    put_nowait(obj, **kwargs)
    get(timeout=None) -> obj          # claims; raises queue.Empty
    get_nowait() -> obj
    task_done()                       # acks (deletes) this thread's claim
    empty() / qsize() / full()
    counts() / snapshot_pending() / sweep_expired_claims()
    bulk_insert(objs) / checkpoint() / close() / journal_mode

Design D1-D6:
- one row per task, strict FIFO by monotonic ``seq``;
- ``put`` is a committed INSERT, so ``True`` (no exception) means durable and
  ``queue.Full`` is unreachable (no capacity bound);
- ``get`` atomically claims the oldest pending row (``UPDATE ... RETURNING``)
  and starts a visibility timeout;
- ``task_done`` deletes the calling thread's claimed row;
- startup reclaims orphaned ``claimed`` rows (at-least-once after a crash) and
  purges rows older than ``max_age_hours`` loudly.

The engine is storage-only: task (de)serialization is injected by the caller.
"""

import json
import os
import queue
import sqlite3
import threading
import time
import weakref
from typing import Any, Callable, Dict, List, Optional

from tools.logger import get_logger

logger = get_logger("storage")

# Bump only with an additive migration (mirrors storage/registry.py).
SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS tasks (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_id      TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    state         TEXT NOT NULL DEFAULT 'pending'
                  CHECK (state IN ('pending', 'claimed')),
    claimed_until REAL,
    created_at    REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_state_seq ON tasks(state, seq);
CREATE INDEX IF NOT EXISTS idx_tasks_claimed ON tasks(claimed_until) WHERE state = 'claimed';
"""


class SqliteTaskQueue:
    """Durable FIFO task queue stored in a single SQLite (WAL) file.

    All access serializes through one internal lock around one connection
    (one-writer discipline, design D3).  Wakeups for blocking ``get`` are
    signalled through a ``threading.Condition`` bound to that same lock, so
    consumers sleep instead of polling (design D2).
    """

    def __init__(
        self,
        path: str,
        *,
        serializer: Callable[[Any], str],
        deserializer: Callable[[str], Any],
        name: str = "",
        visibility_timeout_s: float = 300.0,
        max_age_hours: float = 24.0,
    ) -> None:
        self.path = path
        self.serializer = serializer
        self.deserializer = deserializer
        self.name = name
        self.visibility_timeout_s = float(visibility_timeout_s)
        self.max_age_hours = float(max_age_hours)

        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._local = threading.local()
        # Best-effort in-process identity cache: when a producer still holds
        # the object it enqueued, ``get`` hands that same object back (so a
        # worker's in-place mutation, e.g. ``task.attempts += 1``, is visible
        # to the enqueuer).  Weak values keep the footprint bounded -- an
        # object only stays cached while someone else references it, so the
        # queue never retains the task graph (design D2).
        self._objects: "weakref.WeakValueDictionary[int, Any]" = weakref.WeakValueDictionary()

        # Parent directory is created here so stage construction just works
        # (the queue_state directory may not exist on a first run).  Any
        # failure propagates to the stage's fail-open path (design D7).
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)

        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
        # Registry pragmas verbatim (storage/registry.py:462-464).
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")

        self._bootstrap()
        self._recover_on_open()

    # ------------------------------------------------------------------
    # Construction / maintenance
    # ------------------------------------------------------------------
    def _bootstrap(self) -> None:
        """Create tables/indexes if missing and record the schema version."""
        self._conn.executescript(_DDL)
        self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._conn.commit()

    def _recover_on_open(self) -> None:
        """Reclaim crash orphans and purge aged-out rows (design D1/D14).

        Runs inside the constructor, before any worker can attach, so a
        recovered at-least-once replay is never racing a live consumer.
        """
        with self._cond:
            reclaimed = self._conn.execute(
                "UPDATE tasks SET state='pending', claimed_until=NULL WHERE state='claimed'"
            ).rowcount
            self._conn.commit()
            if reclaimed:
                logger.info(f"[{self.name}] reclaimed {reclaimed} orphaned claimed task(s) at startup")

            cutoff = time.time() - self.max_age_hours * 3600.0
            purged = self._conn.execute("DELETE FROM tasks WHERE created_at < ?", (cutoff,)).rowcount
            self._conn.commit()
            if purged:
                logger.warning(
                    f"[{self.name}] purged {purged} aged-out task(s) older than {self.max_age_hours}h at startup"
                )

    def checkpoint(self) -> None:
        """Run a passive WAL checkpoint (bounded file growth)."""
        with self._cond:
            self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            self._conn.commit()

    def close(self) -> None:
        """Close the underlying connection."""
        with self._cond:
            try:
                self._conn.close()
            except Exception:  # pragma: no cover - defensive
                pass

    @property
    def journal_mode(self) -> str:
        """Current SQLite journal mode (``wal`` expected)."""
        with self._cond:
            return self._conn.execute("PRAGMA journal_mode").fetchone()[0]

    # ------------------------------------------------------------------
    # Producer surface
    # ------------------------------------------------------------------
    def put(
        self,
        obj: Any,
        timeout: Optional[float] = None,
        *,
        dedup_id: str = "",
        created_at: Optional[float] = None,
        attempts: int = 0,
    ) -> bool:
        """Durably commit one task.

        Never raises ``queue.Full`` (there is no capacity bound).  Returns True
        once the INSERT is committed (design D6).
        """
        payload = self.serializer(obj)
        created = float(created_at) if created_at is not None else time.time()
        with self._cond:
            cur = self._conn.execute(
                "INSERT INTO tasks (dedup_id, payload_json, attempts, state, claimed_until, created_at) "
                "VALUES (?, ?, ?, 'pending', NULL, ?)",
                (dedup_id, payload, int(attempts), created),
            )
            self._conn.commit()
            self._remember(cur.lastrowid, obj)
            self._cond.notify_all()
        return True

    def put_nowait(self, obj: Any, **kwargs: Any) -> bool:
        """Non-blocking ``put`` (identical semantics: commit before success)."""
        return self.put(obj, timeout=0, **kwargs)

    def bulk_insert(self, objs: Any) -> int:
        """Insert many tasks in a single transaction (legacy-importer feed).

        ``dedup_id`` is left empty; ``created_at`` is taken from each task when
        available so the age gate stays meaningful.
        """
        count = 0
        with self._cond:
            for obj in objs:
                payload = self.serializer(obj)
                created = getattr(obj, "created_at", None)
                created = float(created) if created is not None else time.time()
                self._conn.execute(
                    "INSERT INTO tasks (dedup_id, payload_json, attempts, state, claimed_until, created_at) "
                    "VALUES (?, ?, 0, 'pending', NULL, ?)",
                    ("", payload, created),
                )
                count += 1
            self._conn.commit()
            if count:
                self._cond.notify_all()
        return count

    # ------------------------------------------------------------------
    # Consumer surface
    # ------------------------------------------------------------------
    def get(self, timeout: Optional[float] = None) -> Any:
        """Claim the oldest available row and return its deserialized payload.

        Raises ``queue.Empty`` when ``timeout`` elapses without a claim.  A
        ``None`` timeout blocks until a task is available.
        """
        with self._cond:
            item = self._claim_locked()
            if item is not None:
                return item
            if timeout is not None and timeout <= 0:
                raise queue.Empty

            if timeout is None:
                while True:
                    self._cond.wait()
                    item = self._claim_locked()
                    if item is not None:
                        return item

            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise queue.Empty
                self._cond.wait(remaining)
                item = self._claim_locked()
                if item is not None:
                    return item

    def get_nowait(self) -> Any:
        """Claim a row if one is immediately available, else raise ``queue.Empty``."""
        return self.get(timeout=0)

    def _claim_locked(self) -> Optional[Any]:
        """Atomically claim one row (caller holds the lock)."""
        now = time.time()
        cur = self._conn.execute(
            "UPDATE tasks SET state='claimed', claimed_until=? "
            "WHERE seq = ("
            "  SELECT seq FROM tasks "
            "  WHERE state='pending' OR (state='claimed' AND claimed_until < ?) "
            "  ORDER BY seq LIMIT 1"
            ") RETURNING seq, payload_json",
            (now + self.visibility_timeout_s, now),
        )
        row = cur.fetchone()
        if row is None:
            self._conn.commit()
            return None

        self._conn.commit()
        seq, payload = row
        self._claim_stack().append(seq)
        cached = self._objects.get(seq)
        if cached is not None:
            return cached
        return self._decode(payload)

    def _remember(self, seq: Optional[int], obj: Any) -> None:
        """Cache ``obj`` for in-process identity (best-effort, weak)."""
        if seq is None:
            return
        try:
            self._objects[seq] = obj
        except TypeError:
            # Not weak-referenceable (e.g. a plain dict payload); durability
            # is unaffected -- the row is already committed.
            pass

    def _decode(self, payload: str) -> Any:
        """Deserialize a stored payload.

        Two conventions are in use by callers: a text-decoder (``json.loads``)
        and a dict-factory (``TaskFactory.from_dict``) paired with a JSON-text
        serializer.  Try the text form first and fall back to the parsed form
        when the deserializer rejects a string payload.
        """
        try:
            return self.deserializer(payload)
        except (TypeError, AttributeError):
            # These are exactly the errors a dict-factory raises on a string
            # payload (and vice-versa); retry with the parsed form.
            return self.deserializer(json.loads(payload))

    def task_done(self) -> None:
        """Acknowledge the calling thread's most recent claim (deletes the row)."""
        stack = self._claim_stack()
        if not stack:
            raise ValueError("task_done() called too many times")
        seq = stack.pop()
        with self._cond:
            self._conn.execute("DELETE FROM tasks WHERE seq = ?", (seq,))
            self._conn.commit()
            self._objects.pop(seq, None)
            self._cond.notify_all()

    def _claim_stack(self) -> List[int]:
        """Per-thread stack of claimed ``seq`` values (design D5)."""
        stack = getattr(self._local, "claims", None)
        if stack is None:
            stack = []
            self._local.claims = stack
        return stack

    # ------------------------------------------------------------------
    # Introspection surface
    # ------------------------------------------------------------------
    def qsize(self) -> int:
        """Number of pending (unclaimed) rows."""
        with self._cond:
            return int(self._conn.execute("SELECT COUNT(*) FROM tasks WHERE state='pending'").fetchone()[0])

    def empty(self) -> bool:
        """True when no row is pending."""
        return self.qsize() == 0

    def full(self) -> bool:
        """Always False: the disk store has no capacity bound."""
        return False

    def counts(self) -> Dict[str, int]:
        """Return ``{"pending": n, "claimed": m}``."""
        with self._cond:
            pending = int(
                self._conn.execute("SELECT COUNT(*) FROM tasks WHERE state='pending'").fetchone()[0]
            )
            claimed = int(
                self._conn.execute("SELECT COUNT(*) FROM tasks WHERE state='claimed'").fetchone()[0]
            )
        return {"pending": pending, "claimed": claimed}

    def snapshot_pending(self) -> List[Any]:
        """Non-destructive FIFO list of pending payloads (design S14)."""
        with self._cond:
            rows = self._conn.execute(
                "SELECT payload_json FROM tasks WHERE state='pending' ORDER BY seq"
            ).fetchall()
        return [self._decode(row[0]) for row in rows]

    def sweep_expired_claims(self) -> int:
        """Return expired unacknowledged claims to pending; return the count."""
        now = time.time()
        with self._cond:
            reclaimed = self._conn.execute(
                "UPDATE tasks SET state='pending', claimed_until=NULL "
                "WHERE state='claimed' AND claimed_until < ?",
                (now,),
            ).rowcount
            self._conn.commit()
            if reclaimed:
                self._cond.notify_all()
        return int(reclaimed)
