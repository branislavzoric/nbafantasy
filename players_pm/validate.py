#!/usr/bin/env python3
"""
Data-quality gate for Players +/- (run after the box score / play-by-play refresh, before projections).

Every check is stored in props.data_checks (insert-only) and in "Players PM/reports/data_quality_<day>.json".
'blocking' checks fail the run (exit code 1) so the pipeline doesn't build projections on broken data;
the others are reported as warnings.

NBA checks
  games_vs_espn        every game ESPN's scoreboard lists as final in the last N days is stored (default 3,
                       --full = the whole current season)
  final_without_box    final games with no box score rows
  duplicate_rows       (game_id, player_id) twice
  box_vs_score         sum of players' points = team's final score
  starters             exactly 5 starters per team-game
  team_minutes         team minutes = 240 + 25 per OT (+-5 for ESPN's whole-minute rounding)
  played_no_minutes    rows marked played with no minutes
  team_games_2025_26   regular-season games per team (82)
  id_name_conflicts    one ESPN id with two different names / one name with two ids on the same team
  pbp_coverage         final games with play-by-play rows (props.player_game_adv)
  pbp_minutes          players whose rebuilt minutes are within 90 s of the box score
  pbp_vs_box_usage     play-by-play FGA / FTA / TOV totals = box score totals per team-game
  injury_report_age    newest official injury report (hours)
  injury_report_match  injury-report rows matched to an ESPN player id
EuroLeague checks
  el_final_without_box, el_duplicate_rows, el_box_vs_score

Usage: python -m players_pm.validate [--full] [--days N]
"""

import datetime as dt
import json
import os
import sys

from sqlalchemy import text

from .common import PM_DIR, engine, ensure_schema, get_json, job

SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
NBA_OK = "EXISTS (SELECT 1 FROM props.teams t WHERE t.league='nba' AND t.code=g.home_team) " \
         "AND EXISTS (SELECT 1 FROM props.teams t WHERE t.league='nba' AND t.code=g.away_team)"


def q(conn, sql, **kw):
    return [dict(r._mapping) for r in conn.execute(text(sql), kw)]


