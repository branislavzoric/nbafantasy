#!/usr/bin/env python3
"""
Players +/- data export: one JSON per slate day -> "Players PM/data/<YYYY-MM-DD>.json" + index.json.

Slate day: a game belongs to day D when it starts between 10:00 on D and 09:59 on D+1, Europe/Belgrade
time. So Thursday's NBA games, which tip off after midnight in Belgrade, appear on "Thursday" next to
Thursday's EuroLeague games. Times shown on the page are Belgrade times.

Per day:
  games    : every NBA/EL game of the slate (schedule from ESPN / EuroLeague API), with odds if listed
  props    : every Soccerbet player line of the slate's games (latest snapshot taken BEFORE tip-off),
             Over/Under prices, milestone flag, scrape time, matched stats player id (or null)
  players  : game logs since the start of 2025-26 for every player with a line (played + DNP rows)
  defense  : per league, per team, per stat: what opponents score against it (this season blended
             with 2025-26 while the sample is small), as a ratio to the league average -> matchup grade
  freshness: last successful run of each job, with its message

Nothing is invented: a line Soccerbet doesn't list isn't in the file, an unmatched player has
pid = null (shown as "no history"), and DNP games are flagged rather than counted.

Usage:
  python -m players_pm.export                 # today (slate day) and yesterday
  python -m players_pm.export 2026-11-05 ...  # specific days
"""

import datetime as dt
import json
import os
import sys
from collections import defaultdict
from zoneinfo import ZoneInfo

from sqlalchemy import text

from .common import PM_DIR, engine, ensure_schema, get_json, job

BEL = ZoneInfo("Europe/Belgrade")
DAY_START_H = 10
OUT = os.path.join(PM_DIR, "data")
HISTORY_FROM = dt.date(2025, 9, 1)            # start of 2025-26
CURRENT_SEASON = "2026-27"
STATS = ["pts", "reb", "ast", "tpm", "stl", "blk", "tov"]
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"


def slate_bounds(day):
    start = dt.datetime.combine(day, dt.time(DAY_START_H), BEL)
    return start, start + dt.timedelta(days=1)


def slate_day(ts):
    """aware or Belgrade-naive datetime -> slate day"""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=BEL)
    return (ts.astimezone(BEL) - dt.timedelta(hours=DAY_START_H)).date()


def nba_schedule(day):
    """ESPN scoreboard for the US dates that overlap the slate (status included)."""
    start, end = slate_bounds(day)
    out = []
    for d in {(start - dt.timedelta(hours=8)).date(), (end - dt.timedelta(hours=8)).date()}:
        for ev in get_json(ESPN_SCOREBOARD, {"dates": d.strftime("%Y%m%d")}).get("events", []):
            comp = ev["competitions"][0]
            t = dt.datetime.fromisoformat(comp["date"].replace("Z", "+00:00"))
            if not (start <= t < end):
                continue
            teams = {c["homeAway"]: c for c in comp["competitors"]}
            st = comp["status"]["type"]
            out.append({"league": "nba", "id": ev["id"], "start": t.astimezone(BEL).isoformat(),
                        "home": teams["home"]["team"]["abbreviation"], "away": teams["away"]["team"]["abbreviation"],
                        "home_score": int(teams["home"]["score"]) if st["state"] != "pre" else None,
                        "away_score": int(teams["away"]["score"]) if st["state"] != "pre" else None,
                        "status": {"pre": "scheduled", "in": "live", "post": "final"}.get(st["state"], st["state"]),
                        "status_text": st.get("shortDetail")})
    return sorted({g["id"]: g for g in out}.values(), key=lambda g: g["start"])


