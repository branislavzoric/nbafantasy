#!/usr/bin/env python3
"""
build_football_combos.py

Value combo bets on today's football (raw_combos.json from `scrape_football.py --combos`, plus the
basic markets in raw_odds.json):
  1. per match, the no-vig 1X2 and Total 2.5 probabilities averaged over the books -> goals model
     (combo_model.fit: home/away expected goals + Dixon-Coles draw correction)
  2. every combo selection parsed (combo_model.parse) and priced: model probability P
  3. each book's payout rate = median of odds x P over all its combos (a sanity check of the parsing:
     a book that pays ~90% everywhere is parsed right)
  4. market view per combo: the same bet at the other books, de-margined with each book's payout rate
  5. fair probability = average of the model and the market view (model only when no other book has it)
     edge = odds x fair - 1. Top picks: odds 1.50-15, edge >= 3%, model AND market both say value,
     edges above 25% held back as likely errors, at most 2 per match; plus a shorter-odds list (<= 4.00).

Usage:
  python build_football_combos.py [date]
Output: Odds/football/<date>/combos.json (+ combo_values.csv)
"""

import csv
import datetime as dt
import json
import os
import statistics
import sys
from collections import defaultdict

import combo_model as cm
from build_football_bets import BOOK_NAMES, FOOT_DIR, match_games, no_vig

MIN_ODDS, MAX_ODDS, MIN_EDGE, MAX_EDGE, PER_GAME, TOP, SHORT_MAX = 1.50, 15.0, 0.03, 0.25, 2, 15, 4.0
BANDS = (2.0, 4.0, 8.0, 15.0)
OUTLIER = 1.20             # combo-arb leg priced this far above every other book = likely a stale line


def band(odds):
    return sum(odds >= b for b in BANDS)


