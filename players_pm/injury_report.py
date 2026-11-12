#!/usr/bin/env python3
"""
Official NBA injury report -> props.nba_injury_report (insert-only snapshots).

Source: https://official.nba.com/nba-injury-report-2026-27-season/ lists a PDF for every 15-minute
update (ak-static.cms.nba.com/referee/injury/Injury-Report_YYYY-MM-DD_HH_MMAM.pdf). Teams must file
statuses (Out / Doubtful / Questionable / Probable / Available) by 5 PM ET the day before a game and
update them through the day, so this is the confirmed availability source; ESPN injuries stay as the
second source (longer-term injury notes, expected returns).

The PDF is parsed from word positions: fixed columns (Game Date, Game Time, Matchup, Team, Player Name,
Current Status, Reason); date/time/matchup/team are printed once per block and carried down; a reason that
wraps onto 2 lines is printed above and below its row and is joined back. Player names ('Last, First')
are matched to ESPN athlete ids among that team's players in our box scores / ESPN roster, else NULL
(never guessed). Teams that haven't filed yet appear as one 'Not Yet Submitted' row.

Usage:
  python -m players_pm.injury_report               # newest report (skipped if already stored)
  python -m players_pm.injury_report --backfill    # also the last report of every earlier day this season
  python -m players_pm.injury_report --url <pdf>   # one specific report
  python -m players_pm.injury_report --history     # for every final game in the DB: the last report before
                                                   # tip-off (older PDFs stay online, only the index is daily)
"""

import datetime as dt
import io
import re
import sys

import pdfplumber
from sqlalchemy import text

from .common import _session, engine, ensure_schema, job, name_key

INDEX_URL = "https://official.nba.com/nba-injury-report-2026-27-season/"
PDF_RE = re.compile(r"https://ak-static\.cms\.nba\.com/referee/injury/Injury-Report_(\d{4}-\d{2}-\d{2})_(\d{2})(?:_(\d{2}))?(AM|PM)\.pdf")
COLS = ["date", "time", "matchup", "team", "player", "status", "reason"]
HEAD = {"Date": "date", "Time": "time", "Matchup": "matchup", "Team": "team", "Player": "player",
        "Current": "status", "Reason": "reason"}
STATUSES = {"Out", "Doubtful", "Questionable", "Probable", "Available"}


def report_links():
    r = _session.get(INDEX_URL, timeout=30)
    r.raise_for_status()
    out = {}
    for m in PDF_RE.finditer(r.text):
        d, hh, mm, ap = m.group(1), int(m.group(2)), int(m.group(3) or 0), m.group(4)
        hh = hh % 12 + (12 if ap == "PM" else 0)
        out[m.group(0)] = dt.datetime.fromisoformat(d) + dt.timedelta(hours=hh, minutes=mm)
    return sorted(out.items(), key=lambda kv: kv[1])


def fetch_pdf(url):
    r = _session.get(url, timeout=60, headers={"Referer": INDEX_URL})
    r.raise_for_status()
    if not r.content.startswith(b"%PDF"):
        raise ValueError(f"not a PDF: {url}")
    return r.content


# ------------------------------------------------------------------------------------------ parse
def page_lines(page):
    words = page.extract_words(x_tolerance=1.5, keep_blank_chars=False)
    head = [w for w in words if w["text"] in ("Date", "Time", "Matchup", "Team", "Player", "Current", "Reason")]
    starts = {}
    for w in head:
        x = w["x0"]
        if w["text"] in ("Date", "Time"):      # 'Game Date' / 'Game Time': the column starts at 'Game'
            prev = [v for v in words if abs(v["top"] - w["top"]) < 3 and v["text"] == "Game" and v["x0"] < x]
            x = max((v["x0"] for v in prev), default=x)
        starts.setdefault(HEAD[w["text"]], x)
    if len(starts) < 7:            # pages without a header row reuse the standard layout
        starts = {"date": 23, "time": 120, "matchup": 200, "team": 264, "player": 425, "status": 586, "reason": 666}
    title_top = min((w["top"] for w in words if w["text"] == "Injury"), default=0)
    head_top = max(min((w["top"] for w in head), default=0), title_top)    # skip the title band too
    bounds = sorted(starts.items(), key=lambda kv: kv[1])
    lines = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        if w["top"] <= head_top + 5 or w["text"] in ("Page",) or re.fullmatch(r"\d+|of", w["text"]) and w["top"] > page.height - 80:
            continue
        if w["top"] > page.height - 70:
            continue
        col = next((c for c, x in reversed(bounds) if w["x0"] >= x - 3), "date")
        if lines and abs(lines[-1]["top"] - w["top"]) < 3:
            ln = lines[-1]
        else:
            ln = {"top": w["top"], **{c: [] for c in COLS}}
            lines.append(ln)
        ln[col].append(w["text"])
    for ln in lines:
        for c in COLS:
            ln[c] = " ".join(ln[c])
        ln["reason"] = re.sub(r"\s*-\s*", "-", ln["reason"]).replace("G League", "G League")
    return lines


