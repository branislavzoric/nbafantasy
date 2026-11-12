#!/usr/bin/env python3
"""
Players +/- full refresh, in order:
  1. EuroLeague schedule + new final box scores        (players_pm.ingest_el)
  2. NBA box scores, catch-up through yesterday (ET)   (collect_boxscores.py)
     NBA play-by-play for new games: stints, exact minutes, usage, rebound/assist chances,
     repairs of ESPN box rows without a player id     (players_pm.pbp)
     Official NBA injury report, newest PDF            (players_pm.injury_report)
     Data-quality gate; a blocking failure stops here  (players_pm.validate)
  3. Soccerbet lines EL + NBA, team/player refresh, name matching   (players_pm.lines)
  4. Weekly (or --train): retrain + backtest the model  (players_pm.model train)
     and refit the game simulator for correlated tickets (players_pm.gamesim fit)
  5. Export today's + yesterday's slate JSON, with model predictions for games not started yet
                                                       (players_pm.export -> players_pm.model.predict)
  6. --site: rebuild b3basketball-site/public          (build_site.py; deploying stays manual)

Each step is retried once and a failure doesn't stop the later steps: the page then shows the
last good data with a red freshness dot (props.job_runs). Meant to run several times a day,
e.g. 10:30, 13:00, 16:00, 18:00 and 23:30 Belgrade time (Soccerbet posts NBA props around noon).
Hosting / scheduling is not decided yet (2026-11-05), so nothing schedules this automatically.

Usage:  python -m players_pm.run [--site] [--no-lines] [--train]
"""

import os
import subprocess
import sys
import time

from .common import PROJECT_DIR

PY = sys.executable
SITE_DIR = os.path.join(os.path.expanduser("~"), "Desktop", "b3basketball-site")
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


OTHER_BOOKS = ["mozzartbet", "superbet", "balkanbet"]


def step(name, cmd, cwd=PROJECT_DIR):
    for attempt in (1, 2):
        print(f"== {name} (attempt {attempt})", flush=True)
        r = subprocess.run(cmd, cwd=cwd, env=ENV)
        if r.returncode == 0:
            return True
        time.sleep(30)
    print(f"!! {name} failed twice -- continuing with the last good data", flush=True)
    return False


def model_age_days():
    cur = os.path.join(PROJECT_DIR, "Players PM", "models", "CURRENT")
    return (time.time() - os.path.getmtime(cur)) / 86400 if os.path.exists(cur) else 999


def main():
    # 1-2. historical data: box scores, then play-by-play (lineups, exact minutes, usage, box repairs)
    ok = [step("EuroLeague box scores", [PY, "-m", "players_pm.ingest_el"]),
          step("NBA box scores", [PY, "collect_boxscores.py"]),
          step("NBA play-by-play (stints, usage)", [PY, "-m", "players_pm.pbp"])]
    # 4. availability: official NBA injury report (ESPN injuries are refreshed inside players_pm.lines) and
    #    EuroLeague injuries from public injury pages (no league feed; never blocks the refresh)
    ok.append(step("NBA official injury report", [PY, "-m", "players_pm.injury_report"]))
    step("EuroLeague injuries", [PY, "-m", "players_pm.el_injuries"])
    # 3. validation gate: blocking failures stop projections (the site keeps the last good data)
    gate = step("Data-quality checks", [PY, "-m", "players_pm.validate"])
    ok.append(gate)
    if not gate:
        print("!! data-quality gate failed: projections and the site build are skipped (see props.data_checks)")
        print("Done with failures (see props.job_runs)")
        sys.exit(1)
    if "--no-lines" not in sys.argv:
        # other bookmakers for the Players +/- "Bookmaker" option (page only; Let's Bet stays on Soccerbet).
        # Admiral and MaxBet price like Soccerbet, so Soccerbet represents them. Never blocks the refresh.
        step("Other books: NBA props (Mozzart, Superbet, BalkanBet)",
             [PY, "scrape_odds.py", "--nba", *OTHER_BOOKS])
        step("Other books: EuroLeague props (Mozzart, Superbet, BalkanBet)", [PY, "scrape_odds.py", *OTHER_BOOKS])
        ok.append(step("Soccerbet lines + matching", [PY, "-m", "players_pm.lines"]))
    if model_age_days() >= 7 or "--train" in sys.argv:          # weekly retrain (adds the new games)
        ok.append(step("Model training + backtest", [PY, "-m", "players_pm.model", "train"]))
        ok.append(step("Game simulator fit + backtest", [PY, "-m", "players_pm.gamesim", "fit"]))
    ok.append(step("Export + predictions", [PY, "-m", "players_pm.export"]))
    # Daily card: settle yesterday's (the export above re-wrote that day with its box scores), build today's
    ok.append(step("Daily card: settle yesterday + finished today", [PY, "-m", "players_pm.daily_card", "settle"]))
    ok.append(step("Daily card: build today", [PY, "-m", "players_pm.daily_card", "build"]))
    # Against the Odds football tickets: settle yesterday + today from ESPN (never blocks the deploy)
    step("Football tickets: settle", [PY, os.path.join("Odds", "football", "settle_league_tickets.py")])
    if "--site" in sys.argv:
        ok.append(step("Site build", [PY, "build_site.py"], cwd=SITE_DIR))
    print("Done" if all(ok) else "Done with failures (see props.job_runs)")
    sys.exit(0 if all(ok) else 1)


if __name__ == "__main__":
    main()
