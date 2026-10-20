#!/usr/bin/env python3
"""
build_football_bets.py

Value bets on today's football (Odds/football/<date>/raw_odds.json from scrape_football.py):
  1. matches the same game across the six books (league + kickoff + team-name similarity)
  2. fair price per selection = leave-one-out consensus: the average no-vig probability of
     the OTHER books that price the whole market (1X2, DC, Total 2.5, BTTS); needs 3+ books
  3. edge = odds x fair probability - 1
  4. top 10 picks: one per match, odds 1.40-4.00, ranked by edge x min(books, 4)/4
  5. 4-leg and 6-leg slips: one ticket at ONE bookmaker, different matches, legs at odds
     1.30-2.50 (the 6-leg slip may go up to 4.00 when no book has 6 shorter legs with an edge),
     highest expected value with total odds >= --slip4-odds / --slip6-odds
Then renders an Instagram carousel (bet_slides_template.html, 1080x1350): cover with the
top 10, picks 1-5, picks 6-10, the 4-leg slip, the 6-leg slip; caption in carousel.json.

Usage:
  python build_football_bets.py [date] [--slip4-odds 8] [--slip6-odds 20]
Output: Odds/football/<date>/picks.json, value_bets.csv, carousel/NN_*.png, carousel.json
"""

import argparse
import csv
import datetime as dt
import difflib
import html
import itertools
import json
import math
import os
import shutil
import tempfile
import unicodedata
from collections import defaultdict

from build_player_cards import find_browser, font_css, make_dir, render_png

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
FOOT_DIR = os.path.join(PROJECT_DIR, "Odds", "football")
TEMPLATE = os.path.join(PROJECT_DIR, "bet_slides_template.html")
BOOK_NAMES = {"soccerbet": "Soccerbet", "maxbet": "MaxBet", "admiralbet": "Admiral", "mozzartbet": "Mozzart",
              "superbet": "Superbet", "balkanbet": "Balkanbet"}
NAME_ORDER = ["superbet", "mozzartbet", "soccerbet", "maxbet", "balkanbet", "admiralbet"]  # display names
MARKET_SELS = {"1X2": ["1", "X", "2"], "DC": ["1X", "12", "X2"], "Total 2.5": ["Under", "Over"],
               "BTTS": ["GG", "NG"]}
MIN_BOOKS, MIN_ODDS, MAX_ODDS, MAX_EDGE = 3, 1.40, 4.00, 0.15
SLIP_ODDS = (1.30, 2.50)                    # per-leg odds on slips, so they stay realistic
STOP = {"fc", "cf", "ac", "sc", "afc", "ssc", "as", "ud", "cd", "sv", "vfb", "fsv", "tsg", "rb", "osc", "ogc", "rc",
        "rcd", "sd", "us", "bv", "club", "de", "cp", "kv", "krc", "rsc", "royal", "calcio", "fk", "sk", "the",
        "1", "04", "05", "09", "1846", "1899", "1910", "1913", "ca", "real"}
e = html.escape

# -----------------------------------------------------------------------------
# MATCHING
# -----------------------------------------------------------------------------

