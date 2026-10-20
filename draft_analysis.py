#!/usr/bin/env python3
"""
draft_analysis.py

Parses the saved Yahoo "Draft Results" page of trebevicka23 (Desktop) and scores every pick with a consensus
projection under the league's scoring (league_scoring.py, incl. DD/TD/TECH/EJCT bonuses):

  consensus FP/G = mean of the ESPN, Yahoo and FantasyPros FP/G that exist for the player
  consensus GP   = mean of their projected games

Team strength = Monte Carlo of 82 lineup days: each player is available with probability GP/82 and the best
legal Yahoo lineup (PG, SG, G, SF, PF, F, C, C, Util, Util) is set from who is available; bench players fill in
for injuries. Ranking = projected season points from the starting slots.

  python draft_analysis.py [path-to-draft-html]   -> "Draft Analysis/trebevicka23_draft.json"
  python draft_analysis.py --league boogie         -> "Draft Analysis/boogie_draft.json"
                                                     (rosters from "Draft Analysis/boogie_picks.json", Boogie scoring)
"""

import html as htmlmod
import json
import os
import random
import re
import sys
from itertools import permutations

import build_more_draft_boards as bmd
from league_scoring import LEAGUES, espn_players, key

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SRC = os.path.join(os.path.expanduser("~"), "Desktop", "Draft Results _ Fantasy Basketball _ Yahoo! Sports.html")
OUT = os.path.join(PROJECT_DIR, "Draft Analysis", "trebevicka23_draft.json")
BOOGIE_PICKS = os.path.join(PROJECT_DIR, "Draft Analysis", "boogie_picks.json")
BOOGIE_OUT = os.path.join(PROJECT_DIR, "Draft Analysis", "boogie_draft.json")
SLOTS = ["PG", "SG", "G", "SF", "PF", "F", "C", "C", "Util", "Util"]
FITS = {"PG": {"PG"}, "SG": {"SG"}, "G": {"PG", "SG"}, "SF": {"SF"}, "PF": {"PF"}, "F": {"SF", "PF"},
        "C": {"C"}, "Util": {"PG", "SG", "SF", "PF", "C"}}
DAYS, SIMS = 82, 120


def parse_draft(path):
    h = open(path, encoding="utf-8", errors="ignore").read()
    t = re.sub(r"<style.*?</style>|<script.*?</script>", "", h, flags=re.S)
    t = htmlmod.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "|", t)))
    t = re.sub(r"(\| ?)+", "|", t)
    body = t[t.index("|Round|Team|"):]
    picks, rnd = [], 0
    for m in re.finditer(r"Round (\d+)\||(\d+)\.\|([^|]+)\|\((\w+) - ([^)]+)\)\|([^|]+)\|", body):
        if m.group(1):
            rnd = int(m.group(1))
            continue
        picks.append({"round": rnd, "rpick": int(m.group(2)), "player": m.group(3).strip(), "nba": m.group(4),
                      "pos": [p.strip() for p in m.group(5).split(",")], "team": m.group(6).strip()})
    # The league drafts with a third-round reversal (R1 1->12, R2 and R3 12->1, then alternating). The saved Yahoo
    # page lists every round as a plain snake, so the overall pick is rebuilt from each team's round-1 slot.
    slot = {p["team"]: p["rpick"] for p in picks if p["round"] == 1}
    for p in picks:
        s_, r = slot[p["team"]], p["round"]
        in_round = s_ if r == 1 or (r >= 4 and r % 2 == 0) else 13 - s_
        p["rpick"], p["overall"] = in_round, (r - 1) * 12 + in_round
    picks.sort(key=lambda p: p["overall"])
    return picks


