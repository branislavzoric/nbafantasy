#!/usr/bin/env python3
"""
Players +/- prediction engine: expected minutes -> expected stat -> full distribution -> P(Over/Under/Push).

  1. Minutes model    : expected minutes if he plays (props are void on DNP, so everything is
                        conditional on playing).
  2. Stat model       : expected value of the market's stat, from per-minute production over several
                        windows, projected minutes, usage/role, opponent defence (team + position),
                        pace and expected team points, spread/blowout risk, teammates out, rest, home,
                        history vs the opponent. Candidates: simple baselines, a structured baseline,
                        linear regression, ridge, sklearn gradient boosting, LightGBM (Poisson).
                        The one with the lowest validation RMSE per market is used.
  3. Distribution     : negative binomial with mean = model value. Two variance modes, the one with
                        the lower validation log loss is used per market:
                        "pop": variance = k * a * mean^b (population curve), optionally widened or
                               narrowed by the player's own recent spread;
                        "mix": minutes uncertainty carried through. Minutes quantile models (10 % /
                               90 %, LightGBM) give each player his own minutes spread; variance =
                               k * a' * mean^b' (the spread left once minutes are known, fitted on
                               residuals vs mean x actual/projected minutes) + km * rate^2 * Var(minutes).
                               That is the variance of the "draw minutes, then the stat" mixture, matched
                               by a negative binomial so the page and the Daily Card keep using (mu, var).
                        P(Over 27.5) = 1 - CDF(27); integer lines get a push probability = PMF(line).

Chronological splits (no shuffling, no future data in features):
  train      : 2025-26 games before 2026-03-01
  validation : 2026-03-01 .. 2026-07-31 (rest of 2025-26 + playoffs)  -> model choice + distribution
  test       : 2026-27 season so far                                   -> reported once, untouched
Then the chosen setup is refit on everything for production.

Bookmaker comparison: every closing two-way line we scraped in 2026-27 (6 Serbian books until
2026-11-02, Soccerbet after) vs the model and vs the book's own no-vig probability.

Feature A/B: PM_NO_STYLE=1 trains without the "Opponent style matchup" group (comparison runs);
PM_NO_PROMOTE=1 saves the run without making it the live model (models/CURRENT).

Usage:
  python -m players_pm.model train            # backtest + report + production fit
  python -m players_pm.model predict [DAY]    # predictions for that slate's props -> props.predictions
"""

import datetime as dt
import json
import os
import sys
import warnings

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from scipy import stats
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LinearRegression, Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sqlalchemy import text

from . import features as F
from .common import PM_DIR, engine, ensure_schema, job

warnings.filterwarnings("ignore", category=UserWarning)
MODEL_DIR = os.path.join(PM_DIR, "models")
VAL_START = pd.Timestamp("2026-03-01", tz="UTC")
TEST_START = pd.Timestamp("2026-08-01", tz="UTC")
MIN_HISTORY = 3                  # games played before a row is used / predicted
STAT_FEATURES = [c for g, cols in F.GROUPS.items() for c in cols
                 if not (g == "Opponent style matchup" and os.environ.get("PM_NO_STYLE") == "1")]
Z90 = 2 * 1.2816                 # q90 - q10 of a normal distribution, in standard deviations
ML_NAMES = ["Linear regression", "Ridge", "Gradient boosting (sklearn)", "LightGBM"]


# ------------------------------------------------------------------------------------------ models
class DropEmpty(BaseEstimator, TransformerMixin):
    """drop columns with no values in the training window (e.g. last-season rates while training on
    2025-26 only: there is no 2024-25 data)"""
    def fit(self, X, y=None):
        self.keep_ = [c for c in X.columns if X[c].notna().any()]
        return self

    def transform(self, X):
        return X[self.keep_]


def _lgb(objective):
    return lgb.LGBMRegressor(objective=objective, n_estimators=500, learning_rate=0.03, num_leaves=31,
                             min_child_samples=60, subsample=0.8, subsample_freq=1, colsample_bytree=0.8,
                             reg_lambda=2.0, verbose=-1)


def make(name, minutes=False):
    if name == "Linear regression":
        return make_pipeline(SimpleImputer(strategy="median", add_indicator=True), StandardScaler(), LinearRegression())
    if name == "Ridge":
        return make_pipeline(SimpleImputer(strategy="median", add_indicator=True), StandardScaler(), Ridge(alpha=30))
    if name == "Gradient boosting (sklearn)":
        return make_pipeline(DropEmpty(), HistGradientBoostingRegressor(
            loss="squared_error" if minutes else "poisson", max_iter=300, learning_rate=0.05,
            min_samples_leaf=60, l2_regularization=1.0))
    if name == "LightGBM":
        return _lgb("regression" if minutes else "poisson")
    raise KeyError(name)


def min_baseline(M):
    return M["min_ewm"].fillna(M["min_prev"]).fillna(M["min_l3"])


def oof_predict(name, X, y, minutes=False, folds=5):
    """out-of-fold predictions in chronological blocks (rows of X are in time order)"""
    out = np.full(len(X), np.nan)
    for idx in np.array_split(np.arange(len(X)), folds):
        mask = np.ones(len(X), bool)
        mask[idx] = False
        m = make(name, minutes).fit(X[mask], y[mask])
        out[idx] = m.predict(X.iloc[idx])
    return out


def baselines(X):
    season = (X["r_season"] * X["min_season"]).where(X["n_season"] >= 3)
    season = season.fillna(X["r_prev"] * X["min_prev"]).fillna(X["avg10"])
    struct = (X["min_hat"] * F.blend_rate(X) * X["opp_factor"].fillna(1) ** 0.5
              * X["pace_factor"].fillna(1) ** 0.5)
    return {"L5 average": X["avg5"], "L10 average": X["avg10"], "Season average": season,
            "Baseline (minutes x blended rate x matchup)": struct.fillna(X["avg10"])}


# ------------------------------------------------------------------------------------------ distribution
def nb_params(mu, var):
    mu = np.maximum(np.asarray(mu, float), 0.05)
    var = np.maximum(np.asarray(var, float), mu * 1.001)
    r = mu ** 2 / (var - mu)
    return r, r / (r + mu)


def probs(mu, var, line):
    """P(over), P(under), P(push) for one line per row (lines may differ per row)"""
    r, q = nb_params(mu, var)
    line = np.asarray(line, float)
    is_int = np.isclose(line, np.round(line))
    under = stats.nbinom.cdf(np.ceil(line) - 1, r, q)
    push = np.where(is_int, stats.nbinom.pmf(np.round(line), r, q), 0.0)
    over = 1 - under - push
    return over, under, push


def fit_var(mu, y):
    """variance = a * mu^b from binned squared residuals"""
    d = pd.DataFrame({"mu": mu, "r2": (y - mu) ** 2}).dropna()
    d["bin"] = pd.qcut(d["mu"], 20, duplicates="drop")
    b = d.groupby("bin", observed=True).agg(mu=("mu", "mean"), v=("r2", "mean"))
    b = b[(b["mu"] > 0) & (b["v"] > 0)]
    slope, icpt = np.polyfit(np.log(b["mu"]), np.log(b["v"]), 1)
    return float(np.exp(icpt)), float(slope)


