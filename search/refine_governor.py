#!/usr/bin/env python3

"""Search-work fan-out governor (add-refine-fanout-governor, design D1-D9).

``RefineGovernor`` sits strictly between "the refine engine produced candidate
children" and "children enter the search queue".  It never rewrites wire
queries: it clamps the partition count handed to the engine, sorts/truncates/
filters the engine's output, and admits children within a per-run budget.

Modes (tri-mode rollout flag):

- ``off``    -- passthrough: input order preserved, all counters inert.
- ``shadow`` -- everything is enqueued exactly as in ``off`` while every
                would-be clamp/truncation/budget/wire-collapse decision is
                computed, counted and logged (measures the coverage price
                before enforcement).
- ``on``     -- decisions enforced.

Wire-collapse guard (fix-refine-fanout-wire-collapse, design D2-D9): when
``drop_wire_indistinguishable`` is true (default), admission withholds any
candidate the transport cannot distinguish from work already selected -- a child
whose wire query equals the parent's, and all but one child of each group of
children sharing a wire query.  The comparison uses ``search.querykey.wire_query``
(the single source of truth that both the actual request and the shared-response
cache key are built from), never ``RefineEngine.clean_regex`` directly, so the
guard is provably inert on the web transport (where ``wire_query`` is the raw
query).  The guard is *selection*, not rewriting: the survivor is one of the
generator's own outputs.

Admission order is ascending ``search/querykey.fingerprint`` with the raw query
as tiebreaker (design D4/D5): the sha256 of ``"<api|web>|<wire_query>"`` is
stable across processes and provider-independent, so identical inputs admit
identical children regardless of the engine's internal (set-based, cross-process
salted) ordering, and twins of the same query from different providers converge
on the same survivor set.  The raw-query tiebreaker additionally makes *which*
candidate survives a same-wire group deterministic, not just the survivor set.
Fingerprint order also spreads truncation gaps pseudo-randomly over the query
space instead of permanently sacrificing one fixed region (lexicographic order
always drops the alphabet tail -- measured on the 2026-09-21 incident seed at
cap 128: ``w x y z`` excluded on every run).

Memory: ``distinct_wire_admitted`` is a cumulative counter -- each parent
contributes the number of distinct wire queries among the children it admitted.
Under enforcement it therefore equals ``children_admitted`` (the spec's
post-condition); with the guard off it exposes the collapse ratio.  It
deliberately does not retain a global set of fingerprints: the same wire can be
admitted under two different parents, so a global set would under-count the
distinguishable work admitted under separate parents (design D6 amended; see the
change's ``design.md``).  It resets with the process like every other governor
counter.

All mutable state lives behind a single ``threading.Lock`` because
``WorkerManager`` may scale the search stage at runtime.
"""

import threading
from typing import Any, Dict, List

