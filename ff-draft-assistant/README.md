# Fantasy Football Draft Assistant

A live-draft tool that recommends picks by simulating full mock-draft
completions and season outcomes, not just static rankings.

## Quick start

```bash
pip install -r requirements.txt
streamlit run app.py
```

Then in the sidebar:
1. Load `league_config.yaml` (a sample 10-team config is included).
2. Load `data/projections.csv` (a sample CSV with 184 fake players is included).
3. Click **Start / Reset Draft**.

The app is immediately demoable with the sample data — no real projections needed to try it out.

## Using it with your real league

1. **Fill in `league_config.yaml`**: your teams, draft slots, roster
   settings, and season schedule. The loader validates it thoroughly
   (missing fields, roster/bench math that doesn't add up, unknown team
   names in the schedule, etc.) and reports every problem it finds at
   once.
2. **Export projections to CSV** from FantasyPros, ESPN, or Yahoo with
   at minimum these columns: `player_name, team, position, week, projected_points`.
   Optional columns `bye_week` and `adp` are used if present. Drop it in
   `data/projections.csv` (or point the sidebar at wherever you saved it).
3. Run the draft — as each pick happens (yours or an opponent's), select
   the player and hit **Draft**. The snake order advances automatically.

## How the recommendation engine works

At each of your picks, three layered steps run (see `src/draft_engine.py`
for the fully-commented implementation):

1. **Value over Replacement (VOR)** — each available player's projected
   season total, minus the projected total of the last player who'd make
   a league-wide starting lineup at that position.
2. **Positional need weighting** — VOR is multiplied by how much *you*
   need that position right now (1.3x if a starter slot is open, 0.5x if
   the position is filled but you still have bench room, 0x if the
   position and your bench are both full — unless the player is a
   top-3-overall value, in which case the zero is overridden).
3. **Simulated win impact** — for the top 5–8 candidates by weighted VOR,
   the app actually drafts each one, auto-completes the rest of the draft
   (you via best-value-available, everyone else via best-ADP-available),
   and runs a full season simulation to see how many games you'd win.
   The recommendation is whichever candidate produced the most wins.

## Known simplifications (by design, not bugs)

- **Opponent modeling is ADP-only.** Real opponents draft on need, hunches,
  and sleepers, not a single consensus number. Treat simulated win totals
  as *directional* signal for comparing your top candidates, not a real
  prediction — this is called out in the UI on every recommendation.
- **The lineup optimizer is greedy, not a real solver.** It fills
  dedicated slots first, then FLEX with the best leftover RB/WR/TE. This
  is optimal for this specific slot structure, but wouldn't generalize to
  more exotic multi-position eligibility rules.
- **Replacement level is recomputed against the live undrafted pool**,
  not a frozen pre-draft baseline. This keeps VOR honest as the board
  changes, but positions with a shallow total universe (DST, K) can look
  artificially attractive late in a draft once most of that position is
  gone — a known quirk of any pool-relative VOR heuristic. If you want to
  curb it, the easiest lever is a hard per-position bench cap in
  `draft_engine.need_multiplier` (not implemented, to avoid overbuilding
  past what was asked for).
- **FantasyPros enrichment in `APIProjectionSource` is a stub.** FantasyPros
  doesn't offer a free, stable projections API, so `_enrich_with_fantasypros`
  fetches the page but doesn't parse it (their HTML markup isn't a
  contract and changes without notice). The CSV path is the recommended
  and fully-implemented source; the API path exists for player metadata
  and gracefully falls back to your CSV for actual point projections.
- **`playoff_weeks` is tracked but not weighted.** Standings tally every
  week equally; `playoff_points_for` is broken out per team in case you
  want to use it as a tiebreaker or add weighting later.

## File structure

```
ff-draft-assistant/
├── league_config.yaml        # sample 10-team config
├── data/
│   ├── projections.csv       # sample projections (184 fake players)
│   └── projections_cache.json  # created automatically by APIProjectionSource
├── src/
│   ├── config.py             # YAML loading + validation
│   ├── projections.py        # Player model, CSV + API projection sources
│   ├── lineup.py             # optimal_lineup()
│   ├── simulator.py          # simulate_season()
│   ├── draft_engine.py       # VOR, need weighting, win-impact simulation
│   └── draft_state.py        # snake order, picks, rosters, undo
├── app.py                    # Streamlit UI
└── requirements.txt
```

## A note on the sample data

The bundled `league_config.yaml` is a 10-team league with a real
round-robin schedule (every team plays every other team on a repeating
cycle across 17 weeks). `data/projections.csv` has 184 fake players
across all rostered positions with per-week noise and bye weeks, enough
to run a full 16-round, 10-team mock draft. None of it reflects any real
players, teams, or projections — swap in your own CSV before drafting for real.