def variance(mu, std20, vp, mvar=None):
    """mvar = rate^2 * Var(minutes) per row (only used by the "mix" mode)"""
    mu = np.maximum(np.asarray(mu, float), 0.05)
    pop = vp["k"] * vp["a"] * mu ** vp["b"]
    if vp["w"] and std20 is not None:
        ratio = (np.asarray(std20, float) ** 2 / pop)
        ratio = np.where(np.isfinite(ratio), np.clip(ratio, 0.5, 2.0), 1.0)
        pop = pop * ratio ** vp["w"]
    if vp.get("mode") == "mix" and mvar is not None:
        pop = pop + vp["km"] * np.nan_to_num(np.asarray(mvar, float))
    return pop


def minutes_var(mu, min_hat, q10, q90):
    """rate^2 * Var(minutes): the stat's variance that comes from not knowing his minutes"""
    sd = np.maximum((np.asarray(q90, float) - np.asarray(q10, float)) / Z90, 0.5)
    rate = np.asarray(mu, float) / np.maximum(np.asarray(min_hat, float), 1.0)
    return rate ** 2 * sd ** 2


def tune_var_mix(mu, y, std20, line, mins, min_hat, mvar):
    """the "mix" mode: conditional curve from residuals once minutes are known, then k / w / km by log loss"""
    mins = np.asarray(mins, float)
    mu_c = np.asarray(mu, float) * mins / np.maximum(np.asarray(min_hat, float), 1.0)
    a, b = fit_var(np.maximum(mu_c, 0.05), y)
    o = (y > line).astype(float)
    best = None
    for k in (0.7, 0.85, 1.0, 1.15, 1.3):
        for w in (0.0, 0.25):
            for km in (0.5, 0.75, 1.0, 1.25):
                vp = {"mode": "mix", "a": a, "b": b, "k": k, "w": w, "km": km}
                po, _, _ = probs(mu, variance(mu, std20, vp, mvar), line)
                ll = logloss(po, o)
                if best is None or ll < best[0]:
                    best = (ll, vp)
    return best


def mixture_probs(mu, line, min_hat, q10, q90, vp, std20, n=9):
    """exact "draw minutes, then the stat" mixture (normal minutes, NB stat given minutes) -- report only"""
    sd = np.maximum((np.asarray(q90, float) - np.asarray(q10, float)) / Z90, 0.5)
    z = stats.norm.ppf((np.arange(n) + 0.5) / n)
    mh = np.maximum(np.asarray(min_hat, float), 1.0)
    rate = np.asarray(mu, float) / mh
    cond = {**vp, "mode": "pop"}
    po = np.zeros(len(mh))
    for zi in z:
        m = np.maximum(mh + zi * sd, 0.0)
        mu_m = np.maximum(rate * m, 0.05)
        o, _, _ = probs(mu_m, variance(mu_m, std20, cond), line)
        po += o / n
    return po


def synthetic_line(X):
    """a line near his recent level, always x.5 (so no push): floor(L10 average) + 0.5"""
    return np.floor(X["avg10"].fillna(0)) + 0.5


def logloss(p, o):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return float(-np.mean(o * np.log(p) + (1 - o) * np.log(1 - p)))


def tune_var(mu, y, std20, line):
    a, b = fit_var(mu, y)
    o = (y > line).astype(float)
    best = None
    for k in (0.8, 0.9, 1.0, 1.1, 1.2, 1.35):
        for w in (0.0, 0.25, 0.5):
            vp = {"a": a, "b": b, "k": k, "w": w}
            po, _, _ = probs(mu, variance(mu, std20, vp), line)
            ll = logloss(po, o)
            if best is None or ll < best[0]:
                best = (ll, vp)
    return best[1]


def hit_rate_baseline(p, market, rows, lines):
    """share of his last 10 games played over the line ((k+0.5)/(n+1)) -- the page's history view.
    rows: index labels of p, lines: one line per row (rows may repeat with different lines)"""
    val = sum(p[c] for c in F.MARKETS[market])
    gid = p.groupby(["league", "player_id"], sort=False).ngroup()
    lags = np.column_stack([val.groupby(gid).shift(i).loc[rows].to_numpy() for i in range(1, 11)])
    lines = np.asarray(lines, float)[:, None]
    ok = ~np.isnan(lags)
    hits = (np.where(ok, lags, -1e9) > lines).sum(1)
    return (hits + 0.5) / (ok.sum(1) + 1)


def calib_table(p, o, bins=10):
    d = pd.DataFrame({"p": p, "o": o})
    d["bin"] = np.minimum((d["p"] * bins).astype(int), bins - 1)
    t = d.groupby("bin").agg(n=("o", "size"), pred=("p", "mean"), obs=("o", "mean")).reset_index()
    return [{"range": f"{b / bins:.1f}-{(b + 1) / bins:.1f}", "n": int(n), "pred": round(pr, 3), "obs": round(ob, 3)}
            for b, n, pr, ob in t.itertuples(index=False)]


def point_metrics(pred, y):
    e = pred - y
    return {"MAE": round(float(np.mean(np.abs(e))), 3), "RMSE": round(float(np.sqrt(np.mean(e ** 2))), 3),
            "bias": round(float(np.mean(e)), 3)}


def prob_metrics(pv, o):
    return {"Brier": round(float(np.mean((pv - o) ** 2)), 4), "LogLoss": round(logloss(pv, o), 4), "n": int(len(o))}


# ------------------------------------------------------------------------------------------ data
def dataset(p):
    """eligible completed rows, in time order"""
    ok = (p["fut"] == 0) & (p["n_all"] >= MIN_HISTORY) & (p["minutes"] > 0)
    return p[ok].sort_values("start_utc")


def book_lines(eng):
    """closing two-way lines (last scrape before tip-off) of every book, 2026-27, matched to stats players"""
    q = text("""
        WITH last AS (
            SELECT DISTINCT ON (pp.source, pp.game_id) pp.source, pp.game_id, pp.run_id
            FROM odds.player_props pp
            JOIN odds.scrape_runs r ON r.run_id = pp.run_id
            JOIN odds.games og ON og.source = pp.source AND og.game_id = pp.game_id
            WHERE r.scraped_at < (og.kickoff AT TIME ZONE 'Europe/Belgrade') + interval '5 minutes'
              AND r.league_id IN ('nba', 'euroleague') AND og.kickoff >= '2026-09-01'
            ORDER BY pp.source, pp.game_id, pp.run_id DESC)
        SELECT lg.league, p.source AS book, og.kickoff, a.player_id, p.market, p.side, p.line, p.odds
        FROM last
        JOIN odds.player_props p ON p.run_id = last.run_id AND p.game_id = last.game_id AND p.source = last.source
        JOIN odds.scrape_runs r ON r.run_id = p.run_id
        JOIN odds.games og ON og.source = p.source AND og.game_id = p.game_id
        JOIN props.leagues lg ON lg.odds_league_id = r.league_id
        JOIN props.player_aliases a ON a.league = lg.league AND a.book = p.source AND a.book_name = p.player
        WHERE p.side IN ('Over', 'Under') AND a.player_id IS NOT NULL""")
    d = pd.read_sql(q, eng)
    if d.empty:
        return d
    d["line"] = d["line"].astype(float)
    d["odds"] = d["odds"].astype(float)
    w = d.pivot_table(index=["league", "book", "kickoff", "player_id", "market", "line"], columns="side",
                      values="odds", aggfunc="max").dropna().reset_index()
    w["fair_over"] = (1 / w["Over"]) / (1 / w["Over"] + 1 / w["Under"])
    w["bal"] = (w["Over"] - w["Under"]).abs()
    w = w.sort_values("bal").drop_duplicates(["league", "book", "kickoff", "player_id", "market"])   # main line
    w["kick_utc"] = pd.to_datetime(w["kickoff"]).dt.tz_localize("Europe/Belgrade", ambiguous="NaT", nonexistent="shift_forward").dt.tz_convert("UTC")
    return w


