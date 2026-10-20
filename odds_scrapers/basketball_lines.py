"""
EuroLeague (SCRAPERS) and NBA (NBA_SCRAPERS) game lines from the six Serbian bookmakers: Winner, Handicap and Total points, all
including overtime. Today's games that haven't tipped off yet.

Every scraper returns rows {"book", "league", "home", "away", "kickoff", "market", "sel", "odds", "ot"}:
  market  "Winner"            sel "1" / "2"
          "Handicap <h>"      h = the HOME team's handicap ("-6.5" = home gives 6.5), sel "1" / "2"
          "Total <t>"         sel "Under" / "Over"
  ot      True when the book says the market includes overtime. Mozzart's handicap and total don't say
          so (its winner market does), so they're flagged False and left out of fair prices and arbitrage.

Endpoints (the same APIs as the player-prop scrapers in this package):
  Soccerbet/MaxBet  /sport/B/mob (games) + /match/<id>       FT_OT_1/2, OT_H1/2 (hcp), OT_OVER/UNDER (total)
                    MaxBet keeps the lines in params (handicapOvertime[N], overUnderOvertime[N]), unsigned
  AdmiralBet        /betsAndGroups/2/123/3060/<event>        "Pobednik", "Hendikep (+OT)", "Ukupno (+OT)"
  MozzartBet        /betting/match/<id> (ids from the "Igrači" offer)
  Superbet          /events/<id>                             "Pobednik / Hendikep poena / Ukupno poena (uklj. produžetke)"
  BalkanBet         NSoft /events/<id>                       marketId 60 winner, 524 handicap, 530 total
"""

import datetime as dt
import json
import re
import time

from . import admiralbet, balkanbet, dualsoft, superbet
from .common import DELAY_S, local_from_ms, local_from_utc, session, to_float

LEAGUE = "EuroLeague"

# Per-league ids. NBA games tip off after midnight Belgrade time, so "tonight" runs until noon tomorrow.
# Dualsoft/Admiral/Mozzart pick the NBA by name (preseason and regular season are separate leagues there);
# Superbet and BalkanBet use one tournament id for the whole NBA season.
LEAGUES = {
    "EuroLeague": {"window": "today", "superbet": superbet.TOURNAMENT, "balkanbet": balkanbet.GAMES_TOURNAMENT,
                   "mozzart": ("evrolig",)},
    "NBA": {"window": "night", "superbet": 164, "balkanbet": 234, "mozzart": ("nba",)},   # regular season (preseason: 2176 / 27380)
}
_league = {"name": LEAGUE}


def _nba_name(name):
    n = (name or "").upper()
    return "NBA" in n and "WNBA" not in n and "SPECIJAL" not in n and "G LIG" not in n


def _today(kickoff):
    now = dt.datetime.now()
    if LEAGUES[_league["name"]]["window"] == "night":
        end = dt.datetime.combine(now.date() + dt.timedelta(days=1), dt.time(12, 0))
        return now < kickoff < end
    return kickoff.date() == now.date() and kickoff > now


def _fmt(x):
    return f"{x:+.1f}".replace("+0.0", "0.0") if x else "0.0"


def _row(book, home, away, kickoff, market, sel, odds, ot=True):
    odds = to_float(odds)
    if not odds or odds <= 1.0:
        return None
    return {"book": book, "league": _league["name"], "home": " ".join(home.split()), "away": " ".join(away.split()),
            "kickoff": kickoff, "market": market, "sel": sel, "odds": odds, "ot": ot}


def _hcp(book, home, away, kickoff, line, sel, odds, ot=True):
    return _row(book, home, away, kickoff, f"Handicap {_fmt(line)}", sel, odds, ot)


def _total(book, home, away, kickoff, line, sel, odds, ot=True):
    return _row(book, home, away, kickoff, f"Total {line:.1f}", sel, odds, ot)

# -----------------------------------------------------------------------------
# Soccerbet / MaxBet
# -----------------------------------------------------------------------------