def describe(parsed, home, away):
    """Readable English for a parsed combo."""
    neg, atoms = parsed
    who = {"T": "", "H": f"{home} ", "A": f"{away} "}
    half = {"": "", "I": " in the 1st half", "II": " in the 2nd half"}
    res = {"1": f"{home} win", "X": "draw", "2": f"{away} win", "1X": f"{home} or draw", "X2": f"{away} or draw",
           "12": "no draw"}
    out = []
    for a in atoms:
        k = a[0]
        if k == "res":
            r = "".join(sorted(a[2], key="1X2".index))
            out.append(res[r] + ("" if not a[1] else " at half time" if a[1] == "I" else " in the 2nd half"))
        elif k == "tot":
            n = f"{a[3]}+" if a[4] == 99 else (f"{a[3]}" if a[3] == a[4] else f"{a[3]}-{a[4]}")
            if a[3] == 0 and a[4] != 99:
                n = f"0-{a[4]}"
            out.append(f"{who[a[2]]}{n} goals{half[a[1]]}".strip())
        elif k == "gg":
            out.append(("both teams score" if a[2] else "not both teams score") + half[a[1]])
        elif k == "htft":
            out.append(f"HT {a[1]} / FT {a[2]}")
        elif k == "dp":
            out.append(f"{home if a[1] == '1' else away} wins both halves")
        elif k == "hp":
            out.append(f"{home if a[1] == '1' else away} wins by 2+")
        elif k == "pg":
            out.append(f"{home if a[1] == '1' else away} scores first")
        elif k == "cmp":
            out.append({"<": "more goals in the 2nd half", "=": "same goals in both halves",
                        ">": "more goals in the 1st half"}[a[1]])
    s = " & ".join(out)
    return f"NOT ({s})" if neg else s


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else dt.date.today().isoformat()
    folder = os.path.join(FOOT_DIR, day)
    basic = json.load(open(os.path.join(folder, "raw_odds.json"), encoding="utf-8"))
    combos = json.load(open(os.path.join(folder, "raw_combos.json"), encoding="utf-8"))
    link, games, _ = match_games(basic["rows"] + combos["rows"])

    # 1. goals model per match from the books' no-vig basic markets
    prices = defaultdict(lambda: defaultdict(dict))
    for r in basic["rows"]:
        g = link.get((r["book"], r["league"], r["home"], r["away"]))
        if g and r["market"] in ("1X2", "Total 2.5"):
            prices[(g, r["book"])][r["market"]][r["sel"]] = r["odds"]
    probs = defaultdict(lambda: defaultdict(list))
    for (g, book), mk in prices.items():
        for market in ("1X2", "Total 2.5"):
            nv = no_vig(mk.get(market, {}), market)
            if nv:
                for s, p in nv.items():
                    probs[g][s].append(p)
    models = {}
    for g, ps in probs.items():
        if ps["1"] and ps["Over"]:
            models[g] = cm.fit(statistics.mean(ps["1"]), statistics.mean(ps["X"]), statistics.mean(ps["Over"]))
    print(f"{len(models)} matches modelled")

    # 2. parse + price every combo
    priced, skipped = [], defaultdict(int)
    parse_cache = {}
    for r in combos["rows"]:
        g = link.get((r["book"], r["league"], r["home"], r["away"]))
        if g not in models:
            continue
        ck = (r["book"], r["text"], r["ctx"])
        if ck not in parse_cache:
            parse_cache[ck] = cm.parse(r["text"], r["ctx"], r["book"])
        parsed = parse_cache[ck]
        if not parsed:
            skipped[r["book"]] += 1
            continue
        p = models[g].prob(parsed)
        if p <= 0.004 or p >= 0.995:
            continue
        priced.append({"game": g, "book": r["book"], "text": r["text"], "key": cm.key(parsed), "parsed": parsed,
                       "odds": r["odds"], "p": p})

    # 3. payout rate per book, overall and per odds band (books take bigger margins on long shots)
    payout = {b: statistics.median(x["odds"] * x["p"] for x in priced if x["book"] == b)
              for b in {x["book"] for x in priced}}
    bands = defaultdict(list)
    for x in priced:
        bands[(x["book"], band(x["odds"]))].append(x["odds"] * x["p"])
    band_pay = {k: statistics.median(v) if len(v) >= 30 else payout[k[0]] for k, v in bands.items()}
    for b in sorted(payout, key=lambda b: -payout[b]):
        n = sum(1 for x in priced if x["book"] == b)
        print(f"  {BOOK_NAMES[b]:10} {n:6} combos priced, {skipped[b]:5} skipped, payout {payout[b]:.1%}")

    # 4-5. market view + fair + edge
    by_key = defaultdict(list)
    for x in priced:
        by_key[(x["game"], x["key"])].append(x)
    for (g, k), xs in by_key.items():
        for x in xs:
            others = [y for y in xs if y["book"] != x["book"]]
            x["others"] = {y["book"]: y["odds"] for y in others}
            x["market_p"] = (statistics.median(band_pay.get((y["book"], band(y["odds"])), payout[y["book"]]) / y["odds"]
                                               for y in others) if others else None)
            x["fair_p"] = (x["p"] + x["market_p"]) / 2 if x["market_p"] else x["p"]
            x["edge"] = x["odds"] * x["fair_p"] - 1
            x["model_edge"] = x["odds"] * x["p"] - 1
            x["market_edge"] = x["odds"] * x["market_p"] - 1 if x["market_p"] else None

    cands = [x for x in priced if MIN_ODDS <= x["odds"] <= MAX_ODDS and x["edge"] >= MIN_EDGE
             and x["model_edge"] > 0 and (x["market_edge"] is None or x["market_edge"] > 0)]
    held = [x for x in cands if x["edge"] > MAX_EDGE]
    cands = sorted((x for x in cands if x["edge"] <= MAX_EDGE),
                   key=lambda x: -(x["edge"] * min(len(x["others"]) + 1, 4) / 4))
    def top(pool, n):
        out_, per_game, seen = [], defaultdict(int), set()
        for x in pool:
            if per_game[x["game"]] >= PER_GAME or (x["game"], x["key"]) in seen:
                continue
            per_game[x["game"]] += 1
            seen.add((x["game"], x["key"]))
            out_.append(x)
            if len(out_) == n:
                break
        return out_

    picks = top(cands, TOP)
    short = top([x for x in cands if x["odds"] <= SHORT_MAX], 10)

    def out(x):
        gm = games[x["game"]]
        return {"game": f'{gm["home"]} – {gm["away"]}', "league": gm["league"],
                "kickoff": gm["kickoff"].strftime("%H:%M"), "book": BOOK_NAMES[x["book"]], "tip": x["text"],
                "bet": describe(x["parsed"], gm["home"], gm["away"]), "odds": x["odds"],
                "fair_odds": round(1 / x["fair_p"], 2), "model_odds": round(1 / x["p"], 2),
                "edge_pct": round(100 * x["edge"], 1), "model_edge_pct": round(100 * x["model_edge"], 1),
                "market_edge_pct": None if x["market_edge"] is None else round(100 * x["market_edge"], 1),
                "others": {BOOK_NAMES[b]: o for b, o in sorted(x["others"].items(), key=lambda kv: -kv[1])}}

    # 6. combo arbitrages: a combo and its exact opposite ("GG&3+" / "NE GG&3+") at the best prices
    side = defaultdict(list)
    for x in priced:
        side[(x["game"], x["key"])].append(x)
    combo_arbs = []
    for (g, k), xs in side.items():
        if k.startswith("NOT ") or (g, "NOT " + k) not in side:
            continue
        legs = []
        for pool in (xs, side[(g, "NOT " + k)]):
            pool = sorted(pool, key=lambda x: -x["odds"])
            best_x, rest = pool[0], [y["odds"] for y in pool[1:]]
            legs.append((best_x, rest))
        inv = sum(1 / lx["odds"] for lx, _ in legs)
        if inv >= 1:
            continue
        gm = games[g]
        combo_arbs.append({
            "game": f'{gm["home"]} – {gm["away"]}', "league": gm["league"], "kickoff": gm["kickoff"].strftime("%H:%M"),
            "margin": round(1 / inv - 1, 4),
            "suspect": any(rest and lx["odds"] > OUTLIER * rest[0] for lx, rest in legs),
            "legs": [{"tip": lx["text"], "bet": describe(lx["parsed"], gm["home"], gm["away"]), "book": BOOK_NAMES[lx["book"]],
                      "odds": lx["odds"], "others": rest[:4], "outlier": bool(rest and lx["odds"] > OUTLIER * rest[0])}
                     for lx, rest in legs]})
    combo_arbs.sort(key=lambda a: (a["suspect"], -a["margin"]))
    print(f"{len(combo_arbs)} combo arbitrages ({sum(not a['suspect'] for a in combo_arbs)} clean)")
    for a in combo_arbs[:10]:
        print(f"  {'SUSPECT ' if a['suspect'] else ''}+{a['margin']:.2%} {a['kickoff']} {a['game'][:32]:32} " +
              " | ".join(f"{l['tip']} {l['odds']} @{l['book']}" for l in a["legs"]))

    model_info = {g: {"match": f'{games[g]["home"]} – {games[g]["away"]}', "xg_home": round(m.lam_h, 2),
                      "xg_away": round(m.lam_a, 2), "rho": round(m.rho, 3), "fit_error": round(m.fit_error, 4)}
                  for g, m in models.items()}
    result = {"date": day, "built": dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "scraped": combos["scraped"],
              "n_games": len(models), "n_priced": len(priced),
              "books": {BOOK_NAMES[b]: {"priced": sum(1 for x in priced if x["book"] == b), "skipped": skipped[b],
                                        "payout": round(payout[b], 4)} for b in payout},
              "models": model_info, "picks": [out(x) for x in picks], "short": [out(x) for x in short], "arbs": combo_arbs, "held_back": [out(x) for x in held[:10]]}
    with open(os.path.join(folder, "combos.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    # every priced combo at ticket-leg odds, for build_football_tickets.py
    legs = [{**out(x), "game_id": x["game"], "key": x["key"], "fair_p": round(x["fair_p"], 4), "model_p": round(x["p"], 4),
             "book_id": x["book"]} for x in priced if 1.20 <= x["odds"] <= 12.0]
    with open(os.path.join(folder, "combo_legs.json"), "w", encoding="utf-8") as f:
        json.dump(legs, f, ensure_ascii=False)
    with open(os.path.join(folder, "combo_values.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["game", "book", "tip", "key", "odds", "model_p", "market_p", "fair_p", "edge_pct"])
        for x in sorted(priced, key=lambda x: -x["edge"]):
            gm = games[x["game"]]
            w.writerow([f'{gm["home"]} – {gm["away"]}', x["book"], x["text"], x["key"], x["odds"], round(x["p"], 4),
                        "" if x["market_p"] is None else round(x["market_p"], 4), round(x["fair_p"], 4),
                        round(100 * x["edge"], 2)])
    for i, x in enumerate(result["picks"], 1):
        print(f"{i:2}. {x['game'][:34]:34} {x['tip']:16} {x['odds']:6.2f} {x['book']:9} fair {x['fair_odds']:5.2f} "
              f"edge {x['edge_pct']:+5.1f}% (model {x['model_edge_pct']:+.1f}%, market {x['market_edge_pct']}) | {x['bet']}")
    print("Shorter odds (<= 4.00):")
    for i, x in enumerate(result["short"], 1):
        print(f"{i:2}. {x['game'][:34]:34} {x['tip']:16} {x['odds']:6.2f} {x['book']:9} fair {x['fair_odds']:5.2f} "
              f"edge {x['edge_pct']:+5.1f}% (model {x['model_edge_pct']:+.1f}%, market {x['market_edge_pct']}) | {x['bet']}")
    if held:
        print(f"{len(held)} held back (edge > {MAX_EDGE:.0%})")


if __name__ == "__main__":
    main()
