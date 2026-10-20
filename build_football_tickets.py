#!/usr/bin/env python3
"""
build_football_tickets.py

Football tickets for the Against the Odds page, from today's top-league odds:
  - basic markets (raw_odds.json: 1X2, double chance, Total 2.5, BTTS), fair price = leave-one-out no-vig
    consensus of the other books (build_football_bets.analyse)
  - combos (combo_legs.json from build_football_combos.py), fair price = average of the goals model and the
    other books' margin-adjusted prices

Tickets (each at ONE bookmaker, one leg per match, no bet used on two tickets):
  safe4 / safe5   highest win chance, total odds in a safe band, value >= -4%
  long4 / long5   total odds 20-100, best value
  epl4            Premier League only, 4 legs, best value with total odds >= 5
  dc4 / dc5 / dc6 legs are "1X & 1-3 goals in each half" or "X2 & 1-3 goals in each half"; highest win chance
                  with value >= -10%
Extrabet top 10 (Soccerbet bet builder, odds 4-10, 5 from the Premier League) from
Odds/extrabet/<date>/today_picks.json (extrabet_value.py --leagues ..., then extrabet_today.py).

Usage:
  python build_football_tickets.py [date]
Output: Odds/tonight/<date>/football_card.json
"""

import datetime as dt
import itertools
import json
import math
import os
import sys
from collections import defaultdict

import build_football_bets as fb

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
BOOKS = fb.BOOK_NAMES
MAX_EDGE = 0.15            # bigger edges are usually stale or mis-parsed lines: not used on tickets
DC_KEYS = {"1X&I1-3&II1-3": "1X", "I1-3&II1-3&X2": "X2"}
TOP_LEAGUES = ["Premier League", "LaLiga", "Serie A", "Bundesliga", "Ligue 1", "Eredivisie", "Primeira Liga"]


def load_legs(day):
    folder = os.path.join(fb.FOOT_DIR, day)
    raw = json.load(open(os.path.join(folder, "raw_odds.json"), encoding="utf-8"))
    games, bets, _, _ = fb.analyse(raw["rows"])
    games = {k: g for k, g in games.items() if g["league"] in TOP_LEAGUES}
    name_to_id = {f'{g["home"]} – {g["away"]}': k for k, g in games.items()}
    legs = []
    for b in bets:
        if b["game"] not in games:
            continue
        g = games[b["game"]]
        legs.append({"game_id": b["game"], "match": f'{g["home"]} – {g["away"]}', "league": g["league"],
                     "kickoff": g["kickoff"].strftime("%Y-%m-%d %H:%M"), "book": b["book"],
                     "key": f'{b["market"]}:{b["sel"]}', "bet": fb.label(b, g), "odds": b["odds"],
                     "p": b["prob"], "edge": b["edge_pct"] / 100, "kind": "basic"})
    for x in json.load(open(os.path.join(folder, "combo_legs.json"), encoding="utf-8")):
        gid = name_to_id.get(x["game"])
        if not gid:
            continue
        g = games[gid]
        legs.append({"game_id": gid, "match": x["game"], "league": g["league"],
                     "kickoff": g["kickoff"].strftime("%Y-%m-%d %H:%M"), "book": x["book_id"], "key": x["key"],
                     "bet": x["bet"], "odds": x["odds"], "p": x["fair_p"], "edge": x["edge_pct"] / 100,
                     "kind": "combo", "tip": x["tip"]})
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    legs = [l for l in legs if l["kickoff"] > now and l["edge"] <= MAX_EDGE]
    return games, legs, raw["scraped"]


