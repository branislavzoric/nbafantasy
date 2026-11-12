#!/usr/bin/env python3
"""
Expected usage rate (xUSG%) for every player on a slate, NBA + EuroLeague.

Usage rate = share of the team's possessions a player ends while on the floor:
    USG% = 100 * (FGA + 0.44*FTA + TOV) * (team minutes / 5) / (MIN * (team FGA + 0.44*team FTA + team TOV))
NBA games with clean play-by-play use the exact on-court figure (props.player_game_adv.usg_pct).

Model (chosen by the backtest below, `python -m players_pm.usage backtest`):
  1. Baseline = minutes-weighted, exponentially decayed average of the player's past usage
     (half-life HL games; last season's games count PREV_W as much), shrunk toward the league mean
     with a prior worth PRIOR_MIN minutes. Low-minute games are noisy, so they weigh little.
  2. Team constraint: five players on the floor always use exactly 100%, so
         sum_i usage_i * minutes_i = 100% * 48 min (x team minutes / 240).
     With tonight's expected minutes (model min_hat for prop players, recent minutes for the rest,
     scaled to 240), the usage left behind by absent players is handed out in proportion to
     usage_i^ALPHA * minutes_i (ALPHA > 1: the main creators absorb more of it than role players).
  3. The result is clamped to [5, 45]%.

Pre-game only: every input is from earlier games, so the same code serves tonight and the backtest.

Usage:
    python -m players_pm.usage backtest           # compares the methods on 2026-27 games
    python -m players_pm.usage day 2026-11-08     # prints tonight's xUSG
"""

import math
import sys
from collections import defaultdict

import numpy as np
import pandas as pd
from sqlalchemy import text

from .common import engine

HL = 15.0          # half-life in games of the decay (tuned on 2025-26 Dec-Jun)
PREV_W = 0.6       # weight of last-season games
PRIOR_MIN = 20.0   # shrinkage strength (minutes of league-average usage; 0-40 tune best)
ALPHA = {"nba": 1.0, "el": 0.0}  # how strongly vacated usage goes to high-usage players (tuned)
LEAGUE_MEAN = 20.0
KEY_USG, KEY_MIN = 24.0, 24.0   # an absent teammate this big triggers the with/without adjustment
SEASONS = ("2025-26", "2026-27")


def load(eng):
    q = text("""
        SELECT league, game_id, season, game_date, start_utc, player_id, player_name, team_code, opp_code,
               status, starter, minutes, fga, fta, tov, usg_pct, pbp_ok
        FROM props.player_games WHERE season = ANY(:s)""")
    with eng.connect() as c:
        d = pd.read_sql(q, c, params={"s": list(SEASONS)})
    d["minutes"] = pd.to_numeric(d["minutes"], errors="coerce").fillna(0.0)
    for c_ in ("fga", "fta", "tov"):
        d[c_] = pd.to_numeric(d[c_], errors="coerce").fillna(0.0)
    d["use"] = d["fga"] + 0.44 * d["fta"] + d["tov"]
    g = d.groupby(["game_id", "team_code"])
    d["tm_use"] = g["use"].transform("sum")
    d["tm_min"] = g["minutes"].transform("sum")
    played = (d["status"] == "played") & (d["minutes"] > 0)
    box = 100 * d["use"] * (d["tm_min"] / 5) / (d["minutes"] * d["tm_use"]).where(played & (d["tm_use"] > 0))
    pbp = pd.to_numeric(d["usg_pct"], errors="coerce").where(d["pbp_ok"].fillna(False).astype(bool))
    d["usg"] = pbp.fillna(box).where(played)
    d["played"] = played
    d = d.sort_values(["start_utc", "game_id"]).reset_index(drop=True)
    return d


class History:
    """walks games in time order and keeps each player's past (usage, minutes, season, team)"""

    def __init__(self):
        self.h = defaultdict(list)            # (league, pid) -> [(usg, min, season)], oldest first
        self.team_recent = defaultdict(list)  # (league, team) -> [game player lists]

    def baseline(self, key, season, hl=HL, prev_w=PREV_W, prior=PRIOR_MIN):
        rows = [r for r in self.h.get(key, []) if r[0] is not None and r[1] > 0][-40:]
        if not rows:
            return LEAGUE_MEAN, 0.0
        n = len(rows)
        num = den = 0.0
        for k, (u, m, s) in enumerate(reversed(rows)):
            w = m * 0.5 ** (k / hl) * (1.0 if s == season else prev_w)
            num += w * u
            den += w
        return (num + prior * LEAGUE_MEAN) / (den + prior), den

    def recent_min(self, key, n=5):
        rows = self.h.get(key, [])[-n:]
        return float(np.mean([r[1] for r in rows])) if rows else 0.0

    def add(self, key, usg, mins, season):
        self.h[key].append((None if usg is None or (isinstance(usg, float) and math.isnan(usg)) else float(usg),
                            float(mins), season))


