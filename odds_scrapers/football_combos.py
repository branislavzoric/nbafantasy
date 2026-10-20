"""
Football combo bets ("1X&I1-3&II1-3", "GG&3+", ...) for today's top-league matches at the six books.
One full-offer call per match per book. Every scraper returns rows
{"book", "league", "home", "away", "kickoff", "text", "ctx", "odds"}: text = the book's selection in the
compact Serbian notation (Superbet's spelled-out markets are converted to it), ctx = market name / long
label, used by combo_model.parse to tell whose goals a selection counts and half-time/full-time from
goal ranges.

Endpoints:
  Soccerbet/MaxBet  /restapi/offer/sr/match/<id> (bet codes) + /ttg_lang (code -> caption, label)
  AdmiralBet        /api/offer/betsAndGroups/1/<region>/<competition>/<event>
  MozzartBet        POST /betting/match/<id>
  Superbet          /events/<id>
  BalkanBet         NSoft /events/<id>
"""

import datetime as dt
import json
import math
import re
import time

from .common import DELAY_S, local_from_ms, local_from_utc, session, to_float
from .football import (ADMIRAL_API, ADMIRAL_LEAGUES, BALKAN_LEAGUES, DUALSOFT, MOZZART_LEAGUES, NSOFT_API,
                       NSOFT_COMPANY, SUPERBET_API, SUPERBET_LEAGUES, _today)


def _row(book, league, home, away, kickoff, text, ctx, odds):
    odds = to_float(odds)
    if not text or not odds or odds <= 1.0:
        return None
    return {"book": book, "league": league, "home": " ".join(home.split()), "away": " ".join(away.split()),
            "kickoff": kickoff, "text": text.strip(), "ctx": ctx or "", "odds": odds}


def _is_combo(text):
    return bool(text) and ("&" in text or text.strip().upper().startswith("NE"))

# -----------------------------------------------------------------------------
# Soccerbet / MaxBet
# -----------------------------------------------------------------------------

def scrape_dualsoft(book, all_leagues=False):
    cfg = DUALSOFT[book]
    s = session()
    q = {"annex": cfg["annex"], "desc": "true", "mobileVersion": "2.0.0", "locale": "sr"}
    picks = s.get(f"{cfg['base']}/ttg_lang", params=q, timeout=60).json()["betPickMap"]
    lst = s.get(f"{cfg['base']}/sport/S/mob", params=q, timeout=60).json()["esMatches"]
    rows = []
    for m in lst:
        league = cfg["leagues"].get(m["leagueId"]) or (m.get("leagueName") if all_leagues else None)
        kickoff = local_from_ms(m["kickOffTime"])
        if not league or not _today(kickoff) or m.get("blocked"):
            continue
        d = s.get(f"{cfg['base']}/match/{m['id']}", params=q, timeout=60).json()
        time.sleep(DELAY_S)
        if "betMap" in d:
            prices = {c: v.get("NULL", {}).get("ov") for c, v in d["betMap"].items() if v.get("NULL", {}).get("s", "U") == "U"}
        else:
            prices = d.get("odds") or {}
        for code, odds in prices.items():
            p = picks.get(f"{code}_S") or {}
            cap = p.get("caption") or ""
            if _is_combo(cap):
                rows.append(_row(book, league, m["home"], m["away"], kickoff, cap,
                                 f"{p.get('tipTypeName')} | {p.get('label') or ''}", odds))
    return [r for r in rows if r]

# -----------------------------------------------------------------------------
# AdmiralBet
# -----------------------------------------------------------------------------

