#!/usr/bin/env python3
"""
espn_nba_projections.py

Pulls, for every player on ESPN's Fantasy Basketball projections page
(https://fantasy.espn.com/basketball/players/projections?leagueFormatId=2),
the same two lines the page shows under each player:

  - "2026 Statistics"  -> actual per-game averages from the 2025-26 season
  - "2027 Projections" -> ESPN's projected per-game averages for 2026-27

Output:
  - espn_players_2026_2027.csv  (always; two rows per player, one line under the other)
  - headshots/<player_id>.png   (ESPN headshot per player; already-downloaded ones are skipped)
  - Postgres tables `espn_players` + `espn_player_lines` (when DATABASE_URL is set).
    Both are rebuilt on every run, so they always hold the latest snapshot.
    espn_players.headshot_path points at the local file, headshot_url at ESPN's copy.

-----------------------------------------------------------------------------
SETUP (Windows / PowerShell)
-----------------------------------------------------------------------------
1. venv\\Scripts\\pip install -r requirements.txt
2. Optional, for Postgres: add to .env
     DATABASE_URL=postgresql+psycopg2://postgres:PASSWORD@localhost:5432/nba_fantasy
3. venv\\Scripts\\python espn_nba_projections.py

NOTE: ESPN numbers seasons by the year they END in. Season 2027 = 2026-27.
The season-2027 endpoint returns both stat blocks we need:
  "002026" = actual 2025-26 stats, "102027" = projected 2026-27 stats.
"""

import csv
import datetime as dt
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# -----------------------------------------------------------------------------
# CONFIG
# -----------------------------------------------------------------------------

SEASON = int(os.environ.get("ESPN_SEASON", "2027"))  # upcoming season (ends 2027)
LEAGUE_FORMAT_ID = int(os.environ.get("ESPN_LEAGUE_FORMAT_ID", "2"))
DATABASE_URL = os.environ.get("DATABASE_URL")
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(PROJECT_DIR, f"espn_players_{SEASON - 1}_{SEASON}.csv")
HEADSHOT_DIR = os.path.join(PROJECT_DIR, "headshots")
HEADSHOT_URL = "https://a.espncdn.com/i/headshots/nba/players/full/{player_id}.png"

API_URL = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/"
           f"seasons/{SEASON}/segments/0/leaguedefaults/{LEAGUE_FORMAT_ID}")
PAGE_SIZE = 250

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json",
    "x-fantasy-source": "kona",
    "x-fantasy-platform": "kona-PROD-fba",
}

# (stat block id, line label, line type) in display order.
LINES = [
    (f"00{SEASON - 1}", f"{SEASON - 1} Statistics", "actual"),
    (f"10{SEASON}", f"{SEASON} Projections", "projected"),
]

# Output column -> ESPN stat id.
STAT_COLUMNS = [
    ("gp", "42"), ("min", "40"),
    ("fgm", "13"), ("fga", "14"), ("fg_pct", "19"),
    ("ftm", "15"), ("fta", "16"), ("ft_pct", "20"),
    ("tpm", "17"), ("tpa", "18"),
    ("reb", "6"), ("ast", "3"), ("stl", "2"), ("blk", "1"), ("tov", "11"),
    ("pts", "0"),
]
PCT_COLUMNS = {"fg_pct", "ft_pct"}

POSITIONS = {1: "PG", 2: "SG", 3: "SF", 4: "PF", 5: "C"}  # defaultPositionId
SLOT_POSITIONS = {0: "PG", 1: "SG", 2: "SF", 3: "PF", 4: "C"}  # eligibleSlots

PRO_TEAMS = {
    0: "FA", 1: "ATL", 2: "BOS", 3: "NOP", 4: "CHI", 5: "CLE", 6: "DAL",
    7: "DEN", 8: "DET", 9: "GSW", 10: "HOU", 11: "IND", 12: "LAC",
    13: "LAL", 14: "MIA", 15: "MIL", 16: "MIN", 17: "BKN", 18: "NYK",
    19: "ORL", 20: "PHI", 21: "PHX", 22: "POR", 23: "SAC", 24: "SAS",
    25: "OKC", 26: "UTA", 27: "WAS", 28: "TOR", 29: "MEM", 30: "CHA",
}

# -----------------------------------------------------------------------------
# FETCH
# -----------------------------------------------------------------------------

