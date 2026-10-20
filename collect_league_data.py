"""Standings, results and fixtures for the b3basketball.com NBA and EuroLeague sections.

    venv/Scripts/python.exe collect_league_data.py [--days 7]

Writes League/nba.json and League/euroleague.json (latest snapshot, overwritten each run), plus the EuroLeague
box scores of the last --days days to Box Scores/EuroLeague/<date>/<game code>.json for the site's Games tab.
Sources: ESPN public API (NBA standings + scoreboard, US Eastern dates/times) and the official EuroLeague live API
(api-live.euroleague.net v1: standings, results, schedules; times are Central European).
"""
import argparse
import datetime as dt
import json
import os
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(PROJECT_DIR, "League")
UA = {"User-Agent": "Mozilla/5.0 (b3basketball.com data collector)"}
ET_TZ, CET = ZoneInfo("America/New_York"), ZoneInfo("Europe/Belgrade")
ESPN = "https://site.api.espn.com/apis"
EL = "https://api-live.euroleague.net/v1"
ESPN_ABBR = {"GS": "GSW", "NY": "NYK", "SA": "SAS", "UTAH": "UTA", "WSH": "WAS", "NO": "NOP"}


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        return r.read()


def abbr(a):
    return ESPN_ABBR.get(a, a)


# ------------------------------------------------------------------ NBA

def nba_standings(season, season_type):
    d = json.loads(get(f"{ESPN}/v2/sports/basketball/nba/standings?season={season}&seasontype={season_type}"))
    confs = []
    for c in d["children"]:
        rows = []
        for e in c["standings"]["entries"]:
            st = {s["name"]: s.get("displayValue") for s in e["stats"]}
            rows.append({"team": abbr(e["team"]["abbreviation"]), "name": e["team"]["displayName"],
                         "w": int(float(st.get("wins") or 0)), "l": int(float(st.get("losses") or 0)),
                         "pct": st.get("winPercent"), "gb": st.get("gamesBehind"), "home": st.get("Home"),
                         "road": st.get("Road"), "l10": st.get("Last Ten Games"), "streak": st.get("streak"),
                         "diff": st.get("differential"), "seed": int(float(st.get("playoffSeed") or 0))})
        rows.sort(key=lambda r: (-(r["w"] / max(r["w"] + r["l"], 1)), -r["w"], r["l"], r["name"]))
        confs.append({"name": c["name"], "rows": rows})
    return confs


def nba_day(day):
    d = json.loads(get(f"{ESPN}/site/v2/sports/basketball/nba/scoreboard?dates={day:%Y%m%d}&limit=100"))
    games = []
    for ev in d.get("events", []):
        c = ev["competitions"][0]
        t = {x["homeAway"]: x for x in c["competitors"]}
        when = dt.datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).astimezone(ET_TZ)
        st = ev["status"]["type"]
        games.append({"id": ev["id"], "date": day.isoformat(), "time": when.strftime("%H:%M"),
                      "season_type": ev["season"]["type"], "state": st["state"], "detail": st.get("shortDetail", ""),
                      "home": abbr(t["home"]["team"]["abbreviation"]), "away": abbr(t["away"]["team"]["abbreviation"]),
                      "home_name": t["home"]["team"]["displayName"], "away_name": t["away"]["team"]["displayName"],
                      "home_score": int(t["home"]["score"]) if st["state"] != "pre" else None,
                      "away_score": int(t["away"]["score"]) if st["state"] != "pre" else None,
                      "venue": (c.get("venue") or {}).get("fullName", ""),
                      "tv": ", ".join(n for b in c.get("broadcasts", []) for n in b.get("names", []))})
    return games


def collect_nba(days):
    today = dt.datetime.now(ET_TZ).date()
    season = today.year + 1 if today.month >= 8 else today.year
    regular = nba_standings(season, 2)
    played = sum(r["w"] + r["l"] for c in regular for r in c["rows"])
    standings = {"kind": "regular", "label": f"{season - 1}-{str(season)[2:]} regular season", "conferences": regular}
    if played == 0:
        standings = {"kind": "preseason", "label": f"{season - 1}-{str(season)[2:]} preseason",
                     "conferences": nba_standings(season, 1), "regular_starts": None}
    results, fixtures = [], []
    for k in range(days, 0, -1):
        results += [g for g in nba_day(today - dt.timedelta(days=k)) if g["state"] == "post"]
    for k in range(0, days + 1):
        games = nba_day(today + dt.timedelta(days=k))
        results += [g for g in games if g["state"] == "post" and k == 0]
        fixtures += [g for g in games if g["state"] != "post"]
    if standings["kind"] == "preseason":
        first = next((g["date"] for g in fixtures if g["season_type"] == 2), None)
        if not first:                                   # opener beyond the window: look ahead up to 3 weeks
            for k in range(days + 1, 22):
                day = today + dt.timedelta(days=k)
                if any(g["season_type"] == 2 for g in nba_day(day)):
                    first = day.isoformat()
                    break
        standings["regular_starts"] = first
    return {"league": "nba", "updated": dt.datetime.now(ET_TZ).strftime("%Y-%m-%d %H:%M ET"),
            "standings": standings, "results": results, "fixtures": fixtures, "tz": "ET"}


