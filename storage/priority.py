#!/usr/bin/env python3

"""
Repository priority scoring for deep-scan target ordering.

A repository's priority is a deterministic, explainable function of evidence
already accumulated in the registry (key statuses, push freshness, size):

    score = W1*valid_present + W2*soft_present
            + W4*exp(-ln2 * age_days / half_life_days)
            - W5*clamp((size_kb - threshold_kb) / (ramp_kb - threshold_kb), 0, 1)

``valid_present``  : a key with status ``valid`` is attributed to the repo
``soft_present``   : a key with status ``wait_check``/``no_quota`` is attributed
``age_days``       : (now - max(repo_pushed_at)) in days; NULL contributes 0
``size_kb``        : max(repo_size_kb); NULL contributes no penalty

Key -> repo attribution is ``keys.source_url_hash -> links.url_hash ->
(owner, repo)``.  Keys without an attributable source link (legacy imports)
contribute only to global statistics, never to a repository score.

Missing inputs degrade neutrally: unknown dates/sizes and absent keys simply
contribute zero; scoring never raises on sparse rows.

The score is denormalized into every ``links.priority`` row of the repository.
This module is deliberately free of logging/network imports so the read-only
export path stays silent and testable in isolation.
"""

import math
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

SECONDS_PER_DAY = 86400.0
LN2 = math.log(2.0)

VALID_STATUSES = ("valid",)
SOFT_STATUSES = ("wait_check", "no_quota")

# Ranking used by consumers to pick the "best" observed status.
STATUS_RANK: Dict[str, int] = {"valid": 4, "wait_check": 3, "no_quota": 2, "invalid": 1}
NO_STATUS = "none"


@dataclass
class PriorityWeights:
    """Canonical scoring parameters (design D2 defaults)."""

    w1: float = 100.0
    w2: float = 40.0
    w4: float = 30.0
    half_life_days: float = 30.0
    w5: float = 20.0
    threshold_kb: float = 50_000.0
    ramp_kb: float = 500_000.0


DEFAULT_WEIGHTS = PriorityWeights()


def weights_from_config(config: Any) -> PriorityWeights:
    """Build :class:`PriorityWeights` from a config-like object.

    Accepts the ``prioritization`` config section (attributes ``w1``, ``w2``,
    ``w4``, ``half_life_days``, ``w5``, ``threshold_kb``, ``ramp_kb``) or an
    already-built :class:`PriorityWeights`.  ``None`` yields the defaults.
    """
    if config is None:
        return PriorityWeights()
    if isinstance(config, PriorityWeights):
        return config
    return PriorityWeights(
        w1=float(getattr(config, "w1", DEFAULT_WEIGHTS.w1)),
        w2=float(getattr(config, "w2", DEFAULT_WEIGHTS.w2)),
        w4=float(getattr(config, "w4", DEFAULT_WEIGHTS.w4)),
        half_life_days=float(getattr(config, "half_life_days", DEFAULT_WEIGHTS.half_life_days)),
        w5=float(getattr(config, "w5", DEFAULT_WEIGHTS.w5)),
        threshold_kb=float(getattr(config, "threshold_kb", DEFAULT_WEIGHTS.threshold_kb)),
        ramp_kb=float(getattr(config, "ramp_kb", DEFAULT_WEIGHTS.ramp_kb)),
    )


def freshness_component(pushed_at: Optional[float], weights: PriorityWeights, now: float) -> float:
    """Exponential freshness decay; ``None`` contributes zero."""
    if pushed_at is None:
        return 0.0
    age_days = max(0.0, (now - float(pushed_at)) / SECONDS_PER_DAY)
    half_life = weights.half_life_days if weights.half_life_days > 0 else DEFAULT_WEIGHTS.half_life_days
    return weights.w4 * math.exp(-LN2 * age_days / half_life)


def size_penalty(size_kb: Optional[float], weights: PriorityWeights) -> float:
    """Clamped size ramp above ``threshold_kb``; ``None``/small sizes -> 0."""
    if size_kb is None:
        return 0.0
    threshold = weights.threshold_kb
    ramp = weights.ramp_kb
    if ramp <= threshold:
        return 0.0
    ratio = (float(size_kb) - threshold) / (ramp - threshold)
    return weights.w5 * min(1.0, max(0.0, ratio))


def compute_score(
    valid_present: bool,
    soft_present: bool,
    pushed_at: Optional[float],
    size_kb: Optional[float],
    weights: PriorityWeights = DEFAULT_WEIGHTS,
    now: Optional[float] = None,
) -> float:
    """Pure scoring formula (design D2)."""
    if now is None:
        now = time.time()
    score = 0.0
    if valid_present:
        score += weights.w1
    if soft_present:
        score += weights.w2
    score += freshness_component(pushed_at, weights, now)
    score -= size_penalty(size_kb, weights)
    return score


def open_readonly(registry_path: str) -> sqlite3.Connection:
    """Open the registry in strict read-only mode (URI ``mode=ro``)."""
    uri = Path(os.path.abspath(str(registry_path))).as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=5.0)


