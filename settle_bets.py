#!/usr/bin/env python3
"""
settle_bets.py

Settles a night's posted bets (Odds/picks/<date>/picks.json from build_bet_picks.py)
against the official EuroLeague box scores, keeps a running ledger of every
settled night, and renders a results carousel (1080x1350, bet_slides_template.html):
  1. cover: the night's record, profit and the all-time tally
  2. the 10 picks with WON / LOST / VOID and the actual stat
  3. the 4-leg slip, leg by leg
  4. the long slip, leg by leg
  5. the arbitrages: which side cashed and the realised profit

Box scores: live.euroleague.net/api/Boxscore (game codes from
api-live.euroleague.net/v2/.../games), cached in Odds/results/<date>/.
Settlement: Over/N+ wins when stat > line, Under when stat < line. A player who
did not play (or is missing from the box score) is VOID, as at the books; a void
leg counts as odds 1.00 on a slip. 1Q/1H markets can't be settled from a box
score and are left OPEN.
Stakes: every pick and slip is 1 unit; arbitrages use the stake split that was
posted (--stake, default 10000 RSD).

Usage:
  python settle_bets.py 2026-10-09 [--stake 10000] [--no-render]
Output: Odds/results/<date>/results.json, Odds/results/ledger.json,
        Odds/results/<date>/carousel_results/NN_*.png + caption.json
"""

import argparse
import datetime as dt
import html
import json
import os
import shutil
import tempfile
import time
import unicodedata
import urllib.request

from build_bet_carousels import TEMPLATE, bet_text, date_label
from build_player_cards import find_browser, font_css, make_dir, render_png

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
PICKS_DIR = os.path.join(PROJECT_DIR, "Odds", "picks")
RESULTS_DIR = os.path.join(PROJECT_DIR, "Odds", "results")
LEDGER = os.path.join(RESULTS_DIR, "ledger.json")
SEASON = "E2026"
GAMES_URL = f"https://api-live.euroleague.net/v2/competitions/E/seasons/{SEASON}/games"
BOX_URL = "https://live.euroleague.net/api/Boxscore?gamecode={}&seasoncode=" + SEASON
UA = {"User-Agent": "Mozilla/5.0"}
e = html.escape

# canonical market -> box score fields that add up to the stat
MARKETS = {
    "Points": ["Points"], "Rebounds": ["TotalRebounds"], "Assists": ["Assistances"],
    "3PM": ["FieldGoalsMade3"], "Steals": ["Steals"], "Blocks": ["BlocksFavour"], "Turnovers": ["Turnovers"],
    "Pts+Reb+Ast": ["Points", "TotalRebounds", "Assistances"], "Pts+Reb": ["Points", "TotalRebounds"],
    "Pts+Ast": ["Points", "Assistances"], "Reb+Ast": ["TotalRebounds", "Assistances"],
}
SHORT = {"Points": "PTS", "Rebounds": "REB", "Assists": "AST", "3PM": "3PM", "Steals": "STL", "Blocks": "BLK",
         "Turnovers": "TO", "Pts+Reb+Ast": "PRA", "Pts+Reb": "P+R", "Pts+Ast": "P+A", "Reb+Ast": "R+A"}

# -----------------------------------------------------------------------------
# DATA
# -----------------------------------------------------------------------------

