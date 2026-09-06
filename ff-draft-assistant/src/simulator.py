"""
simulator.py - Simulates a full season of matchups given full rosters.

For every week in the schedule, each team's optimal lineup (see lineup.py)
is computed, and head-to-head matchups are decided by who scored more
that week. Wins/losses/ties and points-for/points-against are tallied
across the whole season to produce a standings table.

Note on playoff_weeks: the config allows tagging certain weeks as
playoff weeks for future weighting (e.g. valuing playoff-week upside
more heavily in the draft recommendation). This simulator does not
currently apply any such weighting -- wins are wins, every week counts
the same -- to avoid over-building a feature the prompt only flagged as
optional. `playoff_points_for` is still broken out per team in case you
want to use it as a tiebreaker or a UI callout later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from .config import RosterConfig
from .lineup import optimal_lineup
from .projections import Player


@dataclass
class TeamRecord:
    team: str
    wins: float = 0.0
    losses: float = 0.0
    ties: float = 0.0
    points_for: float = 0.0
    points_against: float = 0.0
    playoff_points_for: float = 0.0

    def to_dict(self) -> dict:
        return {
            "team": self.team,
            "wins": self.wins,
            "losses": self.losses,
            "ties": self.ties,
            "points_for": round(self.points_for, 2),
            "points_against": round(self.points_against, 2),
            "playoff_points_for": round(self.playoff_points_for, 2),
        }


def simulate_season(
    all_team_rosters: Dict[str, List[Player]],
    schedule: Dict[int, List[Tuple[str, str]]],
    roster_config: RosterConfig,
    playoff_weeks: List[int] = (),
) -> List[dict]:
    """
    Returns a standings table: a list of dicts sorted by (wins desc,
    points_for desc), one per team.
    """
    records: Dict[str, TeamRecord] = {team: TeamRecord(team=team) for team in all_team_rosters}
    playoff_weeks_set = set(playoff_weeks or [])

    for week, matchups in schedule.items():
        # Compute each team's optimal-lineup score for this week once,
        # rather than recomputing it per matchup.
        weekly_scores: Dict[str, float] = {}
        for team, roster in all_team_rosters.items():
            _, _, total = optimal_lineup(roster, week, roster_config)
            weekly_scores[team] = total

        for team_a, team_b in matchups:
            if team_a not in weekly_scores or team_b not in weekly_scores:
                # Schedule referenced a team we don't have a roster for --
                # config validation should prevent this, but skip
                # defensively rather than crashing a live draft session.
                continue
            score_a = weekly_scores[team_a]
            score_b = weekly_scores[team_b]

            records[team_a].points_for += score_a
            records[team_a].points_against += score_b
            records[team_b].points_for += score_b
            records[team_b].points_against += score_a

            if week in playoff_weeks_set:
                records[team_a].playoff_points_for += score_a
                records[team_b].playoff_points_for += score_b

            if score_a > score_b:
                records[team_a].wins += 1
                records[team_b].losses += 1
            elif score_b > score_a:
                records[team_b].wins += 1
                records[team_a].losses += 1
            else:
                records[team_a].ties += 1
                records[team_b].ties += 1

    standings = sorted(
        records.values(), key=lambda r: (-r.wins, -r.points_for)
    )
    return [r.to_dict() for r in standings]
