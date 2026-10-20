"""
Superbet: offer API at https://production-superbet-offer-rs.freetls.fastly.net/sb-rs/api/v2/sr-Latn-RS
  /events/by-date?offerState=prematch&startDate=..&endDate=..   all events (EuroLeague = tournament 19114)
  /events/<eventId>                                            all markets of one game
Player props sit inside the game: odds with specifiers {"player": "Carter, Tyson", "total": "8.5"}
or {"milestone": "10", ...}. No team is given per player.
"""

import datetime as dt

from .common import local_from_utc, row, session, to_float

API = "https://production-superbet-offer-rs.freetls.fastly.net/sb-rs/api/v2/sr-Latn-RS"
TOURNAMENT = 19114

# marketName prefix (lower-case, double spaces collapsed) -> market. Over/under and milestone
# markets share names, so the outcome decides which kind it is.
MARKETS = [
    ("poeni igrača + skokovi + asistencije", "Pts+Reb+Ast"), ("poeni igrača + skokovi", "Pts+Reb"),
    ("poeni igrača + asistencije", "Pts+Ast"), ("skokovi + asistencije igrača", "Reb+Ast"),
    ("ukupno poena igrača", "Points"), ("ukupno skokova igrača", "Rebounds"),
    ("ukupno asistencija igrača", "Assists"), ("ukupno pogodaka za 3 poena", "3PM"),
    ("3 poena igrača", "3PM"), ("ukupno ukradenih lopti igrača", "Steals"),
    ("ukupno blokada igrača", "Blocks"), ("dabl dabl igrača", "Double-double"),
]


def player_name(raw):
    last, sep, first = raw.partition(", ")
    return f"{first} {last}".strip() if sep else raw


def scrape():
    s = session()
    today = dt.date.today()
    r = s.get(f"{API}/events/by-date", timeout=60, params={
        "currentStatus": "active", "offerState": "prematch",
        "startDate": f"{today} 00:00:00", "endDate": f"{today + dt.timedelta(days=7)} 00:00:00"})
    r.raise_for_status()
    games = []
    for e in r.json()["data"]:
        if e.get("tournamentId") == TOURNAMENT:
            home, _, away = e["matchName"].partition("·")
            games.append({"id": e["eventId"], "home": home, "away": away,
                          "kickoff": local_from_utc(e["utcDate"])})
    print(f"  {len(games)} games")

    rows, unknown = [], set()
    for g in games:
        r = s.get(f"{API}/events/{g['id']}", timeout=30)
        r.raise_for_status()
        data = r.json()["data"]
        for o in (data[0] if isinstance(data, list) else data)["odds"]:
            spec = o.get("specifiers") or {}
            if "player" not in spec or o.get("status") != "active" or not o.get("price"):
                continue
            mname = " ".join(o["marketName"].lower().split())
            market = next((m for prefix, m in MARKETS if mname.startswith(prefix)), None)
            if not market:
                unknown.add(o["marketName"])           # boosts and "team wins + player" combos
                continue
            name = o.get("name") or ""
            if "milestone" in spec:
                side, line = f"{spec['milestone']}+", None
            elif "total" in spec and ("Više" in name or "Manje" in name):
                side, line = ("Over" if "Više" in name else "Under"), to_float(spec["total"])
            elif market == "Double-double":
                if not name.rstrip().endswith(("Da", "da")):
                    continue
                side, line = "Yes", None
            else:
                unknown.add(f"{o['marketName']}/{name}")
                continue
            rows.append(row(g, None, player_name(spec["player"]), market, side, line, o["price"],
                            g["id"], player_raw=spec["player"]))
    if unknown:
        print("  Skipped:", ", ".join(sorted(unknown))[:400])
    return rows, games