def el_schedule(conn, day):
    start, end = slate_bounds(day)
    rows = conn.execute(text("""SELECT * FROM props.el_games WHERE start_utc >= :s AND start_utc < :e
                                ORDER BY start_utc"""), {"s": start, "e": end}).mappings()
    now = dt.datetime.now(dt.timezone.utc)
    out = []
    for g in rows:
        status = "final" if g["box_final"] or (g["played"] and now - g["start_utc"] > dt.timedelta(hours=3)) \
            else "live" if g["start_utc"] <= now else "scheduled"
        out.append({"league": "el", "id": g["game_id"], "start": g["start_utc"].astimezone(BEL).isoformat(),
                    "home": g["home_code"], "away": g["away_code"], "status": status,
                    "home_score": g["home_score"] if g["played"] else None,
                    "away_score": g["away_score"] if g["played"] else None,
                    "round": g["round"]})
    return out


def lines_for(conn, day):
    """Soccerbet lines for games starting in the slate: per book+league, the newest run scraped before
    tip-off that contains the game (later runs drop started games, so each game keeps its last pre-game line)."""
    start, end = slate_bounds(day)
    rows = conn.execute(text("""
        WITH g AS (
            SELECT DISTINCT ON (pp.source, pp.game_id) pp.source, pp.game_id, pp.run_id, r.scraped_at, r.league_id
            FROM odds.player_props pp
            JOIN odds.scrape_runs r ON r.run_id = pp.run_id
            JOIN odds.games og ON og.source = pp.source AND og.game_id = pp.game_id
            JOIN props.bookmakers b ON b.book = pp.source AND b.active
            WHERE og.kickoff >= :s AND og.kickoff < :e
              AND r.scraped_at < (og.kickoff AT TIME ZONE 'Europe/Belgrade') + interval '5 minutes'
              AND r.league_id IN ('nba', 'euroleague')
            ORDER BY pp.source, pp.game_id, pp.run_id DESC
        )
        SELECT lg.league, p.source AS book, g.scraped_at, p.game_id, og.home, og.away, og.kickoff,
               p.team, p.player, p.market, p.side, p.line, p.odds, a.player_id AS pid, a.method
        FROM g
        JOIN odds.player_props p ON p.run_id = g.run_id AND p.game_id = g.game_id AND p.source = g.source
        JOIN odds.games og ON og.source = p.source AND og.game_id = p.game_id
        JOIN props.leagues lg ON lg.odds_league_id = g.league_id
        LEFT JOIN props.player_aliases a ON a.league = lg.league AND a.book = p.source AND a.book_name = p.player
        WHERE p.market NOT IN ('Double-double', '1Q Points', '1H Points')"""),
        {"s": start.replace(tzinfo=None), "e": end.replace(tzinfo=None)}).mappings().all()

    props = {}
    for r in rows:
        if r["side"] in ("Over", "Under"):
            line, milestone = float(r["line"]), False
        elif r["side"].endswith("+") and r["side"][:-1].isdigit():
            line, milestone = int(r["side"][:-1]) - 0.5, True
        else:
            continue
        key = (r["league"], r["book"], r["game_id"], r["player"], r["market"])
        p = props.setdefault(key, {
            "league": r["league"], "book": r["book"], "book_game": r["game_id"],
            "game_name": f"{r['home']} vs {r['away']}", "kickoff": r["kickoff"].replace(tzinfo=BEL).isoformat(),
            "player": r["player"], "team_name": r["team"], "pid": r["pid"], "match": r["method"],
            "market": r["market"], "scraped_at": r["scraped_at"].astimezone(BEL).isoformat(), "lines": {}})
        ln = p["lines"].setdefault(line, {"line": line, "over": None, "under": None, "milestone": milestone})
        side = "under" if r["side"] == "Under" else "over"
        if milestone and not ln["milestone"]:
            continue                                  # a real O/U at this line wins over the N+ milestone
        if not milestone and ln["milestone"]:
            ln.update(over=None, under=None, milestone=False)
        ln[side] = float(r["odds"])
    # opening line: the first run that listed the game
    opening = conn.execute(text("""
        WITH f AS (
            SELECT DISTINCT ON (pp.source, pp.game_id) pp.source, pp.game_id, pp.run_id, r.scraped_at
            FROM odds.player_props pp
            JOIN odds.scrape_runs r ON r.run_id = pp.run_id
            JOIN odds.games og ON og.source = pp.source AND og.game_id = pp.game_id
            JOIN props.bookmakers b ON b.book = pp.source AND b.active
            WHERE og.kickoff >= :s AND og.kickoff < :e AND r.league_id IN ('nba', 'euroleague')
            ORDER BY pp.source, pp.game_id, pp.run_id)
        SELECT p.source, p.game_id, p.player, p.market, p.line, p.side, p.odds, f.scraped_at
        FROM f JOIN odds.player_props p ON p.run_id = f.run_id AND p.game_id = f.game_id AND p.source = f.source
        WHERE p.side IN ('Over', 'Under')"""),
        {"s": start.replace(tzinfo=None), "e": end.replace(tzinfo=None)}).mappings().all()
    op = {}
    for r in opening:
        k = (r["source"], r["game_id"], r["player"], r["market"])
        o = op.setdefault(k, {"at": r["scraped_at"].astimezone(BEL).isoformat(), "lines": {}})
        o["lines"].setdefault(float(r["line"]), {})[r["side"].lower()] = float(r["odds"])

    out = []
    for p in props.values():
        o = op.get((p["book"], p["book_game"], p["player"], p["market"]))
        if o:
            two = [(abs(v["over"] - v["under"]), ln, v) for ln, v in o["lines"].items() if "over" in v and "under" in v]
            if two:
                _, ln, v = min(two, key=lambda t: t[0])
                p["open"] = {"line": ln, "over": v["over"], "under": v["under"], "at": o["at"]}
        p["lines"] = sorted(p["lines"].values(), key=lambda x: x["line"])
        ou = [x for x in p["lines"] if x["over"] and x["under"]]
        # main line = the two-way line with the most balanced prices
        p["main"] = min(ou, key=lambda x: abs(x["over"] - x["under"]))["line"] if ou else p["lines"][0]["line"]
        out.append(p)
    return out