def scrape_admiral():
    s = session(Origin="https://admiralbet.rs", Referer="https://admiralbet.rs/", Language="sr-Latn", OfficeId="138")
    rows = []
    for (region, comp), league in ADMIRAL_LEAGUES.items():
        evs = s.get(f"{ADMIRAL_API}/getWebEventsSelections", timeout=90, params={
            "sportId": 1, "pageId": 35, "isLive": "false", "regionId": region, "competitionId": comp,
            "dateFrom": "2020-01-01T00:00:00.000", "dateTo": "2031-01-01T00:00:00.000"}).json()
        time.sleep(DELAY_S)
        for e in evs:
            home, sep, away = e["name"].partition(" - ")
            kickoff = local_from_utc(e["dateTime"])
            if e.get("isLive") or not sep or e.get("eventTypeId") != 1 or not _today(kickoff):
                continue
            d = s.get(f"{ADMIRAL_API}/betsAndGroups/1/{region}/{comp}/{e['id']}", timeout=90).json()
            time.sleep(DELAY_S)
            for b in d.get("bets") or []:
                for o in b.get("betOutcomes") or []:
                    if o.get("isPlayable", True) and _is_combo(o.get("name")):
                        rows.append(_row("admiralbet", league, home, away, kickoff, o["name"], b.get("betTypeName"),
                                         o.get("odd")))
    return [r for r in rows if r]

# -----------------------------------------------------------------------------
# MozzartBet
# -----------------------------------------------------------------------------

def scrape_mozzart():
    from .mozzartbet import Curl
    c = Curl()
    offer = next((o for o in c.call("POST", "/betting/get-special-offer", {}).get("specialOffer", [])
                  if o.get("name") == "Top Lige"), None)
    if not offer:
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
        league = MOZZART_LEAGUES.get(i["competition"]["name"].strip())
        kickoff = local_from_ms(i["startTime"])
        if not league or not _today(kickoff) or i.get("sport", {}).get("id", 1) != 1:
            continue
        m = c.call("POST", f"/betting/match/{i['id']}", {}).get("match") or {}
        time.sleep(DELAY_S)
        for g in m.get("oddsGroup") or []:
            for o in g.get("odds") or []:
                name = o["subgame"]["name"]
                if o.get("oddStatus") == "ACTIVE" and _is_combo(name):
                    rows.append(_row("mozzartbet", league, i["home"]["name"], i["visitor"]["name"], kickoff, name,
                                     f"{o['game']['name']} | {o['subgame'].get('description') or ''}", o.get("value")))
    return [r for r in rows if r]

# -----------------------------------------------------------------------------
# Superbet: spelled-out markets -> compact notation
# -----------------------------------------------------------------------------

def _ou(sel):
    m = re.match(r"(?i)\s*(više|manje)(?:\s+od)?\s+(\d+(?:\.\d+)?)", sel)
    if not m:
        return None
    n = math.floor(float(m[2]))
    return f"{n + 1}+" if m[1].lower() == "više" else f"0-{n}"


def superbet_text(market, name, home, away):
    """'1. poluvreme ukupno golova & Ukupno golova', 'Više 0.5 & Manje 2.5' -> 'I1+&0-2' (None if unknown)."""
    mk = market.strip()
    m = re.fullmatch(r"(?i)oba tima daju gol & (\d)\+", mk)
    if m:
        return {"Da": f"GG&{m[1]}+", "Ne": f"NE GG&{m[1]}+"}.get(name.strip())
    line = re.search(r"\((\d+(?:\.\d+)?)\)\s*$", mk)
    mk = re.sub(r"\s*\(\d+(?:\.\d+)?\)\s*$", "", mk)
    whole = ""                                                    # "Freiburg - 1. poluvreme ... & 2. poluvreme ..."
    for t, code in ((home, "T1:"), (away, "T2:")):
        if mk.lower().startswith(t.lower() + " - "):
            whole, mk = code, mk[len(t) + 3:]
    parts, sels = [p.strip() for p in mk.split(" & ")], [x.strip() for x in name.split(" & ")]
    if len(parts) != len(sels):
        return None
    out = []
    for p, sel in zip(parts, sels):
        pl = p.lower()
        team = whole or ("T1:" if home.lower() in pl else "T2:" if away.lower() in pl else "")
        half = "II" if "2. poluvreme" in pl else "I" if "1. poluvreme" in pl else ""
        if pl in ("1x2", "konačan ishod") and sel in ("1", "X", "2"):
            out.append(sel)
        elif pl == "dupla šansa" and sel in ("1X", "12", "X2"):
            out.append(sel)
        elif pl == "1. gol" and sel in ("1", "2"):
            out.append(f"PG{sel}")
        elif pl == "poluvreme/kraj" and re.fullmatch(r"[12X]/[12X]", sel):
            out.append("HF:" + sel.replace("/", "-"))
        elif pl == "oba tima daju gol" and sel in ("Da", "Ne"):
            out.append("GG" if sel == "Da" else "NG")
        elif pl == "gol u oba poluvremena" and sel == "Da":
            out.append("I1+&II1+")
        elif pl == "raspon golova" and re.fullmatch(r"\d+-\d+|\d+\+", sel):
            out.append(sel)
        elif "ukupno golova" in pl:
            ou = _ou(sel if re.search(r"\d", sel) or not line else f"{sel} {line[1]}")
            if not ou:
                return None
            out.append(f"{team}{half}{ou}")
        else:
            return None
    return "&".join(out)


