"""Cross-provider fetch-sharing ledger (R7 D1, D2, D3).

Design constraints
------------------
- **Lives outside the task payload.**  The durable queue serialises at
  ``put()`` (``stage/definition.py``), so a later claim by a second provider
  cannot amend a row already persisted.  The beneficiary set therefore lives
  in RAM, not in the task.  Losing it on restart fails open: each provider
  re-creates its own task on the next run (R7 D1).

- **Cap is a seatbelt.**  The live set is bounded by the gather backlog
  (~10 K entries × ~400 B ≈ 4 MB at ``DEFAULT_MAX_TRACKED_KEYS = 50_000``).
  When the cap is reached a new key's claim returns False (fail-open: the
  caller creates its own task) and increments ``ledger_full``.

- **Fetch is shared, rate-limit basket is not** (invariant 1, R7 D7).
  The ledger records which providers benefit from a single fetch so the
  gather worker can fan out downstream outputs.  The rate-limit token is
  consumed once per physical fetch; each beneficiary's coverage row is
  written independently, but the fetch itself is not repeated.

- **Single threading.Lock.**  Search is single-threaded, gather has multiple
  workers; the lock closes the concurrency gap (R7 D1, stone #5).

- **Module-level constants.**  ``DEFAULT_MAX_TRACKED_KEYS`` mirrors the
  project convention of module-level constants (cf. ``DEFAULT_DATING_BUDGET_SECONDS``).
"""

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

DEFAULT_MAX_TRACKED_KEYS = 50_000
MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ON = "on"
VALID_MODES = (MODE_OFF, MODE_SHADOW, MODE_ON)

# Key type: (url_hash, patterns_hash, transport)
_Key = Tuple[str, str, str]


class GatherShareLedger:
    """In-memory ledger mapping fetch keys to their beneficiary providers.

    R7 D2 API:
        claim   — register a provider's interest in a key; returns True if
                  the task should be suppressed (another provider already owns it).
        take    — atomically remove and return the ordered provider list.
        abandon — remove the entry without distributing outputs.
        to_stats — snapshot of all counters for observability.
        close   — release all entries; safe to call twice.
    """

    def __init__(
        self,
        mode: str = MODE_OFF,
        max_tracked: int = DEFAULT_MAX_TRACKED_KEYS,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        # R7 D1: invalid mode degrades to off (mirror storage/gather_skip.py:106).
        self._mode: str = mode if mode in VALID_MODES else MODE_OFF
        self._max_tracked: int = max_tracked
        self._clock: Callable[[], float] = clock if clock is not None else time.monotonic
        self._lock = threading.Lock()

        # Beneficiary map: key -> ordered list of providers (owner first).
        # Only used in "on" mode.
        self._entries: Dict[_Key, List[str]] = {}

        # Shadow-mode seen-set: tracks which keys have been claimed so that
        # would_suppress can be counted.  Separate from _entries because
        # shadow must NOT register entries (tracked == 0, R7 L8 pin).
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

    def claim(self, url_hash: str, patterns_hash: str, transport: str, provider: str) -> bool:
        """Register a provider's interest in a fetch key.

        Returns True if the caller should suppress its own task (another
        provider already owns this key).  Returns False in all other cases.

        In off mode: returns False immediately, zero overhead (R7 D2).
        In shadow mode: counts would_suppress when a different provider already
            registered, but always returns False and never registers entries
            (pure counting, no behavioural change).
        In on mode: first claim registers the owner and returns False; a later
            claim by a different provider returns True (suppress); repeated
            claim by the same provider is idempotent.
        """
        if self._mode == MODE_OFF:
            return False

        key: _Key = (url_hash, patterns_hash, transport)

        with self._lock:
            self._claims += 1

            if self._mode == MODE_SHADOW:
                # Pure counting: no entries registered (tracked stays 0).
                if key in self._shadow_seen:
                    self._would_suppress += 1
                else:
                    # Slice 2.1 (C2b): shadow seen-set bounded by same cap.
                    if len(self._shadow_seen) >= self._max_tracked:
                        self._ledger_full += 1
                        if not self._warned_full:
                            self._warned_full = True
                            logger.warning(
                                "GatherShareLedger: shadow seen-set cap reached (%d); "
                                "new keys fail open (counted but not tracked)",
                                self._max_tracked,
                            )
                    else:
                        self._shadow_seen.add(key)
                return False

            # -- on mode --
            if key in self._entries:
                providers = self._entries[key]
                if provider not in providers:
                    providers.append(provider)
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
                        "GatherShareLedger: tracked-key cap reached (%d); "
                        "new keys fail open (caller creates own task)",
                        self._max_tracked,
                    )
                return False

            # Register the owner.
            self._entries[key] = [provider]
            return False

    def take(self, url_hash: str, patterns_hash: str, transport: str) -> List[str]:
        """Atomically remove and return the ordered provider list for a key.

        Returns [] when the key is absent or mode is off.  When the list has
        more than one provider, increments shared_fetches and fanout_extra.
        Also increments the ``takes`` counter so the ratio takes/claims is
        observable.
        """
        if self._mode == MODE_OFF:
            return []

        key: _Key = (url_hash, patterns_hash, transport)

        with self._lock:
            self._takes += 1
            providers = self._entries.pop(key, None)
            if providers is None:
                return []
            if len(providers) > 1:
                self._shared_fetches += 1
                self._fanout_extra += len(providers) - 1
            return list(providers)

    def abandon(self, url_hash: str, patterns_hash: str, transport: str) -> int:
        """Remove the entry and return how many providers were dropped.

        Returns 0 when the key is absent or mode is off.  The ``abandoned``
        counter is incremented by (dropped - 1), i.e. the beneficiaries that
        lost their attribution (excluding the owner).
        """
        if self._mode == MODE_OFF:
            return 0

        key: _Key = (url_hash, patterns_hash, transport)

        with self._lock:
            providers = self._entries.pop(key, None)
            if providers is None:
                return 0
            dropped = len(providers)
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
