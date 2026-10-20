#!/usr/bin/env python3
"""
build_slip.py

Builds the accumulator with the highest estimated chance of winning, using one bookmaker's
prices, with total odds inside a range. Run scrape_odds.py and compare_odds.py first.

True probability of a leg = the no-vig consensus of the OTHER books' over/under pairs at the
exact same line (at least --min-books of them). Ticket probability = product of the legs
(treated as independent, so at most --per-game legs from one game, one leg per player).

Maximising P(win) with total odds fixed means maximising sum(log(p * odds)): a leg priced above
its true chance (p * odds > 1) adds probability, any other leg costs some.

Usage:
  python build_slip.py [--book soccerbet] [--min 8] [--max 12] [--legs 4] [--min-books 3] [--per-game 2]
"""

import argparse
import datetime as dt
import math
import os

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from compare_odds import BOOKS

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
BIN = 0.004          # log-odds resolution of the search
BEAM = 12            # partial tickets kept per (legs, total-odds bin)
MAX_LEGS = 10


def candidates(cmp, book, min_books):
    others = [b for b in BOOKS if b != book]
    ou = cmp[cmp.side.isin(["Over", "Under"])]
    key = ["game", "player", "market", "line"]
    o = ou[ou.side == "Over"].set_index(key)[others].astype(float)
    u = ou[ou.side == "Under"].set_index(key)[others].astype(float)
    o, u = o.align(u, join="inner")
    p = (1 / o) / (1 / o + 1 / u)
    fair = pd.DataFrame({"p_over": p.mean(axis=1), "n": p.notna().sum(axis=1)})
    fair = fair[fair.n >= min_books]

    legs = []
    now = dt.datetime.now()
    for r in cmp[cmp[book].notna() & cmp.side.isin(["Over", "Under"])].itertuples():
        k = (r.game, r.player, r.market, r.line)
        if k not in fair.index or pd.Timestamp(r.kickoff) <= now + dt.timedelta(minutes=5):
            continue
        prob = fair.at[k, "p_over"] if r.side == "Over" else 1 - fair.at[k, "p_over"]
        odds = float(getattr(r, book))
        if odds <= 1.0 or not 0 < prob < 1:
            continue
        milestone = book in str(r.milestone_books or "").split(",")
        bet = (f"{int(float(r.line) + 0.5)}+ {r.market}" if milestone
               else f"{r.side} {float(r.line):g} {r.market}")
        legs.append({"game": r.game, "kickoff": r.kickoff, "player": r.player, "team": r.team,
                     "bet": bet, "odds": odds, "p": prob, "fair_odds": 1 / prob, "edge": prob * odds - 1,
                     "books": int(fair.at[k, "n"])})
    return pd.DataFrame(legs)


def search(legs, lo, hi, per_game):
    """Beam search over players: state = (legs, odds bin) -> best partial tickets by sum(log p)."""
    lo_w, hi_w = math.log(lo), math.log(hi)
    legs = legs[legs.odds <= hi].copy()
    legs["w"] = legs.odds.map(math.log)
    legs["v"] = legs.p.map(math.log)
    states = {(0, 0): [(0.0, 0.0, (), ())]}          # (sum log p, sum log odds, leg indices, games)
    for _, group in legs.groupby("player"):
        options = list(group.itertuples())
        new = {k: list(v) for k, v in states.items()}
        for (n, _), plist in states.items():
            if n >= MAX_LEGS:
                continue
            for v, w, idx, games in plist:
                for o in options:
                    w2 = w + o.w
                    if w2 > hi_w or games.count(o.game) >= per_game:
                        continue
                    k = (n + 1, int(w2 / BIN))
                    new.setdefault(k, []).append((v + o.v, w2, idx + (o.Index,), games + (o.game,)))
        states = {k: sorted(v, key=lambda t: -t[0])[:BEAM] for k, v in new.items()}
    best = {}
    for (n, _), plist in states.items():
        for v, w, idx, _ in plist:
            if lo_w <= w <= hi_w and (n not in best or v > best[n][0]):
                best[n] = (v, w, idx)
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", default="soccerbet", choices=BOOKS)
    ap.add_argument("--min", type=float, default=8.0)
    ap.add_argument("--max", type=float, default=12.0)
    ap.add_argument("--min-books", type=int, default=3)
    ap.add_argument("--per-game", type=int, default=2)
    ap.add_argument("--legs", type=int, help="exact number of legs (default: whatever is most likely)")
    args = ap.parse_args()
    load_dotenv(os.path.join(PROJECT_DIR, ".env"))
    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as conn:
        cmp = pd.read_sql(text("SELECT * FROM odds.compare"), conn)
        scraped = conn.execute(text("SELECT max(scraped_at) FROM odds.scrape_runs WHERE source = :b"),
                               {"b": args.book}).scalar()
    legs = candidates(cmp, args.book, args.min_books)
    print(f"{args.book} prices scraped {scraped:%d.%m %H:%M}; {len(legs)} legs with a consensus of "
          f"{args.min_books}+ other books, {int((legs.edge > 0).sum())} priced above their true chance")
    best = search(legs, args.min, args.max, args.per_game)
    if not best:
        raise SystemExit("No ticket fits that odds range.")

    print(f"\nBest ticket per number of legs (total odds {args.min:g}-{args.max:g}):")
    for n in sorted(best):
        v, w, _ = best[n]
        print(f"  {n:>2} legs  odds {math.exp(w):6.2f}  P(win) {100 * math.exp(v):5.2f}%  "
              f"EV {100 * (math.exp(v + w) - 1):+5.1f}%")
    if args.legs and args.legs not in best:
        raise SystemExit(f"No {args.legs}-leg ticket fits total odds {args.min:g}-{args.max:g}.")
    n = args.legs or max(best, key=lambda k: best[k][0])
    v, w, idx = best[n]
    slip = legs.loc[list(idx)].sort_values(["kickoff", "game", "player"])
    print(f"\nMost likely ticket: {n} legs, total odds {math.exp(w):.2f}, estimated P(win) {100 * math.exp(v):.1f}% "
          f"(fair odds {math.exp(-v):.2f}), EV {100 * (math.exp(v + w) - 1):+.1f}% per unit staked\n")
    for r in slip.itertuples():
        print(f"  {pd.Timestamp(r.kickoff):%H:%M} {r.game:<36} {r.player:<24} {r.bet:<22} "
              f"{r.odds:>5.2f}  p={100 * r.p:4.1f}%  fair {r.fair_odds:4.2f}  ({r.books} books)")
    out = os.path.join(PROJECT_DIR, "Odds", "compare", dt.date.today().isoformat(), f"slip_{args.book}.csv")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    slip.drop(columns=["w", "v"], errors="ignore").to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n-> {out}")


if __name__ == "__main__":
    main()