def parse_pdf(content):
    rows = []
    carry = {"date": None, "time": None, "matchup": None, "team": None}
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        title = (pdf.pages[0].extract_text() or "").split("\n")[0]
        for page in pdf.pages:
            lines = page_lines(page)
            anchors, frags = [], []
            for ln in lines:
                for c in ("date", "time", "matchup", "team"):
                    if ln[c]:
                        carry[c] = ln[c]
                        if c == "matchup":
                            carry["team"] = ln["team"] or None
                        if c in ("date", "time"):
                            pass
                if ln["player"] or ln["status"] or "NOT YET SUBMITTED" in ln["reason"]:
                    anchors.append({**ln, **{c: carry[c] for c in carry}})
                elif ln["reason"]:
                    frags.append(ln)
            # wrapped reasons: attach to the nearest anchor that has no reason on its own line
            for fr in frags:
                cand = [a for a in anchors if not a["reason"] or a.get("_wrapped")]
                if not cand:
                    continue
                a = min(cand, key=lambda a: abs(a["top"] - fr["top"]))
                if abs(a["top"] - fr["top"]) > 20:
                    continue
                a.setdefault("_parts", []).append((fr["top"], fr["reason"]))
                a["_wrapped"] = True
            for a in anchors:
                if a.get("_parts"):
                    parts = sorted(a["_parts"] + ([(a["top"], a["reason"])] if a["reason"] and not a.get("_wrapped_own") else []))
                    a["reason"] = " ".join(p for _, p in parts if p)
                rows.append(a)
    out = []
    for a in rows:
        if "NOT YET SUBMITTED" in (a["reason"] or "") and not a["player"]:
            status, player, reason = "Not Yet Submitted", "(team report not submitted)", None
        else:
            status, player, reason = a["status"], a["player"], a["reason"] or None
        if not a["team"] or not a["date"] or not player:
            continue
        out.append({"game_date": dt.datetime.strptime(a["date"], "%m/%d/%Y").date(), "game_time": a["time"],
                    "matchup": a["matchup"], "team_name": a["team"], "player_raw": player, "status": status,
                    "reason": reason})
    return title, out


# ------------------------------------------------------------------------------------------ matching
def lookups(conn):
    teams = {name_key(n): c for c, n in conn.execute(text("SELECT code, name FROM props.teams WHERE league='nba'"))}
    teams.update({k: v for k, v in conn.execute(text("SELECT alias, code FROM props.team_aliases WHERE league='nba'"))})
    teams[name_key("LA Clippers")] = "LAC"
    teams[name_key("Los Angeles Clippers")] = "LAC"
    teams[name_key("Los Angeles Lakers")] = "LAL"
    by_team = {}
    for pid, name, team in conn.execute(text("""
            SELECT DISTINCT b.player_id, b.player_name, b.team FROM public.nba_boxscores b
            WHERE b.game_date >= CURRENT_DATE - 400
            UNION SELECT p.player_id, p.name, p.team_code FROM props.players p WHERE p.league='nba'""")):
        by_team.setdefault(team, {}).setdefault(name_key(name), set()).add(pid)
    return teams, by_team


def player_name(raw):
    """'Cenac Jr., Christopher' -> 'Christopher Cenac Jr.'"""
    if "," in raw:
        last, first = raw.split(",", 1)
        return f"{first.strip()} {last.strip()}".strip()
    return raw.strip()


def match(by_team, team, name):
    k = name_key(name)
    ids = (by_team.get(team) or {}).get(k)
    if ids and len(ids) == 1:
        return next(iter(ids))
    allids = set().union(*[d.get(k, set()) for d in by_team.values()])
    return next(iter(allids)) if len(allids) == 1 else None


# ------------------------------------------------------------------------------------------ store
def store(eng, url, ts, content):
    title, rows = parse_pdf(content)
    m = re.search(r"(\d{2}/\d{2}/\d{2})\s+(\d{1,2}:\d{2})\s*(AM|PM)", title or "")
    if m:                                     # the printed report time wins over the file name
        ts = dt.datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%m/%d/%y %I:%M %p")
    with eng.begin() as conn:
        teams, by_team = lookups(conn)
        recs, unmatched_team = [], set()
        for r in rows:
            code = teams.get(name_key(r["team_name"]))
            if not code:
                unmatched_team.add(r["team_name"])
                continue
            name = player_name(r["player_raw"]) if r["status"] != "Not Yet Submitted" else r["player_raw"]
            pid = match(by_team, code, name) if r["status"] != "Not Yet Submitted" else None
            recs.append({"ts": ts, "gd": r["game_date"], "gt": r["game_time"], "mu": r["matchup"], "team": code,
                         "name": name, "pid": pid, "status": r["status"], "reason": r["reason"], "url": url})
        if recs:
            conn.execute(text("""INSERT INTO props.nba_injury_report (report_ts, game_date, game_time, matchup, team,
                                 player_name, player_id, status, reason, source_url)
                                 VALUES (:ts, :gd, :gt, :mu, :team, :name, :pid, :status, :reason, :url)
                                 ON CONFLICT DO NOTHING"""), recs)
    bad_status = sorted({r["status"] for r in recs if r["status"] not in STATUSES | {"Not Yet Submitted"}})
    return {"report": ts.isoformat(), "rows": len(recs), "unmatched_players": sum(r["pid"] is None and
            r["status"] != "Not Yet Submitted" for r in recs), "unmatched_teams": sorted(unmatched_team),
            "odd_statuses": bad_status, "title": title}


