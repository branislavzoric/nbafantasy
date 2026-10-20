"""
Soccerbet and MaxBet: both run the same Angular sportsbook on a public JSON API (/restapi/offer/sr):
  /ttg_lang                                bet-type dictionary (tip type code -> tipTypeName)
  /sport/B/mob                             all basketball games (EuroLeague picked by league id)
  /ext/sport/B/league/<league>/PL/mob      one "match" per player (home = player, away = team)
  /match/<id>                              that player's full odds
Soccerbet returns betMap {code: {"total=14.5": {ov, s}}} (several alt lines per code);
MaxBet returns odds {code: price} plus params {"ouPlPoints": "14.5", ...} holding the lines.
"""

import re
import time

from .common import DELAY_S, game_finder, local_from_ms, row, session

BOOKS = {
    "soccerbet": {"base": "https://www.soccerbet.rs/restapi/offer/sr", "annex": "0",
                  "league": 2516963, "pick_suffix": "B"},
    "maxbet": {"base": "https://www.maxbet.rs/restapi/offer/sr", "annex": "3",
               "league": 131600, "pick_suffix": "SK"},
}

LINE_MARKETS = {"POINTS": "Points", "REBOUNDS": "Rebounds", "ASSISTS": "Assists", "3PTS": "3PM",
                "TO": "Turnovers", "STEALS": "Steals", "BLOCKS": "Blocks",
                "PRA": "Pts+Reb+Ast", "PR": "Pts+Reb", "PA": "Pts+Ast", "RA": "Reb+Ast"}
MILESTONES = {"P": "Points", "R": "Rebounds", "A": "Assists", "3P": "3PM"}
# MaxBet keeps the line of each over/under in params under these keys (_2 / _3 = alt lines)
MAXBET_PARAMS = {"POINTS": "ouPlPoints", "REBOUNDS": "ouPlRebounds", "ASSISTS": "ouPlAssists",
                 "3PTS": "ouPl3Points", "PRA": "ouPlTPRA", "PR": "ouPlTPR", "PA": "ouPlTPA",
                 "RA": "ouPlTRA", "TO": "ouPlTO", "STEALS": "ouPlSt", "BLOCKS": "ouPlB"}


def decode(tip_name):
    """PL_POINTS_OVER(_2) -> ("Points", "Over", "POINTS", "2"); PLP15_PLUS -> ("Points", "15+", None, None)."""
    m = re.fullmatch(r"PL_([A-Z0-9]+)_(OVER|UNDER)(?:_(\d))?", tip_name or "")
    if m and m.group(1) in LINE_MARKETS:
        return LINE_MARKETS[m.group(1)], m.group(2).title(), m.group(1), m.group(3)
    m = re.fullmatch(r"PL(3P|P|R|A)(\d+)_PLUS", tip_name or "")
    if m:
        return MILESTONES[m.group(1)], f"{m.group(2)}+", None, None
    return None, None, None, None


def scrape(book):
    cfg = BOOKS[book]
    s = session()
    params = {"annex": cfg["annex"], "desc": "true", "mobileVersion": "2.0.0", "locale": "sr"}

    def get(path):
        r = s.get(f"{cfg['base']}/{path}", params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    picks = get("ttg_lang")["betPickMap"]
    games = [{"id": g["id"], "home": g["home"], "away": g["away"], "kickoff": local_from_ms(g["kickOffTime"])}
             for g in get("sport/B/mob")["esMatches"] if g["leagueId"] == cfg["league"]]
    find = game_finder(games)
    players = get(f"ext/sport/B/league/{cfg['league']}/PL/mob")["esMatches"]
    print(f"  {len(games)} games, {len(players)} players")

    rows, unknown = [], set()
    for p in players:
        full = get(f"match/{p['id']}")
        time.sleep(DELAY_S)
        team, kickoff = p["away"], local_from_ms(p["kickOffTime"])
        game = find(team, kickoff)
        if "betMap" in full:                                   # Soccerbet format
            items = [(tt, sv, o["ov"], o.get("s") == "U") for tt, svs in full["betMap"].items()
                     for sv, o in svs.items()]
        else:                                                  # MaxBet format
            items = [(tt, None, ov, True) for tt, ov in (full.get("odds") or {}).items()]
        for tt, sv, odd, active in items:
            if not odd or not active:
                continue
            name = (picks.get(f"{tt}_{cfg['pick_suffix']}") or picks.get(f"{tt}_B") or {}).get("tipTypeName")
            market, side, stem, alt = decode(name)
            if not market:
                unknown.add(name or tt)
                continue
            line = None
            if stem:
                if sv is not None:
                    m = re.search(r"total=([\d.]+)", sv)
                    line = float(m.group(1)) if m else None
                else:
                    key = MAXBET_PARAMS.get(stem, "")
                    if alt:
                        key = {"POINTS": "ouPlP"}.get(stem, key + "_") + alt
                    value = (full.get("params") or {}).get(key)
                    line = float(value) if value else None
                if line is None:
                    unknown.add(f"{name} (no line)")
                    continue
            rows.append(row(game, team, p["home"], market, side, line, odd, p["id"], kickoff=kickoff))
    if unknown:
        print("  Skipped:", ", ".join(sorted(map(str, unknown))))
    return rows, games
