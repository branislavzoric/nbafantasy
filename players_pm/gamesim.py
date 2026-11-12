#!/usr/bin/env python3
"""
Game-script simulator: how the player legs of one game move together.

Every player-prop result is driven partly by the game itself and partly by the players around him:
  game factors   z_tot  : the game total vs our expectation (pace / shooting night), shared by both teams
                 z_mar  : the margin vs our expectation, + for one team, - for the other
                 z_blow : how lopsided the game got vs our expectation (|margin| surprise; blowouts cut
                          starters' minutes and hand them to the bench)
  player link    the part left over once the game factors are out: teammates sharing rebounds and shots,
                 a guard's assists feeding the scorers, a player vs the man guarding him.

Fit (out-of-sample model predictions with outcomes, models/<ver>/oos_predictions.csv.gz):
  1. each prop result -> a normal score u = Phi^-1(randomised PIT under the model's NB distribution);
  2. per game: z factors from actual vs expected team points (standardised per league);
  3. loadings: u ~ z_tot + z_mar + z_blow per (league, market, role), role from projected minutes
     (core >= 28, rotation 18-28, bench < 18);
  4. residual correlations between two different players of the same game, per relation (team-mate /
     opponent) x market pair, shrunk toward 0 by the number of games.
Serving: a Gaussian copula. Each leg keeps its own win chance (the Daily Card confidence); only the
dependence comes from the fit. Games are independent of each other.

Validation (fit on the validation period only, scored on 2026-27 bookmaker lines from book_backtest.csv,
marginal = the bookmaker's no-vig chance of the side the model prefers): joint win chance of random
same-game 2- and 4-leg combos, independent product vs copula. The copula is only used when it beats
independence on the 4-leg combos (the card's tickets have 3-5 legs); otherwise the card uses the product.
First fit (2026-11-07): the links are small (residual correlations within +-0.05); 4-leg log loss
0.31410 copula vs 0.31416 independent, 2-leg slightly worse. Combos the copula rates most correlated
did hit more often (9.4 % vs 8.8 % independent), but within noise. Treat it as a small refinement.

Usage:
  python -m players_pm.gamesim fit [VERSION]     # writes models/<ver>/gamesim.json (+ validation)
"""

import json
import math
import os
import sys

import numpy as np
import pandas as pd
from scipy import stats

from .common import PM_DIR

MODEL_DIR = os.path.join(PM_DIR, "models")
FACTORS = ["z_tot", "z_mar", "z_blow"]
N_SIM = 20000


def role_of(min_hat):
    m = np.asarray(min_hat, float)
    return np.where(m >= 28, "core", np.where(m >= 18, "rot", "bench"))


# ------------------------------------------------------------------------------------------ fit
def _pit_scores(d, seed=7):
    """randomised PIT of y under NB(mu, var) -> standard normal score"""
    rng = np.random.default_rng(seed)
    mu = np.maximum(d["mu"].to_numpy(float), 0.05)
    var = np.maximum(d["var"].to_numpy(float), mu * 1.001)
    r = mu ** 2 / (var - mu)
    q = r / (r + mu)
    y = d["y"].to_numpy(float)
    lo = stats.nbinom.cdf(y - 1, r, q)
    pm = stats.nbinom.pmf(y, r, q)
    u = np.clip(lo + rng.random(len(y)) * pm, 1e-5, 1 - 1e-5)
    return stats.norm.ppf(u)


