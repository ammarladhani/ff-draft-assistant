"""Diagnose ESPN player-card coverage for the league's punter position."""

from collections import Counter
import json

import requests

from src.projections import ESPN_PLAYER_CARD_VIEW, ESPN_PLAYERS_URL, ESPNProjectionSource


source = ESPNProjectionSource(season=2026, league_id=2077647142, weeks=range(1, 14))
response = requests.get(
    ESPN_PLAYERS_URL.format(season=source.season, league_id=source.league_id),
    params={
        "scoringPeriodId": 0,
        "view": ESPN_PLAYER_CARD_VIEW,
        "platformVersion": source.platform_version,
    },
    headers={
        "User-Agent": "FantasyDraftAssistant/1.0",
        "x-fantasy-filter": json.dumps(
            {
                "players": {
                    "filterSlotIds": {"value": list(range(26))},
                    "limit": 2000,
                    "offset": 0,
                    "sortDraftRanks": {"sortPriority": 2, "sortAsc": True, "value": "PPR"},
                    "sortPercOwned": {"sortPriority": 4, "sortAsc": False},
                }
            }
        ),
    },
    cookies=source._auth_cookies(),
    timeout=20,
)
response.raise_for_status()
entries = response.json().get("players", [])
punters = [entry.get("player", entry) for entry in entries if entry.get("player", entry).get("defaultPositionId") == 18]
print("Default-position IDs:", dict(sorted(Counter(entry.get("player", entry).get("defaultPositionId") for entry in entries).items())))
print(f"Punter entries: {len(punters)}")
print("Punter entries with a period-zero projection:", sum(
    any(stat.get("scoringPeriodId") == 0 and stat.get("appliedTotal") is not None for stat in player.get("stats", []))
    for player in punters
))
