#!/usr/bin/env python3
"""
league_scoring.py

The user's Yahoo league scoring (league 478.l.37924), checked to the cent against the "Fan Pts" column of the
saved Yahoo pages (2026-10-16):

  FGM +1, FGA -0.45, FTM +1, FTA -0.75, PTS +1, REB +1, AST +1, STL +1.25, BLK +1.25, TO -1, PF -1,
  DD +2.5, TD +5, TECH -2.5, EJCT -5   (bonus values given by the user)

Yahoo shows "-" for TECH/EJCT/DD/TD, so those are projected here as last season's (2025-26) per-game rate from ESPN
(stat ids 37 DD, 38 TD, 10 technical fouls, 7 ejections; 42 GP) times the player's projected games.
Players without a 2025-26 line (rookies) get 0.

espn_players() pulls ESPN's 2026-27 projection totals + 2025-26 actuals once and caches them for 6 hours in
espn_raw_stats.json, keyed by normalised name.
"""

import json
import os
import re
import time

from build_book3_draft_board import key as book3_key

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(PROJECT_DIR, "espn_raw_stats.json")

WEIGHTS = {"fgm": 1, "fga": -0.45, "ftm": 1, "fta": -0.75, "pts": 1, "reb": 1, "ast": 1,
           "stl": 1.25, "blk": 1.25, "tov": -1, "pf": -1}
BONUS = {"dd": 2.5, "td": 5, "tech": -2.5, "ej": -5}
ESPN_IDS = {"pts": "0", "blk": "1", "stl": "2", "ast": "3", "reb": "6", "ej": "7", "pf": "9", "tech": "10",
            "tov": "11", "fgm": "13", "fga": "14", "ftm": "15", "fta": "16", "tpm": "17", "dd": "37", "td": "38",
            "min": "40", "gp": "42"}
SCORING_TEXT = ("FGM +1, FGA −0.45, FTM +1, FTA −0.75, PTS/REB/AST +1, STL/BLK +1.25, TO/PF −1, "
                "DD +2.5, TD +5, TECH −2.5, EJCT −5")

# Boogie Nights (Yahoo league 43952, drafted as 12 teams), from "Yahoo Snapshots/boogie nights league settings.html" (2026-10-16)
BOOGIE_WEIGHTS = dict(WEIGHTS, ast=1.25, stl=2, blk=2, pf=-0.5)
BOOGIE_BONUS = dict(BONUS, tech=-1.25)
LEAGUES = {
    "trebevicka": {"name": "trebevicka23", "teams": 12, "weights": WEIGHTS, "bonus": BONUS, "text": SCORING_TEXT},
    "boogie": {"name": "Boogie Nights", "teams": 12, "weights": BOOGIE_WEIGHTS, "bonus": BOOGIE_BONUS,
               "text": ("FGM +1, FGA −0.45, FTM +1, FTA −0.75, PTS/REB +1, AST +1.25, STL/BLK +2, TO −1, PF −0.5, "
                        "DD +2.5, TD +5, TECH −1.25, EJCT −5")},
}


def key(name):
    """book3 key, plus II/III/IV dropped (FantasyPros: 'Jimmy Butler III', ESPN: 'Jimmy Butler')."""
    return re.sub(r" (ii|iii|iv)$", "", book3_key(name))


def espn_players():
    if os.path.exists(CACHE) and time.time() - os.path.getmtime(CACHE) < 6 * 3600:
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    from espn_nba_projections import fetch_all_players, parse_player
    out = {}
    for entry in fetch_all_players():
        info = parse_player(entry)
        if not info:
            continue
        blocks = {b.get("id"): b.get("stats") or {} for b in entry["player"].get("stats", [])}
        pick = lambda block: {k: blocks.get(block, {}).get(i) for k, i in ESPN_IDS.items()}
        out[key(info["full_name"])] = {
            "id": info["player_id"], "name": info["full_name"], "team": info["pro_team"],
            "positions": info["positions"], "injury": info["injury_status"] or "", "rank": info["espn_rank"],
            "proj": pick("102027"), "last": pick("002026")}
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    return out


def per_game(espn, stat):
    """2025-26 per-game rate of an ESPN stat, or None without a usable line."""
    last = (espn or {}).get("last") or {}
    gp = last.get("gp") or 0
    return (last.get(stat) or 0) / gp if gp else None


def bonus(espn, gp, values=BONUS):
    """Projected DD/TD/TECH/EJ counts for gp games and their points."""
    counts = {s: (per_game(espn, s) or 0) * gp for s in values}
    return counts, sum(counts[s] * w for s, w in values.items())


def score(stats, weights=WEIGHTS):
    """League points for season totals {fgm, fga, ftm, fta, pts, reb, ast, stl, blk, tov, pf}."""
    return sum((stats.get(k) or 0) * w for k, w in weights.items())