# ------------------------------------------------------------------ EuroLeague

def el_xml(path):
    return ET.fromstring(get(f"{EL}/{path}"))


def el_when(date, time):
    return dt.datetime.strptime(f"{date} {time}", "%b %d, %Y %H:%M")


def collect_el(season_code):
    root = el_xml(f"standings?seasonCode={season_code}")
    grp = root.find("group")
    last_round = int(grp.get("gamenumber"))
    rows = [{"rank": int(t.findtext("ranking")), "code": t.findtext("code"), "name": t.findtext("name"),
             "gp": int(t.findtext("totalgames")), "w": int(t.findtext("wins")), "l": int(t.findtext("losses")),
             "pf": int(t.findtext("ptsfavour")), "pa": int(t.findtext("ptsagainst")),
             "diff": int(t.findtext("difference"))} for t in grp.findall("team")]
    names = {r["code"]: r["name"] for r in rows}
    results, fixtures = [], []
    for rnd in range(max(1, last_round - 1), last_round + 1):
        for g in el_xml(f"results?seasonCode={season_code}&gameNumber={rnd}").findall("game"):
            item = {"round": rnd, "code": g.findtext("gamecode"),
                    "when": el_when(g.findtext("date"), g.findtext("time")).isoformat(timespec="minutes"),
                    "home": g.findtext("homecode"), "away": g.findtext("awaycode"),
                    "home_name": names.get(g.findtext("homecode"), g.findtext("hometeam").title()),
                    "away_name": names.get(g.findtext("awaycode"), g.findtext("awayteam").title())}
            if g.findtext("played") == "true":
                results.append({**item, "home_score": int(g.findtext("homescore")), "away_score": int(g.findtext("awayscore"))})
    for rnd in (last_round, last_round + 1, last_round + 2):
        for g in el_xml(f"schedules?seasonCode={season_code}&gameNumber={rnd}").findall("item"):
            if g.findtext("played") == "true":
                continue
            fixtures.append({"round": rnd, "code": g.findtext("gamecode"),
                             "when": el_when(g.findtext("date"), g.findtext("startime")).isoformat(timespec="minutes"),
                             "home": g.findtext("homecode"), "away": g.findtext("awaycode"),
                             "home_name": names.get(g.findtext("homecode"), g.findtext("hometeam").title()),
                             "away_name": names.get(g.findtext("awaycode"), g.findtext("awayteam").title()),
                             "arena": (g.findtext("arenaname") or "").title(),
                             "confirmed": g.findtext("confirmedtime") == "true"})
    results.sort(key=lambda g: g["when"], reverse=True)
    fixtures.sort(key=lambda g: g["when"])
    return {"league": "euroleague", "updated": dt.datetime.now(CET).strftime("%Y-%m-%d %H:%M CET"),
            "season": season_code, "last_round": last_round, "standings": rows, "results": results,
            "fixtures": fixtures, "tz": "CET"}


def collect_el_boxes(season_code, days):
    """Official box score of every played game in the last `days` days -> Box Scores/EuroLeague/<date>/<code>.json
    ({"game": v2 game record, "box": live Boxscore}). Already saved games are skipped."""
    games = json.loads(get(f"https://api-live.euroleague.net/v2/competitions/E/seasons/{season_code}/games"))["data"]
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    saved = 0
    for g in games:
        day = str(g.get("date", ""))[:10]
        if not g.get("played") or day < since:
            continue
        path = os.path.join(PROJECT_DIR, "Box Scores", "EuroLeague", day, f"{g['gameCode']}.json")
        if os.path.exists(path):
            continue
        box = json.loads(get(f"https://live.euroleague.net/api/Boxscore?gamecode={g['gameCode']}&seasoncode={season_code}"))
        if box.get("Live") or not box.get("Stats"):
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"game": g, "box": box}, f, ensure_ascii=False)
        saved += 1
    for g in games:                                   # club crests for the Games tab (logos/el/<code>.png)
        for side in ("local", "road"):
            club = g[side]["club"]
            crest = (club.get("images") or {}).get("crest")
            path = os.path.join(PROJECT_DIR, "logos", "el", f"{club['code']}.png")
            if crest and not os.path.exists(path):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                try:
                    with open(path, "wb") as f:
                        f.write(get(crest))
                except OSError as e:
                    print(f"  crest {club['code']}: {e}")
    return saved


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)
    today = dt.date.today()
    el_season = f"E{today.year if today.month >= 8 else today.year - 1}"
    for name, fn in (("nba", lambda: collect_nba(args.days)), ("euroleague", lambda: collect_el(el_season))):
        try:
            data = fn()
        except Exception as e:  # noqa: BLE001 — keep the previous snapshot if a source is down
            print(f"{name}: FAILED ({e}); previous snapshot kept")
            continue
        with open(os.path.join(OUT_DIR, f"{name}.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"{name}: {len(data['results'])} results, {len(data['fixtures'])} fixtures, updated {data['updated']}")
    try:
        print(f"euroleague box scores: {collect_el_boxes(el_season, args.days)} new")
    except Exception as e:  # noqa: BLE001 — the Games tab keeps what is already saved
        print(f"euroleague box scores: FAILED ({e})")


if __name__ == "__main__":
    main()
