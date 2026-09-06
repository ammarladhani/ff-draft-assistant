import tempfile
import unittest
from pathlib import Path

from src.projections import ESPNProjectionSource


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
                        "player": {"fullName": "Jane QB", "defaultPositionId": 1},
                        "stats": [{"statSourceId": 1, "scoringPeriodId": 0, "appliedTotal": 170}],
                    }
                ]
            }
        )
        players = ESPNProjectionSource(2026, weeks=[1, 2], session=session).load()

        self.assertEqual(len(players), 1)
        self.assertIn("/seasons/2026/segments/0/leagues/0?view=kona_player_info", session.url)
        filter_payload = session.kwargs["headers"]["x-fantasy-filter"]
        self.assertIn("FREEAGENT", filter_payload)

    def test_parses_list_shaped_response(self):
        source = ESPNProjectionSource(season=2026, weeks=[1])
        players = source._parse_players(
            [
                {
                    "player": {"fullName": "Jane QB", "defaultPositionId": 1},
                    "stats": [{"statSourceId": 1, "scoringPeriodId": 1, "appliedTotal": 21.5}],
                }
            ]
        )
        self.assertEqual(len(players), 1)
        self.assertEqual(players[0].name, "Jane QB")
        self.assertEqual(players[0].weekly_projections, {1: 21.5})

    def test_skips_malformed_list_entries(self):
        source = ESPNProjectionSource(season=2026)
        self.assertEqual(source._parse_players(["invalid", {"player": []}]), [])

    def test_uses_weekly_projected_stats_when_available(self):
        source = ESPNProjectionSource(season=2026, weeks=[1, 2])
        players = source._parse_players(
            {
                "players": [
                    {
                        "player": {"fullName": "Jane QB", "defaultPositionId": 1, "proTeamAbbrev": "DAL"},
                        "stats": [
                            {"statSourceId": 1, "scoringPeriodId": 1, "appliedTotal": 21.5},
                            {"statSourceId": 1, "scoringPeriodId": 2, "appliedTotal": 19.0},
                        ],
                    }
                ]
            }
        )
        self.assertEqual(players[0].weekly_projections, {1: 21.5, 2: 19.0})
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


if __name__ == "__main__":
    unittest.main()
