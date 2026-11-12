#!/usr/bin/env python3
"""
Injury watch for today's Daily Card (NBA): pulls the newest official NBA injury report and compares every
status on the teams of tonight's NOT-started games with the statuses the card was built with
(card.json `injury_seen`, written by daily_card.build).

A change matters when it touches
  - a player who has a leg on the card (any status change), or
  - a rotation player (5+ of the last 10 team games, 15+ min on average) moving to/from Out, Doubtful or
    Questionable (teammates' absences move usage, minutes and our with/without adjustment; opponents too).

On a change that matters: Telegram alert (admin chat) -> Soccerbet lines -> export + predictions ->
daily_card build (open games only once the first game has tipped) -> site build -> wrangler deploy -> Telegram
summary. Nothing changed -> nothing happens (one line in the log). Lock file prevents overlapping runs.

Usage:  python -m players_pm.injury_watch [--dry-run] [--no-deploy]
Scheduled by `injury_watch.cmd` (Windows Task Scheduler, every 15 min, 14:00-04:00 Belgrade).
"""

import datetime as dt
import json
import os
import subprocess
import sys
import time

from sqlalchemy import text

from .common import PROJECT_DIR, engine
from . import daily_card as DC

PY = sys.executable
SITE_DIR = os.path.join(os.path.expanduser("~"), "Desktop", "b3basketball-site")
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}
LOCK = os.path.join(PROJECT_DIR, "Players PM", "injury_watch.lock")
LOG = os.path.join(PROJECT_DIR, "Players PM", "reports", "injury_watch.log")
BAD = ("Out", "Doubtful", "Questionable")


def log(msg):
    line = f"{dt.datetime.now(DC.BEL):%Y-%m-%d %H:%M} {msg}"
    print(line, flush=True)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def notify(msg):
    try:
        sys.path.insert(0, PROJECT_DIR)
        from telegram_notify import notify as tg
        tg(msg)
    except Exception as e:                                   # an alert must never break the watch
        log(f"telegram failed: {e}")


def run(name, cmd, cwd=PROJECT_DIR):
    r = subprocess.run(cmd, cwd=cwd, env=ENV, capture_output=True, text=True, encoding="utf-8", errors="replace")
    tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
    log(f"  {name}: exit {r.returncode} {' | '.join(tail)[:300]}")
    return r.returncode == 0


def rotation(eng, teams):
    """{(team, player_id)} who played 5+ of the team's last 10 games at 15+ min on average"""
    with eng.connect() as c:
        rows = c.execute(text("""
            WITH g AS (SELECT team_code, game_id, start_utc,
                              dense_rank() OVER (PARTITION BY team_code ORDER BY start_utc DESC) rk
                       FROM (SELECT DISTINCT team_code, game_id, start_utc FROM props.player_games
                             WHERE league = 'nba' AND team_code = ANY(:t)) x)
            SELECT p.team_code, p.player_id FROM props.player_games p JOIN g USING (team_code, game_id)
            WHERE g.rk <= 10 AND p.status = 'played'
            GROUP BY 1, 2 HAVING count(*) >= 5 AND avg(p.minutes) >= 15"""), {"t": list(teams)}).all()
    return {(r[0], r[1]) for r in rows}


def changes(eng, card, open_games):
    from .model import injuries_now
    teams = set()
    for gid in open_games:
        g = card["games_meta"][gid]
        if g["league"] == "nba":
            teams |= {g["home"], g["away"]}
    if not teams:
        return [], teams
    inj, _ = injuries_now(eng)
    now = {pid: (r["player_name"], r["team_code"], r["status"]) for (lg, pid), r in inj.items()
           if lg == "nba" and r.get("team_code") in teams}
    seen = {pid: tuple(v) for pid, v in (card.get("injury_seen") or {}).items() if v[1] in teams}
    on_card = {l["pid"] for l in card["legs"].values() if l["game"] in open_games}
    rot = rotation(eng, teams)
    out = []
    for pid in set(now) | set(seen):
        old = seen.get(pid, (None, None, None))
        new = now.get(pid, (None, None, None))
        o, n = old[2], new[2]
        if o == n:
            continue
        name, team = new[0] or old[0], new[1] or old[1]
        mattered = pid in on_card or ((team, pid) in rot and (o in BAD or n in BAD))
        if mattered:
            out.append({"pid": pid, "name": name, "team": team, "old": o or "available", "new": n or "available",
                        "on_card": pid in on_card})
    return out, teams


def main():
    dry = "--dry-run" in sys.argv
    if os.path.exists(LOCK) and time.time() - os.path.getmtime(LOCK) < 40 * 60:
        log("another watch run is still going; skipped")
        return
    open(LOCK, "w").close()
    try:
        day = DC.slate_day(dt.datetime.now(DC.BEL)).isoformat()
        card_path = os.path.join(DC.OUT, day, "card.json")
        if not os.path.exists(card_path):
            log(f"{day}: no card yet; nothing to watch")
            return
        with open(card_path, encoding="utf-8") as f:
            card = json.load(f)
        now = dt.datetime.now(DC.BEL)
        open_games = {gid for gid, g in card["games_meta"].items()
                      if g.get("start") and dt.datetime.fromisoformat(g["start"]) > now + dt.timedelta(minutes=5)}
        if not any(card["games_meta"][g]["league"] == "nba" for g in open_games):
            log(f"{day}: no NBA game left to tip; nothing to watch")
            return
        run("official injury report", [PY, "-m", "players_pm.injury_report"])
        eng = engine()
        ch, _ = changes(eng, card, open_games)
        if not ch:
            log(f"{day}: no relevant status change ({len(open_games)} open games)")
            return
        lines = [f"{'⭐ ' if c['on_card'] else ''}{c['name']} ({c['team']}): {c['old']} → {c['new']}" for c in ch]
        log(f"{day}: {len(ch)} change(s): " + "; ".join(lines))
        if dry:
            return
        notify("🚑 <b>Injury report changed</b> — rebuilding tonight's card\n" + "\n".join(lines))
        ok = run("Soccerbet lines", [PY, "-m", "players_pm.lines"])
        ok &= run("export + predictions", [PY, "-m", "players_pm.export"])
        started = card.get("first_tip") and dt.datetime.fromisoformat(card["first_tip"]) <= now
        ok &= run("daily card", [PY, "-m", "players_pm.daily_card", "build", day] + (["--open"] if started else ["--force"]))
        ok &= run("site build", [PY, "build_site.py"], cwd=SITE_DIR)
        if ok and "--no-deploy" not in sys.argv:
            ok &= run("deploy", ["cmd", "/c", "npx", "-y", "wrangler@4", "deploy"], cwd=SITE_DIR)
        with open(card_path, encoding="utf-8") as f:
            new = json.load(f)
        top = [new["legs"][s["leg"]] for s in new["top20"][:5]]
        notify(("✅ Card rebuilt and deployed" if ok else "⚠️ Rebuild had failures (see injury_watch.log)") +
               f"\nTop 5 now:\n" + "\n".join(f"{l['player']} {l['side']} {l['line']:g} {l['short']} @ {l['odds']:.2f} "
                                              f"({round(l['conf'] * 100)}%)" for l in top) +
               "\nhttps://www.b3basketball.com/lets-bet")
    finally:
        try:
            os.remove(LOCK)
        except OSError:
            pass


if __name__ == "__main__":
    main()
