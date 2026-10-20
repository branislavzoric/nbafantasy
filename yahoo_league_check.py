"""
Probe the Yahoo Fantasy API for the personal NBA league.

Requires tokens.json (created by yahoo_auth.py). Refreshes the access
token automatically, then tests a set of endpoints (league, teams,
rosters, player stats, projections) and saves every raw response to
yahoo_probe/ so we can inspect what Yahoo actually returns.
"""

import os
import json
import time
from pathlib import Path

import requests
from dotenv import load_dotenv


# --------------------------------------------------
# Configuration
# --------------------------------------------------

load_dotenv()

CLIENT_ID = os.getenv("YAHOO_CLIENT_ID")
CLIENT_SECRET = os.getenv("YAHOO_CLIENT_SECRET")

TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
FANTASY_API = "https://fantasysports.yahooapis.com/fantasy/v2"

TOKENS_FILE = Path("tokens.json")
LEAGUE_KEY_FILE = Path("league_key.txt")
OUTPUT_DIR = Path("yahoo_probe")

TARGET_LEAGUE_ID = "37924"


# --------------------------------------------------
# Tokens
# --------------------------------------------------

def load_tokens():
    if not TOKENS_FILE.exists():
        raise SystemExit("tokens.json not found - run yahoo_auth.py first.")
    return json.loads(TOKENS_FILE.read_text(encoding="utf-8"))


