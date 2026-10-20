#!/usr/bin/env python3
"""
scrape_football.py

Today's football odds from the six Serbian bookmakers, top 8 leagues, basic markets
(1X2, double chance, Total 2.5, both teams to score); see odds_scrapers/football.py.
Saves every row to Odds/football/<date>/raw_odds.csv (+ .json) for build_football_bets.py.

Usage:
  python scrape_football.py [--all] [book ...]

--all: every football match today in every league (league = the book's own name), saved as raw_all.*
--combos: combo bets ("1X&I1-3&II1-3", "GG&3+", ...) for the top-league matches, one full-offer call per
          match (odds_scrapers/football_combos.py), saved as raw_combos.* for build_football_combos.py
--combos --all: combo bets for every league today, Soccerbet + MaxBet only -> raw_combos_all.*
"""

import csv
import datetime as dt
import json
import os
import sys
import time
from collections import Counter

from odds_scrapers.football import SCRAPERS, SCRAPERS_ALL
from odds_scrapers.football_combos import SCRAPERS as SCRAPERS_COMBOS, SCRAPERS_ALL as SCRAPERS_COMBOS_ALL

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(PROJECT_DIR, "Odds", "football")
FIELDS = ["book", "league", "home", "away", "kickoff", "market", "sel", "odds"]
COMBO_FIELDS = ["book", "league", "home", "away", "kickoff", "text", "ctx", "odds"]


def main():
    args = sys.argv[1:]
    all_leagues = "--all" in args
    combos = "--combos" in args
    scrapers = ((SCRAPERS_COMBOS_ALL if all_leagues else SCRAPERS_COMBOS) if combos
                else SCRAPERS_ALL if all_leagues else SCRAPERS)
    stem = ("raw_combos_all" if all_leagues else "raw_combos") if combos else "raw_all" if all_leagues else "raw_odds"
    books = [a for a in args if not a.startswith("--")] or list(scrapers)
    folder = os.path.join(OUT_DIR, dt.date.today().isoformat())
    os.makedirs(folder, exist_ok=True)
    rows, summary = [], {}
    for book in books:
        print(f"{book}:")
        t = time.time()
        try:
            got = scrapers[book]()
        except Exception as ex:                                   # one broken book shouldn't stop the rest
            print(f"  FAILED: {ex}")
            summary[book] = "failed"
            continue
        games = {(r["league"], r["home"], r["away"]) for r in got}
        per_league = Counter(l for l, _, _ in games)
        print(f"  {len(games)} matches, {len(got)} odds ({time.time() - t:.0f}s) · "
              + (f"{len(per_league)} leagues" if all_leagues else
                 ", ".join(f"{l} {n}" for l, n in sorted(per_league.items()))))
        summary[book] = f"{len(games)} matches"
        rows += got
    for r in rows:
        r["kickoff"] = r["kickoff"].strftime("%Y-%m-%d %H:%M")
    with open(os.path.join(folder, stem + ".csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COMBO_FIELDS if combos else FIELDS)
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(folder, stem + ".json"), "w", encoding="utf-8") as f:
        json.dump({"scraped": dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "books": summary, "rows": rows},
                  f, ensure_ascii=False, indent=1)
    print("\nSummary:")
    for b, s in summary.items():
        print(f"  {b:11} {s}")
    print(f"Saved {len(rows)} odds -> {folder}")


if __name__ == "__main__":
    main()