def team_map(conn):
    teams = {(r.league, r.code): {"name": r.name, "short": r.short, "logo": r.logo}
             for r in conn.execute(text("SELECT * FROM props.teams"))}
    aliases = {(r.league, r.alias): r.code for r in conn.execute(text("SELECT * FROM props.team_aliases"))}
    return teams, aliases


def defense(conn):
    """{league: {team: {stat: ratio}}} -- opponents' per-game team total vs league average.
    Blend: this season weighted n/(n+8), the rest 2025-26 (so ~8 games in, it's half and half)."""
    rows = conn.execute(text(f"""
        SELECT league, season, opp_code AS team, game_id, {", ".join(f"sum({s}) AS {s}" for s in STATS)}
        FROM props.player_games WHERE status = 'played' AND game_date >= :h
        GROUP BY league, season, opp_code, game_id"""), {"h": HISTORY_FROM}).mappings()
    agg = defaultdict(lambda: defaultdict(lambda: [0, defaultdict(float)]))
    for r in rows:
        a = agg[(r["league"], r["season"])][r["team"]]
        a[0] += 1
        for s in STATS:
            a[1][s] += r[s] or 0
    out = {}
    for league in ("nba", "el"):
        cur, prev = agg.get((league, CURRENT_SEASON), {}), agg.get((league, "2025-26"), {})
        def league_avg(src, s):
            n = sum(v[0] for v in src.values())
            return sum(v[1][s] for v in src.values()) / n if n else None
        res = {}
        # rank only this season's teams (2025-26 also has All-Star/exhibition sides and relegated clubs)
        for team in (set(cur) or set(prev)):
            res[team] = {"games": cur.get(team, [0])[0]}
            for s in STATS:
                ratios, weights = [], []
                n_cur = cur.get(team, [0])[0]
                if n_cur and league_avg(cur, s):
                    ratios.append(cur[team][1][s] / n_cur / league_avg(cur, s))
                    weights.append(n_cur / (n_cur + 8))
                if team in prev and league_avg(prev, s):
                    ratios.append(prev[team][1][s] / prev[team][0] / league_avg(prev, s))
                    weights.append(1 - (weights[0] if weights else 0))
                res[team][s] = round(sum(r * w for r, w in zip(ratios, weights)) / sum(weights), 4) if weights else None
        out[league] = res
    return out


