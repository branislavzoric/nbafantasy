#!/usr/bin/env python3
"""
NBA play-by-play -> lineups (stints) and per-player on-court numbers.

For every final NBA game (regular season, play-in, playoffs; no preseason / All-Star) this:
  1. caches the ESPN summary (header, boxscore, plays, injuries) gzipped in "Players PM/raw/nba/<id>.json.gz"
     (a copy already saved by collect_boxscores.py in "Box Scores/<date>/" is reused, not re-fetched);
  2. rebuilds which 5 players each team had on the floor at every moment from the substitutions
     ("X enters the game for Y", participants = [in, out]). Period starters are not logged after Q1,
     so they are inferred: players whose first action in the period is anything except being subbed in;
  3. writes props.nba_stints (one row per unchanged 5-man unit) and props.player_game_adv:
     exact seconds played, team possessions while on, usage % = player (FGA + 0.44 FTA + TOV) / team's
     while he was on, rebound chances (missed FG + missed live last FT, own team = offensive, opponent =
     defensive), teammates' made FG while on (assist-chance proxy);
  4. repairs box scores where ESPN lists a player with no athlete id and blank stats (e.g. Lachlan
     Olbrich, CHI 2025-26): his line is rebuilt from the play-by-play and stored with box_source = 'pbp'.

Nothing is estimated: every number is counted from the official play-by-play. Games whose rebuilt
minutes don't match the box score within 90 s per player are kept but flagged (pbp_ok = false).

Usage:
  python -m players_pm.pbp                 # new games only (no player_game_adv rows yet)
  python -m players_pm.pbp --all           # rebuild everything from the cache (fetches what's missing)
  python -m players_pm.pbp --game 401810560 [--verbose]
"""

import gzip
import json
import os
import re
import sys
from collections import defaultdict

from sqlalchemy import text

from .common import PM_DIR, PROJECT_DIR, engine, ensure_schema, get_json, job

SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary"
ATHLETE_URL = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{}"
CACHE_DIR = os.path.join(PM_DIR, "raw", "nba")
BOX_DIR = os.path.join(PROJECT_DIR, "Box Scores")
KEEP = ("header", "boxscore", "plays", "injuries")
NOT_ON_COURT = ("Technical", "Ejection", "Timeout", "Coach", "Delay", "End Period", "End Game",
                "Substitution", "Jumpball Violation")
LIVE_FT = re.compile(r"Free Throw - (\d) of (\d)$")


