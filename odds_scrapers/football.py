"""
Football (soccer) match odds from the same six Serbian bookmakers, basic markets only:
  1X2 (1 / X / 2), DC = double chance (1X / 12 / X2), Total 2.5 goals (Under / Over),
  BTTS = both teams to score (GG / NG).
Top 8 leagues: Premier League, LaLiga, Serie A, Bundesliga, Ligue 1, Eredivisie,
Primeira Liga, Belgian Pro League. Games that start today (local time) and haven't
kicked off yet.

Every scraper returns rows {"book", "league", "home", "away", "kickoff", "market", "sel", "odds"}
(kickoff = local naive datetime). "0-2" / "3+" goal ranges are Under / Over 2.5.

Endpoints (all public JSON, see the player-prop scrapers in this package for the same APIs):
  Soccerbet/MaxBet  /restapi/offer/sr/sport/S/mob           every football match with its basic odds
  AdmiralBet        /api/offer/getWebEventsSelections       per competition, bets inline
  MozzartBet        /betting/matches ("Top Lige" offer)     via curl (Cloudflare)
  Superbet          /events/by-date + /events/<id>          list has only 1X2, so one call per match
  BalkanBet         NSoft /events?filter[tournamentId]=..   per tournament, markets inline
"""

import datetime as dt
import json
import time

from .common import DELAY_S, local_from_ms, local_from_utc, session, to_float

LEAGUES = ["Premier League", "LaLiga", "Serie A", "Bundesliga", "Ligue 1", "Eredivisie", "Primeira Liga",
           "Belgian Pro League"]


def _today(kickoff):
    now = dt.datetime.now()
    return kickoff.date() == now.date() and kickoff > now


def _row(book, league, home, away, kickoff, market, sel, odds):
    odds = to_float(odds)
    if not odds or odds <= 1.0:
        return None
    return {"book": book, "league": league, "home": " ".join(home.split()), "away": " ".join(away.split()),
            "kickoff": kickoff, "market": market, "sel": sel, "odds": odds}


def _clean(rows):
    return [r for r in rows if r]

# -----------------------------------------------------------------------------
# Soccerbet / MaxBet (same platform, same bet codes)
# -----------------------------------------------------------------------------

DUALSOFT = {
    "soccerbet": {"base": "https://www.soccerbet.rs/restapi/offer/sr", "annex": "0",
                  "leagues": {2516076: "Premier League", 2516061: "LaLiga", 2516000: "Serie A",
                              2515986: "Bundesliga", 2515968: "Ligue 1", 2516075: "Eredivisie",
                              2515943: "Primeira Liga", 2515984: "Belgian Pro League"}},
    "maxbet": {"base": "https://www.maxbet.rs/restapi/offer/sr", "annex": "3",
               "leagues": {152506: "Premier League", 117709: "LaLiga", 117689: "Serie A",
                           117683: "Bundesliga", 117827: "Ligue 1", 117808: "Eredivisie",
                           132736: "Primeira Liga", 152565: "Belgian Pro League"}},
}
DUALSOFT_CODES = {"1": ("1X2", "1"), "2": ("1X2", "X"), "3": ("1X2", "2"), "7": ("DC", "1X"), "8": ("DC", "12"),
                  "9": ("DC", "X2"), "22": ("Total 2.5", "Under"), "24": ("Total 2.5", "Over"),
                  "272": ("BTTS", "GG"), "273": ("BTTS", "NG")}


def scrape_dualsoft(book, all_leagues=False):
    cfg = DUALSOFT[book]
    s = session()
    r = s.get(f"{cfg['base']}/sport/S/mob", timeout=60,
              params={"annex": cfg["annex"], "desc": "true", "mobileVersion": "2.0.0", "locale": "sr"})
    r.raise_for_status()
    rows = []
    for m in r.json()["esMatches"]:
        league = cfg["leagues"].get(m["leagueId"]) or (m.get("leagueName") if all_leagues else None)
        kickoff = local_from_ms(m["kickOffTime"])
        if not league or not _today(kickoff) or m.get("blocked"):
            continue
        if "betMap" in m:                                         # Soccerbet
            prices = {c: v.get("NULL", {}).get("ov") for c, v in m["betMap"].items()
                      if v.get("NULL", {}).get("s", "U") == "U"}
        else:                                                     # MaxBet
            prices = m.get("odds") or {}
        for code, (market, sel) in DUALSOFT_CODES.items():
            rows.append(_row(book, league, m["home"], m["away"], kickoff, market, sel, prices.get(code)))
    return _clean(rows)