def fetch_all_players():
    """Pages through ESPN's kona_player_info view, sorted like the website."""
    session = requests.Session()
    session.headers.update(HEADERS)
    players, offset = [], 0

    while True:
        fantasy_filter = {"players": {
            "limit": PAGE_SIZE,
            "offset": offset,
            "sortDraftRanks": {"sortPriority": 100, "sortAsc": True, "value": "STANDARD"},
            "filterStatsForTopScoringPeriodIds": {
                "value": 2, "additionalValue": [block_id for block_id, _, _ in LINES],
            },
        }}
        headers = {"x-fantasy-filter": json.dumps(fantasy_filter)}

        for attempt in range(6):
            resp = session.get(API_URL, params={"view": "kona_player_info"},
                               headers=headers, timeout=30)
            if resp.status_code != 202:  # 202 = ESPN cache warming
                break
            time.sleep(2 * (attempt + 1))
        if resp.status_code != 200:
            raise RuntimeError(f"ESPN API returned {resp.status_code} at offset {offset}: "
                               f"{resp.text[:300]}")

        page = resp.json().get("players", [])
        players.extend(page)
        print(f"  fetched {len(page)} players (total {len(players)})")
        if len(page) < PAGE_SIZE:
            return players
        offset += PAGE_SIZE
        time.sleep(0.5)

# -----------------------------------------------------------------------------
# PARSE
# -----------------------------------------------------------------------------

def parse_line(block):
    """Per-game averages for one stat block, rounded like the website."""
    avg = block.get("averageStats") or {}
    totals = block.get("stats") or {}
    line = {}
    for col, stat_id in STAT_COLUMNS:
        value = totals.get(stat_id) if col == "gp" else avg.get(stat_id)
        if value is None:
            line[col] = None
        elif col == "gp":
            line[col] = int(value)
        elif col in PCT_COLUMNS:
            line[col] = round(value, 3)
        else:
            line[col] = round(value, 1)
    line["fpts_total"] = round(block.get("appliedTotal") or 0, 1)
    line["fpts_per_game"] = round(block.get("appliedAverage") or 0, 1)
    return line


def parse_player(entry):
    """Returns the player's identity plus the lines ESPN has for them, or
    None if they have neither a 2026 stat line nor a 2027 projection."""
    p = entry["player"]
    blocks = {b.get("id"): b for b in p.get("stats", [])}

    lines = []
    for block_id, label, line_type in LINES:
        block = blocks.get(block_id)
        if block and block.get("averageStats"):
            lines.append({"label": label, "line_type": line_type,
                          "season": int(block_id[2:]), **parse_line(block)})
    if not lines:
        return None

    primary = POSITIONS.get(p.get("defaultPositionId"))
    eligible = [SLOT_POSITIONS[s] for s in p.get("eligibleSlots", []) if s in SLOT_POSITIONS]
    positions = [primary] + [pos for pos in eligible if pos != primary] if primary else eligible
    rank = (p.get("draftRanksByRankType") or {}).get("STANDARD", {}).get("rank")

    return {
        "player_id": p["id"],
        "espn_rank": rank,
        "full_name": p.get("fullName", ""),
        "pro_team": PRO_TEAMS.get(p.get("proTeamId"), str(p.get("proTeamId"))),
        "positions": positions,
        "injury_status": p.get("injuryStatus"),
        "lines": lines,
    }

# -----------------------------------------------------------------------------
# HEADSHOTS
# -----------------------------------------------------------------------------

def download_headshot(session, player):
    """Saves headshots/<player_id>.png unless it's already there. Sets the
    player's headshot_url/headshot_path, or leaves them None if ESPN has no photo."""
    player["headshot_url"] = player["headshot_path"] = None
    filename = f"{player['player_id']}.png"
    path = os.path.join(HEADSHOT_DIR, filename)
    url = HEADSHOT_URL.format(player_id=player["player_id"])

    if not os.path.exists(path):
        resp = session.get(url, timeout=30)
        if resp.status_code != 200 or not resp.headers.get("Content-Type", "").startswith("image/"):
            return "missing"
        with open(path, "wb") as f:
            f.write(resp.content)
        status = "downloaded"
    else:
        status = "cached"

    player["headshot_url"] = url
    player["headshot_path"] = f"headshots/{filename}"
    return status


