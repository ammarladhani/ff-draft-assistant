"""
projections.py - Player data model + pluggable projection sources.

Two sources are provided behind a common ProjectionSource interface:

- CSVProjectionSource: reads a local CSV export (FantasyPros/ESPN/Yahoo-style).
  This is the DEFAULT and most reliable path -- weekly fantasy projections
  aren't reliably available from a free, keyless API, so the CSV is treated
  as the source of truth and the API source is best-effort/supplementary.

- ESPNProjectionSource: turns ESPN's public fantasy player projections into
  the CSV shape the application uses.  ESPN's projection payload is season
  total in some years and weekly in others; season totals are split across
  the selected regular-season weeks when weekly values are unavailable.

- APIProjectionSource: a backwards-compatible live-source wrapper. It uses
  ESPN for points, then optionally enriches matching players with Sleeper's
  free player catalogue. It falls back to CSV if ESPN is unavailable.

All fetched/derived projections are cached to data/projections_cache.json
so refreshing the Streamlit app during a live draft doesn't repeatedly
hammer an external API.
"""

from __future__ import annotations

import json
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pandas as pd

SLEEPER_PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"
ESPN_PLAYERS_URL = (
    "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/"
    "seasons/{season}/segments/0/leagues/{league_id}"
)
ESPN_PLAYER_CARD_VIEW = "kona_playercard"
ESPN_PLATFORM_VERSION = "2d26c1207d605a9476197b1a24b8bf8bcf44eb2f"
ESPN_PRO_TEAM_SCHEDULES_URL = (
    "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/"
    "seasons/{season}"
)
CACHE_MAX_AGE_SECONDS = 60 * 60  # 1 hour -- plenty fresh for a single draft night

# Roster-slot IDs, NOT `defaultPositionId`. A player's `eligibleSlots` list is
# the authoritative source for their position on this endpoint --
# `defaultPositionId` does NOT reliably line up with these same numbers.
# Confirmed via live kona_playercard payloads:
#   - Jeffery Simmons (a real DT) reports defaultPositionId=9,
#     eligibleSlots includes 8.
#   - Myles Garrett (a real DE) reports defaultPositionId=10,
#     eligibleSlots includes 9.
# i.e. defaultPositionId is shifted relative to the roster-slot numbering
# and cannot be used directly as a position lookup key for IDP/K/P. Do not
# reintroduce a defaultPositionId -> position map without re-verifying
# against a live payload first.
ROSTER_SLOT_TO_POSITION = {
    0: "QB",
    2: "RB",
    4: "WR",
    6: "TE",
    8: "DT",
    9: "DE",
    10: "LB",
    12: "CB",
    13: "S",
    16: "DST",
    17: "K",
    18: "P",
}
ESPN_FILTER_SLOT_IDS = (0, 2, 4, 6, 8, 9, 10, 12, 13, 16, 17, 18)


def _position_from_slots(meta: dict, fallback_entry: Optional[dict] = None) -> Optional[str]:
    """Resolve a player's fantasy position from `eligibleSlots`.

    Checked against both the nested `player` object and the raw entry,
    since ESPN has put `eligibleSlots` in slightly different places across
    response shapes. Returns the first roster slot in the list that maps to
    a known position -- a player can be slot-eligible for a handful of
    utility/flex slots too, but those aren't in `ROSTER_SLOT_TO_POSITION`.
    """
    for container in (meta, fallback_entry or {}):
        if not isinstance(container, dict):
            continue
        for slot in container.get("eligibleSlots", []) or []:
            position = ROSTER_SLOT_TO_POSITION.get(slot)
            if position:
                return position
    return None


@dataclass
class Player:
    name: str
    position: str
    nfl_team: str
    bye_week: Optional[int]
    weekly_projections: Dict[int, float] = field(default_factory=dict)
    adp: Optional[float] = None

    def points_for_week(self, week: int) -> float:
        """0.0 on a bye or if we simply have no projection for that week."""
        if self.bye_week is not None and week == self.bye_week:
            return 0.0
        return self.weekly_projections.get(week, 0.0)

    def season_total(self) -> float:
        return sum(self.weekly_projections.values())

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "position": self.position,
            "nfl_team": self.nfl_team,
            "bye_week": self.bye_week,
            "weekly_projections": self.weekly_projections,
            "adp": self.adp,
        }

    @staticmethod
    def from_dict(d: dict) -> "Player":
        return Player(
            name=d["name"],
            position=d["position"],
            nfl_team=d.get("nfl_team", ""),
            bye_week=d.get("bye_week"),
            # JSON object keys are always strings -- convert week numbers back to int.
            weekly_projections={int(k): v for k, v in d.get("weekly_projections", {}).items()},
            adp=d.get("adp"),
        )