# -----------------------------------------------------------------------------
# AdmiralBet
# -----------------------------------------------------------------------------

ADMIRAL_API = "https://srboffer.admiralbet.rs/api/offer"
ADMIRAL_LEAGUES = {(1, 1): "Premier League", (24, 3103): "LaLiga", (23, 597): "Serie A", (22, 614): "Bundesliga",
                   (4, 564): "Ligue 1", (27, 608): "Eredivisie", (28, 626): "Primeira Liga",
                   (25, 606): "Belgian Pro League"}


def scrape_admiral(all_leagues=False):
    s = session(Origin="https://admiralbet.rs", Referer="https://admiralbet.rs/", Language="sr-Latn",
                OfficeId="138")
    rows = []
    today = dt.date.today()
    requests_ = ([(None, {"dateFrom": f"{today}T00:00:00.000", "dateTo": f"{today}T23:59:59.000"})] if all_leagues
                 else [(league, {"regionId": region, "competitionId": comp, "dateFrom": "2020-01-01T00:00:00.000",
                                 "dateTo": "2031-01-01T00:00:00.000"})
                       for (region, comp), league in ADMIRAL_LEAGUES.items()])
    for fixed, extra in requests_:
        r = s.get(f"{ADMIRAL_API}/getWebEventsSelections", timeout=90, params={
            "sportId": 1, "pageId": 35, "isLive": "false", **extra})
        r.raise_for_status()
        time.sleep(DELAY_S)
        for e in r.json():
            league = fixed or ADMIRAL_LEAGUES.get((e.get("regionId"), e.get("competitionId"))) or f"{e.get('regionName', '')} | {e.get('competitionName', '')}"
            if e.get("isLive"):
                continue
            home, sep, away = e["name"].partition(" - ")
            kickoff = local_from_utc(e["dateTime"])
            if not sep or e.get("eventTypeId") != 1 or not _today(kickoff):
                continue
            for b in e.get("bets") or []:
                for o in b["betOutcomes"]:
                    if not o.get("isPlayable", True):
                        continue
                    name = (o.get("name") or "").strip().lower()
                    market = sel = None
                    if b["betTypeId"] == 135 and name in ("1", "x", "2"):
                        market, sel = "1X2", name.upper()
                    elif b["betTypeId"] == 137 and to_float(o.get("sbv") or b.get("sbv")) == 2.5:
                        market, sel = "Total 2.5", {"manje": "Under", "vise": "Over", "više": "Over"}.get(name)
                    elif b["betTypeId"] == 1380 and name == "gg":
                        market, sel = "BTTS", "GG"
                    if market and sel:
                        rows.append(_row("admiralbet", league, home, away, kickoff, market, sel, o.get("odd")))
    return _clean(rows)

# -----------------------------------------------------------------------------
# MozzartBet
# -----------------------------------------------------------------------------

MOZZART_LEAGUES = {"Engleska 1": "Premier League", "Španija 1": "LaLiga", "Italija 1": "Serie A",
                   "Nemačka 1": "Bundesliga", "Francuska 1": "Ligue 1", "Holandija 1": "Eredivisie",
                   "Portugalija 1": "Primeira Liga", "Belgija 1": "Belgian Pro League"}
