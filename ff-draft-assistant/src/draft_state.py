"""
draft_state.py - Tracks an in-progress draft: whose turn it is, what's
been picked, and every team's roster so far.

This is the single source of truth the Streamlit app mutates as picks
happen live, and it's also what draft_engine.py clones and fast-forwards
through when simulating "what if I draft player X" scenarios.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .config import LeagueConfig
from .projections import Player


@dataclass
class DraftPick:
    round: int
    pick_in_round: int
    overall_pick: int
    team: str
    player_name: Optional[str] = None  # filled in once the pick is made


def build_snake_order(team_names: List[str], rounds: int, draft_type: str) -> List[List[str]]:
    """
    Returns a list (length = rounds) of per-round team orders.

    Snake: odd rounds go in draft-slot order, even rounds reverse.
    Linear: every round goes in the same draft-slot order.
    """
    order: List[List[str]] = []
    for r in range(1, rounds + 1):
        if draft_type == "snake" and r % 2 == 0:
            order.append(list(reversed(team_names)))
        else:
            order.append(list(team_names))
    return order


class DraftState:
    def __init__(self, config: LeagueConfig, all_players: List[Player]):
        self.config = config
        # Team names in draft-slot order (slot 1 first).
        self.team_names = [t.name for t in sorted(config.teams, key=lambda t: t.draft_slot)]
        self.snake_order = build_snake_order(self.team_names, config.draft_rounds, config.draft_type)

        self.available: Dict[str, Player] = {p.name: p for p in all_players}
        self.rosters: Dict[str, List[Player]] = {name: [] for name in self.team_names}
        self.pick_history: List[DraftPick] = []

        self._current_round = 1
        self._current_pick_in_round = 0  # 0-indexed into snake_order[round]

    # -- turn-order queries --------------------------------------------

    def is_complete(self) -> bool:
        return self._current_round > self.config.draft_rounds

    def current_round(self) -> Optional[int]:
        return None if self.is_complete() else self._current_round

    def current_team(self) -> Optional[str]:
        if self.is_complete():
            return None
        return self.snake_order[self._current_round - 1][self._current_pick_in_round]

    def overall_pick_number(self) -> Optional[int]:
        if self.is_complete():
            return None
        num_teams = len(self.team_names)
        return (self._current_round - 1) * num_teams + self._current_pick_in_round + 1

    # -- mutating the draft ----------------------------------------------

    def make_pick(self, player_name: str, team: Optional[str] = None) -> None:
        """
        Assigns `player_name` to `team` (defaults to whichever team is
        currently on the clock) and advances the snake order. Raises
        ValueError on an invalid pick (already drafted, wrong turn, etc)
        so the UI can show a clear error instead of corrupting state.
        """
        if self.is_complete():
            raise ValueError("Draft is already complete -- no more picks to make.")
        if player_name not in self.available:
            raise ValueError(f"{player_name!r} is not in the available player pool.")

        on_the_clock = self.current_team()
        team = team or on_the_clock
        if team != on_the_clock:
            raise ValueError(f"It's {on_the_clock}'s pick, not {team}'s.")

        player = self.available.pop(player_name)
        self.rosters[team].append(player)
        self.pick_history.append(
            DraftPick(
                round=self._current_round,
                pick_in_round=self._current_pick_in_round + 1,
                overall_pick=self.overall_pick_number(),
                team=team,
                player_name=player_name,
            )
        )
        self._advance()

    def _advance(self) -> None:
        self._current_pick_in_round += 1
        if self._current_pick_in_round >= len(self.team_names):
            self._current_pick_in_round = 0
            self._current_round += 1

    def _retreat(self) -> None:
        if self._current_pick_in_round == 0:
            self._current_round -= 1
            self._current_pick_in_round = len(self.team_names) - 1
        else:
            self._current_pick_in_round -= 1

    def undo_last_pick(self) -> Optional[DraftPick]:
        """
        Reverses the most recent pick (misclicks happen live). Returns the
        undone DraftPick, or None if there's nothing to undo. Not part of
        the original spec, but a live-draft tool without an undo button is
        one fat-fingered click away from a corrupted draft board.
        """
        if not self.pick_history:
            return None
        last = self.pick_history.pop()
        player = next(p for p in self.rosters[last.team] if p.name == last.player_name)
        self.rosters[last.team].remove(player)
        self.available[player.name] = player
        self._retreat()
        return last

    # -- cloning for "what if" simulation --------------------------------

    def clone(self) -> "DraftState":
        """
        Deep-ish copy sufficient for running hypothetical draft
        continuations without touching the live draft. Player objects are
        deep-copied because draft_engine mutates rosters/available pools
        when simulating, and config is shared read-only.
        """
        new_state = DraftState.__new__(DraftState)
        new_state.config = self.config  # read-only, safe to share
        new_state.team_names = list(self.team_names)
        new_state.snake_order = self.snake_order  # immutable once built, safe to share
        new_state.available = copy.deepcopy(self.available)
        new_state.rosters = copy.deepcopy(self.rosters)
        new_state.pick_history = list(self.pick_history)
        new_state._current_round = self._current_round
        new_state._current_pick_in_round = self._current_pick_in_round
        return new_state

    def remaining_picks_for(self, team: str) -> int:
        """How many more picks `team` has left in the draft."""
        count = 0
        for r in range(self._current_round, self.config.draft_rounds + 1):
            round_order = self.snake_order[r - 1]
            start = self._current_pick_in_round if r == self._current_round else 0
            count += round_order[start:].count(team)
        return count
