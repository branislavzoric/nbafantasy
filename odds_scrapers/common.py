"""
Shared pieces for the bookmaker player-prop scrapers: the row format, CSV output and
the Postgres "odds" schema.

Every scraper's scrape() returns (rows, games):
  games: [{"id", "home", "away", "kickoff"}]                 the bookmaker's EuroLeague games
  rows:  [{"game_id", "home", "away", "kickoff", "team", "player", "player_raw",
           "market", "side", "line", "odds", "player_match_id"}]
         market = one of MARKETS, side = Over / Under / "15+" (milestone) / Yes,
         line = the over/under line (None for milestones), kickoff = local naive datetime.
"""

import csv
import datetime as dt
import os
import re

import requests
from sqlalchemy import create_engine, text

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(PROJECT_DIR, "Odds")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
DELAY_S = 0.3   # pause between per-player requests -- keep the load light

MARKETS = ["Points", "Rebounds", "Assists", "3PM", "Pts+Reb+Ast", "Pts+Reb", "Pts+Ast", "Reb+Ast",
           "Steals", "Blocks", "Turnovers", "Double-double", "1Q Points", "1H Points"]


def session(**headers):
    s = requests.Session()
    s.headers.update({"User-Agent": UA, **headers})
    return s


def local_from_ms(ms):
    return dt.datetime.fromtimestamp(ms / 1000)


def local_from_utc(iso):
    """'2026-10-08T16:00:00' / '...Z' / '2026-10-08 16:00:00' (UTC) -> local naive datetime."""
    t = dt.datetime.fromisoformat(iso.replace("Z", "").replace(" ", "T")[:19])
    return t.replace(tzinfo=dt.timezone.utc).astimezone().replace(tzinfo=None)


def to_float(v):
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _words(name):
    return {w for w in re.split(r"[^a-z0-9]+", (name or "").lower()) if len(w) > 2}


def game_finder(games):
    """team name + kickoff -> game, for books that list players separately from games.
    Falls back to a shared word among the games at that kickoff ("Olimpia Milano" ~ "Armani Milano")."""
    by = {}
    for g in games:
        by[(g["home"], g["kickoff"])] = by[(g["away"], g["kickoff"])] = g

    def find(team, kickoff):
        if (team, kickoff) in by:
            return by[(team, kickoff)]
        hits = [g for g in games if g["kickoff"] == kickoff
                and _words(team) & (_words(g["home"]) | _words(g["away"]))]
        return hits[0] if len(hits) == 1 else None
    return find


def row(game, team, player, market, side, line, odds, player_match_id, player_raw=None, kickoff=None):
    player = " ".join(player.split())
    team = " ".join(team.split()) if team else None
    return {"game_id": game["id"] if game else None,
            "home": game["home"] if game else None, "away": game["away"] if game else None,
            "kickoff": game["kickoff"] if game else kickoff,
            "team": team, "player": player, "player_raw": player_raw or player,
            "market": market, "side": side, "line": line, "odds": float(odds),
            "player_match_id": player_match_id}

# -----------------------------------------------------------------------------
# CSV
# -----------------------------------------------------------------------------

def wide(rows):
    """One row per player/market/line with Over and Under side by side; milestones as their own lines."""
    out = {}
    for r in rows:
        game = f"{r['home']} vs {r['away']}" if r["home"] else ""
        if r["side"] in ("Over", "Under"):
            key = (game, r["team"] or "", r["player"], r["market"], r["line"])
            out.setdefault(key, {})[r["side"].lower()] = r["odds"]
        else:
            key = (game, r["team"] or "", r["player"], f"{r['market']} {r['side']}", None)
            out.setdefault(key, {})["over"] = r["odds"]

    def sort_key(item):
        (game, team, player, market, line), _ = item
        base = re.sub(r" (\d+\+|Yes)$", "", market)
        ms = re.search(r"(\d+)\+$", market)
        return (game, team, player, MARKETS.index(base) if base in MARKETS else 99,
                1 if ms else 0, int(ms.group(1)) if ms else 0, line or 0)
    return [{"game": g, "team": t, "player": p, "market": m, "line": "" if l is None else l,
             "over": v.get("over", ""), "under": v.get("under", "")}
            for (g, t, p, m, l), v in sorted(out.items(), key=sort_key)]