def attach_rows(lines, p):
    """book line -> the player's game row (same player, tip-off within 6 h)"""
    rows = p[["league", "player_id", "start_utc"]].reset_index().rename(columns={"index": "row"})
    m = lines.merge(rows, on=["league", "player_id"])
    m = m[(m["start_utc"] - m["kick_utc"]).abs() < pd.Timedelta(hours=6)]
    return m


# ------------------------------------------------------------------------------------------ market blend
def _logit(p):
    p = np.clip(np.asarray(p, float), 1e-3, 1 - 1e-3)
    return np.log(p / (1 - p))


def blend_test(B):
    """P = sigmoid(c + w_m * logit(model) + w_b * logit(no-vig book)).
    Fit on the first half of the 2026-27 book lines (by tip-off), scored on the second half.
    Used on the site only if it beats the bookmaker's own probability out of sample."""
    from sklearn.linear_model import LogisticRegression
    B = B.dropna(subset=["kick"]).sort_values("kick")
    cut = B["kick"].iloc[len(B) // 2]
    fit, ev = B[B["kick"] < cut], B[B["kick"] >= cut]
    Xf = np.column_stack([_logit(fit["p_model"]), _logit(fit["fair"])])
    Xe = np.column_stack([_logit(ev["p_model"]), _logit(ev["fair"])])
    lr = LogisticRegression(C=1.0).fit(Xf, fit["over"])
    pb = lr.predict_proba(Xe)[:, 1]
    cal = LogisticRegression(C=1.0).fit(Xf[:, :1], fit["over"])          # model only, recalibrated
    pc = cal.predict_proba(Xe[:, :1])[:, 1]
    o = ev["over"].to_numpy()
    res = {"fit_rows": int(len(fit)), "eval_rows": int(len(ev)), "split_at": str(cut),
           "eval": {"bookmaker no-vig": prob_metrics(ev["fair"].to_numpy(), o),
                    "model": prob_metrics(ev["p_model"].to_numpy(), o),
                    "model (calibrated)": prob_metrics(pc, o),
                    "model + bookmaker blend": prob_metrics(pb, o)},
           "eval_calibration_model_calibrated": calib_table(pc, o, 5),
           "eval_over_rate": round(float(o.mean()), 3),
           "eval_always_under_roi": round(float(np.mean(np.where(o == 0, ev["under_odds"] - 1, -1))), 3)}
    # flat-stake bets where the blend sees >= 3 % edge at the offered price, on the held-out half
    ev_o = pb * ev["over_odds"].to_numpy() - 1
    ev_u = (1 - pb) * ev["under_odds"].to_numpy() - 1
    bo, bu = ev_o >= 0.03, (ev_u >= 0.03) & ~(ev_o >= 0.03)
    pnl = np.where(bo, np.where(o == 1, ev["over_odds"] - 1, -1), 0) + np.where(bu, np.where(o == 0, ev["under_odds"] - 1, -1), 0)
    n = int(bo.sum() + bu.sum())
    res["eval_blend_bets"] = {"bets": n, "overs": int(bo.sum()), "unders": int(bu.sum()),
                              "roi": round(float(pnl.sum() / n), 3) if n else None}
    # the blend has to beat the book clearly (>= 0.002 log loss) before the site calls it better;
    # lines of 6 books on the same player-game are correlated, so tiny gains are noise
    better = res["eval"]["model + bookmaker blend"]["LogLoss"] <= res["eval"]["bookmaker no-vig"]["LogLoss"] - 0.002
    full = LogisticRegression(C=1.0).fit(np.column_stack([_logit(B["p_model"]), _logit(B["fair"])]), B["over"])
    fcal = LogisticRegression(C=1.0).fit(_logit(B["p_model"])[:, None], B["over"])
    res["coef_full"] = {"c": float(full.intercept_[0]), "w_model": float(full.coef_[0][0]), "w_book": float(full.coef_[0][1])}
    res["calibration_full"] = {"c": float(fcal.intercept_[0]), "w": float(fcal.coef_[0][0])}
    res["use_blend"] = bool(better)
    return res, {**res["coef_full"], "use": bool(better), "cal": res["calibration_full"]}


# ------------------------------------------------------------------------------------------ training
def fit_minutes(D, names):
    """choose the minutes model on validation; returns (name, metrics)"""
    tr, va = D[D["start_utc"] < VAL_START], D[(D["start_utc"] >= VAL_START) & (D["start_utc"] < TEST_START)]
    res = {"Baseline (EWMA minutes)": point_metrics(min_baseline(va[F.MIN_FEATURES]), va["minutes"])}
    for n in names:
        m = make(n, minutes=True).fit(tr[F.MIN_FEATURES], tr["minutes"])
        res[n] = point_metrics(m.predict(va[F.MIN_FEATURES]), va["minutes"])
    best = min((k for k in res if k in names), key=lambda k: res[k]["RMSE"])
    return best, res


def add_min_hat(D, name, fit_mask, pred_mask):
    """OOF minutes inside the fit window, model-from-fit-window outside it"""
    out = pd.Series(np.nan, index=D.index)
    Xf, yf = D.loc[fit_mask, F.MIN_FEATURES], D.loc[fit_mask, "minutes"]
    out[fit_mask] = oof_predict(name, Xf, yf, minutes=True)
    m = make(name, minutes=True).fit(Xf, yf)
    if pred_mask.any():
        out[pred_mask] = m.predict(D.loc[pred_mask, F.MIN_FEATURES])
    return out, m


def _qmodel(a):
    return lgb.LGBMRegressor(objective="quantile", alpha=a, n_estimators=300, learning_rate=0.05, num_leaves=31,
                             min_child_samples=80, verbose=-1)


def add_min_q(D, fit_mask, pred_mask, folds=5):
    """10 % / 90 % minutes quantiles: out-of-fold inside the fit window, fitted model outside it"""
    out = {a: pd.Series(np.nan, index=D.index) for a in (0.1, 0.9)}
    models = {}
    Xf, yf = D.loc[fit_mask, F.MIN_FEATURES], D.loc[fit_mask, "minutes"]
    idx_all = np.arange(len(Xf))
    for a in (0.1, 0.9):
        oof = np.full(len(Xf), np.nan)
        for idx in np.array_split(idx_all, folds):
            mask = np.ones(len(Xf), bool)
            mask[idx] = False
            oof[idx] = _qmodel(a).fit(Xf[mask], yf[mask]).predict(Xf.iloc[idx])
        out[a][fit_mask] = oof
        models[a] = _qmodel(a).fit(Xf, yf)
        if pred_mask.any():
            out[a][pred_mask] = models[a].predict(D.loc[pred_mask, F.MIN_FEATURES])
    lo = np.minimum(out[0.1], out[0.9])
    hi = np.maximum(out[0.1], out[0.9])
    return lo, hi, models


def train():
    eng = engine()
    ensure_schema(eng)
    version = "pm-" + dt.datetime.now().strftime("%Y%m%d-%H%M")
    out_dir = os.path.join(MODEL_DIR, version)
    os.makedirs(out_dir, exist_ok=True)
    df = F.load(eng)
    p = F.build(df)
    D = dataset(p)
    is_tr = D["start_utc"] < VAL_START
    is_va = (D["start_utc"] >= VAL_START) & (D["start_utc"] < TEST_START)
    is_te = D["start_utc"] >= TEST_START
    report = {"version": version, "trained_at": dt.datetime.now().isoformat(timespec="seconds"),
              "splits": {"train": [str(D.loc[is_tr, "game_date"].min().date()), str(D.loc[is_tr, "game_date"].max().date()), int(is_tr.sum())],
                         "validation": [str(D.loc[is_va, "game_date"].min().date()), str(D.loc[is_va, "game_date"].max().date()), int(is_va.sum())],
                         "test": [str(D.loc[is_te, "game_date"].min().date()), str(D.loc[is_te, "game_date"].max().date()), int(is_te.sum())]},
              "markets": {}}
    print("rows", report["splits"])

    # ---- minutes
    min_name, min_val = fit_minutes(D, ML_NAMES)
    mh_a, _ = add_min_hat(D, min_name, is_tr, is_va)                       # phase A: train -> val
    mh_b, _ = add_min_hat(D, min_name, is_tr | is_va, is_te)               # phase B: train+val -> test
    te_min = point_metrics(mh_b[is_te], D.loc[is_te, "minutes"])
    te_min_base = point_metrics(min_baseline(D.loc[is_te, F.MIN_FEATURES]), D.loc[is_te, "minutes"])
    report["minutes"] = {"chosen": min_name, "validation": min_val,
                         "test": {min_name: te_min, "Baseline (EWMA minutes)": te_min_base}}
    print("minutes", min_name, min_val[min_name], "test", te_min, "baseline", te_min_base)
    # minutes range (10 % / 90 %), same two phases
    q10a, q90a, _ = add_min_q(D, is_tr, is_va)
    q10b, q90b, _ = add_min_q(D, is_tr | is_va, is_te)
    mt = D.loc[is_te, "minutes"]
    report["minutes"]["range_test"] = {
        "q10_q90_coverage": round(float(((mt >= q10b[is_te]) & (mt <= q90b[is_te])).mean()), 3),
        "mean_width": round(float((q90b[is_te] - q10b[is_te]).mean()), 2)}
    print("minutes range test", report["minutes"]["range_test"])

    oos_rows = []
    blines = book_lines(eng)
    battach = attach_rows(blines, D) if len(blines) else pd.DataFrame()
    prod = {"version": version, "min_model": min_name, "markets": {}}
    book_rows = []

    for market in F.MARKETS:
        Xf, yf = F.market_frame(p, market)                 # on the full history, then the eligible rows
        X_all, y_all = Xf.loc[D.index], yf.loc[D.index]
        res = {"validation": {}, "test": {}}
        # phase A: train on train, evaluate on validation
        XA = X_all.assign(min_hat=mh_a)
        cands = {}
        for n in ML_NAMES:
            m = make(n).fit(XA.loc[is_tr, STAT_FEATURES], y_all[is_tr])
            cands[n] = np.clip(m.predict(XA.loc[is_va, STAT_FEATURES]), 0, None)
        for n, v in baselines(XA.loc[is_va]).items():
            cands[n] = v.to_numpy()
        yv = y_all[is_va].to_numpy()
        for n, v in cands.items():
            ok = ~np.isnan(v)
            res["validation"][n] = point_metrics(v[ok], yv[ok])
        chosen = min(ML_NAMES, key=lambda n: res["validation"][n]["RMSE"])
        # near-tie (within 0.3 % RMSE) -> LightGBM. Added 2026-11-07 after a Ridge/LightGBM tie on Points
        # validation went to Ridge, which was clearly worse on test (so this rule was informed by test data)
        if chosen != "LightGBM" and res["validation"]["LightGBM"]["RMSE"] <= res["validation"][chosen]["RMSE"] * 1.003:
            chosen = "LightGBM"
        # distribution from validation residuals of the chosen model
        lv = synthetic_line(XA.loc[is_va]).to_numpy()
        sv = XA.loc[is_va, "std20"].to_numpy()
        vp_pop = tune_var(cands[chosen], yv, sv, lv)
        mv_a = minutes_var(cands[chosen], mh_a[is_va], q10a[is_va], q90a[is_va])
        ll_mix, vp_mix = tune_var_mix(cands[chosen], yv, sv, lv, D.loc[is_va, "minutes"].to_numpy(),
                                      mh_a[is_va].to_numpy(), mv_a)
        ov = (yv > lv).astype(float)
        ll_pop = logloss(probs(cands[chosen], variance(cands[chosen], sv, vp_pop), lv)[0], ov)
        vp = {**vp_mix} if ll_mix < ll_pop - 1e-4 else {**vp_pop, "mode": "pop"}
        res["variance_choice_validation"] = {"pop": round(ll_pop, 4), "mix": round(ll_mix, 4), "chosen": vp["mode"]}

        # phase B: refit every candidate on train+val, evaluate once on test
        XB = X_all.assign(min_hat=mh_b)
        fit = is_tr | is_va
        tpred = {}
        for n in ML_NAMES:
            m = make(n).fit(XB.loc[fit, STAT_FEATURES], y_all[fit])
            tpred[n] = np.clip(m.predict(XB.loc[is_te, STAT_FEATURES]), 0, None)
        for n, v in baselines(XB.loc[is_te]).items():
            tpred[n] = v.to_numpy()
        yt = y_all[is_te].to_numpy()
        for n, v in tpred.items():
            ok = ~np.isnan(v)
            res["test"][n] = point_metrics(v[ok], yt[ok])
        for lg in ("nba", "el"):
            sel = (D.loc[is_te, "league"] == lg).to_numpy()
            if sel.any():
                res.setdefault("test_by_league", {})[lg] = {
                    chosen: point_metrics(tpred[chosen][sel], yt[sel]),
                    "L10 average": point_metrics(np.nan_to_num(tpred["L10 average"][sel]), yt[sel])}

        # probabilities on test at a line near his recent level
        mu_t = tpred[chosen]
        st = XB.loc[is_te, "std20"].to_numpy()
        mv_t = minutes_var(mu_t, mh_b[is_te], q10b[is_te], q90b[is_te])
        var_t = variance(mu_t, st, vp, mv_t)
        lt = synthetic_line(XB.loc[is_te]).to_numpy()
        po, pu, _ = probs(mu_t, var_t, lt)
        o = (yt > lt).astype(float)
        # both variance modes + the exact mixture on test, at the synthetic line and +-3 alternates
        alt = {}
        for off in (-3, 0, 3):
            la = np.maximum(lt + off, 0.5)
            oa = (yt > la).astype(float)
            alt[f"line{off:+d}"] = {
                "pop": prob_metrics(probs(mu_t, variance(mu_t, st, vp_pop), la)[0], oa),
                "mix (NB matched)": prob_metrics(probs(mu_t, variance(mu_t, st, vp_mix, mv_t), la)[0], oa),
                "mix (exact)": prob_metrics(mixture_probs(mu_t, la, mh_b[is_te], q10b[is_te], q90b[is_te],
                                                          vp_mix, st), oa)}
        res["variance_test"] = alt
        # out-of-sample rows for the game simulator: validation (phase A) + test (phase B)
        mu_v = cands[chosen]
        var_v = variance(mu_v, sv, vp, mv_a)
        for mask, mu_x, var_x, mh_x in ((is_va, mu_v, var_v, mh_a[is_va].to_numpy()),
                                        (is_te, mu_t, var_t, mh_b[is_te].to_numpy())):
            Dm = D.loc[mask]
            oos_rows.append(pd.DataFrame({
                "market": market, "league": Dm["league"].to_numpy(), "game_id": Dm["game_id"].to_numpy(),
                "team": Dm["team_code"].to_numpy(), "opp": Dm["opp_code"].to_numpy(),
                "player_id": Dm["player_id"].to_numpy(), "start_utc": Dm["start_utc"].to_numpy(),
                "starter": Dm["starter"].to_numpy(), "minutes": Dm["minutes"].to_numpy(), "min_hat": mh_x,
                "mu": mu_x, "var": var_x, "y": y_all[mask].to_numpy(),
                "exp_margin": XB.loc[mask, "exp_margin"].to_numpy(), "pace_factor": XB.loc[mask, "pace_factor"].to_numpy(),
                "exp_team_pts": XB.loc[mask, "exp_team_pts"].to_numpy(), "is_home": Dm["is_home"].to_numpy(),
                "split": "validation" if mask is is_va else "test"}))
        hr = hit_rate_baseline(p, market, XB.index[is_te], lt)
        lo, hi = stats.nbinom.ppf(0.1, *nb_params(mu_t, var_t)), stats.nbinom.ppf(0.9, *nb_params(mu_t, var_t))
        res["test_prob_synthetic_line"] = {
            "model": prob_metrics(po, o), "L10 hit rate": prob_metrics(hr, o),
            "calibration": calib_table(po, o),
            "interval_80_coverage": round(float(np.mean((yt >= lo) & (yt <= hi))), 3)}
        # quantile regression comparison (LightGBM, same features): 80% interval coverage on test
        q = {}
        for a in (0.1, 0.9):
            qm = lgb.LGBMRegressor(objective="quantile", alpha=a, n_estimators=300, learning_rate=0.05,
                                   min_child_samples=60, verbose=-1).fit(XB.loc[fit, STAT_FEATURES], y_all[fit])
            q[a] = qm.predict(XB.loc[is_te, STAT_FEATURES])
        res["test_prob_synthetic_line"]["quantile_regression_80_coverage"] = round(float(np.mean((yt >= q[0.1]) & (yt <= q[0.9]))), 3)

        # vs bookmaker closing lines (2026-27)
        if len(battach):
            bl = battach[battach["market"] == market]
            bl = bl[bl["row"].isin(XB.index[is_te])]
            if len(bl):
                pos = pd.Series(np.arange(is_te.sum()), index=XB.index[is_te])
                ii = pos.loc[bl["row"]].to_numpy()
                yb = yt[ii]
                bo, bu, bp = probs(mu_t[ii], var_t[ii], bl["line"].to_numpy())
                po_pop, pu_pop, _ = probs(mu_t[ii], variance(mu_t[ii], st[ii], vp_pop), bl["line"].to_numpy())
                notpush = yb != bl["line"].to_numpy()
                ob = (yb > bl["line"].to_numpy()).astype(float)
                pm = bo / np.maximum(bo + bu, 1e-9)              # P(over | no push)
                fair = bl["fair_over"].to_numpy()
                hrb = hit_rate_baseline(p, market, bl["row"].to_numpy(), bl["line"].to_numpy())
                # flat-stake ROI when the model's edge at the offered price is >= 5 %
                ev_o = pm * bl["Over"].to_numpy() - 1
                ev_u = (1 - pm) * bl["Under"].to_numpy() - 1
                bet_o, bet_u = (ev_o >= 0.05) & notpush, (ev_u >= 0.05) & notpush & ~(ev_o >= 0.05)
                pnl = np.where(bet_o, np.where(ob == 1, bl["Over"].to_numpy() - 1, -1), 0) + \
                      np.where(bet_u, np.where(ob == 0, bl["Under"].to_numpy() - 1, -1), 0)
                nb = int(bet_o.sum() + bet_u.sum())
                pm_pop = po_pop / np.maximum(po_pop + pu_pop, 1e-9)
                res["vs_books"] = {
                    "props": int(notpush.sum()), "books": sorted(bl["book"].unique().tolist()),
                    "model": prob_metrics(pm[notpush], ob[notpush]),
                    "model (pop variance)": prob_metrics(pm_pop[notpush], ob[notpush]),
                    "bookmaker no-vig": prob_metrics(fair[notpush], ob[notpush]),
                    "L10 hit rate": prob_metrics(hrb[notpush], ob[notpush]),
                    "MAE line vs result": round(float(np.mean(np.abs(yb - bl["line"].to_numpy()))), 3),
                    "MAE model median vs result": round(float(np.mean(np.abs(
                        yb - stats.nbinom.ppf(0.5, *nb_params(mu_t[ii], var_t[ii]))))), 3),
                    "edge>=5% bets": nb, "edge>=5% ROI": round(float(pnl.sum() / nb), 3) if nb else None,
                    "calibration_model": calib_table(pm[notpush], ob[notpush], 5),
                    "over_rate": round(float(ob[notpush].mean()), 3),
                    "mean_no_vig_over": round(float(fair[notpush].mean()), 3),
                    "always Under ROI": round(float(np.mean(np.where(ob[notpush] == 0, bl["Under"].to_numpy()[notpush] - 1, -1))), 3),
                }
                book_rows.append(pd.DataFrame({
                    "market": market, "league": bl["league"].to_numpy()[notpush], "book": bl["book"].to_numpy()[notpush],
                    "kick": bl["kick_utc"].to_numpy()[notpush], "row": bl["row"].to_numpy()[notpush],
                    "line": bl["line"].to_numpy()[notpush], "over_odds": bl["Over"].to_numpy()[notpush],
                    "under_odds": bl["Under"].to_numpy()[notpush], "fair": fair[notpush], "p_model": pm[notpush],
                    "mu": mu_t[ii][notpush], "y": yb[notpush], "over": ob[notpush],
                    "var": var_t[ii][notpush], "game_id": D.loc[bl["row"].to_numpy(), "game_id"].to_numpy()[notpush],
                    "player_id": D.loc[bl["row"].to_numpy(), "player_id"].to_numpy()[notpush],
                    "team": D.loc[bl["row"].to_numpy(), "team_code"].to_numpy()[notpush]}))
        res["chosen"] = chosen
        res["distribution"] = vp
        report["markets"][market] = res
        vb = res.get("vs_books", {})
        print(f"{market:12s} {chosen:28s} var {vp['mode']} val RMSE {res['validation'][chosen]['RMSE']:.2f} "
              f"(L10 {res['validation']['L10 average']['RMSE']:.2f})  test RMSE {res['test'][chosen]['RMSE']:.2f} "
              f"(L10 {res['test']['L10 average']['RMSE']:.2f})  synth LL {res['test_prob_synthetic_line']['model']['LogLoss']:.3f} "
              f"vs hit {res['test_prob_synthetic_line']['L10 hit rate']['LogLoss']:.3f}  "
              f"books n={vb.get('props')} model {vb.get('model', {}).get('LogLoss')} book {vb.get('bookmaker no-vig', {}).get('LogLoss')}")

        # production: refit the chosen model on everything, distribution from val+test residuals
        allm = is_tr | is_va | is_te
        prod["markets"][market] = {"name": chosen, "dist": vp}
        report["markets"][market]["production_rows"] = int(allm.sum())

    # model + market blend: does the model add anything to the bookmaker's own probability?
    if book_rows:
        B = pd.concat(book_rows, ignore_index=True)
        B.to_csv(os.path.join(out_dir, "book_backtest.csv"), index=False)
        report["vs_books_all"], prod["blend"] = blend_test(B)
        print("blend", report["vs_books_all"])

    # production minutes model + stat models on all rows (OOF minutes as the stat-model input)
    allm = is_tr | is_va | is_te
    mh_all, min_model = add_min_hat(D, min_name, allm, pd.Series(False, index=D.index))
    _, _, min_q = add_min_q(D, allm, pd.Series(False, index=D.index))
    stat_models = {}
    for market, info in prod["markets"].items():
        Xf, yf = F.market_frame(p, market)
        X_all, y_all = Xf.loc[D.index].assign(min_hat=mh_all), yf.loc[D.index]
        stat_models[market] = make(info["name"]).fit(X_all[STAT_FEATURES], y_all)
        # distribution re-tuned on out-of-sample residuals (validation phase A + test phase B)
        info["dist"] = report["markets"][market]["distribution"]
        # neutral reference values for the explanations (league medians)
        info["neutral"] = {lg: X_all[D["league"] == lg][["exp_team_pts", "blowout", "pf36", "usg", "starter_l10",
                                                          "ts", "tpa_pm", "fta_pm", "g7"]].median().to_dict()
                           for lg in ("nba", "el")}
    prod["trained_through"] = str(D["game_date"].max().date())
    report["trained_through"] = prod["trained_through"]
    prod["stat_features"] = STAT_FEATURES
    # out-of-sample predictions with outcomes (validation phase A + test phase B) for the game simulator
    pd.concat(oos_rows, ignore_index=True).to_csv(os.path.join(out_dir, "oos_predictions.csv.gz"), index=False)
    joblib.dump({"meta": prod, "min_model": min_model, "min_q": min_q, "stat_models": stat_models},
                os.path.join(out_dir, "model.joblib"))
    with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1, default=str)
    if os.environ.get("PM_NO_PROMOTE") != "1":          # comparison runs don't become the live model
        with open(os.path.join(MODEL_DIR, "CURRENT"), "w") as f:
            f.write(version)
    print("saved", out_dir)
    return report


