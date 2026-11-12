#!/usr/bin/env python3
"""
Pre-game features for the Players +/- prediction engine (NBA + EuroLeague), one row per player-game.

Every value in a row is computed ONLY from games that started before that game (shift-then-roll),
so the same code builds training rows (finished games) and serving rows (tonight's games, added as
'future' rows with no stats). Nothing about the game being predicted leaks into its own row.

Sources: props.player_games (box scores, both leagues) + props.injuries (NBA, serving only).

Feature families (names prefixed so the explanation groups can find them):
  min_*      minutes: L3/L5/L10 average, EWMA, season, 2025-26, volatility, starter rate
  r{k}_*     per-minute production of each stat over the last 3/5/10/15/20 games played, season,
             previous season, EWMA (the model gets these for the market's stat)
  usg/ts/... usage rate, true shooting, 3PA and FTA per minute, fouls per 36
  opp_*      opponent defence: stat allowed per 100 possessions vs league (team), and per minute
             vs league for the player's position (NBA only: ESPN gives G/F/C, the EL box does not)
  pace/exp_* expected possessions, expected team points and margin from both teams' ratings
             (offensive/defensive rating, pace), blowout risk = |expected margin|
  out_*      rotation teammates missing (avg minutes / points / rebounds / assists of regulars
             who played 2+ of the team's last 5 games at 18+ min and are not playing)
  rest/b2b   days since the team's last game, back-to-back, games in the last 7 days,
             games the player missed since he last played (minutes restriction risk)
  h2h_*      this player's per-minute production vs this opponent relative to his usual rate
Team/opp ratings use this season blended with last season while the sample is small (n/(n+8)).
"""

import numpy as np
import pandas as pd
from sqlalchemy import text

COMP = ["pts", "reb", "ast", "tpm", "stl", "blk", "tov"]
MARKETS = {
    "Points": ["pts"], "Rebounds": ["reb"], "Assists": ["ast"], "3PM": ["tpm"],
    "Steals": ["stl"], "Blocks": ["blk"], "Turnovers": ["tov"],
    "Pts+Reb": ["pts", "reb"], "Pts+Ast": ["pts", "ast"], "Reb+Ast": ["reb", "ast"],
    "Pts+Reb+Ast": ["pts", "reb", "ast"],
}
WINDOWS = [3, 5, 10, 15, 20]
SHRINK = 8           # team ratings: weight of this season = n / (n + SHRINK)
STYLE = ["tpa", "fta", "oreb", "fga"]   # also tracked as "allowed to opponents" for defensive style
POS_MAP = {"G": "G", "PG": "G", "SG": "G", "F": "F", "SF": "F", "PF": "F", "C": "C"}


def next_season(s):
    a = int(s[:4]) + 1
    return f"{a}-{str(a + 1)[2:]}"


# ------------------------------------------------------------------------------------------ load
def load(eng):
    df = pd.read_sql(text("""
        SELECT league, game_id, season, phase, start_utc, game_date, player_id, player_name, team_code,
               opp_code, is_home, status, starter, minutes, pts, reb, ast, tpm, stl, blk, tov,
               fga, fta, tpa, oreb, pf, position
        FROM props.player_games"""), eng)
    return prep(df)


