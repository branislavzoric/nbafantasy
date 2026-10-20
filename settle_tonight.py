#!/usr/bin/env python3
"""
settle_tonight.py

Settles a night's posted combined card (Odds/tonight/<date>/tonight.json from build_tonight_bets.py):
EuroLeague player props + game lines, football (Champions League / Championship), the 4/5/6-leg
slips and the 5 posted arbitrages. Renders a results carousel with a "why it happened" slide.

Sources: EuroLeague box scores (settle_bets.load_box) and ESPN's soccer scoreboard
(site.api.espn.com/apis/site/v2/sports/soccer/<league>/scoreboard?dates=YYYYMMDD).
Rules as in settle_bets.py: Over wins when stat > line, Under when stat < line, DNP = VOID (1.00 on a slip).
Basketball game lines include overtime (as posted). Football markets are 90 minutes.

The analysis slide reads Odds/results/<date>/analysis.json ({"headline", "points": [{"title", "text"}]}),
written by hand after looking at the box scores; without it the slide is skipped.

Usage:
  python settle_tonight.py 2026-10-13 [--stake 10000] [--min-edge 1.0] [--no-render]
Output: Odds/results/<date>/results.json, ledger.json, carousel_results/ + caption.json
"""

import argparse
import datetime as dt
import html
import json
import os
import re
import shutil
import tempfile

import settle_bets as sb
from build_bet_carousels import TEMPLATE, date_label
from build_player_cards import find_browser, font_css, make_dir, render_png

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
TONIGHT_DIR = os.path.join(PROJECT_DIR, "Odds", "tonight")
RESULTS_DIR = sb.RESULTS_DIR
FOOTBALL = {"Champions League": "uefa.champions", "Championship": "eng.2"}
FB_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/{}/scoreboard?dates={}"
BOOK = {"soccerbet": "Soccerbet", "maxbet": "MaxBet", "admiralbet": "Admiral", "mozzartbet": "Mozzart",
        "superbet": "Superbet", "balkanbet": "Balkanbet"}
e = html.escape
PROP_RE = re.compile(r"(Over|Under) ([\d.]+) (.+?)( \(N\+ line\))?$")

# -----------------------------------------------------------------------------
# SETTLEMENT
# -----------------------------------------------------------------------------

def football_scores(day, folder):
    out = []
    for league, code in FOOTBALL.items():
        data = sb.fetch_json(FB_URL.format(code, day.replace("-", "")), os.path.join(folder, f"fb_{code}.json"))
        for ev in data.get("events", []):
            c = ev["competitions"][0]
            if not c["status"]["type"].get("completed"):
                continue
            side = {t["homeAway"]: t for t in c["competitors"]}
            out.append({"league": league, "home": side["home"]["team"]["displayName"],
                        "away": side["away"]["team"]["displayName"],
                        "hg": int(side["home"]["score"]), "ag": int(side["away"]["score"])})
    return out


def match_game(games, name, key=lambda g: g["home"] + " " + g["away"]):
    words = set(sb.fold(name)) - {"vs", "fc", "bc", "kk", "city", "the"}
    best = max(games, key=lambda g: len(words & set(sb.fold(key(g)))), default=None)
    return best if best and words & set(sb.fold(key(best))) else None


def settle_prop(b, box):
    m = PROP_RE.match(b["bet"][len(b["player"]) + 1:])
    leg = {"game": b["match"], "player": b["player"], "market": m.group(3), "side": m.group(1),
           "line": float(m.group(2)), "odds": b["odds"], "book_name": BOOK[b["book"]],
           "milestone": bool(m.group(4)), "fair_odds": b.get("fair_odds"), "edge_pct": b.get("edge_pct"),
           "prob": b.get("prob"), "kind": "prop"}
    out = sb.settle_leg(leg, box)
    if out["status"] in ("won", "lost"):
        p = sb.find_player(sb.game_for(box, b["match"]), b["player"])
        st = p["stats"]
        out["line_detail"] = (f'{st["Minutes"]} min · {st["Points"]} pts · {st["TotalRebounds"]} reb · '
                              f'{st["Assistances"]} ast · 3P {st["FieldGoalsMade3"]}/{st["FieldGoalsAttempted3"]} · '
                              f'FG {st["FieldGoalsMade2"] + st["FieldGoalsMade3"]}/'
                              f'{st["FieldGoalsAttempted2"] + st["FieldGoalsAttempted3"]}')
    out["text"] = f'{out["market"]} {out["side"]} {out["line"]:g}'
    return out


