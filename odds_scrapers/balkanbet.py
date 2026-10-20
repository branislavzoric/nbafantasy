"""
BalkanBet: NSoft "SM" sportsbook. Distribution API https://sports-sm-distribution-api.de-2.nsoftcdn.com/api/v1
(company 4f54c6aa-82a9-475d-bf0e-dc02ded89225). Object query params (language, dataFormat) must be
JSON strings.
  /events?filter[tournamentId]=252     EuroLeague games
  /events?filter[tournamentId]=29227   "Igrači Evroliga": one event per player, "D.Bacon (Dubai)"
  /events/<id>                         that player's full markets (the list leaves some out)
"""

import datetime as dt
import json
import re
import time

from .common import DELAY_S, game_finder, local_from_utc, row, session, to_float

API = "https://sports-sm-distribution-api.de-2.nsoftcdn.com/api/v1"
COMPANY = "4f54c6aa-82a9-475d-bf0e-dc02ded89225"
GAMES_TOURNAMENT, PLAYERS_TOURNAMENT = 252, 29227
MARKETS = {2402: "Points", 5091: "Points", 2403: "Rebounds", 2406: "Assists", 3087: "3PM",
           5094: "3PM", 5500: "Rebounds", 5503: "Assists", 3129: "Reb+Ast", 5062: "Pts+Reb+Ast",
           3123: "Pts+Reb", 3126: "Pts+Ast", 3138: "Pts+Reb+Ast", 4902: "1Q Points", 6613: "1H Points"}


def scrape():
    s = session(Origin="https://sports-sm-web.7platform.net")
    base = {"companyUuid": COMPANY, "timezone": "Europe/Belgrade", "deliveryPlatformId": 3,
            "language": json.dumps({"default": "sr-Latn"}),
            "dataFormat": json.dumps({"default": "object", "events": "array", "outcomes": "array"})}

    def get(path, **params):
        r = s.get(f"{API}/{path}", params={**base, **params}, timeout=40)
        r.raise_for_status()
        return r.json()["data"]

    window = {"filter[from]": dt.datetime.now().strftime("%Y-%m-%dT00:00:00"),
              "filter[to]": (dt.datetime.now() + dt.timedelta(days=7)).strftime("%Y-%m-%dT23:59:59")}
    games = []
    for e in get("events", **window, **{"filter[tournamentId]": GAMES_TOURNAMENT})["events"]:
        home, _, away = e["name"].partition(" - ")
        games.append({"id": e["id"], "home": home.strip(), "away": away.strip(),
                      "kickoff": local_from_utc(e["startsAt"])})
    find = game_finder(games)
    players = (get("events", **window, **{"filter[tournamentId]": PLAYERS_TOURNAMENT})["events"]
               if PLAYERS_TOURNAMENT else [])                # no player tournament known (NBA)
    print(f"  {len(games)} games, {len(players)} players")

    rows, unknown = [], set()
    for p in players:
        full = get(f"events/{p['id']}")
        time.sleep(DELAY_S)
        m = re.fullmatch(r"(.+?)\s*\((.+)\)", full["name"])
        player, team = (m.group(1), m.group(2)) if m else (full["name"], None)
        kickoff = local_from_utc(full["startsAt"])
        game = find(team, kickoff)
        for mk in (full.get("markets") or {}).values():
            market = MARKETS.get(mk.get("marketId"))
            if not market:
                unknown.add(mk.get("name"))
                continue
            line = to_float((mk.get("specialValues") or [None])[0])
            for o in mk.get("outcomes") or []:
                name = (o.get("name") or "").lower()
                side = "Over" if name.startswith("više") else "Under" if name.startswith("manje") else None
                if side and o.get("odd") and line is not None:
                    rows.append(row(game, team, player, market, side, line, o["odd"], p["id"], kickoff=kickoff))
    if unknown:
        print("  Skipped:", ", ".join(sorted(map(str, unknown))))
    return rows, games