def run_checks(eng, days, full):
    C = []

    def add(league, name, ok, value, detail=None, blocking=False):
        C.append({"league": league, "check": name, "ok": bool(ok), "value": value, "blocking": blocking,
                  "detail": detail})

    with eng.connect() as conn:
        # ---------------------------------------------------------------- completeness vs ESPN
        season_start = conn.execute(text("""SELECT min(game_date) FROM public.nba_games
                                            WHERE season = (SELECT max(season) FROM public.nba_games)
                                              AND season_type = 'regular'""")).scalar()
        today = dt.date.today()
        start = season_start if full else max(season_start, today - dt.timedelta(days=days))
        have = {r["game_id"] for r in q(conn, "SELECT game_id FROM public.nba_games WHERE game_date >= :d", d=start)}
        missing, checked = [], 0
        d = start
        while d < today:
            for e in get_json(SCOREBOARD, {"dates": d.strftime("%Y%m%d")}, min_gap=0.3).get("events", []):
                st = e["status"]["type"]
                if st.get("completed") and st.get("name") == "STATUS_FINAL":
                    checked += 1
                    if str(e["id"]) not in have:
                        missing.append({"date": str(d), "game_id": e["id"], "game": e.get("shortName")})
            d += dt.timedelta(days=1)
        add("nba", "games_vs_espn", not missing, len(missing),
            {"from": str(start), "espn_final_games": checked, "missing": missing}, blocking=True)

        r = q(conn, f"""SELECT g.game_id, g.game_date FROM public.nba_games g
                        WHERE g.status='STATUS_FINAL' AND g.season_type <> 'preseason' AND {NBA_OK}
                          AND NOT EXISTS (SELECT 1 FROM public.nba_boxscores b WHERE b.game_id=g.game_id)""")
        add("nba", "final_without_box", not r, len(r), [{**x, "game_date": str(x["game_date"])} for x in r[:20]], True)

        r = q(conn, "SELECT game_id, player_id, count(*) n FROM public.nba_boxscores GROUP BY 1,2 HAVING count(*) > 1")
        add("nba", "duplicate_rows", not r, len(r), r[:20], True)

        r = q(conn, f"""SELECT b.game_id, b.team, g.game_date, sum(b.pts) box_pts,
                               max(CASE WHEN b.team=g.home_team THEN g.home_score ELSE g.away_score END) score
                        FROM public.nba_boxscores b JOIN public.nba_games g USING (game_id)
                        WHERE g.season_type <> 'preseason' AND {NBA_OK}
                        GROUP BY 1,2,3
                        HAVING sum(b.pts) <> max(CASE WHEN b.team=g.home_team THEN g.home_score ELSE g.away_score END)""")
        add("nba", "box_vs_score", not r, len(r), [{**x, "game_date": str(x["game_date"])} for x in r[:20]], True)

        r = q(conn, f"""SELECT b.game_id, b.team, sum(b.starter::int) s FROM public.nba_boxscores b
                        JOIN public.nba_games g USING (game_id) WHERE {NBA_OK} AND g.season_type <> 'preseason'
                        GROUP BY 1,2 HAVING sum(b.starter::int) <> 5""")
        add("nba", "starters", not r, len(r), r[:20])

        r = q(conn, f"""WITH p AS (SELECT game_id, max(period) np FROM props.nba_stints GROUP BY 1),
                        m AS (SELECT b.game_id, b.team, sum(b.min) mins FROM public.nba_boxscores b GROUP BY 1,2)
                        SELECT m.game_id, m.team, m.mins, 240 + 25 * greatest(p.np - 4, 0) expected
                        FROM m JOIN p USING (game_id) JOIN public.nba_games g USING (game_id)
                        WHERE {NBA_OK} AND abs(m.mins - (240 + 25 * greatest(p.np - 4, 0))) > 5""")
        add("nba", "team_minutes", not r, len(r), r[:20])

        r = q(conn, """SELECT count(*) n FROM public.nba_boxscores
                       WHERE NOT did_not_play AND min IS NULL""")
        add("nba", "played_no_minutes", True, r[0]["n"],
            "rows without minutes count as DNP in props.player_games (all have 0 stats)")

        r = q(conn, f"""SELECT t, count(*) n FROM (
                            SELECT home_team t FROM public.nba_games g WHERE season=2026 AND season_type='regular' AND {NBA_OK}
                            UNION ALL SELECT away_team FROM public.nba_games g WHERE season=2026 AND season_type='regular' AND {NBA_OK}) x
                        GROUP BY 1 HAVING count(*) NOT IN (82, 83)""")
        add("nba", "team_games_2025_26", not r, len(r), r or "every team 82 (+1 for the two NBA Cup finalists)")

        r = q(conn, """SELECT player_id, array_agg(DISTINCT player_name) names FROM public.nba_boxscores
                       GROUP BY 1 HAVING count(DISTINCT player_name) > 1""")
        r2 = q(conn, """SELECT player_name, team, array_agg(DISTINCT player_id) ids FROM public.nba_boxscores
                        GROUP BY 1,2 HAVING count(DISTINCT player_id) > 1""")
        add("nba", "id_name_conflicts", not r2, len(r2), {"one_id_several_names": r[:20], "one_name_several_ids": r2[:20]})

        # ---------------------------------------------------------------- play-by-play
        r = q(conn, f"""SELECT count(*) total, count(a.game_id) covered FROM public.nba_games g
                        LEFT JOIN (SELECT DISTINCT game_id FROM props.player_game_adv) a USING (game_id)
                        WHERE g.status='STATUS_FINAL' AND g.season_type <> 'preseason' AND {NBA_OK}""")[0]
        add("nba", "pbp_coverage", r["covered"] == r["total"], r["covered"], r)

        r = q(conn, """SELECT count(*) n, sum(pbp_ok::int) ok, count(DISTINCT game_id) FILTER (WHERE NOT pbp_ok) bad_games
                       FROM props.player_game_adv WHERE sec_box > 0""")[0]
        share = (r["ok"] or 0) / r["n"] if r["n"] else 0
        add("nba", "pbp_minutes", share >= 0.97, round(100 * share, 2),
            {**r, "note": "% of player-games within 90 s of the box score; usage uses only pbp_ok rows"})

        r = q(conn, """WITH a AS (SELECT game_id, team, sum(player_use) pbp_use FROM props.player_game_adv GROUP BY 1,2),
                       b AS (SELECT game_id, team, sum(coalesce(fga,0) + 0.44*coalesce(fta,0) + coalesce(tov,0)) box_use
                             FROM public.nba_boxscores GROUP BY 1,2)
                       SELECT count(*) n, sum((abs(pbp_use - box_use) <= 2)::int) close_n
                       FROM a JOIN b USING (game_id, team)""")[0]
        share = (r["close_n"] or 0) / r["n"] if r["n"] else 0
        add("nba", "pbp_vs_box_usage", share >= 0.95, round(100 * share, 2),
            {**r, "note": "% of team-games where play-by-play FGA+0.44FTA+TOV is within 2 of the box score "
                          "(team turnovers without a player are in the box total only)"})

        # ---------------------------------------------------------------- injury report
        r = q(conn, """SELECT max(report_ts) newest, count(DISTINCT report_ts) reports,
                              count(*) FILTER (WHERE status NOT IN ('Not Yet Submitted')) rows,
                              count(*) FILTER (WHERE status NOT IN ('Not Yet Submitted') AND player_id IS NULL) unmatched
                       FROM props.nba_injury_report""")[0]
        now_et = dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=-5))).replace(tzinfo=None)
        age = (now_et - r["newest"]).total_seconds() / 3600 if r["newest"] else None
        add("nba", "injury_report_age", age is not None and age <= 6, round(age, 1) if age is not None else None,
            {"newest": str(r["newest"]), "reports": r["reports"]})
        share = 1 - (r["unmatched"] or 0) / r["rows"] if r["rows"] else 0
        add("nba", "injury_report_match", share >= 0.95, round(100 * share, 2),
            {"rows": r["rows"], "unmatched": r["unmatched"],
             "note": "unmatched = mostly two-way / G League players who have never played an NBA minute"})

        # ---------------------------------------------------------------- EuroLeague
        r = q(conn, """SELECT game_id FROM props.el_games g WHERE g.box_final
                       AND NOT EXISTS (SELECT 1 FROM props.el_boxscores b WHERE b.game_id = g.game_id)""")
        add("el", "el_final_without_box", not r, len(r), r[:20], True)
        r = q(conn, "SELECT game_id, player_id FROM props.el_boxscores GROUP BY 1,2 HAVING count(*) > 1")
        add("el", "el_duplicate_rows", not r, len(r), r[:20], True)
        cols = {x["column_name"] for x in q(conn, """SELECT column_name FROM information_schema.columns
                                                     WHERE table_schema='props' AND table_name='el_games'""")}
        if {"home_score", "away_score"} <= cols:
            r = q(conn, """SELECT b.game_id, b.team_code, sum(b.pts) box_pts,
                                  max(CASE WHEN b.team_code=g.home_code THEN g.home_score ELSE g.away_score END) score
                           FROM props.el_boxscores b JOIN props.el_games g USING (game_id) WHERE g.box_final
                           GROUP BY 1,2 HAVING sum(b.pts) <> max(CASE WHEN b.team_code=g.home_code
                                                                 THEN g.home_score ELSE g.away_score END)""")
            add("el", "el_box_vs_score", not r, len(r), r[:20])

        # ---------------------------------------------------------------- totals for the report
        totals = q(conn, f"""SELECT g.season, g.season_type, count(DISTINCT g.game_id) games, count(b.*) player_rows,
                                    count(b.*) FILTER (WHERE NOT b.did_not_play AND b.min > 0) played_rows,
                                    count(b.*) FILTER (WHERE b.box_source = 'pbp') rebuilt_rows
                             FROM public.nba_games g LEFT JOIN public.nba_boxscores b USING (game_id)
                             WHERE {NBA_OK} GROUP BY 1,2 ORDER BY 1,2""")
        el_totals = q(conn, """SELECT g.season_code, count(DISTINCT g.game_id) games, count(b.*) player_rows
                               FROM props.el_games g LEFT JOIN props.el_boxscores b USING (game_id)
                               WHERE g.box_final GROUP BY 1 ORDER BY 1""")
    return C, {"nba": totals, "el": el_totals}


