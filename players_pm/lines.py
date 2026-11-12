#!/usr/bin/env python3
"""
Soccerbet player-prop lines for EuroLeague + NBA -> odds.* (insert-only: each run is a new snapshot),
then team/player tables refresh and bookmaker-name -> stats-player matching (props.player_aliases).

Only Soccerbet for now (user's choice, 2026-11-05). Soccerbet's feed is the same one MaxBet and
AdmiralBet resell. The scraper is the existing odds_scrapers/dualsoft.py (public JSON offer API,
0.3 s between player requests). Soccerbet has no official/licensed API; keep the cadence light.

Usage:
  python -m players_pm.lines              # scrape EL + NBA, then match
  python -m players_pm.lines --match-only # just (re)match names against the stats tables
"""

import datetime as dt
import difflib
import re
import sys
import traceback

from sqlalchemy import text

from odds_scrapers import dualsoft
from odds_scrapers.common import save_db

from zoneinfo import ZoneInfo

from .common import engine, ensure_schema, fold, get_json, job, name_key

BOOK = "soccerbet"
BEL = ZoneInfo("Europe/Belgrade")
LEAGUE_IDS = {"el": 2516963, "nba": 2521548}          # Soccerbet league ids (games and players share them)
ODDS_LEAGUE = {"el": ("euroleague", "EuroLeague"), "nba": ("nba", "NBA")}
ESPN_TEAMS = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams"
ESPN_INJURIES = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
SB_BASE = "https://www.soccerbet.rs/restapi/offer/sr"
SB_PARAMS = {"annex": "0", "desc": "true", "mobileVersion": "2.0.0", "locale": "sr"}


def _balanced(over, under):
    """{line: price} ladders -> (line, over, under) with the most even prices, or (None,)*3"""
    both = [(abs(over[k] - under[k]), k) for k in over if k in under]
    if not both:
        return None, None, None
    k = min(both)[1]
    return k, over[k], under[k]


def scrape_game_lines(eng, league):
    """Game total / spread / team totals / moneyline per Soccerbet game -> props.game_lines (snapshot).
    Used for context on the page (implied team total, blowout risk); not a trained model input yet,
    because there is no history of these lines before 2026-11-05."""
    with job(eng, "game_lines", league) as j:
        picks = get_json(f"{SB_BASE}/ttg_lang", SB_PARAMS)["betPickMap"]
        games = [g for g in get_json(f"{SB_BASE}/sport/B/mob", SB_PARAMS)["esMatches"]
                 if g["leagueId"] == LEAGUE_IDS[league]]
        n = 0
        with eng.begin() as conn:
            for g in games:
                bm = get_json(f"{SB_BASE}/match/{g['id']}", SB_PARAMS, min_gap=0.3).get("betMap", {})
                lad = {}
                for code, svs in bm.items():
                    name = (picks.get(f"{code}_B") or {}).get("tipTypeName")
                    for sv, o in svs.items():
                        if o.get("ov") and o.get("s") == "U":
                            val = None
                            if "=" in sv:
                                val = float(sv.split("=")[-1])
                            lad.setdefault(name, {})[val] = float(o["ov"])
                total, t_o, t_u = _balanced(lad.get("OT_OVER", {}), lad.get("OT_UNDER", {}))
                spread, s_h, s_a = _balanced(lad.get("OT_H1", {}), lad.get("OT_H2", {}))
                ht = next(iter(lad.get("OT_H_OVER", {})), None)
                at = next(iter(lad.get("OT_A_OVER", {})), None)
                ml1 = lad.get("FT_OT_1", {}).get(None)
                ml2 = lad.get("FT_OT_2", {}).get(None)
                if total is None and spread is None and ml1 is None:
                    continue
                conn.execute(text("""
                    INSERT INTO props.game_lines (league, book, book_game, home, away, kickoff, total, total_over,
                        total_under, spread_home, spread_home_odds, spread_away_odds, home_total, away_total, ml_home, ml_away)
                    VALUES (:lg, :b, :id, :h, :a, :k, :t, :to, :tu, :s, :sh, :sa, :ht, :at, :m1, :m2)"""),
                    {"lg": league, "b": BOOK, "id": str(g["id"]), "h": g["home"], "a": g["away"],
                     "k": dt.datetime.fromtimestamp(g["kickOffTime"] / 1000, BEL).replace(tzinfo=None),
                     "t": total, "to": t_o, "tu": t_u, "s": spread, "sh": s_h, "sa": s_a,
                     "ht": ht, "at": at, "m1": ml1, "m2": ml2})
                n += 1
        j["rows"], j["message"] = n, f"{n} games with game lines"
        print(f"  {league}: game lines for {n} games")


