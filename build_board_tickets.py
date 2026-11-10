#!/usr/bin/env python3
"""
build_board_tickets.py

The ticket sets for the two drawing boards on the Against the Odds page:
  basketball board  15 player-prop tickets (NBA/EuroLeague props) + 5 game-line tickets (winner, handicap, total)
  football board    15 tickets from today's matches: regular tickets at any book, plus Soccerbet tickets that
                    each carry one Extrabet leg (Soccerbet's daily Extrabet freebet promotion)

Same pricing as build_tonight_bets.py: fair price = leave-one-out no-vig consensus of the other books; Extrabet
legs use the goals model from extrabet_value.py (extrabet_priced.csv). Ticket rules: one bookmaker per ticket,
one leg per player, at most 2 legs per game (props) or 1 (lines, football), a leg is used on at most MAX_USE
tickets and two tickets never share more than half their legs. Each profile looks for positive value first and
relaxes the floor step by step only when nothing qualifies (the value is printed on the board either way).

Run after scrape_odds.py --nba + compare_odds.py, build_tonight_bets.py, scrape_football.py --all and
extrabet_value.py:
  python build_board_tickets.py [--from HH:MM] [--football-only | --basketball-only]
Output: basket.prop_tickets / basket.line_tickets in Odds/tonight/<date>/tonight.json (settled by
settle_tonight.py) and Odds/tonight/<date>/football_board.json
"""

import csv
import datetime as dt
import itertools
import json
import math
import os
import re
import sys
from collections import defaultdict

import build_football_bets as fb
import build_football_tickets as ft
import build_tonight_bets as bt
from compare_odds import FEED

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
BOOKS = fb.BOOK_NAMES
MAX_EDGE = 0.15
MAX_USE = 3
POOL = 16
EV_FLOORS = (0.0, -0.04, -0.08, -0.15, -0.30)
SKIP_LEAGUE = re.compile(r"( W$| W | U\d\d|Rezerv|Mladih|Razvojna|Amater)", re.I)
SKIP_TEAM = re.compile(r"\b(U\d\d|II|B|W|Women|Reserves?)\b")
# leagues tools_fb/fb_scores.py can settle (ESPN scoreboards); others are left off the football board
SETTLEABLE = {"Champions League", "Europa League", "Conference League", "Premier League", "Championship",
              "League One", "LaLiga", "Serie A", "Bundesliga", "Ligue 1", "Eredivisie", "Primeira Liga"}
LEAD = dt.timedelta(minutes=60)       # legs must start at least this long after the build
LEAGUE_EN = {"Liga Šampiona": "Champions League", "Liga Evrope": "Europa League", "Liga Konferencija": "Conference League",
             "Japan 1": "J1 League", "Japan 2": "J2 League", "Japan 3": "J3 League", "Egipat 1": "Egyptian Premier League",
             "Saudijska Arabija 2": "Saudi First Division", "Malta 1": "Maltese Premier League",
             "Estonija 1": "Estonian Meistriliiga", "Norveška 2": "Norwegian First Division", "LaLiga": "LaLiga",
             "Engleska 2": "Championship", "Engleska 3": "League One", "Engleska 1": "Premier League",
             "Italija 1": "Serie A", "Nemačka 1": "Bundesliga", "Francuska 1": "Ligue 1", "Holandija 1": "Eredivisie",
             "Portugal 1": "Primeira Liga", "Kipar Kup": "Cypriot Cup", "Danska Kup": "Danish Cup",
             "Slovačka Kup": "Slovak Cup", "Bugarska Pro Kup": "Bulgarian Cup", "Uzbekistan Kup": "Uzbek Cup",
             "Tajland Kup": "Thai FA Cup", "Indonezija 1": "Indonesian Liga 1", "Singapur 1": "Singapore Premier League",
             "AFC Challenge Liga": "AFC Challenge League", "Švajcarska 3": "Swiss Promotion League"}

# -----------------------------------------------------------------------------
# TICKET SEARCH
# -----------------------------------------------------------------------------

def leg_key(l):
    """Soccerbet, MaxBet and Admiral share one price feed: the same bet there is the same leg."""
    return ("feed" if l["book"] in FEED else l["book"], l["match"], l["bet"])


