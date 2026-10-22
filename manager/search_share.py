"""Search-side cross-provider share ledger (D-03.6/D-03.7, F-07, NEW-3/N-09).

Mirror of :class:`manager.gather_share.GatherShareLedger` (R17) for the search
stage.  The key is ``(wire_fp, page)`` — provider is NOT part of the key,
because the answer is a function of the wire query and page, not the provider.
The ``patterns_hash`` is carried alongside so the resolver can determine
beneficiaries at ``take`` time (F-07 fan-out).

Design constraints
------------------
- **Lives outside the task payload.**  The durable queue serialises at
  ``put()``, so a later claim by a second provider cannot amend a row already
  persisted.  The beneficiary set lives in RAM, not in the task.
- **Cap is a seatbelt.**  When the cap is reached, new keys fail open (claim
  accepts without tracking) and ``ledger_full`` increments.
- **Single threading.Lock.**  Search is single-threaded but the lock closes
  the concurrency gap (mirror R7).
- **Run-scoped in-RAM.**  Lost on restart; within-run duplicates are cheap
  (aggregation + basket pays again — acceptable fail-open).
"""

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

DEFAULT_MAX_TRACKED = 50_000
MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ON = "on"
VALID_MODES = (MODE_OFF, MODE_SHADOW, MODE_ON)

# Key type: (wire_fp, page)
_Key = Tuple[str, int]


