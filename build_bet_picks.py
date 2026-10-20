#!/usr/bin/env python3
"""
build_bet_picks.py

Tonight's betting card from the cross-book comparison (run scrape_odds.py and
compare_odds.py first):
  - top 10 picks: the best-priced player props vs the no-vig consensus of the
    OTHER books (leave-one-out), one pick per player, ranked by edge with a
    confidence weight for how many books form the consensus
  - a 4-leg slip with total odds >= 10 and the highest expected value
  - a 10+ leg slip with the highest expected value
  - the 5 most profitable arbitrages
A slip is placed at ONE bookmaker, so every leg of a slip comes from the same
book. Legs are different players (same player = correlated).

Output: Odds/picks/<date>/picks.json (+ printed summary)

Usage:
  python build_bet_picks.py [--min-books 3] [--slip-odds 10] [--long-legs 10]
"""

import argparse
import datetime as dt
import itertools
import json
import math
import os

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from build_odds_page import BOOK_LABELS
from compare_odds import BOOKS, analyse

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(PROJECT_DIR, "Odds", "picks")

ODDS_MIN, ODDS_MAX = 1.40, 4.00       # picks outside this range are lottery tickets or not worth it
LEG_MIN, LEG_MAX = 1.30, 3.50         # slip legs
CANDIDATES = 40                       # legs per book searched for the 4-leg slip


def load():
    load_dotenv(os.path.join(PROJECT_DIR, ".env"))
    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as conn:
        cmp = pd.read_sql(text("SELECT * FROM odds.compare ORDER BY kickoff, game, player, market, line, side"),
                          conn)
        runs = pd.read_sql(text("""SELECT DISTINCT ON (source) source, scraped_at, odds FROM odds.scrape_runs
                                   ORDER BY source, run_id DESC"""), conn)
    for b in BOOKS:
        cmp[b] = cmp[b].astype(float)
    cmp = cmp[pd.to_datetime(cmp["kickoff"]) > pd.Timestamp.now()]
    return cmp, runs


def leg(r):
    p = 1 / r.fair_odds
    return {"game": r.game, "player": r.player, "market": r.market, "side": r.side, "line": float(r.line),
            "book": r.book, "book_name": BOOK_LABELS[r.book], "odds": float(r.odds), "fair_odds": float(r.fair_odds),
            "prob": round(p, 4), "edge_pct": float(r.edge_pct), "books": int(r.books_in_consensus),
            "milestone": bool(r.via_milestone)}


def top_picks(lines, team_of, kickoff_of, n=10):
    """Best price per bet, one per player, ranked by edge x consensus confidence."""
    pool = lines[(lines.odds >= ODDS_MIN) & (lines.odds <= ODDS_MAX) & (lines.edge_pct > 0)].copy()
    pool["score"] = pool.edge_pct * pool.books_in_consensus.clip(upper=4) / 4
    pool = pool.sort_values(["score", "odds"], ascending=False)
    picks, players = [], set()
    for r in pool.itertuples():
        if r.player in players:
            continue
        same = pool[(pool.player == r.player) & (pool.market == r.market) & (pool.line == r.line)
                    & (pool.side == r.side)]
        alt = [BOOK_LABELS[b] for b in same.book if b != r.book and same[same.book == b].odds.iloc[0] == r.odds]
        d = leg(r)
        d.update(team=team_of.get((r.game, r.player), ""), kickoff=kickoff_of[r.game], also_at=alt,
                 ev_per_100=round(100 * r.edge_pct / 100, 1))
        picks.append(d)
        players.add(r.player)
        if len(picks) == n:
            break
    return picks


def slip_stats(legs):
    odds = math.prod(l["odds"] for l in legs)
    prob = math.prod(l["prob"] for l in legs)
    return {"total_odds": round(odds, 2), "hit_prob": round(prob, 5), "ev_pct": round(100 * (odds * prob - 1), 1),
            "legs": legs, "book": legs[0]["book"], "book_name": legs[0]["book_name"]}


def best_short_slip(lines, n_legs=4, min_odds=10.0):
    """Per book: the n-leg combination (different players) with total odds >= min_odds and the
    highest expected value; the best book wins."""
    best = None
    for book, g in lines.groupby("book"):
        g = g[(g.odds >= LEG_MIN) & (g.odds <= LEG_MAX) & (g.books_in_consensus >= 2)]
        g = g.sort_values("edge_pct", ascending=False).drop_duplicates("player").head(CANDIDATES)
        cand = [leg(r) for r in g.itertuples()]
        for combo in itertools.combinations(cand, n_legs):
            if len({c["player"] for c in combo}) < n_legs:
                continue
            odds = math.prod(c["odds"] for c in combo)
            if odds < min_odds:
                continue
            ev = odds * math.prod(c["prob"] for c in combo)
            if best is None or ev > best[0]:
                best = (ev, combo)
    return slip_stats(list(best[1])) if best else None


