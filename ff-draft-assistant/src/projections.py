"""
projections.py - Player data model + pluggable projection sources.

Two sources are provided behind a common ProjectionSource interface:

- CSVProjectionSource: reads a local CSV export (FantasyPros/ESPN/Yahoo-style).
  This is the DEFAULT and most reliable path -- weekly fantasy projections
  aren't reliably available from a free, keyless API, so the CSV is treated
  as the source of truth and the API source is best-effort/supplementary.

- APIProjectionSource: pulls player metadata (name/position/team/bye) from
  Sleeper's free public API, attempts to enrich with FantasyPros consensus
  rankings, and falls back to a CSVProjectionSource if either step fails
  for any reason (network error, HTML layout change, rate limiting, etc).
  This is intentionally defensive: draft day is the worst time for an
  unhandled exception.

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
from typing import Dict, List, Optional

import pandas as pd

SLEEPER_PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"
CACHE_MAX_AGE_SECONDS = 60 * 60  # 1 hour -- plenty fresh for a single draft night


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


class APIProjectionSource(ProjectionSource):
    """
    Best-effort live source. Tries to:
      1. Pull player metadata (name, position, team, bye week is NOT
         reliably in Sleeper's payload, so bye weeks may come back None).
      2. Supplement with FantasyPros public rankings pages (best-effort
         scrape -- FantasyPros does not offer a free projections API, and
         their HTML structure can change without notice, so this step is
         wrapped in a broad try/except).

    If EITHER step fails for any reason, we fall back to the provided
    CSVProjectionSource rather than raising -- a stale or manually
    exported CSV beats a crashed draft tool.
    """

    def __init__(
        self,
        fallback_csv_path: str,
        cache_path: str = "data/projections_cache.json",
        session=None,
    ):
        self.fallback = CSVProjectionSource(fallback_csv_path)
        self.cache_path = cache_path
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
        import requests

        resp = requests.get(SLEEPER_PLAYERS_URL, timeout=15)
        resp.raise_for_status()
        raw = resp.json()

        players = []
        for _, meta in raw.items():
            if meta.get("position") not in ("QB", "RB", "WR", "TE", "DST", "K"):
                continue
            name = meta.get("full_name") or f"{meta.get('first_name', '')} {meta.get('last_name', '')}".strip()
            if not name:
                continue
            players.append(
                Player(
                    name=name,
                    position=meta["position"],
                    nfl_team=(meta.get("team") or "FA"),
                    bye_week=None,  # Sleeper's free endpoint does not expose this reliably.
                    weekly_projections={},  # Filled in by _enrich_with_fantasypros below.
                    adp=None,
                )
            )

        self._enrich_with_fantasypros(players)

        # Without real weekly projections, this source is not usable on its
        # own -- surface that to the caller so it falls back to CSV instead
        # of silently returning an all-zero recommendation engine.
        if not any(p.weekly_projections for p in players):
            raise RuntimeError(
                "Sleeper metadata fetched, but no weekly projection data could "
                "be attached (FantasyPros enrichment unavailable). "
                "A CSV export is required for real point projections."
            )
        return players

    def _enrich_with_fantasypros(self, players: List[Player]) -> None:
        """
        Best-effort scrape of FantasyPros' public consensus rankings.

        This is deliberately conservative: FantasyPros' page structure is
        not a stable, documented API, so any parsing failure here is
        swallowed and simply results in players with no weekly_projections
        (which triggers the CSV fallback in _fetch_live above). Treat this
        as a "nice to have" enrichment step, never a load-bearing one.
        """
        try:
            import requests

            resp = requests.get(
                "https://www.fantasypros.com/nfl/rankings/consensus-cheatsheets.php",
                timeout=15,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            resp.raise_for_status()
            # Real parsing would go here (e.g. with pandas.read_html or an
            # HTML parser targeting FantasyPros' ranking table). Intentionally
            # not implemented against a moving-target HTML page in this
            # starter project -- wire this up if/when you pin down their
            # current markup, or just rely on the CSV path, which is the
            # recommended default anyway.
            return
        except Exception:
            return

    def _load_cache_if_fresh(self) -> Optional[List[Player]]:
        if not os.path.exists(self.cache_path):
            return None
        try:
            with open(self.cache_path, "r") as f:
                payload = json.load(f)
            if time.time() - payload.get("fetched_at", 0) > CACHE_MAX_AGE_SECONDS:
                return None
            return [Player.from_dict(p) for p in payload.get("players", [])]
        except Exception:
            return None

    def _write_cache(self, players: List[Player]) -> None:
        os.makedirs(os.path.dirname(self.cache_path) or ".", exist_ok=True)
        payload = {
            "fetched_at": time.time(),
            "players": [p.to_dict() for p in players],
        }
        with open(self.cache_path, "w") as f:
            json.dump(payload, f)
