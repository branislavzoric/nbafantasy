#!/usr/bin/env python3
"""
nba_prop_analysis.py

Tonight's NBA player props against this season's game logs:
  - logs from every saved regular-season ESPN box score in "Box Scores/<date>/<id>.json"
  - projected minutes = 0.5 season + 0.5 last 3; per-minute rate = 0.6 season + 0.4 last 3
  - points-type stats scaled by the opponent's points allowed vs league average (half weight)
  - ESPN injury report for tonight's games: Out/Doubtful players are dropped, teammates' absences are listed
  - P(over) from a negative binomial, blended 50/50 with the books' no-vig price
Run after scrape_odds.py --nba and compare_odds.py:
  python nba_prop_analysis.py [YYYY-MM-DD]
Output: Odds/compare/<date>/nba_props_analysis.csv and nba_players.json
"""

import datetime as dt
import glob
import json
import math
import os
import sys
from collections import defaultdict

import pandas as pd
from scipy.stats import nbinom

import collect_boxscores as cb
from settle_tonight import nba_name

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
BOOKS = ["soccerbet", "maxbet", "admiralbet", "mozzartbet", "superbet", "balkanbet"]
STAT = {"Points": ["pts"], "Rebounds": ["reb"], "Assists": ["ast"], "3PM": ["tpm"], "Steals": ["stl"],
        "Blocks": ["blk"], "Turnovers": ["tov"], "Pts+Ast": ["pts", "ast"], "Pts+Reb": ["pts", "reb"],
        "Pts+Reb+Ast": ["pts", "reb", "ast"], "Reb+Ast": ["reb", "ast"]}
DISP = {"pts": 1.9, "reb": 1.5, "ast": 1.5, "tpm": 1.3, "stl": 1.2, "blk": 1.3, "tov": 1.2}
OPP_SCALED = {"pts", "tpm"}


def minutes(v):
    if v is None:
        return 0.0
    if isinstance(v, str) and ":" in v:
        m, s = v.split(":")
        return int(m) + int(s) / 60
    return float(v)


def load_logs(before):
    logs, games = defaultdict(list), {}
    now = dt.datetime.now()
    for path in sorted(glob.glob(os.path.join(cb.RAW_DIR, "20*", "*.json"))):
        day = os.path.basename(os.path.dirname(path))
        if day >= before or not os.path.basename(path)[:-5].isdigit():
            continue
        try:
            summary = json.load(open(path, encoding="utf-8"))
            game, lines = cb.parse_game(summary, dt.date.fromisoformat(day), now)
        except Exception:
            continue
        if game["season_type"] not in ("regular", "Regular Season", "2") or game["status"] != "STATUS_FINAL":
            continue
        games[game["game_id"]] = game
        for l in lines:
            m = minutes(l["min"])
            if l["did_not_play"] or m <= 0:
                continue
            logs[l["player_id"]].append({**l, "MIN": m})
    for v in logs.values():
        v.sort(key=lambda l: l["game_date"])
    return logs, games


def team_defense(games):
    allowed = defaultdict(list)
    for g in games.values():
        allowed[g["home_team"]].append(g["away_score"])
        allowed[g["away_team"]].append(g["home_score"])
    pa = {t: sum(v) / len(v) for t, v in allowed.items()}
    lg = sum(pa.values()) / len(pa)
    return {t: v / lg for t, v in pa.items()}, pa


def injuries(day):
    """ESPN injury list of tonight's games -> {player name: (team, status)}."""
    out = {}
    for ev in cb.games_on(dt.date.fromisoformat(day)):
        try:
            s = cb.get_json(cb.SUMMARY_URL, {"event": ev["id"]})
        except Exception:
            continue
        for team in s.get("injuries", []):
            ab = team.get("team", {}).get("abbreviation")
            for i in team.get("injuries", []):
                out[i["athlete"]["displayName"]] = (ab, i.get("status"), (i.get("details") or {}).get("type", ""))
    return out


def p_over(mean, line, cols):
    k = sum(DISP[c] for c in cols) / len(cols) + (0.3 if len(cols) > 1 else 0)
    thr = math.floor(line) + 1
    if mean <= 0:
        return 0.0
    p = 1 / k
    r = mean * p / (1 - p)
    return float(1 - nbinom.cdf(thr - 1, r, p))