def write_csvs(source, rows, prefix="euroleague"):
    folder = os.path.join(OUT_DIR, source, dt.date.today().isoformat())
    os.makedirs(folder, exist_ok=True)
    long_rows = [{**r, "kickoff": r["kickoff"].strftime("%Y-%m-%d %H:%M") if r["kickoff"] else ""}
                 for r in rows]
    for path, data in ((os.path.join(folder, f"{prefix}_players.csv"), long_rows),
                       (os.path.join(folder, f"{prefix}_players_wide.csv"), wide(rows))):
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:   # BOM so Excel shows č/ć
                w = csv.DictWriter(f, fieldnames=list(data[0]))
                w.writeheader()
                w.writerows(data)
        except PermissionError:
            print(f"  Warning: {path} is open in another program (Excel?) -- CSV not updated")
    return folder

# -----------------------------------------------------------------------------
# POSTGRES -- schema "odds", same DATABASE_URL as the NBA data
# -----------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS odds;
CREATE TABLE IF NOT EXISTS odds.scrape_runs (
    run_id      serial PRIMARY KEY,
    source      text NOT NULL,
    league_id   text NOT NULL,
    scraped_at  timestamptz NOT NULL DEFAULT now(),
    games       int,
    players     int,
    odds        int
);
CREATE TABLE IF NOT EXISTS odds.games (
    source      text NOT NULL,
    game_id     bigint NOT NULL,
    league_id   text NOT NULL,
    league_name text,
    home        text NOT NULL,
    away        text NOT NULL,
    kickoff     timestamp,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, game_id)
);
CREATE TABLE IF NOT EXISTS odds.player_props (
    run_id          int NOT NULL REFERENCES odds.scrape_runs (run_id) ON DELETE CASCADE,
    source          text NOT NULL,
    game_id         bigint,
    player_match_id bigint NOT NULL,
    team            text,
    player          text NOT NULL,
    market          text NOT NULL,
    side            text NOT NULL,
    line            numeric,
    odds            numeric NOT NULL
);
ALTER TABLE odds.player_props ALTER COLUMN team DROP NOT NULL;
ALTER TABLE odds.player_props ADD COLUMN IF NOT EXISTS player_raw text;
DROP INDEX IF EXISTS odds.player_props_uniq;
CREATE UNIQUE INDEX IF NOT EXISTS player_props_uniq2
    ON odds.player_props (run_id, player_match_id, player, market, side, COALESCE(line, -1));
CREATE INDEX IF NOT EXISTS player_props_player ON odds.player_props (player, market);
CREATE INDEX IF NOT EXISTS player_props_game ON odds.player_props (source, game_id, run_id);
CREATE OR REPLACE VIEW odds.player_props_latest AS   -- replace in place: odds.compare depends on it
SELECT g.league_name, g.home, g.away, g.kickoff, p.*, r.scraped_at
FROM odds.player_props p
JOIN odds.scrape_runs r ON r.run_id = p.run_id
LEFT JOIN odds.games g ON g.source = p.source AND g.game_id = p.game_id
WHERE p.run_id = (SELECT max(r2.run_id) FROM odds.scrape_runs r2 WHERE r2.source = p.source);
"""


def save_db(source, league, rows, games, league_name="EuroLeague"):
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_DIR, ".env"))
    engine = create_engine(os.environ["DATABASE_URL"])
    # A book can repeat an identical odd (same player/market/side/line) -- keep the first
    seen, unique = set(), []
    for r in rows:
        key = (r["player_match_id"], r["player"], r["market"], r["side"], r["line"])
        if key not in seen:
            seen.add(key)
            unique.append(r)
    with engine.begin() as conn:
        conn.execute(text(SCHEMA_SQL))
        run_id = conn.execute(text("""
            INSERT INTO odds.scrape_runs (source, league_id, games, players, odds)
            VALUES (:s, :l, :g, :p, :o) RETURNING run_id"""),
            {"s": source, "l": league, "g": len({r["game_id"] for r in unique if r["game_id"]}),
             "p": len({(r["game_id"], r["player"]) for r in unique}), "o": len(unique)}).scalar()
        if games:
            conn.execute(text("""
                INSERT INTO odds.games (source, game_id, league_id, league_name, home, away, kickoff)
                VALUES (:source, :id, :league, :name, :home, :away, :kickoff)
                ON CONFLICT (source, game_id) DO UPDATE SET home = EXCLUDED.home, away = EXCLUDED.away,
                    kickoff = EXCLUDED.kickoff, league_name = EXCLUDED.league_name, updated_at = now()"""),
                [{**g, "source": source, "league": league, "name": league_name} for g in games])
        conn.execute(text("""
            INSERT INTO odds.player_props (run_id, source, game_id, player_match_id, team, player,
                                           player_raw, market, side, line, odds)
            VALUES (:run, :source, :game_id, :player_match_id, :team, :player, :player_raw,
                    :market, :side, :line, :odds)"""),
            [{**r, "run": run_id, "source": source} for r in unique])
    return run_id, len(unique)