def search(cands, legs, total_rng, leg_odds, goal, uses, made, per_game, min_ev, min_books=3, book=None,
           fixed=None):
    """Best ticket at one book. `fixed` = a leg every ticket must contain (Extrabet tickets)."""
    by_book = defaultdict(list)
    for c in cands:
        if book and c["book"] != book:
            continue
        if not (leg_odds[0] <= c["odds"] <= leg_odds[1] and c["edge_pct"] <= 100 * MAX_EDGE
                and c["books"] >= min_books and uses[leg_key(c)] < MAX_USE):
            continue
        if fixed and c["game_id"] == fixed["game_id"]:
            continue
        by_book[c["book"]].append(c)
    best = None
    for bk, cs in by_book.items():
        # pool: best value legs plus the most likely ones, one per player / per game market
        pool, seen = [], set()
        for c in sorted(cs, key=lambda x: -x["edge_pct"] * min(x["books"], 4) / 4)[:POOL] + \
                sorted([x for x in cs if x["edge_pct"] > -3], key=lambda x: -x["prob"])[:POOL // 2]:
            tag = c.get("player") or (c["game_id"], c["bet"])
            if tag not in seen:
                seen.add(tag)
                pool.append(c)
        for n in legs:
            k = n - (1 if fixed else 0)
            for combo in itertools.combinations(pool, k):
                combo = (fixed, *combo) if fixed else combo
                games = [c["game_id"] for c in combo]
                if max(games.count(g) for g in games) > per_game:
                    continue
                if len({c.get("player") or id(c) for c in combo}) < n:
                    continue
                total = math.prod(c["odds"] for c in combo)
                if not total_rng[0] <= total <= total_rng[1]:
                    continue
                keys = {leg_key(c) for c in combo}
                if any(len(keys & t) * 2 > len(keys) for t in made):
                    continue
                hit = math.prod(c["prob"] for c in combo)
                ev = total * hit - 1
                if ev < min_ev:
                    continue
                rank = hit if goal == "safe" else ev
                if best is None or rank > best[0]:
                    best = (rank, bk, combo, total, hit, ev)
    if not best:
        return None
    _, bk, combo, total, hit, ev = best
    return {"book": BOOKS.get(bk, bk), "legs": sorted(combo, key=lambda c: c["kickoff"]),
            "total_odds": round(total, 2), "hit_prob": hit, "ev": ev}


def make_set(cands, profiles, per_game, min_books=3, prefix=""):
    """profiles: (name, label, legs range, total range, leg odds, goal, count) -> list of tickets."""
    uses, made, out = defaultdict(int), [], []
    for name, label, legs, rng, leg_odds, goal, count in profiles:
        for i in range(count):
            t = None
            for floor in EV_FLOORS:
                t = search(cands, legs, rng, leg_odds, goal, uses, made, per_game, floor, min_books)
                if t:
                    break
            if not t:
                print(f"  {label}: nothing left")
                break
            for l in t["legs"]:
                uses[leg_key(l)] += 1
            made.append({leg_key(l) for l in t["legs"]})
            out.append({"name": name, "label": f"{prefix}{len(out) + 1} · {label}", "range": list(rng), **t})
    return out


def clean(l):
    return {k: v for k, v in l.items() if k not in ("key", "others")}


def show(title, tickets):
    print(f"\n{title}: {len(tickets)} tickets")
    for t in tickets:
        print(f"  {t['label']:28} @ {t['book']:9} odds {t['total_odds']:7.2f}  win {100 * t['hit_prob']:5.1f}%  "
              f"value {100 * t['ev']:+5.1f}%")
        for l in t["legs"]:
            print(f"      {l['kickoff'][-5:]} {l['match'][:40]:40} {l['bet'][:60]:60} {l['odds']:.2f} "
                  f"(fair {l['fair_odds']})")

# -----------------------------------------------------------------------------
# BASKETBALL
# -----------------------------------------------------------------------------

def basketball(day, now):
    def rows(sub):
        f = os.path.join(PROJECT_DIR, "Odds", sub, day, "raw_lines.json")
        return [r for r in json.load(open(f, encoding="utf-8"))["rows"] if r.get("ot", True)] if os.path.exists(f) else []
    lines = []
    for sub, src in (("nba_lines", "NBA lines"), ("el_lines", "EL lines")):
        rs = rows(sub)
        if rs:
            games, raw, _ = bt.line_bets(rs)
            lines += bt.unify(games, raw, src)
    nba_teams = {w for r in rows("nba_lines") for w in (r["home"] + " " + r["away"]).lower().split()}
    props_df, _, kick = bt.load_props()
    props = bt.unify_props(props_df, kick, {})
    for p in props:
        if len(set(p["match"].lower().replace(" vs ", " ").split()) & nba_teams) >= 2:
            p["source"], p["league"] = "NBA props", "NBA"
    soon = (now + LEAD).strftime("%Y-%m-%d %H:%M")
    lines = [clean(l) for l in lines if l["kickoff"] > soon]
    props = [clean(p) for p in props if p["kickoff"] > soon]
    print(f"Basketball: {len(lines)} game-line bets, {len(props)} prop bets "
          f"({len({p['player'] for p in props})} players, books {sorted({p['book'] for p in props})})")
    prop_books = max((p["books"] for p in props), default=0)
    prop_t = make_set(props, (
        ("safe", "Safer", range(2, 4), (2.5, 6), (1.40, 2.40), "safe", 5),
        ("value", "Value", range(3, 5), (6, 15), (1.50, 2.80), "ev", 5),
        ("long", "Long shot", range(5, 7), (15, 80), (1.50, 3.20), "ev", 5)),
        per_game=2, min_books=min(3, prop_books), prefix="Player ticket ")
    line_t = make_set(lines, (
        ("safe", "Safer", range(2, 4), (2.5, 6), (1.30, 2.40), "safe", 2),
        ("value", "Value", range(3, 5), (6, 15), (1.50, 2.80), "ev", 2),
        ("long", "Long shot", range(5, 7), (20, 100), (1.50, 3.50), "ev", 1)),
        per_game=1, prefix="Match ticket ")
    show("Player-prop tickets", prop_t)
    show("Game-line tickets", line_t)
    return prop_t, line_t, prop_books

# -----------------------------------------------------------------------------
# FOOTBALL
# -----------------------------------------------------------------------------

def football_rows(day, now):
    """Every book's rows for today's not-started matches, leagues named after Soccerbet's fixture list
    (bt.load_football, but for every senior league instead of UCL + Championship)."""
    path = os.path.join(PROJECT_DIR, "Odds", "football", day, "raw_all.csv")
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    fixtures = {(r["home"], r["away"]): (r["league"], dt.datetime.fromisoformat(r["kickoff"]))
                for r in rows if r["book"] == "soccerbet" and not SKIP_LEAGUE.search(r["league"])
                and not SKIP_TEAM.search(r["home"] + " " + r["away"])}
    fixtures = {k: v for k, v in fixtures.items() if v[1] > now + LEAD}
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
        for sc, g, fx, league in sorted(scored, key=lambda x: -x[0]):
            if g in done or fx in taken or sc < 1.1:
                continue
            done.add(g)
            taken.add(fx)
            assign[g] = LEAGUE_EN.get(league, league)
    return [{**r, "league": assign[(r["book"], r["league"], r["home"], r["away"])]}
            for r in rows if assign.get((r["book"], r["league"], r["home"], r["away"])) in SETTLEABLE]


def player_teams(day):
    """{normalised player name: team} from the anytime-scorer lists in Odds/extrabet/<day>/raw/*.json."""
    import extrabet_value as ev
    raw, out = os.path.join(PROJECT_DIR, "Odds", "extrabet", day, "raw"), {}
    for fn in os.listdir(raw):
        if fn[0].isdigit():
            d = json.load(open(os.path.join(raw, fn), encoding="utf-8"))
            for x in (d.get("pl") or {}).get("esMatches") or []:
                out[" ".join(ev.norm(x["home"]))] = x["away"]
    return out


def extrabet_legs(day, games, now):
    """Soccerbet Extrabet combos (result / goals + a scorer) at odds 3-12 priced by the goals model. A combo that
    backs one team to win and a player of the other team to score is left out."""
    import extrabet_value as ev
    f = os.path.join(PROJECT_DIR, "Odds", "extrabet", day, "extrabet_priced.csv")
    if not os.path.exists(f):
        return []
    teams, out = player_teams(day), []
    for r in csv.DictReader(open(f, encoding="utf-8-sig")):
        if r["type"] not in ("FT_PLG", "FT_OUG_PLG", "OUG_PLG", "FT_BTS_PLG") or "order=-" in r["selection"]:
            continue
        sel = dict(kv.split("=", 1) for kv in r["selection"].split(","))
        if "team" in sel and teams.get(" ".join(ev.norm(sel["participant"]))) not in (sel["team"], None):
            continue
        odds, prob = float(r["price"]), float(r["prob"])
        if not 3.0 <= odds <= 12.0:
            continue
        home, away = r["match"].split(" - ", 1)
        gid, g = max(games.items(), key=lambda kv: fb.sim(home, kv[1]["home"]) + fb.sim(away, kv[1]["away"]))
        if fb.sim(home, g["home"]) + fb.sim(away, g["away"]) < 0.8 or g["kickoff"] <= now + LEAD:
            continue
        out.append({"source": "Extrabet", "league": g["league"], "game_id": f"Fo:{gid}",
                    "match": f'{g["home"]} – {g["away"]}', "kickoff": str(g["kickoff"])[:16],
                    "bet": ft.describe_xb(r["type"], r["selection"], g["home"], g["away"]),
                    "type": r["type"], "selection": r["selection"], "book": "soccerbet", "odds": odds,
                    "prob": prob, "fair_odds": round(1 / prob, 2), "edge_pct": round(100 * (odds * prob - 1), 1),
                    "books": 1})
    return out


def football(day, now):
    rows = football_rows(day, now)
    games, raw, _ = bt.line_bets(rows)
    legs = [clean(l) for l in bt.unify(games, raw, "Football")]
    for l in legs:
        l["market"], l["sel"] = None, None
    xb = extrabet_legs(day, games, now)
    leagues = defaultdict(int)
    for g in games.values():
        leagues[g["league"]] += 1
    print(f"\nFootball: {len(games)} matches in {len(leagues)} leagues, {len(legs)} priced bets, {len(xb)} Extrabet legs")
    regular = make_set(legs, (
        ("safe", "Safer", range(2, 4), (2.5, 6), (1.30, 2.40), "safe", 3),
        ("value", "Value", range(3, 5), (6, 15), (1.50, 2.80), "ev", 3),
        ("long", "Long shot", range(5, 7), (20, 100), (1.50, 3.50), "ev", 3)),
        per_game=1, min_books=4, prefix="Ticket ")
    # Extrabet tickets: the six best-valued Extrabet legs (one per match, different players) + 1-2 Soccerbet legs
    sb_legs = [l for l in legs if l["book"] == "soccerbet"]
    xb_t, used_players, used_games = [], set(), defaultdict(int)
    uses, made = defaultdict(int), []
    for x in sorted(xb, key=lambda x: -x["edge_pct"]):
        if len(xb_t) == 6:
            break
        player = x["selection"].split("participant=")[-1].split(",")[0]
        if player in used_players or used_games[x["game_id"]] >= 1:
            continue
        t = None
        for floor in EV_FLOORS:
            t = search(sb_legs, range(2, 4), (x["odds"] * 1.6, x["odds"] * 6), (1.30, 2.50), "ev", uses, made, 1,
                       floor, min_books=4, book="soccerbet", fixed=x)
            if t:
                break
        if not t:
            continue
        used_players.add(player)
        used_games[x["game_id"]] += 1
        for l in t["legs"]:
            if l["source"] != "Extrabet":
                uses[leg_key(l)] += 1
        made.append({leg_key(l) for l in t["legs"]})
        xb_t.append({"name": "xb", "label": f"Ticket {len(regular) + len(xb_t) + 1} · Extrabet", "range": None, **t})
    show("Football tickets", regular)
    show("Extrabet tickets", xb_t)
    return {"date": day, "built": dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "n_games": len(games),
            "leagues": sorted(leagues.items(), key=lambda kv: -kv[1]), "tickets": regular + xb_t}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    now = dt.datetime.now()
    if "--from" in sys.argv:              # --from HH:MM: only legs kicking off after that time (minus LEAD)
        hh, mm = map(int, sys.argv[sys.argv.index("--from") + 1].split(":"))
        now = max(now, now.replace(hour=hh, minute=mm) - LEAD)
    day = dt.date.today().isoformat()
    folder = os.path.join(PROJECT_DIR, "Odds", "tonight", day)
    if "--football-only" not in sys.argv:
        prop_t, line_t, prop_books = basketball(day, now)
        tf = os.path.join(folder, "tonight.json")
        t = json.load(open(tf, encoding="utf-8"))
        t["basket"]["prop_tickets"] = prop_t
        t["basket"]["line_tickets"] = line_t
        t["nba_props"] = bool(prop_t)
        json.dump(t, open(tf, "w", encoding="utf-8"), ensure_ascii=False, indent=2, default=str)
        print(f"-> {tf}")
    if "--basketball-only" not in sys.argv:
        card = football(day, now)
        out = os.path.join(folder, "football_board.json")
        json.dump(card, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2, default=str)
        print(f"-> {out}")


if __name__ == "__main__":
    main()