class ProjectionSource(ABC):
    @abstractmethod
    def load(self) -> List[Player]:
        """Return the full list of players with their weekly projections."""
        raise NotImplementedError


class CSVProjectionSource(ProjectionSource):
    """
    Reads a CSV with (at minimum) columns:
        player_name, team, position, week, projected_points
    Optional columns:
        bye_week, adp

    One row per (player, week). Rows for the same player are grouped into
    a single Player with a weekly_projections dict.
    """

    REQUIRED_COLUMNS = ("player_name", "team", "position", "week", "projected_points")

    def __init__(self, csv_path: str):
        self.csv_path = csv_path

    def load(self) -> List[Player]:
        if not os.path.exists(self.csv_path):
            raise FileNotFoundError(
                f"Projections CSV not found at {self.csv_path}. "
                f"Export one from FantasyPros/ESPN/Yahoo with columns: "
                f"{', '.join(self.REQUIRED_COLUMNS)}"
            )

        df = pd.read_csv(self.csv_path)
        missing = [c for c in self.REQUIRED_COLUMNS if c not in df.columns]
        if missing:
            raise ValueError(
                f"{self.csv_path} is missing required column(s): {missing}. "
                f"Required columns: {', '.join(self.REQUIRED_COLUMNS)}"
            )

        has_adp = "adp" in df.columns
        has_bye = "bye_week" in df.columns

        players: Dict[str, Player] = {}
        for _, row in df.iterrows():
            name = str(row["player_name"]).strip()
            if name not in players:
                bye = None
                if has_bye and not pd.isna(row.get("bye_week")):
                    bye = int(row["bye_week"])
                adp = None
                if has_adp and not pd.isna(row.get("adp")):
                    adp = float(row["adp"])
                players[name] = Player(
                    name=name,
                    position=str(row["position"]).strip().upper(),
                    nfl_team=str(row["team"]).strip().upper(),
                    bye_week=bye,
                    adp=adp,
                )
            week = int(row["week"])
            points = float(row["projected_points"])
            players[name].weekly_projections[week] = points

        result = list(players.values())

        # If no ADP column was supplied at all, derive a stand-in ranking
        # from season-total projected points (best available proxy) so
        # downstream code (opponent modeling) always has an adp to sort by.
        if not has_adp:
            for i, p in enumerate(sorted(result, key=lambda p: p.season_total(), reverse=True)):
                p.adp = float(i + 1)

        return result


