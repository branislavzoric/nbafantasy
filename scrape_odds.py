#!/usr/bin/env python3
"""
scrape_odds.py

EuroLeague (default) or NBA player-prop odds from Serbian bookmakers -> Postgres schema "odds" + CSVs.

Usage:
  python scrape_odds.py                       # all books, EuroLeague
  python scrape_odds.py --nba                 # all books, NBA
  python scrape_odds.py soccerbet maxbet      # just these
  python scrape_odds.py --no-db               # CSVs only
Books: soccerbet, maxbet, admiralbet, mozzartbet, superbet, balkanbet
Output: odds.player_props (+ odds.games, odds.scrape_runs, view odds.player_props_latest)
        "Odds/<book>/<date>/euroleague_players.csv" (or nba_players.csv) and "..._wide.csv"
A run replaces the book's previous run as "latest", so compare_odds.py compares the league scraped last.
"""

import sys
import traceback

from odds_scrapers import admiralbet, balkanbet, dualsoft, mozzartbet, superbet
from odds_scrapers.common import save_db, write_csvs

SCRAPERS = {
    "soccerbet": lambda: dualsoft.scrape("soccerbet"),
    "maxbet": lambda: dualsoft.scrape("maxbet"),
    "admiralbet": admiralbet.scrape,
    "mozzartbet": mozzartbet.scrape,
    "superbet": superbet.scrape,
    "balkanbet": balkanbet.scrape,
}


def use_nba():
    """Point every scraper at the NBA. Admiral and BalkanBet had no NBA player-prop listing when this
    was written (Oct 2026); they still scrape the games, so props show up once a listing id is added."""
    dualsoft.BOOKS["soccerbet"]["league"] = 2521548     # games and players share the league id
    dualsoft.BOOKS["maxbet"]["league"] = 144532
    admiralbet.GAMES = {"sportId": 2, "regionId": 122, "competitionId": 12}
    admiralbet.PLAYERS = None
    mozzartbet.COMPETITION = "nba"
    superbet.TOURNAMENT = 164
    balkanbet.GAMES_TOURNAMENT, balkanbet.PLAYERS_TOURNAMENT = 234, None
    return "nba", "NBA"


def main():
    league, league_name = use_nba() if "--nba" in sys.argv else ("euroleague", "EuroLeague")
    books = [a for a in sys.argv[1:] if not a.startswith("--")] or list(SCRAPERS)
    bad = [b for b in books if b not in SCRAPERS]
    if bad:
        raise SystemExit(f"Unknown book(s): {', '.join(bad)}. Choose from: {', '.join(SCRAPERS)}")
    summary = []
    for book in books:
        print(f"{book}:")
        try:
            rows, games = SCRAPERS[book]()
        except Exception as e:                      # one broken site shouldn't stop the others
            traceback.print_exc()
            summary.append((book, f"FAILED: {e}"))
            continue
        if not rows:
            summary.append((book, "no player odds"))
            continue
        folder = write_csvs(book, rows, league)
        msg = f"{len(rows)} odds, {len({(r['game_id'], r['player']) for r in rows})} players, CSV in {folder}"
        if "--no-db" not in sys.argv:
            run_id, n = save_db(book, league, rows, games, league_name)
            msg += f", DB run {run_id} ({n} rows)"
        summary.append((book, msg))
    print(f"\nSummary ({league_name}):")
    for book, msg in summary:
        print(f"  {book:<11} {msg}")


if __name__ == "__main__":
    main()