# ------------------------------------------------------------------------------------------ fetch
def load_summary(game_id, game_date):
    path = os.path.join(CACHE_DIR, f"{game_id}.json.gz")
    if os.path.exists(path):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    local = os.path.join(BOX_DIR, str(game_date), f"{game_id}.json")
    if os.path.exists(local):
        with open(local, encoding="utf-8") as f:
            s = json.load(f)
    else:
        s = get_json(SUMMARY_URL, {"event": game_id}, min_gap=0.3)
    s = {k: s.get(k) for k in KEEP}
    if not s.get("plays"):
        return s                                   # don't cache an empty summary; retry next run
    os.makedirs(CACHE_DIR, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(s, f)
    return s


# ------------------------------------------------------------------------------------------ helpers
def period_len(p):
    return 720 if p <= 4 else 300


def period_start(p):
    return (p - 1) * 720 if p <= 4 else 2880 + (p - 5) * 300


def clock_s(display):
    d = (display or "0").strip()
    if ":" in d:
        m, s = d.split(":")
        return int(m) * 60 + float(s)
    return float(d)


def elapsed(play):
    p = play["period"]["number"]
    return period_start(p) + period_len(p) - clock_s(play["clock"]["displayValue"])


def ptype(play):
    return (play.get("type") or {}).get("text") or ""


def pids(play):
    return [str(x["athlete"]["id"]) for x in play.get("participants") or [] if (x.get("athlete") or {}).get("id")]


def is_fg(play):
    return bool(play.get("shootingPlay")) and "Free Throw" not in ptype(play)


def is_ft(play):
    return "Free Throw" in ptype(play)


def is_live_ft(play):
    m = LIVE_FT.search(ptype(play))
    return bool(m) and m.group(1) == m.group(2)


def is_tov(play):
    return "Turnover" in ptype(play)


def is_foul(play):
    t = ptype(play)
    return "Foul" in t and "Technical" not in t and "Turnover" not in t


# ------------------------------------------------------------------------------------------ parse
def roster(summary):
    """team id -> abbreviation; player id -> {team, starter, min, name}; id-less box entries per team"""
    teams, players, idless = {}, {}, defaultdict(list)
    for tb in summary["boxscore"].get("players") or []:
        abbr, tid = tb["team"]["abbreviation"], str(tb["team"]["id"])
        teams[tid] = abbr
        for st in tb["statistics"]:
            keys = st.get("keys") or []
            for a in st["athletes"]:
                ath = a.get("athlete") or {}
                stats = dict(zip(keys, a.get("stats") or []))
                mins = stats.get("minutes")
                mins = int(mins) if mins and mins.isdigit() else None
                if not ath.get("id"):
                    idless[abbr].append(ath.get("shortName") or ath.get("displayName") or "?")
                    continue
                players[str(ath["id"])] = {"team": abbr, "starter": bool(a.get("starter")), "min": mins,
                                           "name": ath.get("displayName")}
    return teams, players, idless


def parse_game(summary):
    teams, box, idless = roster(summary)
    # sequenceNumber is not chronological (ESPN appends late corrections), so order by game clock first
    plays = sorted(summary.get("plays") or [], key=lambda p: (round(elapsed(p), 1), int(p.get("sequenceNumber") or 0)))
    abbrs = list(teams.values())
    team_of = {pid: v["team"] for pid, v in box.items()}
    issues = []

    # players in the play-by-play but not in the box (ESPN id-less entries): team from the plays
    for p in plays:
        tid = str((p.get("team") or {}).get("id") or "")
        ids = pids(p)
        if ptype(p) == "Substitution" and tid in teams:
            for x in ids:
                team_of.setdefault(x, teams[tid])
        elif ids and tid in teams and ids[0] not in team_of and not is_foul(p):
            team_of[ids[0]] = teams[tid]
    unknown = sorted(x for x in team_of if x not in box)

    periods = sorted({p["period"]["number"] for p in plays})
    lineup = {}
    prev_end = {a: set() for a in abbrs}
    stints = []                    # dicts: team, period, start, end, players
    events = []                    # (elapsed, play, {team: frozenset lineup})
    for per in periods:
        pp = [p for p in plays if p["period"]["number"] == per]
        first = {}                 # player -> 'in' / 'out' / 'act'
        order = {}
        for i, p in enumerate(pp):
            ids = pids(p)
            if ptype(p) == "Substitution" and len(ids) >= 2:
                first.setdefault(ids[0], "in"); order.setdefault(ids[0], i)
                first.setdefault(ids[1], "out"); order.setdefault(ids[1], i)
            elif not any(k in ptype(p) for k in NOT_ON_COURT):
                for x in ids:
                    first.setdefault(x, "act"); order.setdefault(x, i)
        for a in abbrs:
            if per == 1:
                start = {x for x, v in box.items() if v["team"] == a and v["starter"]}
            else:
                start = {x for x, f in first.items() if f != "in" and team_of.get(x) == a}
            if len(start) > 5:
                issues.append(f"P{per} {a}: {len(start)} period starters found, kept the first 5 to act")
                start = set(sorted(start, key=lambda x: order.get(x, 1e9))[:5])
            if len(start) < 5:
                # someone played silently (no event, no sub): prefer last period's closers, then most minutes
                fill = [x for x in prev_end[a] if x not in start and first.get(x) != "in"]
                fill += sorted((x for x, v in box.items() if v["team"] == a and x not in start
                                and x not in fill and first.get(x) is None and (v["min"] or 0) > 0),
                               key=lambda x: -(box[x]["min"] or 0))
                need = 5 - len(start)
                start |= set(fill[:need])
                if per > 1 and need:
                    issues.append(f"P{per} {a}: {need} silent starter(s) inferred")
            if len(start) != 5:
                issues.append(f"P{per} {a}: lineup has {len(start)} players")
            lineup[a] = set(start)

        t0 = period_start(per)
        open_at = {a: t0 for a in abbrs}
        last_seen = {}                     # player -> elapsed time of his last action / sub-in

        def close(a, t):
            if t > open_at[a]:
                stints.append({"team": a, "period": per, "start": open_at[a], "end": t,
                               "players": frozenset(lineup[a])})
                open_at[a] = t

        def bench_idlest(a, keep):
            """ESPN sometimes drops substitutions: take off the player idle the longest"""
            cand = [x for x in lineup[a] if x not in keep]
            if cand:
                lineup[a].discard(min(cand, key=lambda x: last_seen.get(x, -1)))

        for p in pp:
            t = min(max(elapsed(p), t0), t0 + period_len(per))
            ids = pids(p)
            if ptype(p) == "Substitution" and len(ids) >= 2:
                a = team_of.get(ids[0]) or team_of.get(ids[1])
                if a not in lineup:
                    issues.append(f"sub with unknown team {ids}")
                    continue
                close(a, t)
                if ids[0] in lineup[a]:
                    issues.append(f"P{per} {a}: {ids[0]} subbed in but already on the floor")
                    lineup[a].discard(ids[1])
                    while len(lineup[a]) > 5:
                        bench_idlest(a, {ids[0]})
                    continue
                if ids[1] in lineup[a]:
                    lineup[a].discard(ids[1])
                else:
                    issues.append(f"P{per} {a}: {ids[1]} subbed out but not on the floor")
                    if len(lineup[a]) >= 5:
                        bench_idlest(a, set())
                lineup[a].add(ids[0])
                last_seen[ids[0]] = t
            else:
                if not any(k in ptype(p) for k in NOT_ON_COURT):
                    for x in ids:
                        a = team_of.get(x)
                        if a in lineup and x not in lineup[a]:
                            issues.append(f"P{per} {a}: {x} acts at {p['clock']['displayValue']} without a logged sub-in")
                            close(a, t)
                            bench_idlest(a, set(ids))
                            lineup[a].add(x)
                        last_seen[x] = t
                events.append((t, p, {a: frozenset(lineup[a]) for a in abbrs}))
        for a in abbrs:
            end = t0 + period_len(per)
            if end > open_at[a]:
                stints.append({"team": a, "period": per, "start": open_at[a], "end": end,
                               "players": frozenset(lineup[a])})
            prev_end[a] = set(lineup[a])
    return {"teams": teams, "abbrs": abbrs, "box": box, "team_of": team_of, "unknown": unknown,
            "idless": idless, "stints": stints, "events": events, "issues": issues}


def team_counts():
    return defaultdict(lambda: {"pts": 0, "fga": 0, "fgm": 0, "fta": 0, "tov": 0, "oreb": 0, "dreb": 0, "miss": 0})


def compute(g):
    """stint rows + per-player rows from a parsed game"""
    teams, abbrs, team_of = g["teams"], g["abbrs"], g["team_of"]
    opp = {a: next((b for b in abbrs if b != a), None) for a in abbrs}

    # per-event attribution
    player = defaultdict(lambda: {"sec": 0.0, "use": 0.0, "team_use": 0.0, "poss": 0.0, "orc": 0, "drc": 0,
                                  "tm_fgm": 0, "tm_fga": 0, "opp_fga": 0})
    stint_stats = defaultdict(team_counts)          # (team, players) key per stint index
    stints = sorted(g["stints"], key=lambda s: (s["team"], s["start"]))
    by_team = defaultdict(list)
    for s in stints:
        by_team[s["team"]].append(s)

    def stint_index(a, t, players):
        for i, s in enumerate(by_team[a]):
            if s["players"] == players and s["start"] <= t <= s["end"]:
                return i
        return None

    for t, p, lu in g["events"]:
        tid = str((p.get("team") or {}).get("id") or "")
        a = teams.get(tid)
        if a is None:
            continue
        ids = pids(p)
        typ = ptype(p)
        c = {"pts": 0, "fga": 0, "fgm": 0, "fta": 0, "tov": 0, "oreb": 0, "dreb": 0, "miss": 0}
        if p.get("scoringPlay"):
            c["pts"] = int(p.get("scoreValue") or 0)
        if is_fg(p):
            c["fga"] = 1
            c["fgm"] = int(bool(p.get("scoringPlay")))
            c["miss"] = 1 - c["fgm"]
        elif is_ft(p) and "Technical" not in typ:
            c["fta"] = 1
            c["miss"] = int(is_live_ft(p) and not p.get("scoringPlay"))
        elif is_ft(p):
            c["fta"] = 1
        if is_tov(p):
            c["tov"] = 1
        if typ == "Offensive Rebound":
            c["oreb"] = 1
        elif typ == "Defensive Rebound":
            c["dreb"] = 1
        if not any(c.values()):
            continue
        for side in (a, opp[a]):
            i = stint_index(side, t, lu[side])
            if i is None:
                continue
            key = (side, i)
            for k, v in c.items():
                stint_stats[key][("for_" if side == a else "against_") + k] = \
                    stint_stats[key].get(("for_" if side == a else "against_") + k, 0) + v
        # players on the floor
        use_ev = c["fga"] + 0.44 * c["fta"] + c["tov"]
        actor = ids[0] if ids else None
        for x in lu[a]:
            q = player[(a, x)]
            q["team_use"] += use_ev
            q["orc"] += c["miss"]
            q["tm_fga"] += c["fga"] if x != actor else 0
            q["tm_fgm"] += c["fgm"] if x != actor else 0
            if x == actor:
                q["use"] += use_ev
        for x in lu[opp[a]]:
            q = player[(opp[a], x)]
            q["drc"] += c["miss"]
            q["opp_fga"] += c["fga"]

    stint_rows = []
    for a in abbrs:
        for i, s in enumerate(by_team[a]):
            st = stint_stats.get((a, i), {})
            f = lambda k: st.get(k, 0)
            poss_f = f("for_fga") - f("for_oreb") + f("for_tov") + 0.44 * f("for_fta")
            poss_a = f("against_fga") - f("against_oreb") + f("against_tov") + 0.44 * f("against_fta")
            stint_rows.append({"team": a, "stint_no": i + 1, "period": s["period"], "start_s": round(s["start"]),
                               "end_s": round(s["end"]), "players": sorted(s["players"]),
                               "pts_for": f("for_pts"), "pts_against": f("against_pts"),
                               "poss_for": round(poss_f, 2), "poss_against": round(poss_a, 2)})
            for x in s["players"]:
                q = player[(a, x)]
                q["sec"] += s["end"] - s["start"]
                q["poss"] += poss_f
    return stint_rows, player


def player_rows(g, player):
    rows = []
    for (a, x), q in player.items():
        bm = (g["box"].get(x) or {}).get("min")
        sec_box = bm * 60 if bm is not None else None
        ok = sec_box is not None and abs(q["sec"] - sec_box) <= 90
        rows.append({"player_id": x, "team": a, "sec_played": round(q["sec"]), "sec_box": sec_box,
                     "poss_on": round(q["poss"], 2), "player_use": round(q["use"], 2),
                     "team_use_on": round(q["team_use"], 2),
                     "usg_pct": round(100 * q["use"] / q["team_use"], 2) if q["team_use"] > 0 else None,
                     "oreb_chances": q["orc"], "dreb_chances": q["drc"], "tm_fgm_on": q["tm_fgm"],
                     "tm_fga_on": q["tm_fga"], "opp_fga_on": q["opp_fga"], "pbp_ok": ok})
    return rows


# ------------------------------------------------------------------------------------------ box repair
def rebuild_line(summary, g, pid, sec):
    """box line for a player ESPN listed without an athlete id, counted from the play-by-play"""
    s = {k: 0 for k in ("pts", "fgm", "fga", "tpm", "tpa", "ftm", "fta", "oreb", "dreb", "ast", "stl", "blk", "tov", "pf")}
    for p in summary["plays"]:
        ids = pids(p)
        if not ids:
            continue
        typ, txt = ptype(p), (p.get("text") or "").lower()
        if ids[0] == pid:
            if p.get("scoringPlay"):
                s["pts"] += int(p.get("scoreValue") or 0)
            if is_fg(p):
                s["fga"] += 1
                s["fgm"] += int(bool(p.get("scoringPlay")))
                if int(p.get("pointsAttempted") or 0) == 3:
                    s["tpa"] += 1
                    s["tpm"] += int(bool(p.get("scoringPlay")))
            elif is_ft(p):
                s["fta"] += 1
                s["ftm"] += int(bool(p.get("scoringPlay")))
            if typ == "Offensive Rebound":
                s["oreb"] += 1
            elif typ == "Defensive Rebound":
                s["dreb"] += 1
            if is_tov(p):
                s["tov"] += 1
            if is_foul(p):
                s["pf"] += 1
        if len(ids) > 1 and ids[1] == pid:
            if p.get("scoringPlay") and "assist" in txt:
                s["ast"] += 1
            if is_tov(p) and "steal" in txt:
                s["stl"] += 1
            if is_fg(p) and "block" in txt:
                s["blk"] += 1
    s["reb"] = s["oreb"] + s["dreb"]
    s["min"] = round(sec / 60)
    return s


def athlete_name(pid):
    try:
        j = get_json(ATHLETE_URL.format(pid), min_gap=0.3)
        return (j.get("athlete") or {}).get("displayName")
    except Exception:
        return None


def repair_box(conn, summary, g, player, game):
    """insert box rows for id-less players (only when ESPN's box really has an id-less entry for that team)"""
    from collect_boxscores import fantasy_points
    out = []
    for pid in g["unknown"]:
        a = g["team_of"][pid]
        if not g["idless"].get(a):
            continue
        sec = player.get((a, pid), {}).get("sec", 0)
        line = rebuild_line(summary, g, pid, sec)
        if sec <= 0 and not any(v for k, v in line.items() if k != "min"):
            continue
        name = athlete_name(pid) or g["idless"][a][0]
        row = {"game_id": game["game_id"], "player_id": pid, "player_name": name, "team": a,
               "opponent": next(b for b in g["abbrs"] if b != a), "game_date": game["game_date"],
               "season_type": game["season_type"], "position": None, "starter": False,
               "did_not_play": sec <= 0, "dnp_reason": None, "plus_minus": None, **line}
        row["fpts"] = fantasy_points(row)
        conn.execute(text("""
            INSERT INTO public.nba_boxscores (game_id, player_id, player_name, team, opponent, game_date, season_type,
                position, starter, did_not_play, dnp_reason, min, pts, fgm, fga, tpm, tpa, ftm, fta, oreb, dreb, reb,
                ast, stl, blk, tov, pf, plus_minus, fpts, fetched_at, box_source)
            VALUES (:game_id, :player_id, :player_name, :team, :opponent, :game_date, :season_type, :position,
                :starter, :did_not_play, :dnp_reason, :min, :pts, :fgm, :fga, :tpm, :tpa, :ftm, :fta, :oreb, :dreb,
                :reb, :ast, :stl, :blk, :tov, :pf, :plus_minus, :fpts, now(), 'pbp')
            ON CONFLICT (game_id, player_id) DO NOTHING"""), row)
        g["box"][pid] = {"team": a, "starter": False, "min": line["min"], "name": name}
        out.append((name, line["min"], line["pts"]))
    return out


# ------------------------------------------------------------------------------------------ main
def games_todo(eng, everything, only=None):
    sql = """
        SELECT g.game_id, g.game_date, g.season_type FROM public.nba_games g
        WHERE g.status = 'STATUS_FINAL' AND g.season_type <> 'preseason'
          AND EXISTS (SELECT 1 FROM props.teams t WHERE t.league='nba' AND t.code=g.home_team)
          AND EXISTS (SELECT 1 FROM props.teams t WHERE t.league='nba' AND t.code=g.away_team)"""
    if only:
        sql += " AND g.game_id = :only"
    elif not everything:
        sql += " AND NOT EXISTS (SELECT 1 FROM props.player_game_adv a WHERE a.game_id = g.game_id)"
    sql += " ORDER BY g.game_date, g.game_id"
    with eng.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(text(sql), {"only": only})]