def _game_factors(d, team_pts):
    """per (game, team): z_tot, z_mar (team's view), z_blow -- actual team points (box score totals)
    vs the expected team points / margin in the model rows (exp_team_pts / exp_margin are per team)"""
    pts = d[d["market"] == "Points"]
    tm = pts.groupby(["league", "game_id", "team", "opp"], as_index=False).agg(
        exp=("exp_team_pts", "first"), exp_margin=("exp_margin", "first"), is_home=("is_home", "first"))
    tm = tm.merge(team_pts, on=["game_id", "team"], how="inner")
    g = tm.merge(tm[["game_id", "team", "act", "exp"]].rename(columns={"team": "opp", "act": "o_act", "exp": "o_exp"}),
                 on=["game_id", "opp"])
    g["d_tot"] = (g["act"] + g["o_act"]) - (g["exp"] + g["o_exp"])
    g["d_mar"] = (g["act"] - g["o_act"]) - g["exp_margin"]
    g["d_blow"] = (g["act"] - g["o_act"]).abs() - g["exp_margin"].abs()
    sd = {}
    for lg, x in g.groupby("league"):
        sd[lg] = {c: float(x[c].std()) for c in ("d_tot", "d_mar", "d_blow")}
        sd[lg].update({f"mean_{c}": float(x[c].mean()) for c in ("d_tot", "d_mar", "d_blow")})
        sd[lg]["mean_d_mar"] = 0.0                     # the margin surprise is symmetric by construction
    for c, z in (("d_tot", "z_tot"), ("d_mar", "z_mar"), ("d_blow", "z_blow")):
        g[z] = [(v - sd[lg][f"mean_{c}"]) / sd[lg][c] for v, lg in zip(g[c], g["league"])]
    cov = {}
    for lg, x in g[g["is_home"] == 1].groupby("league"):
        cov[lg] = np.cov(x[FACTORS].to_numpy().T).tolist()          # home team's view
    return g[["game_id", "team"] + FACTORS], sd, cov


def fit_params(d, team_pts):
    d = d.dropna(subset=["mu", "var", "y", "min_hat"]).copy()
    d["u"] = _pit_scores(d)
    d["role"] = role_of(d["min_hat"])
    gf, sd, cov = _game_factors(d, team_pts)
    d = d.merge(gf, on=["game_id", "team"], how="inner")
    load, resid = {}, {}
    d["e"] = np.nan
    for (lg, mk, rl), x in d.groupby(["league", "market", "role"]):
        if len(x) < 150:
            continue
        Z = x[FACTORS].to_numpy()
        beta, *_ = np.linalg.lstsq(np.column_stack([np.ones(len(x)), Z]), x["u"].to_numpy(), rcond=None)
        e = x["u"].to_numpy() - np.column_stack([np.ones(len(x)), Z]) @ beta
        load[f"{lg}|{mk}|{rl}"] = {"b": [round(float(v), 4) for v in beta[1:]], "s": round(float(e.std()), 4),
                                    "n": int(len(x))}
        d.loc[x.index, "e"] = e
    # pooled fallbacks per (league, market)
    for (lg, mk), x in d.groupby(["league", "market"]):
        Z = x[FACTORS].to_numpy()
        beta, *_ = np.linalg.lstsq(np.column_stack([np.ones(len(x)), Z]), x["u"].to_numpy(), rcond=None)
        e = x["u"].to_numpy() - np.column_stack([np.ones(len(x)), Z]) @ beta
        load[f"{lg}|{mk}|*"] = {"b": [round(float(v), 4) for v in beta[1:]], "s": round(float(e.std()), 4),
                                "n": int(len(x))}
        miss = x.index[d.loc[x.index, "e"].isna()]
        d.loc[miss, "e"] = e[x.index.get_indexer(miss)]
    # residual correlations between different players of the same game
    e = d[["game_id", "team", "player_id", "market", "e", "league"]].dropna()
    corr = {}
    markets = sorted(e["market"].unique())
    for i, ma in enumerate(markets):
        a = e[e["market"] == ma]
        for mb in markets[i:]:
            b = e[e["market"] == mb]
            m = a.merge(b, on=["game_id", "league"], suffixes=("_a", "_b"))
            m = m[m["player_id_a"] != m["player_id_b"]]
            if ma == mb:
                m = m[m["player_id_a"] < m["player_id_b"]]
            m["rel"] = np.where(m["team_a"] == m["team_b"], "team", "opp")
            for rel, x in m.groupby("rel"):
                if len(x) < 200:
                    continue
                n_g = x["game_id"].nunique()
                r = float(np.corrcoef(x["e_a"], x["e_b"])[0, 1])
                corr[f"{rel}|{ma}|{mb}"] = {"r": round(r * n_g / (n_g + 50), 4), "raw": round(r, 4),
                                            "games": int(n_g), "pairs": int(len(x))}
    return {"loadings": load, "corr": corr, "factor_cov": cov, "factor_sd": sd,
            "rows": int(len(d)), "games": int(d["game_id"].nunique())}


# ------------------------------------------------------------------------------------------ simulate
def _loading(P, lg, mk, role):
    return P["loadings"].get(f"{lg}|{mk}|{role}") or P["loadings"].get(f"{lg}|{mk}|*")


def _rho(P, rel, ma, mb):
    a, b = sorted([ma, mb])
    c = P["corr"].get(f"{rel}|{a}|{b}")
    return c["r"] if c else 0.0


