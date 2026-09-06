import tempfile
import unittest
from pathlib import Path

import yaml

from src.config import RosterConfig, load_config
from src.lineup import optimal_lineup
from src.projections import Player


IDP_ROSTER = {
    "QB": 1,
    "RB": 2,
    "WR": 2,
    "TE": 1,
    "FLEX": 1,
    "DT": 1,
    "DE": 1,
    "LB": 1,
    "CB": 1,
    "S": 1,
    "K": 1,
    "P": 1,
    "BENCH": 7,
}


class IDPRosterConfigTests(unittest.TestCase):
    def test_loads_idp_and_punter_slots_and_calculates_21_round_roster(self):
        raw_config = {
            "league": {
                "num_teams": 2,
                "roster": IDP_ROSTER,
                "season_weeks": 1,
            },
            "teams": [
                {"name": "My Team", "draft_slot": 1, "is_me": True},
                {"name": "Opponent", "draft_slot": 2, "is_me": False},
            ],
            "schedule": {1: [["My Team", "Opponent"]]},
            "draft": {"type": "snake", "rounds": 21},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "league_config.yaml"
            path.write_text(yaml.safe_dump(raw_config))
            config = load_config(str(path))

        self.assertEqual(config.roster.starters["DT"], 1)
        self.assertEqual(config.roster.starters["DE"], 1)
        self.assertEqual(config.roster.starters["LB"], 1)
        self.assertEqual(config.roster.starters["CB"], 1)
        self.assertEqual(config.roster.starters["S"], 1)
        self.assertEqual(config.roster.starters["P"], 1)
        self.assertEqual(config.roster.total_roster_size(), 21)

    def test_lineup_starts_each_configured_idp_and_punter_slot(self):
        starters = [
            Player(
                position=position,
                name=position,
                nfl_team="T",
                bye_week=None,
                weekly_projections={1: 10},
            )
            for position, count in IDP_ROSTER.items()
            if position not in ("FLEX", "BENCH")
            for _ in range(count)
        ]
        starters.append(
            Player(
                position="RB",
                name="Flex RB",
                nfl_team="T",
                bye_week=None,
                weekly_projections={1: 8},
            )
        )
        config = load_config_from_roster_for_lineup()

        lineup, bench, total = optimal_lineup(starters, 1, config)

        self.assertEqual(set(lineup), set(IDP_ROSTER) - {"BENCH"})
        self.assertEqual(bench, [])
        self.assertEqual(total, 138)


def load_config_from_roster_for_lineup():
    return RosterConfig(
        starters={
            key: value
            for key, value in IDP_ROSTER.items()
            if key not in ("FLEX", "BENCH")
        },
        flex=IDP_ROSTER["FLEX"],
        bench=IDP_ROSTER["BENCH"],
    )


if __name__ == "__main__":
    unittest.main()