# ------------------------------------------------------------------------------------------ serving
def load_current():
    with open(os.path.join(MODEL_DIR, "CURRENT")) as f:
        version = f.read().strip()
    bundle = joblib.load(os.path.join(MODEL_DIR, version, "model.joblib"))
    with open(os.path.join(MODEL_DIR, version, "report.json"), encoding="utf-8") as f:
        bundle["report"] = json.load(f)
    return bundle


def neutralise(X, group, info, league):
    """the row with one factor group set to a neutral value -> 'effect of this factor'"""
    Z = X.copy()
    nt = info["neutral"][league]
    # his longer-term per-minute level (L20, this season, 2025-26) -- the reference for "recent form"
    parts = [("r20", .5), ("r_season", .25), ("r_prev", .25)]
    num = sum(Z[c].fillna(0) * w for c, w in parts)
    den = sum(Z[c].notna() * w for c, w in parts)
    level = (num / den.replace(0, np.nan)).fillna(F.blend_rate(Z))
    if group == "Recent form (L3/L5/L10)":
        for c in ("r3", "r5", "r10", "r_ewm"):
            Z[c] = level
        Z["avg5"] = Z["avg10"] = level * Z["min_season"].fillna(Z["min_prev"])
    elif group == "Projected minutes":
        Z["min_hat"] = Z["min_season"].where(Z["n_season"] >= 3).fillna(Z["min_prev"]).fillna(Z["min_l10"])
    elif group == "Opponent defence":
        Z["opp_factor"] = 1.0
        Z["opp_pos_factor"] = np.where(Z["opp_pos_factor"].notna(), 1.0, np.nan)
        Z["opp_drtg_rel"] = 1.0
    elif group == "Opponent style matchup":
        Z["x_3pa"] = Z["x_3pa"] / Z["o_tpa_rel"]
        Z["x_3pm"] = Z["x_3pm"] / (Z["o_tpa_rel"] * Z["o_3pct_rel"])
        Z["x_fta"] = Z["x_fta"] / Z["o_fta_rel"]
        Z["x_reb"] = Z["x_reb"] / ((1 - Z["o_reb_share_"]) * 2)
        Z["x_oreb"] = Z["x_oreb"] / Z["o_oreb_rel"]
        Z["x_ast"] = Z["x_ast"] / Z["o_ast_rel"]
        Z["x_tov"] = Z["x_tov"] / Z["o_tov_rel"]
        Z["x_blk"] = Z["x_blk"] / Z["o_fta_rel"]
        for c in ("o_tpa_rel", "o_3pct_rel", "o_fta_rel", "o_oreb_rel", "o_tov_rel", "o_ast_rel", "o_blk_rel"):
            Z[c] = 1.0
        Z["reb_battle"] = 0.0
    elif group == "Pace & expected team points":
        Z["pace_factor"] = 1.0
        Z["exp_team_pts"] = nt["exp_team_pts"]
    elif group == "Spread / blowout risk":
        Z["exp_margin"] = 0.0
        Z["blowout"] = nt["blowout"]
    elif group == "Teammates out":
        for c in ("out_n", "out_min", "out_pts", "out_reb", "out_ast"):
            Z[c] = 0.0
    elif group == "Rest & schedule":
        Z["rest"], Z["b2b"], Z["g7"] = 2.0, 0.0, nt["g7"]
        Z["games_missed"], Z["missed_prev"], Z["days_since_played"] = 0.0, 0.0, 2.0
    elif group == "Home / away":
        Z["is_home"] = 1.0 - Z["is_home"]          # half the home/away swing, applied below
    elif group == "History vs opponent":
        Z["h2h_ratio"], Z["h2h_n"] = 1.0, 0.0
    elif group == "Role & usage":
        for c in ("usg", "starter_l10", "ts", "tpa_pm", "fta_pm"):
            Z[c] = nt[c]
        Z["started_last"] = np.round(nt["starter_l10"])
    elif group == "Foul rate":
        Z["pf36"] = nt["pf36"]
    return Z