def _psd(C):
    w, V = np.linalg.eigh(C)
    w = np.clip(w, 1e-6, None)
    C2 = V @ np.diag(w) @ V.T
    dd = np.sqrt(np.diag(C2))
    return C2 / np.outer(dd, dd)


def simulate_game(P, legs, n=N_SIM, seed=11):
    """legs of ONE game: dicts with league, team, home (bool), market, side, p (win chance), min_hat.
    Returns (wins: bool array n x legs, z: n x 3 factor draws in the HOME team's view)"""
    rng = np.random.default_rng(seed)
    lg = legs[0]["league"]
    cov = np.array(P["factor_cov"].get(lg) or np.eye(3))
    z = rng.multivariate_normal(np.zeros(3), cov, size=n)
    k = len(legs)
    L = np.zeros((k, 3))
    s = np.ones(k)
    for i, l in enumerate(legs):
        ld = _loading(P, lg, l["market"], role_of([l.get("min_hat") or 20])[0])
        if ld:
            b = np.array(ld["b"], float)
            if not l.get("home"):
                b = b * np.array([1, -1, 1])          # margin factor is drawn in the home team's view
            L[i], s[i] = b, ld["s"]
    R = np.eye(k)
    for i in range(k):
        for j in range(i + 1, k):
            rel = "team" if legs[i]["team"] == legs[j]["team"] else "opp"
            R[i, j] = R[j, i] = _rho(P, rel, legs[i]["market"], legs[j]["market"])
    R = _psd(R)
    e = rng.multivariate_normal(np.zeros(k), R, size=n) * s
    tot_sd = np.sqrt(np.einsum("ij,jk,ik->i", L, cov, L) + s ** 2)
    u = (z @ L.T + e) / tot_sd
    wins = np.zeros((n, k), bool)
    for i, l in enumerate(legs):
        pwin = min(max(l["p"], 1e-4), 1 - 1e-4)
        if l["side"] == "Over":
            wins[:, i] = u[:, i] > stats.norm.ppf(1 - pwin)
        else:
            wins[:, i] = u[:, i] < stats.norm.ppf(pwin)
    return wins, z


def joint(P, legs, n=N_SIM, seed=11):
    """joint win chance of a ticket (legs may span games; games are independent).
    Returns (p_joint, p_independent, per-game scenario lean: mean factors when the ticket wins)"""
    p_ind = math.prod(l["p"] for l in legs)
    if not P:
        return p_ind, p_ind, {}
    by = {}
    for l in legs:
        by.setdefault(l["game"], []).append(l)
    ok = np.ones(n, bool)
    zs = {}
    for gi, (g, gl) in enumerate(sorted(by.items())):
        w, z = simulate_game(P, gl, n, seed + gi)
        hit = w.all(axis=1)
        ok &= hit
        zs[g] = z
    lean = {}
    if ok.sum() >= 50:
        for g, z in zs.items():
            lean[g] = dict(zip(FACTORS, [round(float(v), 3) for v in z[ok].mean(axis=0)]))
    return float(ok.mean()), p_ind, lean


def lean_text(P, lean_g, league, home, away):
    """factor means when the ticket wins -> readable 'this ticket wins when ...' in points"""
    sd = (P or {}).get("factor_sd", {}).get(league)
    if not sd or not lean_g:
        return None
    tot = lean_g["z_tot"] * sd["d_tot"]
    mar = lean_g["z_mar"] * sd["d_mar"]
    blow = lean_g["z_blow"] * sd["d_blow"]
    bits = []
    if abs(tot) >= 2:
        bits.append(f"{'more' if tot > 0 else 'fewer'} points than we expect ({tot:+.0f})")
    if abs(mar) >= 2:
        bits.append(f"{home if mar > 0 else away} does better than expected ({abs(mar):.0f} pts on the margin)")
    if abs(blow) >= 2:
        bits.append("a lopsided game" if blow > 0 else "a close game")
    return ", ".join(bits) if bits else "no strong game script: the legs are close to independent"


