"""
AdmiralBet: offer API at https://srboffer.admiralbet.rs/api/offer.
  /getWebEventsSelections?sportId=2&regionId=123&competitionId=3060      EuroLeague games
  /getWebEventsSelections?sportId=123&regionId=1416&competitionId=25158  one event per player,
                                                                          named "Player - Team"
  /betsAndGroups/123/1416/25158/<eventId>                                that player's full odds
Bets carry a readable betTypeName ("Ukupno poena", "Postiže poena" milestones, ...).
"""

import time

from .common import DELAY_S, game_finder, local_from_utc, row, session, to_float

API = "https://srboffer.admiralbet.rs/api/offer"
GAMES = {"sportId": 2, "regionId": 123, "competitionId": 3060}
PLAYERS = {"sportId": 123, "regionId": 1416, "competitionId": 25158}

BET_TYPES = {   # betTypeName (lower-case) -> market
    "ukupno poena": "Points", "ukupno skokova": "Rebounds", "ukupno asistencija": "Assists",
    "ukupno pogodjenih trojki": "3PM", "ukupno ukradenih lopti": "Steals",
    "ukupno blokada": "Blocks", "ukupno izgubljenih lopti": "Turnovers",
    "ukupno poena+asistencija+skokova": "Pts+Reb+Ast", "ukupno poena+skokova": "Pts+Reb",
    "ukupno poena+asistencija": "Pts+Ast", "ukupno asistencija+skokova": "Reb+Ast",
}
MILESTONE_TYPES = {"postiže poena": "Points", "postize poena": "Points", "postiže skokova": "Rebounds",
                   "postiže asistencija": "Assists", "postiže trojki": "3PM"}


def events(s, ids):
    q = {**ids, "pageId": 35, "isLive": "false", "dateFrom": "2020-01-01T00:00:00.000",
         "dateTo": "2031-01-01T00:00:00.000"}
    r = s.get(f"{API}/getWebEventsSelections", params=q, timeout=30)
    r.raise_for_status()
    return r.json()


def scrape():
    s = session(Origin="https://admiralbet.rs", Referer="https://admiralbet.rs/",
                Language="sr-Latn", OfficeId="138")
    games = []
    for e in events(s, GAMES):
        home, _, away = e["name"].partition(" - ")
        games.append({"id": e["id"], "home": home.strip(), "away": away.strip(),
                      "kickoff": local_from_utc(e["dateTime"])})
    find = game_finder(games)
    players = events(s, PLAYERS) if PLAYERS else []      # no player competition known (NBA)
    print(f"  {len(games)} games, {len(players)} players")

    rows, unknown = [], set()
    for p in players:
        r = s.get(f"{API}/betsAndGroups/{PLAYERS['sportId']}/{PLAYERS['regionId']}/"
                  f"{PLAYERS['competitionId']}/{p['id']}", timeout=30)
        r.raise_for_status()
        time.sleep(DELAY_S)
        player, _, team = p["name"].rpartition(" - ")
        kickoff = local_from_utc(p["dateTime"])
        game = find(team.strip(), kickoff)
        for b in r.json().get("bets", []):
            kind = b["betTypeName"].strip().lower()
            for o in b["betOutcomes"]:
                if not o.get("odd") or not o.get("isPlayable", True):
                    continue
                name = (o.get("name") or "").strip().lower()
                if kind in BET_TYPES and name in ("vise", "više", "manje"):
                    side = "Under" if name == "manje" else "Over"
                    rows.append(row(game, team.strip(), player.strip(), BET_TYPES[kind], side,
                                    to_float(o.get("sbv") or b.get("sbv")), o["odd"], p["id"], kickoff=kickoff))
                elif kind in MILESTONE_TYPES and name.endswith("+"):
                    rows.append(row(game, team.strip(), player.strip(), MILESTONE_TYPES[kind], name, None,
                                    o["odd"], p["id"], kickoff=kickoff))
                else:
                    unknown.add(f"{b['betTypeName']}/{o.get('name')}")
    if unknown:
        print("  Skipped:", ", ".join(sorted(unknown)))
    return rows, games