def repos_for_url_hashes(conn: sqlite3.Connection, hashes: Iterable[str]) -> Set[Tuple[str, str]]:
    """Resolve source URL hashes to the ``(owner, repo)`` rows that carry them."""
    unique = sorted({hashed for hashed in hashes if hashed})
    if not unique:
        return set()
    repos: Set[Tuple[str, str]] = set()
    # Chunk to stay below SQLite's bound-parameter limit on large batches.
    for start in range(0, len(unique), 900):
        chunk = unique[start : start + 900]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT DISTINCT owner, repo FROM links WHERE url_hash IN ({placeholders})",
            tuple(chunk),
        ).fetchall()
        repos.update((owner, repo) for owner, repo in rows if owner and repo)
    return repos


def score_repo(
    conn: sqlite3.Connection,
    owner: str,
    repo: str,
    weights: PriorityWeights = DEFAULT_WEIGHTS,
    now: Optional[float] = None,
) -> float:
    """Compute the score for one repository from current registry state."""
    if now is None:
        now = time.time()

    meta = conn.execute(
        "SELECT MAX(repo_pushed_at), MAX(repo_size_kb) FROM links WHERE owner = ? AND repo = ?",
        (owner, repo),
    ).fetchone()
    pushed_at = meta[0] if meta else None
    size_kb = meta[1] if meta else None

    # Attribution: keys join links through their source URL hash.
    valid_placeholder = ",".join("?" for _ in VALID_STATUSES)
    soft_placeholder = ",".join("?" for _ in SOFT_STATUSES)
    evidence = conn.execute(
        "SELECT "
        f"  MAX(CASE WHEN k.status IN ({valid_placeholder}) THEN 1 ELSE 0 END), "
        f"  MAX(CASE WHEN k.status IN ({soft_placeholder}) THEN 1 ELSE 0 END) "
        "FROM keys k JOIN links l ON k.source_url_hash = l.url_hash "
        "WHERE l.owner = ? AND l.repo = ?",
        (*VALID_STATUSES, *SOFT_STATUSES, owner, repo),
    ).fetchone()
    valid_present = bool(evidence[0]) if evidence else False
    soft_present = bool(evidence[1]) if evidence else False

    return compute_score(valid_present, soft_present, pushed_at, size_kb, weights, now)


def apply_scores(conn: sqlite3.Connection, scores: Dict[Tuple[str, str], float]) -> int:
    """Denormalize repo scores into every ``links.priority`` row of the repo."""
    if not scores:
        return 0
    conn.executemany(
        "UPDATE links SET priority = ? WHERE owner = ? AND repo = ?",
        [(float(score), owner, repo) for (owner, repo), score in scores.items()],
    )
    return len(scores)


def score_many(
    conn: sqlite3.Connection,
    repos: Iterable[Tuple[str, str]],
    weights: PriorityWeights = DEFAULT_WEIGHTS,
    now: Optional[float] = None,
) -> Dict[Tuple[str, str], float]:
    """Score a bounded set of repositories (dirty-set path)."""
    if now is None:
        now = time.time()
    return {repo: score_repo(conn, repo[0], repo[1], weights, now) for repo in repos if repo[0] and repo[1]}


def sweep(
    conn: sqlite3.Connection,
    weights: PriorityWeights = DEFAULT_WEIGHTS,
    now: Optional[float] = None,
) -> int:
    """Recompute and persist priorities for every repository (run-finish sweep)."""
    if now is None:
        now = time.time()
    rows = conn.execute(
        "SELECT DISTINCT owner, repo FROM links "
        "WHERE owner IS NOT NULL AND owner != '' AND repo IS NOT NULL AND repo != ''"
    ).fetchall()
    scores = score_many(conn, [(owner, repo) for owner, repo in rows], weights, now)
    return apply_scores(conn, scores)


def status_counts_for_repos(conn: sqlite3.Connection) -> Dict[Tuple[str, str], Dict[str, int]]:
    """Attribute ledger statuses to repositories for reporting surfaces."""
    counts: Dict[Tuple[str, str], Dict[str, int]] = {}
    rows = conn.execute(
        "SELECT l.owner, l.repo, k.status, COUNT(*) FROM keys k "
        "JOIN links l ON k.source_url_hash = l.url_hash "
        "WHERE k.status IS NOT NULL AND k.status != '' "
        "GROUP BY l.owner, l.repo, k.status"
    ).fetchall()
    for owner, repo, status, count in rows:
        if not owner or not repo:
            continue
        counts.setdefault((owner, repo), {})[status] = count
    return counts


def best_status(counts: Dict[str, int]) -> str:
    """Pick the highest-ranked observed status, or ``none``."""
    ranked = [status for status in (counts or {}) if status in STATUS_RANK]
    if not ranked:
        return NO_STATUS
    return max(ranked, key=lambda status: STATUS_RANK[status])


def top_candidates(registry_path: str, limit: int) -> List[List[Any]]:
    """Return ``[[owner, repo, priority], ...]`` highest-priority first."""
    if limit <= 0 or not os.path.exists(str(registry_path)):
        return []
    conn = open_readonly(registry_path)
    try:
        rows = conn.execute(
            "SELECT owner, repo, MAX(priority) FROM links "
            "WHERE owner IS NOT NULL AND owner != '' AND repo IS NOT NULL AND repo != '' "
            "GROUP BY owner, repo "
            "ORDER BY MAX(priority) DESC, owner ASC, repo ASC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [[owner, repo, float(priority or 0.0)] for owner, repo, priority in rows]
    finally:
        conn.close()