def prep(df):
    df = df.copy()
    df["start_utc"] = pd.to_datetime(df["start_utc"], utc=True)
    df["game_date"] = pd.to_datetime(df["game_date"])
    for c in COMP + ["minutes", "fga", "fta", "tpa", "oreb", "pf"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    df["starter"] = df["starter"].fillna(False).astype(float)
    df["is_home"] = df["is_home"].fillna(False).astype(float)
    df["pos"] = df["position"].map(POS_MAP)
    df["playoffs"] = df["phase"].fillna("").str.lower().isin(["postseason", "playoffs", "play-in", "po", "ff", "pi"]).astype(float)
    return df.sort_values(["start_utc", "game_id"]).reset_index(drop=True)


# ------------------------------------------------------------------------------------------ teams
def team_games(df):
    """one row per team per game, incl. future games (no stats), with pre-game ratings"""
    played = df[df["status"] == "played"]
    keys = ["league", "season", "game_id", "start_utc", "game_date", "team_code", "opp_code", "is_home", "playoffs"]
    agg = played.groupby(keys, as_index=False)[COMP + ["fga", "fta", "tpa", "oreb", "minutes"]].sum()
    agg["poss_own"] = agg["fga"] + 0.44 * agg["fta"] - agg["oreb"] + agg["tov"]
    opp = agg[["game_id", "team_code", "poss_own"] + COMP + STYLE].rename(
        columns={"team_code": "opp_code", "poss_own": "opp_poss", **{c: f"a_{c}" for c in COMP + STYLE}})
    tg = agg.merge(opp, on=["game_id", "opp_code"], how="inner")
    tg["poss"] = (tg["poss_own"] + tg["opp_poss"]) / 2
    tg["done"] = 1.0
    fut = df[df["status"].isin(["future", "future_team"])][keys].drop_duplicates(["game_id", "team_code"])
    if len(fut):
        fut = fut.assign(done=0.0)
        tg = pd.concat([tg, fut], ignore_index=True)
    tg = tg.sort_values(["start_utc", "game_id"]).reset_index(drop=True)

    # league average per day from all earlier days (strictly before the game's date)
    sums = ["poss"] + COMP + [f"a_{c}" for c in COMP + STYLE]
    daily = tg[tg["done"] == 1].groupby(["league", "game_date"])[sums + ["done"]].sum().groupby(level=0).cumsum()
    daily = daily.groupby(level=0).shift(1)
    tg = tg.merge(daily.add_prefix("lgc_").reset_index(), on=["league", "game_date"], how="left")
    # future dates (or dates with no completed games) take the last known league totals
    tg = tg.sort_values(["league", "game_date"])
    lgc_cols = [c for c in tg if c.startswith("lgc_")]
    last = tg[tg["done"] == 1].groupby("league")[[*sums, "done"]].sum().add_prefix("lgc_")
    for c in lgc_cols:
        fill = tg["league"].map(last[c])
        tg[c] = tg[c].where(~(tg["done"] == 0) | tg[c].notna(), fill)
        tg[c] = tg.groupby("league")[c].ffill()
    tg["lg_pace"] = tg["lgc_poss"] / tg["lgc_done"]
    tg["lg_rtg"] = tg["lgc_pts"] / tg["lgc_poss"] * 100
    for c in COMP:
        tg[f"lg_{c}"] = tg[f"lgc_{c}"] / tg["lgc_poss"] * 100
    for c in STYLE:
        tg[f"lg_{c}"] = tg[f"lgc_a_{c}"] / tg["lgc_poss"] * 100
    tg["lg_3pct"] = tg["lgc_a_tpm"] / tg["lgc_a_tpa"]

    # team running totals within the season (before the game)
    tg = tg.sort_values(["start_utc", "game_id"])
    g = tg.groupby(["league", "season", "team_code"])
    run = ["poss", "pts", "reb", "oreb"] + [f"a_{c}" for c in COMP + STYLE]
    for c in run:
        tg[f"cs_{c}"] = g[c].transform(lambda s: s.fillna(0).cumsum().shift(1))
    tg["n_team"] = g["done"].transform(lambda s: s.cumsum().shift(1)).fillna(0)
    # previous season full totals for the same team
    tot = tg[tg["done"] == 1].groupby(["league", "season", "team_code"])[run + ["done"]].sum().reset_index()
    tot["season"] = tot["season"].map(next_season)
    tg = tg.merge(tot.rename(columns={c: f"ps_{c}" for c in run + ["done"]}),
                  on=["league", "season", "team_code"], how="left")

    w = tg["n_team"] / (tg["n_team"] + SHRINK)

    def blend(cur_num, cur_den, prev_num, prev_den, league_val):
        cur = cur_num / cur_den.replace(0, np.nan)
        prev = (prev_num / prev_den.replace(0, np.nan)).fillna(league_val)
        return (w * cur.fillna(0) + (1 - w) * prev).where(cur.notna(), prev)

    tg["t_pace"] = blend(tg["cs_poss"], tg["n_team"], tg["ps_poss"], tg["ps_done"], tg["lg_pace"])
    tg["t_ortg"] = blend(tg["cs_pts"] * 100, tg["cs_poss"], tg["ps_pts"] * 100, tg["ps_poss"], tg["lg_rtg"])
    tg["t_drtg"] = blend(tg["cs_a_pts"] * 100, tg["cs_poss"], tg["ps_a_pts"] * 100, tg["ps_poss"], tg["lg_rtg"])
    for c in COMP + STYLE:
        tg[f"t_allow_{c}"] = blend(tg[f"cs_a_{c}"] * 100, tg["cs_poss"], tg[f"ps_a_{c}"] * 100, tg["ps_poss"], tg[f"lg_{c}"])
    # defensive style (opponents' view): 3P% allowed is noisy, so it is shrunk harder toward the league
    w3 = tg["n_team"] / (tg["n_team"] + 25)
    cur3 = tg["cs_a_tpm"] / tg["cs_a_tpa"].replace(0, np.nan)
    prev3 = (tg["ps_a_tpm"] / tg["ps_a_tpa"].replace(0, np.nan)).fillna(tg["lg_3pct"])
    prev3 = 0.5 * prev3 + 0.5 * tg["lg_3pct"]
    tg["t_allow_3pct"] = (w3 * cur3.fillna(0) + (1 - w3) * prev3).where(cur3.notna(), prev3)
    # rebounding strength: share of all rebounds in its games that the team got
    half = pd.Series(0.5, index=tg.index)
    tg["t_reb_share"] = blend(tg["cs_reb"], tg["cs_reb"] + tg["cs_a_reb"], tg["ps_reb"], tg["ps_reb"] + tg["ps_a_reb"], half)

    # schedule: rest days / back-to-back / games in the last 7 days (any season)
    tg = tg.sort_values(["league", "team_code", "start_utc"])
    gt = tg.groupby(["league", "team_code"])
    prev_date = gt["game_date"].shift(1)
    tg["rest"] = (tg["game_date"] - prev_date).dt.days.clip(upper=10)
    tg["b2b"] = (tg["rest"] == 1).astype(float)
    tg["g7"] = gt["game_date"].transform(
        lambda s: pd.Series([((s < d) & (s >= d - pd.Timedelta(days=7))).sum() for d in s], index=s.index))
    tg["tgn"] = tg.groupby(["league", "season", "team_code"]).cumcount()
    return tg.sort_values(["start_utc", "game_id"]).reset_index(drop=True)


PROFILE = ["reb", "ast", "blk", "tpa", "stl", "oreb", "fta"]


def infer_positions(df):
    """EuroLeague box scores carry no position. A multinomial logistic model trained on NBA players
    (ESPN G/F/C, per-minute profile over each player-season) labels every EL row from that player's
    earlier games only (expanding totals, 3+ games played; earlier rows stay unlabelled)."""
    from sklearn.linear_model import LogisticRegression
    if not (df["league"] == "el").any():
        return df
    nba = df[(df["league"] == "nba") & (df["status"] == "played") & df["pos"].notna() & (df["minutes"] > 0)]
    seas = nba.groupby(["player_id", "season", "pos"])[PROFILE + ["minutes"]].sum()
    seas = seas[seas["minutes"] >= 150].reset_index()
    if seas.empty:
        return df
    Xs = (seas[PROFILE].div(seas["minutes"], axis=0) * 36).to_numpy()
    clf = LogisticRegression(max_iter=2000, C=1.0).fit(Xs, seas["pos"])
    el = df[df["league"] == "el"].sort_values("start_utc")
    gid = el.groupby("player_id", sort=False).ngroup()
    has = ((el["minutes"] > 0) & (el["status"] == "played")).astype(float)
    cum = {}
    for c in PROFILE + ["minutes"]:
        v = el[c].where(has == 1, 0).fillna(0)
        cum[c] = v.groupby(gid).cumsum() - v
    n = has.groupby(gid).cumsum() - has
    prof = pd.DataFrame({c: cum[c] / cum["minutes"].replace(0, np.nan) * 36 for c in PROFILE}, index=el.index)
    ok = (n >= 3) & prof.notna().all(axis=1)
    df = df.copy()
    if ok.any():
        df.loc[prof.index[ok], "pos"] = clf.predict(prof[ok].to_numpy())
    return df


def position_defence(df):
    """what each team allowed per minute to guards / forwards / centers, before each game
    (NBA: ESPN positions; EuroLeague: positions inferred from each player's stat profile)"""
    p = df[(df["status"].isin(["played", "future"])) & df["pos"].notna()]
    if p.empty:
        return pd.DataFrame()
    keys = ["league", "season", "game_id", "start_utc", "game_date", "opp_code", "pos"]
    agg = p.groupby(keys, as_index=False, dropna=False)[COMP + ["minutes"]].sum(min_count=1)
    agg["done"] = agg["minutes"].notna().astype(float)
    agg = agg.sort_values(["start_utc", "game_id"])
    g = agg.groupby(["league", "season", "opp_code", "pos"])
    for c in COMP + ["minutes"]:
        agg[f"cs_{c}"] = g[c].transform(lambda s: s.fillna(0).cumsum().shift(1))
    agg["n"] = g["done"].transform(lambda s: s.cumsum().shift(1)).fillna(0)
    tot = agg[agg["done"] == 1].groupby(["league", "season", "opp_code", "pos"])[COMP + ["minutes"]].sum().reset_index()
    tot["season"] = tot["season"].map(next_season)
    agg = agg.merge(tot.rename(columns={c: f"ps_{c}" for c in COMP + ["minutes"]}),
                    on=["league", "season", "opp_code", "pos"], how="left")
    # league per-minute rate by position, from earlier days
    daily = agg[agg["done"] == 1].groupby(["league", "pos", "game_date"])[COMP + ["minutes"]].sum() \
        .groupby(level=[0, 1]).cumsum()
    daily = daily.add_prefix("lp_").reset_index()          # merge_asof below takes days strictly before
    daily["lp_key"] = daily["league"] + "|" + daily["pos"]
    agg["lp_key"] = agg["league"] + "|" + agg["pos"]
    agg = agg.sort_values("game_date")
    agg = pd.merge_asof(agg, daily.drop(columns=["league", "pos"]).sort_values("game_date"), on="game_date",
                        by="lp_key", allow_exact_matches=False)
    w = agg["n"] / (agg["n"] + 10)
    out = agg[["game_id", "opp_code", "pos"]].copy()
    for c in COMP:
        lg = agg[f"lp_{c}"] / agg["lp_minutes"]
        cur = agg[f"cs_{c}"] / agg["cs_minutes"].replace(0, np.nan)
        prev = (agg[f"ps_{c}"] / agg["ps_minutes"]).fillna(lg)
        rate = (w * cur.fillna(0) + (1 - w) * prev).where(cur.notna(), prev)
        out[f"pd_{c}"] = rate
        out[f"pdl_{c}"] = lg
    return out


# ------------------------------------------------------------------------------------------ players
def _before(x, gid):
    """sum of the earlier rows of the same group (vectorised, NaN = 0)"""
    x = x.fillna(0)
    return x.groupby(gid).cumsum() - x


def _last_k(x, gid, k):
    """sum over the previous k rows of the same group"""
    cs = _before(x, gid)
    return cs - cs.groupby(gid).shift(k).fillna(0)


def _grouped(obj, gid, fn):
    """groupby rolling/ewm on already-shifted data, aligned back to the original index"""
    return fn(obj.groupby(gid)).droplevel(0).reindex(obj.index)


def player_features(df, tg):
    """rows = played games + future games; features from earlier played games only"""
    p = df[df["status"].isin(["played", "future"])].copy()
    p["fut"] = (p["status"] == "future").astype(float)
    t = tg[["game_id", "team_code", "fga", "fta", "tov", "minutes", "tgn", "rest", "b2b", "g7"]].rename(
        columns={"fga": "tm_fga", "fta": "tm_fta", "tov": "tm_tov", "minutes": "tm_min"})
    p = p.merge(t, on=["game_id", "team_code"], how="left")
    p["use"] = p["fga"] + 0.44 * p["fta"] + p["tov"]
    p["use_den"] = p["minutes"] * (p["tm_fga"] + 0.44 * p["tm_fta"] + p["tm_tov"]) / (p["tm_min"] / 5)
    p["tsa"] = 2 * (p["fga"] + 0.44 * p["fta"])
    p = p.sort_values(["league", "player_id", "start_utc"]).reset_index(drop=True)
    gid = p.groupby(["league", "player_id"], sort=False).ngroup()
    sid = p.groupby(["league", "player_id", "season"], sort=False).ngroup()
    hid = p.groupby(["league", "player_id", "opp_code"], sort=False).ngroup()
    has = p["minutes"].notna().astype(float)
    f = {}

    f["n_all"] = _before(has, gid)
    f["n_season"] = _before(has, sid)
    for k in WINDOWS:
        f[f"min_s{k}"] = _last_k(p["minutes"], gid, k)
        f[f"n{k}"] = _last_k(has, gid, k)
        for c in COMP:
            f[f"s{k}_{c}"] = _last_k(p[c], gid, k)
    for k in (3, 5, 10):
        f[f"min_l{k}"] = f[f"min_s{k}"] / f[f"n{k}"].replace(0, np.nan)
    shifted = p[["minutes", "starter"] + COMP].groupby(gid).shift(1)
    f["min_std10"] = _grouped(shifted["minutes"], gid, lambda g: g.rolling(10, min_periods=3).std())
    f["min_ewm"] = _grouped(shifted["minutes"], gid, lambda g: g.ewm(halflife=4, ignore_na=True).mean())
    ew = _grouped(shifted[["minutes"] + COMP], gid, lambda g: g.ewm(halflife=5, ignore_na=True).mean())
    f["ew_min"] = ew["minutes"]
    for c in COMP:
        f[f"ew_{c}"] = ew[c]
    f["starter_l10"] = _last_k(p["starter"].where(has == 1), gid, 10) / f["n10"].replace(0, np.nan)
    f["started_last"] = shifted["starter"]
    s10 = {col: _last_k(p[col], gid, 10) for col in ("use", "use_den", "pts", "tsa", "tpa", "fta")}
    f["usg"] = s10["use"] / s10["use_den"].replace(0, np.nan)
    f["ts"] = s10["pts"] / s10["tsa"].replace(0, np.nan)
    f["tpa_pm"] = s10["tpa"] / f["min_s10"].replace(0, np.nan)
    f["fta_pm"] = s10["fta"] / f["min_s10"].replace(0, np.nan)
    f["pf36"] = _last_k(p["pf"], gid, 20) / f["min_s20"].replace(0, np.nan) * 36

    # season to date
    for c in COMP + ["minutes"]:
        f[f"ss_{c}"] = _before(p[c], sid)
    f["min_season"] = f["ss_minutes"] / f["n_season"].replace(0, np.nan)

    # missed games since last played (same team & season), days since last game played
    prev_team, prev_season = p["team_code"].groupby(gid).shift(1), p["season"].groupby(gid).shift(1)
    same = (prev_team == p["team_code"]) & (prev_season == p["season"])
    f["games_missed"] = (p["tgn"] - p["tgn"].groupby(gid).shift(1) - 1).where(same).clip(lower=0, upper=20)
    f["missed_prev"] = (f["games_missed"] > 0).astype(float).where(f["games_missed"].notna())
    f["days_since_played"] = (p["game_date"] - p["game_date"].groupby(gid).shift(1)).dt.days.clip(upper=60)

    # vs this opponent (any season), before the game
    f["h2h_n"] = _before(has, hid)
    for c in COMP + ["minutes"]:
        f[f"h2h_{c}"] = _before(p[c], hid)
    p = pd.concat([p, pd.DataFrame(f)], axis=1)

    # previous season totals
    tot = p[p["fut"] == 0].groupby(["league", "player_id", "season"])[COMP + ["minutes"]].agg(["sum", "count"])
    prev = pd.DataFrame({f"pv_{c}": tot[(c, "sum")] for c in COMP + ["minutes"]})
    prev["pv_n"] = tot[("minutes", "count")]
    prev = prev.reset_index()
    prev["season"] = prev["season"].map(next_season)
    p = p.merge(prev, on=["league", "player_id", "season"], how="left")
    p["min_prev"] = p["pv_minutes"] / p["pv_n"]
    return p


def teammates_out(df, tg):
    """per team-game: regulars (2+ of the last 5 team games at an avg 18+ min) not playing"""
    played = df[df["status"] == "played"].merge(tg[["game_id", "team_code", "tgn"]], on=["game_id", "team_code"])
    rows = []
    for d in range(1, 6):
        x = played[["league", "season", "team_code", "tgn", "player_id", "minutes", "pts", "reb", "ast"]].copy()
        x["target"] = x["tgn"] + d
        rows.append(x)
    x = pd.concat(rows)
    win = x.groupby(["league", "season", "team_code", "target", "player_id"]).agg(
        cnt=("minutes", "size"), m=("minutes", "mean"), p=("pts", "mean"), r=("reb", "mean"), a=("ast", "mean")).reset_index()
    reg = win[(win["cnt"] >= 2) & (win["m"] >= 18)].rename(columns={"target": "tgn"})
    here = played[["league", "season", "team_code", "tgn", "player_id"]].assign(here=1)
    reg = reg.merge(here, on=["league", "season", "team_code", "tgn", "player_id"], how="left")
    return reg   # 'here' NaN = regular not in tonight's played list (completed games)


def build(df, injuries=None):
    """df = prep()'d player_games (+ optional future rows). injuries: {(league, player_id): status}
    for future games. Returns one row per played/future player-game with all features."""
    df = infer_positions(df)
    tg = team_games(df)
    p = player_features(df, tg)

    tcols = ["game_id", "team_code", "t_pace", "t_ortg", "t_drtg", "lg_pace", "lg_rtg", "n_team", "lg_3pct",
             "t_reb_share"] + [f"lg_{c}" for c in COMP + STYLE]
    own = tg[tcols]
    ocols = ["t_allow_3pct", "t_reb_share"]
    opp = tg[["game_id", "team_code", "t_pace", "t_ortg", "t_drtg"] + [f"t_allow_{c}" for c in COMP + STYLE] + ocols].rename(
        columns={"team_code": "opp_code", "t_pace": "o_pace", "t_ortg": "o_ortg", "t_drtg": "o_drtg",
                 "t_allow_3pct": "o_3pct", "t_reb_share": "o_reb_share",
                 **{f"t_allow_{c}": f"o_allow_{c}" for c in COMP + STYLE}})
    p = p.merge(own, on=["game_id", "team_code"], how="left").merge(opp, on=["game_id", "opp_code"], how="left").copy()
    p["pace_exp"] = p["t_pace"] * p["o_pace"] / p["lg_pace"]
    p["pace_factor"] = p["pace_exp"] / p["lg_pace"]
    p["exp_team_pts"] = p["t_ortg"] * p["o_drtg"] / p["lg_rtg"] * p["pace_exp"] / 100
    p["exp_opp_pts"] = p["o_ortg"] * p["t_drtg"] / p["lg_rtg"] * p["pace_exp"] / 100
    hca = np.where(p["league"] == "el", 3.0, 2.0) * np.where(p["is_home"] == 1, 1, -1)
    p["exp_margin"] = p["exp_team_pts"] - p["exp_opp_pts"] + hca
    p["blowout"] = p["exp_margin"].abs()
    p["opp_drtg_rel"] = p["o_drtg"] / p["lg_rtg"]
    # opponent defensive style vs the league (1 = average)
    p["o_tpa_rel"] = p["o_allow_tpa"] / p["lg_tpa"]            # 3-point attempts it gives up
    p["o_3pct_rel"] = p["o_3pct"] / p["lg_3pct"]               # 3P% it allows
    p["o_fta_rel"] = p["o_allow_fta"] / p["lg_fta"]            # free throws it gives up (rim / fouling)
    p["o_oreb_rel"] = p["o_allow_oreb"] / p["lg_oreb"]         # offensive boards it gives up
    p["o_tov_rel"] = p["o_allow_tov"] / p["lg_tov"]            # turnovers it forces
    p["o_ast_rel"] = p["o_allow_ast"] / p["lg_ast"]            # assists it allows (ball movement)
    p["o_blk_rel"] = p["o_allow_blk"] / p["lg_blk"]            # shots it blocks (rim protection)
    p["reb_battle"] = p["t_reb_share"] - p["o_reb_share"]       # own minus opponent rebounding strength

    pdf = position_defence(df)
    if len(pdf):
        p = p.merge(pdf, on=["game_id", "opp_code", "pos"], how="left")

    # teammates out
    reg = teammates_out(df, tg)
    done = reg[reg["here"].isna()]
    fut_games = set(p.loc[p["fut"] == 1, "game_id"])
    t_fut = tg[tg["game_id"].isin(fut_games)][["league", "season", "team_code", "tgn", "game_id"]]
    out_c = done.merge(tg[["league", "season", "team_code", "tgn", "game_id", "done"]],
                       on=["league", "season", "team_code", "tgn"])
    out_c = out_c[out_c["done"] == 1]
    if injuries is not None and len(t_fut):
        cand = reg.merge(t_fut, on=["league", "season", "team_code", "tgn"])
        cand["status"] = [injuries.get((lg, pid)) for lg, pid in zip(cand["league"], cand["player_id"])]
        cand = cand[cand["status"].isin(["Out", "Doubtful", "Suspension"])]
        out_c = pd.concat([out_c, cand], ignore_index=True)
    out = out_c.groupby(["game_id", "team_code"]).agg(
        out_n=("m", "size"), out_min=("m", "sum"), out_pts=("p", "sum"), out_reb=("r", "sum"),
        out_ast=("a", "sum"), out_names=("player_id", lambda s: list(s))).reset_index()
    p = p.merge(out, on=["game_id", "team_code"], how="left")
    for c in ("out_n", "out_min", "out_pts", "out_reb", "out_ast"):
        p[c] = p[c].fillna(0.0)
    p["el"] = (p["league"] == "el").astype(float)
    return p


# ------------------------------------------------------------------------------------------ per market
def _rate(num, den):
    return num / den.replace(0, np.nan)


def market_frame(p, market):
    """X (model features), y (actual, NaN for future), and some readable columns, for one market"""
    cs = MARKETS[market]
    S = lambda prefix: sum(p[f"{prefix}_{c}"] for c in cs)
    X = pd.DataFrame(index=p.index)
    for k in WINDOWS:
        X[f"r{k}"] = _rate(S(f"s{k}"), p[f"min_s{k}"])
    X["r_season"] = _rate(S("ss"), p["ss_minutes"])
    X["r_prev"] = _rate(S("pv"), p["pv_minutes"])
    X["r_ewm"] = _rate(S("ew"), p["ew_min"])
    X["avg5"] = S("s5") / p["n5"]
    X["avg10"] = S("s10") / p["n10"]
    # spread of the stat itself over the last 20 games played
    val = sum(p[c] for c in cs)
    gid = p.groupby(["league", "player_id"], sort=False).ngroup()
    X["std20"] = _grouped(val.groupby(gid).shift(1), gid, lambda g: g.rolling(20, min_periods=5).std())
    for c in ("min_l3", "min_l5", "min_l10", "min_ewm", "min_season", "min_prev", "min_std10",
              "starter_l10", "started_last", "usg", "ts", "tpa_pm", "fta_pm", "pf36",
              "pace_factor", "exp_team_pts", "exp_margin", "blowout", "opp_drtg_rel",
              "out_n", "out_min", "out_pts", "out_reb", "out_ast",
              "rest", "b2b", "g7", "games_missed", "missed_prev", "days_since_played",
              "is_home", "el", "playoffs", "n_all", "n_season"):
        X[c] = p[c]
    for c in STYLE_COLS:
        X[c] = p[c]
    # player type x opponent style: his own tendencies (last 10 games, per minute) times what the
    # opponent concedes in that area
    m10 = p["min_s10"].replace(0, np.nan)
    reb_pm, ast_pm = p["s10_reb"] / m10, p["s10_ast"] / m10
    tov_pm, blk_pm = p["s10_tov"] / m10, p["s10_blk"] / m10
    X["x_3pa"] = X["tpa_pm"] * p["o_tpa_rel"]
    X["x_3pm"] = X["tpa_pm"] * p["o_tpa_rel"] * p["o_3pct_rel"]
    X["x_fta"] = X["fta_pm"] * p["o_fta_rel"]
    X["x_reb"] = reb_pm * (1 - p["o_reb_share"]) * 2
    X["x_oreb"] = reb_pm * p["o_oreb_rel"]
    X["x_ast"] = ast_pm * p["o_ast_rel"]
    X["x_tov"] = tov_pm * p["o_tov_rel"]
    X["x_blk"] = blk_pm * p["o_fta_rel"]
    X["o_reb_share_"] = p["o_reb_share"]            # helper for the explanations, not a model input
    X["is_big"] = (p["pos"] == "C").astype(float).where(p["pos"].notna())
    X["is_guard"] = (p["pos"] == "G").astype(float).where(p["pos"].notna())
    lg_sum = sum(p[f"lg_{c}"] for c in cs)
    X["opp_factor"] = sum(p[f"o_allow_{c}"] for c in cs) / lg_sum
    if "pd_pts" in p:
        X["opp_pos_factor"] = sum(p[f"pd_{c}"] for c in cs) / sum(p[f"pdl_{c}"] for c in cs)
    else:
        X["opp_pos_factor"] = np.nan
    # history vs opponent, relative to his overall rate, shrunk to 1 with n/(n+4)
    base = X["r20"].fillna(X["r_prev"])
    h2h = _rate(S("h2h"), p["h2h_minutes"]) / base
    w = p["h2h_n"] / (p["h2h_n"] + 4)
    X["h2h_ratio"] = (w * h2h.fillna(1) + (1 - w)).clip(0.5, 1.6)
    X["h2h_n"] = p["h2h_n"]
    y = val.where(p["fut"] == 0)
    return X, y


# blend used by the simple baseline and as the "season level" number on the page
STYLE_COLS = ["o_tpa_rel", "o_3pct_rel", "o_fta_rel", "o_oreb_rel", "o_tov_rel", "o_ast_rel", "o_blk_rel",
              "reb_battle"]


def blend_rate(X):
    parts = [("r5", .35), ("r10", .30), ("r_season", .20), ("r_prev", .15)]
    num = sum(X[c].fillna(0) * w for c, w in parts)
    den = sum(X[c].notna() * w for c, w in parts)
    return num / den.replace(0, np.nan)


GROUPS = {
    "Recent form (L3/L5/L10)": ["r3", "r5", "r10", "r_ewm", "avg5", "avg10"],
    "Longer-term level (L15/L20/season/last season)": ["r15", "r20", "r_season", "r_prev", "n_all", "n_season"],
    "Projected minutes": ["min_hat"],
    "Role & usage": ["usg", "starter_l10", "started_last", "ts", "tpa_pm", "fta_pm"],
    "Opponent defence": ["opp_factor", "opp_pos_factor", "opp_drtg_rel"],
    "Opponent style matchup": ["o_tpa_rel", "o_3pct_rel", "o_fta_rel", "o_oreb_rel", "o_tov_rel", "o_ast_rel",
                               "o_blk_rel", "reb_battle", "x_3pa", "x_3pm", "x_fta", "x_reb", "x_oreb", "x_ast",
                               "x_tov", "x_blk", "is_big", "is_guard"],
    "Pace & expected team points": ["pace_factor", "exp_team_pts"],
    "Spread / blowout risk": ["exp_margin", "blowout"],
    "Teammates out": ["out_n", "out_min", "out_pts", "out_reb", "out_ast"],
    "Rest & schedule": ["rest", "b2b", "g7", "games_missed", "missed_prev", "days_since_played"],
    "Home / away": ["is_home"],
    "History vs opponent": ["h2h_ratio", "h2h_n"],
    "Foul rate": ["pf36"],
    "Consistency": ["std20", "min_std10"],
    "League / playoffs": ["el", "playoffs"],
}
MIN_FEATURES = ["min_l3", "min_l5", "min_l10", "min_ewm", "min_season", "min_prev", "min_std10",
                "starter_l10", "started_last", "usg", "pf36", "exp_margin", "blowout", "out_n", "out_min",
                "rest", "b2b", "g7", "games_missed", "missed_prev", "days_since_played", "is_home", "el",
                "playoffs", "n_all", "n_season"]
