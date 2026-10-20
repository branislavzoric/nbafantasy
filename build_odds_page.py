#!/usr/bin/env python3
"""
build_odds_page.py

Builds the interactive odds page ("Odds/odds_board.html") from the odds.compare view:
every player line with all books side by side, value bets and arbitrage.
Run scrape_odds.py and compare_odds.py first.

Usage:
  python build_odds_page.py [--min-books 3] [--min-edge 0.03]
"""

import argparse
import datetime as dt
import json
import math
import os

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from compare_odds import BOOKS, analyse

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(PROJECT_DIR, "odds_board_template.html")
OUT = os.path.join(PROJECT_DIR, "Odds", "odds_board.html")
BOOK_LABELS = {"soccerbet": "Soccerbet", "maxbet": "MaxBet", "admiralbet": "Admiral",
               "mozzartbet": "Mozzart", "superbet": "Superbet", "balkanbet": "Balkanbet"}


def num(v):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-books", type=int, default=3)
    ap.add_argument("--min-edge", type=float, default=0.03)
    args = ap.parse_args()
    load_dotenv(os.path.join(PROJECT_DIR, ".env"))
    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as conn:
        cmp = pd.read_sql(text("SELECT * FROM odds.compare ORDER BY kickoff, game, player, market, line, side"), conn)
        runs = pd.read_sql(text("""SELECT DISTINCT ON (source) source, scraped_at FROM odds.scrape_runs
                                   ORDER BY source, run_id DESC"""), conn)
    for b in BOOKS:
        cmp[b] = cmp[b].astype(float)
    cmp["line"] = cmp["line"].astype(float)
    cmp, value, arb = analyse(cmp, args.min_books, args.min_edge)

    # No-vig consensus per line (all books with both sides), shown in the board's "Fair" column
    ou = cmp[cmp.side.isin(["Over", "Under"])]
    key = ["game", "player", "market", "line"]
    o = ou[ou.side == "Over"].set_index(key)[BOOKS]
    u = ou[ou.side == "Under"].set_index(key)[BOOKS]
    o, u = o.align(u, join="inner")
    p_over = ((1 / o) / (1 / o + 1 / u)).mean(axis=1)
    pairs = ((1 / o) / (1 / o + 1 / u)).notna().sum(axis=1)

    games = list(dict.fromkeys(cmp.game))
    game_info = {g: cmp[cmp.game == g].iloc[0] for g in games}
    players = list(dict.fromkeys(zip(cmp.game, cmp.player)))
    pidx = {p: i for i, p in enumerate(players)}
    value_cells = {(r.game, r.player, r.market, r.side, r.line, r.book): r.edge_pct for r in value.itertuples()} \
        if len(value) else {}

    rows = []
    for r in cmp.itertuples():
        k = (r.game, r.player, r.market, r.line)
        fair = None
        if k in p_over.index and pairs[k] >= 2:
            p = p_over[k] if r.side == "Over" else 1 - p_over[k]
            fair = round(1 / p, 2) if p > 0 else None
        ms = str(r.milestone_books or "").split(",")
        rows.append({
            "p": pidx[(r.game, r.player)], "m": r.market, "s": r.side, "l": num(r.line),
            "o": [num(getattr(r, b)) for b in BOOKS],
            "ms": [b in ms for b in BOOKS],
            "v": [value_cells.get((r.game, r.player, r.market, r.side, r.line, b)) for b in BOOKS],
            "f": fair,
        })
    data = {
        "built": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "books": [{"id": b, "name": BOOK_LABELS[b],
                   "scraped": next((pd.Timestamp(t).tz_convert(None).tz_localize("UTC").tz_convert("Europe/Belgrade")
                                    .strftime("%d.%m %H:%M") for s, t in zip(runs.source, runs.scraped_at) if s == b), None)}
                  for b in BOOKS],
        "games": [{"name": g, "kickoff": pd.Timestamp(game_info[g].kickoff).strftime("%a %d.%m %H:%M")} for g in games],
        "players": [{"g": games.index(g), "name": p,
                     "team": next((t for t in cmp[(cmp.game == g) & (cmp.player == p)].team if isinstance(t, str)), None)}
                    for g, p in players],
        "rows": rows,
        "value": [{"p": pidx[(r.game, r.player)], "m": r.market, "s": r.side, "l": r.line, "b": BOOKS.index(r.book),
                   "o": r.odds, "f": r.fair_odds, "e": r.edge_pct, "n": int(r.books_in_consensus),
                   "ms": bool(r.via_milestone)} for r in value.itertuples()] if len(value) else [],
        "arb": [{"p": pidx[(r.game, r.player)], "m": r.market, "l": r.line,
                 "oo": float(r.best_odds_over), "ob": BOOKS.index(r.best_book_over), "oms": bool(r.best_is_milestone),
                 "uo": float(r.best_odds_under), "ub": BOOKS.index(r.best_book_under), "pr": float(r.profit_pct)}
                for r in arb.itertuples()],
        "settings": {"minEdge": args.min_edge, "minBooks": args.min_books},
    }
    with open(TEMPLATE, encoding="utf-8") as f:
        html = f.read().replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"{len(rows)} lines, {len(players)} players, {len(data['value'])} value bets, "
          f"{len(data['arb'])} arbitrage -> {OUT} ({os.path.getsize(OUT) // 1024} KB)")


if __name__ == "__main__":
    main()