def parse_roster_list(path):
    """Boogie Nights was drafted offline: {"order": [teams by round-1 slot], "teams": {team: [13 names in round order]}}.
    NBA team and Yahoo positions come from the projection sources (Yahoo first)."""
    d = json.load(open(path, encoding="utf-8"))
    bmd.TOTAL_PICKS = 5000
    info = {}
    for loader in (bmd.load_fantasypros, bmd.load_espn, bmd.load_yahoo):    # later loaders win
        for r in loader()[0]:
            info[key(r["full_name"])] = r
    picks = []
    for s_, t in enumerate(d["order"], 1):
        for r, name in enumerate(d["teams"][t], 1):
            i = info[key(name)]
            in_round = s_ if r == 1 or (r >= 4 and r % 2 == 0) else 13 - s_
            picks.append({"round": r, "rpick": in_round, "player": i["full_name"], "nba": i["team"],
                          "pos": list(i["positions"]), "team": t, "overall": (r - 1) * 12 + in_round})
    picks.sort(key=lambda p: p["overall"])
    return picks


def sources():
    bmd.TOTAL_PICKS = 5000                      # keep every projected player, not just the board's top 156
    out = {}
    for name, loader in (("espn", bmd.load_espn), ("yahoo", bmd.load_yahoo), ("fantasypros", bmd.load_fantasypros)):
        rows, _ = loader()
        for r in rows:
            out.setdefault(key(r["full_name"]), {})[name] = {"fppg": r["fppg"], "gp": r["gp"], "fpts": r["fpts"]}
    return out


# Games-played caps from injury timelines in our fact-checked news (News/<date>/news.json)
GP_CAP = {
    "Jusuf Nurkić": (74, "partial plantar plate tear (right toe), out at least four weeks from Oct 9"),
    "Maxime Raynaud": (77, "right patellar subluxation, re-evaluated two weeks after Oct 13, misses the start"),
    "Nic Claxton": (72, "hamstring strain, out \"awhile\", no timeline (we assume ~10 games)"),
    "Brandon Ingram": (70, "partially torn Achilles, not ready for opening night"),
}
NOTES = {
    "AJ Dybantsa": "sprained ankle in the preseason finale; Wizards hope he plays the opener",
    "Collin Sexton": "lower-leg injury, missed all of preseason; opener in doubt",
    "Franz Wagner": "right shoulder soreness in the preseason finale",
    "Bam Adebayo": "right groin tightness, sat Oct 14",
    "Kawhi Leonard": "eased in during preseason, 21 points in 17 minutes in his Raptors return",
    "Tyrese Haliburton": "back from the Achilles tear; ankle tweak in his first game, MRI clean",
    "Kyrie Irving": "back from the torn ACL (19 months out)",
    "Fred VanVleet": "back from the torn ACL",
    "Damian Lillard": "back from the ruptured Achilles",
    "Jayson Tatum": "back from the ruptured Achilles; Globe says recovery is complete",
}
GP_CAP_K = {key(n): v for n, v in GP_CAP.items()}
NOTES_K = {key(n): v for n, v in NOTES.items()}
W = {"fgm": 1, "fga": -0.45, "ftm": 1, "fta": -0.75, "pts": 1, "reb": 1, "ast": 1, "stl": 1.25, "blk": 1.25,
     "tov": -1, "pf": -1}


BON = {"dd": 2.5, "td": 5}