MOZZART_GAMES = {("Konačan ishod", "1"): ("1X2", "1"), ("Konačan ishod", "X"): ("1X2", "X"),
                 ("Konačan ishod", "2"): ("1X2", "2"), ("Dupla šansa", "1X"): ("DC", "1X"),
                 ("Dupla šansa", "12"): ("DC", "12"), ("Dupla šansa", "X2"): ("DC", "X2"),
                 ("Ukupno golova na meču", "0-2"): ("Total 2.5", "Under"),
                 ("Ukupno golova na meču", "3+"): ("Total 2.5", "Over"),
                 ("Oba tima daju gol", "GG"): ("BTTS", "GG"), ("Oba tima daju gol", "NG"): ("BTTS", "NG")}


def scrape_mozzart(all_leagues=False):
    from .mozzartbet import Curl
    c = Curl()
    offers = c.call("POST", "/betting/get-special-offer", {}).get("specialOffer", [])
    offer = next((o for o in offers if o.get("name") == "Top Lige"), None)
    if not offer:
        print("  No 'Top Lige' offer right now")
        return []
    f = {**offer["filter"], "sportIds": [1]}
    items, page = [], 0
    while True:
        batch = c.call("POST", "/betting/matches", {**f, "currentPage": page}).get("items") or []
        items += batch
        if len(batch) < f.get("pageSize", 50) or page > 40:
            break
        page += 1
    rows = []
    for i in items:
        league = MOZZART_LEAGUES.get(i["competition"]["name"].strip()) or (i["competition"]["name"].strip() if all_leagues else None)
        kickoff = local_from_ms(i["startTime"])
        if not league or not _today(kickoff) or i.get("sport", {}).get("id", 1) != 1:
            continue
        for group in i.get("oddsGroup", []):
            for o in group["odds"]:
                hit = MOZZART_GAMES.get((o["game"]["name"], o["subgame"]["name"]))
                if hit and o.get("oddStatus") == "ACTIVE":
                    rows.append(_row("mozzartbet", league, i["home"]["name"], i["visitor"]["name"], kickoff,
                                     *hit, o.get("value")))
    return _clean(rows)

# -----------------------------------------------------------------------------
# Superbet
# -----------------------------------------------------------------------------

SUPERBET_API = "https://production-superbet-offer-rs.freetls.fastly.net/sb-rs/api/v2/sr-Latn-RS"
SUPERBET_LEAGUES = {106: "Premier League", 98: "LaLiga", 104: "Serie A", 245: "Bundesliga", 100: "Ligue 1",
                    256: "Eredivisie", 142: "Primeira Liga", 324: "Belgian Pro League"}


def scrape_superbet(all_leagues=False):
    s = session()
    today = dt.date.today()
    r = s.get(f"{SUPERBET_API}/events/by-date", timeout=60, params={
        "currentStatus": "active", "offerState": "prematch",
        "startDate": f"{today - dt.timedelta(days=1)} 22:00:00", "endDate": f"{today + dt.timedelta(days=1)} 00:00:00"})
    r.raise_for_status()
    todo = []
    for e in r.json()["data"]:
        league = SUPERBET_LEAGUES.get(e.get("tournamentId")) or (f"Superbet tournament {e.get('tournamentId')}" if all_leagues else None)
        kickoff = local_from_utc(e["utcDate"])
        if e.get("sportId") != 5 or not league or not _today(kickoff):
            continue
        todo.append((e, league, kickoff))

    def detail(item):
        e = item[0]
        for i in range(3):
            try:
                d = s.get(f"{SUPERBET_API}/events/{e['eventId']}", timeout=30)
                d.raise_for_status()
                time.sleep(DELAY_S)
                return d.json()["data"]
            except Exception:
                time.sleep(2 + 3 * i)
        return None

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(4 if all_leagues else 1) as pool:   # all leagues = hundreds of calls
        details = list(pool.map(detail, todo))
    rows = []
    for (e, league, kickoff), data in zip(todo, details):
        if not data:
            continue
        home, _, away = e["matchName"].partition("·")
        for o in (data[0] if isinstance(data, list) else data)["odds"]:
            if o.get("status") != "active":
                continue
            name, mid, spec = o.get("name") or "", o["marketId"], o.get("specifiers") or {}
            market = sel = None
            if mid in (547, 100001) and name in ("1", "X", "2"):
                market, sel = "1X2", name
            elif mid == 531 and name in ("1X", "12", "X2"):
                market, sel = "DC", name
            elif mid == 200734 and spec.get("total") == "2.5":
                market, sel = "Total 2.5", "Under" if name.startswith("Manje") else "Over"
            elif mid == 539 and name in ("Da", "Ne"):
                market, sel = "BTTS", "GG" if name == "Da" else "NG"
            if market:
                rows.append(_row("superbet", league, home, away, kickoff, market, sel, o.get("price")))
    return _clean(rows)

