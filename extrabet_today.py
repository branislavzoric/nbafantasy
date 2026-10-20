#!/usr/bin/env python3
"""
extrabet_today.py

Picks for Soccerbet's "Extrabet bonus ticket" promo (a free Extrabet ticket worth the stake of
any ticket at odds >= 4, paid whatever the result):
  1. qualifying ticket: must itself be an Extrabet combo at odds >= 4: the one returning most (r = p x odds)
  2. bonus ticket: the Extrabet combos worth most as a free ticket (winnings only):
     value v = p x (odds - 1) per 1 RSD of bonus

Lineup-aware: once ESPN has a team's confirmed XI, the starters share STARTERS_SHARE of the team's
goals (in proportion to Soccerbet's anytime-scorer odds), substitutes get SUB_FACTOR of a starter's
share, and players outside the matchday squad are ruled out. Before that, the listed players' shares
are capped at PRE_LINEUP_SHARE of the team's goals and each pick says who has to start.

Uses the raw odds saved by extrabet_value.py (run it first to refresh):
  python extrabet_value.py            # scrape fresh odds
  python extrabet_today.py [--top 15]
Output: printed + Odds/extrabet/<date>/today_picks.json
"""

import argparse
import datetime as dt
import glob
import json
import os
import sys
import time

import numpy as np
import requests

import extrabet_value as X

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.champions"
STARTERS_SHARE = 0.88      # own goals + subs take the rest
SUB_FACTOR = 0.25          # a substitute's share relative to his share as a starter (~25 min)
PRE_LINEUP_SHARE = 1.3     # before lineups: cap on the listed players' summed shares
MIN_ODDS = 4.0


def tokens(name):
    return X.norm(name)


def same_player(a, b):
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return False
    if ta == tb or set(ta) <= set(tb) or set(tb) <= set(ta):
        return True
    return ta[-1] == tb[-1] and ta[0][0] == tb[0][0]


def same_team(a, b):
    ta, tb = set(tokens(a)), set(tokens(b))
    alias = {"inter": "internazionale", "internazionale": "inter", "munchen": "munich", "munich": "munchen",
             "atl": "atletico", "atletico": "atl", "psv": "psv", "sabah": "sabah"}
    ta |= {alias[t] for t in ta if t in alias}
    return bool(ta & tb - {"fc", "fk", "cp", "club", "rb", "sc"})


def espn_lineups(day):
    """{(home, away) espn names: {team name: {"starters": [...], "bench": [...]}}} for confirmed lineups."""
    out = {}
    try:
        events = requests.get(f"{ESPN}/scoreboard", params={"dates": day.strftime("%Y%m%d")}, timeout=20).json().get("events", [])
    except requests.RequestException:
        return out
    for e in events:
        try:
            s = requests.get(f"{ESPN}/summary", params={"event": e["id"]}, timeout=20).json()
        except requests.RequestException:
            continue
        teams = {}
        for t in s.get("rosters") or []:
            roster = t.get("roster") or []
            if not roster:
                continue
            name = t["team"]["displayName"]
            teams[name] = {"starters": [p["athlete"]["displayName"] for p in roster if p.get("starter")],
                           "bench": [p["athlete"]["displayName"] for p in roster if not p.get("starter")]}
        if teams:
            out[e["name"]] = teams
        time.sleep(0.3)
    return out


def team_lineup(lineups, team):
    for teams in lineups.values():
        for name, lu in teams.items():
            if same_team(name, team) and len(lu["starters"]) >= 11:
                return lu
    return None


def shares_for_match(d, ttg, grid):
    """Player shares + per-player status ('starts', 'bench', 'out', 'unknown')."""
    m = d["main"]
    base = X.player_shares(d["pl"], ttg, m["home"], m["away"], grid, 0.0)
    out, status = {}, {}
    for side, team in (("h", m["home"]), ("a", m["away"])):
        items = {k: v for k, v in base.items() if v[0] == side}
        lu = LINEUPS_BY_TEAM.get(team)
        if lu:
            st = {k: ("starts" if any(same_player(v[2], n) for n in lu["starters"]) else
                      "bench" if any(same_player(v[2], n) for n in lu["bench"]) else "out") for k, v in items.items()}
            tot = sum(v[1] for k, v in items.items() if st[k] == "starts")
            c = min(1.0, STARTERS_SHARE / tot) if tot else 0
            for k, v in items.items():
                status[v[2]] = st[k]
                if st[k] == "starts":
                    out[k] = (side, v[1] * c, v[2], 0)
                elif st[k] == "bench":
                    out[k] = (side, v[1] * c * SUB_FACTOR, v[2], 0)
        else:
            tot = sum(v[1] for v in items.values())
            c = min(1.0, PRE_LINEUP_SHARE / tot) if tot else 0
            for k, v in items.items():
                status[v[2]] = "unknown"
                out[k] = (side, v[1] * c, v[2], 0)
    return out, status