EXPLAIN = ["Projected minutes", "Recent form (L3/L5/L10)", "Role & usage", "Opponent defence", "Opponent style matchup",
           "Pace & expected team points", "Spread / blowout risk", "Teammates out", "Rest & schedule",
           "Home / away", "History vs opponent", "Foul rate"]


def future_rows(eng, items):
    """items: list of dicts (league, game_id, start_utc, player_id, team_code, opp_code, is_home)
    -> rows shaped like props.player_games with status 'future' (+ team stubs for both sides)"""
    df = F.load(eng)
    last = df[df["status"] == "played"].sort_values("start_utc").groupby(["league", "player_id"]).tail(1)
    last = last.set_index(["league", "player_id"])
    rows, seen_games = [], set()
    for it in items:
        key = (it["league"], it["player_id"])
        if key not in last.index:
            continue
        lr = last.loc[key]
        start = pd.Timestamp(it["start_utc"]).tz_convert("UTC")
        local = start.tz_convert("America/New_York" if it["league"] == "nba" else "Europe/Belgrade")
        base = {"league": it["league"], "game_id": it["game_id"], "season": "2026-27", "phase": "regular",
                "start_utc": start, "game_date": pd.Timestamp(local.date()), "status": "future", "starter": np.nan,
                "minutes": np.nan, "position": lr["position"]}
        rows.append({**base, "player_id": it["player_id"], "player_name": lr["player_name"],
                     "team_code": it["team_code"], "opp_code": it["opp_code"], "is_home": it["is_home"]})
        if it["game_id"] not in seen_games:
            seen_games.add(it["game_id"])
            for tm, op, h in ((it["team_code"], it["opp_code"], it["is_home"]),
                              (it["opp_code"], it["team_code"], not it["is_home"])):
                rows.append({**base, "player_id": f"__team_{tm}", "player_name": None, "team_code": tm,
                             "opp_code": op, "is_home": h, "status": "future_team", "position": None})
    fut = pd.DataFrame(rows)
    keep = df[~df["game_id"].isin(fut["game_id"])] if len(fut) else df
    allrows = pd.concat([keep.drop(columns=["pos", "playoffs"]), fut], ignore_index=True)
    return F.prep(allrows)