def best_long_slip(lines, n_legs=10):
    """Per book: the n highest-edge legs (different players); the book with the best EV wins."""
    best = None
    for book, g in lines.groupby("book"):
        g = g[(g.odds >= LEG_MIN) & (g.odds <= LEG_MAX) & (g.books_in_consensus >= 2)]
        g = g.sort_values("edge_pct", ascending=False).drop_duplicates("player").head(n_legs)
        if len(g) < n_legs:
            continue
        s = slip_stats([leg(r) for r in g.itertuples()])
        if best is None or s["ev_pct"] > best["ev_pct"]:
            best = s
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-books", type=int, default=3)
    ap.add_argument("--slip-odds", type=float, default=10.0)
    ap.add_argument("--long-legs", type=int, default=10)
    args = ap.parse_args()

    cmp, runs = load()
    # every priced line with its leave-one-out fair price (min_edge -1 keeps them all)
    _, lines, arb = analyse(cmp, 2, -1.0)
    lines = lines[lines.books_in_consensus >= 2]
    team_of = {(r.game, r.player): r.team for r in cmp.itertuples() if isinstance(r.team, str)}
    kickoff_of = {r.game: pd.Timestamp(r.kickoff).strftime("%H:%M") for r in cmp.itertuples()}

    picks = top_picks(lines[lines.books_in_consensus >= args.min_books], team_of, kickoff_of)
    short = best_short_slip(lines, 4, args.slip_odds)
    long_ = best_long_slip(lines, args.long_legs)
    arbs = [{"game": r.game, "player": r.player, "team": team_of.get((r.game, r.player), ""), "market": r.market,
             "line": float(r.line), "over_odds": float(r.best_odds_over), "over_book": BOOK_LABELS[r.best_book_over],
             "over_milestone": bool(r.best_is_milestone), "under_odds": float(r.best_odds_under),
             "under_book": BOOK_LABELS[r.best_book_under], "profit_pct": float(r.profit_pct),
             "kickoff": kickoff_of[r.game]} for r in arb.head(5).itertuples()]

    day = dt.date.today().isoformat()
    books_used = [{"book": BOOK_LABELS[b], "odds": int(runs[runs.source == b].odds.iloc[0])
                   if (runs.source == b).any() else 0, "has_lines": bool(cmp[b].notna().any())} for b in BOOKS]
    out = {"date": day, "built": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
           "games": sorted({(kickoff_of[g], g) for g in cmp.game}), "books": books_used,
           "picks": picks, "slip_4": short, "slip_long": long_, "arbitrage": arbs}
    folder = os.path.join(OUT_DIR, day)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "picks.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"Top {len(picks)} picks:")
    for i, p in enumerate(picks, 1):
        ms = " (N+)" if p["milestone"] else ""
        print(f"  {i:>2}. {p['player']:<24} {p['market']} {p['side']} {p['line']}  {p['odds']} @ {p['book_name']}"
              f"{ms}  fair {p['fair_odds']}  edge +{p['edge_pct']}%  ({p['books']} books)")
    for name, s in (("4-leg slip", short), (f"{args.long_legs}-leg slip", long_)):
        if not s:
            print(f"\n{name}: none found")
            continue
        print(f"\n{name} @ {s['book_name']}: total {s['total_odds']}, hit {100 * s['hit_prob']:.2f}%, "
              f"EV {s['ev_pct']:+}%")
        for l in s["legs"]:
            print(f"     {l['player']:<24} {l['market']} {l['side']} {l['line']}  {l['odds']} (fair {l['fair_odds']})")
    print("\nTop 5 arbitrage:")
    for a in arbs:
        print(f"  +{a['profit_pct']}%  {a['player']:<24} {a['market']} {a['line']}  O {a['over_odds']} "
              f"@ {a['over_book']}{' (N+)' if a['over_milestone'] else ''} / U {a['under_odds']} @ {a['under_book']}")
    print(f"\n-> {os.path.join(folder, 'picks.json')}")


if __name__ == "__main__":
    main()
