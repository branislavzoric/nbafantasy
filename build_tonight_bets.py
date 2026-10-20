#!/usr/bin/env python3
"""
build_tonight_bets.py

Tonight's combined card across three sources:
  - EuroLeague game lines   (Winner / Handicap / Total incl. OT, odds_scrapers/basketball_lines.py)
  - EuroLeague player props (odds.compare in Postgres: scrape_odds.py + compare_odds.py)
  - football: Champions League + Championship (Odds/football/<date>/raw_all.json from scrape_football.py --all)

Same method everywhere: fair price = leave-one-out no-vig consensus of the OTHER books (3+ books),
edge = odds x fair probability - 1. Output:
  - value picks: top 10 overall (one per game) + the top 5 of each source
  - arbitrages: best price per outcome across books, positive profit only (football 1X2 is 3-way)
  - 4-, 5- and 6-leg slips: each ticket at ONE bookmaker, one leg per game, and no bet (and no player)
    is used on more than one ticket

Usage:
  python build_tonight_bets.py [--lines-only-scrape] [--min4 8] [--min5 12] [--min6 20]
Output: Odds/tonight/<date>/tonight.json (+ printed summary)
"""

import argparse
import csv
import datetime as dt
import itertools
import json
import math
import os
from collections import defaultdict

import build_football_bets as fb
from odds_scrapers.basketball_lines import NBA_SCRAPERS, SCRAPERS as LINE_SCRAPERS

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(PROJECT_DIR, "Odds", "tonight")
FOOTBALL = {"Liga Šampiona": "Champions League", "Engleska 2": "Championship"}   # Soccerbet names = fixture list
BOOKS = fb.BOOK_NAMES
PICK_ODDS = (1.40, 4.00)
LEG_ODDS = (1.40, 2.60)
MAX_EDGE = 0.15          # bigger edges are usually a stale or mis-keyed line -> listed as "check", not used
TOP = 18                 # candidate legs per book for the slip search

# -----------------------------------------------------------------------------
# LOAD
# -----------------------------------------------------------------------------

def scrape_lines(day, scrapers=None, sub="el_lines"):
    rows, status = [], {}
    for book, fn in (scrapers or LINE_SCRAPERS).items():
        try:
            got = fn()
            status[book] = f"{len(got)} odds, {len({(r['home'], r['away']) for r in got})} games"
            rows += got
        except Exception as e:                       # one broken site shouldn't stop the others
            status[book] = f"FAILED: {e}"
    for r in rows:
        r["kickoff"] = r["kickoff"].isoformat(sep=" ")
    folder = os.path.join(PROJECT_DIR, "Odds", sub, day)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "raw_lines.json"), "w", encoding="utf-8") as f:
        json.dump({"status": status, "rows": rows}, f, ensure_ascii=False)
    return rows, status


def load_football(day):
    """Today's UCL + Championship rows from every book, matched onto Soccerbet's fixture list
    (Superbet/Balkanbet only label leagues by id, so leagues are matched by teams + kickoff)."""
    path = os.path.join(PROJECT_DIR, "Odds", "football", day, "raw_all.csv")
    if not os.path.exists(path):                          # football not scraped today
        return []
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    now = dt.datetime.now()
    fixtures = {}
    for r in rows:
        if r["book"] == "soccerbet" and r["league"] in FOOTBALL:
            fixtures[(r["home"], r["away"])] = (FOOTBALL[r["league"]], dt.datetime.fromisoformat(r["kickoff"]))
    fixtures = {k: v for k, v in fixtures.items() if v[1] > now}
    games = {(r["book"], r["league"], r["home"], r["away"]): dt.datetime.fromisoformat(r["kickoff"]) for r in rows}
    assign = {}
    for book in {g[0] for g in games}:
        scored = []
        for (b, l, h, a), k in games.items():
            if b != book:
                continue
            for (fh, fa), (league, fk) in fixtures.items():
                if abs((fk - k).total_seconds()) <= 1800:
                    scored.append((fb.sim(h, fh) + fb.sim(a, fa), (b, l, h, a), (fh, fa), league))
        done, taken = set(), set()
        for score, g, fx, league in sorted(scored, key=lambda x: -x[0]):
            if g in done or fx in taken or score < 1.1:
                continue
            done.add(g)
            taken.add(fx)
            assign[g] = league
    out = []
    for r in rows:
        league = assign.get((r["book"], r["league"], r["home"], r["away"]))
        if league:
            out.append({**r, "league": league})
    return out