def line_result(bet, home_name, h, a):
    """Winner / handicap / total from the final score (incl. OT). -> (status, stat text)."""
    m = re.match(r"(Over|Under) ([\d.]+) points", bet)
    if m:
        tot, line = h + a, float(m.group(2))
        diff = tot - line if m.group(1) == "Over" else line - tot
        return ("won" if diff > 0 else "lost" if diff < 0 else "void"), f"{tot} pts ({h}-{a})"
    m = re.match(r"(.+) ([+-][\d.]+) \(incl\. OT\)$", bet)
    if m:                                               # handicap: the bet team's margin plus its line
        margin = (h - a) if m.group(1) == home_name else (a - h)
        diff = margin + float(m.group(2))
        return ("won" if diff > 0 else "lost" if diff < 0 else "void"), f"{h}-{a}"
    team = bet.split(" to win")[0]
    return ("won" if (h > a if team == home_name else a > h) else "lost"), f"{h}-{a}"


def settle_line(b, box):
    games = [{"home": g["local"], "away": g["road"], "score": g["score"]} for g in box.values()]
    g = match_game(games, b["match"].replace("–", " "))
    out = {**b, "kind": "line", "player": b["match"], "book_name": BOOK[b["book"]], "text": b["bet"]}
    if g is None:
        return {**out, "status": "open", "note": "game not found"}
    h, a = map(int, g["score"].split("-"))
    status, stat = line_result(b["bet"], b["match"].split(" – ")[0], h, a)
    return {**out, "status": status, "stat_text": stat}


_NBA = {}


def nba_scores(day):
    """Final NBA scores for the board's date (ESPN, US Eastern date = the night of the board)."""
    if day not in _NBA:
        import collect_league_data as cl
        _NBA[day] = [g for g in cl.nba_day(dt.date.fromisoformat(day)) if g["state"] == "post"]
    return _NBA[day]


def settle_nba_line(b, day):
    out = {**b, "kind": "line", "player": b["match"], "book_name": BOOK[b["book"]], "text": b["bet"]}
    games = [{"home": g["home_name"], "away": g["away_name"], "h": g["home_score"], "a": g["away_score"]}
             for g in nba_scores(day)]
    g = match_game(games, b["match"].replace("–", " "))
    if g is None:
        return {**out, "status": "open", "note": "game not final / not found"}
    status, stat = line_result(b["bet"], b["match"].split(" – ")[0], g["h"], g["a"])
    return {**out, "status": status, "stat_text": stat}


NBA_FIELDS = {"Points": ["pts"], "Rebounds": ["reb"], "Assists": ["ast"], "3PM": ["tpm"], "Steals": ["stl"],
              "Blocks": ["blk"], "Turnovers": ["tov"], "Pts+Reb+Ast": ["pts", "reb", "ast"], "Pts+Reb": ["pts", "reb"],
              "Pts+Ast": ["pts", "ast"], "Reb+Ast": ["reb", "ast"]}
SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}
_NBA_BOX = {}


def nba_day_of(kickoff):
    """Board kickoff (Belgrade time) -> US Eastern game date: tip-offs after midnight belong to the evening before."""
    return kickoff[:10] if kickoff[11:13] >= "12" else (dt.date.fromisoformat(kickoff[:10]) - dt.timedelta(days=1)).isoformat()