def snapshot_injuries(eng):
    """ESPN NBA injury report -> props.injuries (one snapshot per run). EuroLeague: no public feed."""
    with job(eng, "injuries", "nba") as j:
        now = dt.datetime.now(dt.timezone.utc)
        teams = get_json(ESPN_INJURIES)["injuries"]
        with eng.connect() as conn:
            codes = {r.name: r.code for r in conn.execute(text("SELECT code, name FROM props.teams WHERE league = 'nba'"))}
        n = 0
        with eng.begin() as conn:
            for t in teams:
                for i in t.get("injuries", []):
                    a = i.get("athlete", {})
                    pid = next((re.search(r"/id/(\d+)", l.get("href", "")).group(1) for l in a.get("links", [])
                                if re.search(r"/id/(\d+)", l.get("href", ""))), None)
                    if not pid:
                        continue
                    conn.execute(text("""INSERT INTO props.injuries VALUES (:f, 'nba', :p, :n, :t, :s, :d, :r)
                                         ON CONFLICT DO NOTHING"""),
                                 {"f": now, "p": pid, "n": a.get("displayName"), "t": codes.get(t.get("displayName")),
                                  "s": i.get("status"), "d": (i.get("shortComment") or "")[:500],
                                  "r": i.get("date")})
                    n += 1
        j["rows"], j["message"] = n, f"{n} players on the NBA injury report"
        print(f"  injuries: {n} NBA players listed")


def scrape(eng, league):
    with job(eng, "lines", league) as j:
        dualsoft.BOOKS[BOOK]["league"] = LEAGUE_IDS[league]
        rows, games = dualsoft.scrape(BOOK)
        if not rows:
            j["rows"], j["message"] = 0, "Soccerbet lists no player props for this league right now"
            print(f"  {league}: no props listed")
            return
        run_id, n = save_db(BOOK, ODDS_LEAGUE[league][0], rows, games, ODDS_LEAGUE[league][1])
        j["rows"], j["message"] = n, f"odds run {run_id}, {len(games)} games"
        print(f"  {league}: run {run_id}, {n} prices, {len(games)} games")


def refresh_nba_teams(eng):
    teams = get_json(ESPN_TEAMS)["sports"][0]["leagues"][0]["teams"]
    with eng.begin() as conn:
        conn.execute(text("DELETE FROM props.team_aliases WHERE league = 'nba'"))
        for t in (x["team"] for x in teams):
            logo = (t.get("logos") or [{}])[0].get("href")
            conn.execute(text("""INSERT INTO props.teams VALUES ('nba', :c, :n, :s, :l)
                                 ON CONFLICT (league, code) DO UPDATE SET name = EXCLUDED.name,
                                 short = EXCLUDED.short, logo = EXCLUDED.logo"""),
                         {"c": t["abbreviation"], "n": t["displayName"], "s": t["shortDisplayName"], "l": logo})
            # no bare location ("Los Angeles" is two teams); "Los Angeles Clippers" for ESPN's "LA Clippers"
            full = f"{t.get('location')} {t.get('name')}".replace("LA ", "Los Angeles ")
            for alias in {t["displayName"], t["shortDisplayName"], t["abbreviation"], t.get("name"),
                          f"{t.get('location')} {t.get('name')}", full}:
                if alias:
                    conn.execute(text("""INSERT INTO props.team_aliases VALUES ('nba', :a, :c)
                                         ON CONFLICT (league, alias) DO NOTHING"""),
                                 {"a": " ".join(fold(alias)), "c": t["abbreviation"]})


def refresh_players(eng):
    """props.players = every player in the game log, with the name/team of his most recent game."""
    with eng.begin() as conn:
        conn.execute(text("""
            INSERT INTO props.players (league, player_id, name, team_code, last_game)
            SELECT DISTINCT ON (league, player_id) league, player_id, player_name, team_code, game_date
            FROM props.player_games ORDER BY league, player_id, start_utc DESC
            ON CONFLICT (league, player_id) DO UPDATE SET name = EXCLUDED.name,
                team_code = EXCLUDED.team_code, last_game = EXCLUDED.last_game"""))
        # EL box scores write 'LAST, FIRST' in capitals -> 'First Last'
        conn.execute(text("""
            UPDATE props.players SET name = initcap(trim(split_part(name, ',', 2))) || ' ' ||
                                            initcap(trim(split_part(name, ',', 1)))
            WHERE league = 'el' AND name LIKE '%,%'"""))


def team_code(aliases, names, league, book_team):
    key = " ".join(fold(book_team))
    if not key:
        return None
    if (league, key) in aliases:
        return aliases[(league, key)]
    words = set(key.split()) - {"bc", "kk", "fc", "bk", "sk", "basket", "basketball"}
    # shared words weighted by rarity: "clippers" (1 team) beats "los" + "angeles" (2 teams each)
    team_words = {code: set(fold(name)) for (lg, code), name in names.items() if lg == league}
    freq = {}
    for ws in team_words.values():
        for w in ws:
            freq[w] = freq.get(w, 0) + 1
    scored = sorted(((sum(1 / freq[w] ** 2 for w in words & ws), code) for code, ws in team_words.items()),
                    reverse=True)
    if not scored or scored[0][0] == 0 or (len(scored) > 1 and scored[0][0] == scored[1][0]):
        return None                                   # no or ambiguous match: leave unresolved
    return scored[0][1]