def logs(conn, pids_by_league):
    out = {}
    for league, pids in pids_by_league.items():
        if not pids:
            continue
        rows = conn.execute(text(f"""
            SELECT player_id, game_id, season, phase, game_date, team_code, opp_code, is_home, status, dnp_reason,
                   starter, minutes, {", ".join(STATS)}
            FROM props.player_games WHERE league = :l AND player_id = ANY(:p) AND game_date >= :h
            ORDER BY start_utc DESC"""), {"l": league, "p": list(pids), "h": HISTORY_FROM}).mappings()
        for r in rows:
            p = out.setdefault(f"{league}:{r['player_id']}", {"log": []})
            p["log"].append([r["game_date"].isoformat(), r["season"], r["team_code"], r["opp_code"],
                             "H" if r["is_home"] else "A", r["status"],
                             float(r["minutes"]) if r["minutes"] is not None else None,
                             *[r[s] for s in STATS], r["dnp_reason"]])
    return out


def freshness(conn):
    rows = conn.execute(text("""
        SELECT DISTINCT ON (job, league) job, league, status, started_at, finished_at, rows, message
        FROM props.job_runs WHERE job <> 'export' ORDER BY job, league, started_at DESC""")).mappings()
    last_ok = {(r.job, r.league): r.finished_at for r in conn.execute(text(
        "SELECT job, league, max(finished_at) AS finished_at FROM props.job_runs WHERE status <> 'failed' GROUP BY 1, 2"))}
    return [{"job": r["job"], "league": r["league"], "status": r["status"],
             "at": r["started_at"].astimezone(BEL).isoformat(),
             "last_ok": last_ok[(r["job"], r["league"])].astimezone(BEL).isoformat()
             if last_ok.get((r["job"], r["league"])) else None,
             "rows": r["rows"], "message": r["message"]} for r in rows]


def game_lines(conn, day, games, code):
    """Soccerbet game total / spread / team totals for each slate game: last pre-tip-off snapshot and the
    first one (line movement). Keyed by our game id; spread and team totals from OUR home team's side."""
    start, end = slate_bounds(day)
    rows = conn.execute(text("""
        SELECT * FROM props.game_lines WHERE kickoff >= :s AND kickoff < :e
          AND scraped_at < (kickoff AT TIME ZONE 'Europe/Belgrade') + interval '5 minutes'
        ORDER BY scraped_at"""), {"s": start.replace(tzinfo=None), "e": end.replace(tzinfo=None)}).mappings().all()
    num = lambda v: None if v is None else float(v)
    out = {}
    for r in rows:
        hc, ac = code(r["league"], r["home"]), code(r["league"], r["away"])
        ko = r["kickoff"].replace(tzinfo=BEL)
        g = next((g for g in games if g["league"] == r["league"] and {g["home"], g["away"]} == {hc, ac}
                  and abs(dt.datetime.fromisoformat(g["start"]) - ko) < dt.timedelta(hours=3)), None)
        if not g:
            continue
        flip = g["home"] != hc                         # book lists the teams the other way round
        sp = num(r["spread_home"])
        snap = {"at": r["scraped_at"].astimezone(BEL).isoformat(), "total": num(r["total"]),
                "spread_home": (-sp if flip and sp is not None else sp),
                "home_total": num(r["away_total"] if flip else r["home_total"]),
                "away_total": num(r["home_total"] if flip else r["away_total"])}
        e = out.setdefault(g["id"], {"open": snap})
        e["last"] = snap
    return out


