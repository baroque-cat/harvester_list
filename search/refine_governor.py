#!/usr/bin/env python3

"""Search-work fan-out governor (add-refine-fanout-governor, design D1-D9).

``RefineGovernor`` sits strictly between "the refine engine produced candidate
children" and "children enter the search queue".  It never rewrites wire
queries: it clamps the partition count handed to the engine, sorts/truncates
the engine's output, and admits children within a per-run budget.

Modes (tri-mode rollout flag):

- ``off``    -- passthrough: input order preserved, all counters inert.
- ``shadow`` -- everything is enqueued exactly as in ``off`` while every
                would-be clamp/truncation/budget decision is computed, counted
                and logged (measures the coverage price before enforcement).
- ``on``     -- decisions enforced.

Admission order is ascending ``search/querykey.fingerprint`` (design D5): the
sha256 of ``"<api|web>|<wire_query>"`` is stable across processes and
provider-independent, so identical inputs admit identical children regardless of
the engine's internal (set-based, cross-process salted) ordering, and twins of
the same query from different providers converge on the same survivor set.
Fingerprint order also spreads truncation gaps pseudo-randomly over the query
space instead of permanently sacrificing one fixed region (lexicographic order
always drops the alphabet tail -- measured on the 2026-09-21 incident seed at
cap 128: ``w x y z`` excluded on every run).

All mutable state lives behind a single ``threading.Lock`` because
``WorkerManager`` may scale the search stage at runtime.
"""

import threading
from typing import Any, Dict, List

from search.querykey import fingerprint
from tools.logger import get_logger

logger = get_logger("search")

_VALID_MODES = ("off", "shadow", "on")


