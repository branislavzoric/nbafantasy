#!/usr/bin/env python3
"""
yahoo_projections.py

Reads Yahoo Fantasy Basketball "Projected Season (total)" player pages that
were saved from the browser (Ctrl+S -> "Webpage, Complete") into
"Yahoo Snapshots/Page*.html", and stores Yahoo's projected season totals.

Yahoo's page does not show minutes or age, so for every Yahoo player we find
the matching ESPN player (already loaded by espn_nba_projections.py) and take:
  - mpg        -> ESPN's 2027 projected minutes per game (espn_player_lines)
  - birth_date -> ESPN athlete API, used to compute age

Output:
  - yahoo_projections_2026_2027.csv
  - Postgres table `yahoo_projections` (rebuilt on every run, one row per player).
    espn_player_id links a row to espn_players when a match was found.

Run espn_nba_projections.py first so espn_players / espn_player_lines exist.

NOTE: stats are season TOTALS (ESPN's table holds per-game averages), except
mpg and fpts_per_game. Yahoo's points-league view has no 3PM / 3PA columns, so
tpm and tpa stay empty until a snapshot that includes them is saved.
"""

import csv
import datetime as dt
import glob
import os
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# -----------------------------------------------------------------------------
# CONFIG
# -----------------------------------------------------------------------------

SEASON = 2027  # 2026-27, numbered by the year it ends in (same as the ESPN script)
DATABASE_URL = os.environ.get("DATABASE_URL")
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_GLOB = os.path.join(PROJECT_DIR, "Yahoo Snapshots", "Page*.html")
CSV_PATH = os.path.join(PROJECT_DIR, f"yahoo_projections_{SEASON - 1}_{SEASON}.csv")
ESPN_ATHLETE_URL = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/athletes/{player_id}"

# Output column -> Yahoo column header.
YAHOO_COLUMNS = [
    ("gp", "GP*"),
    ("fgm", "FGM"), ("fga", "FGA"),
    ("ftm", "FTM"), ("fta", "FTA"),
    ("reb", "REB"), ("ast", "AST"), ("stl", "ST"), ("blk", "BLK"), ("tov", "TO"),
    ("pf", "PF"), ("pts", "PTS"),
    ("fpts_total", "Fan Pts"),
    ("yahoo_preseason_rank", "Pre-Season"),
    ("yahoo_rank", "Current"),
]
FLOAT_COLUMNS = {"fpts_total"}

# Stat columns in ESPN's order (tpm/tpa are not on Yahoo's page).
STAT_COLUMNS = ["gp", "mpg", "fgm", "fga", "fg_pct", "ftm", "fta", "ft_pct", "tpm", "tpa",
                "reb", "ast", "stl", "blk", "tov", "pf", "pts", "fpts_total", "fpts_per_game"]

# Yahoo name -> ESPN name, for players whose names differ beyond accents/suffixes.
NAME_ALIASES = {}

# -----------------------------------------------------------------------------
# PARSE YAHOO HTML
# -----------------------------------------------------------------------------

class YahooPlayersPage(HTMLParser):
    """Collects the rows of the first table (the players table) as text cells,
    plus Yahoo id / name / team-position / injury tag from each player cell."""

    def __init__(self):
        super().__init__()
        self.table_depth = 0
        self.tables_seen = 0
        self.rows = []      # list of (cells, player_info)
        self.row = None
        self.cell = None
        self.info = None
        self.capture = None  # which player_info field the current text belongs to

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "table":
            self.table_depth += 1
            self.tables_seen += 1
        if self.tables_seen != 1:
            return
        if tag == "tr":
            self.row, self.info = [], {}
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []
        elif tag == "a" and "name" in cls.split() and "data-ys-playerid" in a:
            self.info["yahoo_player_id"] = int(a["data-ys-playerid"])
            self.info["full_name"] = a.get("title", "")
        elif tag == "span" and "F-injury" in cls:
            self.capture = "injury_status"
        elif tag == "span" and cls == "Fz-xxs" and "yahoo_player_id" in self.info:
            self.capture = "team_pos"

    def handle_endtag(self, tag):
        if tag == "table":
            self.table_depth -= 1
        if self.tables_seen != 1:
            return
        if tag in ("td", "th") and self.cell is not None:
            self.row.append(" ".join(self.cell))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append((self.row, self.info))
            self.row = None
        elif tag == "span" and self.capture:
            self.capture = None

    def handle_data(self, data):
        text = data.strip()
        if not text:
            return
        if self.cell is not None:
            self.cell.append(text)
        if self.capture and self.capture not in self.info:
            self.info[self.capture] = text


