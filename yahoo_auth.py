import os
import json
import webbrowser
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv


# --------------------------------------------------
# Configuration
# --------------------------------------------------

load_dotenv()

CLIENT_ID = os.getenv("YAHOO_CLIENT_ID")
CLIENT_SECRET = os.getenv("YAHOO_CLIENT_SECRET")

if not CLIENT_ID or not CLIENT_SECRET:
    raise RuntimeError(
        "Missing YAHOO_CLIENT_ID or YAHOO_CLIENT_SECRET in .env"
    )

REDIRECT_URI = "oob"

AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"

FANTASY_API = "https://fantasysports.yahooapis.com/fantasy/v2"

TARGET_LEAGUE_ID = "37924"
TARGET_LEAGUE_NAME = "trebevicka23"


# --------------------------------------------------
# Step 1 - Build Yahoo authorization URL
# --------------------------------------------------

params = {
    "client_id": CLIENT_ID,
    "redirect_uri": REDIRECT_URI,
    "response_type": "code",
    "scope": "fspt-r",
    "language": "en-us",
    "prompt": "consent",
}

authorization_url = AUTH_URL + "?" + urlencode(params)

print()
print("=" * 70)
print("YAHOO AUTHORIZATION")
print("=" * 70)
print()
print("Opening Yahoo authorization page in your browser...")
print()

webbrowser.open(authorization_url)

print("If the browser did not open, copy/paste this URL:")
print()
print(authorization_url)
print()

input("Press ENTER after you have authorized the application...")

print()
print("Yahoo should have shown you an authorization code.")
print()

authorization_code = input("Paste the authorization code here: ").strip()

if not authorization_code:
    raise RuntimeError("No authorization code entered.")


# --------------------------------------------------
# Step 2 - Exchange authorization code for tokens
# --------------------------------------------------

print()
print("Exchanging authorization code for access token...")

token_response = requests.post(
    TOKEN_URL,
    data={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "code": authorization_code,
        "grant_type": "authorization_code",
    },
    timeout=30,
)

if not token_response.ok:
    print()
    print("Yahoo token request failed.")
    print("HTTP status:", token_response.status_code)
    print("Response:", token_response.text)
    raise SystemExit(1)

tokens = token_response.json()

access_token = tokens.get("access_token")
refresh_token = tokens.get("refresh_token")

if not access_token:
    print("No access token received.")
    print(tokens)
    raise SystemExit(1)


# Save tokens locally
with open("tokens.json", "w", encoding="utf-8") as f:
    json.dump(tokens, f, indent=2)

print("Authentication successful.")
print("Tokens saved to tokens.json")


# --------------------------------------------------
# Step 3 - Ask Yahoo for user's NBA leagues
# --------------------------------------------------

print()
print("=" * 70)
print("SEARCHING FOR YOUR NBA LEAGUES")
print("=" * 70)
print()

headers = {
    "Authorization": f"Bearer {access_token}"
}

league_url = (
    f"{FANTASY_API}/users;use_login=1/"
    f"games;game_keys=nba/leagues"
)

response = requests.get(
    league_url,
    headers=headers,
    params={"format": "json"},
    timeout=30,
)

if not response.ok:
    print("Yahoo Fantasy API request failed.")
    print("HTTP status:", response.status_code)
    print("Response:")
    print(response.text)
    raise SystemExit(1)

data = response.json()


# --------------------------------------------------
# Step 4 - Save raw response for debugging
# --------------------------------------------------

with open("nba_leagues_raw.json", "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)

print("Yahoo response saved to nba_leagues_raw.json")
print()


# --------------------------------------------------
# Step 5 - Extract leagues
# --------------------------------------------------

try:
    users = data["fantasy_content"]["users"]

    # Yahoo sometimes returns a dictionary containing
    # numeric keys and sometimes a slightly different
    # structure depending on the number of results.

    user = users["0"]["user"]

    games = user[1]["games"]

except (KeyError, TypeError, IndexError) as e:
    print("Could not understand Yahoo's response structure.")
    print("Error:", e)
    print()
    print("The complete response was saved to:")
    print("nba_leagues_raw.json")
    raise SystemExit(1)


# --------------------------------------------------
# Step 6 - Find target league
# --------------------------------------------------

found = False

print("Your NBA leagues:")
print("-" * 70)

for game_index, game_container in games.items():

    if game_index == "count":
        continue

    try:
        game = game_container["game"]
        game_key = str(game[0]["game_key"])
        game_name = game[0].get("name", "")
        season = game[0].get("season", "")

        leagues_container = game[1].get("leagues", {})

    except (KeyError, TypeError, IndexError):
        continue

    if not leagues_container:
        continue

    for league_index, league_container in leagues_container.items():

        if league_index == "count":
            continue

        try:
            league = league_container["league"]

            league_key = str(league[0]["league_key"])
            league_id = str(league[0]["league_id"])
            league_name = league[0].get("name", "")

        except (KeyError, TypeError, IndexError):
            continue

        print()
        print("League:", league_name)
        print("League ID:", league_id)
        print("Game:", game_name)
        print("Season:", season)
        print("Game Key:", game_key)
        print("League Key:", league_key)

        if (
            league_id == TARGET_LEAGUE_ID
            or league_name.lower() == TARGET_LEAGUE_NAME.lower()
        ):
            found = True

            print()
            print("=" * 70)
            print("TARGET LEAGUE FOUND")
            print("=" * 70)
            print()
            print("League Name :", league_name)
            print("League ID   :", league_id)
            print("Game Key    :", game_key)
            print("League Key  :", league_key)
            print()

            with open("league_key.txt", "w", encoding="utf-8") as f:
                f.write(league_key)

            print("League key saved to league_key.txt")
            print()

if not found:
    print()
    print("=" * 70)
    print("TARGET LEAGUE NOT FOUND")
    print("=" * 70)
    print()
    print("Expected:")
    print("Name:", TARGET_LEAGUE_NAME)
    print("ID:", TARGET_LEAGUE_ID)
    print()
    print("Check nba_leagues_raw.json for the complete Yahoo response.")