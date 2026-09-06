"""
app.py - Streamlit entrypoint for the live fantasy draft assistant.

Run with:
    streamlit run app.py

Workflow:
  1. In the sidebar, load your league_config.yaml and projections.csv.
  2. Click "Start / Reset Draft" to initialize the draft board.
  3. As picks happen (yours or anyone else's), select the player and click
     "Draft this player" -- the snake order advances automatically.
  4. When it's your turn, the recommendation panel at the top shows your
     best options, weighted by value-over-replacement, your roster needs,
     and simulated season win impact.
"""

from __future__ import annotations

import os

import pandas as pd
import streamlit as st

from src.config import ConfigError, load_config
from src.draft_engine import autocomplete_draft, recommend_picks, weighted_vor_rankings
from src.draft_state import DraftState
from src.lineup import optimal_lineup
from src.projections import CSVProjectionSource, APIProjectionSource
from src.simulator import simulate_season

st.set_page_config(page_title="Fantasy Draft Assistant", layout="wide")


# ---------------------------------------------------------------------------
# Sidebar: config + projections loading
# ---------------------------------------------------------------------------

st.sidebar.title("Setup")

config_path = st.sidebar.text_input("League config path", value="league_config.yaml")
if st.sidebar.button("Load config"):
    try:
        st.session_state.config = load_config(config_path)
        st.session_state.pop("draft_state", None)  # config changed -> old draft state is stale
        st.sidebar.success(f"Loaded {len(st.session_state.config.teams)}-team league config.")
    except ConfigError as e:
        st.sidebar.error(str(e))
    except FileNotFoundError:
        st.sidebar.error(f"No file found at {config_path!r}.")

st.sidebar.divider()

projections_path = st.sidebar.text_input("Projections CSV path", value="data/projections.csv")
use_live_source = st.sidebar.checkbox(
    "Try live API source first (falls back to CSV)",
    value=False,
    help="Attempts Sleeper/FantasyPros first; the CSV above is always the fallback and the "
    "recommended default, since free weekly projection data isn't reliably available live.",
)
if st.sidebar.button("Load / refresh projections"):
    try:
        if use_live_source:
            source = APIProjectionSource(fallback_csv_path=projections_path)
        else:
            source = CSVProjectionSource(projections_path)
        players = source.load()
        st.session_state.all_players = players
        st.session_state.pop("draft_state", None)  # player pool changed -> reset the board
        st.sidebar.success(f"Loaded projections for {len(players)} players.")
    except Exception as e:  # noqa: BLE001 -- surface any loader error plainly to the sidebar
        st.sidebar.error(f"Failed to load projections: {e}")

st.sidebar.divider()

if "config" in st.session_state and "all_players" in st.session_state:
    if st.sidebar.button("Start / Reset Draft", type="primary"):
        st.session_state.draft_state = DraftState(
            st.session_state.config, list(st.session_state.all_players)
        )
        st.sidebar.success("Draft board initialized.")
else:
    st.sidebar.info("Load a config and projections file to begin.")


# ---------------------------------------------------------------------------
# Main panel
# ---------------------------------------------------------------------------

st.title("🏈 Fantasy Draft Assistant")

if "config" not in st.session_state or "all_players" not in st.session_state:
    st.info("Use the sidebar to load `league_config.yaml` and a projections CSV to get started.")
    st.stop()

if "draft_state" not in st.session_state:
    st.info("Click **Start / Reset Draft** in the sidebar to initialize the draft board.")
    st.stop()

config = st.session_state.config
state: DraftState = st.session_state.draft_state
roster_config = config.roster
my_team = config.my_team().name
num_teams = config.num_teams

# --- Pick indicator ---------------------------------------------------

top_l, top_r = st.columns([3, 1])
with top_l:
    if state.is_complete():
        st.success("Draft complete!")
    else:
        on_clock = state.current_team()
        you_flag = "  🔵 **(You)**" if on_clock == my_team else ""
        st.subheader(
            f"Round {state.current_round()} · Pick {state.overall_pick_number()} "
            f"— **{on_clock}** on the clock{you_flag}"
        )
with top_r:
    if st.button("↩️ Undo last pick") and state.pick_history:
        undone = state.undo_last_pick()
        if undone:
            st.toast(f"Undid: {undone.team} — {undone.player_name}")
            st.rerun()

st.divider()

# --- Recommendation panel (only meaningful on my turn) -----------------

if not state.is_complete() and state.current_team() == my_team:
    st.markdown("### 🎯 Recommended picks")
    with st.spinner("Simulating outcomes for top candidates..."):
        recs = recommend_picks(
            state=state,
            roster_config=roster_config,
            schedule=config.schedule,
            num_teams=num_teams,
            my_team=my_team,
            playoff_weeks=config.playoff_weeks,
        )

    top3 = recs[:3]
    cols = st.columns(len(top3)) if top3 else []
    for col, rec in zip(cols, top3):
        with col:
            st.metric(
                label=f"{rec['name']} ({rec['position']})",
                value=f"{rec['simulated_wins']:.1f} proj. wins",
                delta=f"VOR {rec['vor']:+.1f} · weight {rec['need_weight']}x",
            )
            st.caption(
                f"Weighted VOR {rec['weighted_vor']:+.1f} · "
                f"Season pts {rec['season_points']} · "
                f"Sim. PF {rec['simulated_points_for']}"
            )
            if st.button(f"Draft {rec['name']}", key=f"draft_rec_{rec['name']}"):
                try:
                    state.make_pick(rec["name"])
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))

    with st.expander("Full shortlist considered (simulated)"):
        st.dataframe(pd.DataFrame(recs), use_container_width=True, hide_index=True)

    st.caption(
        "⚠️ Simulated win impact is directional, not gospel: your remaining picks are "
        "auto-completed using best-value-available, but every other team is modeled as "
        "drafting by ADP alone -- a simplification, not real opponent intelligence."
    )
    st.divider()

