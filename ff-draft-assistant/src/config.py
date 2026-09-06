"""
config.py - Loads and validates league_config.yaml

The config file describes the league's roster shape, the list of teams,
the season's matchup schedule, and the draft format. This module turns
that YAML into small, easy-to-use dataclasses and raises a single
ConfigError (with ALL problems found, not just the first) if anything
is missing, inconsistent, or malformed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

import yaml

# Positions that can fill a FLEX slot. Kept as a module-level constant so
# other modules (lineup, draft_engine) agree on the same definition.
FLEX_ELIGIBLE = ("RB", "WR", "TE")

# The set of roster keys we understand. BENCH is capacity, not a starting
# position, and FLEX is a multi-position slot -- both are handled specially
# wherever we iterate over "positions". IDP leagues commonly use the
# position-specific DT, DE, LB, CB, and S slots, plus a P slot for punters.
KNOWN_ROSTER_KEYS = (
    "QB", "RB", "WR", "TE", "FLEX", "DST", "K", "P",
    "DT", "DE", "LB", "CB", "S", "BENCH",
)


class ConfigError(Exception):
    """Raised when league_config.yaml is missing or fails validation.

    The message is a newline-separated list of every problem found, so
    the user can fix their config in one pass instead of one error at a
    time.
    """

    def __init__(self, problems: List[str]):
        self.problems = problems
        super().__init__(
            "Invalid league_config.yaml:\n" + "\n".join(f"  - {p}" for p in problems)
        )


@dataclass
class RosterConfig:
    # e.g. {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "DT": 1, "LB": 1, "K": 1}
    starters: Dict[str, int]
    flex: int
    bench: int

    def starter_slot_count(self) -> int:
        return sum(self.starters.values()) + self.flex

    def total_roster_size(self) -> int:
        return self.starter_slot_count() + self.bench


@dataclass
class TeamConfig:
    name: str
    draft_slot: int
    is_me: bool = False


@dataclass
class LeagueConfig:
    num_teams: int
    roster: RosterConfig
    season_weeks: int
    playoff_weeks: List[int]
    teams: List[TeamConfig]
    schedule: Dict[int, List[Tuple[str, str]]]
    draft_type: str  # "snake" or "linear"
    draft_rounds: int

    def my_team(self) -> TeamConfig:
        # Validation guarantees exactly one team has is_me=True, so this
        # is safe to call after load_config() succeeds.
        return next(t for t in self.teams if t.is_me)

    def team_names(self) -> List[str]:
        return [t.name for t in self.teams]


def _require(d: dict, key: str, path: str, problems: List[str]):
    if key not in d or d[key] is None:
        problems.append(f"Missing required field: {path}.{key}")
        return None
    return d[key]


def load_config(path: str) -> LeagueConfig:
    with open(path, "r") as f:
        raw = yaml.safe_load(f) or {}

    problems: List[str] = []

    league_raw = raw.get("league")
    teams_raw = raw.get("teams")
    schedule_raw = raw.get("schedule")
    draft_raw = raw.get("draft")

    if league_raw is None:
        problems.append("Missing top-level section: league")
        league_raw = {}
    if teams_raw is None:
        problems.append("Missing top-level section: teams")
        teams_raw = []
    if schedule_raw is None:
        problems.append("Missing top-level section: schedule")
        schedule_raw = {}
    if draft_raw is None:
        problems.append("Missing top-level section: draft")
        draft_raw = {}

    # ---- league.* ----
    num_teams = _require(league_raw, "num_teams", "league", problems)
    roster_raw = _require(league_raw, "roster", "league", problems)
    season_weeks = _require(league_raw, "season_weeks", "league", problems)
    playoff_weeks = league_raw.get("playoff_weeks", []) or []

    roster_config = None
    if roster_raw is not None:
        unknown_keys = set(roster_raw.keys()) - set(KNOWN_ROSTER_KEYS)
        if unknown_keys:
            problems.append(
                f"league.roster has unrecognized keys: {sorted(unknown_keys)}. "
                f"Expected some subset of {KNOWN_ROSTER_KEYS}"
            )
        starters = {
            k: v for k, v in roster_raw.items() if k not in ("FLEX", "BENCH") and k in KNOWN_ROSTER_KEYS
        }
        for pos, count in starters.items():
            if not isinstance(count, int) or count < 0:
                problems.append(f"league.roster.{pos} must be a non-negative integer, got {count!r}")
        flex = roster_raw.get("FLEX", 0)
        bench = roster_raw.get("BENCH", 0)
        if not isinstance(flex, int) or flex < 0:
            problems.append(f"league.roster.FLEX must be a non-negative integer, got {flex!r}")
            flex = 0
        if not isinstance(bench, int) or bench < 0:
            problems.append(f"league.roster.BENCH must be a non-negative integer, got {bench!r}")
            bench = 0
        roster_config = RosterConfig(starters=starters, flex=flex, bench=bench)

    # ---- teams ----
    teams: List[TeamConfig] = []
    seen_names = set()
    seen_slots = set()
    me_count = 0
    for i, t in enumerate(teams_raw):
        name = t.get("name")
        slot = t.get("draft_slot")
        is_me = bool(t.get("is_me", False))
        if not name:
            problems.append(f"teams[{i}] is missing 'name'")
            continue
        if name in seen_names:
            problems.append(f"Duplicate team name: {name!r}")
        seen_names.add(name)
        if slot is None:
            problems.append(f"teams[{i}] ({name}) is missing 'draft_slot'")
        else:
            if slot in seen_slots:
                problems.append(f"Duplicate draft_slot {slot} (team {name!r})")
            seen_slots.add(slot)
        if is_me:
            me_count += 1
        teams.append(TeamConfig(name=name, draft_slot=slot, is_me=is_me))

    if num_teams is not None and len(teams) != num_teams:
        problems.append(
            f"league.num_teams is {num_teams} but {len(teams)} teams were listed under 'teams'"
        )
    if teams and seen_slots and (min(seen_slots) != 1 or max(seen_slots) != len(teams)):
        problems.append(
            f"teams[].draft_slot values must be exactly 1..{len(teams)} with no gaps or duplicates, "
            f"got {sorted(seen_slots)}"
        )
    if me_count == 0:
        problems.append("Exactly one team must have is_me: true -- none found")
    elif me_count > 1:
        problems.append(f"Exactly one team must have is_me: true -- found {me_count}")

    # ---- schedule ----
    schedule: Dict[int, List[Tuple[str, str]]] = {}
    valid_team_names = seen_names
    for week_key, matchups in schedule_raw.items():
        try:
            week_num = int(week_key)
        except (TypeError, ValueError):
            problems.append(f"schedule key {week_key!r} is not a valid week number")
            continue
        parsed_matchups = []
        for m in matchups or []:
            if not isinstance(m, (list, tuple)) or len(m) != 2:
                problems.append(f"schedule week {week_num} has a malformed matchup: {m!r}")
                continue
            a, b = m
            if a not in valid_team_names:
                problems.append(f"schedule week {week_num} references unknown team {a!r}")
            if b not in valid_team_names:
                problems.append(f"schedule week {week_num} references unknown team {b!r}")
            parsed_matchups.append((a, b))
        schedule[week_num] = parsed_matchups

    if season_weeks is not None:
        missing_weeks = [w for w in range(1, season_weeks + 1) if w not in schedule]
        if missing_weeks:
            problems.append(f"schedule is missing entries for week(s): {missing_weeks}")

    if teams and schedule:
        # Every team should appear exactly once per week (single matchup/week assumed).
        for week_num, matchups in schedule.items():
            appearances = [name for pair in matchups for name in pair]
            missing = valid_team_names - set(appearances)
            if missing:
                problems.append(
                    f"schedule week {week_num} does not include team(s): {sorted(missing)}"
                )
            dupes = {name for name in appearances if appearances.count(name) > 1}
            if dupes:
                problems.append(
                    f"schedule week {week_num} lists team(s) more than once: {sorted(dupes)}"
                )

    # ---- draft ----
    draft_type = draft_raw.get("type", "snake")
    if draft_type not in ("snake", "linear"):
        problems.append(f"draft.type must be 'snake' or 'linear', got {draft_type!r}")
    draft_rounds = draft_raw.get("rounds")
    if draft_rounds is None:
        problems.append("Missing required field: draft.rounds")
    elif roster_config is not None and draft_rounds != roster_config.total_roster_size():
        problems.append(
            f"draft.rounds ({draft_rounds}) does not match roster size "
            f"(starters {roster_config.starter_slot_count()} + bench {roster_config.bench} "
            f"= {roster_config.total_roster_size()}). Each round should fill exactly one roster spot."
        )

    if problems:
        raise ConfigError(problems)

    return LeagueConfig(
        num_teams=num_teams,
        roster=roster_config,
        season_weeks=season_weeks,
        playoff_weeks=list(playoff_weeks),
        teams=teams,
        schedule=schedule,
        draft_type=draft_type,
        draft_rounds=draft_rounds,
    )