def load_props():
    import build_bet_picks as bp
    from compare_odds import analyse
    cmp, _ = bp.load()
    _, lines, arb = analyse(cmp, 2, -1.0)
    kick = {r.game: str(r.kickoff)[:16] for r in cmp.itertuples()}
    return lines, arb, kick

# -----------------------------------------------------------------------------
# ANALYSIS
# -----------------------------------------------------------------------------

def line_bets(rows):
    """Game lines + football through the football engine (leave-one-out no-vig per market)."""
    for r in rows:
        m = r["market"]
        if m not in fb.MARKET_SELS:
            fb.MARKET_SELS[m] = ["Under", "Over"] if m.startswith("Total ") else ["1", "2"]
    fb.MAX_EDGE = 9.9                                   # keep everything; this script splits off the suspects
    games, bets, _, unmatched = fb.analyse(rows)
    return games, bets, unmatched


def el_label(b, g):
    m, s = b["market"], b["sel"]
    team = g["home"] if s == "1" else g["away"]
    if m == "Winner":
        return f"{team} to win (incl. OT)"
    if m.startswith("Handicap"):
        h = float(m.split()[1])
        h = h if s == "1" else -h
        return f"{team} {h:+.1f} (incl. OT)"
    return f"{s} {m.split()[1]} points (incl. OT)"


def unify(games, bets, source):
    out = []
    for b in bets:
        g = games[b["game"]]
        label = el_label(b, g) if source in ("EL lines", "NBA lines") else fb.label(b, g)
        out.append({"source": source, "league": g["league"], "game_id": f"{source[:2]}:{b['game']}",
                    "match": f"{g['home']} – {g['away']}", "kickoff": str(g["kickoff"])[:16],
                    "bet": label, "key": (b["game"], b["market"], b["sel"]), "book": b["book"],
                    "odds": b["odds"], "prob": b["prob"], "fair_odds": b["fair_odds"],
                    "edge_pct": b["edge_pct"], "books": b["books"], "others": b.get("others", {})})
    return out


def unify_props(lines, kick, el_games):
    """Props -> same shape; game_id points at the EL game-lines match so a slip never mixes a
    player prop with a line from the same game."""
    def el_id(game):
        h, _, a = game.partition(" vs ")
        best = max(el_games.items(), key=lambda kv: max(fb.sim(h, kv[1]["home"]), fb.sim(a, kv[1]["away"])),
                   default=None)
        return f"EL:{best[0]}" if best else f"EP:{game}"
    if lines.empty:                                       # no EuroLeague props tonight
        return []
    ids = {g: el_id(g) for g in lines.game.unique()}
    out = []
    for r in lines.itertuples():
        side = r.side if r.side in ("Over", "Under") else r.side
        out.append({"source": "EL props", "league": "EuroLeague", "game_id": ids[r.game], "match": r.game,
                    "kickoff": kick.get(r.game, ""), "player": r.player,
                    "bet": f"{r.player} {side} {float(r.line):g} {r.market}" + (" (N+ line)" if r.via_milestone else ""),
                    "key": (r.game, r.player, r.market, r.side, float(r.line)), "book": r.book,
                    "odds": float(r.odds), "prob": round(1 / float(r.fair_odds), 4), "fair_odds": float(r.fair_odds),
                    "edge_pct": float(r.edge_pct), "books": int(r.books_in_consensus), "others": {}})
    return out


def score(b):
    return b["edge_pct"] * min(b["books"], 4) / 4