def game_expectations(eng, games):
    """per slate game: expected points of both teams, total, margin and pace from the same pre-game team
    ratings the model uses (features.team_games), plus each team's league rank in offence / defence / pace.
    Works before any player prop is posted (team stubs for games not in the box-score tables yet)."""
    import pandas as pd
    from . import features as F
    if not games:
        return {}
    df = F.load(eng)
    have = set(df["game_id"].astype(str))
    stubs = []
    for g in games:
        if g["id"] in have:
            continue
        start = pd.Timestamp(g["start"]).tz_convert("UTC")
        local = start.tz_convert("America/New_York" if g["league"] == "nba" else "Europe/Belgrade")
        for tm, op, h in ((g["home"], g["away"], True), (g["away"], g["home"], False)):
            stubs.append({"league": g["league"], "game_id": g["id"], "season": CURRENT_SEASON, "phase": "regular",
                          "start_utc": start, "game_date": pd.Timestamp(local.date()), "status": "future_team",
                          "starter": None, "minutes": None, "position": None, "player_id": f"__team_{tm}",
                          "player_name": None, "team_code": tm, "opp_code": op, "is_home": h})
    if stubs:
        df = F.prep(pd.concat([df.drop(columns=["pos", "playoffs"]), pd.DataFrame(stubs)], ignore_index=True))
    tg = F.team_games(df)
    tg["game_id"] = tg["game_id"].astype(str)
    cur = tg[tg["season"] == CURRENT_SEASON].sort_values("start_utc").groupby(["league", "team_code"]).tail(1)
    ranks = {}
    for lg, x in cur.groupby("league"):
        for col, key, asc in (("t_ortg", "off", False), ("t_drtg", "def", True), ("t_pace", "pace", False)):
            r = x[col].rank(ascending=asc, method="min")
            for tm, v in zip(x["team_code"], r):
                ranks.setdefault((lg, tm), {})[key] = int(v)
        for tm in x["team_code"]:
            ranks[(lg, tm)]["of"] = int(len(x))
    out = {}
    idx = tg.set_index(["game_id", "team_code"])
    for g in games:
        try:
            h, a = idx.loc[(g["id"], g["home"])], idx.loc[(g["id"], g["away"])]
        except KeyError:
            continue
        if isinstance(h, pd.DataFrame) or isinstance(a, pd.DataFrame):
            h, a = (h.iloc[0] if isinstance(h, pd.DataFrame) else h), (a.iloc[0] if isinstance(a, pd.DataFrame) else a)
        pace = h["t_pace"] * a["t_pace"] / h["lg_pace"]
        hp = h["t_ortg"] * a["t_drtg"] / h["lg_rtg"] * pace / 100
        ap = a["t_ortg"] * h["t_drtg"] / h["lg_rtg"] * pace / 100
        hca = 3.0 if g["league"] == "el" else 2.0
        if any(pd.isna(v) for v in (hp, ap, pace)):
            continue
        out[g["id"]] = {"home_pts": round(float(hp + hca / 2), 1), "away_pts": round(float(ap - hca / 2), 1),
                        "total": round(float(hp + ap), 1), "margin_home": round(float(hp - ap + hca), 1),
                        "pace": round(float(pace), 1), "lg_pace": round(float(h["lg_pace"]), 1),
                        "ranks": {g["home"]: ranks.get((g["league"], g["home"])), g["away"]: ranks.get((g["league"], g["away"]))}}
    return out


def injuries_by_team(conn):
    """latest ESPN NBA injury snapshot: team code -> [{name, status, detail}] (NBA only: EuroLeague
    team codes overlap NBA ones, e.g. MIL; EL injuries reach the model through model.injuries_now)"""
    rows = conn.execute(text("""
        SELECT team_code, player_name, status, detail FROM props.injuries
        WHERE league = 'nba' AND fetched_at = (SELECT max(fetched_at) FROM props.injuries WHERE league = 'nba')
          AND status IN ('Out', 'Doubtful', 'Questionable', 'Day-To-Day', 'Suspension')
        ORDER BY team_code, status, player_name""")).mappings().all()
    out = {}
    for r in rows:
        if r["team_code"]:
            out.setdefault(r["team_code"], []).append({"name": r["player_name"], "status": r["status"],
                                                       "detail": r["detail"]})
    return out