def search(legs, n, lo, hi, objective, min_ev, used, pool=20, sort_key=None):
    """Best n-leg ticket at one book: one leg per match, total odds in [lo, hi], EV >= min_ev."""
    best = None
    by_book = defaultdict(list)
    for l in legs:
        if (l["game_id"], l["key"]) not in used:
            by_book[l["book"]].append(l)
    for book, ls in by_book.items():
        ls.sort(key=sort_key or (lambda l: -l["p"]))
        cand, seen = [], defaultdict(int)
        for l in ls:                                  # at most 3 options per match in the pool
            if seen[l["game_id"]] < 3:
                seen[l["game_id"]] += 1
                cand.append(l)
            if len(cand) == pool:
                break
        for combo in itertools.combinations(cand, n):
            if len({l["game_id"] for l in combo}) < n:
                continue
            odds = math.prod(l["odds"] for l in combo)
            if not lo <= odds <= hi:
                continue
            hit = math.prod(l["p"] for l in combo)
            ev = odds * hit - 1
            if ev < min_ev:
                continue
            score = objective(hit, ev, odds)
            if best is None or score > best[0]:
                best = (score, book, combo, odds, hit, ev)
    if not best:
        return None
    _, book, combo, odds, hit, ev = best
    for l in combo:
        used.add((l["game_id"], l["key"]))
    legs_out = sorted(combo, key=lambda l: l["kickoff"])
    return {"book": BOOKS[book], "total_odds": round(odds, 2), "hit_prob": hit, "ev": ev,
            "legs": [{"match": l["match"], "league": l["league"], "kickoff": l["kickoff"], "bet": l["bet"],
                      "odds": l["odds"], "fair_odds": round(1 / l["p"], 2), "edge_pct": round(100 * l["edge"], 1),
                      "book": BOOKS[book], **({"tip": l["tip"]} if l.get("tip") else {})} for l in legs_out]}


def safe(hit, ev, odds):
    return hit


def value(hit, ev, odds):
    return ev


def ticket(label, name, legs, n, lo, hi, obj, min_evs, used, **kw):
    for m in min_evs:                                 # relax the value floor only if nothing qualifies
        t = search(legs, n, lo, hi, obj, m, used, **kw)
        if t:
            print(f"{label}: {t['book']} {t['total_odds']} win {100 * t['hit_prob']:.1f}% EV {100 * t['ev']:+.1f}%")
            for l in t["legs"]:
                print(f"    {l['kickoff'][-5:]} {l['match']:38} {l['bet']:55} {l['odds']:.2f} (fair {l['fair_odds']})")
            return {"label": label, "name": name, "range": [lo, hi] if hi < 1000 else None, **t}
    print(f"{label}: none")
    return None


def describe_xb(t, sel, home, away):
    """Readable English for a Soccerbet Extrabet selection (type + 'k=v,...' string)."""
    a = dict(kv.split("=", 1) for kv in sel.split(",") if "=" in kv)
    parts = []
    if "team" in a:
        parts.append(f'{a["team"]} win')
    if "ftht" in a:
        h, f = a["ftht"].split("-")
        side = {"1": home, "2": away, "X": "draw"}
        parts.append(f'half-time/full-time {h}/{f} ({side.get(h, h)} / {side.get(f, f)})')
    if "total" in a:
        parts.append(f'{"over" if a.get("order", "+") == "+" else "under"} {a["total"]} goals')
    if "BTS" in t:
        parts.append("both teams score")
    if "PLFG" in t:
        parts.append(f'{a["participant"]} scores the first goal')
    elif "participant1" in a:
        parts.append(f'{a["participant1"]} and {a["participant2"]} both score')
    elif "participant" in a:
        parts.append(f'{a["participant"]} scores')
    return " + ".join(parts)