def scrape_dualsoft(book):
    cfg = dualsoft.BOOKS[book]
    suffix = "_" + cfg["pick_suffix"]
    s = session()
    params = {"annex": cfg["annex"], "desc": "true", "mobileVersion": "2.0.0", "locale": "sr"}

    def get(path):
        r = s.get(f"{cfg['base']}/{path}", params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    names = {k.rsplit("_", 1)[0]: v.get("tipTypeName") for k, v in get("ttg_lang")["betPickMap"].items()
             if k.endswith(suffix)}
    names_b = {k.rsplit("_", 1)[0]: v.get("tipTypeName") for k, v in get("ttg_lang")["betPickMap"].items()
               if k.endswith("_B")} if suffix != "_B" else {}
    rows = []
    for g in get("sport/B/mob")["esMatches"]:
        kickoff = local_from_ms(g["kickOffTime"])
        wanted = (_nba_name(g.get("leagueName")) if _league["name"] == "NBA" else g["leagueId"] == cfg["league"])
        if not wanted or not _today(kickoff):
            continue
        full = get(f"match/{g['id']}")
        time.sleep(DELAY_S)
        home, away = g["home"], g["away"]

        def tip(code):
            return names.get(str(code)) or names_b.get(str(code)) or ""
        if "betMap" in full:                                    # Soccerbet: lines in the keys
            for code, svs in full["betMap"].items():
                t = tip(code)
                for sv, o in svs.items():
                    if o.get("s") != "U" or not o.get("ov"):
                        continue
                    if t in ("FT_OT_1", "FT_OT_2"):
                        rows.append(_row(book, home, away, kickoff, "Winner", t[-1], o["ov"]))
                    elif t in ("OT_H1", "OT_H2") and (m := re.search(r"hcp=(-?[\d.]+)", sv)):
                        rows.append(_hcp(book, home, away, kickoff, float(m[1]), t[-1], o["ov"]))
                    elif t in ("OT_OVER", "OT_UNDER") and (m := re.search(r"total=([\d.]+)", sv)):
                        rows.append(_total(book, home, away, kickoff, float(m[1]), t[3:].title(), o["ov"]))
        else:                                                   # MaxBet: main + _2.._5 alt lines in params
            odds, prm = full.get("odds") or {}, full.get("params") or {}
            by_tip = {tip(c): v for c, v in odds.items()}
            fav_home = (by_tip.get("FT_OT_1") or 9) <= (by_tip.get("FT_OT_2") or 9)
            for t, v in by_tip.items():
                if not v:
                    continue
                if t in ("FT_OT_1", "FT_OT_2"):
                    rows.append(_row(book, home, away, kickoff, "Winner", t[-1], v))
                    continue
                m = re.fullmatch(r"OT_(H1|H2|OVER|UNDER)(?:_(\d))?", t)
                if not m:
                    continue
                n = m[2] or ""
                if m[1] in ("H1", "H2"):
                    line = to_float(prm.get(f"handicapOvertime{n}"))
                    if line is not None:                        # unsigned: the favourite gives the points
                        line = -abs(line) if fav_home else abs(line)
                        rows.append(_hcp(book, home, away, kickoff, line, m[1][-1], v))
                else:
                    line = to_float(prm.get(f"overUnderOvertime{n}"))
                    if line is not None:
                        rows.append(_total(book, home, away, kickoff, line, m[1].title(), v))
    return [r for r in rows if r]

# -----------------------------------------------------------------------------
# AdmiralBet
# -----------------------------------------------------------------------------

def scrape_admiral():
    s = session(Origin="https://admiralbet.rs", Referer="https://admiralbet.rs/", Language="sr-Latn", OfficeId="138")
    if _league["name"] == "NBA":
        now = dt.datetime.now()
        tree = s.get(f"{admiralbet.API}/webTree/null/true/true/true/{now:%Y-%m-%dT00:00:00.000}/"
                     f"{now + dt.timedelta(days=2):%Y-%m-%dT00:00:00.000}/false", timeout=30).json()
        comps = [{"sportId": 2, "regionId": c["regionId"], "competitionId": c["competitionId"]}
                 for sp in tree if sp.get("id") == 2 for reg in sp.get("regions", [])
                 for c in reg.get("competitions", []) if _nba_name(c.get("competitionName"))]
    else:
        comps = [admiralbet.GAMES]
    rows = []
    for g_ids in comps:
        rows += _admiral_comp(s, g_ids)
    return [r for r in rows if r]


def _admiral_comp(s, g_ids):
    rows = []
    for e in admiralbet.events(s, g_ids):
        home, sep, away = e["name"].partition(" - ")
        kickoff = local_from_utc(e["dateTime"])
        if not sep or not _today(kickoff):
            continue
        r = s.get(f"{admiralbet.API}/betsAndGroups/{g_ids['sportId']}/{g_ids['regionId']}/"
                  f"{g_ids['competitionId']}/{e['id']}", timeout=30)
        r.raise_for_status()
        time.sleep(DELAY_S)
        home, away = home.strip(), away.strip()
        for b in r.json().get("bets", []):
            kind = b["betTypeName"].strip()
            for o in b["betOutcomes"]:
                if not o.get("odd") or not o.get("isPlayable", True):
                    continue
                name = (o.get("name") or "").strip().lower()
                sbv = to_float(o.get("sbv") or b.get("sbv"))
                if kind == "Pobednik" and name in ("1", "2"):
                    rows.append(_row("admiralbet", home, away, kickoff, "Winner", name, o["odd"]))
                elif kind == "Hendikep (+OT)" and name in ("1", "2") and sbv is not None:
                    rows.append(_hcp("admiralbet", home, away, kickoff, sbv, name, o["odd"]))
                elif kind == "Ukupno (+OT)" and name in ("vise", "više", "manje") and sbv is not None:
                    rows.append(_total("admiralbet", home, away, kickoff, sbv,
                                       "Under" if name == "manje" else "Over", o["odd"]))
    return rows

# -----------------------------------------------------------------------------
# MozzartBet
# -----------------------------------------------------------------------------

def scrape_mozzart():
    from .mozzartbet import Curl
    c = Curl()
    offers = c.call("POST", "/betting/get-special-offer", {}).get("specialOffer", [])
    rows, ids = [], {}
    for name in ("Igrači", "Top Lige"):
        offer = next((o for o in offers if o.get("name") == name), None)
        if not offer:
            continue
        page = 0
        while True:
            batch = c.call("POST", "/betting/matches", {**offer["filter"], "sportIds": [2],
                                                        "currentPage": page}).get("items") or []
            for i in batch:
                cname = i["competition"]["name"].lower()
                if any(k in cname for k in LEAGUES[_league["name"]]["mozzart"]) and "wnba" not in cname:
                    ids[i["id"]] = i
            if len(batch) < offer["filter"].get("pageSize", 50) or page > 10:
                break
            page += 1
    for mid, i in ids.items():
        kickoff = local_from_ms(i["startTime"])
        if not _today(kickoff):
            continue
        m = c.call("POST", f"/betting/match/{mid}", {}).get("match") or {}
        time.sleep(DELAY_S)
        home, away = i["home"]["name"], i["visitor"]["name"]
        for g in m.get("oddsGroup") or []:
            for o in g.get("odds") or []:
                if o.get("oddStatus") != "ACTIVE" or not o.get("value"):
                    continue
                game, sub = o["game"]["name"], o["subgame"]["name"].strip().lower()
                sv = to_float(o.get("specialOddValue"))
                if game == "Pobednik meča sa ev. produžecima" and sub in ("1", "2"):
                    rows.append(_row("mozzartbet", home, away, kickoff, "Winner", sub, o["value"]))
                elif game == "Hendikep" and sub in ("1", "2") and sv is not None:
                    # both sides carry the home team's line
                    rows.append(_hcp("mozzartbet", home, away, kickoff, sv, sub, o["value"], ot=False))
                elif game == "Ukupno poena na meču" and sub in ("manje", "više", "vise") and sv is not None:
                    rows.append(_total("mozzartbet", home, away, kickoff, sv,
                                       "Under" if sub == "manje" else "Over", o["value"], ot=False))
    return [r for r in rows if r]

# -----------------------------------------------------------------------------
# Superbet
# -----------------------------------------------------------------------------

def scrape_superbet():
    s = session()
    today = dt.date.today()
    r = s.get(f"{superbet.API}/events/by-date", timeout=60, params={
        "currentStatus": "active", "offerState": "prematch",
        "startDate": f"{today} 00:00:00", "endDate": f"{today + dt.timedelta(days=1)} 12:00:00"})
    r.raise_for_status()
    rows = []
    for e in r.json()["data"]:
        kickoff = local_from_utc(e["utcDate"])
        if e.get("tournamentId") != LEAGUES[_league["name"]]["superbet"] or not _today(kickoff):
            continue
        home, _, away = e["matchName"].partition("·")
        d = s.get(f"{superbet.API}/events/{e['eventId']}", timeout=30).json()["data"]
        d = d[0] if isinstance(d, list) else d
        for o in d["odds"]:
            if o.get("status") != "active" or not o.get("price"):
                continue
            mk, sp, code = o["marketName"], o.get("specifiers") or {}, str(o.get("code"))
            if mk == "Pobednik (uklj. produžetke)" and code in ("1", "2"):
                rows.append(_row("superbet", home, away, kickoff, "Winner", code, o["price"]))
            elif mk == "Hendikep poena (uklj. produžetke)" and code in ("1", "2") and "hcp" in sp:
                rows.append(_hcp("superbet", home, away, kickoff, float(sp["hcp"]), code, o["price"]))
            elif mk == "Ukupno poena (uklj. produžetke)" and "total" in sp:
                name = o.get("name") or ""
                side = "Over" if name.startswith("Više") else "Under" if name.startswith("Manje") else None
                if side:
                    rows.append(_total("superbet", home, away, kickoff, float(sp["total"]), side, o["price"]))
    return [r for r in rows if r]

# -----------------------------------------------------------------------------
# BalkanBet
# -----------------------------------------------------------------------------

def scrape_balkan():
    s = session(Origin="https://sports-sm-web.7platform.net")
    base = {"companyUuid": balkanbet.COMPANY, "timezone": "Europe/Belgrade", "deliveryPlatformId": 3,
            "language": json.dumps({"default": "sr-Latn"}),
            "dataFormat": json.dumps({"default": "object", "events": "array", "outcomes": "array"})}

    def get(path, **params):
        r = s.get(f"{balkanbet.API}/{path}", params={**base, **params}, timeout=40)
        r.raise_for_status()
        return r.json()["data"]

    now = dt.datetime.now()
    window = {"filter[from]": now.strftime("%Y-%m-%dT00:00:00"),
              "filter[to]": (now + dt.timedelta(days=1)).strftime("%Y-%m-%dT12:00:00"),
              "filter[tournamentId]": LEAGUES[_league["name"]]["balkanbet"]}
    rows = []
    for e in get("events", **window)["events"]:
        kickoff = local_from_utc(e["startsAt"])
        if not _today(kickoff):
            continue
        home, _, away = e["name"].partition(" - ")
        full = get(f"events/{e['id']}")
        time.sleep(DELAY_S)
        for mk in (full.get("markets") or {}).values():
            mid, sv = mk.get("marketId"), to_float((mk.get("specialValues") or [None])[0])
            for o in mk.get("outcomes") or []:
                name, odd = (o.get("name") or "").strip(), o.get("odd")
                if mid == 60 and name in ("P1", "P2"):
                    rows.append(_row("balkanbet", home, away, kickoff, "Winner", name[1], odd))
                elif mid == 524 and sv is not None and name[:2] in ("H1", "H2"):
                    rows.append(_hcp("balkanbet", home, away, kickoff, sv, name[1], odd))
                elif mid == 530 and sv is not None and name.lower().startswith(("više", "manje")):
                    rows.append(_total("balkanbet", home, away, kickoff, sv,
                                       "Over" if name.lower().startswith("više") else "Under", odd))
    return [r for r in rows if r]


_BASE = {
    "soccerbet": lambda: scrape_dualsoft("soccerbet"),
    "maxbet": lambda: scrape_dualsoft("maxbet"),
    "admiralbet": scrape_admiral,
    "mozzartbet": scrape_mozzart,
    "superbet": scrape_superbet,
    "balkanbet": scrape_balkan,
}


def _for(league):
    def wrap(fn):
        def run():
            prev, _league["name"] = _league["name"], league
            try:
                return fn()
            finally:
                _league["name"] = prev
        return run
    return {book: wrap(fn) for book, fn in _BASE.items()}


SCRAPERS = _for("EuroLeague")
NBA_SCRAPERS = _for("NBA")