def stored_predictions(conn, day):
    rows = conn.execute(text("""
        SELECT DISTINCT ON (league, game_id, player_id, market) league, game_id, player_id, market, created_at, detail
        FROM props.predictions WHERE day = :d ORDER BY league, game_id, player_id, market, created_at DESC"""),
        {"d": day}).mappings().all()
    return {(r["league"], r["game_id"], r["player_id"], r["market"]): dict(r) for r in rows}


def model_summary(bundle):
    """what the page needs from the backtest report: per-market verdicts + calibration"""
    rep, meta = bundle["report"], bundle["meta"]
    mk = {}
    for m, r in rep["markets"].items():
        c = r["chosen"]
        vb = r.get("vs_books", {})
        mk[m] = {"model": c, "dist": meta["markets"][m]["dist"],
                 "test_rmse": r["test"][c]["RMSE"], "test_rmse_l10": r["test"]["L10 average"]["RMSE"],
                 "test_rmse_season": r["test"]["Season average"]["RMSE"],
                 "test_mae": r["test"][c]["MAE"], "test_mae_l10": r["test"]["L10 average"]["MAE"],
                 "synth_ll": r["test_prob_synthetic_line"]["model"]["LogLoss"],
                 "synth_ll_hit": r["test_prob_synthetic_line"]["L10 hit rate"]["LogLoss"],
                 "cover80": r["test_prob_synthetic_line"]["interval_80_coverage"],
                 "books_n": vb.get("props"), "books_ll_model": vb.get("model", {}).get("LogLoss"),
                 "books_ll_book": vb.get("bookmaker no-vig", {}).get("LogLoss"),
                 "books_ll_hit": vb.get("L10 hit rate", {}).get("LogLoss"),
                 "mae_line": vb.get("MAE line vs result"), "mae_model": vb.get("MAE model median vs result"),
                 "over_rate": vb.get("over_rate")}
    return {"version": meta["version"], "trained_through": meta.get("trained_through"), "splits": rep["splits"],
            "minutes": rep["minutes"], "markets": mk, "vs_books": rep.get("vs_books_all"),
            "blend": meta.get("blend")}


