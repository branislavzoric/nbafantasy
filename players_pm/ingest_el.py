#!/usr/bin/env python3
"""
EuroLeague schedule + box scores -> props.el_games / props.el_boxscores / props.teams.

Source: the official EuroLeague API (public, no key):
  https://api-live.euroleague.net/v2/competitions/E/seasons/<E2026>/games   schedule + results
  https://live.euroleague.net/api/Boxscore?gamecode=<n>&seasoncode=<E2026>  box score
Only games that have been played are fetched; a box score is marked final (box_final) once the
game is over by 3+ hours, so a live/unfinished box score is never counted as history.
Raw JSON is cached in "Players PM/raw/el/<season>/" so a re-run doesn't hit the API again.

Usage:
  python -m players_pm.ingest_el                 # current season E2026
  python -m players_pm.ingest_el E2025 E2026     # backfill 2025-26 too
"""

import datetime as dt
import json
import os
import sys

from sqlalchemy import text

from .common import PM_DIR, engine, ensure_schema, fold, get_json, job

GAMES_URL = "https://api-live.euroleague.net/v2/competitions/E/seasons/{}/games"
BOX_URL = "https://live.euroleague.net/api/Boxscore"
CURRENT = "E2026"


def minutes(v):
    if not v or ":" not in str(v):
        return None
    m, s = str(v).split(":")[:2]
    return round(int(m) + int(s) / 60, 2)


def display_name(raw):
    last, _, first = raw.partition(",")
    return " ".join(w.capitalize() if w.isupper() else w for w in f"{first.strip()} {last.strip()}".split())


def box_for(season, code, cache_dir, final):
    path = os.path.join(cache_dir, f"box_{code}.json")
    if final and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    box = get_json(BOX_URL, {"gamecode": code, "seasoncode": season}, min_gap=1.0)
    if final:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(box, f, ensure_ascii=False)
    return box


