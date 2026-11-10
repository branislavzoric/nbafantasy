#!/usr/bin/env python3
"""
ladder_gaps.py

Every player-prop price from every book (main over/under, alternative over/under lines and the "N+"
ladders), put on one scale ("N+" = Over N-0.5), to study how the books spread their prices and to find
the gaps between them. Run scrape_odds.py and compare_odds.py first (it uses compare's player matching).

Sources: Soccerbet, MaxBet and Admiral share one feed (compare_odds.FEED) and count as ONE source,
"feed", priced at the best of the three; Mozzart, Superbet and BalkanBet are the others.

Prints and writes to Odds/compare/<date>/:
  ladder_margins.csv   margin of each source's main lines, by market
  ladder_drift.csv     ladder/alt price vs the no-vig consensus, by source and distance from the main line
  ladder_arbs.csv      every Over L1 + Under L2 >= L1 pair with a guaranteed profit (any source, any line)
  ladder_near.csv      the same pairs within 3% of a guaranteed profit (where gaps open when odds move)
  ladder_dominance.csv a book pays more for an easier bet than another source for a harder one
                       (Over at a lower line priced above another source's Over at a higher line)
Usage: python ladder_gaps.py [--near 3]
"""

import argparse
import datetime as dt
import os

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from compare_odds import FEED

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCE = {b: "feed" for b in FEED} | {"mozzartbet": "mozzart", "superbet": "superbet", "balkanbet": "balkanbet"}
BOOK = {"soccerbet": "Soccerbet", "maxbet": "MaxBet", "admiralbet": "Admiral", "mozzartbet": "Mozzart",
        "superbet": "Superbet", "balkanbet": "BalkanBet"}


def load(engine):
    with engine.connect() as conn:
        df = pd.read_sql(text("""
            SELECT pp.source AS book, pp.market, pp.side, pp.line, pp.odds, pl.cplayer_id, pl.player,
                   g.home || ' vs ' || g.away AS game, g.kickoff
            FROM odds.player_props_latest pp
            JOIN odds.cmp_player_links cl ON cl.source = pp.source AND cl.game_id = pp.game_id AND cl.player = pp.player
            JOIN odds.cmp_players pl ON pl.cplayer_id = cl.cplayer_id
            JOIN odds.cmp_games g ON g.cgame_id = pl.cgame_id"""), conn)
    df = df[pd.to_datetime(df.kickoff) > pd.Timestamp.now()]
    ladder = df.side.str.fullmatch(r"\d+\+")
    df["kind"] = ladder.map({True: "ladder", False: "line"})
    df.loc[ladder, "line"] = df.loc[ladder, "side"].str[:-1].astype(float) - 0.5
    df.loc[ladder, "side"] = "Over"
    df = df[df.side.isin(["Over", "Under"]) & df.line.notna()]
    df["src"] = df.book.map(SOURCE)
    return df


def main_lines(df):
    """Each book's own over/under pairs -> margin and no-vig P(over); the consensus averages one value
    per source (feed books averaged first)."""
    o = df[(df.kind == "line") & (df.side == "Over")]
    u = df[(df.kind == "line") & (df.side == "Under")]
    k = ["book", "src", "cplayer_id", "market", "line"]
    pair = o.merge(u, on=k, suffixes=("_o", "_u"))
    pair["margin"] = 1 / pair.odds_o + 1 / pair.odds_u - 1
    pair["p_over"] = (1 / pair.odds_o) / (1 / pair.odds_o + 1 / pair.odds_u)
    per_src = pair.groupby(["src", "cplayer_id", "market", "line"]).p_over.mean().reset_index()
    fair = (per_src.groupby(["cplayer_id", "market", "line"])
            .agg(p_fair=("p_over", "mean"), n_src=("src", "nunique")).reset_index())
    return pair, per_src, fair


NORMAL_K = {"Points": 1.9}            # other points combos: 2.0
COUNT_VR = {"Rebounds": 1.7, "Assists": 1.7, "3PM": 1.5, "Steals": 1.2, "Blocks": 1.3}
BANDS = ["far below", "-2 steps", "-1 step", "main", "+1 step", "+2 steps", "+3-4 steps", "+5 or more"]


def dist(market, mu):
    from scipy import stats
    if market in COUNT_VR:
        vr = COUNT_VR[market]
        p = 1 / vr
        return stats.nbinom(mu * p / (1 - p), p)
    return stats.norm(mu, NORMAL_K.get(market, 2.0) * max(mu, 1) ** 0.5)