PDF_BASE = "https://ak-static.cms.nba.com/referee/injury/Injury-Report_"


def url_for(ts, style):
    """'quarter' = current naming (…_06_30PM.pdf, every 15 min); 'hour' = early 2025-26 naming (…_06PM.pdf)"""
    h12 = ts.hour % 12 or 12
    ap = "AM" if ts.hour < 12 else "PM"
    if style == "hour":
        return f"{PDF_BASE}{ts:%Y-%m-%d}_{h12:02d}{ap}.pdf"
    return f"{PDF_BASE}{ts:%Y-%m-%d}_{h12:02d}_{ts.minute:02d}{ap}.pdf"


def history_targets(eng):
    """for every final game: the last report before tip-off (ET), probed on the 30-min / hourly grid"""
    with eng.connect() as conn:
        tips = conn.execute(text("""
            SELECT DISTINCT (g.start_time AT TIME ZONE 'America/New_York') AS tip   -- timestamptz -> ET wall clock
            FROM public.nba_games g
            WHERE g.status = 'STATUS_FINAL' AND g.season_type <> 'preseason' AND g.start_time IS NOT NULL
              AND EXISTS (SELECT 1 FROM props.teams t WHERE t.league='nba' AND t.code=g.home_team)""")).scalars().all()
    want = set()
    for tip in tips:
        t = tip - dt.timedelta(minutes=30)
        want.add(t.replace(minute=0 if t.minute < 30 else 30, second=0, microsecond=0))
    return sorted(want)


def fetch_history(eng, have):
    """probe each target time: quarter-hour naming, then hourly naming, then 30/60 min earlier"""
    results = []
    for ts in history_targets(eng):
        tries = [(ts, "quarter"), (ts.replace(minute=0), "hour"),
                 (ts - dt.timedelta(minutes=30), "quarter"), (ts.replace(minute=0) - dt.timedelta(hours=1), "hour")]
        for t, style in tries:
            url = url_for(t, style)
            if url in have:
                break
            r = _session.get(url, timeout=60, headers={"Referer": INDEX_URL})
            if r.status_code == 200 and r.content.startswith(b"%PDF"):
                try:
                    results.append(store(eng, url, t, r.content))
                except Exception as e:                     # an odd old layout must not stop the backfill
                    results.append({"report": t.isoformat(), "rows": 0, "error": f"{type(e).__name__}: {e}"})
                have.add(url)
                print(results[-1], flush=True)
                break
        else:
            results.append({"report": ts.isoformat(), "rows": 0, "error": "no report found"})
            print(results[-1], flush=True)
    return results


def stored(eng):
    with eng.connect() as conn:
        return {r[0] for r in conn.execute(text("SELECT DISTINCT source_url FROM props.nba_injury_report"))}


def main():
    eng = engine()
    ensure_schema(eng)
    with job(eng, "nba_injury_report", "nba") as j:
        if "--url" in sys.argv:
            url = sys.argv[sys.argv.index("--url") + 1]
            m = PDF_RE.fullmatch(url)
            hh = int(m.group(2)) % 12 + (12 if m.group(4) == "PM" else 0)
            todo = [(url, dt.datetime.fromisoformat(m.group(1)) + dt.timedelta(hours=hh, minutes=int(m.group(3) or 0)))]
        else:
            links = report_links()
            if not links:
                raise RuntimeError("no report links found on the index page")
            todo = [links[-1]]
            if "--backfill" in sys.argv:
                last_per_day = {}
                for url, ts in links:
                    last_per_day[ts.date()] = (url, ts)
                todo = sorted(set(last_per_day.values()) | set(todo), key=lambda x: x[1])
        have = stored(eng)
        results = fetch_history(eng, have) if "--history" in sys.argv else []
        for url, ts in todo:
            if url in have:
                continue
            results.append(store(eng, url, ts, fetch_pdf(url)))
            print(results[-1], flush=True)
        j["rows"] = sum(r["rows"] for r in results)
        j["message"] = f"{len(results)} report(s) stored" + (f", newest {results[-1]['report']}" if results else ", nothing new")
        print(j["message"])


if __name__ == "__main__":
    main()
