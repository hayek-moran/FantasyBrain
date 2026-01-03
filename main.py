import streamlit as st
import requests
import pandas as pd
import pulp
from PIL import Image, ImageDraw, ImageFont

# basic page setup
st.set_page_config(page_title="FantasyBrain", page_icon="🧠", layout="wide")

# main header
st.title("FantasyBrain 🧠⚽")
st.subheader("AI-Powered Wildcard Optimizer")

# --- global settings ---
PLAYER_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
FIXTURES_URL = "https://fantasy.premierleague.com/api/fixtures/"

# filters
MIN_PPG = 4.0
MIN_AVG_MINUTES = 30

# weights for the algorithm
WEIGHTS = {
    'form': 1.5, 'ep_next': 1.0, 'difficulty': 1.0, 'season_points': 1.5,
    'efficiency': 1.0, 'differential': 1.0, 'momentum': 0.5
}

# pitch coordinates for 4-4-2
FORMATION_COORDS = {
    'GKP': [(350, 520)],
    'DEF': [(50, 400), (200, 420), (500, 420), (650, 400)],
    'MID': [(50, 250), (200, 270), (500, 270), (650, 250)],
    'FWD': [(250, 100), (450, 100)]
}


# --- helper functions ---

@st.cache_data
def fetch_data(url):
    # fetching raw data from fpl api
    response = requests.get(url)
    if response.status_code == 200:
        return response.json()
    return None


def create_lookup_maps(data):
    # mapping ids to names
    teams_list = data['teams']
    team_map = {team['id']: team['name'] for team in teams_list}
    positions_list = data['element_types']
    position_map = {pos['id']: pos['singular_name_short'] for pos in positions_list}
    return team_map, position_map


def get_gameweek_ids(data):
    # getting current and next gameweek
    next_gameweek_id = None
    for event in data['events']:
        if event['is_next']:
            next_gameweek_id = event['id']
            break
    if next_gameweek_id is None: return None, None
    return max(1, next_gameweek_id - 1), next_gameweek_id


def create_player_dataframe(data, team_map, position_map):
    # creating the main player table
    players_list = data['elements']
    players_df = pd.DataFrame(players_list)

    relevant_columns = ['id', 'web_name', 'team', 'element_type', 'now_cost', 'total_points', 'minutes', 'form',
                        'ep_next', 'bps', 'selected_by_percent', 'status', 'transfers_in_event']
    players_df = players_df[relevant_columns].copy()

    # fixing price format
    players_df['now_cost'] = players_df['now_cost'] / 10.0
    players_df['team_name'] = players_df['team'].map(team_map)
    players_df['position'] = players_df['element_type'].map(position_map)

    return players_df


def create_opponent_dataframe(fixtures_data, next_gameweek_id, team_map):
    # analyzing fixtures difficulty
    fixtures_df = pd.DataFrame(fixtures_data)
    final_fixtures_df = fixtures_df[fixtures_df['event'] == next_gameweek_id][
        ['event', 'team_h', 'team_a', 'team_h_difficulty', 'team_a_difficulty']].copy()

    home_df = final_fixtures_df[['team_h', 'team_a', 'team_h_difficulty']].rename(
        columns={'team_h': 'team', 'team_a': 'opponent_team_id', 'team_h_difficulty': 'difficulty'})
    away_df = final_fixtures_df[['team_a', 'team_h', 'team_a_difficulty']].rename(
        columns={'team_a': 'team', 'team_h': 'opponent_team_id', 'team_a_difficulty': 'difficulty'})

    opponents_df = pd.concat([home_df, away_df], ignore_index=True)
    opponents_df['opponent_name'] = opponents_df['opponent_team_id'].map(team_map)
    return opponents_df


def filter_and_score_players(players_df, opponents_df, current_gw):
    # merging data and calculating the genius score
    merged_df = pd.merge(players_df, opponents_df, on='team', how='left')

    numeric_cols = ['form', 'ep_next', 'selected_by_percent', 'bps', 'minutes', 'transfers_in_event']
    for col in numeric_cols: merged_df[col] = pd.to_numeric(merged_df[col], errors='coerce')

    merged_df['points_per_game'] = merged_df['total_points'] / (current_gw + 0.001)

    # filtering junk players
    filtered_df = merged_df[
        (merged_df['status'] == 'a') & (merged_df['difficulty'].notna()) &
        (merged_df['minutes'] > (current_gw * MIN_AVG_MINUTES)) &
        (merged_df['points_per_game'] > MIN_PPG)
        ].copy()

    # algorithm
    total_score = (
            filtered_df['form'] * WEIGHTS['form'] +
            filtered_df['ep_next'] * WEIGHTS['ep_next'] +
            (6 - filtered_df['difficulty']) * 2.0 * WEIGHTS['difficulty'] +
            filtered_df['points_per_game'] * WEIGHTS['season_points']
    )

    filtered_df['genius_score'] = total_score / filtered_df['now_cost']
    return filtered_df.set_index('id').sort_values(by='genius_score', ascending=False)