def value_picks(bets, n=10, per=None):
    """per: None (all), one source name, or a tuple of sources."""
    per = (per,) if isinstance(per, str) else per
    pool = [b for b in bets if PICK_ODDS[0] <= b["odds"] <= PICK_ODDS[1] and 0 < b["edge_pct"] <= 100 * MAX_EDGE
            and b["books"] >= 3 and (per is None or b["source"] in per)]
    pool.sort(key=lambda b: (-score(b), -b["odds"]))
    out, seen = [], set()
    for b in pool:
        tag = b.get("player") or b["game_id"]           # props: one per player; lines/football: one per game
        if tag in seen or (b["source"] != "EL props" and b["game_id"] in seen):
            continue
        seen.update({tag, b["game_id"]} if b["source"] != "EL props" else {tag})
        same = sorted(BOOKS[x["book"]] for x in bets if x["key"] == b["key"] and x["book"] != b["book"]
                      and x["odds"] >= b["odds"])
        out.append({**b, "also_at": same})
        if len(out) == n:
            break
    return out


def arbitrages(lines_rows, games_l, link_l, props_arb, n=8):
    """Best price per outcome across books for each market; positive margin only."""
    best = defaultdict(dict)
    for r in lines_rows:
        if r.get("ot") is False:                        # Mozzart's regular-time lines settle differently
            continue
        key = link_l.get((r["book"], r["league"], r["home"], r["away"]))
        if not key:
            continue
        cur = best[(key, r["market"])].get(r["sel"])
        if not cur or float(r["odds"]) > cur[0]:
            best[(key, r["market"])][r["sel"]] = (float(r["odds"]), r["book"])
    out = []
    for (key, market), sels in best.items():
        need = fb.MARKET_SELS.get(market)
        if not need or any(s not in sels for s in need) or market == "DC":
            continue
        inv = sum(1 / sels[s][0] for s in need)
        if inv >= 1 or len({sels[s][1] for s in need}) < 2:
            continue
        g = games_l[key]
        out.append({"source": "EuroLeague" if g["league"] == "EuroLeague" else g["league"],
                    "match": f"{g['home']} – {g['away']}", "kickoff": str(g["kickoff"])[:16], "market": market,
                    "legs": [{"sel": s, "odds": sels[s][0], "book": BOOKS[sels[s][1]],
                              "stake_pct": round(100 * (1 / sels[s][0]) / inv, 1)} for s in need],
                    "profit_pct": round(100 * (1 / inv - 1), 2)})
    for r in props_arb.itertuples():
        inv = 1 / r.best_odds_over + 1 / r.best_odds_under
        out.append({"source": "EL props", "match": r.game, "kickoff": "", "market": f"{r.player} {r.market} {r.line:g}",
                    "legs": [{"sel": "Over" + (" (N+)" if r.best_is_milestone else ""), "odds": float(r.best_odds_over),
                              "book": BOOKS.get(r.best_book_over, r.best_book_over),
                              "stake_pct": round(100 / r.best_odds_over / inv, 1)},
                             {"sel": "Under", "odds": float(r.best_odds_under),
                              "book": BOOKS.get(r.best_book_under, r.best_book_under),
                              "stake_pct": round(100 / r.best_odds_under / inv, 1)}],
                    "profit_pct": float(r.profit_pct)})
    out.sort(key=lambda a: -a["profit_pct"])
    return out[:n]


def best_slip(bets, legs, min_total, used_bets, used_games, leg_odds=LEG_ODDS):
    best = None
    by_book = defaultdict(dict)
    for b in sorted(bets, key=lambda x: -x["edge_pct"]):
        if not (leg_odds[0] <= b["odds"] <= leg_odds[1] and 0 < b["edge_pct"] <= 100 * MAX_EDGE and b["books"] >= 3):
            continue
        if b["key"] in used_bets or b.get("player") in used_games:     # no bet / player on two tickets
            continue
        by_book[b["book"]].setdefault(b["game_id"], b)        # one leg per game
    for book, per_game in by_book.items():
        cands = sorted(per_game.values(), key=lambda x: -x["edge_pct"])[:TOP]
        for combo in itertools.combinations(cands, legs):
            total = math.prod(c["odds"] for c in combo)
            if total < min_total:
                continue
            hit = math.prod(c["prob"] for c in combo)
            ev = total * hit - 1
            if best is None or ev > best["ev"]:
                best = {"book": BOOKS[book], "legs": list(combo), "total_odds": round(total, 2),
                        "hit_prob": hit, "ev": ev}
    return best