def process(eng, game, verbose=False):
    summary = load_summary(game["game_id"], game["game_date"])
    if not summary.get("plays"):
        return {"game_id": game["game_id"], "error": "no play-by-play"}
    g = parse_game(summary)
    stint_rows, player = compute(g)
    with eng.begin() as conn:
        repaired = repair_box(conn, summary, g, player, game) if g["unknown"] else []
        rows = player_rows(g, player)
        conn.execute(text("DELETE FROM props.nba_stints WHERE game_id = :g"), {"g": game["game_id"]})
        conn.execute(text("DELETE FROM props.player_game_adv WHERE game_id = :g"), {"g": game["game_id"]})
        if stint_rows:
            conn.execute(text("""INSERT INTO props.nba_stints (game_id, team, stint_no, period, start_s, end_s, players,
                                 pts_for, pts_against, poss_for, poss_against)
                                 VALUES (:game_id, :team, :stint_no, :period, :start_s, :end_s, :players, :pts_for,
                                 :pts_against, :poss_for, :poss_against)"""),
                         [{**r, "game_id": game["game_id"]} for r in stint_rows])
        if rows:
            conn.execute(text("""INSERT INTO props.player_game_adv (game_id, player_id, team, sec_played, sec_box, poss_on,
                                 player_use, team_use_on, usg_pct, oreb_chances, dreb_chances, tm_fgm_on, tm_fga_on,
                                 opp_fga_on, pbp_ok)
                                 VALUES (:game_id, :player_id, :team, :sec_played, :sec_box, :poss_on, :player_use,
                                 :team_use_on, :usg_pct, :oreb_chances, :dreb_chances, :tm_fgm_on, :tm_fga_on,
                                 :opp_fga_on, :pbp_ok)"""),
                         [{**r, "game_id": game["game_id"]} for r in rows])
    bad = [r for r in rows if not r["pbp_ok"] and r["sec_box"] is not None]
    if verbose:
        for i in g["issues"]:
            print("   ", i)
        for r in bad:
            print(f"    minutes mismatch {r['player_id']}: pbp {r['sec_played']}s vs box {r['sec_box']}s")
    return {"game_id": game["game_id"], "stints": len(stint_rows), "players": len(rows), "bad": len(bad),
            "issues": len(g["issues"]), "repaired": repaired}