def run_optimization(scored_players_df, budget):
    # running the solver
    player_indices = list(scored_players_df.index)
    prob = pulp.LpProblem("FPL_Team", pulp.LpMaximize)
    player_vars = pulp.LpVariable.dicts("player", player_indices, cat='Binary')

    prob += pulp.lpSum(scored_players_df.loc[i, 'genius_score'] * player_vars[i] for i in player_indices)
    prob += pulp.lpSum(scored_players_df.loc[i, 'now_cost'] * player_vars[i] for i in player_indices) <= budget
    prob += pulp.lpSum(player_vars[i] for i in player_indices) == 15

    # formation constraints
    pos_constraints = {'GKP': 2, 'DEF': 5, 'MID': 5, 'FWD': 3}
    for pos, count in pos_constraints.items():
        prob += pulp.lpSum(
            player_vars[i] for i in player_indices if scored_players_df.loc[i, 'position'] == pos) == count

    # maximum 3 per team
    for team_id in scored_players_df['team'].unique():
        prob += pulp.lpSum(player_vars[i] for i in player_indices if scored_players_df.loc[i, 'team'] == team_id) <= 3

    prob.solve()

    chosen_players = []
    for i in player_indices:
        if player_vars[i].varValue == 1:
            chosen_players.append(scored_players_df.loc[i])

    return pd.DataFrame(chosen_players)


def create_squad_image_streamlit(squad_df):
    # creates the pitch visualization
    try:
        img = Image.open("pitch.png")
        base_width = 800
        w_percent = (base_width / float(img.size[0]))
        h_size = int((float(img.size[1]) * float(w_percent)))
        img = img.resize((base_width, h_size), Image.Resampling.LANCZOS)

        draw = ImageDraw.Draw(img)
        font = ImageFont.truetype("font.ttf", 24)

        squad_df = squad_df.sort_values(by='genius_score', ascending=False)
        starters = {
            'GKP': squad_df[squad_df['position'] == 'GKP'].head(1),
            'DEF': squad_df[squad_df['position'] == 'DEF'].head(4),
            'MID': squad_df[squad_df['position'] == 'MID'].head(4),
            'FWD': squad_df[squad_df['position'] == 'FWD'].head(2)
        }

        for pos, players in starters.items():
            coords = FORMATION_COORDS[pos]
            for i, (index, player) in enumerate(players.iterrows()):
                if i < len(coords):
                    name = player['web_name']
                    x, y = coords[i]
                    draw.text((x - 1, y - 1), name, font=font, fill='black')
                    draw.text((x + 1, y - 1), name, font=font, fill='black')
                    draw.text((x - 1, y + 1), name, font=font, fill='black')
                    draw.text((x + 1, y + 1), name, font=font, fill='black')
                    draw.text(coords[i], name, font=font, fill='white')
        return img
    except Exception as e:
        st.error(f"Image error: {e}")
        return None


# --- UI Layout ---

st.sidebar.header("Settings ⚙️")
budget = st.sidebar.slider("Bank Budget (£m)", 80.0, 105.0, 100.0, 0.5)

st.write(
    f"Click the button below to generate the mathematically optimal squad for the upcoming gameweek, based on a budget of **£{budget}m**.")

if st.button("Generate Optimal Wildcard Team 🚀", type="primary"):
    with st.spinner('Crunching the numbers...'):
        player_data = fetch_data(PLAYER_URL)
        fixture_data = fetch_data(FIXTURES_URL)

    if player_data and fixture_data:
        team_map, position_map = create_lookup_maps(player_data)
        current_gw, next_gw = get_gameweek_ids(player_data)

        players_df = create_player_dataframe(player_data, team_map, position_map)
        opponents_df = create_opponent_dataframe(fixture_data, next_gw, team_map)
        scored_df = filter_and_score_players(players_df, opponents_df, current_gw)

        optimal_squad = run_optimization(scored_df, budget)

        # results
        st.success(f"**Optimization Complete for Gameweek {next_gw}!**")

        tab1, tab2 = st.tabs(["⚽ Pitch View", "📊 Data View"])

        with tab1:
            img = create_squad_image_streamlit(optimal_squad)
            if img:
                st.image(img, caption=f"Optimal XI (Budget: £{budget}m)", use_container_width=True)
            else:
                st.warning("Could not load pitch image. Check if 'pitch.png' is in the folder.")

        with tab2:
            st.dataframe(
                optimal_squad[['web_name', 'team_name', 'position', 'now_cost', 'genius_score', 'ep_next', 'form']])

            total_cost = optimal_squad['now_cost'].sum()
            total_score = optimal_squad['genius_score'].sum()
            st.metric(label="Total Squad Cost", value=f"£{total_cost:.1f}m")
            st.metric(label="Projected Genius Score", value=f"{total_score:.2f}")

    else:
        st.error("Error connecting to FPL API.")