def preseason():
    """ESPN id -> (games, league FP/G, MPG) over the saved preseason box scores (Oct 2026)."""
    import glob
    agg = {}
    for f in glob.glob(os.path.join(PROJECT_DIR, "Box Scores", "2026-10-*", "*.json")):
        if "EuroLeague" in f or not os.path.basename(f)[:-5].isdigit():
            continue
        d = json.load(open(f, encoding="utf-8"))
        for t in d.get("boxscore", {}).get("players", []):
            st = t["statistics"][0]
            for a in st["athletes"]:
                if not a.get("stats") or a.get("didNotPlay"):
                    continue
                x = dict(zip(st["keys"], a["stats"]))
                try:
                    mins = float(x["minutes"])
                except ValueError:
                    continue
                if mins <= 0:
                    continue
                fgm, fga = map(int, x["fieldGoalsMade-fieldGoalsAttempted"].split("-"))
                ftm, fta = map(int, x["freeThrowsMade-freeThrowsAttempted"].split("-"))
                v = {"fgm": fgm, "fga": fga, "ftm": ftm, "fta": fta, "pts": int(x["points"]), "reb": int(x["rebounds"]),
                     "ast": int(x["assists"]), "stl": int(x["steals"]), "blk": int(x["blocks"]),
                     "tov": int(x["turnovers"]), "pf": int(x["fouls"])}
                fp = sum(v[k] * w for k, w in W.items())
                tens = sum(v[k] >= 10 for k in ("pts", "reb", "ast", "stl", "blk"))
                fp += BON["td"] if tens >= 3 else BON["dd"] if tens == 2 else 0
                g = agg.setdefault(a["athlete"]["id"], [0, 0.0, 0.0])
                g[0] += 1
                g[1] += fp
                g[2] += mins
    return {k: (g, fp / g, m / g) for k, (g, fp, m) in agg.items()}


def lineup_exact(players):
    """Exact best assignment of 10 slots by DFS (13 players x 10 slots, small)."""
    best = [0.0, None]
    players = sorted(players, key=lambda p: -p["fppg"])

    def dfs(i, slots, total, chosen):
        if i == len(players) or not slots:
            if total > best[0]:
                best[:] = [total, list(chosen)]
            return
        # upper bound: remaining best players fill remaining slots
        if total + sum(p["fppg"] for p in players[i:i + len(slots)]) <= best[0]:
            return
        p = players[i]
        tried = set()
        for j, s in enumerate(slots):
            if s in tried or not FITS[s] & set(p["pos"]):
                continue
            tried.add(s)
            dfs(i + 1, slots[:j] + slots[j + 1:], total + p["fppg"], chosen + [(p, s)])
        dfs(i + 1, slots, total, chosen)

    dfs(0, list(SLOTS), 0.0, [])
    return best


ESPN_ABBR = {"GSW": "GS", "NOP": "NO", "SAS": "SA", "UTA": "UTAH", "WAS": "WSH", "NYK": "NY"}
SCHED = os.path.join(PROJECT_DIR, "Draft Analysis", "nba_schedule_2026_27.json")


def schedule():
    """{team abbr (Yahoo style): [US Eastern dates]} for the 2026-27 regular season (ESPN team schedules)."""
    if os.path.exists(SCHED):
        return json.load(open(SCHED, encoding="utf-8"))
    import datetime as dt
    import requests
    teams = ["ATL", "BOS", "BKN", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GSW", "HOU", "IND", "LAC", "LAL", "MEM",
             "MIA", "MIL", "MIN", "NOP", "NYK", "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SAS", "TOR", "UTA", "WAS"]
    out = {}
    for t in teams:
        d = requests.get(f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/"
                         f"{ESPN_ABBR.get(t, t).lower()}/schedule", params={"season": 2027}, timeout=30).json()
        out[t] = sorted((dt.datetime.fromisoformat(e["date"].replace("Z", "+00:00")) - dt.timedelta(hours=5)).date().isoformat()
                        for e in d["events"])
    json.dump(out, open(SCHED, "w", encoding="utf-8"), indent=0)
    return out


def simulate(roster, sched, seed=7):
    """Season points from the 10 starting slots over the real schedule: each day, the roster players whose NBA team
    plays are available with probability GP / team games; the best legal lineup is set (bench overflow is lost)."""
    rnd = random.Random(seed)
    days = {}
    for p in roster:
        for d in sched[p["nba"]]:
            days.setdefault(d, []).append(p)
    tot = games = lost = 0.0
    for _ in range(SIMS):
        for d, ps in days.items():
            avail = [p for p in ps if rnd.random() < min(p["gp"] / len(sched[p["nba"]]), 1)]
            pts, chosen = lineup_exact(avail)
            tot += pts
            games += len(chosen or [])
            lost += sum(p["fppg"] for p in avail) - pts
    return tot / SIMS, games / SIMS, lost / SIMS