LINEUPS_BY_TEAM = {}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()
    day = dt.date.today()
    raw = sorted(glob.glob(os.path.join(PROJECT_DIR, "Odds", "extrabet", "*", "raw")))[-1]
    ttg = json.load(open(os.path.join(raw, "ttg.json"), encoding="utf-8"))
    lineups = espn_lineups(day)
    now_ms = time.time() * 1000
    H, A = np.meshgrid(np.arange(X.MAXG + 1), np.arange(X.MAXG + 1), indexing="ij")
    quals, bonus, lineup_note = [], [], []
    for fn in sorted(os.listdir(raw)):
        if not fn[0].isdigit():
            continue
        d = json.load(open(os.path.join(raw, fn), encoding="utf-8"))
        m = d["main"]
        if m["kickOffTime"] < now_ms or dt.datetime.fromtimestamp(m["kickOffTime"] / 1000).date() != day:
            continue
        home, away = m["home"], m["away"]
        kick = dt.datetime.fromtimestamp(m["kickOffTime"] / 1000).strftime("%H:%M")
        for team in (home, away):
            lu = team_lineup(lineups, team)
            if lu:
                LINEUPS_BY_TEAM[team] = lu
        known = [t for t in (home, away) if t in LINEUPS_BY_TEAM]
        lineup_note.append(f"{kick} {home} - {away}: lineups {'confirmed' if len(known) == 2 else 'partly confirmed (' + ', '.join(known) + ')' if known else 'not out yet'}")
        lh, la, rho, f, _, _ = X.fit_match(m, ttg)
        grid = X.dc_grid(lh, la, rho)
        # every Extrabet combo: as a qualifying ticket (return r = p x odds, odds >= MIN_ODDS)
        # and as the bonus ticket (value v = p x (odds - 1), winnings only)
        shares, status = shares_for_match(d, ttg, grid)
        for x in (d["mt"] or {}).get("esMatches") or []:
            for code, svs in x["betMap"].items():
                tt = (ttg.get(f"{code}_S") or {}).get("tipTypeName")
                for sv, o in svs.items():
                    if o.get("s") != "U" or o.get("ov", 0) <= 1.0:
                        continue
                    p, why = X.price_combo(tt, sv, grid, f, home, away, shares)
                    if not p:
                        continue
                    a_ = X.parse_sv(sv)
                    players = [a_[k] for k in ("participant", "participant1", "participant2") if a_.get(k, "dummy") != "dummy"]
                    st = []
                    for pl in players:
                        s = next((v for n, v in status.items() if same_player(n, pl)), "unknown")
                        st.append(f"{pl}: {s}")
                    row = {"kickoff": kick, "match": f"{home} - {away}", "type": tt, "selection": sv,
                           "odds": o["ov"], "prob": round(p, 4), "return": round(p * o["ov"], 3),
                           "value": round(p * (o["ov"] - 1), 3),
                           "players": st, "needs_lineup": any(s.endswith("unknown") for s in st)}
                    bonus.append(row)
                    if o["ov"] >= MIN_ODDS:
                        quals.append(row)
    quals.sort(key=lambda r: -r["return"])
    bonus.sort(key=lambda r: -r["value"])
    out_dir = os.path.dirname(raw)
    json.dump({"made": dt.datetime.now().isoformat(timespec="minutes"), "lineups": lineup_note,
               "qualifiers": quals, "bonus": bonus[:200]},
              open(os.path.join(out_dir, "today_picks.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n".join(lineup_note))
    print(f"\nQualifying ticket: Extrabet combos at odds >= {MIN_ODDS} that return the most (per 1 RSD staked)")
    for r in quals[:args.top]:
        print(f"  {r['return']:.3f}  {r['odds']:>7}  {r['kickoff']}  {r['match']:32} {r['type']:12} {r['selection']}  "
              f"(p {r['prob']:.1%}) [{'; '.join(r['players'])}]")
    print("\nBonus ticket: Extrabet combos worth most as a free ticket (value per 1 RSD of bonus)")
    for r in bonus[:args.top]:
        print(f"  {r['value']:.3f}  {r['odds']:>7}  {r['kickoff']}  {r['match']:32} {r['type']:12} {r['selection']}  [{'; '.join(r['players'])}]")
    if quals and bonus:
        q, b = quals[0], bonus[0]
        print(f"\nBest pair per 1,000 RSD: qualifying {q['return'] * 1000 - 1000:+.0f} + bonus {b['value'] * 1000:+.0f} "
              f"= {(q['return'] - 1 + b['value']) * 1000:+.0f} RSD expected")


if __name__ == "__main__":
    main()
