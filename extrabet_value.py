#!/usr/bin/env python3
"""
extrabet_value.py

Is there value in Soccerbet's "Extrabet" (bet-builder combos) for Champions League matches?

1. Scrape, per UCL match (Soccerbet public JSON API, same as odds_scrapers/dualsoft.py):
     /match/<id>                                  main markets (1X2, totals, team goals, BTTS, halves)
     /super/match/<matchCode>/mob?leagueCategory=PL   player markets (anytime / first scorer)
     /super/match/<matchCode>/mob?leagueCategory=MT   Extrabet combos
2. Fit a Dixon-Coles Poisson model (home rate, away rate, rho, first-half share) to the de-vigged
   main markets.
3. Each player's share of his team's goals (q) from Soccerbet's anytime-scorer odds with an assumed
   margin (--scorer-margin, default 0 = their prices taken as fair: the bettor-friendly bound).
4. Price every goal-based combo exactly by enumerating scores (given team goals h, each goal is the
   player's with prob q, independently; first-half goals ~ Binomial(h, f)) -> fair odds and
   EV = price x p - 1.
Combos with shots / corners / cards / fouls / passes / assists need models we don't have; they
are counted but not priced.

The fair prices come from Soccerbet's OWN main markets, so this finds Extrabet prices that are
inconsistent with the book's singles, not prices that beat the whole market.

Usage:
  python extrabet_value.py                 # today's + tomorrow's not-started UCL matches
  python extrabet_value.py --offline DIR   # re-analyse saved raw JSON
Output: Odds/extrabet/<date>/raw/*.json, extrabet_priced.csv, summary printed
"""

import argparse
import csv
import datetime as dt
import json
import math
import os
import re
import sys
import time
import unicodedata

import numpy as np
import requests
from scipy.optimize import brentq, minimize
from scipy.stats import poisson

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE = "https://www.soccerbet.rs/restapi/offer/sr"
PARAMS = {"annex": "0", "desc": "true", "mobileVersion": "2.0.0", "locale": "sr"}
UCL_LEAGUE = 2519992
DELAY_S = 1.5
MAXG = 10
# Scorer odds are priced per player as if he plays (bets on players who don't play are void), so
# they can't be normalised to the team's goal total. Instead a margin on them is assumed:
# 0 = take Soccerbet's own anytime prices as fair (the most bettor-friendly assumption).
SCORER_MARGIN = 0.0

# Main-market groups used for the fit: each group is mutually exclusive + exhaustive.
# predicate(h, a, h1, a1) on full-time and first-half goals.
GROUPS = [
    {"KI_1": lambda h, a: h > a, "KI_X": lambda h, a: h == a, "KI_2": lambda h, a: h < a},
    {"G0_2": lambda h, a: h + a <= 2, "G3_PLUS": lambda h, a: h + a >= 3},
    {"G0_1": lambda h, a: h + a <= 1, "G2_PLUS": lambda h, a: h + a >= 2},
    {"G0_3": lambda h, a: h + a <= 3, "G4_PLUS": lambda h, a: h + a >= 4},
    {"GGG": lambda h, a: h > 0 and a > 0, "GNG": lambda h, a: h == 0 or a == 0},
    {"G_DAJE_DOMACIN": lambda h, a: h > 0, "G_NEDAJE_DOMACIN": lambda h, a: h == 0},
    {"G_DAJE_GOST": lambda h, a: a > 0, "G_NEDAJE_GOST": lambda h, a: a == 0},
    {"GH_0_1": lambda h, a: h <= 1, "GH_2_PLUS": lambda h, a: h >= 2},
    {"GA_0_1": lambda h, a: a <= 1, "GA_2_PLUS": lambda h, a: a >= 2},
]
HALF_GROUPS = [  # first-half / second-half total goals
    ("G1P0", "G1P1_PLUS", 1), ("G2P0", "G2P1_PLUS", 2),
]

# -----------------------------------------------------------------------------
# SCRAPE
# -----------------------------------------------------------------------------

def get(s, path, **extra):
    r = s.get(f"{BASE}/{path}", params={**PARAMS, **extra}, timeout=30)
    r.raise_for_status()
    time.sleep(DELAY_S)
    return r.json()