def download_headshots(players):
    os.makedirs(HEADSHOT_DIR, exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = HEADERS["User-Agent"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(lambda p: download_headshot(session, p), players))
    return {s: statuses.count(s) for s in ("downloaded", "cached", "missing")}

# -----------------------------------------------------------------------------
# SAVE
# -----------------------------------------------------------------------------

CSV_STAT_HEADERS = ["GP", "MIN", "FGM", "FGA", "FG%", "FTM", "FTA", "FT%",
                    "3PM", "3PA", "REB", "AST", "STL", "BLK", "TO", "PTS", "FPTS", "FPTS/G"]


def save_csv(players):
    # utf-8-sig so Excel opens it with the right encoding
    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Rank", "Player", "Team", "Pos", "Status", "Line"] + CSV_STAT_HEADERS)
        for pl in players:
            for i, line in enumerate(pl["lines"]):
                ident = ([pl["espn_rank"] or "", pl["full_name"], pl["pro_team"],
                          "/".join(pl["positions"]), pl["injury_status"] or ""]
                         if i == 0 else ["", "", "", "", ""])
                stats = [line[c] for c, _ in STAT_COLUMNS] + [line["fpts_total"], line["fpts_per_game"]]
                w.writerow(ident + [line["label"]] + ["" if v is None else v for v in stats])


def save_db(players, now):
    from sqlalchemy import (BigInteger, Column, DateTime, Float, ForeignKey, Integer, JSON,
                            MetaData, String, Table, Text, UniqueConstraint, create_engine)
    from sqlalchemy.dialects.postgresql import insert

    metadata = MetaData()
    players_t = Table(
        "espn_players", metadata,
        Column("player_id", BigInteger, primary_key=True),
        Column("full_name", Text, nullable=False),
        Column("espn_rank", Integer),
        Column("pro_team", String(8)),
        Column("positions", JSON),
        Column("injury_status", String(32)),
        Column("headshot_url", Text),
        Column("headshot_path", Text),  # relative to the project folder
        Column("updated_at", DateTime, nullable=False),
    )
    lines_t = Table(
        "espn_player_lines", metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("player_id", BigInteger, ForeignKey("espn_players.player_id"), nullable=False),
        Column("season", Integer, nullable=False),
        Column("line_type", String(16), nullable=False),  # actual | projected
        *[Column(c, Integer if c == "gp" else Float) for c, _ in STAT_COLUMNS],
        Column("fpts_total", Float),
        Column("fpts_per_game", Float),
        Column("fetched_at", DateTime, nullable=False),
        UniqueConstraint("player_id", "season", "line_type"),
    )

    engine = create_engine(DATABASE_URL)
    with engine.begin() as conn:
        metadata.drop_all(conn)
        metadata.create_all(conn)
        conn.execute(insert(players_t), [{
            "player_id": pl["player_id"], "full_name": pl["full_name"],
            "espn_rank": pl["espn_rank"], "pro_team": pl["pro_team"],
            "positions": pl["positions"], "injury_status": pl["injury_status"],
            "headshot_url": pl["headshot_url"], "headshot_path": pl["headshot_path"],
            "updated_at": now,
        } for pl in players])
        conn.execute(insert(lines_t), [{
            "player_id": pl["player_id"], "season": line["season"],
            "line_type": line["line_type"],
            **{c: line[c] for c, _ in STAT_COLUMNS},
            "fpts_total": line["fpts_total"], "fpts_per_game": line["fpts_per_game"], "fetched_at": now,
        } for pl in players for line in pl["lines"]])

# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------

def print_preview(players, n=5):
    cols = ["gp", "min", "fg_pct", "ft_pct", "tpm", "reb", "ast", "stl", "blk", "tov", "pts", "fpts_per_game"]
    headers = ["GP", "MIN", "FG%", "FT%", "3PM", "REB", "AST", "STL", "BLK", "TO", "PTS", "FPTS/G"]
    print(f"\n{'':18}" + "".join(f"{h:>8}" for h in headers))
    for pl in players[:n]:
        print(f"{pl['espn_rank'] or '-'}. {pl['full_name']} ({pl['pro_team']}, {'/'.join(pl['positions'])})")
        for line in pl["lines"]:
            print(f"  {line['label']:16}" + "".join(
                f"{'' if line[c] is None else line[c]:>8}" for c in cols))


def main():
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    print(f"Fetching ESPN players for {SEASON - 1}-{str(SEASON)[-2:]} (leagueFormatId={LEAGUE_FORMAT_ID})...")
    entries = fetch_all_players()

    players = [p for p in map(parse_player, entries) if p]
    players.sort(key=lambda p: (p["espn_rank"] is None, p["espn_rank"] or 0,
                                -max(l["fpts_per_game"] for l in p["lines"])))
    n_actual = sum(any(l["line_type"] == "actual" for l in p["lines"]) for p in players)
    n_proj = sum(any(l["line_type"] == "projected" for l in p["lines"]) for p in players)
    print(f"{len(players)} players with data ({n_actual} with {SEASON - 1} stats, "
          f"{n_proj} with {SEASON} projections).")

    save_csv(players)
    print(f"Saved {CSV_PATH}")

    print("Downloading headshots...")
    counts = download_headshots(players)
    print(f"Headshots in {HEADSHOT_DIR}: {counts['downloaded']} downloaded, "
          f"{counts['cached']} already there, {counts['missing']} not available on ESPN.")

    if DATABASE_URL:
        save_db(players, now)
        print("Saved to Postgres: espn_players, espn_player_lines")
    else:
        print("DATABASE_URL not set -- skipped Postgres (add it to .env to enable).")

    print_preview(players)


if __name__ == "__main__":
    main()