def main():
    global OUT
    args = sys.argv[1:]
    league = "trebevicka"
    if args[:1] == ["--league"]:
        league, args = args[1], args[2:]
    if league == "boogie":
        bmd.set_league("boogie")
        W.update(LEAGUES["boogie"]["weights"])
        BON.update(LEAGUES["boogie"]["bonus"])
        src, OUT = BOOGIE_PICKS, BOOGIE_OUT
        picks = parse_roster_list(src)
    else:
        src = args[0] if args else DEFAULT_SRC
        picks = parse_draft(src)
    assert len(picks) == 156, len(picks)
    proj = sources()
    espn = espn_players()
    pres = preseason()
    sched = schedule()
    missing = []
    for p in picks:
        k = key(p["player"])
        s = proj.get(k, {})
        if not s:
            missing.append(p["player"])
        vals = list(s.values())
        p["proj"] = {n: {"fppg": round(v["fppg"], 2), "gp": v["gp"]} for n, v in s.items()}
        p["fppg"] = round(sum(v["fppg"] for v in vals) / len(vals), 2) if vals else 0.0
        p["gp"] = round(sum(v["gp"] for v in vals) / len(vals), 1) if vals else 0.0
        if k in GP_CAP_K:
            cap, why = GP_CAP_K[k]
            p["gp_note"] = why
            p["gp"] = min(p["gp"], cap)
        p["note"] = NOTES_K.get(k)
        p["fpts"] = round(p["fppg"] * p["gp"], 1)
        e = espn.get(k) or {}
        p["injury"] = e.get("injury", "")
        p["espn_id"] = e.get("id")
        pr = (e.get("proj") or {})
        g = pr.get("gp") or 0
        p["line"] = {s_: round((pr.get(s_) or 0) / g, 1) for s_ in ("min", "pts", "reb", "ast", "stl", "blk", "tov", "tpm")} if g else None
        pre = pres.get(str(p["espn_id"])) if p["espn_id"] else None
        p["pre"] = {"g": pre[0], "fppg": round(pre[1], 1), "mpg": round(pre[2], 1)} if pre else None
    # consensus overall rank among drafted + everyone projected (season points)
    pool = {}
    for k, s in proj.items():
        vals = list(s.values())
        pool[k] = sum(v["fppg"] for v in vals) / len(vals) * sum(v["gp"] for v in vals) / len(vals)
    ranked = sorted(pool, key=lambda k: -pool[k])
    rank_of = {k: i + 1 for i, k in enumerate(ranked)}
    for p in picks:
        p["proj_rank"] = rank_of.get(key(p["player"]))
        p["value"] = (p["overall"] - p["proj_rank"]) if p["proj_rank"] else None
    teams = {}
    for p in picks:
        teams.setdefault(p["team"], []).append(p)
    out = {"league": LEAGUES[league]["name"], "source": src, "slots": SLOTS, "teams": {}, "missing": missing}
    for t, roster in teams.items():
        lu_total, lu = lineup_exact(roster)
        season, games, lost = simulate(roster, sched)
        starters = {id(p): s for p, s in lu}
        for p in roster:
            p["slot"] = starters.get(id(p), "BN")
        out["teams"][t] = {"draft_slot": roster[0]["rpick"], "picks": roster, "lineup_fppg": round(lu_total, 1),
                           "season_pts": round(season), "lineup_games": round(games), "bench_lost": round(lost), "roster_fpts": round(sum(p["fpts"] for p in roster))}
    order = sorted(out["teams"], key=lambda t: -out["teams"][t]["season_pts"])
    for i, t in enumerate(order, 1):
        out["teams"][t]["rank"] = i
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("missing projections:", missing)
    for t in order:
        d = out["teams"][t]
        print(f'{d["rank"]:2} {t:22} slot {d["draft_slot"]:2}  season {d["season_pts"]:6}  full-health lineup {d["lineup_fppg"]:6} FP/G')


if __name__ == "__main__":
    main()