def scrape(raw_dir, leagues=(UCL_LEAGUE,)):
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0"
    os.makedirs(raw_dir, exist_ok=True)
    ttg = get(s, "ttg_lang")
    json.dump(ttg["betPickMap"], open(os.path.join(raw_dir, "ttg.json"), "w", encoding="utf-8"), ensure_ascii=False)
    now = time.time() * 1000
    matches = [m for m in get(s, "sport/S/mob")["esMatches"]
               if m["leagueId"] in leagues and m["kickOffTime"] > now]
    print(f"{len(matches)} matches not started")
    for m in matches:
        code = m["matchCode"]
        main = get(s, f"match/{m['id']}")
        pl = get(s, f"super/match/{code}/mob", leagueCategory="PL")
        mt = get(s, f"super/match/{code}/mob", leagueCategory="MT")
        json.dump({"main": main, "pl": pl, "mt": mt}, open(os.path.join(raw_dir, f"{code}.json"), "w", encoding="utf-8"),
                  ensure_ascii=False)
        n = sum(len(v) for x in (mt or {}).get("esMatches") or [] for v in x["betMap"].values())
        print(f"  {code} {m['home']} - {m['away']}: {n} extrabet prices")

# -----------------------------------------------------------------------------
# MODEL
# -----------------------------------------------------------------------------

def dc_grid(lh, la, rho):
    ph, pa = poisson.pmf(np.arange(MAXG + 1), lh), poisson.pmf(np.arange(MAXG + 1), la)
    g = np.outer(ph, pa)
    g[0, 0] *= 1 - lh * la * rho
    g[0, 1] *= 1 + lh * rho
    g[1, 0] *= 1 + la * rho
    g[1, 1] *= 1 - rho
    return g / g.sum()


def prices_by_name(bm, ttg):
    out = {}
    for code, svs in bm.items():
        name = (ttg.get(f"{code}_S") or {}).get("tipTypeName")
        for sv, o in svs.items():
            if o.get("s") == "U" and o.get("ov", 0) > 1.0 and name:
                out.setdefault(name, {})[sv] = o["ov"]
    return out


def fit_match(main, ttg):
    """-> (lh, la, rho, f, fit_error, n_groups)"""
    p = prices_by_name(main["betMap"], ttg)
    single = {k: v.get("NULL") for k, v in p.items()}
    targets = []
    for g in GROUPS:
        if all(single.get(k) for k in g):
            inv = {k: 1 / single[k] for k in g}
            tot = sum(inv.values())
            targets.append([(g[k], inv[k] / tot) for k in g])
    halves = []
    for k0, k1, half in HALF_GROUPS:
        if single.get(k0) and single.get(k1):
            i0, i1 = 1 / single[k0], 1 / single[k1]
            halves.append((half, i0 / (i0 + i1)))
    H, A = np.meshgrid(np.arange(MAXG + 1), np.arange(MAXG + 1), indexing="ij")
    masks = [[(np.vectorize(fn)(H, A), t) for fn, t in grp] for grp in targets]

    def loss(x):
        lh, la, rho = math.exp(x[0]), math.exp(x[1]), x[2]
        if not -0.25 < rho < 0.25:
            return 1e3
        g = dc_grid(lh, la, rho)
        return sum((g[m].sum() - t) ** 2 for grp in masks for m, t in grp)

    best = minimize(loss, [math.log(1.5), math.log(1.1), 0.0], method="Nelder-Mead",
                    options={"xatol": 1e-6, "fatol": 1e-10, "maxiter": 4000})
    lh, la, rho = math.exp(best.x[0]), math.exp(best.x[1]), best.x[2]
    # first-half share f: P(no goals in half) = exp(-share * (lh + la))
    f = 0.45
    if halves:
        def hl(fr):
            return sum((math.exp(-(fr if half == 1 else 1 - fr) * (lh + la)) - t) ** 2 for half, t in halves)
        f = minimize(lambda x: hl(x[0]), [0.45], bounds=[(0.3, 0.6)], method="L-BFGS-B").x[0]
    rms = math.sqrt(best.fun / max(sum(len(g) for g in masks), 1))
    return lh, la, rho, f, rms, len(targets)


def norm(name):
    return re.sub(r"[^a-z ]", "", unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()).split()


def player_shares(pl, ttg, home, away, grid, margin):
    """{normalised name tokens tuple: (team side, q, display, margin)} from anytime-scorer odds."""
    raw = {"h": [], "a": []}
    for x in (pl or {}).get("esMatches") or []:
        side = "h" if x["away"] == home else "a" if x["away"] == away else None
        if not side:
            continue
        p = prices_by_name(x["betMap"], ttg)
        odds = next((v for k in ("PL_1_PLUS_SSUB", "PL_1_PLUS") for v in (p.get(k) or {}).values()), None)
        if odds:
            raw[side].append((x["home"], 1 / odds))
    hdist, adist = grid.sum(axis=1), grid.sum(axis=0)
    n = np.arange(MAXG + 1)
    out = {}
    for side, dist in (("h", hdist), ("a", adist)):
        if not raw[side]:
            continue

        def q_of(prob):   # P(player scores) = sum_h P(h) (1 - (1-q)^h)
            if prob >= 1 - dist[0] - 1e-9:
                return 1.0
            return brentq(lambda q: (dist * (1 - (1 - q) ** n)).sum() - prob, 0, 1)

        for name, pr in raw[side]:
            out[tuple(norm(name))] = (side, q_of(pr / (1 + margin)), name, margin)
    return out