def build_day(eng, day):
    with eng.connect() as conn:
        teams, aliases = team_map(conn)
        try:
            nba_games = nba_schedule(day)
            nba_err = None
        except Exception as e:                       # schedule source down: say so, don't invent games
            nba_games, nba_err = [], f"NBA schedule unavailable: {e}"
        games = nba_games + el_schedule(conn, day)
        props = lines_for(conn, day)
        players = {}
        for p in props:
            if p["pid"]:
                players.setdefault(p["league"], set()).add(p["pid"])
        player_logs = logs(conn, players)
        names = {f"{r.league}:{r.player_id}": r.name for r in conn.execute(text("SELECT * FROM props.players"))}
        for k, v in player_logs.items():
            v["name"] = names.get(k)

        # attach each prop to a schedule game (team codes + kickoff within 3 h)
        def code(league, name):
            from .lines import team_code
            return team_code(aliases, {k: f"{v['name']} {v['short'] or ''}" for k, v in teams.items()}, league, name)
        for p in props:
            home, away = p["game_name"].split(" vs ", 1)
            hc, ac, tc = code(p["league"], home), code(p["league"], away), code(p["league"], p["team_name"])
            ko = dt.datetime.fromisoformat(p["kickoff"])
            g = next((g for g in games if g["league"] == p["league"] and {g["home"], g["away"]} == {hc, ac}
                      and abs(dt.datetime.fromisoformat(g["start"]) - ko) < dt.timedelta(hours=3)), None)
            p["game"] = g["id"] if g else None
            p["team"] = tc
            p["opp"] = (ac if tc == hc else hc if tc == ac else None)
            if g and p["opp"] is None and tc in (g["home"], g["away"]):
                p["opp"] = g["away"] if tc == g["home"] else g["home"]
            # game is over -> the actual stat of that game is NOT in the history used for the numbers;
            # the page shows it separately as the result (only when the box score is final).
        # some books (Superbet, Mozzart) send no team with the player: use his team in his latest game
        # before this slate, if that team plays in the prop's game
        for p in props:
            g = next((g for g in games if g["id"] == p["game"]), None)
            if g and p["pid"] and p["team"] not in (g["home"], g["away"]):
                log = (player_logs.get(f"{p['league']}:{p['pid']}") or {}).get("log") or []
                last = next((r for r in log if r[0] < day.isoformat()), None)
                if last and last[2] in (g["home"], g["away"]):
                    p["team"] = last[2]
                    p["opp"] = g["away"] if last[2] == g["home"] else g["home"]
        for p in props:
            g = next((g for g in games if g["id"] == p["game"]), None)
            p["is_home"] = bool(g and p["team"] == g["home"])
        glines = game_lines(conn, day, games, code)
        injuries = injuries_by_team(conn)
        warnings_ = [w for w in [nba_err] if w]
        model_info = None
        try:
            from . import model as M
            bundle = M.load_current()
            model_info = model_summary(bundle)
            with job(eng, "predict") as j:
                made = M.predict(eng, day, props)
                j["rows"], j["message"] = len(made), f"{day}: {len(made)} predictions ({bundle['meta']['version']})"
        except FileNotFoundError:
            warnings_.append("No trained model yet: run python -m players_pm.model train")
        except Exception as e:                       # predictions failing must not take the page down
            warnings_.append(f"Predictions unavailable: {type(e).__name__}: {e}")
        preds = stored_predictions(conn, day)
        for p in props:
            pr = preds.get((p["league"], p["game"], p["pid"], p["market"])) if p["pid"] and p["game"] else None
            if pr and pr["created_at"] <= dt.datetime.fromisoformat(p["kickoff"]):
                p["pred"] = pr["detail"]
        try:
            expect = game_expectations(eng, games)
        except Exception as e:                       # context only: never take the page down
            expect = {}
            warnings_.append(f"Game expectations unavailable: {type(e).__name__}: {e}")
        data = {
            "day": day.isoformat(),
            "generated_at": dt.datetime.now(BEL).isoformat(timespec="seconds"),
            "tz": "Europe/Belgrade",
            "current_season": CURRENT_SEASON,
            "teams": {f"{k[0]}:{k[1]}": v for k, v in teams.items()},
            "games": games,
            "props": props,
            "players": player_logs,
            "defense": defense(conn),
            "freshness": freshness(conn),
            "game_lines": glines,
            "expect": expect,
            "injuries": injuries,
            "model": model_info,
            "warnings": warnings_,
        }
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, f"{day.isoformat()}.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"), default=str)
    return data


def write_index():
    days = sorted(f[:-5] for f in os.listdir(OUT) if f[:4].isdigit() and f.endswith(".json"))
    with open(os.path.join(OUT, "index.json"), "w", encoding="utf-8") as f:
        json.dump({"days": days, "generated_at": dt.datetime.now(BEL).isoformat(timespec="seconds")}, f)


def main():
    eng = engine()
    ensure_schema(eng)
    today = slate_day(dt.datetime.now(BEL))
    days = [dt.date.fromisoformat(a) for a in sys.argv[1:] if a[:4].isdigit()] or [today - dt.timedelta(days=1), today]
    for day in days:
        with job(eng, "export") as j:
            d = build_day(eng, day)
            matched = sum(1 for p in d["props"] if p["pid"])
            j["rows"] = len(d["props"])
            j["message"] = f"{day}: {len(d['games'])} games, {len(d['props'])} props ({matched} with history)"
            print(j["message"])
    write_index()


if __name__ == "__main__":
    main()
