"""
draft_engine.py - The draft recommendation "magic".

Three layered steps, per pick:

  Step A - Value over Replacement (VOR):
      For each available player, VOR = player's projected season total
      minus the "replacement level" at their position -- the projected
      season total of the player who would be the last one drafted into
      a starting lineup at that position, league-wide, if the draft
      stopped there. This normalizes across positions (e.g. a WR13 and
      an RB25 might both be "replacement level" for their position, even
      though their raw point totals differ a lot).

  Step B - Positional need weighting:
      Multiplies VOR by how much *I* actually need this position right
      now, given my roster so far. An empty starter slot is worth more
      than topping up a position I've already started and benched.

  Step C - Simulated win impact ("the real magic"):
      For the handful of top candidates from Step B, actually fast-
      forward a full mock draft (my remaining picks via best-VOR-
      available, everyone else via best-ADP-available), run a full
      season simulation on the resulting complete league, and see how
      many games *I* win. This is the most expensive step, so it's only
      run on a short list of realistic candidates, not every available
      player.

IMPORTANT CAVEAT (surfaced in the UI, not just here): Step C's opponent
model is a simplifying assumption. Real opponents don't draft by ADP
alone -- they have their own needs, sleepers, and biases. Treat the
resulting win totals as *directional* signal for comparing the top
candidates against each other, not as a gospel prediction of the season.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .config import RosterConfig, FLEX_ELIGIBLE
from .draft_state import DraftState
from .projections import Player
from .simulator import simulate_season

# Step B tuning constants -- named so the "magic numbers" have obvious meaning.
NEED_BOOST_OPEN_STARTER = 1.3
NEED_PENALTY_FILLED_WITH_BENCH_ROOM = 0.5
NEED_ZERO_FILLED_AND_BENCH_FULL = 0.0
NEED_ELITE_OVERRIDE = 1.0  # applied instead of 0.0 for a top-3-overall value even when full
ELITE_OVERALL_RANK_CUTOFF = 3


# ---------------------------------------------------------------------------
# Step A: Value over Replacement
# ---------------------------------------------------------------------------

def compute_replacement_levels(
    players: List[Player], roster_config: RosterConfig, num_teams: int
) -> Dict[str, float]:
    """
    Replacement level per position = the projected season total of the
    player ranked just outside league-wide starting lineups at that
    position, given the CURRENT undrafted pool.

    Demand for any configured dedicated slot (including IDP and punter slots)
    = num_teams * starters[pos].
    FLEX demand is split evenly across the three flex-eligible positions
    (RB/WR/TE) -- a simplifying assumption (real flex usage skews toward
    whichever position is deepest at the margin), but it's a reasonable,
    easy-to-explain default and avoids over-engineering a position-usage
    model for what is ultimately a soft heuristic.
    """
    by_position: Dict[str, List[Player]] = {}
    for p in players:
        by_position.setdefault(p.position, []).append(p)
    for pos in by_position:
        by_position[pos].sort(key=lambda p: p.season_total(), reverse=True)

    flex_share = (roster_config.flex * num_teams) / len(FLEX_ELIGIBLE) if roster_config.flex else 0.0

    replacement_levels: Dict[str, float] = {}
    for pos, dedicated_count in roster_config.starters.items():
        demand = num_teams * dedicated_count
        if pos in FLEX_ELIGIBLE:
            demand += flex_share
        rank_index = int(round(demand))  # 0-indexed: this is the first player OUTSIDE starters
        pool = by_position.get(pos, [])
        if not pool:
            replacement_levels[pos] = 0.0
        elif rank_index < len(pool):
            replacement_levels[pos] = pool[rank_index].season_total()
        else:
            # Thinner pool than demand (e.g. deep into a draft) -- floor at
            # the worst remaining player rather than extrapolating.
            replacement_levels[pos] = pool[-1].season_total()
    return replacement_levels


def compute_vor(players: List[Player], replacement_levels: Dict[str, float]) -> Dict[str, float]:
    return {
        p.name: p.season_total() - replacement_levels.get(p.position, 0.0)
        for p in players
    }


# ---------------------------------------------------------------------------
# Step B: Positional need weighting
# ---------------------------------------------------------------------------

def _flex_slots_open(my_roster: List[Player], roster_config: RosterConfig) -> int:
    """How many FLEX slots aren't already spoken for by surplus RB/WR/TE."""
    flex_used = sum(
        max(0, sum(1 for p in my_roster if p.position == fp) - roster_config.starters.get(fp, 0))
        for fp in FLEX_ELIGIBLE
    )
    return max(0, roster_config.flex - flex_used)


def _bench_is_full(my_roster: List[Player], roster_config: RosterConfig) -> bool:
    bench_used = max(0, len(my_roster) - roster_config.starter_slot_count())
    return bench_used >= roster_config.bench


def need_multiplier(
    candidate: Player,
    my_roster: List[Player],
    roster_config: RosterConfig,
    is_elite_overall: bool,
) -> float:
    """See module docstring, Step B. Returns the multiplier to apply to VOR."""
    pos = candidate.position
    dedicated_slots = roster_config.starters.get(pos, 0)
    have_at_pos = sum(1 for p in my_roster if p.position == pos)
    dedicated_open = have_at_pos < dedicated_slots
    flex_open = pos in FLEX_ELIGIBLE and _flex_slots_open(my_roster, roster_config) > 0

    if dedicated_open or flex_open:
        return NEED_BOOST_OPEN_STARTER

    # Starters at this position are fully spoken for.
    if _bench_is_full(my_roster, roster_config):
        return NEED_ELITE_OVERRIDE if is_elite_overall else NEED_ZERO_FILLED_AND_BENCH_FULL
    return NEED_PENALTY_FILLED_WITH_BENCH_ROOM