def refresh_tokens(tokens):
    response = requests.post(
        TOKEN_URL,
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": "oob",
            "refresh_token": tokens["refresh_token"],
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if not response.ok:
        print("Token refresh failed:", response.status_code, response.text)
        raise SystemExit(1)

    new_tokens = response.json()
    # Yahoo does not always return a new refresh token
    new_tokens.setdefault("refresh_token", tokens["refresh_token"])
    new_tokens["obtained_at"] = int(time.time())
    TOKENS_FILE.write_text(json.dumps(new_tokens, indent=2), encoding="utf-8")
    return new_tokens


def get_access_token():
    tokens = load_tokens()
    obtained_at = tokens.get("obtained_at", 0)
    expires_in = int(tokens.get("expires_in", 3600))

    # Access tokens live 1 hour; refresh with a 5 minute margin
    if time.time() > obtained_at + expires_in - 300:
        print("Refreshing access token...")
        tokens = refresh_tokens(tokens)

    return tokens["access_token"]


# --------------------------------------------------
# API helper
# --------------------------------------------------

ACCESS_TOKEN = None


def api_get(path):
    response = requests.get(
        f"{FANTASY_API}/{path}",
        headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
        params={"format": "json"},
        timeout=30,
    )
    try:
        body = response.json()
    except ValueError:
        body = {"raw_text": response.text}
    return response.status_code, body


def probe(name, path):
    status, body = api_get(path)

    OUTPUT_DIR.mkdir(exist_ok=True)
    (OUTPUT_DIR / f"{name}.json").write_text(
        json.dumps(body, indent=2), encoding="utf-8"
    )

    label = "OK  " if status == 200 else "FAIL"
    print(f"[{label}] {status}  {name:<32} {path}")
    if status != 200:
        error = body.get("error", {}) if isinstance(body, dict) else {}
        print(f"          -> {error.get('description', str(body)[:200])}")
    return status, body


# --------------------------------------------------
# Helpers for Yahoo's nested list/dict structure
# --------------------------------------------------

def flatten(items):
    """Yahoo returns objects as a list of single-key dicts; merge them."""
    merged = {}
    for item in items:
        if isinstance(item, dict):
            merged.update(item)
        elif isinstance(item, list):
            merged.update(flatten(item))
    return merged


def find_league_key(leagues_body):
    games = leagues_body["fantasy_content"]["users"]["0"]["user"][1]["games"]
    for key, game_container in games.items():
        if key == "count":
            continue
        leagues = game_container["game"][1].get("leagues", {})
        for lkey, league_container in leagues.items():
            if lkey == "count":
                continue
            league = league_container["league"][0]
            if str(league["league_id"]) == TARGET_LEAGUE_ID:
                return league["league_key"]
    return None


def extract_player_keys(roster_body, limit=3):
    team = roster_body["fantasy_content"]["team"]
    players = team[1]["roster"]["0"]["players"]
    keys = []
    for key, player_container in players.items():
        if key == "count":
            continue
        info = flatten(player_container["player"][0])
        keys.append((info["player_key"], info["name"]["full"]))
        if len(keys) >= limit:
            break
    return keys


# --------------------------------------------------
# Main
# --------------------------------------------------

def main():
    global ACCESS_TOKEN
    ACCESS_TOKEN = get_access_token()

    print()
    print("=" * 70)
    print("1. USER / LEAGUE")
    print("=" * 70)

    _, leagues_body = probe(
        "user_nba_leagues", "users;use_login=1/games;game_keys=nba/leagues"
    )

    league_key = None
    if LEAGUE_KEY_FILE.exists():
        league_key = LEAGUE_KEY_FILE.read_text(encoding="utf-8").strip()
    if not league_key:
        try:
            league_key = find_league_key(leagues_body)
        except (KeyError, TypeError, IndexError):
            league_key = None
    if not league_key:
        raise SystemExit(f"League {TARGET_LEAGUE_ID} not found in your NBA leagues.")

    print(f"\nLeague key: {league_key}\n")

    probe("league_metadata", f"league/{league_key}/metadata")
    probe("league_settings", f"league/{league_key}/settings")
    probe("league_standings", f"league/{league_key}/standings")
    probe("league_teams", f"league/{league_key}/teams")

    print()
    print("=" * 70)
    print("2. MY TEAM / ROSTER")
    print("=" * 70)

    status, my_team_body = probe(
        "my_team", f"users;use_login=1/games;game_keys=nba/teams"
    )

    my_team_key = None
    try:
        teams = my_team_body["fantasy_content"]["users"]["0"]["user"][1]["games"]["0"]["game"][1]["teams"]
        for key, team_container in teams.items():
            if key == "count":
                continue
            info = flatten(team_container["team"][0])
            if info["team_key"].startswith(league_key):
                my_team_key = info["team_key"]
                print(f"\nMy team: {info['name']}  ({my_team_key})\n")
    except (KeyError, TypeError, IndexError):
        pass

    player_keys = []
    if my_team_key:
        _, roster_body = probe("my_roster", f"team/{my_team_key}/roster")
        try:
            player_keys = extract_player_keys(roster_body)
        except (KeyError, TypeError, IndexError):
            pass

    # Fall back to top available players if roster is empty (e.g. pre-draft)
    if not player_keys:
        _, avail_body = probe(
            "top_players", f"league/{league_key}/players;sort=AR;count=3"
        )
        try:
            players = avail_body["fantasy_content"]["league"][1]["players"]
            for key, pc in players.items():
                if key == "count":
                    continue
                info = flatten(pc["player"][0])
                player_keys.append((info["player_key"], info["name"]["full"]))
        except (KeyError, TypeError, IndexError):
            pass

    print("\nSample players:", ", ".join(name for _, name in player_keys) or "none")
    pk = ",".join(k for k, _ in player_keys)

    print()
    print("=" * 70)
    print("3. PLAYER STATS (actual)")
    print("=" * 70)

    probe("stats_season", f"league/{league_key}/players;player_keys={pk}/stats;type=season")
    probe("stats_lastmonth", f"league/{league_key}/players;player_keys={pk}/stats;type=lastmonth")
    probe("stats_average_season", f"league/{league_key}/players;player_keys={pk}/stats;type=average_season")

    print()
    print("=" * 70)
    print("4. PROJECTIONS (experimental - not officially documented)")
    print("=" * 70)

    projection_paths = {
        "proj_projected_season": "stats;type=projected_season",
        "proj_projected_next7": "stats;type=projected_next7days",
        "proj_projected_next14": "stats;type=projected_next14days",
        "proj_projected_week": "stats;type=projected_week",
        "proj_season_projected_flag": "stats;type=season;is_projected=1",
    }
    for name, suffix in projection_paths.items():
        probe(name, f"league/{league_key}/players;player_keys={pk}/{suffix}")

    print()
    print("=" * 70)
    print("5. RANKINGS / DRAFT DATA (projection-adjacent)")
    print("=" * 70)

    probe("draft_analysis", f"league/{league_key}/players;player_keys={pk}/draft_analysis")
    probe("percent_owned", f"league/{league_key}/players;player_keys={pk}/percent_owned")
    probe("players_sorted_by_projection", f"league/{league_key}/players;sort=PS;sort_type=season;count=5")

    print()
    print(f"All raw responses saved to {OUTPUT_DIR.resolve()}")


if __name__ == "__main__":
    main()
