#!/usr/bin/env python3
"""
collect_boxscores.py

Collects NBA box scores (preseason, regular season, playoffs) from ESPN's public
API and stores them in Postgres. Built to run every morning: with no arguments
it catches up on every day from the last stored game date (or SEASON_START)
through yesterday, so a missed morning is filled in by the next run.

(cdn.nba.com / stats.nba.com block scripted requests with 403, so ESPN is used.
Player ids are ESPN athlete ids, the same ids as espn_players.player_id.)

Output:
  - "Box Scores/<YYYY-MM-DD>/<game_id>.json"  raw ESPN summary JSON per game
  - Postgres table `nba_games`      one row per game
  - Postgres table `nba_boxscores`  one row per player per game, incl. Yahoo
                                    default fantasy points (fpts)
Rows are upserted, so re-running a date is safe. Tables are never dropped.

Usage:
  python collect_boxscores.py                       # catch up through yesterday
  python collect_boxscores.py 2026-10-05            # one date (US Eastern game date)
  python collect_boxscores.py 2026-10-03 2026-10-05 # a date range
"""

import datetime as dt
import json
import os
import sys
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from sqlalchemy import (Boolean, Column, Date, DateTime, Float, Integer, MetaData,
                        String, Table, create_engine, func, select)
from sqlalchemy.dialects.postgresql import insert

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# -----------------------------------------------------------------------------
# CONFIG
# -----------------------------------------------------------------------------

SEASON_START = dt.date(2026, 10, 1)  # first preseason games were 2026-10-03
DATABASE_URL = os.environ.get("DATABASE_URL")
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.join(PROJECT_DIR, "Box Scores")
ET = ZoneInfo("America/New_York")  # ESPN's ?dates= and NBA game dates are US Eastern

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary"

# Yahoo default points-league scoring.
FPTS_WEIGHTS = {"pts": 1.0, "reb": 1.2, "ast": 1.5, "stl": 3.0, "blk": 3.0, "tov": -1.0}

# ESPN box score key -> our column(s). "a-b" keys split into made/attempted.
STAT_KEYS = {
    "minutes": "min",
    "points": "pts",
    "fieldGoalsMade-fieldGoalsAttempted": ("fgm", "fga"),
    "threePointFieldGoalsMade-threePointFieldGoalsAttempted": ("tpm", "tpa"),
    "freeThrowsMade-freeThrowsAttempted": ("ftm", "fta"),
    "rebounds": "reb",
    "assists": "ast",
    "turnovers": "tov",
    "steals": "stl",
    "blocks": "blk",
    "offensiveRebounds": "oreb",
    "defensiveRebounds": "dreb",
    "fouls": "pf",
    "plusMinus": "plus_minus",
}
STAT_COLUMNS = ["min", "pts", "fgm", "fga", "tpm", "tpa", "ftm", "fta", "oreb", "dreb", "reb",
                "ast", "stl", "blk", "tov", "pf", "plus_minus"]

SEASON_TYPES = {1: "preseason", 2: "regular", 3: "playoffs", 5: "play-in"}

# -----------------------------------------------------------------------------
# TABLES
# -----------------------------------------------------------------------------

metadata = MetaData()

games_t = Table(
    "nba_games", metadata,
    Column("game_id", String, primary_key=True),          # ESPN event id
    Column("game_date", Date, nullable=False),             # US Eastern date
    Column("start_time", DateTime(timezone=True)),
    Column("season", Integer, nullable=False),             # 2027 = 2026-27
    Column("season_type", String, nullable=False),         # preseason / regular / playoffs
    Column("status", String, nullable=False),              # STATUS_FINAL, ...
    Column("home_team", String, nullable=False),
    Column("away_team", String, nullable=False),
    Column("home_score", Integer),
    Column("away_score", Integer),
    Column("fetched_at", DateTime, nullable=False),
)

boxscores_t = Table(
    "nba_boxscores", metadata,
    Column("game_id", String, primary_key=True),
    Column("player_id", String, primary_key=True),        # ESPN athlete id = espn_players.player_id
    Column("player_name", String, nullable=False),
    Column("team", String, nullable=False),
    Column("opponent", String, nullable=False),
    Column("game_date", Date, nullable=False),
    Column("season_type", String, nullable=False),
    Column("position", String),
    Column("starter", Boolean, nullable=False),
    Column("did_not_play", Boolean, nullable=False),
    Column("dnp_reason", String),
    *[Column(c, Integer) for c in STAT_COLUMNS],
    Column("fpts", Float),                                 # Yahoo default points
    Column("fetched_at", DateTime, nullable=False),
)

# -----------------------------------------------------------------------------
# FETCH + PARSE
# -----------------------------------------------------------------------------

session = requests.Session()


def get_json(url, params):
    r = session.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def games_on(day):
    """ESPN events for one US Eastern game date."""
    return get_json(SCOREBOARD_URL, {"dates": day.strftime("%Y%m%d")}).get("events", [])


def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def fantasy_points(line):
    if line["did_not_play"] or line["min"] is None:
        return None
    return round(sum(w * (line[c] or 0) for c, w in FPTS_WEIGHTS.items()), 1)