def extrabet_top(day, games, n=10, epl=5, lo=4.0, hi=10.0):
    """Soccerbet Extrabet combos at odds lo-hi, ranked by return (model chance x odds) from extrabet_today.py,
    n picks with `epl` from the Premier League, one per match."""
    f = os.path.join(PROJECT_DIR, "Odds", "extrabet", day, "today_picks.json")
    if not os.path.exists(f):
        return []
    rows = json.load(open(f, encoding="utf-8"))["qualifiers"]
    out = []
    for r in rows:
        if not lo <= r["odds"] <= hi:
            continue
        home, away = r["match"].split(" - ", 1)
        best = max(games.values(), key=lambda g: fb.sim(home, g["home"]) + fb.sim(away, g["away"]))
        if fb.sim(home, best["home"]) + fb.sim(away, best["away"]) < 0.8:
            continue
        out.append({"match": f'{best["home"]} – {best["away"]}', "league": best["league"],
                    "kickoff": best["kickoff"].strftime("%Y-%m-%d %H:%M"),
                    "bet": describe_xb(r["type"], r["selection"], best["home"], best["away"]),
                    "type": r["type"], "selection": r["selection"], "odds": r["odds"], "fair": round(1 / r["prob"], 2),
                    "ev": round(r["return"] - 1, 3), "needs_lineup": r["needs_lineup"]})
    picked, per_match = [], defaultdict(int)
    for pool, k in (([x for x in out if x["league"] == "Premier League"], epl),
                    ([x for x in out if x["league"] != "Premier League"], n - epl)):
        got = 0
        for x in pool:
            if got == k:
                break
            if per_match[x["match"]] >= 1:
                continue
            per_match[x["match"]] += 1
            picked.append(x)
            got += 1
    picked.sort(key=lambda x: -x["ev"])
    return picked


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    day = sys.argv[1] if len(sys.argv) > 1 else dt.date.today().isoformat()
    games, legs, scraped = load_legs(day)
    print(f"{len(games)} matches, {len(legs)} legs")
    general = [l for l in legs if 1.25 <= l["odds"] <= 3.5]
    safe_legs = [l for l in general if 1.40 <= l["odds"] <= 2.2 and l["edge"] >= -0.02]
    used = set()
    tickets = [
        ticket("4-leg · safer", "safe", safe_legs, 4, 4.0, 12.0, safe, (-0.04, -0.08), used, pool=28),
        ticket("4-leg · long shot", "long", general, 4, 20.0, 100.0, value, (0.0, -0.1), used, sort_key=lambda l: -l["edge"]),
        ticket("5-leg · safer", "safe", safe_legs, 5, 6.0, 20.0, safe, (-0.04, -0.08), used, pool=24),
        ticket("5-leg · long shot", "long", general, 5, 20.0, 100.0, value, (0.0, -0.1), used, sort_key=lambda l: -l["edge"]),
        ticket("Premier League · 4-leg", "epl", [l for l in general if l["league"] == "Premier League"], 4, 5.0, 60.0,
               value, (0.0, -0.06, -0.12), used, pool=15, sort_key=lambda l: -l["edge"]),
    ]
    dc = [l for l in legs if l["key"] in DC_KEYS]
    dc_tickets = [ticket(f"{n}-leg · 1X/X2 + 1-3 goals each half", "dc", dc, n, 1.0, 10000.0, safe,
                         (-0.06, -0.15, -0.25, -0.4), used, pool=24) for n in (4, 5, 6)]
    for t in dc_tickets:
        if t:
            t["range"] = None
    xb = extrabet_top(day, games)
    print("Extrabet:")
    for x in xb:
        print(f"  {x['league']:15} {x['match']:35} {x['bet']:60} {x['odds']:.2f} fair {x['fair']:.2f} ev {x['ev']:+.1%}")
    lc = defaultdict(int)
    for g in games.values():
        lc[g["league"]] += 1
    out = {"date": day, "built": scraped, "leagues": sorted(lc.items(), key=lambda kv: -kv[1]), "n_games": len(games),
           "tickets": [t for t in tickets if t], "dc_tickets": [t for t in dc_tickets if t], "extrabet": xb}
    folder = os.path.join(PROJECT_DIR, "Odds", "tonight", day)
    os.makedirs(folder, exist_ok=True)
    json.dump(out, open(os.path.join(folder, "football_card.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("->", os.path.join(folder, "football_card.json"))


if __name__ == "__main__":
    main()