def to_number(text, as_float=False):
    text = text.replace(",", "").strip()
    if text in ("", "-"):
        return None
    return float(text) if as_float else int(text)


def parse_page(path):
    page = YahooPlayersPage()
    with open(path, encoding="utf-8") as f:
        page.feed(f.read())

    header = next(cells for cells, _ in page.rows if "Players" in cells)
    # Header cells can carry icon glyphs (e.g. "Fan Pts "); match on the text part.
    index = {}
    for i, name in enumerate(header):
        clean = re.sub(r"[^\x20-\x7e]", "", name).strip()
        index.setdefault(clean, i)
    missing = [h for _, h in YAHOO_COLUMNS if h not in index]
    if missing:
        raise RuntimeError(f"{os.path.basename(path)}: columns not found: {missing}")

    players = []
    for cells, info in page.rows:
        if "yahoo_player_id" not in info:
            continue
        team, _, positions = info.get("team_pos", "").partition(" - ")
        player = {
            "yahoo_player_id": info["yahoo_player_id"],
            "full_name": info["full_name"],
            "pro_team": team.strip(),
            "positions": [p for p in positions.strip().split(",") if p],
            "injury_status": info.get("injury_status"),
        }
        for col, header_name in YAHOO_COLUMNS:
            player[col] = to_number(cells[index[header_name]], col in FLOAT_COLUMNS)
        players.append(player)
    return players


def add_derived_stats(p):
    p["fg_pct"] = round(p["fgm"] / p["fga"], 3) if p["fga"] else None
    p["ft_pct"] = round(p["ftm"] / p["fta"], 3) if p["fta"] else None
    p["fpts_per_game"] = round(p["fpts_total"] / p["gp"], 1) if p["gp"] else None
    p["tpm"] = p["tpa"] = None

# -----------------------------------------------------------------------------
# MATCH TO ESPN (minutes + age)
# -----------------------------------------------------------------------------

def normalize_name(name):
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"[.'`]", "", name.lower()).replace("-", " ")
    name = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", name)
    return " ".join(name.split())


def load_espn_players(engine):
    from sqlalchemy import text
    query = text("""
        SELECT p.player_id, p.full_name, p.pro_team, l.min
        FROM espn_players p
        LEFT JOIN espn_player_lines l
          ON l.player_id = p.player_id AND l.line_type = 'projected' AND l.season = :season
    """)
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(query, {"season": SEASON})]


def match_espn(players, espn_players):
    by_name = {}
    for e in espn_players:
        by_name.setdefault(normalize_name(e["full_name"]), []).append(e)

    unmatched = []
    for p in players:
        name = NAME_ALIASES.get(p["full_name"], p["full_name"])
        candidates = by_name.get(normalize_name(name), [])
        if len(candidates) > 1:
            candidates = [e for e in candidates if e["pro_team"] == p["pro_team"]] or candidates
        match = candidates[0] if len(candidates) == 1 else None

        p["espn_player_id"] = match["player_id"] if match else None
        p["mpg"] = round(match["min"], 1) if match and match["min"] is not None else None
        if not match:
            unmatched.append(p["full_name"])
    return unmatched


def fetch_birth_date(session, espn_player_id):
    resp = session.get(ESPN_ATHLETE_URL.format(player_id=espn_player_id), timeout=30)
    if resp.status_code != 200:
        return None
    dob = resp.json().get("dateOfBirth")
    return dt.date.fromisoformat(dob[:10]) if dob else None


def add_ages(players, today):
    session = requests.Session()
    ids = [p["espn_player_id"] for p in players if p["espn_player_id"]]
    with ThreadPoolExecutor(max_workers=8) as pool:
        birth_dates = dict(zip(ids, pool.map(lambda i: fetch_birth_date(session, i), ids)))

    for p in players:
        born = birth_dates.get(p["espn_player_id"])
        p["birth_date"] = born
        p["age"] = (today.year - born.year - ((today.month, today.day) < (born.month, born.day))
                    if born else None)