def match(eng, books=None, days=14):
    """Book name + team -> stats player id. 'manual' rows are never overwritten.
    books=None: the active books (props.bookmakers); a list: those books (used by the model backtest
    to match the older multi-book lines)."""
    with eng.connect() as conn:
        aliases = {(r.league, r.alias): r.code for r in conn.execute(text("SELECT * FROM props.team_aliases"))}
        names = {(r.league, r.code): f"{r.name} {r.short or ''}" for r in conn.execute(text("SELECT * FROM props.teams"))}
        # candidates: everyone with a game since the start of last season, with each team he played for
        cands = {}
        for r in conn.execute(text("""SELECT league, player_id, player_name, team_code, max(start_utc) AS last
                                      FROM props.player_games GROUP BY 1, 2, 3, 4""")):
            nm = r.player_name
            if "," in nm:
                last, _, first = nm.partition(",")
                nm = f"{first} {last}"
            c = cands.setdefault((r.league, r.player_id), {"key": name_key(nm), "teams": {}})
            c["teams"][r.team_code] = r.last
        # every name the active books used in the last 14 days (so past slate days stay matched too)
        todo = conn.execute(text("""
            SELECT DISTINCT ON (lg.league, p.source, p.player) lg.league, p.source, p.player, p.team
            FROM odds.player_props p
            JOIN odds.scrape_runs r ON r.run_id = p.run_id
            JOIN props.leagues lg ON lg.odds_league_id = r.league_id
            WHERE r.scraped_at > now() - make_interval(days => :days)
              AND (CAST(:books AS text[]) IS NULL AND p.source IN (SELECT book FROM props.bookmakers WHERE active)
                   OR p.source = ANY(CAST(:books AS text[])))
            ORDER BY lg.league, p.source, p.player, r.run_id DESC"""), {"days": days, "books": books}).fetchall()
        manual = {(r.league, r.book, r.book_name) for r in
                  conn.execute(text("SELECT league, book, book_name FROM props.player_aliases WHERE method = 'manual'"))}

    results, unmatched = [], []
    for lg, book, player, team in todo:
        if (lg, book, player) in manual:
            continue
        code = team_code(aliases, names, lg, team)
        key = name_key(player)
        toks = key.split()
        pool = [(pid, c) for (l, pid), c in cands.items() if l == lg]
        on_team = [(pid, c) for pid, c in pool if code in c["teams"]]

        found, method, score = None, None, None
        for scope, label in ((on_team, ""), (pool, "-other-team")):
            hits = [pid for pid, c in scope if c["key"] == key]
            if len(hits) == 1:
                found, method, score = hits[0], "exact" + label, 1.0
                break
            if not toks:
                break
            # surname + first initial ("N. Jokic", "Nate/Nathan")
            hits = [pid for pid, c in scope if c["key"].split()[-1:] == toks[-1:]
                    and c["key"][:1] == toks[0][:1]]
            if len(hits) == 1:
                found, method, score = hits[0], "initial" + label, 0.9
                break
            if scope is on_team:   # surname unique on the team ("Aleksander"/"Olek" Balcerowski)
                hits = [pid for pid, c in scope if c["key"].split()[-1:] == toks[-1:]]
                if len(hits) == 1:
                    found, method, score = hits[0], "surname-team", 0.8
                    break
            if scope is on_team:   # fuzzy only within the team: too risky league-wide
                best = max(((difflib.SequenceMatcher(None, key, c["key"]).ratio(), pid) for pid, c in scope),
                           default=(0, None))
                if best[0] >= 0.85:
                    found, method, score = best[1], "fuzzy", round(best[0], 3)
                    break
        if not found:
            unmatched.append(f"{lg}: {player} ({team})")
        results.append({"l": lg, "b": book, "n": player, "t": team, "p": found, "m": method, "s": score})

    with eng.begin() as conn:
        for r in results:
            conn.execute(text("""
                INSERT INTO props.player_aliases (league, book, book_name, book_team, player_id, method, score)
                VALUES (:l, :b, :n, :t, :p, :m, :s)
                ON CONFLICT (league, book, book_name) DO UPDATE SET book_team = EXCLUDED.book_team,
                    player_id = EXCLUDED.player_id, method = EXCLUDED.method, score = EXCLUDED.score,
                    last_seen = now()"""), r)
    return results, unmatched


def main():
    eng = engine()
    ensure_schema(eng)
    if "--match-only" not in sys.argv:
        for league in ("el", "nba"):
            for fn in (scrape, scrape_game_lines):
                try:
                    fn(eng, league)
                except Exception:
                    traceback.print_exc()          # logged as failed in props.job_runs; keep going
    try:
        snapshot_injuries(eng)
    except Exception:
        traceback.print_exc()
    refresh_nba_teams(eng)
    refresh_players(eng)
    with job(eng, "match") as j:
        results, unmatched = match(eng)
        j["rows"] = len(results) - len(unmatched)
        j["message"] = f"{len(unmatched)} unmatched: " + "; ".join(unmatched)[:1800] if unmatched else "all matched"
        j["status"] = "partial" if unmatched else "ok"
    print(f"Matched {len(results) - len(unmatched)}/{len(results)} book names")
    for u in unmatched:
        print("  unmatched:", u)


if __name__ == "__main__":
    main()