class ESPNProjectionSource(ProjectionSource):
    """Load ESPN fantasy projections and optionally write them as our CSV.

    ESPN exposes player projections without credentials, but does not promise
    a stable schema. Keeping its parsing here (rather than in the Streamlit
    view) makes failures explicit and leaves the existing CSV workflow intact.
    ``session`` is injectable so parsing can be tested without network access.
    """

    def __init__(
        self,
        season: int,
        league_id: int | str = 2077647142,
        weeks=range(1, 18),
        session=None,
        platform_version: Optional[str] = ESPN_PLATFORM_VERSION,
        swid: Optional[str] = None,
        espn_s2: Optional[str] = None,
    ):
        self.season = season
        self.league_id = league_id
        self.weeks = list(weeks)
        self._session = session
        self.platform_version = platform_version
        # Private leagues require the same two cookies your signed-in ESPN
        # browser session uses. Environment variables keep them out of source
        # control and work for both Streamlit and refresh_projections.py.
        self.swid = swid or os.getenv("ESPN_SWID")
        self.espn_s2 = espn_s2 or os.getenv("ESPN_S2")

    def load(self) -> List[Player]:
        import requests

        client = self._session or requests

        # Properly defined cookies dictionary
        cookies = {}
        if self.swid:
            cookies["swid"] = self.swid
        if self.espn_s2:
            cookies["espn_s2"] = self.espn_s2

        response = client.get(
            ESPN_PLAYERS_URL.format(season=self.season, league_id=self.league_id),
            timeout=20,
            params={"view": ESPN_PLAYER_CARD_VIEW},
            cookies=cookies if cookies else None,
            headers={
                "User-Agent": "FantasyDraftAssistant/1.0",
                # ESPN otherwise returns only a small, popularity-sorted
                # page. ``leaguedefaults/3`` is ESPN's public PPR scoring
                # configuration and does not require league credentials.
                "x-fantasy-filter": json.dumps(
                    {
                        "players": {
                            # Fixed: Use the module-level constant to ensure slot 18 (P) is included
                            "filterSlotIds": {"value": list(ESPN_FILTER_SLOT_IDS)},
                            "limit": 2000,
                            "offset": 0,
                            "sortDraftRanks": {
                                "sortPriority": 2,
                                "sortAsc": True,
                                "value": "PPR",
                            },
                            "sortPercOwned": {"sortPriority": 4, "sortAsc": False},
                        }
                    }
                ),
            },
        )
        response.raise_for_status()
        return self._parse_players(response.json())

    def write_csv(self, csv_path: str) -> int:
        """Fetch, validate, and replace ``csv_path`` with standard CSV rows."""
        players = self.load()
        if not players:
            raise RuntimeError("ESPN returned no draftable players with projections.")
        rows = []
        for player in players:
            for week, points in sorted(player.weekly_projections.items()):
                rows.append(
                    {
                        "player_name": player.name,
                        "team": player.nfl_team,
                        "position": player.position,
                        "week": week,
                        "projected_points": round(points, 2),
                        "bye_week": player.bye_week,
                        "adp": player.adp,
                    }
                )
        os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)
        pd.DataFrame(rows).to_csv(csv_path, index=False)
        return len(players)

    def _parse_players(
        self, payload: Any, bye_by_team: Optional[Dict[int, int]] = None
    ) -> List[Player]:
        """Parse ESPN's documented and list-shaped player response formats.

        ESPN has returned both a ``{"players": [...]}`` object and a bare
        player list from this endpoint.  Validate provider data at this
        boundary so a schema variation produces no players (and a useful UI
        error) rather than an ``AttributeError``.
        """
        if isinstance(payload, list):
            entries = payload
        elif isinstance(payload, dict):
            entries = payload.get("players", [])
        else:
            return []
        if not isinstance(entries, list):
            return []

        result = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            meta = entry.get("player", entry)
            if not isinstance(meta, dict):
                continue
            position = _position_from_slots(meta, fallback_entry=entry)
            name = meta.get("fullName") or meta.get("name")
            if not name or not position:
                continue
            bye_week = self._bye_week(meta, fallback_entry=entry)
            if bye_week is None:
                try:
                    bye_week = (bye_by_team or {}).get(int(meta.get("proTeamId")))
                except (TypeError, ValueError):
                    pass
            weekly = self._weekly_points(meta, bye_week, fallback_entry=entry)
            if not weekly:
                continue
            result.append(
                Player(
                    name=name.strip(),
                    position=position,
                    nfl_team=str(meta.get("proTeamAbbrev") or meta.get("proTeamId") or "FA"),
                    bye_week=bye_week,
                    weekly_projections=weekly,
                    adp=self._adp(entry),
                )
            )
        return result

    def _weekly_points(
        self,
        player: dict,
        bye_week: Optional[int],
        fallback_entry: Optional[dict] = None,
    ) -> Dict[int, float]:
        """Allocate ESPN's period-zero season projection over non-bye weeks."""
        season_total = None
        stats = player.get("stats")
        if not isinstance(stats, list) and fallback_entry is not None:
            stats = fallback_entry.get("stats", [])
        if not isinstance(stats, list):
            return {}
        for stat in stats:
            if not isinstance(stat, dict):
                continue
            if stat.get("statSourceId") not in (None, 1):
                continue
            if stat.get("statSplitTypeId", 0) != 0:
                continue
            value = stat.get("appliedTotal", stat.get("projectedTotal"))
            if value is None:
                continue
            if int(stat.get("scoringPeriodId", 0)) == 0:
                season_total = float(value)
        if season_total is None or not self.weeks:
            return {}
        playing_weeks = [week for week in self.weeks if week != bye_week]
        if not playing_weeks:
            return {}
        points = season_total / len(playing_weeks)
        return {week: (0.0 if week == bye_week else points) for week in self.weeks}

    @staticmethod
    def _bye_week(player: dict, fallback_entry: Optional[dict] = None) -> Optional[int]:
        """Read a bye from ESPN's player-card response when it is present."""
        for container in (player, fallback_entry or {}):
            if not isinstance(container, dict):
                continue
            for key in ("byeWeek", "bye_week"):
                value = container.get(key)
                if value is not None:
                    try:
                        return int(value)
                    except (TypeError, ValueError):
                        pass
            for key in ("proTeam", "team", "schedule"):
                nested = container.get(key)
                if isinstance(nested, dict):
                    for bye_key in ("byeWeek", "bye_week"):
                        value = nested.get(bye_key)
                        if value is not None:
                            try:
                                return int(value)
                            except (TypeError, ValueError):
                                pass
        return None

    @staticmethod
    def _adp(entry: dict) -> Optional[float]:
        for key in ("draftRanksByRankType", "ratings"):
            ratings = entry.get(key, {})
            if isinstance(ratings, dict):
                for rating in ratings.values():
                    if isinstance(rating, dict) and rating.get("rank") is not None:
                        return float(rating["rank"])
        return None