def team_redistribute(players, alpha=1.0, tm_min=240.0):
    """players: list of dicts with base (usage %) and m (expected minutes). Adds 'x' = expected usage."""
    tot_m = sum(p["m"] for p in players)
    if tot_m <= 0:
        for p in players:
            p["x"] = p["base"]
        return players
    scale = tm_min / tot_m                              # expected minutes always fill the 240
    for p in players:
        p["m"] *= scale
    target = 100.0 * tm_min / 5                          # sum usage*minutes of the five on the floor
    have = sum(p["base"] * p["m"] for p in players)
    gap = target - have
    wsum = sum((p["base"] ** alpha) * p["m"] for p in players) or 1.0
    for p in players:
        share = (p["base"] ** alpha) * p["m"] / wsum
        p["x"] = min(45.0, max(5.0, p["base"] + gap * share / max(p["m"], 1e-6)))
    return players


def backtest(test_from="2026-10-20", test_to="2099-01-01", alpha_grid=(0.0, 1.0, 1.5, 2.0, 3.0),
             hl=HL, prev_w=PREV_W, prior=PRIOR_MIN, d=None):
    """pre-game predictions on every 2026-27 team-game; known absences = players who didn't play
    (the injury report knows most of them before tip). Expected minutes = last-5 average of the
    players who did play (no peeking at tonight's minutes). Metric: minutes-weighted MAE, players 15+ min."""
    d = load(engine()) if d is None else d
    H = History()
    res = defaultdict(list)
    for (gid, team), grp in d.groupby(["game_id", "team_code"], sort=False):
        lg, season = grp["league"].iat[0], grp["season"].iat[0]
        is_test = test_from <= str(grp["game_date"].iat[0]) < test_to
        if is_test:
            avail = grp[grp["played"]]
            base = {}
            for _, r in avail.iterrows():
                key = (lg, r["player_id"])
                rows = [x for x in H.h.get(key, []) if x[0] is not None and x[1] > 0]
                l10 = [x for x in rows[-10:]]
                ssn = [x for x in rows if x[2] == season]
                base[r["player_id"]] = {
                    "l10": (sum(u * m for u, m, _ in l10) / sum(m for _, m, _ in l10)) if l10 else LEAGUE_MEAN,
                    "season": (sum(u * m for u, m, _ in ssn) / sum(m for _, m, _ in ssn)) if ssn else
                              ((sum(u * m for u, m, _ in rows) / sum(m for _, m, _ in rows)) if rows else LEAGUE_MEAN),
                    "ewma": H.baseline(key, season, hl, prev_w, prior)[0],
                    "m": H.recent_min(key) or 8.0}
            for a in alpha_grid:
                pl = [{"pid": p, "base": b["ewma"], "m": b["m"]} for p, b in base.items()]
                team_redistribute(pl, alpha=a, tm_min=240.0 if lg == "nba" else 200.0)
                xs = {p["pid"]: p["x"] for p in pl}
                for _, r in avail.iterrows():
                    if r["minutes"] >= 15 and not pd.isna(r["usg"]) and H.h.get((lg, r["player_id"])):
                        res[f"ewma+team a={a}"].append((lg, r["minutes"], abs(xs[r["player_id"]] - r["usg"])))
            for _, r in avail.iterrows():
                if r["minutes"] >= 15 and not pd.isna(r["usg"]) and H.h.get((lg, r["player_id"])):
                    b = base[r["player_id"]]
                    for k in ("l10", "season", "ewma"):
                        res[k].append((lg, r["minutes"], abs(b[k] - r["usg"])))
        for _, r in grp.iterrows():
            if r["played"]:
                H.add((lg, r["player_id"]), r["usg"], r["minutes"], season)
    out = []
    for k, v in res.items():
        a = np.array([(x[1], x[2]) for x in v])
        row = {"method": k, "n": len(v), "MAE": float(np.average(a[:, 1], weights=a[:, 0]))}
        for lg in ("nba", "el"):
            b = np.array([(x[1], x[2]) for x in v if x[0] == lg])
            row[f"MAE_{lg}"] = float(np.average(b[:, 1], weights=b[:, 0])) if len(b) else None
        out.append(row)
    return pd.DataFrame(out).sort_values("MAE")