# -----------------------------------------------------------------------------
# BASKETBALL CARD (site "Against the Odds"): singles, 4-leg slip, safer 5-20 ticket, long 20-100 ticket
# -----------------------------------------------------------------------------
BASKET = ("EL props", "EL lines", "NBA lines")


def ticket(bets, legs, total_range, used, leg_odds=LEG_ODDS, per_game=2, goal="ev"):
    """Best ticket at ONE bookmaker: `legs` (int or range), total odds inside total_range, one leg per player,
    at most `per_game` legs per game, no bet from `used`. goal "ev" = best expected value, "safe" = highest
    win chance among positive-EV tickets."""
    legs = range(legs, legs + 1) if isinstance(legs, int) else legs
    by_book = defaultdict(list)
    for b in sorted(bets, key=lambda x: -x["edge_pct"]):
        if (leg_odds[0] <= b["odds"] <= leg_odds[1] and 0 < b["edge_pct"] <= 100 * MAX_EDGE and b["books"] >= 3
                and b["key"] not in used):
            by_book[b["book"]].append(b)
    best = None
    for book, cands in by_book.items():
        uniq, seen = [], set()
        for c in cands:                                   # one bet per player / per game line market
            tag = c.get("player") or (c["game_id"], c["bet"])
            if tag not in seen:
                seen.add(tag); uniq.append(c)
        uniq = uniq[:TOP]
        for n in legs:
            for combo in itertools.combinations(uniq, n):
                games = [c["game_id"] for c in combo]
                if max(games.count(g) for g in games) > per_game:
                    continue
                if sum(c["source"] == "EL lines" for c in combo) > len(set(games)):
                    continue
                if any(sum(c["source"] == "EL lines" and c["game_id"] == g for c in combo) > 1 for g in games):
                    continue                              # one game line per game
                total = math.prod(c["odds"] for c in combo)
                if not total_range[0] <= total <= total_range[1]:
                    continue
                hit = math.prod(c["prob"] for c in combo)
                ev = total * hit - 1
                if ev <= 0:
                    continue
                rank = hit if goal == "safe" else ev
                if best is None or rank > best["_rank"]:
                    best = {"book": BOOKS[book], "legs": list(combo), "total_odds": round(total, 2),
                            "hit_prob": hit, "ev": ev, "_rank": rank}
    if best:
        best.pop("_rank")
    return best


def basket_card(bets, arbs):
    pool = [b for b in bets if b["source"] in BASKET]
    singles = value_picks(pool, 10, BASKET)
    tickets, used = [], set()
    for name, label, legs, rng, leg_odds, goal in (
            ("four", "4-leg slip", 4, (1, 10_000), LEG_ODDS, "ev"),
            ("safe", "Safer ticket", range(3, 6), (5, 20), (1.30, 2.20), "safe"),
            ("long", "Long shot", range(4, 8), (20, 100), LEG_ODDS, "ev")):
        t = ticket(pool, legs, rng, used, leg_odds, goal=goal) or ticket(pool, legs, rng, set(), leg_odds, goal=goal)
        if t:
            used |= {l["key"] for l in t["legs"]}
            tickets.append({"name": name, "label": label, "range": list(rng) if name != "four" else None, **t})
    return {"singles": singles, "tickets": tickets,
            "arbitrage": [a for a in arbs if a["source"] in ("EL props", "EuroLeague", "NBA")]}