# ------------------------------------------------------------------------------------------ validation
def validate(P, bb, n_combo=4000, seed=3):
    """bb = book_backtest.csv rows (2026-27). Same-game 2- and 4-leg combos of the model's preferred
    side, marginal = bookmaker no-vig chance of that side. Independent vs copula, log loss + calibration."""
    rng = np.random.default_rng(seed)
    bb = bb.sort_values("book").drop_duplicates(["game_id", "player_id", "market"]).copy()
    bb["side"] = np.where(bb["p_model"] >= 0.5, "Over", "Under")
    bb["p"] = np.where(bb["side"] == "Over", bb["fair"], 1 - bb["fair"])
    bb["win"] = np.where(bb["side"] == "Over", bb["over"] == 1, bb["over"] == 0)
    out = {}
    for k in (2, 4):
        rows = []
        games = [g for g, x in bb.groupby("game_id") if x["player_id"].nunique() >= k]
        for t in range(n_combo):
            g = games[rng.integers(len(games))]
            x = bb[bb["game_id"] == g]
            pl = rng.choice(x["player_id"].unique(), k, replace=False)
            sel = x[x["player_id"].isin(pl)].groupby("player_id").sample(1, random_state=int(rng.integers(1e9)))
            legs = [{"league": r.league, "game": g, "team": r.team, "home": bool(r.is_home), "market": r.market,
                     "side": r.side, "p": float(r.p), "min_hat": r.min_hat} for r in sel.itertuples()]
            pj, pi, _ = joint(P, legs, n=4000, seed=t)
            rows.append((pj, pi, bool(sel["win"].all())))
        a = np.array(rows, float)
        y = a[:, 2]

        def ll(p):
            p = np.clip(p, 1e-4, 1 - 1e-4)
            return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))
        out[f"{k}-leg"] = {"combos": len(a), "actual_hit": round(float(y.mean()), 4),
                           "independent": {"mean_p": round(float(a[:, 1].mean()), 4), "LogLoss": round(ll(a[:, 1]), 5)},
                           "copula": {"mean_p": round(float(a[:, 0].mean()), 4), "LogLoss": round(ll(a[:, 0]), 5)}}
    # the card only builds 3- to 5-leg tickets, so the 4-leg test decides (the 2-leg one is reported)
    out["use_copula"] = bool(out["4-leg"]["copula"]["LogLoss"] <= out["4-leg"]["independent"]["LogLoss"])
    return out


def fit(version=None):
    version = version or open(os.path.join(MODEL_DIR, "CURRENT")).read().strip()
    vdir = os.path.join(MODEL_DIR, version)
    ids = {"game_id": str, "player_id": str, "team": str}
    d = pd.read_csv(os.path.join(vdir, "oos_predictions.csv.gz"), dtype=ids)
    from sqlalchemy import text
    from .common import engine
    team_pts = pd.read_sql(text("""SELECT game_id, team_code AS team, sum(pts) AS act FROM props.player_games
                                   WHERE status = 'played' GROUP BY game_id, team_code"""), engine())
    team_pts["game_id"] = team_pts["game_id"].astype(str)
    team_pts["act"] = team_pts["act"].astype(float)
    # honest check: fit on the validation period, score on 2026-27 bookmaker lines
    Pv = fit_params(d[d["split"] == "validation"], team_pts)
    bb_path = os.path.join(vdir, "book_backtest.csv")
    val = None
    if os.path.exists(bb_path):
        bb = pd.read_csv(bb_path, dtype=ids)
        need = {"game_id", "player_id", "team"}
        if need <= set(bb.columns):
            meta = d[["game_id", "player_id", "is_home", "min_hat", "league"]].drop_duplicates(["game_id", "player_id"])
            bb = bb.drop(columns=["league"]).merge(meta, on=["game_id", "player_id"], how="inner")
            val = validate(Pv, bb)
    P = fit_params(d, team_pts)                           # production: validation + test
    P["version"] = version
    P["validation"] = val
    P["use"] = bool(val and val["use_copula"])
    with open(os.path.join(vdir, "gamesim.json"), "w", encoding="utf-8") as f:
        json.dump(P, f, indent=1)
    print(json.dumps({"validation": val, "rows": P["rows"], "games": P["games"]}, indent=1))
    return P


def load():
    """the live model's simulator parameters, or None (-> independent legs)"""
    try:
        version = open(os.path.join(MODEL_DIR, "CURRENT")).read().strip()
        with open(os.path.join(MODEL_DIR, version, "gamesim.json"), encoding="utf-8") as f:
            P = json.load(f)
        return P if P.get("use") else None
    except (OSError, ValueError):
        return None


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "fit":
        fit(sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        raise SystemExit(__doc__)