class APIProjectionSource(ProjectionSource):
    """
    Best-effort live source. Uses ESPN's public fantasy player feed for
    projections, then Sleeper's player catalogue for current NFL team data.

    If ESPN fails for any reason, we fall back to the provided
    CSVProjectionSource rather than raising -- a stale or manually exported
    CSV beats a crashed draft tool. Sleeper enrichment is optional.
    """

    def __init__(
        self,
        fallback_csv_path: str,
        cache_path: str = "data/projections_cache.json",
        season: Optional[int] = None,
        league_id: int | str = 2077647142,
        weeks=range(1, 18),
        session=None,
        swid: Optional[str] = None,
        espn_s2: Optional[str] = None,
    ):
        self.fallback = CSVProjectionSource(fallback_csv_path)
        self.cache_path = cache_path
        self.season = season or time.gmtime().tm_year
        self.league_id = league_id
        self.weeks = list(weeks)
        self.swid = swid
        self.espn_s2 = espn_s2
        # `session` is injectable for testing; defaults to the `requests`
        # library's module-level functions via a tiny shim below.
        self._session = session

    def load(self) -> List[Player]:
        cached = self._load_cache_if_fresh()
        if cached is not None:
            return cached

        try:
            players = self._fetch_live()
            if players:
                self._write_cache(players)
                return players
        except Exception as exc:  # noqa: BLE001 -- deliberately broad: any
            # live-fetch failure should degrade to the CSV, not crash the app.
            print(f"[projections] Live fetch failed ({exc!r}); falling back to CSV.")

        return self.fallback.load()

    # -- internals -----------------------------------------------------

    def _fetch_live(self) -> List[Player]:
        players = ESPNProjectionSource(
            season=self.season,
            league_id=self.league_id,
            weeks=self.weeks,
            session=self._session,
            swid=self.swid,
            espn_s2=self.espn_s2,
        ).load()
        if not players:
            raise RuntimeError("ESPN returned no usable fantasy projections.")

        # Sleeper is metadata only: it cannot supply future weekly fantasy
        # points. A failed enrichment is non-fatal because ESPN already gave
        # us a complete set of projections.
        try:
            self._enrich_with_sleeper(players)
        except Exception as exc:  # noqa: BLE001 -- metadata is optional
            print(f"[projections] Sleeper metadata fetch failed ({exc!r}).")
        return players

    def _enrich_with_sleeper(self, players: List[Player]) -> None:
        import requests

        client = self._session or requests
        resp = client.get(SLEEPER_PLAYERS_URL, timeout=15)
        resp.raise_for_status()
        raw = resp.json()
        sleeper_by_name = {
            (meta.get("full_name") or "").casefold(): meta for meta in raw.values()
        }
        for player in players:
            meta = sleeper_by_name.get(player.name.casefold())
            if meta and meta.get("team"):
                player.nfl_team = meta["team"]

    def _load_cache_if_fresh(self) -> Optional[List[Player]]:
        if not os.path.exists(self.cache_path):
            return None
        try:
            with open(self.cache_path, "r") as f:
                payload = json.load(f)
            if time.time() - payload.get("fetched_at", 0) > CACHE_MAX_AGE_SECONDS:
                return None
            if (
                payload.get("season") != self.season
                or str(payload.get("league_id")) != str(self.league_id)
                or payload.get("weeks") != self.weeks
            ):
                return None
            return [Player.from_dict(p) for p in payload.get("players", [])]
        except Exception:
            return None

    def _write_cache(self, players: List[Player]) -> None:
        os.makedirs(os.path.dirname(self.cache_path) or ".", exist_ok=True)
        payload = {
            "fetched_at": time.time(),
            "season": self.season,
            "league_id": self.league_id,
            "weeks": self.weeks,
            "players": [p.to_dict() for p in players],
        }
        with open(self.cache_path, "w") as f:
            json.dump(payload, f)