# -----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min4", type=float, default=8.0)
    ap.add_argument("--min5", type=float, default=12.0)
    ap.add_argument("--min6", type=float, default=20.0)
    ap.add_argument("--no-scrape", action="store_true", help="reuse today's raw_lines.json")
    args = ap.parse_args()
    day = dt.date.today().isoformat()

    if args.no_scrape:
        with open(os.path.join(PROJECT_DIR, "Odds", "el_lines", day, "raw_lines.json"), encoding="utf-8") as f:
            saved = json.load(f)
        el_rows, status = saved["rows"], saved["status"]
    else:
        el_rows, status = scrape_lines(day)
    print("EuroLeague lines:", status)
    nba_file = os.path.join(PROJECT_DIR, "Odds", "nba_lines", day, "raw_lines.json")
    if args.no_scrape and os.path.exists(nba_file):
        with open(nba_file, encoding="utf-8") as f:
            saved = json.load(f)
        nba_rows, nba_status = saved["rows"], saved["status"]
    else:
        nba_rows, nba_status = scrape_lines(day, NBA_SCRAPERS, "nba_lines")
    print("NBA lines:", nba_status)
    nba_cons = [r for r in nba_rows if r.get("ot", True)]
    nba_games, nba_raw, nba_unmatched = line_bets(nba_cons) if nba_cons else ({}, [], [])
    link_nba = fb.match_games(nba_cons)[0] if nba_cons else {}

    # Mozzart's handicap/total are regular time: leave them out of the consensus and arbs
    el_cons = [r for r in el_rows if r.get("ot", True)]
    el_games, el_raw, el_unmatched = line_bets(el_cons) if el_cons else ({}, [], [])   # no EuroLeague tonight
    link_el = fb.match_games(el_cons)[0] if el_cons else {}
    foot_rows = load_football(day)
    fg, fraw, f_unmatched = line_bets(foot_rows) if foot_rows else ({}, [], [])   # no UCL/Championship tonight
    link_f = fb.match_games(foot_rows)[0] if foot_rows else {}
    print(f"Football: {len(fg)} matches ({sum(g['league'] == 'Champions League' for g in fg.values())} UCL, "
          f"{sum(g['league'] == 'Championship' for g in fg.values())} Championship), "
          f"{len({r['book'] for r in foot_rows})} books")
    props, props_arb, kick = load_props()

    bets = (unify(el_games, el_raw, "EL lines") + unify(nba_games, nba_raw, "NBA lines") + unify(fg, fraw, "Football")
            + unify_props(props, kick, el_games))
    suspects = sorted([b for b in bets if b["edge_pct"] > 100 * MAX_EDGE and b["books"] >= 3],
                      key=lambda b: -b["edge_pct"])[:10]

    picks = value_picks(bets)
    per_source = {s: value_picks(bets, 5, s) for s in ("EL lines", "NBA lines", "EL props", "Football")}
    arbs = arbitrages(el_cons + foot_rows, {**el_games, **fg}, {**link_el, **link_f}, props_arb)
    # per league: both leagues number their games g0, g1, ... so their dicts can't be merged
    basket_arbs = arbitrages(el_cons, el_games, link_el, props_arb, n=12)
    if nba_cons:
        basket_arbs += arbitrages(nba_cons, nba_games, link_nba, props_arb.iloc[0:0], n=12)
    basket_arbs = sorted(basket_arbs, key=lambda a: -a["profit_pct"])[:12]
    basket = basket_card(bets, basket_arbs)

    slips, used_bets, used_games = {}, set(), set()
    for legs, mn in ((6, args.min6), (5, args.min5), (4, args.min4)):    # longest first: it needs the most games
        s = best_slip(bets, legs, mn, used_bets, used_games)
        slips[legs] = s
        if s:
            used_bets |= {l["key"] for l in s["legs"]}
            used_games |= {l["player"] for l in s["legs"] if l.get("player")}

    # separate cards for the two carousels
    el_picks = value_picks(bets, 10, ("EL props", "EL lines"))
    foot_picks = {lg: value_picks([b for b in bets if b["league"] == lg], 5, "Football")
                  for lg in ("Champions League", "Championship")}
    foot_slip = best_slip([b for b in bets if b["source"] == "Football"], 4, 5.0, set(), set(), (1.30, 3.00))

    def clean(b):
        return {k: v for k, v in b.items() if k not in ("key", "others")}
    out = {"date": day, "built": dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "el_status": status,
           "picks": [clean(p) for p in picks], "per_source": {k: [clean(p) for p in v] for k, v in per_source.items()},
           "arbitrage": arbs, "suspects": [clean(s) for s in suspects],
           "el_picks": [clean(p) for p in el_picks],
           "football_picks": {k: [clean(p) for p in v] for k, v in foot_picks.items()},
           "football_slip": None if not foot_slip else {**foot_slip, "legs": [clean(l) for l in foot_slip["legs"]]},
           "football_games": sorted({(str(g["kickoff"])[11:16], g["league"], g["home"], g["away"]) for g in fg.values()}),
           "el_games": sorted({(str(g["kickoff"])[11:16], g["home"], g["away"]) for g in el_games.values()}),
           "nba_status": nba_status,
           "nba_games": sorted({(str(g["kickoff"])[5:16], g["home"], g["away"]) for g in nba_games.values()}),
           "slips": {str(k): (None if not s else {**s, "legs": [clean(l) for l in s["legs"]]}) for k, s in slips.items()},
           "basket": {"singles": [clean(p) for p in basket["singles"]], "arbitrage": basket["arbitrage"],
                      "tickets": [{**t, "legs": [clean(l) for l in t["legs"]]} for t in basket["tickets"]]},
           "unmatched": {"el": el_unmatched, "nba": nba_unmatched, "football": f_unmatched}}
    folder = os.path.join(OUT_DIR, day)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "tonight.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)

    print(f"\nTop {len(picks)} value picks:")
    for i, p in enumerate(picks, 1):
        print(f"  {i:>2}. [{p['source']}] {p['match']} | {p['bet']} | {p['odds']} @ {BOOKS[p['book']]} "
              f"fair {p['fair_odds']} edge +{p['edge_pct']}% ({p['books']} books)")
    for s, ps in per_source.items():
        print(f"\n  {s} top 5:")
        for p in ps:
            print(f"     {p['match']} | {p['bet']} | {p['odds']} @ {BOOKS[p['book']]} fair {p['fair_odds']} +{p['edge_pct']}%")
    for k in (4, 5, 6):
        s = slips[k]
        if not s:
            print(f"\n{k}-leg slip: none found")
            continue
        print(f"\n{k}-leg slip @ {s['book']}: odds {s['total_odds']}, win chance {100 * s['hit_prob']:.2f}%, "
              f"EV {100 * s['ev']:+.1f}%")
        for l in s["legs"]:
            print(f"     [{l['source']}] {l['match']} | {l['bet']} | {l['odds']} (fair {l['fair_odds']})")
    print("\nBasketball card:")
    for t in basket["tickets"]:
        print(f"  {t['label']} @ {t['book']}: odds {t['total_odds']}, win chance {100 * t['hit_prob']:.1f}%, "
              f"EV {100 * t['ev']:+.1f}%")
        for l in t["legs"]:
            print(f"     {l['match']} | {l['bet']} | {l['odds']} (fair {l['fair_odds']})")
    print("\nArbitrages:")
    for a in arbs:
        legs = " / ".join(f"{l['sel']} {l['odds']} @ {l['book']} ({l['stake_pct']}%)" for l in a["legs"])
        print(f"  +{a['profit_pct']}%  [{a['source']}] {a['match']} {a['market']}: {legs}")
    if suspects:
        print("\nEdges over 15% (likely stale/mis-keyed, not used):")
        for b in suspects[:6]:
            print(f"  {b['match']} | {b['bet']} | {b['odds']} @ {BOOKS[b['book']]} fair {b['fair_odds']} +{b['edge_pct']}%")
    print(f"\nUnmatched: EL {len(el_unmatched)}, football {len(f_unmatched)}")
    print(f"-> {os.path.join(folder, 'tonight.json')}")


if __name__ == "__main__":
    main()