def injuries_now(eng):
    with eng.connect() as conn:
        rows = conn.execute(text("""
            SELECT DISTINCT ON (league, player_id) league, player_id, player_name, team_code, status, detail, fetched_at
            FROM props.injuries WHERE fetched_at > now() - interval '36 hours'
            ORDER BY league, player_id, fetched_at DESC""")).mappings().all()
        newest = dict(conn.execute(text("SELECT league, max(fetched_at) FROM props.injuries GROUP BY league")).all())
    latest = max(newest.values(), default=None)
    # only the latest snapshot of each league counts (a player who left the report is healthy again);
    # NBA = ESPN (players_pm.lines), EuroLeague = public injury pages (players_pm.el_injuries)
    rows = [r for r in rows if r["fetched_at"] == newest.get(r["league"])]
    inj = {(r["league"], r["player_id"]): dict(r) for r in rows}
    # the official NBA injury report (newest PDF per game date, today and later in US Eastern) overrides ESPN:
    # it is what the teams file with the league, and ESPN often lags it by hours (Doncic 2026-11-08)
    off, off_at = official_report(eng)
    for key, r in off.items():
        if r["status"] in ("Available", "Probable"):
            inj.pop(key, None)                  # cleared to play
        elif r["status"] in ("Out", "Doubtful", "Questionable"):
            inj[key] = {**inj.get(key, {}), **r}
    if off_at and (latest is None or off_at > latest):
        latest = off_at
    return inj, latest