def project(gl, cols, opp_factor):
    mins = [g["MIN"] for g in gl]
    pmin = 0.5 * sum(mins) / len(mins) + 0.5 * sum(mins[-3:]) / len(mins[-3:])
    mean = 0.0
    for c in cols:
        season = sum(g[c] or 0 for g in gl) / sum(mins)
        last3 = sum(g[c] or 0 for g in gl[-3:]) / sum(mins[-3:])
        rate = 0.6 * season + 0.4 * last3
        f = 1 + 0.5 * (opp_factor - 1) if c in OPP_SCALED else 1
        mean += rate * pmin * f
    return mean, pmin


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else dt.date.today().isoformat()
    cmp = pd.read_csv(os.path.join(PROJECT_DIR, "Odds", "compare", day, "odds_comparison.csv"))
    logs, games = load_logs(day)
    opp_f, pa = team_defense(games)
    inj = injuries(day)
    by_name = {}
    for pid, gl in logs.items():
        by_name[tuple(nba_name(gl[-1]["player_name"]))] = pid

    def find(name):
        want = tuple(nba_name(name))
        if want in by_name:
            return by_name[want]
        hits = [pid for k, pid in by_name.items() if k and want and k[-1] == want[-1] and k[0][0] == want[0][0]]
        return hits[0] if len(hits) == 1 else None

    def injury_of(name):
        want = nba_name(name)
        for n, v in inj.items():
            if nba_name(n) == want:
                return v
        return None

    rows, players = [], {}
    for (game, kickoff, player, market, line), grp in cmp.groupby(["game", "kickoff", "player", "market", "line"]):
        if market not in STAT:
            continue
        o, u = grp[grp.side == "Over"], grp[grp.side == "Under"]
        if o.empty:
            continue
        o, u = o.iloc[0], (u.iloc[0] if not u.empty else None)
        nv = [(1 / o[b]) / (1 / o[b] + 1 / u[b]) for b in BOOKS
              if u is not None and pd.notna(o[b]) and pd.notna(u[b])]
        mkt = sum(nv) / len(nv) if nv else None
        pid = find(player)
        gl = logs.get(pid, [])
        cols = STAT[market]
        inj_p = injury_of(player)
        rec = {"game": game, "kickoff": kickoff, "player": player, "market": market, "line": line,
               "over": o["best_odds"], "over_book": o["best_book"], "over_books": o["books"],
               "under": u["best_odds"] if u is not None else None,
               "under_book": u["best_book"] if u is not None else None, "mkt_p": mkt, "gp": len(gl),
               "injury": f"{inj_p[1]} ({inj_p[2]})" if inj_p else "", "espn_id": pid}
        rec.update({f"over_{b}": o[b] for b in BOOKS})
        rec.update({f"under_{b}": (u[b] if u is not None else None) for b in BOOKS})
        if len(gl) >= 2:
            team = gl[-1]["team"]
            opp_tonight = TONIGHT.get(day, {}).get(team)
            f = opp_f.get(opp_tonight, 1.0) if opp_tonight else 1.0
            mean, pmin = project(gl, cols, f)
            vals = [sum(g[c] or 0 for c in cols) for g in gl]
            pm = p_over(mean, line, cols)
            blend = 0.5 * pm + 0.5 * mkt if mkt is not None else pm
            rec.update(team=team, opp=opp_tonight, proj=round(mean, 1), proj_min=round(pmin, 1),
                       avg=round(sum(vals) / len(vals), 1), last5=" ".join(str(v) for v in vals[-5:]),
                       hit_over=f"{sum(v > line for v in vals)}/{len(vals)}", model_p=round(pm, 3),
                       blend_p=round(blend, 3),
                       ev_over=round(blend * o["best_odds"] - 1, 3),
                       ev_under=round((1 - blend) * rec["under"] - 1, 3) if rec["under"] else None)
            players[player] = {"espn_id": pid, "team": team, "opp": opp_tonight, "gp": len(gl),
                               "min_last5": [round(g["MIN"]) for g in gl[-5:]], "injury": rec["injury"],
                               "log": [f'{str(g["game_date"])[5:]} {"" if g["starter"] else "(b) "}{g["opponent"]} '
                                       f'{g["MIN"]:.0f}m {g["pts"]}p {g["reb"]}r {g["ast"]}a {g["tpm"]}/{g["tpa"]}3'
                                       for g in gl]}
        rows.append(rec)
    out_dir = os.path.join(PROJECT_DIR, "Odds", "compare", day)
    pd.DataFrame(rows).to_csv(os.path.join(out_dir, "nba_props_analysis.csv"), index=False, encoding="utf-8-sig")
    team_inj = defaultdict(list)
    for n, (t, s, typ) in inj.items():
        team_inj[t].append(f"{n} {s}")
    json.dump({"players": players, "injuries": team_inj, "opp_pa": pa}, open(os.path.join(out_dir, "nba_players.json"), "w",
              encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"{len(rows)} lines, {len(players)} players with logs, {len(inj)} injury entries")


TONIGHT = {}


def tonight_opponents(day):
    """{team abbreviation: opponent abbreviation} for tonight's ESPN scoreboard."""
    out = {}
    for ev in cb.games_on(dt.date.fromisoformat(day)):
        c = [x["team"]["abbreviation"] for x in ev["competitions"][0]["competitors"]]
        if len(c) == 2:
            out[c[0]], out[c[1]] = c[1], c[0]
    return out


if __name__ == "__main__":
    d = sys.argv[1] if len(sys.argv) > 1 else dt.date.today().isoformat()
    TONIGHT[d] = tonight_opponents(d)
    main()