@dataclass
class RankedCandidate:
    player: Player
    vor: float
    need_weight: float
    weighted_vor: float


def weighted_vor_rankings(
    available: List[Player],
    my_roster: List[Player],
    roster_config: RosterConfig,
    num_teams: int,
) -> List[RankedCandidate]:
    """Step A + Step B combined, sorted best-first by weighted VOR."""
    replacement_levels = compute_replacement_levels(available, roster_config, num_teams)
    vor_by_name = compute_vor(available, replacement_levels)

    # "Elite" = top-3 remaining by raw (unweighted) VOR -- computed once,
    # up front, so every candidate's elite-override check is consistent.
    elite_names = {
        name
        for name, _ in sorted(vor_by_name.items(), key=lambda kv: kv[1], reverse=True)[
            :ELITE_OVERALL_RANK_CUTOFF
        ]
    }

    ranked = []
    for p in available:
        vor = vor_by_name[p.name]
        weight = need_multiplier(p, my_roster, roster_config, p.name in elite_names)
        ranked.append(RankedCandidate(player=p, vor=vor, need_weight=weight, weighted_vor=vor * weight))
    ranked.sort(key=lambda rc: rc.weighted_vor, reverse=True)
    return ranked


# ---------------------------------------------------------------------------
# Step C: Simulated win impact
# ---------------------------------------------------------------------------

def _best_adp_pick(available: Dict[str, Player]) -> Player:
    """Simplest reasonable opponent model: everyone else drafts best-ADP-
    available. Missing ADP sorts last (never reached ahead of ranked players).
    """
    return min(available.values(), key=lambda p: p.adp if p.adp is not None else float("inf"))


def autocomplete_draft(
    state: DraftState, my_team: str, roster_config: RosterConfig, num_teams: int
) -> None:
    """
    Mutates `state` in place, playing out every remaining pick:
      - my_team picks best-weighted-VOR-available given its roster so far.
      - every other team picks best-ADP-available (a simplifying stand-in
        for "reasonable but not omniscient" opponents -- see module docstring).

    Public (not prefixed with _) because the Streamlit app also uses this
    directly to preview projected final standings from the current draft
    state, not just internally during Step C simulation.
    """
    while not state.is_complete():
        team = state.current_team()
        if team == my_team:
            ranked = weighted_vor_rankings(
                list(state.available.values()), state.rosters[my_team], roster_config, num_teams
            )
            pick = ranked[0].player
        else:
            pick = _best_adp_pick(state.available)
        state.make_pick(pick.name, team=team)


def simulate_candidate_win_impact(
    state: DraftState,
    candidate: Player,
    my_team: str,
    roster_config: RosterConfig,
    schedule: dict,
    num_teams: int,
    playoff_weeks: List[int],
) -> Tuple[float, float]:
    """
    Clones the draft state, drafts `candidate` to my_team right now,
    auto-completes the rest of the draft, simulates the season, and
    returns (my_wins, my_points_for) for that hypothetical outcome.
    """
    sim_state = state.clone()
    sim_state.make_pick(candidate.name, team=my_team)
    autocomplete_draft(sim_state, my_team, roster_config, num_teams)

    standings = simulate_season(sim_state.rosters, schedule, roster_config, playoff_weeks)
    my_row = next(row for row in standings if row["team"] == my_team)
    return my_row["wins"], my_row["points_for"]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def recommend_picks(
    state: DraftState,
    roster_config: RosterConfig,
    schedule: dict,
    num_teams: int,
    my_team: str,
    playoff_weeks: List[int] = (),
    deep_sim_count: int = 6,
) -> List[dict]:
    """
    Full recommendation pipeline for whoever is on the clock (intended
    for use on my_team's turn). Returns a list of dicts, best pick first,
    each with:
        name, position, season_points, vor, need_weight, weighted_vor,
        simulated_wins (None if this candidate wasn't in the deep-sim
        shortlist), simulated_points_for.

    Only the top `deep_sim_count` candidates by weighted VOR get the
    expensive Step C simulation (clamped to 5-8 per the design spec) --
    running it on the full available pool would be far too slow to stay
    "usable live, during a draft".
    """
    deep_sim_count = max(5, min(8, deep_sim_count))
    available = list(state.available.values())
    my_roster = state.rosters[my_team]

    ranked = weighted_vor_rankings(available, my_roster, roster_config, num_teams)
    shortlist = ranked[:deep_sim_count]

    results = []
    for rc in shortlist:
        wins, points_for = simulate_candidate_win_impact(
            state, rc.player, my_team, roster_config, schedule, num_teams, playoff_weeks
        )
        results.append(
            {
                "name": rc.player.name,
                "position": rc.player.position,
                "season_points": round(rc.player.season_total(), 1),
                "vor": round(rc.vor, 1),
                "need_weight": rc.need_weight,
                "weighted_vor": round(rc.weighted_vor, 1),
                "simulated_wins": wins,
                "simulated_points_for": round(points_for, 1),
            }
        )

    # Recommend by simulated win total; break ties with raw projected points,
    # per the spec ("Break ties with total projected points").
    results.sort(key=lambda r: (-r["simulated_wins"], -r["season_points"]))
    return results