def parse_game(summary, day, now):
    comp = summary["header"]["competitions"][0]
    teams = {c["homeAway"]: c for c in comp["competitors"]}
    season = summary["header"]["season"]
    season_type = SEASON_TYPES.get(season["type"], str(season["type"]))
    game = {
        "game_id": summary["header"]["id"],
        "game_date": day,
        "start_time": dt.datetime.fromisoformat(comp["date"].replace("Z", "+00:00")),
        "season": season["year"],
        "season_type": season_type,
        "status": comp["status"]["type"]["name"],
        "home_team": teams["home"]["team"]["abbreviation"],
        "away_team": teams["away"]["team"]["abbreviation"],
        "home_score": to_int(teams["home"].get("score")),
        "away_score": to_int(teams["away"].get("score")),
        "fetched_at": now,
    }

    team_abbrs = [t["team"]["abbreviation"] for t in summary["boxscore"].get("players", [])]
    lines = []
    for team_block in summary["boxscore"].get("players", []):
        team = team_block["team"]["abbreviation"]
        opponent = next((a for a in team_abbrs if a != team), "")
        for stat_block in team_block["statistics"]:
            keys = stat_block["keys"]
            for a in stat_block["athletes"]:
                line = {
                    "game_id": game["game_id"],
                    "player_id": a["athlete"]["id"],
                    "player_name": a["athlete"]["displayName"],
                    "team": team,
                    "opponent": opponent,
                    "game_date": day,
                    "season_type": season_type,
                    "position": (a["athlete"].get("position") or {}).get("abbreviation"),
                    "starter": bool(a.get("starter")),
                    "did_not_play": bool(a.get("didNotPlay")) or not a.get("stats"),
                    "dnp_reason": a.get("reason") if a.get("didNotPlay") else None,
                    **{c: None for c in STAT_COLUMNS},
                    "fetched_at": now,
                }
                for key, value in zip(keys, a.get("stats") or []):
                    col = STAT_KEYS.get(key)
                    if isinstance(col, tuple):
                        made, _, att = value.partition("-")
                        line[col[0]], line[col[1]] = to_int(made), to_int(att)
                    elif col:
                        line[col] = to_int(value.lstrip("+"))
                line["fpts"] = fantasy_points(line)
                lines.append(line)
    return game, lines


def save_raw(summary, day):
    folder = os.path.join(RAW_DIR, day.isoformat())
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, f"{summary['header']['id']}.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f)

# -----------------------------------------------------------------------------
# DB
# -----------------------------------------------------------------------------

def upsert(conn, table, rows, keys):
    if not rows:
        return
    stmt = insert(table).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=keys,
        set_={c.name: stmt.excluded[c.name] for c in table.columns if c.name not in keys},
    )
    conn.execute(stmt)


def final_game_ids(conn, day):
    rows = conn.execute(select(games_t.c.game_id).where(
        games_t.c.game_date == day, games_t.c.status == "STATUS_FINAL"))
    return {r[0] for r in rows}


def default_range(engine):
    """From the last stored game date (re-checked in case it was incomplete)
    or SEASON_START, through yesterday (US Eastern)."""
    with engine.connect() as conn:
        last = conn.execute(select(func.max(games_t.c.game_date))).scalar()
    yesterday = dt.datetime.now(ET).date() - dt.timedelta(days=1)
    return last or SEASON_START, yesterday

# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------

def parse_args(engine):
    args = sys.argv[1:]
    if not args:
        return default_range(engine)
    start = dt.date.fromisoformat(args[0])
    end = dt.date.fromisoformat(args[1]) if len(args) > 1 else start
    return start, end


def main():
    if not DATABASE_URL:
        raise SystemExit("DATABASE_URL not set in .env")
    engine = create_engine(DATABASE_URL)
    metadata.create_all(engine)

    start, end = parse_args(engine)
    print(f"Collecting box scores {start} .. {end}")
    total_games = total_lines = 0
    day = start
    while day <= end:
        events = games_on(day)
        with engine.begin() as conn:
            already_final = final_game_ids(conn, day)
            for event in events:
                name = event["shortName"]
                if event["id"] in already_final:
                    print(f"  {day}  {name:12} already stored")
                    continue
                if event["status"]["type"]["name"] != "STATUS_FINAL":
                    print(f"  {day}  {name:12} {event['status']['type']['name']} -- skipped")
                    continue
                summary = get_json(SUMMARY_URL, {"event": event["id"]})
                save_raw(summary, day)
                game, lines = parse_game(summary, day, dt.datetime.now(dt.timezone.utc).replace(tzinfo=None))
                upsert(conn, games_t, [game], ["game_id"])
                upsert(conn, boxscores_t, lines, ["game_id", "player_id"])
                played = sum(not l["did_not_play"] for l in lines)
                print(f"  {day}  {name:12} {game['away_score']}-{game['home_score']}  "
                      f"{played} players")
                total_games += 1
                total_lines += len(lines)
        if not events:
            print(f"  {day}  no games")
        day += dt.timedelta(days=1)

    print(f"Saved {total_games} games, {total_lines} player lines to nba_games / nba_boxscores.")


if __name__ == "__main__":
    main()
