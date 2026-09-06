"""
lineup.py - Greedy weekly optimal-lineup solver.

For a single team's roster and a single week, figure out which players
should start and which should sit, to maximize that week's total
projected points.

This is intentionally a greedy assignment, not a real optimizer (LP/ILP):
  1. Fill each dedicated position slot (QB, RB, WR, TE, DST, K) with the
     best remaining player(s) at that position.
  2. Fill FLEX with the single best remaining RB/WR/TE.
  3. Everyone else left on the roster is bench.

This greedy approach is optimal for this problem shape (independent
per-slot point maximization with one flex pool) as long as flex is
filled *after* dedicated slots, since a flex-eligible player who was the
best choice for their own dedicated slot is already locked in before
flex looks at leftovers.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .config import RosterConfig, FLEX_ELIGIBLE
from .projections import Player


def optimal_lineup(
    roster: List[Player],
    week: int,
    roster_config: RosterConfig,
) -> Tuple[Dict[str, List[Player]], List[Player], float]:
    """
    Returns (starters, bench, total_points).

    `starters` maps position key -> list of starting players at that slot
    (e.g. {"QB": [p], "RB": [p1, p2], "FLEX": [p3]}). Bye-week players
    contribute 0 points that week and are never worth starting over a
    healthy alternative, so they naturally fall to bench unless the
    roster is too thin to avoid it.
    """
    remaining = list(roster)
    starters: Dict[str, List[Player]] = {}

    def take_best(pool: List[Player], n: int) -> List[Player]:
        pool_sorted = sorted(pool, key=lambda p: p.points_for_week(week), reverse=True)
        chosen = pool_sorted[:n]
        for p in chosen:
            remaining.remove(p)
        return chosen

    # 1. Dedicated position slots.
    for pos, count in roster_config.starters.items():
        if count <= 0:
            continue
        eligible = [p for p in remaining if p.position == pos]
        starters[pos] = take_best(eligible, count)

    # 2. FLEX -- best remaining RB/WR/TE not already used above.
    if roster_config.flex > 0:
        flex_eligible = [p for p in remaining if p.position in FLEX_ELIGIBLE]
        starters["FLEX"] = take_best(flex_eligible, roster_config.flex)

    bench = remaining
    total_points = sum(
        p.points_for_week(week) for players in starters.values() for p in players
    )
    return starters, bench, total_points