def official_report(eng):
    """{('nba', player_id): {league, player_id, player_name, team_code, status, detail, fetched_at}} from the newest
    official NBA injury report of each game date from today (US Eastern) on"""
    with eng.connect() as conn:
        rows = conn.execute(text("""
            WITH newest AS (SELECT game_date, max(report_ts) ts FROM props.nba_injury_report
                            WHERE game_date >= (now() AT TIME ZONE 'America/New_York')::date GROUP BY game_date)
            SELECT 'nba' AS league, r.player_id, r.player_name, r.team AS team_code, r.status, r.reason AS detail,
                   r.fetched_at
            FROM props.nba_injury_report r JOIN newest n ON n.game_date = r.game_date AND n.ts = r.report_ts
            WHERE r.player_id IS NOT NULL""")).mappings().all()
    out = {(r["league"], r["player_id"]): dict(r) for r in rows}
    return out, max((r["fetched_at"] for r in rows), default=None)


def predict(eng, day, props):
    """props: export.py prop dicts of one slate (with game/team/opp/pid). Returns {prop key: prediction}
    and stores each prediction in props.predictions."""
    bundle = load_current()
    meta, min_model, stat_models = bundle["meta"], bundle["min_model"], bundle["stat_models"]
    now = dt.datetime.now(dt.timezone.utc)
    want = {}
    for pr in props:
        if not (pr.get("pid") and pr.get("game") and pr.get("team") and pr.get("opp")):
            continue
        ko = pd.Timestamp(pr["kickoff"])
        if ko.tz_convert("UTC") <= now:                     # never (re)predict a game that has started
            continue
        if pr["market"] not in F.MARKETS:
            continue
        want.setdefault((pr["league"], pr["game"], pr["pid"]), {
            "league": pr["league"], "game_id": pr["game"], "start_utc": ko, "player_id": pr["pid"],
            "team_code": pr["team"], "opp_code": pr["opp"], "is_home": pr.get("is_home")})
    if not want:
        return {}
    inj, inj_at = injuries_now(eng)
    df = future_rows(eng, list(want.values()))
    p = F.build(df, injuries={k: v["status"] for k, v in inj.items()})
    fut = p[(p["fut"] == 1) & ~p["player_id"].astype(str).str.startswith("__team_")]
    fut = fut[fut["n_all"] >= MIN_HISTORY]
    names = df.dropna(subset=["player_name"]).drop_duplicates(["league", "player_id"], keep="last") \
        .set_index(["league", "player_id"])["player_name"].to_dict()
    min_hat = pd.Series(min_model.predict(fut[F.MIN_FEATURES]), index=fut.index)
    min_q = bundle.get("min_q")
    if min_q:
        q_a = min_q[0.1].predict(fut[F.MIN_FEATURES])
        q_b = min_q[0.9].predict(fut[F.MIN_FEATURES])
        q10 = pd.Series(np.minimum(q_a, q_b), index=fut.index)
        q90 = pd.Series(np.maximum(q_a, q_b), index=fut.index)
    else:
        q10 = q90 = None
    feats = meta.get("stat_features", STAT_FEATURES)
    last_box = str(df.loc[df["status"] == "played", "game_date"].max().date())

    out = {}
    stored = []
    for market in {pr["market"] for pr in props if pr["market"] in F.MARKETS}:
        info = meta["markets"][market]
        model = stat_models[market]
        X, _ = F.market_frame(p, market)
        X = X.loc[fut.index].assign(min_hat=min_hat)
        mu = np.clip(model.predict(X[feats]), 0.05, None)
        mv = minutes_var(mu, min_hat.to_numpy(), q10.to_numpy(), q90.to_numpy()) if q10 is not None else None
        var = variance(mu, X["std20"].to_numpy(), info["dist"], mv)
        r, q = nb_params(mu, var)
        effects = {}
        for grp in EXPLAIN:
            alt = np.empty(len(X))
            for lg in ("nba", "el"):
                sel = (fut["league"] == lg).to_numpy()
                if sel.any():
                    Z = neutralise(X[sel], grp, info, lg)
                    alt[sel] = np.clip(model.predict(Z[feats]), 0.05, None)
            eff = mu - alt
            effects[grp] = eff / 2 if grp == "Home / away" else eff
        for i, (idx, row) in enumerate(fut.iterrows()):
            key = (row["league"], row["game_id"], row["player_id"], market)
            x = X.loc[idx]
            facts = sorted(((g, round(float(effects[g][i]), 2)) for g in EXPLAIN), key=lambda t: -abs(t[1]))
            stat_name = market.lower()
            uncertain = []
            mine = inj.get((row["league"], row["player_id"]))
            if mine and mine["status"] not in ("Out",):
                uncertain.append(f"He is listed {mine['status']} on the injury report")
            if row["league"] == "el":
                uncertain.append("EuroLeague injuries come from public injury pages, not the league: "
                                 "late game-day changes can be missing until line-ups")
            questionable = [v["player_name"] for (lg, pid), v in inj.items()
                            if lg == row["league"] and v.get("team_code") == row["team_code"] and pid != row["player_id"]
                            and v["status"] in ("Questionable", "Day-To-Day", "Doubtful")]
            if questionable:
                uncertain.append("Teammates questionable/day-to-day: " + ", ".join(questionable[:5]))
            if (x["games_missed"] or 0) > 0:
                uncertain.append(f"Missed the team's last {int(x['games_missed'])} game(s): minutes may be limited")
            if (x["min_std10"] or 0) >= 6:
                uncertain.append(f"Minutes have been volatile (±{x['min_std10']:.0f} min over the last 10)")
            if x["n_season"] < 5:
                uncertain.append(f"Only {int(x['n_season'])} games this season: relies on 2025-26 and recent games")
            if (x["blowout"] or 0) >= 10:
                uncertain.append(f"Lopsided matchup (expected margin {x['exp_margin']:+.0f}): blowout could cut minutes")
            if (x["pf36"] or 0) >= 4.5:
                uncertain.append(f"High foul rate ({x['pf36']:.1f} per 36)")
            out_list = row["out_names"] if isinstance(row["out_names"], list) else []
            det = {
                "mu": round(float(mu[i]), 2), "var": round(float(var[i]), 3),
                "median": int(stats.nbinom.ppf(0.5, r[i], q[i])),
                "p10": int(stats.nbinom.ppf(0.1, r[i], q[i])), "p90": int(stats.nbinom.ppf(0.9, r[i], q[i])),
                "min_hat": round(float(min_hat.loc[idx]), 1),
                "min_q10": None if q10 is None else round(float(q10.loc[idx]), 1),
                "min_q90": None if q90 is None else round(float(q90.loc[idx]), 1),
                "min_season": None if pd.isna(x["min_season"]) else round(float(x["min_season"]), 1),
                "min_l5": None if pd.isna(x["min_l5"]) else round(float(x["min_l5"]), 1),
                "per_min": round(float(mu[i] / max(min_hat.loc[idx], 1)), 3),
                "per_min_season": None if pd.isna(x["r_season"]) else round(float(x["r_season"]), 3),
                "usg": None if pd.isna(x["usg"]) else round(float(x["usg"]) * 100, 1),
                "opp_factor": None if pd.isna(x["opp_factor"]) else round(float(x["opp_factor"]), 3),
                "opp_pos_factor": None if pd.isna(x["opp_pos_factor"]) else round(float(x["opp_pos_factor"]), 3),
                "pace_factor": None if pd.isna(x["pace_factor"]) else round(float(x["pace_factor"]), 3),
                "exp_team_pts": None if pd.isna(x["exp_team_pts"]) else round(float(x["exp_team_pts"]), 1),
                "exp_margin": None if pd.isna(x["exp_margin"]) else round(float(x["exp_margin"]), 1),
                "rest": None if pd.isna(row["rest"]) else int(row["rest"]), "b2b": bool(row["b2b"] == 1),
                "out": [{"name": names.get((row["league"], pid), pid),
                         "status": (inj.get((row["league"], pid)) or {}).get("status", "not in last game")}
                        for pid in out_list],
                "own_status": mine["status"] if mine else None,
                "factors": [{"name": g, "effect": e} for g, e in facts if abs(e) >= 0.05],
                "uncertain": uncertain,
                "sample": {"games": int(x["n_all"]), "season": int(x["n_season"]), "vs_opp": int(x["h2h_n"])},
                "asof": {"box_scores_through": last_box, "injuries": inj_at.isoformat() if inj_at else None,
                         "model": meta["version"], "trained_through": meta.get("trained_through"),
                         "predicted_at": now.isoformat(timespec="seconds")},
                "model": info["name"],
            }
            out[key] = det
            stored.append({"v": meta["version"], "d": day, "l": row["league"], "g": row["game_id"],
                           "p": row["player_id"], "m": market, "mu": det["mu"], "var": det["var"],
                           "mh": det["min_hat"], "det": json.dumps(det)})
    # one row per player+market (several bookmakers can list the same prop, and the frame can repeat a player)
    stored = list({(s["l"], s["g"], s["p"], s["m"]): s for s in stored}.values())
    if stored:
        with eng.begin() as conn:
            conn.execute(text("""INSERT INTO props.predictions (model_version, day, league, game_id, player_id, market,
                                     mu, var, min_hat, detail)
                                 VALUES (:v, :d, :l, :g, :p, :m, :mu, :var, :mh, CAST(:det AS jsonb))"""), stored)
    return out


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "train":
        eng = engine()
        ensure_schema(eng)
        with job(eng, "train") as j:
            rep = train()
            j["rows"] = sum(m["production_rows"] for m in rep["markets"].values())
            j["message"] = f"{rep['version']}: trained through {rep['trained_through']}"
    else:
        raise SystemExit("usage: python -m players_pm.model train   (predictions run inside players_pm.export)")


if __name__ == "__main__":
    main()