from search.querykey import fingerprint, wire_query
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
        drop_wire_indistinguishable: bool = True,
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
        # Boolean, never coerced: ``bool("false")`` is ``True``, so coercion would
        # silently invert an operator's rollback (design D8).  Trailing keyword
        # with a default keeps the pinned positional signature unchanged.
        if not isinstance(drop_wire_indistinguishable, bool):
            raise ValueError(
                "refine_governor.drop_wire_indistinguishable must be a boolean"
            )

        self.mode = normalized
        self.max_refine_depth = depth
        self.max_partitions_per_refine = partitions
        self.max_search_tasks_per_run = budget
        self.drop_wire_indistinguishable = drop_wire_indistinguishable

        self._lock = threading.Lock()
        self._children_generated = 0
        self._children_admitted = 0
        self._refused_depth = 0
        self._refused_budget = 0
        self._refused_wire_collapse = 0
        self._truncated_to_cap = 0
        self._parents_refined = 0
        self._parents_at_depth_cap_paginated = 0
        self._budget_used = 0
        self._distinct_wire_admitted = 0
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

        With ``drop_wire_indistinguishable`` (default true) and ``mode`` in
        ``shadow``/``on``, the wire-collapse guard additionally withholds every
        candidate whose ``search.querykey.wire_query`` equals its parent's and all
        but one candidate of each group sharing a wire query (design D2/D3/D5).
        Withheld children consume neither the partition cap nor the run budget.

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

        # Deterministic admission order: ascending wire fingerprint, raw query as
        # tiebreaker so same-wire siblings also have a deterministic relative
        # order and the lexicographically smallest raw survives its wire group
        # (design D4/D5).
        ordered = sorted(candidates, key=lambda q: (fingerprint(q, use_api), q))

        # Wire-collapse guard (design D2/D3/D5): selection among the generator's
        # own outputs, applied before the cap and the budget so withheld children
        # consume neither.  Computed identically in shadow and on; inert when the
        # rollback flag is off.
        parent_equal = 0
        sibling_duplicate = 0
        if self.drop_wire_indistinguishable:
            parent_wire = wire_query(parent_query, use_api)
            survivors: List[str] = []
            seen_wires = set()
            for child in ordered:
                child_wire = wire_query(child, use_api)
                if child_wire == parent_wire:
                    parent_equal += 1
                elif child_wire in seen_wires:
                    sibling_duplicate += 1
                else:
                    seen_wires.add(child_wire)
                    survivors.append(child)
            collapsed = parent_equal + sibling_duplicate
            population = survivors
        else:
            collapsed = 0
            population = ordered

        cap = self.max_partitions_per_refine
        capped = population[:cap]
        # Truncation surplus is measured on the post-guard population so a
        # withheld child is never counted twice (design D5).
        truncated = len(population) - len(capped)

        if self.mode == "shadow":
            with self._lock:
                self._children_generated += generated
                self._refused_wire_collapse += collapsed
                self._truncated_to_cap += truncated
                remaining = max(0, self.max_search_tasks_per_run - self._budget_used)
                would_admit = capped[:remaining]
                would_refuse = capped[len(would_admit):]
                self._refused_budget += len(would_refuse)
                # Track the would-be "on" trajectory so subsequent batches
                # measure the same budget drain enforcement would cause.
                self._budget_used += len(would_admit)
                # ``distinct_wire_admitted`` is an observation, not a decision:
                # shadow records the wires enforcement would admit (design D8).
                # It is a per-parent distinct count summed over the run, so under
                # enforcement it equals ``children_admitted`` even though the same
                # wire may be admitted under two different parents (design D6).
                self._distinct_wire_admitted += len(
                    {fingerprint(q, use_api) for q in would_admit}
                )
                if generated > 0:
                    self._parents_refined += 1
            self._log_wire_collapse(
                provider, parent_query, generated, parent_equal,
                sibling_duplicate, len(would_admit), shadow=True,
            )
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
            self._refused_wire_collapse += collapsed
            self._truncated_to_cap += truncated
            remaining = max(0, self.max_search_tasks_per_run - self._budget_used)
            admitted = capped[:remaining]
            refused = capped[len(admitted):]
            self._budget_used += len(admitted)
            self._children_admitted += len(admitted)
            self._refused_budget += len(refused)
            self._distinct_wire_admitted += len(
                {fingerprint(q, use_api) for q in admitted}
            )
            if generated > 0:
                self._parents_refined += 1

        self._log_wire_collapse(
            provider, parent_query, generated, parent_equal,
            sibling_duplicate, len(admitted), shadow=False,
        )
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

    def _log_wire_collapse(
        self,
        provider: str,
        parent_query: str,
        generated: int,
        parent_equal: int,
        sibling_duplicate: int,
        admitted: int,
        shadow: bool,
    ) -> None:
        """One aggregated INFO line per parent that withheld anything (design D7).

        Never one line per withheld child: the per-child ``_log_budget_refusals``
        pattern produced 56 048 of 70 059 WARNING lines in the measured arm.  The
        stable token ``wire_collapse`` identifies the line.
        """
        collapsed = parent_equal + sibling_duplicate
        if collapsed <= 0:
            return
        marker = " (shadow)" if shadow else ""
        logger.info(
            f"[refine_governor] wire_collapse: provider={provider} query={parent_query} "
            f"generated={generated} withheld={collapsed} "
            f"parent_equal={parent_equal} sibling_duplicate={sibling_duplicate} "
            f"admitted={admitted}{marker}"
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
                "refused_wire_collapse": self._refused_wire_collapse,
                "truncated_to_cap": self._truncated_to_cap,
                "parents_refined": self._parents_refined,
                "parents_at_depth_cap_paginated": self._parents_at_depth_cap_paginated,
                "budget_remaining": max(0, self.max_search_tasks_per_run - self._budget_used),
                "distinct_wire_admitted": self._distinct_wire_admitted,
                "coverage_estimate_min": min(coverage) if coverage else 1.0,
                "coverage_estimate_avg": (sum(coverage) / len(coverage)) if coverage else 1.0,
            }
