import os
from pathlib import Path
from dotenv import load_dotenv
from yfpy.query import YahooFantasySportsQuery

load_dotenv()

client_id = os.getenv("YAHOO_CLIENT_ID")
client_secret = os.getenv("YAHOO_CLIENT_SECRET")

if not client_id or not client_secret:
    raise ValueError("Missing Yahoo credentials in .env")

print("Creating Yahoo Fantasy API connection...")

yahoo_query = YahooFantasySportsQuery(
    auth_directory=Path("."),
    league_id=None,
    game_code="nba",
    offline=False,
    consumer_key=client_id,
    consumer_secret=client_secret
)

print("Getting NBA leagues...")

try:
    leagues = yahoo_query.get_user_leagues_by_game_key("nba")

    print("\nSUCCESS!")
    print(f"Found {len(leagues)} league(s)\n")

    for league in leagues:
        print(
            f"League ID: {league.league_id} | "
            f"Name: {league.name} | "
            f"Season: {league.season}"
        )

except Exception as e:
    print("\nYAHOO API ERROR:")
    print(e)