def nba_box(day):
    """Final NBA games of one US Eastern date with their player lines (collect_boxscores.parse_game on ESPN's
    summary JSON; reuses "Box Scores/<date>/<id>.json" when collect_boxscores.py already saved it)."""
    if day not in _NBA_BOX:
        import collect_boxscores as cb
        out = []
        for g in nba_scores(day):
            path = os.path.join(cb.RAW_DIR, day, f'{g["id"]}.json')
            os.makedirs(os.path.dirname(path), exist_ok=True)
            summary = sb.fetch_json(f'{cb.SUMMARY_URL}?event={g["id"]}', path)
            _, lines = cb.parse_game(summary, dt.date.fromisoformat(day), dt.datetime.now())
            out.append({"home": g["home_name"], "away": g["away_name"], "lines": lines})
        _NBA_BOX[day] = out
    return _NBA_BOX[day]


def nba_name(s):
    return [w for w in sb.fold(s) if w not in SUFFIXES]


def nba_player(lines, name):
    """Book name -> ESPN line: same full name, else same surname + first initial ("Shai Gilgeous Alexander",
    "Au.Thompson" style initials are handled by the surname + initial rule)."""
    want = nba_name(name)
    if not want:
        return None
    exact = [l for l in lines if nba_name(l["player_name"]) == want]
    if exact:
        return exact[0]
    hits = [l for l in lines if (n := nba_name(l["player_name"])) and n[-1] == want[-1] and n[0][0] == want[0][0]]
    if len(hits) > 1:                                   # Ausar / Amen Thompson: longer first-name prefix wins
        hits = [l for l in hits if nba_name(l["player_name"])[0].startswith(want[0].rstrip("."))] or hits
    return hits[0] if len(hits) == 1 else None


def nba_prop_leg(match, player, market, side, line, day):
    """-> {status, stat, minutes, detail} for one NBA player prop (Over wins when stat > line, DNP = void)."""
    fields = NBA_FIELDS.get(market)
    games = nba_box(day)
    g = match_game(games, match.replace(" vs ", " ").replace("–", " "))
    if not fields:
        return {"status": "open", "note": "market can't be settled from the box score"}
    if g is None:
        return {"status": "open", "note": "game not final / not found"}
    p = nba_player(g["lines"], player)
    if p is None or p["did_not_play"] or not p["min"]:
        return {"status": "void", "stat": None, "note": "did not play" if p else "not in the box score"}
    stat = sum(p[k] or 0 for k in fields)
    won = stat > line if side.lower() in ("over", "yes") or side.endswith("+") else stat < line
    detail = (f'{p["min"]} min · {p["pts"]} pts · {p["reb"]} reb · {p["ast"]} ast · '
              f'3P {p["tpm"]}/{p["tpa"]} · FG {p["fgm"]}/{p["fga"]}')
    return {"status": "won" if won else "lost", "stat": stat, "minutes": p["min"], "line_detail": detail}


def settle_nba_prop(b):
    m = PROP_RE.match(b["bet"][len(b["player"]) + 1:])
    out = {**b, "kind": "prop", "game": b["match"], "market": m.group(3), "side": m.group(1),
           "line": float(m.group(2)), "milestone": bool(m.group(4)), "book_name": BOOK[b["book"]]}
    out.update(nba_prop_leg(b["match"], b["player"], out["market"], out["side"], out["line"], nba_day_of(b["kickoff"])))
    out["text"] = f'{out["market"]} {out["side"]} {out["line"]:g}'
    return out


def settle_football(b, scores):
    out = {**b, "kind": "football", "player": b["match"], "book_name": BOOK[b["book"]], "text": b["bet"]}
    g = match_game([s for s in scores if s["league"] == b["league"]], b["match"].replace("–", " "))
    if g is None:
        return {**out, "status": "open", "note": "match not found"}
    hg, ag, bet = g["hg"], g["ag"], b["bet"]
    is_home = lambda t: t == b["match"].split(" – ")[0]
    if bet.startswith("Both teams NOT"):
        won = hg == 0 or ag == 0
    elif bet.startswith("Both teams"):
        won = hg > 0 and ag > 0
    elif m := re.match(r"(Over|Under) ([\d.]+) goals", bet):
        won = (hg + ag > float(m.group(2))) if m.group(1) == "Over" else (hg + ag < float(m.group(2)))
    elif bet.endswith(" or draw"):
        t = bet[:-len(" or draw")]
        won = hg >= ag if is_home(t) else ag >= hg
    elif bet == "Draw":
        won = hg == ag
    elif bet.endswith(" to win"):
        t = bet[:-len(" to win")]
        won = hg > ag if is_home(t) else ag > hg
    else:
        return {**out, "status": "open", "note": "market not handled"}
    return {**out, "status": "won" if won else "lost", "stat_text": f"{hg}-{ag}"}


