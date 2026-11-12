#!/usr/bin/env python3
"""
EuroLeague injuries -> props.injuries (league 'el', one snapshot per run).

The EuroLeague has no open injury feed (euroleaguebasketball.net sits behind a Vercel bot check), so
this reads two public injury pages that follow the official round reports and club news:
  1. basketballsphere.com/en/injuries/euroleague/  - JSON-LD ItemList "Team, Status, Injury, Return";
     curated status column: Out / Doubtful / Day to day / Questionable / Cleared
  2. basketballstats.eu/en/injury-report/          - per-team sections, "Injured: ..." / "Doubtful: ..." /
     "Out: Club decision" with an "upd." date and often an "Expected back:" date
Merge: basketballsphere's status wins when it lists the player (Cleared removes him); basketballstats.eu adds
the players it doesn't list. "Has missed his team's last N games" is ignored (inferred from box scores, the
minutes model already sees it). An "Injured" note whose exact expected-back date has passed becomes
Questionable. The manual file wins over both (late game-day news):
  "Players PM/el_injuries_manual.json": [{"player": "Johnathan Motley", "team": "RED", "status": "Out",
                                         "detail": "virus", "until": "2026-11-12"}]   (until = last day it applies)
Names are matched to EuroLeague player ids among players with box scores in this or last season (exact
name, else unique surname + first initial, team used as tie-break); unmatched names are logged, never guessed.
If both pages fail, nothing is written and the previous EL snapshot stays in use (model.injuries_now
keeps the latest snapshot per league for 36 hours).

Usage:  python -m players_pm.el_injuries [--dry-run]
"""

import datetime as dt
import json
import os
import re
import sys
from html import unescape

from sqlalchemy import text

from .common import PM_DIR, _session, engine, fold, job

SPHERE = "https://basketballsphere.com/en/injuries/euroleague/"
BSTATS = "https://basketballstats.eu/en/injury-report/"
MANUAL = os.path.join(PM_DIR, "el_injuries_manual.json")
SPHERE_STATUS = {"out": "Out", "doubtful": "Doubtful", "day to day": "Day-To-Day", "questionable": "Questionable",
                 "probable": "Probable", "cleared": "Cleared", "suspended": "Suspension"}
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], 1)}
# words that don't identify a club ("Basket", sponsor names are absent from both pages anyway)
TEAM_STOP = {"basket", "basketball", "bc", "fc", "kk", "as", "the", "ldlc", "rapyd", "ibi", "aktor", "armani",
             "mozzart", "bet", "meridianbet", "tarfin", "kosner", "vitoria", "gasteiz", "piraeus", "athens",
             "istanbul", "belgrade", "kaunas", "villeurbanne"}
TEAM_ALIAS = {"milano": "milan", "olimpia": "milan", "efes": "anadolu", "asvel": "asvel", "fenerbahce": "fenerbahce",
              "valencia": "valencia", "virtus": "virtus", "bayern": "bayern"}


def get_text(url):
    r = _session.get(url, timeout=30)
    r.raise_for_status()
    if "Security Checkpoint" in r.text[:3000]:
        raise RuntimeError(f"bot check at {url}")
    return r.text


def team_words(name):
    return {TEAM_ALIAS.get(w, w) for w in fold(name) if w not in TEAM_STOP}


def parse_sphere(html):
    out = []
    for block in re.findall(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S):
        try:
            g = json.loads(block)
        except json.JSONDecodeError:
            continue
        for node in g.get("@graph", [g]) if isinstance(g, dict) else g:
            if not isinstance(node, dict) or node.get("@type") != "ItemList":
                continue
            for it in node.get("itemListElement", []):
                parts = [p.strip() for p in (it.get("description") or "").split(",")]
                if len(parts) < 2:
                    continue
                status = SPHERE_STATUS.get(parts[1].lower())
                if status:
                    out.append({"player": unescape(it.get("name", "")).strip(), "team": parts[0], "status": status,
                                "detail": ", ".join(parts[2:]), "src": "basketballsphere"})
    return out


def _expected_back(s, today):
    """'Expected back: 30 November' -> date (only exact day dates; month-only returns None)"""
    m = re.search(r"Expected back:\s*(\d{1,2})\s+([A-Za-z]+)(?:\s+(\d{4}))?", s)
    if not m or m.group(2).lower() not in MONTHS:
        return None
    year = int(m.group(3)) if m.group(3) else today.year
    d = dt.date(year, MONTHS[m.group(2).lower()], int(m.group(1)))
    if not m.group(3) and d < today - dt.timedelta(days=180):     # "5 January" written in November
        d = d.replace(year=year + 1)
    return d


