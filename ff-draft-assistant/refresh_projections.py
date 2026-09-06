"""Refresh league projections and print player counts by position."""

from collections import Counter

from src.projections import CSVProjectionSource, ESPNProjectionSource


source = ESPNProjectionSource(
    season=2026,
    league_id=2077647142,
    weeks=range(1, 14),
)
source.write_csv("data/projections.csv")
players = CSVProjectionSource("data/projections.csv").load()
print(f"Wrote {len(players)} players")
print(dict(sorted(Counter(player.position for player in players).items())))