def source_of(b):
    """NBA card legs may carry no source: a player leg is an NBA prop, anything else an NBA game line."""
    return b.get("source") or ("NBA props" if b.get("player") else "NBA lines")


def settle_any(b, box, scores):
    src = source_of(b)
    if src == "NBA lines":
        return settle_nba_line(b, nba_day_of(b["kickoff"]))
    if src == "NBA props":
        return settle_nba_prop(b)
    if src == "EL props":
        return settle_prop(b, box)
    if b["source"] == "EL lines":
        return settle_line(b, box)
    return settle_football(b, scores)


def with_profit(x):
    x["profit"] = round(x["odds"] - 1, 2) if x["status"] == "won" else (-1 if x["status"] == "lost" else 0)
    return x


def settle_slip(slip, box, scores, n):
    legs = [settle_any(l, box, scores) for l in slip["legs"]]
    states = {l["status"] for l in legs}
    status = "lost" if "lost" in states else "open" if "open" in states else "won"
    odds = 1.0
    for l in legs:
        odds *= 1.0 if l["status"] == "void" else l["odds"]
    profit = (odds - 1) if status == "won" else (-1 if status == "lost" else 0)
    return {"n": n, "book_name": slip["book"], "total_odds": slip["total_odds"], "hit_prob": slip["hit_prob"],
            "legs": legs, "status": status, "settled_odds": round(odds, 2), "profit": round(profit, 2),
            "hits": sum(l["status"] == "won" for l in legs)}


def settle_arb(a, box, stake):
    m = re.match(r"(.+) (\S+) ([\d.]+)$", a["market"])
    player, market, line = m.group(1), m.group(2), float(m.group(3))
    over, under = a["legs"]
    so = round(stake * over["stake_pct"] / 100 / 10) * 10
    su = stake - so
    if a.get("source", "NBA props") == "NBA props":    # NBA card arbs carry no source and no kickoff
        leg = (nba_prop_leg(a["match"], player, market, "Over", line, a["_day"]) if a.get("_day")
               else {"status": "open", "note": "game not on the board"})
    else:
        leg = sb.settle_leg({"game": a["match"], "player": player, "market": market, "side": "Over", "line": line}, box)
    if leg["status"] in ("void", "open"):
        ret, side = None, leg["status"]
    else:
        side = "over" if leg["status"] == "won" else "under"
        ret = so * over["odds"] if side == "over" else su * under["odds"]
    return {"player": player, "market": market, "line": line, "match": a["match"],
            "over_odds": over["odds"], "over_book": over["book"], "under_odds": under["odds"],
            "under_book": under["book"], "profit_pct": a["profit_pct"], "stake_over": so, "stake_under": su,
            "status": side, "stat": leg.get("stat"),
            "returned": None if ret is None else round(ret), "profit": None if ret is None else round(ret - stake)}