def fetch_json(url, path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        data = json.load(r)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    time.sleep(1)
    return data


def fold(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return "".join(c if c.isalnum() else " " for c in s).split()


def load_box(day, folder):
    """{game code: {"teams": [...], "players": [{name tokens, stats}]}} for the night's played games."""
    games = fetch_json(GAMES_URL, os.path.join(folder, "games.json"))
    if isinstance(games, dict):
        games = games.get("data", [])
    out = {}
    for g in games:
        if not str(g.get("date", "")).startswith(day) or not g.get("played"):
            continue
        box = fetch_json(BOX_URL.format(g["gameCode"]), os.path.join(folder, f"box_{g['gameCode']}.json"))
        players = []
        for team in box["Stats"]:
            for p in team["PlayersStats"]:
                last, _, first = p["Player"].partition(",")
                players.append({"last": fold(last), "first": fold(first), "name": p["Player"], "stats": p,
                                "played": p.get("Minutes") not in (None, "", "DNP")})
        out[g["gameCode"]] = {"local": g["local"]["club"]["name"], "road": g["road"]["club"]["name"],
                              "score": f'{g["local"]["score"]}-{g["road"]["score"]}', "players": players}
    return out


def game_for(box, game_name):
    """Match 'Olympiacos vs Anadolu Efes' to a box score by team-name words."""
    words = set(fold(game_name)) - {"vs", "fc", "bc", "kk"}
    best = max(box.values(), key=lambda g: len(words & set(fold(g["local"] + " " + g["road"]))), default=None)
    return best if best and words & set(fold(best["local"] + " " + best["road"])) else None


def find_player(game, name):
    toks = [t for t in fold(name) if t not in ("jr", "sr", "ii", "iii")]
    if not toks:
        return None
    first, last = toks[0], toks[-1]
    hits = [p for p in game["players"] if last in p["last"] or "".join(toks[1:]) == "".join(p["last"])]
    if len(hits) > 1:
        hits = [p for p in hits if p["first"] and p["first"][0][0] == first[0]] or hits
    if not hits:  # reversed order ("Dozier PJ") or a surname written as first token
        hits = [p for p in game["players"] if first in p["last"]]
    return hits[0] if len(hits) == 1 else None

# -----------------------------------------------------------------------------
# SETTLEMENT
# -----------------------------------------------------------------------------

def settle_leg(bet, box):
    """Adds status (won/lost/void/open), stat and player_found to a copy of the bet."""
    out = dict(bet)
    fields = MARKETS.get(bet["market"])
    game = game_for(box, bet["game"])
    if not fields or game is None:
        out.update(status="open", note="market can't be settled from the box score" if not fields
                   else "game not found")
        return out
    p = find_player(game, bet["player"])
    if p is None or not p["played"]:
        out.update(status="void", stat=None, note="did not play" if p else "not in the box score")
        return out
    stat = sum(p["stats"].get(k) or 0 for k in fields)
    side = bet["side"].lower()
    won = stat > bet["line"] if side in ("over", "yes") or side.endswith("+") else stat < bet["line"]
    out.update(status="won" if won else "lost", stat=stat, minutes=p["stats"]["Minutes"])
    return out


def settle_slip(slip, box):
    legs = [settle_leg(l, box) for l in slip["legs"]]
    states = {l["status"] for l in legs}
    if "lost" in states:
        status = "lost"
    elif "open" in states:
        status = "open"
    else:
        status = "won"
    odds = 1.0
    for l in legs:
        odds *= 1.0 if l["status"] == "void" else l["odds"]
    profit = (odds - 1) if status == "won" else (-1 if status == "lost" else 0)
    return {**slip, "legs": legs, "status": status, "settled_odds": round(odds, 2), "profit": round(profit, 2),
            "hits": sum(l["status"] == "won" for l in legs)}


def settle_arb(a, box, stake):
    io, iu = 1 / a["over_odds"], 1 / a["under_odds"]
    so = round(stake * io / (io + iu) / 10) * 10  # same split as the posted slide
    su = stake - so
    leg = settle_leg({"game": a["game"], "player": a["player"], "market": a["market"], "side": "Over",
                      "line": a["line"]}, box)
    if leg["status"] in ("void", "open"):
        ret, side = None, leg["status"]
    else:
        side = "over" if leg["status"] == "won" else "under"
        ret = so * a["over_odds"] if side == "over" else su * a["under_odds"]
    return {**a, "stake_over": so, "stake_under": su, "status": side, "stat": leg.get("stat"),
            "returned": None if ret is None else round(ret), "profit": None if ret is None else round(ret - stake)}


def settle(day, stake):
    with open(os.path.join(PICKS_DIR, day, "picks.json"), encoding="utf-8") as f:
        data = json.load(f)
    folder = os.path.join(RESULTS_DIR, day)
    os.makedirs(folder, exist_ok=True)
    box = load_box(day, folder)
    if not box:
        raise SystemExit(f"No played EuroLeague games found for {day}.")
    picks = [settle_leg(p, box) for p in data["picks"]]
    for p in picks:
        p["profit"] = round(p["odds"] - 1, 2) if p["status"] == "won" else (-1 if p["status"] == "lost" else 0)
    res = {"date": day, "settled": dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "stake": stake,
           "games": [f'{g["local"]} {g["score"]} {g["road"]}' for g in box.values()],
           "picks": picks, "slip_4": settle_slip(data["slip_4"], box),
           "slip_long": settle_slip(data["slip_long"], box),
           "arbitrage": [settle_arb(a, box, stake) for a in data["arbitrage"]]}
    with open(os.path.join(folder, "results.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    update_ledger(res)
    return res


def summary(res):
    picks = res["picks"]
    won = sum(p["status"] == "won" for p in picks)
    lost = sum(p["status"] == "lost" for p in picks)
    void = sum(p["status"] == "void" for p in picks)
    staked = won + lost
    profit = sum(p["profit"] for p in picks)
    arbs = [a for a in res["arbitrage"] if a["profit"] is not None]
    return {"won": won, "lost": lost, "void": void, "profit": round(profit, 2),
            "roi": round(100 * profit / staked, 1) if staked else 0.0,
            "slips_won": sum(s["status"] == "won" for s in (res["slip_4"], res["slip_long"])),
            "slip_profit": round(res["slip_4"]["profit"] + res["slip_long"]["profit"], 2),
            "arb_profit": sum(a["profit"] for a in arbs), "arb_stake": res["stake"] * len(arbs), "arbs": len(arbs)}


def update_ledger(res):
    try:
        with open(LEDGER, encoding="utf-8") as f:
            ledger = json.load(f)
    except FileNotFoundError:
        ledger = {"nights": {}}
    ledger["nights"][res["date"]] = summary(res)
    with open(LEDGER, "w", encoding="utf-8") as f:
        json.dump(ledger, f, ensure_ascii=False, indent=2)


def all_time():
    with open(LEDGER, encoding="utf-8") as f:
        nights = json.load(f)["nights"].values()
    t = {k: sum(n[k] for n in nights) for k in ("won", "lost", "void", "profit", "slip_profit", "arb_profit",
                                                  "arb_stake", "arbs", "slips_won")}
    t["nights"] = len(nights)
    t["roi"] = round(100 * t["profit"] / (t["won"] + t["lost"]), 1) if t["won"] + t["lost"] else 0.0
    return t

# -----------------------------------------------------------------------------
# RENDER
# -----------------------------------------------------------------------------

def pill(status):
    return f'<span class="st {status}">{status.upper()}</span>'


def actual(b):
    if b["status"] in ("won", "lost"):
        return f'{b["stat"]:g} {SHORT.get(b["market"], b["market"])}'
    return b.get("note", "")


def units(x):
    return f'{x:+.2f}u'


def slide(kick, body, res, cls="res"):
    return (f'<div class="slide {cls}"><div class="top"><span class="kick">{e(kick)}</span>'
            f'<span class="date">{e(date_label(res["date"]))} · settled</span></div>{body}'
            f'<div class="foot"><span><b>18+</b> · Results from the official EuroLeague box scores · '
            f'Past results don\'t predict future ones</span><span>1 unit per pick / slip</span></div></div>')


def cover(res, s, t):
    games = "".join(f'<div>{e(g)}</div>' for g in res["games"])
    tone = "up" if s["profit"] >= 0 else "down"
    void = f' · {s["void"]} void' if s["void"] else ""
    rows = "".join(f'<div class="li"><span class="p">{e(p["player"])}<small>{bet_text(p)}</small></span>'
                   f'<span class="o">{p["odds"]:.2f}</span>{pill(p["status"])}</div>' for p in res["picks"])
    body = (f'<h1>Last night\'s<br><span>results</span></h1>'
            f'<div class="score"><div class="rec"><b>{s["won"]}-{s["lost"]}</b><span>picks won-lost{void}</span></div>'
            f'<div class="pnl {tone}"><b>{units(s["profit"])}</b><span>profit on the 10 picks · ROI {s["roi"]:+.0f}%</span></div></div>'
            f'<div class="mini"><div><span>4-leg slip</span>{pill(res["slip_4"]["status"])}'
            f'<em>{res["slip_4"]["hits"]}/{len(res["slip_4"]["legs"])} legs</em></div>'
            f'<div><span>{len(res["slip_long"]["legs"])}-leg slip</span>{pill(res["slip_long"]["status"])}'
            f'<em>{res["slip_long"]["hits"]}/{len(res["slip_long"]["legs"])} legs</em></div>'
            f'<div><span>Arbitrages</span><span class="st won">+{s["arb_profit"]:,} RSD</span>'
            f'<em>{s["arbs"]} of {len(res["arbitrage"])} cashed</em></div></div>'
            f'<div class="games">{games}</div><div class="list">{rows}</div>'
            f'<div class="alltime"><b>All-time</b> · {t["nights"]} night{"s" if t["nights"] != 1 else ""} · picks '
            f'{t["won"]}-{t["lost"]} · {units(t["profit"])} (ROI {t["roi"]:+.0f}%) · slips {t["slips_won"]} won · '
            f'arbs +{t["arb_profit"]:,} RSD</div>')
    return slide("EuroLeague · bet results", body, res)


def picks_slide(res):
    rows = []
    for i, p in enumerate(res["picks"], 1):
        rows.append(f'<div class="rr {p["status"]}"><span class="n">{i}</span>'
                    f'<div class="who"><span class="pl">{e(p["player"])}</span><span class="bt">{bet_text(p)} · '
                    f'{p["odds"]:.2f} @ {e(p["book_name"])}</span></div>'
                    f'<div class="act"><b>{e(actual(p))}</b><span>{units(p["profit"])}</span></div>{pill(p["status"])}</div>')
    body = (f'<div class="head">All 10 picks<small>Each pick 1 unit at the posted price · the actual stat '
            f'from the box score</small></div><div class="rlist">{"".join(rows)}</div>')
    return slide("Picks · settled", body, res)


def slip_slide(res, s, title, long_=False):
    n = len(s["legs"])
    size = "--lp:3px;--lf:14px;--bf:9px;--of:12px" if long_ else ""
    legs = "".join(f'<div class="sl {l["status"]}"><div><div class="pl">{e(l["player"])}</div>'
                   f'<div class="bt">{bet_text(l)} · <b>{e(actual(l))}</b></div></div>'
                   f'<span class="o">{l["odds"]:.2f}</span>{pill(l["status"])}</div>' for l in s["legs"])
    verdict = ("Cashed!" if s["status"] == "won" else "Busted" if s["status"] == "lost" else "Pending")
    body = (f'<div class="head">{e(title)}<small>{s["hits"]} of {n} legs hit · one ticket at {e(s["book_name"])}'
            f'</small></div>'
            f'<div class="slip" style="{size}"><div class="sh"><b>{n}-leg slip</b>{pill(s["status"])}</div>{legs}'
            f'<div class="tot"><span class="k">Total odds {s["total_odds"]:,.2f}</span>'
            f'<span class="v {s["status"]}">{verdict}</span></div></div>'
            f'<div class="facts"><div><span>Legs hit</span><b>{s["hits"]}/{n}</b></div>'
            f'<div><span>Pre-game win chance</span><b>{100 * s["hit_prob"]:.{2 if s["hit_prob"] < .01 else 1}f}%</b></div>'
            f'<div><span>Result</span><b class="{"pos" if s["profit"] > 0 else "neg"}">{units(s["profit"])}</b></div></div>'
            f'<div class="note">{"Long shots by design: " if s["status"] != "won" else ""}the slip needed every leg. '
            f'Whether the picks have an edge only shows over many nights, not one.</div>')
    return slide("Slip · settled", body, res)


def arbs_slide(res, s):
    rows = []
    for a in res["arbitrage"]:
        side = a["status"]
        cashed = (f'<b>{"Over" if side == "over" else "Under"} cashed</b> · {a["stat"]:g} '
                  f'{SHORT.get(a["market"], a["market"])}' if side in ("over", "under") else e(side.upper()))
        profit = "" if a["profit"] is None else f'+{a["profit"]:,}'
        rows.append(f'<div class="ar"><div class="who"><span class="pl">{e(a["player"])}</span>'
                    f'<span class="bt">{e(a["market"])} {a["line"]:g} · O {a["over_odds"]:.2f} {e(a["over_book"])} / '
                    f'U {a["under_odds"]:.2f} {e(a["under_book"])}</span><span class="cs">{cashed}</span></div>'
                    f'<div class="pr"><b>{profit}</b>'
                    f'<span>RSD on {res["stake"]:,.0f}</span></div></div>')
    body = (f'<div class="head">Arbitrages<small>One side always cashes · stakes split as posted, '
            f'{res["stake"]:,.0f} RSD each</small></div><div class="alist">{"".join(rows)}</div>'
            f'<div class="facts"><div><span>Total profit</span><b>+{s["arb_profit"]:,} RSD</b></div>'
            f'<div><span>Total staked</span><b>{s["arb_stake"]:,.0f}</b></div>'
            f'<div><span>Return</span><b>+{100 * s["arb_profit"] / s["arb_stake"]:.1f}%</b></div></div>')
    return slide("Arbitrage · settled", body, res, "res arb")


EXTRA_CSS = """
<style>
.slide.res { --accent: var(--pop); }
.res h1 { font-size: 50px; margin-top: 10px; }
.res .li small i { font-style: normal; font-size: 8px; font-weight: 800; color: #ff9a6b; border: 1px solid currentColor; border-radius: 3px; padding: 0 3px; margin-left: 4px; }
.st { display: inline-block; font-family: var(--font); font-size: 9px; font-weight: 800; letter-spacing: .1em; padding: 3px 7px 2px;
  border-radius: 999px; text-align: center; white-space: nowrap; }
.st.won { background: #2fd27a; color: #062414; } .st.lost { background: #ff5a4f; color: #2a0705; }
.st.void, .st.open { background: rgba(255, 255, 255, .18); color: #fff; }
.score { margin-top: 10px; display: grid; grid-template-columns: 1fr 1.2fr; gap: 10px; }
.score > div { background: rgba(255, 255, 255, .06); border-radius: 10px; padding: 7px 14px; display: grid; }
.score b { font-family: var(--display); font-weight: 800; font-size: 46px; line-height: .95; }
.score span { font-size: 10px; font-weight: 700; color: var(--muted); }
.pnl.up b { color: #2fd27a; } .pnl.down b { color: #ff5a4f; }
.mini { margin-top: 10px; display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; }
.mini > div { background: rgba(255, 255, 255, .06); border-radius: 8px; padding: 6px 10px; display: grid; gap: 3px; justify-items: start; }
.mini > div > span:first-child { font-size: 8.5px; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); }
.mini em { font-style: normal; font-size: 10px; font-weight: 700; color: var(--muted); }
.res .games { margin-top: 10px; display: grid; gap: 1px; }
.res .games div { font-size: 12.5px; }
.res .list { margin-top: 8px; gap: 3px; }
.res .li { grid-template-columns: 1fr auto 52px; padding: 2px 10px; }
.res .li .p { font-size: 14px; }
.res .li .o { font-size: 12px; }
.alltime { margin-top: 8px; font-size: 9.5px; font-weight: 600; color: var(--muted); border-top: 1px solid var(--faint); padding-top: 7px; }
.alltime b { color: var(--pop); text-transform: uppercase; letter-spacing: .08em; }
.rlist { margin-top: 12px; display: grid; gap: 6px; }
.rr { display: grid; grid-template-columns: 24px 1fr auto 52px; gap: 10px; align-items: center; padding: 8px 12px; border-radius: 9px;
  background: var(--paper); color: var(--paper-ink); border-left: 5px solid #999; }
.rr.won { border-left-color: #2fd27a; } .rr.lost { border-left-color: #ff5a4f; opacity: .9; }
.rr .n { font-family: var(--display); font-weight: 800; font-size: 22px; color: var(--paper-muted); text-align: center; }
.rr .who { min-width: 0; display: grid; }
.rr .pl { font-family: var(--display); font-weight: 800; font-size: 19px; line-height: 1; text-transform: uppercase; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.rr .bt { font-size: 10px; font-weight: 600; color: var(--paper-muted); white-space: nowrap; }
.rr .bt i { font-style: normal; font-size: 8px; font-weight: 800; color: #a8461d; border: 1px solid currentColor; border-radius: 3px; padding: 0 3px; margin-left: 4px; }
.rr .act { text-align: right; display: grid; }
.rr .act b { font-family: var(--display); font-weight: 800; font-size: 18px; line-height: 1; }
.rr .act span { font-family: var(--mono); font-size: 10px; font-weight: 700; color: var(--paper-muted); }
.rr.won .act span { color: #0b8a4f; } .rr.lost .act span { color: #b42318; }
.sl { grid-template-columns: 1fr auto 52px; }
.sl.lost .pl, .sl.lost .o { color: #b42318; } .sl.won .pl { color: #0b6b3c; }
.sl .bt b { color: var(--paper-ink); }
.slip .sh .st { align-self: center; }
.facts b.neg { color: #ff5a4f; } .facts b.pos { color: #2fd27a; }
.tot .v.won { color: #0b8a4f; } .tot .v.lost { color: #b42318; font-size: 38px; } .tot .k { align-self: center; }
.alist { margin-top: 14px; display: grid; gap: 8px; }
.ar { display: grid; grid-template-columns: 1fr auto; gap: 10px; align-items: center; padding: 10px 14px; border-radius: 10px; background: var(--paper); color: var(--paper-ink); border-left: 5px solid var(--arb); }
.ar .who { display: grid; gap: 1px; min-width: 0; }
.ar .pl { font-family: var(--display); font-weight: 800; font-size: 21px; line-height: 1; text-transform: uppercase; }
.ar .bt { font-size: 10px; font-weight: 600; color: var(--paper-muted); }
.ar .cs { font-size: 11px; font-weight: 600; } .ar .cs b { color: #0b6b3c; }
.ar .pr { text-align: right; display: grid; }
.ar .pr b { font-family: var(--display); font-weight: 800; font-size: 28px; color: #0b8a4f; line-height: 1; }
.ar .pr span { font-size: 9px; font-weight: 700; color: var(--paper-muted); }
</style>
"""


def caption(res, s, t):
    lines = [f"🏀 EuroLeague bet results · {date_label(res['date'])}", "",
             f"Picks: {s['won']}-{s['lost']}" + (f" ({s['void']} void)" if s["void"] else "")
             + f" · {units(s['profit'])} · ROI {s['roi']:+.0f}%", ""]
    for p in res["picks"]:
        icon = {"won": "✅", "lost": "❌"}.get(p["status"], "➖")
        lines.append(f"{icon} {p['player']} {p['market']} {p['side']} {p['line']:g} @ {p['odds']:.2f} → {actual(p)}")
    for s_, name in ((res["slip_4"], "4-leg slip"), (res["slip_long"], f"{len(res['slip_long']['legs'])}-leg slip")):
        lines.append(f"\n🎟️ {name} ({s_['total_odds']:,.2f}): {s_['status'].upper()} · {s_['hits']}/{len(s_['legs'])} legs hit")
    lines += ["", f"💰 Arbitrages: {s['arbs']}/{len(res['arbitrage'])} cashed, +{s['arb_profit']:,} RSD on "
                  f"{s['arb_stake']:,.0f} staked", "",
              f"All-time: picks {t['won']}-{t['lost']} · {units(t['profit'])} · arbs +{t['arb_profit']:,} RSD", "",
              "Settled from the official EuroLeague box scores. 1 unit per pick/slip. Past results don't predict "
              "future ones. 18+, bet responsibly.", "",
              "#EuroLeague #PlayerProps #SportsBetting #BettingResults #Basketball"]
    return "\n".join(lines)[:2200]


def render(res, out_dir):
    s, t = summary(res), all_time()
    with open(TEMPLATE, encoding="utf-8") as f:
        template = f.read().replace("{{FONTS}}", font_css()).replace("{{BODY}}", EXTRA_CSS + "{{BODY}}")
    slides = [("01_cover", cover(res, s, t)), ("02_picks", picks_slide(res)),
              ("03_slip-4-legs", slip_slide(res, res["slip_4"], "4-leg slip")),
              ("04_slip-long", slip_slide(res, res["slip_long"], f"{len(res['slip_long']['legs'])}-leg slip", True)),
              ("05_arbs", arbs_slide(res, s))]
    os.makedirs(out_dir, exist_ok=True)
    browser = find_browser()
    work_dir = make_dir(tempfile.gettempdir())
    try:
        for name, body in slides:
            page = template.replace("{{TITLE}}", e(name)).replace("{{BODY}}", body)
            render_png(browser, page, os.path.join(out_dir, name + ".png"), work_dir, 540, 675)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    with open(os.path.join(out_dir, "caption.json"), "w", encoding="utf-8") as f:
        json.dump({"images": [n + ".png" for n, _ in slides], "caption": caption(res, s, t)}, f,
                  ensure_ascii=False, indent=2)
    print(f"{len(slides)} slides -> {out_dir}")


def main():
    ap = argparse.ArgumentParser(description="Settle a night's posted bets.")
    ap.add_argument("date", nargs="?", default=(dt.date.today() - dt.timedelta(days=1)).isoformat())
    ap.add_argument("--stake", type=float, default=10000, help="total arbitrage stake used on the slides")
    ap.add_argument("--no-render", action="store_true")
    args = ap.parse_args()
    res = settle(args.date, args.stake)
    s = summary(res)
    for p in res["picks"]:
        print(f'{p["status"].upper():5} {p["player"]:24} {p["market"]} {p["side"]} {p["line"]:g} @ {p["odds"]:.2f}'
              f' -> {actual(p)}')
    for k in ("slip_4", "slip_long"):
        sl = res[k]
        print(f'{k}: {sl["status"].upper()} {sl["hits"]}/{len(sl["legs"])} -> ' +
              ", ".join(f'{l["player"]} {l["status"]}' for l in sl["legs"]))
    for a in res["arbitrage"]:
        print(f'arb {a["player"]} {a["market"]} {a["line"]:g}: {a["status"]} stat={a["stat"]} profit={a["profit"]}')
    print(s)
    if not args.no_render:
        render(res, os.path.join(RESULTS_DIR, args.date, "carousel_results"))


if __name__ == "__main__":
    main()