def find_player(name, shares):
    toks = norm(name)
    if tuple(toks) in shares:
        return shares[tuple(toks)]
    cands = [v for k, v in shares.items() if k and k[-1] == toks[-1] and k[0][0] == toks[0][0]]
    if len(cands) == 1:
        return cands[0]
    cands = [v for k, v in shares.items() if set(k) <= set(toks) or set(toks) <= set(k)]
    return cands[0] if len(cands) == 1 else None

# -----------------------------------------------------------------------------
# COMBO PRICING (exact enumeration)
# -----------------------------------------------------------------------------

def parse_sv(sv):
    return dict(kv.split("=", 1) for kv in sv.split(",") if "=" in kv)


def result_ok(team, h, a, home, away):
    return (team == home and h > a) or (team == away and a > h) or (team in ("X", "x", "draw") and h == a)


def p_scores(q, n):              # player scores at least once among his team's n goals
    return 1 - (1 - q) ** n


def p_both(q1, q2, n):           # two players of the same team both score
    return 1 - (1 - q1) ** n - (1 - q2) ** n + max(1 - q1 - q2, 0) ** n


def price_combo(tt, sv, grid, f, home, away, shares):
    """Fair probability of an Extrabet combo, or (None, reason)."""
    a_ = parse_sv(sv)
    pl = []
    for key in ("participant", "participant1", "participant2"):
        if key in a_ and a_[key] != "dummy":
            info = find_player(a_[key], shares)
            if not info:
                return None, f"no scorer price for {a_[key]}"
            pl.append(info)
    over = lambda tot, h, a: (h + a > float(a_["total"])) if a_.get("order", "+") == "+" else (h + a < float(a_["total"]))

    def goal_prob(h, a, need_first=False):
        """P(all listed players score [/ the single player scores first]) given (h, a)."""
        if need_first:
            side, q = pl[0][0], pl[0][1]
            n_team, n_all = (h if side == "h" else a), h + a
            return 0.0 if n_all == 0 else n_team / n_all * q
        if len(pl) == 1:
            side, q = pl[0][:2]
            return p_scores(q, h if side == "h" else a)
        (s1, q1), (s2, q2) = pl[0][:2], pl[1][:2]
        if s1 == s2:
            return p_both(q1, q2, h if s1 == "h" else a)
        return p_scores(q1, h if s1 == "h" else a) * p_scores(q2, h if s2 == "h" else a)

    first = tt in ("FT_PLFG", "TR_PLFG", "FT_OUG_PLFG")
    if tt in ("FT_PLG", "FT_PLFG", "FT_PLG_PLG", "PLG_PLG", "OUG_PLG", "OUG_PLG_PLG", "FT_OUG_PLG",
              "FT_OUG_PLFG", "FT_BTS_PLG", "TR_PL_1PLUS", "TR_PLFG", "HTFT_PLG"):
        if tt == "PLG_PLG" and len(pl) == 2 and pl[0][2] == pl[1][2]:
            return None, "same player twice"
        tot = 0.0
        for h in range(MAXG + 1):
            for a in range(MAXG + 1):
                w = grid[h, a]
                if w < 1e-12:
                    continue
                if "team" in a_ and tt.startswith("FT") and not result_ok(a_["team"], h, a, home, away):
                    continue
                if tt.startswith("TR_"):
                    sh, sa = map(int, a_["score"].split(":"))
                    if (h, a) != (sh, sa):
                        continue
                if "OUG" in tt and not over(None, h, a):
                    continue
                if tt == "FT_BTS_PLG" and not (h > 0 and a > 0):
                    continue
                gp = goal_prob(h, a, first)
                if tt == "HTFT_PLG":
                    ht, ft = a_["ftht"].split("-")
                    if not result_ok({"1": home, "2": away}.get(ft, "X"), h, a, home, away):
                        continue
                    # P(half-time result) with h1 ~ Bin(h, f), a1 ~ Bin(a, f); independent of who scores
                    from scipy.stats import binom
                    bh, ba = binom.pmf(np.arange(h + 1), h, f), binom.pmf(np.arange(a + 1), a, f)
                    ph = sum(bh[i] * ba[j] for i in range(h + 1) for j in range(a + 1)
                             if (ht == "1" and i > j) or (ht == "2" and j > i) or (ht.upper() == "X" and i == j))
                    gp *= ph
                tot += w * gp
        return tot, None
    return None, "not modelled"

# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------

def analyse(raw_dir, out_csv, margin=SCORER_MARGIN):
    ttg = json.load(open(os.path.join(raw_dir, "ttg.json"), encoding="utf-8"))
    rows, skipped, fits = [], {}, []
    for fn in sorted(os.listdir(raw_dir)):
        if not fn[0].isdigit():
            continue
        d = json.load(open(os.path.join(raw_dir, fn), encoding="utf-8"))
        main = d["main"]
        home, away = main["home"], main["away"]
        kick = dt.datetime.fromtimestamp(main["kickOffTime"] / 1000)
        lh, la, rho, f, rms, ngr = fit_match(main, ttg)
        grid = dc_grid(lh, la, rho)
        shares = player_shares(d["pl"], ttg, home, away, grid, margin)
        margins = sorted({round(v[3], 3) for v in shares.values()})
        fits.append((home, away, lh, la, rho, f, rms, ngr, margins))
        for x in (d["mt"] or {}).get("esMatches") or []:
            for code, svs in x["betMap"].items():
                tt = (ttg.get(f"{code}_S") or {}).get("tipTypeName")
                for sv, o in svs.items():
                    if o.get("s") != "U" or o.get("ov", 0) <= 1.0:
                        continue
                    p, why = price_combo(tt, sv, grid, f, home, away, shares)
                    if p is None:
                        skipped[tt if why == "not modelled" else why.split(" for ")[0]] = \
                            skipped.get(tt if why == "not modelled" else why.split(" for ")[0], 0) + 1
                        continue
                    if p <= 0:
                        continue
                    rows.append({"kickoff": kick.strftime("%a %d.%m %H:%M"), "match": f"{home} - {away}",
                                 "type": tt, "selection": sv, "price": o["ov"], "fair_odds": round(1 / p, 2),
                                 "prob": round(p, 4), "ev": round(o["ov"] * p - 1, 4)})
    rows.sort(key=lambda r: -r["ev"])
    with open(out_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["match"])
        w.writeheader()
        w.writerows(rows)
    return rows, skipped, fits


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", help="raw dir to re-analyse")
    ap.add_argument("--scorer-margin", type=float, default=SCORER_MARGIN,
                    help="assumed margin on the anytime-scorer odds (0 = take them as fair)")
    ap.add_argument("--leagues", help="comma-separated Soccerbet league ids (default: UCL)")
    args = ap.parse_args()
    day = dt.date.today().isoformat()
    out_dir = os.path.join(PROJECT_DIR, "Odds", "extrabet", day)
    raw_dir = args.offline or os.path.join(out_dir, "raw")
    if not args.offline:
        scrape(raw_dir, tuple(int(x) for x in args.leagues.split(",")) if args.leagues else (UCL_LEAGUE,))
    os.makedirs(out_dir, exist_ok=True)
    rows, skipped, fits = analyse(raw_dir, os.path.join(out_dir, "extrabet_priced.csv"), args.scorer_margin)
    print(f"\nScorer odds margin assumed: {args.scorer_margin:.0%}")

    print("\nModel fits (goals: home/away, Dixon-Coles rho, first-half share, RMS error):")
    for home, away, lh, la, rho, f, rms, ngr, m in fits:
        print(f"  {home:>20} - {away:<20} {lh:.2f}/{la:.2f} rho {rho:+.3f} f {f:.2f} rms {rms:.3f} "
              f"({ngr} groups)")
    evs = np.array([r["ev"] for r in rows])
    print(f"\nPriced {len(rows)} combos; median EV {np.median(evs):+.1%}, mean {evs.mean():+.1%}, "
          f"{(evs > 0).sum()} positive, {(evs > 0.05).sum()} above +5%")
    by_type = {}
    for r in rows:
        by_type.setdefault(r["type"], []).append(r["ev"])
    print("\nMedian EV by combo type (= Soccerbet's effective margin on it):")
    for t, v in sorted(by_type.items(), key=lambda kv: -np.median(kv[1])):
        print(f"  {t:14} n={len(v):4}  median {np.median(v):+.1%}  best {max(v):+.1%}")
    print(f"\nNot priced: {sum(skipped.values())} -> " + ", ".join(f"{k} {v}" for k, v in sorted(skipped.items(), key=lambda kv: -kv[1])[:20]))
    print("\nTop 25 by EV:")
    for r in rows[:25]:
        print(f"  {r['ev']:+.1%}  {r['price']:>7} vs fair {r['fair_odds']:>7}  {r['kickoff']}  {r['match']}  {r['type']}  {r['selection']}")


if __name__ == "__main__":
    main()