def parse_bstats(html, today):
    out = []
    for team, body in re.findall(r'<section class="injteam"[^>]*><h2><a [^>]*>([^<]+)</a>(.*?)</section>', html, re.S):
        for title, player, note in re.findall(
                r'<span class="injm[^"]*" title="([^"]+)">.*?</span><a [^>]*>([^<]+)</a>\s*<span class="injt">([^<]*)',
                body, re.S):
            note = unescape(note).strip()
            if title == "Missed recent games":
                continue
            status = {"Injured": "Out", "Doubtful": "Doubtful", "Out (not injury)": "Out",
                      "Questionable": "Questionable", "Suspended": "Suspension"}.get(title)
            if not status:
                continue
            back = _expected_back(note, today)
            if status == "Out" and back and back <= today:
                status = "Questionable"
            out.append({"player": unescape(player).strip(), "team": unescape(team).strip(), "status": status,
                        "detail": re.sub(r"^\w[\w ()]*:\s*", "", note), "src": "basketballstats.eu"})
    return out


def load_manual(today):
    if not os.path.exists(MANUAL):
        return []
    with open(MANUAL, encoding="utf-8") as f:
        rows = json.load(f)
    keep = []
    for r in rows:
        until = r.get("until")
        if until and dt.date.fromisoformat(until) < today:
            continue
        keep.append({"player": r["player"], "team": r.get("team", ""), "status": r.get("status", "Out"),
                     "detail": r.get("detail", "manual"), "src": "manual"})
    return keep


def players_index(conn, today):
    """EL players with box scores this or last season: [(pid, name, tokens, last team code, team words)]"""
    season = today.year if today.month >= 8 else today.year - 1
    rows = conn.execute(text("""
        SELECT DISTINCT ON (b.player_id) b.player_id, b.player_name, b.team_code
        FROM props.el_boxscores b JOIN props.el_games g USING (game_id)
        WHERE g.season_code IN (:s1, :s0) ORDER BY b.player_id, g.start_utc DESC"""),
        {"s1": f"E{season}", "s0": f"E{season - 1}"}).all()
    names = {r.code: r.name for r in conn.execute(text("SELECT code, name FROM props.teams WHERE league = 'el'"))}
    return [(r.player_id, r.player_name, set(fold(r.player_name)), r.team_code, team_words(names.get(r.team_code, "")))
            for r in rows]


def match(idx, player, team):
    toks = set(fold(player))
    tw = team_words(team) if len(team) > 3 else set()
    code = team.upper() if len(team) <= 3 else None
    cands = [p for p in idx if p[2] == toks]
    if not cands and len(toks) >= 2:
        f = fold(player)
        first, last = f[0], f[-1]
        cands = [p for p in idx if last in p[2] and any(t.startswith(first[0]) for t in p[2] - {last})]
    if len(cands) > 1:
        same = [p for p in cands if (code and p[3] == code) or (tw and tw & p[4])]
        cands = same or cands
    return cands[0] if len(cands) == 1 else None


def collect(today):
    errors, sphere, bstats = [], [], []
    try:
        sphere = parse_sphere(get_text(SPHERE))
    except Exception as e:
        errors.append(f"basketballsphere: {e}")
    try:
        bstats = parse_bstats(get_text(BSTATS), today)
    except Exception as e:
        errors.append(f"basketballstats.eu: {e}")
    return sphere, bstats, load_manual(today), errors


def main():
    dry = "--dry-run" in sys.argv
    today = dt.date.today()
    eng = engine()
    with job(eng, "injuries", "el") as j:
        sphere, bstats, manual, errors = collect(today)
        if not sphere and not bstats:
            raise RuntimeError("no EuroLeague injury source could be read: " + "; ".join(errors))
        with eng.connect() as conn:
            idx = players_index(conn, today)
        merged, unmatched = {}, []
        for layer in (bstats, sphere, manual):           # later layers override earlier ones
            for r in layer:
                p = match(idx, r["player"], r["team"])
                if not p:
                    unmatched.append(f"{r['player']} ({r['team']}, {r['src']})")
                    continue
                merged[p[0]] = {**r, "pid": p[0], "code": p[3]}
        rows = [r for r in merged.values() if r["status"] != "Cleared"]
        now = dt.datetime.now(dt.timezone.utc)
        if not dry:
            with eng.begin() as conn:
                for r in rows:
                    conn.execute(text("""INSERT INTO props.injuries VALUES (:f, 'el', :p, :n, :t, :s, :d, :r)
                                         ON CONFLICT DO NOTHING"""),
                                 {"f": now, "p": r["pid"], "n": r["player"], "t": r["code"], "s": r["status"],
                                  "d": f"{r['detail']} [{r['src']}]"[:500], "r": now})
        for r in sorted(rows, key=lambda r: (r["code"], r["status"], r["player"])):
            print(f"  {r['code']:4s} {r['status']:12s} {r['player']:26s} {r['detail'][:60]} [{r['src']}]")
        if unmatched:
            print("  not matched (no EuroLeague box score history):", ", ".join(sorted(set(unmatched))))
        for e in errors:
            print("  !!", e)
        j["rows"] = len(rows)
        j["message"] = (f"{len(rows)} EL players listed (sphere {len(sphere)}, bstats {len(bstats)}, manual {len(manual)})"
                        + ("; " + "; ".join(errors) if errors else "") + (" [dry run]" if dry else ""))
        print(f"  EuroLeague injuries: {j['message']}")


if __name__ == "__main__":
    main()