def expected_for_day(eng, games, minutes_hint=None, out_names=None):
    """games: [{league, game, team, opp}] for both sides of tonight's games (team codes as in player_games).
    minutes_hint: {(league, pid): {min, team}} from the minutes model; out_names: {(league, team): set(folded names)} unavailable.
    Returns {(league, pid): {usg, base, l10, min}} plus {("_absent", league, team): {names, dates}} for teams
    missing a key creator tonight (dates = that team's games this and last season without all of them)."""
    d = load(eng)
    H = History()
    last_team = {}
    for r in d.itertuples():
        if r.played:
            H.add((r.league, r.player_id), r.usg, r.minutes, r.season)
        last_team[(r.league, r.player_id)] = (r.team_code, r.start_utc)
    import unicodedata
    fold = lambda n: "".join(c for c in unicodedata.normalize("NFKD", n or "") if not unicodedata.combining(c)).lower().strip()
    names = {(r.league, r.player_id): fold(r.player_name) for r in d[["league", "player_id", "player_name"]].drop_duplicates().itertuples()}
    minutes_hint = minutes_hint or {}
    out_names = out_names or {}
    out = {}
    for g in games:
        lg, team = g["league"], g["team"]
        tg = d[(d["league"] == lg) & (d["team_code"] == team)]
        recent_games = tg.drop_duplicates("game_id").sort_values("start_utc")["game_id"].tolist()[-3:]
        roster = tg[tg["game_id"].isin(recent_games) & tg["played"]]["player_id"].unique().tolist()
        roster = [p for p in roster if last_team.get((lg, p), (team,))[0] == team]   # traded away -> drop
        roster += [p for (l2, p) in minutes_hint if l2 == lg and p not in roster and minutes_hint.get((lg, p), {}).get("team") == team]
        season = d[d["league"] == lg]["season"].max()
        pl = []
        for p in roster:
            key = (lg, p)
            if names.get((lg, p)) in out_names.get((lg, team), set()):
                b0, _ = H.baseline(key, season)
                if b0 >= KEY_USG and H.recent_min(key) >= KEY_MIN:     # a key creator is out tonight
                    mine = d[(d["league"] == lg) & (d["player_id"] == p) & d["played"]]["game_id"]
                    first = d[(d["league"] == lg) & (d["player_id"] == p) & (d["team_code"] == team)]["start_utc"].min()
                    tgs = tg[tg["season"].isin(SEASONS) & (tg["start_utc"] >= first)].drop_duplicates("game_id")
                    absent = tgs[~tgs["game_id"].isin(set(mine))]["game_date"].astype(str).tolist()
                    a = out.setdefault(("_absent", lg, team), {"names": [], "dates": None})
                    a["names"].append(d[(d["league"] == lg) & (d["player_id"] == p)]["player_name"].iat[-1])
                    a["dates"] = set(absent) if a["dates"] is None else a["dates"] & set(absent)
                continue
            base, n = H.baseline(key, season)
            hint = minutes_hint.get(key, {})
            m = hint.get("min") if hint.get("min") is not None else H.recent_min(key)
            rows = [x for x in H.h.get(key, []) if x[0] is not None and x[1] > 0][-10:]
            l10 = sum(u * mm for u, mm, _ in rows) / sum(mm for _, mm, _ in rows) if rows else None
            pl.append({"pid": p, "base": base, "m": max(float(m or 0), 0.0), "l10": l10})
        team_redistribute(pl, alpha=ALPHA[lg], tm_min=240.0 if lg == "nba" else 200.0)
        for p in pl:
            out[(lg, p["pid"])] = {"usg": round(p["x"], 1), "base": round(p["base"], 1),
                                   "l10": None if p["l10"] is None else round(p["l10"], 1), "min": round(p["m"], 1)}
    return out


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "tune":          # tune on 2025-26 (Dec-Jun), report on 2026-27
        D = load(engine())
        rows = []
        for hl in (4, 8, 15, 30):
            for pw in (0.3, 0.6, 1.0):
                for pr in (0, 40, 120):
                    r = backtest("2025-12-01", "2026-07-01", (0.0, 1.0), hl, pw, pr, D)
                    for _, x in r.iterrows():
                        if x["method"].startswith("ewma"):
                            rows.append({"hl": hl, "prev_w": pw, "prior": pr, **x.to_dict()})
        T = pd.DataFrame(rows)
        for lg in ("nba", "el"):
            print(lg, T.sort_values(f"MAE_{lg}").head(5).to_string(index=False))
        print(backtest("2025-12-01", "2026-07-01", (0.0,), d=D)[["method", "MAE_nba", "MAE_el"]].to_string(index=False))
    elif len(sys.argv) > 1 and sys.argv[1] == "backtest":
        pd.set_option("display.width", 160)
        print(backtest().to_string(index=False))
    else:
        print(__doc__)
