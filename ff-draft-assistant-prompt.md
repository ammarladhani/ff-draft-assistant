# Build Prompt: Fantasy Football Draft Optimizer

Build a Python application that helps me draft optimally in a fantasy football league by simulating season outcomes and recommending picks that maximize projected wins.

## Tech stack
- Python 3.11+
- `requests` for API calls
- `pandas` for data handling
- `streamlit` for the interactive UI (needs to be usable live, during a draft, refreshing fast)
- Store everything in local files (JSON/CSV) — no database needed

## 1. Config file (`league_config.yaml`)

Create a YAML config the user fills in before drafting. Include:

```yaml
league:
  num_teams: 10
  roster:
    QB: 1
    RB: 2
    WR: 2
    TE: 1
    FLEX: 1        # eligible: RB/WR/TE
    DST: 1
    K: 1
    BENCH: 6
  season_weeks: 17
  playoff_weeks: [15, 16, 17]  # optional, for weighting

teams:
  - name: "My Team"
    draft_slot: 3
    is_me: true
  - name: "Team 2"
    draft_slot: 1
    is_me: false
  # ... one entry per team

schedule:
  # week number -> list of [team_a, team_b] matchups
  1: [["My Team", "Team 2"], ["Team 3", "Team 4"]]
  2: [...]
  # ... all weeks

draft:
  type: snake        # snake or linear
  rounds: 16
```

Write a config loader with clear validation errors (missing required fields, roster counts not matching bench/starters, schedule referencing unknown team names, etc).

## 2. Live projections data

Pull weekly player projections from the **Sleeper API** (free, no key required):
- Players: `GET https://api.sleeper.app/v1/players/nfl`
- Weekly stats/projections aren't directly in Sleeper's free API for future weeks, so as a fallback/primary source, use **FantasyPros' public consensus rankings/projections pages** via scraping, OR let the user drop in a CSV export (from FantasyPros, ESPN, or Yahoo) with columns: `player_name, team, position, week, projected_points`.

Build this as a pluggable `ProjectionSource` interface with two implementations:
1. `CSVProjectionSource` — reads a local CSV (this should be the default, most reliable path)
2. `APIProjectionSource` — attempts Sleeper/FantasyPros fetch, falls back to CSV if it fails

Cache all fetched projections to disk (`data/projections_cache.json`) so repeated draft-tool refreshes don't hammer the API.

Data model per player: `{name, position, nfl_team, bye_week, weekly_projections: {week: points}, adp}`.

## 3. Weekly lineup optimizer

Given a team's rostered players and the roster config, write a function `optimal_lineup(roster, week, roster_config) -> (starters, bench, total_points)` that:
- Picks the highest-projected player at each required position slot
- Fills FLEX with the best remaining eligible RB/WR/TE
- Handles bye weeks (0 points that week, excluded from lineup)
- Returns total projected points for that week

This is just a greedy assignment — no need for real optimization solvers, but sort by position first, fill locked slots, then fill FLEX with leftover best player.

## 4. Season simulator

Write `simulate_season(all_team_rosters, schedule, roster_config) -> standings` that:
- For every week, computes each team's optimal lineup total (step 3)
- For every matchup in the schedule, the team with the higher weekly total wins
- Tallies wins/losses/points-for/points-against per team across all weeks
- Returns a standings table sorted by wins, then points-for

## 5. Draft recommendation engine (the core "magic")

At each of my draft picks, I need a ranked recommendation of available players. Implement this approach:

**Step A — Value over Replacement (VOR):**
For each available player, compute VOR = (player's projected season total) − (average projected season total of players at the same position ranked just outside starting lineups league-wide, i.e. the "replacement level" player at that position, given `num_teams × starters_at_position`).

**Step B — Positional need weighting:**
Multiply VOR by a need multiplier based on my current roster: if a position is already filled at all starter slots and I have bench depth, reduce weight (~0.5x); if a starter slot is empty, boost weight (~1.3x); never let a filled position with a full bench go above 0 priority unless it's clearly elite value (top-3 remaining overall).

**Step C — Simulated win impact (the real magic):**
For the top 5–8 candidates by weighted VOR, actually simulate drafting each one:
1. Add candidate to my roster
2. Auto-complete the rest of MY draft using a simple best-VOR-available heuristic for remaining rounds
3. Auto-complete all OTHER teams' drafts using best-ADP-available (simplest reasonable opponent model)
4. Run `simulate_season()` on the resulting full league
5. Record my resulting win total

Recommend the candidate that produces the highest simulated win total. Break ties with total projected points.

Note in the UI that steps 3-5 are a simplified projection (opponent behavior is approximated by ADP, not real intelligence) — this is directional, not gospel.

## 6. Streamlit UI

Build a single-page live-draft app:
- Sidebar: load config, load/refresh projections CSV
- Main panel: 
  - Current pick indicator (whose turn, round/pick number)
  - Big recommendation panel showing top 3 recommended players with their VOR, need-weight, and simulated win impact
  - Full available-players table, sortable/filterable by position, searchable by name
  - "Draft this player" button per row that assigns them to whichever team is currently on the clock (auto-advances the snake order) and removes them from the pool
  - Sidebar or tab showing my current roster + projected weekly lineup + running simulated standings, updating live as picks happen

## 7. File structure

```
ff-draft-assistant/
├── league_config.yaml
├── data/
│   ├── projections.csv          # user-provided or fetched
│   └── projections_cache.json
├── src/
│   ├── config.py                # config loading/validation
│   ├── projections.py           # ProjectionSource classes
│   ├── lineup.py                # optimal_lineup()
│   ├── simulator.py             # simulate_season()
│   ├── draft_engine.py          # VOR, need weighting, recommendation
│   └── draft_state.py           # tracks picks made, whose turn, rosters
├── app.py                       # Streamlit entrypoint
└── requirements.txt
```

## Deliverable expectations

- Fully runnable end-to-end: `streamlit run app.py` should work after the user fills in `league_config.yaml` and drops a projections CSV in `data/`.
- Include a sample `league_config.yaml` for a 10-team league and a sample `projections.csv` with a handful of fake players so the app is demoable without real data.
- Comment the draft simulation logic clearly since it's the most complex part.
- Don't over-engineer the opponent-behavior model — ADP-based greedy is fine and should be explicitly labeled as a simplifying assumption in a code comment and in the UI.