# -----------------------------------------------------------------------------
# SAVE
# -----------------------------------------------------------------------------

CSV_HEADERS = ["Rank", "Pre-Season Rank", "Player", "Team", "Pos", "Status", "Age",
               "GP", "MPG", "FGM", "FGA", "FG%", "FTM", "FTA", "FT%", "3PM", "3PA",
               "REB", "AST", "STL", "BLK", "TO", "PF", "PTS", "FPTS", "FPTS/G"]


def save_csv(players):
    # utf-8-sig so Excel opens it with the right encoding
    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADERS)
        for p in players:
            row = [p["yahoo_rank"], p["yahoo_preseason_rank"], p["full_name"], p["pro_team"],
                   "/".join(p["positions"]), p["injury_status"], p["age"]]
            row += [p[c] for c in STAT_COLUMNS]
            w.writerow(["" if v is None else v for v in row])


def save_db(engine, players, now):
    from sqlalchemy import (BigInteger, Column, Date, DateTime, Float, Integer, JSON,
                            MetaData, String, Table, Text)
    from sqlalchemy.dialects.postgresql import insert

    metadata = MetaData()
    table = Table(
        "yahoo_projections", metadata,
        Column("yahoo_player_id", BigInteger, primary_key=True),
        Column("espn_player_id", BigInteger),  # espn_players.player_id, NULL if not matched
        Column("season", Integer, nullable=False),
        Column("full_name", Text, nullable=False),
        Column("yahoo_rank", Integer),            # rank by projected fantasy points
        Column("yahoo_preseason_rank", Integer),
        Column("pro_team", String(8)),
        Column("positions", JSON),
        Column("injury_status", String(32)),
        Column("birth_date", Date),
        Column("age", Integer),
        *[Column(c, Float if c in ("mpg", "fg_pct", "ft_pct", "fpts_total", "fpts_per_game")
                 else Integer) for c in STAT_COLUMNS],
        Column("snapshot_at", DateTime, nullable=False),
    )

    with engine.begin() as conn:
        metadata.drop_all(conn)
        metadata.create_all(conn)
        conn.execute(insert(table), [{
            **{c.name: p.get(c.name) for c in table.columns
               if c.name not in ("season", "snapshot_at")},
            "season": SEASON, "snapshot_at": now,
        } for p in players])

# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------

def main():
    from sqlalchemy import create_engine

    if not DATABASE_URL:
        raise SystemExit("DATABASE_URL not set in .env -- needed for ESPN minutes and saving.")
    engine = create_engine(DATABASE_URL)
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)

    pages = sorted(glob.glob(SNAPSHOT_GLOB), key=lambda p: int(re.search(r"(\d+)\.html$", p).group(1)))
    players, seen = [], set()
    for path in pages:
        page_players = parse_page(path)
        print(f"  {os.path.basename(path)}: {len(page_players)} players")
        for p in page_players:
            if p["yahoo_player_id"] not in seen:  # overlapping saves are harmless
                seen.add(p["yahoo_player_id"])
                players.append(p)
    for p in players:
        add_derived_stats(p)
    players.sort(key=lambda p: (p["yahoo_rank"] is None, p["yahoo_rank"] or 0))
    print(f"{len(players)} Yahoo players from {len(pages)} pages.")

    unmatched = match_espn(players, load_espn_players(engine))
    print(f"Matched to ESPN: {len(players) - len(unmatched)}/{len(players)}")
    if unmatched:
        print("  Not matched (no MPG / age):", ", ".join(unmatched))
    no_mpg = [p["full_name"] for p in players if p["espn_player_id"] and p["mpg"] is None]
    if no_mpg:
        print("  Matched but ESPN has no projected minutes:", ", ".join(no_mpg))

    print("Fetching birth dates from ESPN...")
    add_ages(players, now.date())
    no_age = [p["full_name"] for p in players if p["espn_player_id"] and p["age"] is None]
    if no_age:
        print("  No birth date on ESPN:", ", ".join(no_age))

    save_csv(players)
    print(f"Saved {CSV_PATH}")
    save_db(engine, players, now)
    print("Saved to Postgres: yahoo_projections")


if __name__ == "__main__":
    main()