def scrape_superbet():
    s = session()
    today = dt.date.today()
    lst = s.get(f"{SUPERBET_API}/events/by-date", timeout=60, params={
        "currentStatus": "active", "offerState": "prematch",
        "startDate": f"{today - dt.timedelta(days=1)} 22:00:00", "endDate": f"{today + dt.timedelta(days=1)} 00:00:00"}).json()["data"]
    rows = []
    for e in lst:
        league = SUPERBET_LEAGUES.get(e.get("tournamentId"))
        kickoff = local_from_utc(e["utcDate"])
        if e.get("sportId") != 5 or not league or not _today(kickoff):
            continue
        data = s.get(f"{SUPERBET_API}/events/{e['eventId']}", timeout=60).json()["data"]
        time.sleep(DELAY_S)
        data = data[0] if isinstance(data, list) else data
        home, _, away = e["matchName"].partition("·")
        for o in data.get("odds") or []:
            mk = o.get("marketName") or ""
            if o.get("status") != "active" or "&" not in mk:
                continue
            text = superbet_text(mk, o.get("name") or "", home.strip(), away.strip())
            if text:
                rows.append(_row("superbet", league, home, away, kickoff, text, mk, o.get("price")))
    return [r for r in rows if r]

# -----------------------------------------------------------------------------
# BalkanBet
# -----------------------------------------------------------------------------

def scrape_balkan():
    s = session(Origin="https://sports-sm-web.7platform.net")
    base = {"companyUuid": NSOFT_COMPANY, "timezone": "Europe/Belgrade", "deliveryPlatformId": 3,
            "language": json.dumps({"default": "sr-Latn"}),
            "dataFormat": json.dumps({"default": "object", "events": "array", "outcomes": "array"})}
    day = {"filter[from]": dt.datetime.now().strftime("%Y-%m-%dT00:00:00"),
           "filter[to]": dt.datetime.now().strftime("%Y-%m-%dT23:59:59")}
    rows = []
    for tid, league in BALKAN_LEAGUES.items():
        evs = s.get(f"{NSOFT_API}/events", params={**base, **day, "filter[tournamentId]": tid}, timeout=120).json()["data"]["events"]
        time.sleep(DELAY_S)
        for e in evs:
            home, sep, away = e["name"].partition(" - ")
            kickoff = local_from_utc(e["startsAt"])
            if not sep or not _today(kickoff):
                continue
            full = s.get(f"{NSOFT_API}/events/{e['id']}", params=base, timeout=120).json()["data"]
            time.sleep(DELAY_S)
            for mk in (full.get("markets") or {}).values():
                if not mk.get("active", 1):
                    continue
                for o in mk.get("outcomes") or []:
                    if o.get("active", 1) and _is_combo(o.get("name")):
                        rows.append(_row("balkanbet", league, home, away, kickoff, o["name"], mk.get("name"), o.get("odd")))
    return [r for r in rows if r]


SCRAPERS = {"soccerbet": lambda: scrape_dualsoft("soccerbet"), "maxbet": lambda: scrape_dualsoft("maxbet"),
            "admiralbet": scrape_admiral, "mozzartbet": scrape_mozzart, "superbet": scrape_superbet,
            "balkanbet": scrape_balkan}
# lower leagues too: Soccerbet + MaxBet only (the other books send several MB per match)
SCRAPERS_ALL = {"soccerbet": lambda: scrape_dualsoft("soccerbet", True), "maxbet": lambda: scrape_dualsoft("maxbet", True)}
