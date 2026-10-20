#!/usr/bin/env python3
"""
build_edges_page.py

Value bets + arbitrage page ("Odds/prop_edges.html", template prop_edges_template.html)
from the newest compare_odds.py output in "Odds/compare/<date>/": value_bets.csv,
arbitrage.csv, and odds_comparison.csv (every book's price on each of those lines).
Run scrape_odds.py and compare_odds.py first.

Usage:
  python build_edges_page.py [YYYY-MM-DD]
"""

import datetime as dt
import json
import math
import os
import sys

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from build_odds_page import BOOK_LABELS
from compare_odds import BOOKS

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
COMPARE_DIR = os.path.join(PROJECT_DIR, "Odds", "compare")
TEMPLATE = os.path.join(PROJECT_DIR, "prop_edges_template.html")
OUT = os.path.join(PROJECT_DIR, "Odds", "prop_edges.html")


def num(v):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)


def main():
    load_dotenv(os.path.join(PROJECT_DIR, ".env"))
    day = sys.argv[1] if len(sys.argv) > 1 else max(os.listdir(COMPARE_DIR))
    folder = os.path.join(COMPARE_DIR, day)
    read = lambda name: pd.read_csv(os.path.join(folder, name), encoding="utf-8-sig")
    value, arb, cmp = read("value_bets.csv"), read("arbitrage.csv"), read("odds_comparison.csv")

    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as conn:
        runs = pd.read_sql(text("""SELECT DISTINCT ON (source) source, scraped_at, odds FROM odds.scrape_runs
                                   ORDER BY source, run_id DESC"""), conn)
    runs = {r.source: r for r in runs.itertuples()}

    def prices(game, player, market, line):
        """Every book's Over / Under price on one line."""
        rows = cmp[(cmp.game == game) & (cmp.player == player) & (cmp.market == market) & (cmp.line == line)]
        out = {}
        for r in rows.itertuples():
            ms = set(str(r.milestone_books).split(",")) if isinstance(r.milestone_books, str) else set()
            out[r.side] = [{"o": num(getattr(r, b)), "ms": b in ms} for b in BOOKS]
        return out

    games = (cmp[["game", "kickoff"]].drop_duplicates().sort_values(["kickoff", "game"]))
    team_of = {(r.game, r.player): r.team for r in cmp.itertuples() if isinstance(r.team, str)}
    data = {
        "built": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "day": day,
        "books": [{"id": b, "name": BOOK_LABELS[b],
                   "scraped": (runs[b].scraped_at.strftime("%d.%m %H:%M") if b in runs else None),
                   "fresh": bool(b in runs and runs[b].scraped_at.date().isoformat() == day
                                 and runs[b].odds and cmp[b].notna().any())} for b in BOOKS],
        "games": [{"name": g.game, "kickoff": pd.Timestamp(g.kickoff).strftime("%H:%M")}
                  for g in games.itertuples()],
        "value": [{"g": r.game, "p": r.player, "t": team_of.get((r.game, r.player), ""), "m": r.market,
                   "s": r.side, "l": r.line, "b": r.book, "o": r.odds, "f": r.fair_odds, "e": r.edge_pct,
                   "ms": bool(r.via_milestone), "n": int(r.books_in_consensus),
                   "all": prices(r.game, r.player, r.market, r.line)} for r in value.itertuples()],
        "arb": [{"g": r.game, "p": r.player, "t": team_of.get((r.game, r.player), ""), "m": r.market,
                 "l": r.line, "oo": r.best_odds_over, "ob": r.best_book_over, "oms": bool(r.best_is_milestone),
                 "uo": r.best_odds_under, "ub": r.best_book_under, "pr": r.profit_pct,
                 "all": prices(r.game, r.player, r.market, r.line)} for r in arb.itertuples()],
    }
    with open(TEMPLATE, encoding="utf-8") as f:
        page = f.read().replace("/*DATA*/null", json.dumps(data, ensure_ascii=False))
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"{len(data['value'])} value bets, {len(data['arb'])} arbitrage lines -> {OUT}")


if __name__ == "__main__":
    main()