def tokens(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    toks = ["".join(c for c in w if c.isalnum()) for w in s.replace("-", " ").replace(".", " ").split()]
    return [t for t in toks if t and t not in STOP] or [t for t in toks if t]


def sim(a, b):
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    jac = len(set(ta) & set(tb)) / len(set(ta) | set(tb))
    pre = max((1.0 for x in ta for y in tb if len(x) >= 4 and len(y) >= 4 and (x.startswith(y[:4]) or y.startswith(x[:4]))),
              default=0.0)
    seq = difflib.SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()
    return max(jac, 0.8 * pre, seq)


def match_games(rows):
    """-> {(book, league, home, away): game key} and {key: game info}."""
    per_book = defaultdict(dict)
    for r in rows:
        per_book[r["book"]][(r["league"], r["home"], r["away"])] = dt.datetime.fromisoformat(r["kickoff"])
    ref = max(per_book, key=lambda b: (len(per_book[b]), -NAME_ORDER.index(b)))
    games = {f"g{i}": {"league": l, "kickoff": k, "names": {ref: (h, a)}}
             for i, ((l, h, a), k) in enumerate(sorted(per_book[ref].items(), key=lambda x: (x[1], x[0])))}
    link = {(ref, l, h, a): key for key, g in games.items() for (l, (h, a)) in [(g["league"], g["names"][ref])]}
    unmatched = []
    for book, gs in per_book.items():
        if book == ref:
            continue
        taken = set()
        scored = []
        for (l, h, a), k in gs.items():
            for key, g in games.items():
                if g["league"] != l or abs((g["kickoff"] - k).total_seconds()) > 1800:
                    continue
                rh, ra = g["names"][ref]
                scored.append((sim(h, rh) + sim(a, ra), (l, h, a), key))
        done = set()
        for score, gk, key in sorted(scored, reverse=True):
            if gk in done or key in taken or score < 0.9:
                continue
            done.add(gk)
            taken.add(key)
            link[(book, *gk)] = key
            games[key]["names"][book] = gk[1:]
        unmatched += [(book, *gk) for gk in gs if gk not in done]
    for g in games.values():
        g["home"], g["away"] = next(g["names"][b] for b in NAME_ORDER if b in g["names"])
    return link, games, unmatched

# -----------------------------------------------------------------------------
# ANALYSIS
# -----------------------------------------------------------------------------

def no_vig(prices, market):
    """{sel: odds} for a whole market -> {sel: probability}; None if the market is incomplete."""
    sels = MARKET_SELS[market]
    if any(s not in prices for s in sels):
        return None
    inv = {s: 1 / prices[s] for s in sels}
    total = sum(inv.values()) / (2 if market == "DC" else 1)    # DC outcomes cover each result twice
    return {s: v / total for s, v in inv.items()}


def analyse(rows):
    link, games, unmatched = match_games(rows)
    book_odds = defaultdict(lambda: defaultdict(dict))            # (game, market) -> book -> {sel: odds}
    for r in rows:
        key = link.get((r["book"], r["league"], r["home"], r["away"]))
        if key:
            book_odds[(key, r["market"])][r["book"]][r["sel"]] = float(r["odds"])
    bets, suspect = [], []
    for (key, market), books in book_odds.items():
        fair = {b: no_vig(p, market) for b, p in books.items()}
        for book, prices in books.items():
            for sel, odds in prices.items():
                others = [fair[b][sel] for b in books if b != book and fair[b]]
                if len(others) < MIN_BOOKS:
                    continue
                p = sum(others) / len(others)
                edge = odds * p - 1
                bet = {"game": key, "market": market, "sel": sel, "book": book, "odds": odds,
                       "prob": round(p, 4), "fair_odds": round(1 / p, 2), "edge_pct": round(100 * edge, 1),
                       "books": len(others) + 1,
                       "others": {b: books[b].get(sel) for b in books if b != book and books[b].get(sel)}}
                if edge > MAX_EDGE:
                    suspect.append(bet)
                else:
                    bets.append(bet)
    return games, bets, suspect, unmatched


def label(bet, g):
    m, s = bet["market"], bet["sel"]
    if m == "1X2":
        return {"1": f"{g['home']} to win", "X": "Draw", "2": f"{g['away']} to win"}[s]
    if m == "DC":
        return {"1X": f"{g['home']} or draw", "12": "No draw (12)", "X2": f"{g['away']} or draw"}[s]
    if m == "Total 2.5":
        return f"{s} 2.5 goals"
    return "Both teams to score" if s == "GG" else "Both teams NOT to score"


def short_code(bet):
    return {"1X2": "", "DC": "DC ", "Total 2.5": "", "BTTS": ""}[bet["market"]] + {
        "Under": "Under 2.5", "Over": "Over 2.5", "GG": "GG", "NG": "NG"}.get(bet["sel"], bet["sel"])


def top_picks(bets, n=10):
    pool = [b for b in bets if MIN_ODDS <= b["odds"] <= MAX_ODDS and b["edge_pct"] > 0]
    pool.sort(key=lambda b: -b["edge_pct"] * min(b["books"], 4) / 4)
    picks, seen = [], set()
    for b in pool:
        if b["game"] in seen:
            continue
        seen.add(b["game"])
        same = [x["book"] for x in bets if x["game"] == b["game"] and x["market"] == b["market"]
                and x["sel"] == b["sel"] and x["book"] != b["book"] and x["odds"] >= b["odds"]]
        picks.append({**b, "also_at": [BOOK_NAMES[x] for x in same]})
        if len(picks) == n:
            break
    return picks


def best_slip(bets, legs, min_total, max_leg=None, top=18):
    """Highest-EV slip of `legs` different matches at one book with total odds >= min_total."""
    best = None
    by_book = defaultdict(list)
    for b in bets:
        if SLIP_ODDS[0] <= b["odds"] <= (max_leg or SLIP_ODDS[1]) and b["edge_pct"] > 0:
            by_book[b["book"]].append(b)
    for book, cands in by_book.items():
        per_game = {}
        for b in sorted(cands, key=lambda x: -x["edge_pct"]):
            per_game.setdefault(b["game"], b)                     # one leg per match
        cands = sorted(per_game.values(), key=lambda x: -x["edge_pct"])[:top]
        for combo in itertools.combinations(cands, legs):
            total = math.prod(c["odds"] for c in combo)
            if total < min_total:
                continue
            hit = math.prod(c["prob"] for c in combo)
            ev = total * hit - 1
            if best is None or ev > best["ev"]:
                best = {"book": book, "legs": list(combo), "total_odds": total, "hit_prob": hit, "ev": ev}
    if best:
        best["legs"].sort(key=lambda c: c["game"])
    return best

# -----------------------------------------------------------------------------
# RENDER
# -----------------------------------------------------------------------------

def date_label(day):
    d = dt.date.fromisoformat(day)
    return f"{d.strftime('%a')} {d.strftime('%b')} {d.day}"


def foot(data):
    return (f'<div class="foot"><span><b>18+</b> · Odds as of {e(data["built"][11:])} and they change · '
            f'Not a guarantee, bet responsibly</span><span>Football · top 8 leagues</span></div>')


def slide(kick, body, data):
    return (f'<div class="slide"><div class="top"><span class="kick">{e(kick)}</span>'
            f'<span class="date">{e(date_label(data["date"]))}</span></div>{body}{foot(data)}</div>')


def match_line(g):
    return f'{g["home"]} – {g["away"]}'


def cover(data):
    games = data["games"]
    rows = "".join(f'<div class="li"><span class="n">{i}</span><span class="p">{e(match_line(games[p["game"]]))}'
                   f'<small>{e(p["label"])}</small></span>'
                   f'<span class="o">{p["odds"]:.2f}</span><span class="e">+{p["edge_pct"]:.1f}%</span></div>'
                   for i, p in enumerate(data["picks"], 1))
    leagues = "".join(f'<span>{e(l)} · {n}</span>' for l, n in data["leagues"])
    body = (f'<h1>Today\'s football<br><span>value picks</span></h1>'
            f'<div class="lede">{data["n_games"]} matches in the top 8 leagues, priced at 6 bookmakers. '
            f'A pick is a price above the fair odds of the other books. Edge = how much better.</div>'
            f'<div class="list fl">{rows}</div><div class="books">{leagues}</div>')
    return slide("Football · value picks", body, data)


def picks_slide(data, start):
    games, tickets = data["games"], []
    for i, p in enumerate(data["picks"][start:start + 5], start + 1):
        g = games[p["game"]]
        also = f' · also {", ".join(p["also_at"])}' if p["also_at"] else ""
        tickets.append(
            f'<div class="ticket"><span class="n">{i}</span><div class="who"><span class="pl">{e(match_line(g))}</span>'
            f'<span class="bet">{e(p["label"])}</span>'
            f'<span class="meta">{e(g["league"])} · {e(g["kickoff"][11:16])}{e(also)}</span></div>'
            f'<div class="px"><b>{p["odds"]:.2f}</b><span>{e(BOOK_NAMES[p["book"]])} · fair {p["fair_odds"]:.2f}</span>'
            f'<em>+{p["edge_pct"]:.1f}% edge</em></div></div>')
    body = (f'<div class="head">Picks {start + 1}-{start + 5}<small>Best price at one book vs the fair odds from '
            f'the others · win chance = 1 / fair odds</small></div><div class="tickets fl">{"".join(tickets)}</div>')
    return slide("Top 10 value picks", body, data)


def slip_slide(data, s, title):
    games, n = data["games"], len(s["legs"])
    size = "--lp:7px;--lf:17px;--bf:11px;--of:17px" if n > 4 else ""
    legs = "".join(f'<div class="sl"><div><div class="pl">{e(match_line(games[l["game"]]))}</div>'
                   f'<div class="bt">{e(l["label"])} · {e(games[l["game"]]["kickoff"][11:16])}</div></div>'
                   f'<span class="o">{l["odds"]:.2f}</span></div>' for l in s["legs"])
    chance = 100 * s["hit_prob"]
    body = (f'<div class="head">{e(title)}<small>All legs at {e(BOOK_NAMES[s["book"]])}, one ticket, '
            f'{n} different matches</small></div>'
            f'<div class="slip fl" style="{size}"><div class="sh"><b>{n}-leg slip</b><span>{e(BOOK_NAMES[s["book"]])}</span></div>'
            f'{legs}<div class="tot"><span class="k">Total odds</span><span class="v">{s["total_odds"]:,.2f}</span></div></div>'
            f'<div class="facts"><div><span>Win chance</span><b>{chance:.{1 if chance >= 1 else 2}f}%</b></div>'
            f'<div><span>100 RSD returns</span><b>{100 * s["total_odds"]:,.0f}</b></div>'
            f'<div><span>Math edge</span><b>{100 * s["ev"]:+.0f}%</b></div></div>'
            f'<div class="note">Highest expected value we found at a single book, using only legs priced above '
            f'fair. Every leg must win, so small errors in the fair prices multiply.'
            f'{" Today only longer-priced legs had an edge, so this one is a real long shot." if s["hit_prob"] < .01 else ""}</div>')
    return slide("Bet slip", body, data)


EXTRA_CSS = """<style>
.fl .li .p { font-size: 15px; } .fl .li .p small { display: block; margin: 1px 0 0; font-size: 10px; }
.fl.list { gap: 3px; margin-top: 10px; } .fl .li { padding: 3px 10px; }
.slide .books span { font-size: 8.5px; padding: 2px 7px; }
.slide h1 { font-size: 54px; margin-top: 10px; } .slide .lede { margin-top: 7px; font-size: 12px; }
.tickets.fl { gap: 11px; } .fl .ticket { padding: 13px 16px; } .fl .ticket .pl { font-size: 21px; }
.fl .ticket .bet { font-size: 14px; color: var(--edge); }
.slip.fl .sl .pl { font-size: var(--lf, 20px); }
</style>"""


def caption(data):
    games = data["games"]
    lines = [f"⚽ Today's football value picks · {date_label(data['date'])}",
             "Top 8 leagues · 6 bookmakers compared · best price vs fair odds", ""]
    for i, p in enumerate(data["picks"], 1):
        g = games[p["game"]]
        lines.append(f"{i}. {match_line(g)} ({g['kickoff'][11:16]}): {p['label']} @ {p['odds']:.2f} "
                     f"{BOOK_NAMES[p['book']]} (fair {p['fair_odds']:.2f}, +{p['edge_pct']:.1f}%)")
    for key, name in (("slip_4", "4-leg slip"), ("slip_6", "6-leg slip")):
        s = data[key]
        if s:
            lines += ["", f"🎟️ {name} @ {BOOK_NAMES[s['book']]}: total odds {s['total_odds']:.2f} · "
                          f"win chance {100 * s['hit_prob']:.1f}%"]
            lines += [f"  • {match_line(games[l['game']])}: {l['label']} {l['odds']:.2f}" for l in s["legs"]]
    lines += ["", "Edge = our price vs the no-vig consensus of the other books. Slips are long shots: every leg "
                  "must land.", "", "18+. Odds change and nothing here is a guarantee. Bet only what you can "
                  "afford to lose.", "",
              "#Football #Soccer #BettingTips #ValueBets #PremierLeague #LaLiga #SerieA #Bundesliga #Ligue1"]
    return "\n".join(lines)[:2200]


def render(data, folder):
    with open(TEMPLATE, encoding="utf-8") as f:
        template = f.read().replace("{{FONTS}}", font_css()).replace("{{BODY}}", EXTRA_CSS + "{{BODY}}")
    slides = [("01_cover", cover(data)), ("02_picks-1-5", picks_slide(data, 0)),
              ("03_picks-6-10", picks_slide(data, 5))]
    if data["slip_4"]:
        slides.append(("04_slip-4-legs", slip_slide(data, data["slip_4"], "4-leg slip")))
    if data["slip_6"]:
        slides.append(("05_slip-6-legs", slip_slide(data, data["slip_6"], "6-leg slip")))
    out = os.path.join(folder, "carousel")
    os.makedirs(out, exist_ok=True)
    browser = find_browser()
    work_dir = make_dir(tempfile.gettempdir())
    try:
        for name, body in slides:
            page = template.replace("{{TITLE}}", e(name)).replace("{{BODY}}", body)
            render_png(browser, page, os.path.join(out, name + ".png"), work_dir, 540, 675)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    with open(os.path.join(folder, "carousel.json"), "w", encoding="utf-8") as f:
        json.dump({"images": [n + ".png" for n, _ in slides], "caption": caption(data)}, f, ensure_ascii=False,
                  indent=2)
    print(f"{len(slides)} slides -> {out}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", default=dt.date.today().isoformat())
    ap.add_argument("--slip4-odds", type=float, default=8.0)
    ap.add_argument("--slip6-odds", type=float, default=20.0)
    ap.add_argument("--no-render", action="store_true")
    args = ap.parse_args()
    folder = os.path.join(FOOT_DIR, args.date)
    with open(os.path.join(folder, "raw_odds.json"), encoding="utf-8") as f:
        raw = json.load(f)
    games, bets, suspect, unmatched = analyse(raw["rows"])
    for b in bets + suspect:
        b["label"] = label(b, games[b["game"]])
    if unmatched:
        print("Unmatched:", unmatched)
    for b in sorted(suspect, key=lambda x: -x["edge_pct"]):
        g = games[b["game"]]
        print(f"  suspect (edge > {100 * MAX_EDGE:.0f}%, left out): {match_line(g)} {b['label']} "
              f"{b['odds']} @ {b['book']} vs {b['others']}")
    picks = top_picks(bets)
    slip4 = best_slip(bets, 4, args.slip4_odds)
    slip6 = best_slip(bets, 6, args.slip6_odds) or best_slip(bets, 6, args.slip6_odds, max_leg=MAX_ODDS)
    league_n = defaultdict(int)
    for g in games.values():
        league_n[g["league"]] += 1
        g["kickoff"] = g["kickoff"].strftime("%Y-%m-%d %H:%M")
        g.pop("names", None)
    data = {"date": args.date, "built": raw["scraped"], "n_games": len(games), "games": games,
            "leagues": sorted(league_n.items(), key=lambda x: -x[1]), "picks": picks,
            "slip_4": slip4, "slip_6": slip6, "books": raw["books"]}
    with open(os.path.join(folder, "picks.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    with open(os.path.join(folder, "value_bets.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["league", "kickoff", "match", "bet", "book", "odds", "fair_odds", "edge_pct", "books", "others"])
        for b in sorted(bets, key=lambda x: -x["edge_pct"]):
            g = games[b["game"]]
            w.writerow([g["league"], g["kickoff"], match_line(g), b["label"], BOOK_NAMES[b["book"]], b["odds"],
                        b["fair_odds"], b["edge_pct"], b["books"],
                        " ".join(f"{BOOK_NAMES[k]}={v}" for k, v in b["others"].items())])
    print(f"{len(games)} matches, {len(bets)} priced selections, {sum(b['edge_pct'] > 0 for b in bets)} with an edge")
    for i, p in enumerate(picks, 1):
        g = games[p["game"]]
        print(f"{i:2}. {match_line(g):40} {p['label']:32} {p['odds']:.2f} {BOOK_NAMES[p['book']]:9} "
              f"fair {p['fair_odds']:.2f} +{p['edge_pct']:.1f}% ({p['books']} books) {p['others']}")
    for name, s in (("4-leg", slip4), ("6-leg", slip6)):
        if s:
            print(f"{name} @ {BOOK_NAMES[s['book']]}: odds {s['total_odds']:.2f}, win {100 * s['hit_prob']:.2f}%, "
                  f"EV {100 * s['ev']:+.1f}%")
            for l in s["legs"]:
                print(f"     {match_line(games[l['game']])}: {l['label']} {l['odds']} (+{l['edge_pct']}%)")
    if not args.no_render:
        render(data, folder)


if __name__ == "__main__":
    main()
