"""Diagnose and extract ESPN player-card coverage for Punters."""

from collections import Counter
import json
import requests

from src.projections import (
    ESPN_PLAYER_CARD_VIEW,
    ESPN_PLAYERS_URL,
    ESPNProjectionSource,
)

source = ESPNProjectionSource(season=2026, league_id=2077647142, weeks=range(1, 14))

cookies = {}
if source.swid and source.espn_s2:
    cookies = {"swid": source.swid, "espn_s2": source.espn_s2}

entries = []
offset = 0
limit = 2000

while True:
    filter_config = {
        "players": {
            # Slot 18 is the dedicated Punter (P) lineup slot in ESPN
            "filterSlotIds": {"value": [0, 2, 4, 6, 8, 9, 10, 12, 13, 16, 17, 18]},
            "limit": limit,
            "offset": offset,
            "sortDraftRanks": {"sortPriority": 2, "sortAsc": True, "value": "PPR"},
            "sortPercOwned": {"sortPriority": 4, "sortAsc": False},
        }
    }

    response = requests.get(
        ESPN_PLAYERS_URL.format(season=source.season, league_id=source.league_id),
        params={
            "scoringPeriodId": 0,
            "view": ESPN_PLAYER_CARD_VIEW,
            "platformVersion": source.platform_version,
        },
        headers={
            "User-Agent": "FantasyDraftAssistant/1.0",
            "x-fantasy-filter": json.dumps(filter_config),
        },
        cookies=cookies,
        timeout=20,
    )
    response.raise_for_status()

    batch = response.json().get("players", [])
    if not batch:
        break

    entries.extend(batch)
    offset += limit

    if len(batch) < limit:
        break


def is_punter(entry: dict) -> bool:
    meta = entry.get("player", entry)
    # Check for defaultPositionId 7 or explicit Punter slot 18
    return meta.get("defaultPositionId") == 7 or 18 in meta.get("eligibleSlots", [])


punters = [entry for entry in entries if is_punter(entry)]

print(f"Total Punters Extracted: {len(punters)}\n")

for p in punters:
    meta = p.get("player", p)
    name = meta.get("fullName")
    player_id = meta.get("id")
    stats = meta.get("stats", [])

    # Get projected points payload (statSourceId == 1)
    proj_stat = next(
        (s for s in stats if s.get("statSourceId") == 1 and s.get("statSplitTypeId") == 0),
        None,
    )

    proj_pts = proj_stat.get("appliedTotal", 0.0) if proj_stat else 0.0
    print(f" - {name} (ID: {player_id}) | Projected Fantasy Points: {proj_pts}")