def ingest(eng, season):
    cache_dir = os.path.join(PM_DIR, "raw", "el", season)
    os.makedirs(cache_dir, exist_ok=True)
    data = get_json(GAMES_URL.format(season))
    games = data.get("data", data) if isinstance(data, dict) else data
    now = dt.datetime.now(dt.timezone.utc)
    with eng.connect() as conn:
        have = {r[0] for r in conn.execute(text("SELECT game_id FROM props.el_games WHERE box_final"))}

    teams, n_games, n_lines, problems = {}, 0, 0, []
    for g in games:
        for side in ("local", "road"):
            c = g[side]["club"]
            teams[c["code"]] = (c["name"], c.get("editorialName") or c.get("abbreviatedName"),
                                (c.get("images") or {}).get("crest"), c.get("abbreviatedName"), c.get("tvCode"))
        gid = f"{season}_{g['gameCode']}"
        start = dt.datetime.fromisoformat(g["utcDate"].replace("Z", "+00:00"))
        final = bool(g.get("played")) and now - start > dt.timedelta(hours=3)
        row = {"game_id": gid, "season_code": season, "game_code": g["gameCode"],
               "phase": (g.get("phaseType") or {}).get("code"), "round": g.get("round"), "start_utc": start,
               "home_code": g["local"]["club"]["code"], "away_code": g["road"]["club"]["code"],
               "home_score": g["local"].get("score") if g.get("played") else None,
               "away_score": g["road"].get("score") if g.get("played") else None,
               "played": bool(g.get("played"))}
        lines = []
        if final and gid not in have:
            try:
                box = box_for(season, g["gameCode"], cache_dir, final=True)
            except Exception as e:                       # one bad game shouldn't stop the season
                problems.append(f"{gid}: {e}")
                final = False
            else:
                for team in box.get("Stats") or []:
                    for p in team.get("PlayersStats") or []:
                        pid = (p.get("Player_ID") or "").strip()
                        if not pid.startswith("P"):
                            continue
                        mins = minutes(p.get("Minutes"))
                        lines.append({
                            "game_id": gid, "player_id": pid, "player_name": p["Player"],
                            "team_code": p["Team"].strip(), "starter": bool(p.get("IsStarter")),
                            "played": bool(mins),   # IsPlaying = on court at the end, NOT "appeared"
                            "minutes": mins, "pts": p.get("Points"),
                            "fgm2": p.get("FieldGoalsMade2"), "fga2": p.get("FieldGoalsAttempted2"),
                            "tpm": p.get("FieldGoalsMade3"), "tpa": p.get("FieldGoalsAttempted3"),
                            "ftm": p.get("FreeThrowsMade"), "fta": p.get("FreeThrowsAttempted"),
                            "oreb": p.get("OffensiveRebounds"), "dreb": p.get("DefensiveRebounds"),
                            "reb": p.get("TotalRebounds"), "ast": p.get("Assistances"),
                            "stl": p.get("Steals"), "blk": p.get("BlocksFavour"), "tov": p.get("Turnovers"),
                            "pf": p.get("FoulsCommited"), "pir": p.get("Valuation")})
                if not lines:
                    problems.append(f"{gid}: empty box score")
                    final = False
        row["box_final"] = final and (gid in have or bool(lines))
        with eng.begin() as conn:
            conn.execute(text("""
                INSERT INTO props.el_games (game_id, season_code, game_code, phase, round, start_utc, home_code,
                    away_code, home_score, away_score, played, box_final, fetched_at)
                VALUES (:game_id, :season_code, :game_code, :phase, :round, :start_utc, :home_code, :away_code,
                    :home_score, :away_score, :played, :box_final, now())
                ON CONFLICT (game_id) DO UPDATE SET phase = EXCLUDED.phase, round = EXCLUDED.round,
                    start_utc = EXCLUDED.start_utc, home_code = EXCLUDED.home_code, away_code = EXCLUDED.away_code,
                    home_score = EXCLUDED.home_score, away_score = EXCLUDED.away_score, played = EXCLUDED.played,
                    box_final = props.el_games.box_final OR EXCLUDED.box_final, fetched_at = now()"""), row)
            if lines:
                cols = list(lines[0])
                conn.execute(text(f"""
                    INSERT INTO props.el_boxscores ({", ".join(cols)}) VALUES ({", ".join(":" + c for c in cols)})
                    ON CONFLICT (game_id, player_id) DO UPDATE SET
                    {", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in ("game_id", "player_id"))}"""), lines)
                n_games += 1
                n_lines += len(lines)
                print(f"  {gid} {row['home_code']}-{row['away_code']}: {len(lines)} players")

    with eng.begin() as conn:
        for code, (name, short, logo, abbr, tv) in teams.items():
            conn.execute(text("""INSERT INTO props.teams (league, code, name, short, logo) VALUES ('el', :c, :n, :s, :l)
                                 ON CONFLICT (league, code) DO UPDATE SET name = EXCLUDED.name,
                                 short = EXCLUDED.short, logo = EXCLUDED.logo"""),
                         {"c": code, "n": name, "s": short, "l": logo})
            for alias in {name, short, abbr, tv, code}:
                if alias:
                    conn.execute(text("""INSERT INTO props.team_aliases VALUES ('el', :a, :c)
                                         ON CONFLICT (league, alias) DO NOTHING"""),
                                 {"a": " ".join(fold(alias)), "c": code})
    return n_games, n_lines, problems


def main():
    seasons = [a for a in sys.argv[1:] if a.startswith("E")] or [CURRENT]
    eng = engine()
    ensure_schema(eng)
    for season in seasons:
        with job(eng, "el_stats", "el") as j:
            n_games, n_lines, problems = ingest(eng, season)
            j["rows"] = n_lines
            j["message"] = f"{season}: {n_games} new games" + (f"; problems: {'; '.join(problems)}" if problems else "")
            j["status"] = "partial" if problems else "ok"
            print(f"{season}: {n_games} new games, {n_lines} player lines" + (f", {len(problems)} problems" if problems else ""))


if __name__ == "__main__":
    main()