class RefineGovernor:
    """Bounded, deterministic, observable search-work generation."""

    def __init__(
        self,
        mode: str = "on",
        max_refine_depth: int = 2,
        max_partitions_per_refine: int = 128,
        max_search_tasks_per_run: int = 10000,
    ) -> None:
        normalized = str(mode).strip().lower()
        if normalized not in _VALID_MODES:
            raise ValueError("refine_governor.mode must be one of: off, shadow, on")
        depth = int(max_refine_depth)
        partitions = int(max_partitions_per_refine)
        budget = int(max_search_tasks_per_run)
        if depth <= 0 or partitions <= 0 or budget < 0:
            raise ValueError(
                "refine_governor depth/partitions must be positive and budget non-negative"
            )

        self.mode = normalized
        self.max_refine_depth = depth
        self.max_partitions_per_refine = partitions
        self.max_search_tasks_per_run = budget

        self._lock = threading.Lock()
        self._children_generated = 0
        self._children_admitted = 0
        self._refused_depth = 0
        self._refused_budget = 0
        self._truncated_to_cap = 0
        self._parents_refined = 0
        self._parents_at_depth_cap_paginated = 0
        self._budget_used = 0
        self._coverage: List[float] = []

    # ------------------------------------------------------------------
    # Depth gate
    # ------------------------------------------------------------------
    def may_refine(self, depth: int) -> bool:
        """Whether a task at ``depth`` may refine (``off`` inert, ``shadow`` measures)."""
        depth = int(depth)
        if self.mode == "off":
            return True

        if depth < self.max_refine_depth:
            return True

        with self._lock:
            self._refused_depth += 1
        if self.mode == "shadow":
            logger.warning(
                f"[refine_governor] would-be depth refusal: depth {depth} >= "
                f"max_refine_depth {self.max_refine_depth} (shadow)"
            )
            return True

        logger.warning(
            f"[refine_governor] depth refusal: depth {depth} >= "
            f"max_refine_depth {self.max_refine_depth}; paginating instead"
        )
        return False

    def clamp_partitions(self, raw: int) -> int:
        """Partition count handed to the generator (identity in ``off``/``shadow``)."""
        raw = int(raw)
        if self.mode == "on":
            return min(raw, self.max_partitions_per_refine)
        # off: pre-change behavior.  shadow: measure the full unclamped output.
        return raw

    def record_depth_cap_pagination(self) -> None:
        """Count a task that paginated because it was at the refinement depth cap."""
        with self._lock:
            self._parents_at_depth_cap_paginated += 1

    # ------------------------------------------------------------------
    # Admission
    # ------------------------------------------------------------------
    def select_children(
        self,
        provider: str,
        parent_query: str,
        queries: List[str],
        transport_limit: int,
        total: int,
        use_api: bool = True,
    ) -> List[str]:
        """Filter, (clamp + sort + truncate), and admit refined children.

        Returns the children to enqueue for this parent.  ``off`` and ``shadow``
        withhold nothing; ``on`` returns the admitted subset.

        ``off`` deliberately returns the generator's list verbatim -- empty and
        self-referential entries included -- so the stage's own pre-existing
        filters (and their warnings) fire exactly as they did before this change:
        ``off`` stays byte-equivalent in tasks *and* in log output.  ``shadow``
        and ``on`` apply the empty/self filter here so counted candidates match
        what would really be enqueued; the stage-level filter is then a no-op.

        ``use_api`` supplies the transport half of the admission fingerprint
        (design D5).  It is a trailing keyword with a default so the pinned
        positional signature is unchanged.
        """
        if self.mode == "off":
            return list(queries)

        candidates = [q for q in queries if q and q != parent_query]
        generated = len(candidates)

        cap = self.max_partitions_per_refine
        # Deterministic admission order: ascending wire fingerprint (design D5).
        ordered = sorted(candidates, key=lambda q: fingerprint(q, use_api))
        capped = ordered[:cap]
        truncated = generated - len(capped)

        if self.mode == "shadow":
            with self._lock:
                self._children_generated += generated
                self._truncated_to_cap += truncated
                remaining = max(0, self.max_search_tasks_per_run - self._budget_used)
                would_admit = capped[:remaining]
                would_refuse = capped[len(would_admit):]
                self._refused_budget += len(would_refuse)
                # Track the would-be "on" trajectory so subsequent batches
                # measure the same budget drain enforcement would cause.
                self._budget_used += len(would_admit)
                if generated > 0:
                    self._parents_refined += 1
            self._log_truncation(provider, parent_query, truncated)
            self._log_budget_refusals(provider, parent_query, would_refuse)
            coverage = self._record_coverage(would_admit, transport_limit, total)
            logger.info(
                f"[refine_governor] shadow parent: provider={provider} query={parent_query} "
                f"generated={generated} would_admit={len(would_admit)} "
                f"would_truncate={truncated} would_refuse_budget={len(would_refuse)} "
                f"coverage={coverage:.6f}"
            )
            return list(candidates)

        with self._lock:
            self._children_generated += generated
            self._truncated_to_cap += truncated
            remaining = max(0, self.max_search_tasks_per_run - self._budget_used)
            admitted = capped[:remaining]
            refused = capped[len(admitted):]
            self._budget_used += len(admitted)
            self._children_admitted += len(admitted)
            self._refused_budget += len(refused)
            if generated > 0:
                self._parents_refined += 1

        self._log_truncation(provider, parent_query, truncated)
        self._log_budget_refusals(provider, parent_query, refused)
        coverage = self._record_coverage(admitted, transport_limit, total)
        logger.info(
            f"[refine_governor] parent: provider={provider} query={parent_query} "
            f"generated={generated} admitted={len(admitted)} truncated={truncated} "
            f"refused_budget={len(refused)} coverage={coverage:.6f}"
        )
        return list(admitted)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _record_coverage(self, admitted: List[str], transport_limit: int, total: int) -> float:
        limit = int(transport_limit) if transport_limit else 0
        estimate = min(1.0, len(admitted) * limit / max(int(total), 1))
        with self._lock:
            self._coverage.append(estimate)
        return estimate

    def _log_truncation(self, provider: str, parent_query: str, truncated: int) -> None:
        if truncated <= 0:
            return
        logger.warning(
            f"[refine_governor] cap truncation: provider={provider} query={parent_query} "
            f"refused {truncated} candidate(s) beyond max_partitions_per_refine "
            f"{self.max_partitions_per_refine} (reason=cap)"
        )

    def _log_budget_refusals(self, provider: str, parent_query: str, refused: List[str]) -> None:
        for query in refused:
            logger.warning(
                f"[refine_governor] budget refusal: provider={provider} "
                f"query={parent_query} child={query} reason=budget "
                f"(max_search_tasks_per_run {self.max_search_tasks_per_run})"
            )

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------
    @property
    def metrics(self) -> Dict[str, Any]:
        """Thread-safe snapshot for ``PipelineStatus.refine_metrics``."""
        with self._lock:
            coverage = list(self._coverage)
            return {
                "mode": self.mode,
                "children_generated": self._children_generated,
                "children_admitted": self._children_admitted,
                "refused_depth": self._refused_depth,
                "refused_budget": self._refused_budget,
                "truncated_to_cap": self._truncated_to_cap,
                "parents_refined": self._parents_refined,
                "parents_at_depth_cap_paginated": self._parents_at_depth_cap_paginated,
                "budget_remaining": max(0, self.max_search_tasks_per_run - self._budget_used),
                "coverage_estimate_min": min(coverage) if coverage else 1.0,
                "coverage_estimate_avg": (sum(coverage) / len(coverage)) if coverage else 1.0,
            }