def main():
    eng = engine()
    ensure_schema(eng)
    only = sys.argv[sys.argv.index("--game") + 1] if "--game" in sys.argv else None
    todo = games_todo(eng, "--all" in sys.argv, only)
    print(f"{len(todo)} games to process", flush=True)
    with job(eng, "nba_pbp", "nba") as j:
        done = bad_games = 0
        errors, repaired = [], []
        for n, game in enumerate(todo, 1):
            try:
                r = process(eng, game, verbose="--verbose" in sys.argv or bool(only))
            except Exception as e:                     # one broken game must not stop the backfill
                errors.append(f"{game['game_id']}: {type(e).__name__}: {e}")
                continue
            if r.get("error"):
                errors.append(f"{game['game_id']}: {r['error']}")
                continue
            done += 1
            bad_games += r["bad"] > 0
            for x in r["repaired"]:
                repaired.append(f"{game['game_id']} {x[0]} {x[1]} min {x[2]} pts")
            if n % 50 == 0 or only:
                print(f"  {n}/{len(todo)} {game['game_date']} {game['game_id']}: {r}", flush=True)
        j["rows"] = done
        j["message"] = json.dumps({"games": done, "games_with_minute_mismatch": bad_games,
                                   "repaired_rows": repaired, "errors": errors[:20]})
        print(j["message"])


if __name__ == "__main__":
    main()
