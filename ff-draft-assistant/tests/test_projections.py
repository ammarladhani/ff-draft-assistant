import json
import tempfile
import unittest
from pathlib import Path

from src.projections import ESPN_FILTER_SLOT_IDS, ESPNProjectionSource


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload

    def get(self, *_args, **_kwargs):
        return FakeResponse(self.payload)


class RecordingSession(FakeSession):
    def get(self, url, **kwargs):
        self.url = url
        self.kwargs = kwargs
        return super().get(url, **kwargs)


class ESPNProjectionSourceTests(unittest.TestCase):
    def test_load_uses_public_league_projection_view(self):
        session = RecordingSession(
            {
                "players": [
                    {
                        "player": {
                            "fullName": "Jane QB",
                            "defaultPositionId": 1,
                            "stats": [
                                {"statSourceId": 1, "statSplitTypeId": 0, "scoringPeriodId": 0, "appliedTotal": 170}
                            ],
                        },
                    }
                ]
            }
        )
        players = ESPNProjectionSource(2026, weeks=[1, 2], session=session).load()

        self.assertEqual(len(players), 1)
        self.assertIn("/seasons/2026/segments/0/leagues/2077647142", session.url)
        self.assertEqual(session.kwargs["params"]["scoringPeriodId"], 0)
        self.assertEqual(session.kwargs["params"]["view"], "kona_playercard")
        self.assertIn("platformVersion", session.kwargs["params"])
        filter_payload = session.kwargs["headers"]["x-fantasy-filter"]
        self.assertIn("sortDraftRanks", filter_payload)
        self.assertEqual(
            json.loads(filter_payload)["players"]["filterSlotIds"]["value"],
            list(ESPN_FILTER_SLOT_IDS),
        )

    def test_parses_idp_and_punter_positions(self):
        source = ESPNProjectionSource(season=2026, weeks=[1])
        players = source._parse_players(
            {
                "players": [
                    {
                        "player": {"fullName": position, "defaultPositionId": position_id},
                        "stats": [{"statSourceId": 1, "scoringPeriodId": 1, "appliedTotal": 10}],
                    }
                    for position_id, position in {
                        8: "DT",
                        9: "DE",
                        10: "LB",
                        12: "CB",
                        13: "S",
                        18: "P",
                    }.items()
                ]
            }
        )

        self.assertEqual({player.position for player in players}, {"DT", "DE", "LB", "CB", "S", "P"})

    def test_parses_list_shaped_response(self):
        source = ESPNProjectionSource(season=2026, weeks=[1])
        players = source._parse_players(
            [
                {
                    "player": {"fullName": "Jane QB", "defaultPositionId": 1},
                    "stats": [{"statSourceId": 1, "scoringPeriodId": 0, "appliedTotal": 21.5}],
                }
            ]
        )
        self.assertEqual(len(players), 1)
        self.assertEqual(players[0].name, "Jane QB")
        self.assertEqual(players[0].weekly_projections, {1: 21.5})

    def test_skips_malformed_list_entries(self):
        source = ESPNProjectionSource(season=2026)
        self.assertEqual(source._parse_players(["invalid", {"player": []}]), [])

    def test_ignores_weekly_stats_and_uses_the_season_total(self):
        source = ESPNProjectionSource(season=2026, weeks=[1, 2])
        players = source._parse_players(
            {
                "players": [
                    {
                        "player": {"fullName": "Jane QB", "defaultPositionId": 1, "proTeamAbbrev": "DAL"},
                        "stats": [
                            {"statSourceId": 1, "scoringPeriodId": 0, "appliedTotal": 40.0},
                            {"statSourceId": 1, "scoringPeriodId": 1, "appliedTotal": 21.5},
                            {"statSourceId": 1, "scoringPeriodId": 2, "appliedTotal": 19.0},
                        ],
                    }
                ]
            }
        )
        self.assertEqual(players[0].weekly_projections, {1: 20.0, 2: 20.0})
        self.assertEqual(players[0].nfl_team, "DAL")

    def test_writes_season_total_as_standard_csv(self):
        payload = {
            "players": [
                {
                    "player": {"fullName": "Jane RB", "defaultPositionId": 2, "proTeamAbbrev": "NYJ"},
                    "stats": [{"statSourceId": 1, "scoringPeriodId": 0, "appliedTotal": 30}],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "projections.csv"
            source = ESPNProjectionSource(2026, weeks=[1, 2, 3], session=FakeSession(payload))
            self.assertEqual(source.write_csv(str(path)), 1)
            rows = path.read_text().splitlines()
        self.assertEqual(len(rows), 4)
        self.assertIn("Jane RB,NYJ,RB,1,10.0", rows[1])

    def test_splits_season_total_only_across_non_bye_weeks(self):
        source = ESPNProjectionSource(season=2026, weeks=[1, 2, 3, 4])
        players = source._parse_players(
            {
                "players": [
                    {
                        "player": {
                            "fullName": "Jane WR",
                            "defaultPositionId": 3,
                            "byeWeek": 3,
                            "stats": [
                                {"statSourceId": 1, "scoringPeriodId": 0, "appliedTotal": 60},
                                {"statSourceId": 1, "scoringPeriodId": 1, "appliedTotal": 99},
                            ],
                        }
                    }
                ]
            }
        )
        self.assertEqual(players[0].bye_week, 3)
        self.assertEqual(players[0].weekly_projections, {1: 20.0, 2: 20.0, 3: 0.0, 4: 20.0})
        self.assertEqual(players[0].season_total(), 60.0)

    def test_uses_pro_team_schedule_bye_when_player_card_has_none(self):
        source = ESPNProjectionSource(season=2026, weeks=[1, 2, 3])
        players = source._parse_players(
            {
                "players": [
                    {
                        "player": {
                            "fullName": "Jane Punter",
                            "defaultPositionId": 18,
                            "proTeamId": 12,
                            "stats": [{"statSourceId": 1, "scoringPeriodId": 0, "appliedTotal": 18}],
                        }
                    }
                ]
            },
            bye_by_team={12: 2},
        )
        self.assertEqual(players[0].position, "P")
        self.assertEqual(players[0].weekly_projections, {1: 9.0, 2: 0.0, 3: 9.0})


if __name__ == "__main__":
    unittest.main()