# -----------------------------------------------------------------------------
# BalkanBet (NSoft)
# -----------------------------------------------------------------------------

NSOFT_API = "https://sports-sm-distribution-api.de-2.nsoftcdn.com/api/v1"
NSOFT_COMPANY = "4f54c6aa-82a9-475d-bf0e-dc02ded89225"
BALKAN_LEAGUES = {33: "Premier League", 15: "LaLiga", 51: "Serie A", 87: "Bundesliga", 84: "Ligue 1",
                  93: "Eredivisie", 516: "Primeira Liga", 96: "Belgian Pro League"}
BALKAN_MARKETS = {6: {"1": ("1X2", "1"), "X": ("1X2", "X"), "2": ("1X2", "2")},
                  368: {"1X": ("DC", "1X"), "12": ("DC", "12"), "X2": ("DC", "X2")},
                  443: {"0-2": ("Total 2.5", "Under"), "3+": ("Total 2.5", "Over")},
                  425: {"GG": ("BTTS", "GG"), "NG": ("BTTS", "NG")}}


def scrape_balkan(all_leagues=False):
    s = session(Origin="https://sports-sm-web.7platform.net")
    base = {"companyUuid": NSOFT_COMPANY, "timezone": "Europe/Belgrade", "deliveryPlatformId": 3,
            "language": json.dumps({"default": "sr-Latn"}),
            "dataFormat": json.dumps({"default": "object", "events": "array", "outcomes": "array"}),
            "filter[from]": dt.datetime.now().strftime("%Y-%m-%dT00:00:00"),
            "filter[to]": dt.datetime.now().strftime("%Y-%m-%dT23:59:59")}
    rows = []
    calls = ([(None, {"filter[sportId]": 18})] if all_leagues       # 18 = football
             else [(league, {"filter[tournamentId]": tid}) for tid, league in BALKAN_LEAGUES.items()])
    for fixed, extra in calls:
        r = s.get(f"{NSOFT_API}/events", params={**base, **extra}, timeout=120)
        r.raise_for_status()
        time.sleep(DELAY_S)
        for e in r.json()["data"]["events"]:
            league = fixed or BALKAN_LEAGUES.get(e.get("tournamentId")) or f"Balkan tournament {e.get('tournamentId')}"
            home, sep, away = e["name"].partition(" - ")
            kickoff = local_from_utc(e["startsAt"])
            if not sep or not _today(kickoff):
                continue
            for mk in (e.get("markets") or {}).values():
                names = BALKAN_MARKETS.get(mk.get("marketId"))
                if not names or not mk.get("active", 1):
                    continue
                for o in mk.get("outcomes") or []:
                    hit = names.get(o.get("name"))
                    if hit and o.get("active", 1):
                        rows.append(_row("balkanbet", league, home, away, kickoff, *hit, o.get("odd")))
    return _clean(rows)


SCRAPERS = {"soccerbet": lambda: scrape_dualsoft("soccerbet"), "maxbet": lambda: scrape_dualsoft("maxbet"),
            "admiralbet": scrape_admiral, "mozzartbet": scrape_mozzart, "superbet": scrape_superbet,
            "balkanbet": scrape_balkan}
SCRAPERS_ALL = {"soccerbet": lambda: scrape_dualsoft("soccerbet", True),
                "maxbet": lambda: scrape_dualsoft("maxbet", True), "admiralbet": lambda: scrape_admiral(True),
                "mozzartbet": lambda: scrape_mozzart(True), "superbet": lambda: scrape_superbet(True),
                "balkanbet": lambda: scrape_balkan(True)}