def model_ev(df, fair, main_line):
    """EV of every Over price against a distribution fitted to the consensus main line."""
    from scipy import optimize
    m = fair.merge(main_line, left_on=["cplayer_id", "market", "line"], right_on=["cplayer_id", "market", "main"])
    mus = {}
    for r in m.itertuples():
        f = lambda mu: dist(r.market, mu).sf(r.line) - r.p_fair
        try:
            mus[(r.cplayer_id, r.market)] = optimize.brentq(f, 0.05, 80)
        except ValueError:
            pass
    ov = df[df.side == "Over"].merge(main_line, on=["cplayer_id", "market"])
    ov = ov[[(c, mk) in mus for c, mk in zip(ov.cplayer_id, ov.market)]].copy()
    ov["p"] = [dist(mk, mus[(c, mk)]).sf(l) for c, mk, l in zip(ov.cplayer_id, ov.market, ov.line)]
    ov["ev"] = ov.odds * ov.p - 1
    unit = ov.market.map(lambda mk: 1 if mk in COUNT_VR else 2.5)       # a "step": 1 count or 2.5 points
    s = ((ov.line - ov.main) / unit).round()
    ov["band"] = pd.cut(s, [-99, -2.5, -1.5, -0.5, 0.5, 1.5, 2.5, 4.5, 99], labels=BANDS).astype(str)
    return ov[["book", "src", "kind", "player", "market", "line", "main", "odds", "p", "ev", "band"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--near", type=float, default=3.0, help="near-arb window, in %% of margin")
    args = ap.parse_args()
    load_dotenv(os.path.join(PROJECT_DIR, ".env"))
    df = load(create_engine(os.environ["DATABASE_URL"]))
    out = os.path.join(PROJECT_DIR, "Odds", "compare", dt.date.today().isoformat())
    os.makedirs(out, exist_ok=True)
    pair, per_src, fair = main_lines(df)
    print(f"{len(df)} prices, {df.cplayer_id.nunique()} players, sources: "
          + ", ".join(f"{s} {n}" for s, n in df.groupby('src').size().items()))

    # 1. the spread each source builds into its main lines
    marg = (pair.groupby(["book", "market"]).margin.median().unstack().mul(100).round(1))
    marg["ALL"] = pair.groupby("book").margin.median().mul(100).round(1)
    marg.to_csv(os.path.join(out, "ladder_margins.csv"), encoding="utf-8-sig")
    print("\n1. Main-line margin (1/over + 1/under - 1), median %, by book and market")
    print(marg.fillna("").to_string())

    # 2. drift: every over price (ladder or line) vs the no-vig consensus of the OTHER sources at that line,
    #    by distance from the player's main line (the line where most sources quote both sides)
    main_line = (fair.sort_values(["n_src", "p_fair"], key=lambda c: c if c.name == "n_src" else -(c - .5).abs(),
                                  ascending=False).drop_duplicates(["cplayer_id", "market"])
                 [["cplayer_id", "market", "line"]].rename(columns={"line": "main"}))
    ov = df[df.side == "Over"].merge(main_line, on=["cplayer_id", "market"])
    ov = ov.merge(per_src.rename(columns={"src": "o_src", "p_over": "p"}), on=["cplayer_id", "market", "line"])
    ov = ov[ov.o_src != ov.src]
    ov = (ov.groupby(["book", "src", "kind", "cplayer_id", "player", "market", "line", "main", "odds"])
            .p.mean().reset_index())
    ov["ev"] = ov.odds * ov.p - 1
    ov["steps"] = (ov.line - ov.main).round(1)
    step = ov.steps.clip(-6, 12).where(ov.market != "Points", (ov.steps / 2.5).round() * 2.5)
    ov["dist"] = step.map(lambda x: f"{x:+g}")
    drift = (ov.groupby(["src", "kind", "market", "dist"]).ev.agg(["median", "count"]).reset_index())
    drift.to_csv(os.path.join(out, "ladder_drift.csv"), index=False, encoding="utf-8-sig")
    print("\n2. Price vs the other sources' no-vig fair price (EV %), Over side, by distance from the main line")
    for market in ("Points", "Rebounds", "Assists", "3PM"):
        t = ov[ov.market == market]
        if t.empty:
            continue
        tab = t.pivot_table(index=["src", "kind"], columns="steps" if market != "Points" else "dist",
                            values="ev", aggfunc="median").mul(100).round(1)
        cnt = t.pivot_table(index=["src", "kind"], columns="steps" if market != "Points" else "dist",
                            values="ev", aggfunc="count")
        tab = tab.where(cnt >= 5)
        print(f"\n  {market} (columns = line minus main line; cells with 5+ prices)")
        print(tab.dropna(axis=1, how="all").fillna("").to_string())

    # 2b. the same with a fair price at EVERY line: each player's stat distribution fitted to the consensus
    #     main line (Points/combos normal, sd = k * sqrt(mean); counts negative binomial, var = vr * mean;
    #     k and vr calibrated on 2026-10-21 from the books' own alternative over/under pairs)
    tail = model_ev(df, fair, main_line)
    tail.to_csv(os.path.join(out, "ladder_model.csv"), index=False, encoding="utf-8-sig")
    print("\n2b. Over prices vs a fitted fair distribution (EV %, median), by line distance from the main line")
    for market in ("Points", "Rebounds", "Assists", "3PM", "Pts+Reb+Ast"):
        t = tail[tail.market == market]
        if t.empty:
            continue
        tab = t.pivot_table(index=["src", "kind"], columns="band", values="ev", aggfunc="median").mul(100).round(1)
        cnt = t.pivot_table(index=["src", "kind"], columns="band", values="ev", aggfunc="count")
        tab = tab.where(cnt >= 5)[[c for c in BANDS if c in tab.columns]]
        print(f"\n  {market}")
        print(tab.dropna(axis=1, how="all").fillna("").to_string())

    # 3. arbitrage: best over at L1 (any book, line or ladder) + best under at L2 >= L1 (any book)
    best = (df.sort_values("odds", ascending=False)
              .drop_duplicates(["cplayer_id", "market", "side", "line"]))
    bo, bu = best[best.side == "Over"], best[best.side == "Under"]
    pairs = bo.merge(bu, on=["cplayer_id", "player", "game", "market"], suffixes=("_o", "_u"))
    pairs = pairs[pairs.line_u >= pairs.line_o].copy()
    pairs["margin_pct"] = 100 * (1 / pairs.odds_o + 1 / pairs.odds_u - 1)
    pairs["profit_pct"] = (-pairs.margin_pct / (100 + pairs.margin_pct) * 100).round(2)
    pairs["middle"] = pairs.line_u - pairs.line_o
    pairs["same_source"] = pairs.src_o == pairs.src_u
    cols = ["game", "player", "market", "line_o", "odds_o", "book_o", "kind_o", "line_u", "odds_u", "book_u",
            "middle", "margin_pct", "profit_pct", "same_source"]
    arbs = pairs[pairs.margin_pct < 0].sort_values("profit_pct", ascending=False)[cols]
    near = pairs[(pairs.margin_pct >= 0) & (pairs.margin_pct < args.near)].sort_values("margin_pct")[cols]
    arbs.to_csv(os.path.join(out, "ladder_arbs.csv"), index=False, encoding="utf-8-sig")
    near.to_csv(os.path.join(out, "ladder_near.csv"), index=False, encoding="utf-8-sig")
    print(f"\n3. {len(pairs)} over/under pairs checked: {len(arbs)} arbitrages, {len(near)} within {args.near:g}%")
    for r in pd.concat([arbs, near]).head(25).itertuples():
        tag = "ARB " if r.margin_pct < 0 else "near"
        mid = f" middle {r.middle:g}" if r.middle else ""
        print(f"  {tag} {r.margin_pct:+5.2f}%  {r.player:<22} {r.market:<11} O {r.line_o:g} {r.odds_o} "
              f"{BOOK[r.book_o]}{' (N+)' if r.kind_o == 'ladder' else ''} / U {r.line_u:g} {r.odds_u} "
              f"{BOOK[r.book_u]}{mid}")

    # 4. dominance: A pays more for Over L_low than another source pays for Over L_high (L_high > L_low)
    so = (df[df.side == "Over"].sort_values("odds", ascending=False)
            .drop_duplicates(["cplayer_id", "market", "src", "line"]))
    m = so.merge(so, on=["cplayer_id", "player", "market"], suffixes=("_a", "_b"))
    dom = m[(m.src_a != m.src_b) & (m.line_a < m.line_b) & (m.odds_a > m.odds_b)].copy()
    dom["ratio"] = (dom.odds_a / dom.odds_b).round(3)
    dom = dom.sort_values("ratio", ascending=False)[
        ["player", "market", "line_a", "odds_a", "book_a", "kind_a", "line_b", "odds_b", "book_b", "kind_b", "ratio"]]
    dom.to_csv(os.path.join(out, "ladder_dominance.csv"), index=False, encoding="utf-8-sig")
    print(f"\n4. Dominance gaps: {len(dom)} (a book pays more for an easier Over than another source for a harder one)")
    for r in dom.head(15).itertuples():
        print(f"  {r.player:<22} {r.market:<11} O {r.line_a:g} {r.odds_a} {BOOK[r.book_a]} ({r.kind_a})  >  "
              f"O {r.line_b:g} {r.odds_b} {BOOK[r.book_b]} ({r.kind_b})   x{r.ratio}")

    # 5. biggest single-price gaps vs fair (any line, any kind)
    top = ov[ov.ev > 0.05].sort_values("ev", ascending=False)
    print(f"\n5. Over prices 5%+ above the other sources' fair price: {len(top)}")
    for r in top.head(15).itertuples():
        print(f"  +{100 * r.ev:4.1f}%  {r.player:<22} {r.market:<11} O {r.line:g} ({r.dist} vs main {r.main:g}) "
              f"{r.odds} {BOOK[r.book]} {r.kind}  fair {1 / r.p:.2f}")
    print(f"\nCSVs -> {out}")


if __name__ == "__main__":
    main()