def settle(day, stake, min_edge):
    with open(os.path.join(TONIGHT_DIR, day, "tonight.json"), encoding="utf-8") as f:
        data = json.load(f)
    folder = os.path.join(RESULTS_DIR, day)
    os.makedirs(folder, exist_ok=True)
    box = sb.load_box(day, folder)
    scores = football_scores(day, folder)
    picks = [with_profit(settle_any(p, box, scores)) for p in data["picks"]]
    lines = [with_profit(settle_line(p, box)) for p in data["per_source"]["EL lines"]]
    football = [with_profit(settle_football(p, scores)) for lg in data["football_picks"].values()
                for p in lg if p["edge_pct"] >= min_edge]
    slips = [settle_slip(data["slips"][k], box, scores, int(k)) for k in sorted(data["slips"], key=int)
             if data["slips"][k]]
    # NBA games ("MM-DD HH:MM" Belgrade time, home, away) -> US Eastern date, for the NBA prop arbitrages
    nba_days = {f"{h} vs {a}": nba_day_of(f"{day[:4]}-{k}") for k, h, a in data.get("nba_games", [])}
    basket = data.get("basket")
    if basket:                                   # basketball card: singles, 4-leg / safer / long-shot tickets
        picks = [with_profit(settle_any(p, box, scores)) for p in basket["singles"]]
        slips = [{**settle_slip(t, box, scores, len(t["legs"])), "name": t["name"], "label": t["label"]}
                 for t in basket["tickets"]]
    res = {"date": day, "settled": dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "stake": stake,
           "games": [f'{g["local"]} {g["score"]} {g["road"]}' for g in box.values()],
           "football_scores": scores, "picks": picks, "lines": lines, "football": football, "slips": slips,
           "arbitrage": [settle_arb({**a, "_day": nba_days.get(a["match"])}, box, stake)
                         for a in (basket or data)["arbitrage"][:5]
                         if a.get("source", "NBA props") in ("EL props", "NBA props")]}
    with open(os.path.join(folder, "results.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    update_ledger(res)
    return res


def record(bets):
    won = sum(b["status"] == "won" for b in bets)
    lost = sum(b["status"] == "lost" for b in bets)
    profit = round(sum(b["profit"] for b in bets), 2)
    return {"won": won, "lost": lost, "void": sum(b["status"] == "void" for b in bets), "profit": profit,
            "roi": round(100 * profit / (won + lost), 1) if won + lost else 0.0}


def summary(res):
    arbs = [a for a in res["arbitrage"] if a["profit"] is not None]
    return {**record(res["picks"]),
            "slips_won": sum(s["status"] == "won" for s in res["slips"]),
            "slip_profit": round(sum(s["profit"] for s in res["slips"]), 2),
            "arb_profit": sum(a["profit"] for a in arbs), "arb_stake": res["stake"] * len(arbs), "arbs": len(arbs),
            "lines": record(res["lines"]), "football": record(res["football"])}


def update_ledger(res):
    try:
        with open(sb.LEDGER, encoding="utf-8") as f:
            ledger = json.load(f)
    except FileNotFoundError:
        ledger = {"nights": {}}
    ledger["nights"][res["date"]] = summary(res)
    with open(sb.LEDGER, "w", encoding="utf-8") as f:
        json.dump(ledger, f, ensure_ascii=False, indent=2)

# -----------------------------------------------------------------------------
# RENDER
# -----------------------------------------------------------------------------

pill, units = sb.pill, sb.units


def actual(b):
    if b["status"] not in ("won", "lost"):
        return b.get("note", b["status"])
    if b["kind"] == "prop":
        return f'{b["stat"]:g} {sb.SHORT.get(b["market"], b["market"])}'
    return b["stat_text"]


def bt(b):
    ms = f'<i>{int(b["line"] + 0.5)}+</i>' if b.get("milestone") else ""
    return e(b["text"]) + ms


def slide(kick, body, res, cls="res"):
    return (f'<div class="slide {cls}"><div class="top"><span class="kick">{e(kick)}</span>'
            f'<span class="date">{e(date_label(res["date"]))} · settled</span></div>{body}'
            f'<div class="foot"><span><b>18+</b> · Official EuroLeague box scores · ESPN NBA box scores · ESPN football scores · '
            f'Past results don\'t predict future ones</span><span>1 unit per pick / slip</span></div></div>')


def cover(res, s, t):
    tone = "up" if s["profit"] >= 0 else "down"
    rows = "".join(f'<div class="li"><span class="p">{e(p["player"])}<small>{bt(p)}</small></span>'
                   f'<span class="o">{p["odds"]:.2f}</span>{pill(p["status"])}</div>' for p in res["picks"])
    mini = "".join(f'<div><span>{sl.get("label") or str(sl["n"]) + "-leg slip"}</span>{pill(sl["status"])}'
                   f'<em>{sl["hits"]}/{len(sl["legs"])} legs</em></div>' for sl in res["slips"])
    L, F = s["lines"], s["football"]
    body = (f'<h1>Last night\'s<br><span>results</span></h1>'
            f'<div class="score"><div class="rec"><b>{s["won"]}-{s["lost"]}</b><span>top-10 picks won-lost</span></div>'
            f'<div class="pnl {tone}"><b>{units(s["profit"])}</b><span>profit on the 10 picks · ROI {s["roi"]:+.0f}%</span></div></div>'
            f'<div class="mini">{mini}</div>'
            f'<div class="mini"><div><span>Game lines</span><b class="sm">{L["won"]}-{L["lost"]}</b><em>{units(L["profit"])}</em></div>'
            f'<div><span>Football</span><b class="sm">{F["won"]}-{F["lost"]}</b><em>{units(F["profit"])}</em></div>'
            f'<div><span>Arbitrages</span><span class="st won">+{s["arb_profit"]:,} RSD</span>'
            f'<em>{s["arbs"]} of {len(res["arbitrage"])} cashed</em></div></div>'
            f'<div class="list">{rows}</div>'
            f'<div class="alltime"><b>All-time</b> · {t["nights"]} nights · top picks '
            f'{t["won"]}-{t["lost"]} · {units(t["profit"])} (ROI {t["roi"]:+.0f}%) · slips {t["slips_won"]} won · '
            f'arbs +{t["arb_profit"]:,} RSD</div>')
    return slide("EuroLeague + football · bet results", body, res)


def rows_html(bets, numbered=True):
    out = []
    for i, p in enumerate(bets, 1):
        out.append(f'<div class="rr {p["status"]}"><span class="n">{i if numbered else ""}</span>'
                   f'<div class="who"><span class="pl">{e(p["player"])}</span><span class="bt">{bt(p)} · '
                   f'{p["odds"]:.2f} @ {e(p["book_name"])}</span></div>'
                   f'<div class="act"><b>{e(actual(p))}</b><span>{units(p["profit"])}</span></div>{pill(p["status"])}</div>')
    return "".join(out)


def picks_slide(res):
    body = (f'<div class="head">Top 10 picks<small>Each pick 1 unit at the posted price · the actual result</small></div>'
            f'<div class="rlist">{rows_html(res["picks"])}</div>')
    return slide("Value picks · settled", body, res)


def lines_slide(res, s):
    L, F = s["lines"], s["football"]
    body = (f'<div class="head">Game lines<small>EuroLeague totals / winner incl. OT · {L["won"]}-{L["lost"]} · '
            f'{units(L["profit"])}</small></div><div class="rlist sm">{rows_html(res["lines"])}</div>'
            f'<div class="head">Football<small>Champions League + Championship · {F["won"]}-{F["lost"]} · '
            f'{units(F["profit"])}</small></div><div class="rlist sm">{rows_html(res["football"])}</div>')
    return slide("Game lines + football · settled", body, res, "res wide")


def slip_slide(res, sl):
    n = len(sl["legs"])
    size = "--lp:7px;--lf:17px;--bf:10.5px;--of:15px" if n > 4 else ""
    legs = "".join(f'<div class="sl {l["status"]}"><div><div class="pl">{e(l.get("player") or l["match"])}</div>'
                   f'<div class="bt">{bt(l)} · <b>{e(actual(l))}</b></div></div>'
                   f'<span class="o">{l["odds"]:.2f}</span>{pill(l["status"])}</div>' for l in sl["legs"])
    verdict = "Cashed!" if sl["status"] == "won" else "Busted" if sl["status"] == "lost" else "Pending"
    body = (f'<div class="head">{e(sl.get("label") or f"{n}-leg slip")}<small>{sl["hits"]} of {n} legs hit · one ticket at {e(sl["book_name"])}'
            f'</small></div>'
            f'<div class="slip" style="{size}"><div class="sh"><b>{e(sl.get("label") or f"{n}-leg slip")}</b>{pill(sl["status"])}</div>{legs}'
            f'<div class="tot"><span class="k">Total odds {sl["total_odds"]:,.2f}</span>'
            f'<span class="v {sl["status"]}">{verdict}</span></div></div>'
            f'<div class="facts"><div><span>Legs hit</span><b>{sl["hits"]}/{n}</b></div>'
            f'<div><span>Pre-game win chance</span><b>{100 * sl["hit_prob"]:.1f}%</b></div>'
            f'<div><span>Result</span><b class="{"pos" if sl["profit"] > 0 else "neg"}">{units(sl["profit"])}</b></div></div>')
    return slide("Slip · settled", body, res)


def arbs_slide(res, s):
    rows = []
    for a in res["arbitrage"]:
        side = a["status"]
        cashed = (f'<b>{"Over" if side == "over" else "Under"} cashed</b> · {a["stat"]:g} '
                  f'{sb.SHORT.get(a["market"], a["market"])}' if side in ("over", "under") else e(side.upper()))
        profit = "" if a["profit"] is None else f'+{a["profit"]:,}'
        rows.append(f'<div class="ar"><div class="who"><span class="pl">{e(a["player"])}</span>'
                    f'<span class="bt">{e(a["market"])} {a["line"]:g} · O {a["over_odds"]:.2f} {e(a["over_book"])} / '
                    f'U {a["under_odds"]:.2f} {e(a["under_book"])}</span><span class="cs">{cashed}</span></div>'
                    f'<div class="pr"><b>{profit}</b><span>RSD on {res["stake"]:,.0f}</span></div></div>')
    pct = 100 * s["arb_profit"] / s["arb_stake"] if s["arb_stake"] else 0
    body = (f'<div class="head">Arbitrages<small>One side always cashes · stakes split as posted, '
            f'{res["stake"]:,.0f} RSD each</small></div><div class="alist">{"".join(rows)}</div>'
            f'<div class="facts"><div><span>Total profit</span><b>+{s["arb_profit"]:,} RSD</b></div>'
            f'<div><span>Total staked</span><b>{s["arb_stake"]:,.0f}</b></div>'
            f'<div><span>Return</span><b>+{pct:.1f}%</b></div></div>')
    return slide("Arbitrage · settled", body, res, "res arb")


def analysis_slide(res, an):
    pts = "".join(f'<div class="why"><b>{e(p["title"])}</b><p>{e(p["text"])}</p></div>' for p in an["points"])
    body = f'<div class="head">{e(an["headline"])}<small>{e(an.get("sub", ""))}</small></div><div class="whys">{pts}</div>'
    return slide("Why it happened", body, res, "res why-s")


EXTRA_CSS = sb.EXTRA_CSS + """
<style>
.mini b.sm { font-family: var(--display); font-weight: 800; font-size: 20px; line-height: 1; }
.res .mini + .mini { margin-top: 6px; }
.rlist.sm { margin-top: 8px; gap: 4px; }
.rlist.sm .rr { padding: 5px 12px; }
.rlist.sm .pl { font-size: 15px; }
.rlist.sm .act b { font-size: 13px; }
.wide .rr { grid-template-columns: 24px 1fr auto 52px; }
.wide .rr .n { font-size: 16px; }
.whys { margin-top: 12px; display: grid; gap: 7px; }
.why { background: rgba(255, 255, 255, .06); border-left: 4px solid var(--pop); border-radius: 8px; padding: 8px 12px; }
.why b { font-family: var(--display); font-weight: 800; font-size: 16px; text-transform: uppercase; color: var(--pop); }
.why p { margin: 3px 0 0; font-size: 11px; font-weight: 500; line-height: 1.38; }
</style>
"""


def caption(res, s, t, an):
    icon = lambda b: {"won": "✅", "lost": "❌"}.get(b["status"], "➖")
    L, F = s["lines"], s["football"]
    lines = [f"🏀⚽ Bet results · {date_label(res['date'])}", "",
             f"Top 10 picks: {s['won']}-{s['lost']} · {units(s['profit'])} · ROI {s['roi']:+.0f}%", ""]
    lines += [f"{icon(p)} {p['player']} {p['text']} @ {p['odds']:.2f} → {actual(p)}" for p in res["picks"]]
    lines += ["", f"Game lines: {L['won']}-{L['lost']} ({units(L['profit'])}) · Football: {F['won']}-{F['lost']} "
                  f"({units(F['profit'])})"]
    lines += [f"🎟️ {sl.get("label") or str(sl["n"]) + "-leg slip"} ({sl['total_odds']:,.2f}): {sl['status'].upper()} · {sl['hits']}/{len(sl['legs'])}"
              for sl in res["slips"]]
    lines += [f"💰 Arbitrages: {s['arbs']}/{len(res['arbitrage'])} cashed, +{s['arb_profit']:,} RSD on "
              f"{s['arb_stake']:,.0f} staked"]
    tail = ["", f"All-time: top picks {t['won']}-{t['lost']} · {units(t['profit'])} · arbs +{t['arb_profit']:,} RSD", "",
            "1 unit per pick/slip. Past results don't predict future ones. 18+, bet responsibly.", "",
            "#EuroLeague #ChampionsLeague #SportsBetting #BettingResults #Basketball"]
    if an:  # full analysis if it fits Instagram's 2,200 chars, else just the titles (details are on the slide)
        full = ["", "🔎 Why it happened:"] + [f"• {p['title']}: {p['text']}" for p in an["points"]]
        short = ["", "🔎 Why it happened (slide 3):"] + [f"• {p['title']}" for p in an["points"]]
        lines += full if len("\n".join(lines + full + tail)) <= 2200 else short
    return "\n".join(lines + tail)[:2200]


def render(res, out_dir):
    s, t = summary(res), sb.all_time()
    an_path = os.path.join(RESULTS_DIR, res["date"], "analysis.json")
    an = json.load(open(an_path, encoding="utf-8")) if os.path.exists(an_path) else None
    with open(TEMPLATE, encoding="utf-8") as f:
        template = f.read().replace("{{FONTS}}", font_css()).replace("{{BODY}}", EXTRA_CSS + "{{BODY}}")
    slides = [("01_cover", cover(res, s, t)), ("02_picks", picks_slide(res))]
    if an:
        slides.append(("03_why", analysis_slide(res, an)))
    slides.append((f"{len(slides) + 1:02d}_lines-football", lines_slide(res, s)))
    for sl in res["slips"]:
        slides.append((f"{len(slides) + 1:02d}_slip-{sl['n']}", slip_slide(res, sl)))
    slides.append((f"{len(slides) + 1:02d}_arbs", arbs_slide(res, s)))
    shutil.rmtree(out_dir, ignore_errors=True)
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
        json.dump({"images": [n + ".png" for n, _ in slides], "caption": caption(res, s, t, an)}, f,
                  ensure_ascii=False, indent=2)
    print(f"{len(slides)} slides -> {out_dir}")


def main():
    ap = argparse.ArgumentParser(description="Settle a night's posted combined card (tonight.json).")
    ap.add_argument("date", nargs="?", default=(dt.date.today() - dt.timedelta(days=1)).isoformat())
    ap.add_argument("--stake", type=float, default=10000)
    ap.add_argument("--min-edge", type=float, default=1.0, help="football picks shown on the posted slides")
    ap.add_argument("--no-render", action="store_true")
    args = ap.parse_args()
    res = settle(args.date, args.stake, args.min_edge)
    for grp in ("picks", "lines", "football"):
        print(f"--- {grp}")
        for p in res[grp]:
            print(f'{p["status"].upper():5} {p["player"]:30} {p["text"]:40} @ {p["odds"]:.2f} -> {actual(p)}'
                  f'  [{p.get("line_detail", "")}]  fair {p.get("fair_odds")}')
    for sl in res["slips"]:
        print(f'--- slip {sl["n"]} @ {sl["book_name"]}: {sl["status"].upper()} {sl["hits"]}/{len(sl["legs"])}')
        for l in sl["legs"]:
            print(f'   {l["status"].upper():5} {l.get("player") or l["match"]:24} {l["text"]:28} {actual(l):14} [{l.get("line_detail", "")}]')
    for a in res["arbitrage"]:
        print(f'arb {a["player"]} {a["market"]} {a["line"]:g}: {a["status"]} stat={a["stat"]} profit={a["profit"]}')
    print(summary(res))
    if not args.no_render:
        render(res, os.path.join(RESULTS_DIR, args.date, "carousel_results"))


if __name__ == "__main__":
    main()