class SearchShareLedger:
    """In-memory ledger mapping search keys to their beneficiary providers.

    D-03.6/D-03.7 API:
        claim   — register a provider's interest in a key; returns True if
                  the caller should suppress its own task.
        take    — atomically remove and return the ordered provider list.
        abandon — remove the entry without distributing outputs.
        to_stats — snapshot of all counters for observability.
        close   — release all entries; safe to call twice.
    """

    def __init__(
        self,
        mode: str = MODE_OFF,
        max_tracked: int = DEFAULT_MAX_TRACKED,
        beneficiaries_resolver: Optional[Callable[[str], List[str]]] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        # Invalid mode degrades to off (mirror gather_share.py:65).
        self._mode: str = mode if mode in VALID_MODES else MODE_OFF
        self._max_tracked: int = max_tracked
        self._beneficiaries_resolver = beneficiaries_resolver
        self._clock: Callable[[], float] = clock if clock is not None else time.monotonic
        self._lock = threading.Lock()

        # Entry map: key -> (owner_provider, patterns_hash_value)
        # Only used in "on" mode.
        self._entries: Dict[_Key, Tuple[str, str]] = {}

        # Shadow-mode seen-set: tracks which keys have been claimed so that
        # would_suppress can be counted.  Separate from _entries because
        # shadow must NOT register entries (tracked == 0).
        self._shadow_seen: Set[_Key] = set()

        # Counters
        self._claims: int = 0
        self._would_suppress: int = 0
        self._suppressed: int = 0
        self._takes: int = 0
        self._shared_fetches: int = 0
        self._fanout_extra: int = 0
        self._abandoned: int = 0
        self._ledger_full: int = 0

        # Loud-once guard for cap-refusal WARNING.
        self._warned_full: bool = False

    # -- properties ----------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """True when the ledger is in shadow or on mode."""
        return self._mode != MODE_OFF

    @property
    def enforce(self) -> bool:
        """True only in on mode (shadow counts but does not suppress)."""
        return self._mode == MODE_ON

    # -- core API ------------------------------------------------------------

    def claim(self, wire_fp: str, page: int, provider: str, patterns_hash_value: str) -> bool:
        """Register a provider's interest in a search key.

        Returns True if the caller should suppress its own task (another
        provider already owns this key).  Returns False in all other cases.

        In off mode: returns False immediately, zero overhead.
        In shadow mode: counts would_suppress when a different provider already
            registered, but always returns False and never registers entries.
        In on mode: first claim registers the owner and returns False; a later
            claim by a different provider returns True (suppress); repeated
            claim by the same provider is idempotent.
        """
        if self._mode == MODE_OFF:
            return False

        key: _Key = (wire_fp, page)

        with self._lock:
            self._claims += 1

            if self._mode == MODE_SHADOW:
                # Pure counting: no entries registered (tracked stays 0).
                if key in self._shadow_seen:
                    self._would_suppress += 1
                else:
                    # Shadow seen-set bounded by same cap (mirror C2b).
                    if len(self._shadow_seen) >= self._max_tracked:
                        self._ledger_full += 1
                        if not self._warned_full:
                            self._warned_full = True
                            logger.warning(
                                "SearchShareLedger: shadow seen-set cap reached (%d); "
                                "new keys fail open (counted but not tracked)",
                                self._max_tracked,
                            )
                    else:
                        self._shadow_seen.add(key)
                return False

            # -- on mode --
            if key in self._entries:
                owner, _ph = self._entries[key]
                if owner != provider:
                    self._suppressed += 1
                    return True
                # Idempotent: same provider again, no change.
                return False

            # New key: check cap.
            if len(self._entries) >= self._max_tracked:
                self._ledger_full += 1
                if not self._warned_full:
                    self._warned_full = True
                    logger.warning(
                        "SearchShareLedger: tracked-key cap reached (%d); "
                        "new keys fail open (caller creates own task)",
                        self._max_tracked,
                    )
                return False

            # Register the owner with its patterns_hash.
            self._entries[key] = (provider, patterns_hash_value)
            return False

    def take(self, wire_fp: str, page: int) -> List[str]:
        """Atomically remove and return the ordered provider list for a key.

        Returns [] when the key is absent or mode is off.  When the list has
        more than one provider, increments shared_fetches and fanout_extra.
        Also increments the ``takes`` counter.

        Beneficiaries are resolved via the ``beneficiaries_resolver`` callback
        (F-07): the owner is always first, followed by other providers with
        the same patterns_hash in sorted order.
        """
        if self._mode == MODE_OFF:
            return []

        key: _Key = (wire_fp, page)

        with self._lock:
            self._takes += 1
            entry = self._entries.pop(key, None)
            if entry is None:
                return []

            owner, ph = entry

            # Resolve beneficiaries.
            if self._beneficiaries_resolver is not None:
                try:
                    extra = self._beneficiaries_resolver(ph)
                except Exception:
                    extra = []
                # Owner first, then sorted others (excluding owner).
                beneficiaries = [owner]
                for p in sorted(extra):
                    if p != owner:
                        beneficiaries.append(p)
            else:
                beneficiaries = [owner]

            if len(beneficiaries) > 1:
                self._shared_fetches += 1
                self._fanout_extra += len(beneficiaries) - 1
            return beneficiaries

    def abandon(self, wire_fp: str, page: int) -> int:
        """Remove the entry and return how many beneficiaries were dropped.

        Returns 0 when the key is absent or mode is off.  The ``abandoned``
        counter is incremented by max(0, dropped - 1), i.e. the beneficiaries
        that lost their attribution (excluding the owner).
        """
        if self._mode == MODE_OFF:
            return 0

        key: _Key = (wire_fp, page)

        with self._lock:
            entry = self._entries.pop(key, None)
            if entry is None:
                return 0

            owner, ph = entry

            # Count beneficiaries that are dropped.
            if self._beneficiaries_resolver is not None:
                try:
                    extra = self._beneficiaries_resolver(ph)
                except Exception:
                    extra = []
                beneficiaries = [owner]
                for p in sorted(extra):
                    if p != owner:
                        beneficiaries.append(p)
            else:
                beneficiaries = [owner]

            dropped = len(beneficiaries)
            # Beneficiaries minus the owner.
            if dropped > 1:
                self._abandoned += dropped - 1
            return dropped

    def to_stats(self) -> Dict[str, Any]:
        """Snapshot of all counters.

        Returns EXACTLY these keys in this order:
        mode, claims, would_suppress, suppressed, takes, shared_fetches,
        fanout_extra, abandoned, ledger_full, tracked.
        """
        with self._lock:
            tracked = len(self._entries)
        return {
            "mode": self._mode,
            "claims": self._claims,
            "would_suppress": self._would_suppress,
            "suppressed": self._suppressed,
            "takes": self._takes,
            "shared_fetches": self._shared_fetches,
            "fanout_extra": self._fanout_extra,
            "abandoned": self._abandoned,
            "ledger_full": self._ledger_full,
            "tracked": tracked,
        }

    def close(self) -> None:
        """Clear all entries.  Safe to call twice."""
        with self._lock:
            self._entries.clear()
            self._shadow_seen.clear()