# --- Available players table --------------------------------------------

st.markdown("### Available players")

f1, f2, f3 = st.columns([1, 2, 1])
with f1:
    position_filter = st.selectbox(
        "Position", ["All", "QB", "RB", "WR", "TE", "DST", "K"], key="pos_filter"
    )
with f2:
    name_search = st.text_input("Search by name", key="name_search")
with f3:
    sort_by = st.selectbox("Sort by", ["ADP", "Weighted VOR", "Season Points"], key="sort_by")

available_players = list(state.available.values())
ranked = weighted_vor_rankings(available_players, state.rosters[my_team], roster_config, num_teams)
vor_by_name = {rc.player.name: rc for rc in ranked}

rows = []
for p in available_players:
    if position_filter != "All" and p.position != position_filter:
        continue
    if name_search and name_search.lower() not in p.name.lower():
        continue
    rc = vor_by_name[p.name]
    rows.append(
        {
            "name": p.name,
            "position": p.position,
            "nfl_team": p.nfl_team,
            "bye": p.bye_week,
            "adp": p.adp if p.adp is not None else float("inf"),
            "season_points": round(p.season_total(), 1),
            "vor": round(rc.vor, 1),
            "need_weight": rc.need_weight,
            "weighted_vor": round(rc.weighted_vor, 1),
        }
    )

sort_key_map = {"ADP": "adp", "Weighted VOR": "weighted_vor", "Season Points": "season_points"}
reverse = sort_by != "ADP"
rows.sort(key=lambda r: r[sort_key_map[sort_by]], reverse=reverse)

PAGE_SIZE = 25
total_matches = len(rows)
st.caption(
    f"{total_matches} player(s) match your filters"
    + (f" -- showing top {PAGE_SIZE} by {sort_by}. Narrow your filters to see more." if total_matches > PAGE_SIZE else ".")
)

display_rows = rows[:PAGE_SIZE]
if display_rows:
    header_cols = st.columns([3, 1, 1, 1, 1, 1, 1, 1, 1])
    for c, label in zip(
        header_cols,
        ["Player", "Pos", "Team", "Bye", "ADP", "Season Pts", "VOR", "Weight", ""],
    ):
        c.markdown(f"**{label}**")

    for row in display_rows:
        c = st.columns([3, 1, 1, 1, 1, 1, 1, 1, 1])
        c[0].write(row["name"])
        c[1].write(row["position"])
        c[2].write(row["nfl_team"])
        c[3].write(row["bye"])
        c[4].write("-" if row["adp"] == float("inf") else row["adp"])
        c[5].write(row["season_points"])
        c[6].write(row["vor"])
        c[7].write(f"{row['need_weight']}x")
        if not state.is_complete():
            if c[8].button("Draft", key=f"draft_{row['name']}"):
                try:
                    state.make_pick(row["name"])
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
else:
    st.write("No players match your filters.")

st.divider()

# --- My roster / lineup / standings tabs --------------------------------

tab_roster, tab_standings = st.tabs(["My Roster & Lineup", "League Standings"])

with tab_roster:
    my_roster = state.rosters[my_team]
    if not my_roster:
        st.write("No players drafted yet.")
    else:
        roster_df = pd.DataFrame(
            [
                {
                    "name": p.name,
                    "position": p.position,
                    "team": p.nfl_team,
                    "bye": p.bye_week,
                    "season_points": round(p.season_total(), 1),
                }
                for p in my_roster
            ]
        ).sort_values("position")
        st.dataframe(roster_df, use_container_width=True, hide_index=True)

        st.markdown("#### Projected lineup for a given week")
        week = st.slider("Week", min_value=1, max_value=config.season_weeks, value=1)
        starters, bench, total = optimal_lineup(my_roster, week, roster_config)
        st.write(f"**Projected total: {total:.1f} points**")
        for slot, players in starters.items():
            for p in players:
                st.write(f"- **{slot}**: {p.name} ({p.points_for_week(week):.1f} pts)")
        if bench:
            with st.expander("Bench"):
                for p in bench:
                    st.write(f"- {p.name} ({p.position}) — {p.points_for_week(week):.1f} pts")

with tab_standings:
    st.caption(
        "Projected final standings if the draft finished from here: your remaining picks "
        "use best-value-available, every other team is modeled on ADP. Updates live as picks happen."
    )
    if st.button("Compute projected final standings"):
        preview_state = state.clone()
        autocomplete_draft(preview_state, my_team, roster_config, num_teams)
        standings = simulate_season(
            preview_state.rosters, config.schedule, roster_config, config.playoff_weeks
        )
        st.dataframe(pd.DataFrame(standings), use_container_width=True, hide_index=True)