def main():
    eng = engine()
    ensure_schema(eng)
    days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 3
    with job(eng, "validate") as j:
        checks, totals = run_checks(eng, days, "--full" in sys.argv)
        now = dt.datetime.now(dt.timezone.utc)
        with eng.begin() as conn:
            conn.execute(text("""INSERT INTO props.data_checks (checked_at, league, check_name, ok, value, detail)
                                 VALUES (:t, :league, :check, :ok, :value, :detail)"""),
                         [{"t": now, **c, "detail": json.dumps(c["detail"], default=str)} for c in checks])
        failed = [c for c in checks if c["blocking"] and not c["ok"]]
        warn = [c for c in checks if not c["blocking"] and not c["ok"]]
        os.makedirs(os.path.join(PM_DIR, "reports"), exist_ok=True)
        path = os.path.join(PM_DIR, "reports", f"data_quality_{now:%Y-%m-%d}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"checked_at": now.isoformat(), "totals": totals, "checks": checks}, f, indent=1, default=str)
        for c in checks:
            flag = "OK  " if c["ok"] else ("FAIL" if c["blocking"] else "WARN")
            print(f"{flag} {c['league']:3s} {c['check']:22s} {c['value']}")
        for k, rows in totals.items():
            for r in rows:
                print(k, {kk: (str(v) if not isinstance(v, (int, float)) else v) for kk, v in r.items()})
        j["rows"] = len(checks)
        j["status"] = "failed" if failed else "ok"
        j["message"] = f"{len(failed)} blocking failure(s), {len(warn)} warning(s); report {path}"
        print(j["message"])
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
