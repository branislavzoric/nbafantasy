#!/usr/bin/env python3
"""
B3 Daily Card: one betting day of player-prop bets built from the Players +/- confidence numbers,
staked in units from a 100-unit bankroll, settled the next day and tracked in a ledger.

Source: "Players PM/data/<day>.json" (players_pm.export). Confidence is computed exactly like the
Players +/- page (players_pm_page.py JS): P(side) = 50% model (calibrated at the main line) +
30% recency-weighted hit rate (+ matchup, shrunk n/(n+10)) + 20% Soccerbet no-vig price.

Eligible legs (same rules for NBA and EuroLeague)
  - Soccerbet MAIN line with both an Over and an Under price (no milestones), game not started yet
  - player matched, 5+ games of history, not listed Out / Doubtful / Questionable
  - the chosen side's price is 1.40 or more, and confidence x odds >= 1: the price has to pay for the
    chance we give it (the most "confident" props are often short-priced Unders that lose money long term)
  - one leg per player per ticket, at most 2 legs from the same game (league tickets)

The card
  top20        the 20 most confident legs of the day (both leagues, every market, a player can have
               up to 2 markets), highest first
  el / nba     3-, 4- and 5-leg tickets per league, chosen from the 15 most confident legs of that league
               for the highest joint value (max 2 legs per game, no shared players between the three)
  games        one 4-leg ticket per game (4 different players of that game), chosen from that game's 12
               most confident legs for the highest joint value

Joint win chance: players_pm.gamesim simulates each game (game total, margin and blowout vs our
expectation, plus the links between players) and keeps every leg's own confidence. Soccerbet prices a
same-game ticket as if the legs were independent, so legs that win together are worth more than the
product says and legs that work against each other are worth less. If the simulator didn't beat
independence in its backtest, the product of the confidences is used instead.

Staking ("100-unit plan", 1u = 1% of the starting bankroll)
  singles      quarter Kelly on our confidence vs the price, from the current bankroll, 0.25u..2u
  tickets      3-leg 0.5u · 4-leg 0.5u · 5-leg 0.25u · game 4-leg 0.25u
  daily cap    20u in total; if the card would stake more, singles are scaled down

Locking: a card can be rebuilt until the first game on it starts; after that it is frozen, so the
results are honest. Settling uses the same day file after it has been re-exported with box scores.

Usage:
  python -m players_pm.daily_card build [DAY] [--force]
  python -m players_pm.daily_card build [DAY] --open    # after tip-off: rebuild only the games not started
  python -m players_pm.daily_card settle [DAY]          # default: yesterday's slate, plus today's
                                                        # slate once every game on its card is final
  (the slate day switches at 10:00 Belgrade, so a morning run settles last night automatically)
"""

import datetime as dt
import itertools
CARD_BOOK = "soccerbet"   # the daily card / Let's Bet uses Soccerbet lines only
import json
import math
import os
import sys
from zoneinfo import ZoneInfo

import numpy as np
from scipy import stats

from . import gamesim
from .common import PM_DIR, PROJECT_DIR

BEL = ZoneInfo("Europe/Belgrade")
DATA = os.path.join(PM_DIR, "data")
OUT = os.path.join(PROJECT_DIR, "Odds", "daily_card")
COLS = {"pts": 7, "reb": 8, "ast": 9, "tpm": 10, "stl": 11, "blk": 12, "tov": 13}
MK = {"Points": ["pts"], "Rebounds": ["reb"], "Assists": ["ast"], "3PM": ["tpm"], "Steals": ["stl"],
      "Blocks": ["blk"], "Turnovers": ["tov"], "Pts+Reb+Ast": ["pts", "reb", "ast"], "Pts+Reb": ["pts", "reb"],
      "Pts+Ast": ["pts", "ast"], "Reb+Ast": ["reb", "ast"]}
SHORT = {"Points": "PTS", "Rebounds": "REB", "Assists": "AST", "3PM": "3PM", "Steals": "STL", "Blocks": "BLK",
         "Turnovers": "TO", "Pts+Reb+Ast": "PRA", "Pts+Reb": "P+R", "Pts+Ast": "P+A", "Reb+Ast": "R+A"}
MIN_ODDS = 1.40
MAX_PER_PLAYER = 2      # markets per player in the top 20
BAD_STATUS = ("out", "doubtful", "questionable")
STAKE_TICKET = {3: 0.5, 4: 0.5, 5: 0.25}
STAKE_GAME = 0.25
DAILY_CAP = 20.0
POOL_GAME = 12          # legs per game considered for its 4-pack
POOL_LEAGUE = 15        # legs per league considered for the league tickets
N_SIM = 40000
START_BANKROLL = 100.0


def single_stake(p, odds, bankroll=START_BANKROLL):
    """quarter Kelly on our confidence, in quarter units, 0.25u..2u"""
    kelly = (p * odds - 1) / (odds - 1)
    u = 0.25 * kelly * bankroll
    return min(2.0, max(0.25, round(u * 4) / 4))


# ------------------------------------------------------------------------------------------ confidence
def stat_of(row, cols):
    v = 0
    for c in cols:
        x = row[COLS[c]]
        if x is None:
            return None
        v += x
    return v


def tally(vals, line):
    o = sum(v > line for v in vals)
    u = sum(v < line for v in vals)
    return {"n": len(vals), "o": o, "u": u, "p": len(vals) - o - u}


def matchup_ratio(D, p, cols, hist):
    r = (D.get("defense") or {}).get(p["league"], {}).get(p.get("opp"))
    if not r:
        return None
    num = den = 0.0
    for c in cols:
        if r.get(c) is None:
            return None
        avg = sum((h[COLS[c]] or 0) for h in hist) / len(hist) if hist else 1
        w = max(avg, 0.1)
        num += r[c] * w
        den += w
    return num / den


def hist_p_over(D, p, line, cols):
    pl = (D.get("players") or {}).get(f"{p['league']}:{p['pid']}") if p.get("pid") else None
    if not pl:
        return None, [], []
    hist = [r for r in pl["log"] if r[0] < D["day"] and r[5] == "played" and stat_of(r, cols) is not None]
    v = [stat_of(r, cols) for r in hist]
    cur = [stat_of(r, cols) for r in hist if r[1] == D["current_season"]]
    prev = [stat_of(r, cols) for r in hist if r[1] == "2025-26"]
    w = {"l5": tally(v[:5], line), "l10": tally(v[:10], line), "cur": tally(cur, line), "prev": tally(prev, line)}
    s = ws = 0.0
    for k, wt in (("l5", .35), ("l10", .30), ("cur", .20), ("prev", .15)):
        t = w[k]
        if t["n"] >= 3:
            s += wt * (t["o"] + t["p"] / 2) / t["n"]
            ws += wt
    if not ws or len(v) < 5:
        return None, v, hist
    ratio = matchup_ratio(D, p, cols, hist[:20])
    adj = max(-.1, min(.1, (ratio - 1) * .6)) if ratio is not None else 0
    n = min(len(v), 20)
    k = n / (n + 10)
    return min(.95, max(.05, .5 + (s / ws + adj - .5) * k)), v, hist


def model_p_over(D, pred, line, is_main):
    mu = max(pred["mu"], .05)
    var = max(pred["var"], mu * 1.001)
    r = mu * mu / (var - mu)
    q = r / (r + mu)
    nb = stats.nbinom(r, q)
    is_int = abs(line - round(line)) < 1e-9
    u = float(nb.cdf(math.ceil(line) - 1))
    push = float(nb.pmf(round(line))) if is_int else 0.0
    o = max(0.0, 1 - u - push)
    cond = o / max(o + u, 1e-9)
    cal = ((D.get("model") or {}).get("blend") or {}).get("cal")
    if is_main and cal:
        pc = min(1 - 1e-3, max(1e-3, cond))
        return 1 / (1 + math.exp(-(cal["c"] + cal["w"] * math.log(pc / (1 - pc))))), True
    return cond, False


def leg_for(D, p):
    """the main-line leg for one prop with its confidence, or None if it isn't eligible"""
    cols = MK.get(p["market"])
    L = next((l for l in p["lines"] if l["line"] == p["main"]), None)
    if not cols or not L or L.get("milestone") or not L.get("over") or not L.get("under") or not p.get("pid"):
        return None
    line = L["line"]
    ph, vals, hist = hist_p_over(D, p, line, cols)
    if ph is None:
        return None
    pred = p.get("pred")
    if pred and (pred.get("own_status") or "").lower().startswith(BAD_STATUS):
        return None
    # a key teammate (e.g. the star) is out tonight: lean on this player's own games without him (with/without)
    wowy = None
    ab = (D.get("_absent") or {}).get((p["league"], p.get("team")))
    if ab and ab["dates"] and hist:
        same = [(h, v) for h, v in zip(hist, vals) if h[2] == p.get("team")]   # only games for this team
        wo = [v for h, v in same if h[0] in ab["dates"]]
        wi = [v for h, v in same if h[0] not in ab["dates"]]
        if len(wo) >= 3 and wi and sum(wi) > 0:
            w = len(wo) / (len(wo) + 8)
            f = min(1.5, max(0.75, 1 + (sum(wo) / len(wo) / (sum(wi) / len(wi)) - 1) * w))
            t = tally(wo, line)
            ph = (1 - w) * ph + w * (t["o"] + t["p"] / 2) / t["n"]
            if pred:
                pred = {**pred, "mu": pred["mu"] * f, "var": pred["var"] * f}
            wowy = {"names": ab["names"], "n": len(wo), "avg_wo": round(sum(wo) / len(wo), 1),
                    "avg_with": round(sum(wi) / len(wi), 1), "factor": round(f, 3)}
    parts = []
    if pred:
        pm, calibrated = model_p_over(D, pred, line, True)
        parts.append(("model", .5, pm))
    parts.append(("hit rate", .3, ph))
    mkt = (1 / L["over"]) / ((1 / L["over"]) + (1 / L["under"]))
    parts.append(("market", .2, mkt))
    ws = sum(w for _, w, _ in parts)
    p_over = sum(w * x for _, w, x in parts) / ws
    side = "Over" if p_over >= .5 else "Under"
    conf = p_over if side == "Over" else 1 - p_over
    odds = L["over"] if side == "Over" else L["under"]
    if odds < MIN_ODDS or conf * odds < 1:          # the price must pay for the chance (no negative-value bets)
        return None
    return {"key": f"{p['league']}|{p['game']}|{p['pid']}|{p['market']}", "league": p["league"], "game": p["game"],
            "pid": p["pid"], "player": p["player"], "team": p.get("team"), "opp": p.get("opp"),
            "market": p["market"], "short": SHORT[p["market"]], "line": line, "side": side, "odds": odds,
            "conf": round(conf, 4), "fair": round(mkt if side == "Over" else 1 - mkt, 4),
            "edge": round(conf - (mkt if side == "Over" else 1 - mkt), 4),
            "ev": round(conf * odds - 1, 4), "l5": vals[:5], "l10_avg": round(sum(vals[:10]) / len(vals[:10]), 1),
            "l10": [{"d": h[0], "opp": h[3], "v": v} for h, v in zip(hist[:10], vals[:10])],
            "proj": round(pred["mu"], 1) if pred else None, "kickoff": p.get("kickoff"),
            "home": bool(p.get("is_home")), "min_hat": (pred or {}).get("min_hat"), "wowy": wowy}


def expected_usage(D, started):
    """xUSG% for every player of tonight's not-started games (players_pm.usage): baseline usage + the
    team's 100% constraint with tonight's expected minutes (model min_hat) and known absences."""
    from . import usage
    from .common import engine
    games, hint, outs = [], {}, {}
    inj = D.get("injuries") or {}
    for g in D.get("games", []):
        if g["id"] in started:
            continue
        for team, opp in ((g["home"], g["away"]), (g["away"], g["home"])):
            games.append({"league": g["league"], "game": g["id"], "team": team, "opp": opp})
            if g["league"] == "nba":
                outs[(g["league"], team)] = {fold(x["name"]) for x in inj.get(team, []) if (x.get("status") or "") == "Out"}
    for p in D.get("props", []):
        pr = p.get("pred") or {}
        if p.get("pid") and pr.get("min_hat") is not None:
            hint[(p["league"], p["pid"])] = {"min": pr["min_hat"], "team": p.get("team")}
        if (pr.get("own_status") or "").lower().startswith(BAD_STATUS):
            outs.setdefault((p["league"], p.get("team")), set()).add(fold(p["player"]))
        for n in pr.get("out") or []:
            outs.setdefault((p["league"], p.get("team")), set()).add(fold(n if isinstance(n, str) else n.get("name", "")))
    return usage.expected_for_day(engine(), games, hint, outs)


def injury_snapshot(D):
    """statuses this card was built with (official NBA report over ESPN), for players_pm.injury_watch"""
    try:
        from .model import injuries_now
        from .common import engine
        teams = {t for g in D.get("games", []) if g["league"] == "nba" for t in (g["home"], g["away"])}
        inj, _ = injuries_now(engine())
        return {pid: [r["player_name"], r["team_code"], r["status"]] for (lg, pid), r in inj.items()
                if lg == "nba" and r.get("team_code") in teams}
    except Exception as e:
        print("injury snapshot failed:", e)
        return {}


def fold(name):
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", name or "") if not unicodedata.combining(c)).lower().strip()


# ------------------------------------------------------------------------------------------ tickets
class Sim:
    """one simulation of every candidate leg: games simulated jointly (copula) or, without simulator
    parameters, independently. Any ticket's joint chance = share of draws where all its legs win."""

    def __init__(self, legs, P, n=N_SIM):
        self.P, self.n, self.win, self.z, self.idx = P, n, {}, {}, {}
        by = {}
        for l in legs:
            by.setdefault(l["game"], []).append(l)
        rng = np.random.default_rng(20261107)
        for gi, (g, gl) in enumerate(sorted(by.items())):
            if P:
                w, z = gamesim.simulate_game(P, [{**l, "p": l["conf"]} for l in gl], n, seed=1000 + gi)
                self.z[g] = z
            else:
                w = rng.random((n, len(gl))) < np.array([l["conf"] for l in gl])
            for i, l in enumerate(gl):
                self.win[l["key"]] = w[:, i]

    def chance(self, keys):
        ok = np.ones(self.n, bool)
        for k in keys:
            ok &= self.win[k]
        return float(ok.mean()), ok

    def lean(self, keys, card_games):
        """per game: 'this ticket wins when ...' from the factor draws where it wins"""
        if not self.P:
            return {}
        _, ok = self.chance(keys)
        if ok.sum() < 50:
            return {}
        out = {}
        for g in {k.split("|")[1] for k in keys}:
            if g not in self.z:
                continue
            m = self.z[g][ok].mean(axis=0)
            gm = card_games.get(g, {})
            out[g] = gamesim.lean_text(self.P, dict(zip(gamesim.FACTORS, m)), gm.get("league"),
                                       gm.get("home"), gm.get("away"))
        return out


def best_ticket(sim, legs, n, used=frozenset(), per_game=2):
    """highest joint EV among n-leg combos of `legs`: different players, at most per_game legs per game,
    players in `used` avoided when possible"""
    best = None
    for pass_used in (used, frozenset()):
        pool = [l for l in legs if (l["league"], l["pid"]) not in pass_used]
        for combo in itertools.combinations(pool, n):
            if len({(l["league"], l["pid"]) for l in combo}) < n:
                continue
            games = {}
            for l in combo:
                games[l["game"]] = games.get(l["game"], 0) + 1
            if max(games.values()) > per_game:
                continue
            pj, _ = sim.chance([l["key"] for l in combo])
            ev = pj * math.prod(l["odds"] for l in combo) - 1
            if best is None or ev > best[0]:
                best = (ev, list(combo))
        if best:
            break
    return best[1] if best else None


def pick_ticket(legs, n, used=frozenset(), per_game=2):
    """greedy: most confident legs, one per player, max per_game legs per game, avoiding players already
    used on the other tickets of the same league (reused only when there aren't enough legs)"""
    out, players, games = [], set(), {}
    for pass_used in (used, frozenset()):
        for l in legs:
            if len(out) == n:
                break
            if (l["league"], l["pid"]) in pass_used or l in out or (l["league"], l["pid"]) in players:
                continue
            if games.get(l["game"], 0) >= per_game:
                continue
            out.append(l)
            players.add((l["league"], l["pid"]))
            games[l["game"]] = games.get(l["game"], 0) + 1
        if len(out) == n:
            break
    return out if len(out) == n else None


def ticket(name, kind, legs, stake, sim=None, card_games=None):
    odds = math.prod(l["odds"] for l in legs)
    p_ind = math.prod(l["conf"] for l in legs)
    p = sim.chance([l["key"] for l in legs])[0] if sim else p_ind
    t = {"name": name, "kind": kind, "legs": [l["key"] for l in legs], "n": len(legs), "odds": round(odds, 2),
         "win_chance": round(p, 4), "win_chance_indep": round(p_ind, 4), "ev": round(p * odds - 1, 4),
         "stake": stake, "to_win": round(stake * odds, 2)}
    if sim and sim.P:
        t["wins_when"] = sim.lean([l["key"] for l in legs], card_games or {})
    return t


def game_reads(D, started):
    """per game: our expected total / margin (team ratings, from the model rows) vs Soccerbet's last lines"""
    out = {}
    exp = {}
    for p in D.get("props", []):
        pr = p.get("pred") or {}
        if p.get("game") and pr.get("exp_team_pts") is not None and p.get("team"):
            exp.setdefault(p["game"], {})[p["team"]] = (pr["exp_team_pts"], pr.get("exp_margin"))
    for g in D.get("games", []):
        if g["id"] in started or g["id"] not in exp:
            continue
        e = exp[g["id"]]
        h, a = e.get(g["home"]), e.get(g["away"])
        if not h or not a:
            continue
        gl = ((D.get("game_lines") or {}).get(g["id"]) or {}).get("last") or {}
        out[g["id"]] = {"our_total": round(h[0] + a[0], 1), "our_margin_home": round(h[1], 1) if h[1] is not None else None,
                        "sb_total": gl.get("total"),
                        "sb_margin_home": (-gl["spread_home"] if gl.get("spread_home") is not None else None)}
    return out


def rebuild_open(day):
    """Rebuild the card for the games that have NOT started (new lines / injuries), keeping everything
    on games that already tipped exactly as it was (those bets are live and settle as placed)."""
    card_path = os.path.join(OUT, day, "card.json")
    with open(card_path, encoding="utf-8") as f:
        old = json.load(f)
    now = dt.datetime.now(BEL)
    with open(os.path.join(DATA, f"{day}.json"), encoding="utf-8") as f:
        D = json.load(f)
    started = {g["id"] for g in D.get("games", [])
               if g.get("status") != "scheduled" or dt.datetime.fromisoformat(g["start"]) <= now}
    is_locked = lambda k: k.split("|")[1] in started          # key = league|game|pid|market
    keep_top = [s for s in old["top20"] if is_locked(s["leg"])]
    keep_t = [t for t in old["tickets"] if any(is_locked(k) for k in t["legs"])]
    keep_g = [t for t in old["games"] if t["game"] in started]
    bak = card_path.replace("card.json", "card_before_open_rebuild.json")
    if not os.path.exists(bak):
        with open(bak, "w", encoding="utf-8") as f:
            json.dump(old, f, ensure_ascii=False, indent=1)
    new = build(day, force=True)                         # open games only (build skips started games)
    nlegs = {k: l for k, l in new["legs"].items()}
    fresh_top = [s for s in new["top20"]]
    allk = {**{k: l for k, l in old["legs"].items() if is_locked(k)}, **nlegs}
    # a kept ticket can hold legs on games that have NOT started (placed as a whole at the old price):
    # keep those legs exactly as placed, even if the prop is no longer listed (e.g. pulled after injury news)
    for k in {k for t in keep_t + keep_g for k in t["legs"]}:
        if k in old["legs"]:
            allk[k] = old["legs"][k]
    have = {s["leg"] for s in keep_top}                        # placed bets on started games always stay
    top = sorted(keep_top + [s for s in fresh_top if s["leg"] not in have][:max(0, 20 - len(keep_top))],
                 key=lambda s: -allk[s["leg"]]["conf"])
    t_names = {t["name"] for t in keep_t}
    tickets = keep_t + [t for t in new["tickets"] if t["name"] not in t_names]
    games = keep_g + [t for t in new["games"] if t["game"] not in started]
    best = [k for k in new.get("best") or [] if k in allk]
    used = {s["leg"] for s in top} | {k for t in tickets + games for k in t["legs"]} | set(best)
    stake = lambda: sum(s["stake"] for s in top) + sum(t["stake"] for t in tickets + games)
    while stake() > DAILY_CAP + 1e-9:                    # trim the open singles to stay under the cap
        big = max((s for s in top if s["stake"] > 0.25 and not is_locked(s["leg"])), key=lambda s: s["stake"], default=None)
        if not big:
            break
        big["stake"] -= 0.25
    card = {**new, "legs": {k: l for k, l in allk.items() if k in used}, "top20": top, "tickets": tickets,
            "games": games, "staked": round(stake(), 2), "first_tip": old["first_tip"],
            "best": best,
            "rebuilt_open": now.isoformat(timespec="seconds"), "built_first": old.get("built_first", old["built"]),
            "locked_games": sorted(started)}
    with open(card_path, "w", encoding="utf-8") as f:
        json.dump(card, f, ensure_ascii=False, indent=1)
    print(f"{day}: open games rebuilt; kept {len(keep_top)} singles, {len(keep_t)} league tickets, {len(keep_g)} game "
          f"tickets on started games; {card['staked']}u staked")
    return card


def build(day, force=False):
    path = os.path.join(DATA, f"{day}.json")
    with open(path, encoding="utf-8") as f:
        D = json.load(f)
    out_dir = os.path.join(OUT, day)
    card_path = os.path.join(out_dir, "card.json")
    now = dt.datetime.now(BEL)
    if os.path.exists(card_path) and not force:
        with open(card_path, encoding="utf-8") as f:
            old = json.load(f)
        if old.get("first_tip") and dt.datetime.fromisoformat(old["first_tip"]) <= now:
            print(f"{day}: card is locked (first game started {old['first_tip']}), not rebuilt")
            return old
    started = {g["id"] for g in D.get("games", [])
               if g.get("status") != "scheduled" or dt.datetime.fromisoformat(g["start"]) <= now}
    try:
        xu = expected_usage(D, started)
    except Exception as e:                     # usage is extra information, never blocks the card
        print("expected usage failed:", e)
        xu = {}
    D["_absent"] = {(k[1], k[2]): v for k, v in xu.items() if k[0] == "_absent"}
    for (lg, team), v in D["_absent"].items():
        print(f"  key absence {lg} {team}: {', '.join(v['names'])} -> {len(v['dates'] or [])} team games without")
    legs = []
    for p in D.get("props", []):
        if p.get("book") != CARD_BOOK:                 # Let's Bet stays Soccerbet-only (other books are page-only)
            continue
        if p.get("game") in started or not p.get("game"):
            continue
        l = leg_for(D, p)
        if l:
            legs.append(l)
    legs.sort(key=lambda l: (-l["conf"], -l["ev"], l["player"]))
    # the 20 most confident legs, every market, at most MAX_PER_PLAYER markets per player (user, 2026-11-08)
    top, per = [], {}
    for l in legs:
        k = (l["league"], l["pid"])
        if per.get(k, 0) >= MAX_PER_PLAYER:
            continue
        per[k] = per.get(k, 0) + 1
        top.append(l)
        if len(top) == 20:
            break
    bankroll = current_bankroll()
    singles = [{"leg": l["key"], "stake": single_stake(l["conf"], l["odds"], bankroll)} for l in top]

    P = gamesim.load()
    cg = {g["id"]: g for g in D.get("games", [])}
    # candidate pools: the most confident legs per league and per game
    pools = {lg: [l for l in legs if l["league"] == lg][:POOL_LEAGUE] for lg in ("el", "nba")}
    gpools = {}
    for g in D.get("games", []):
        if g["id"] not in started:
            gpools[g["id"]] = [l for l in legs if l["game"] == g["id"]][:POOL_GAME]
    cand = {l["key"]: l for pl in list(pools.values()) + list(gpools.values()) for l in pl}
    sim = Sim(list(cand.values()), P)
    tickets = []
    for lg, label in (("el", "EuroLeague"), ("nba", "NBA")):
        used = set()
        for n, name in ((3, "Treble"), (4, "Four-fold"), (5, "Five-fold")):
            t = best_ticket(sim, pools[lg], n, frozenset(used))
            if t:
                used |= {(l["league"], l["pid"]) for l in t}
                tickets.append({**ticket(f"{label} {name}", lg, t, STAKE_TICKET[n], sim, cg), "league": lg})
    games = []
    for g in sorted(D.get("games", []), key=lambda g: g["start"]):
        if g["id"] in started:
            continue
        t = best_ticket(sim, gpools[g["id"]], 4, per_game=4)
        if t:
            games.append({**ticket(f"{g['away']} @ {g['home']}", "game", t, STAKE_GAME, sim, cg), "league": g["league"],
                          "game": g["id"], "start": g["start"], "home": g["home"], "away": g["away"]})

    total = sum(s["stake"] for s in singles) + sum(t["stake"] for t in tickets) + sum(t["stake"] for t in games)
    if total > DAILY_CAP:
        rest = total - sum(s["stake"] for s in singles)
        scale = max(0.0, (DAILY_CAP - rest)) / sum(s["stake"] for s in singles)
        for s in singles:
            s["stake"] = round(max(0.25, s["stake"] * scale) * 4) / 4      # quarter units
    while sum(s["stake"] for s in singles) + sum(t["stake"] for t in tickets + games) > DAILY_CAP + 1e-9:
        big = max((s for s in singles if s["stake"] > 0.25), key=lambda s: s["stake"], default=None)
        if not big:
            break
        big["stake"] -= 0.25                                              # rounding can overshoot the cap
    # best bets tonight: biggest expected value among confident legs (conf >= 55%), one per player
    best, seen = [], set()
    for l in sorted(legs, key=lambda l: (-l["ev"], -l["conf"])):
        if l["conf"] >= .55 and l["ev"] > 0 and (l["league"], l["pid"]) not in seen:
            seen.add((l["league"], l["pid"]))
            best.append(l["key"])
            if len(best) == 5:
                break
    used_keys = {s["leg"] for s in singles} | {k for t in tickets + games for k in t["legs"]} | set(best)
    for l in legs:
        u = xu.get((l["league"], l["pid"]))
        if u:
            l["usg"] = u
    first_tip = min((l["kickoff"] for l in legs if l["key"] in used_keys and l.get("kickoff")), default=None)
    card = {"day": day, "built": now.isoformat(timespec="seconds"), "lines_at": max((p["scraped_at"] for p in D["props"] if p.get("book") == CARD_BOOK), default=None),
            "first_tip": first_tip, "teams": {k: {"name": v.get("name"), "short": v.get("short"), "logo": v.get("logo")}
                                              for k, v in (D.get("teams") or {}).items()},
            "games_meta": {g["id"]: {"league": g["league"], "home": g["home"], "away": g["away"], "start": g["start"],
                                     "round": g.get("round")} for g in D.get("games", [])},
            "legs": {l["key"]: l for l in legs if l["key"] in used_keys},
            "top20": singles, "best": best, "tickets": tickets, "games": games,
            "staked": round(sum(s["stake"] for s in singles) + sum(t["stake"] for t in tickets + games), 2),
            "eligible_legs": len(legs),
            "rules": {"min_odds": MIN_ODDS, "daily_cap": DAILY_CAP, "stake_ticket": STAKE_TICKET, "stake_game": STAKE_GAME},
            "joint_model": ({"kind": "game simulator", "version": P.get("version"),
                             "validation": P.get("validation")} if P else {"kind": "independent legs"}),
            "game_reads": game_reads(D, started), "injury_seen": injury_snapshot(D)}
    os.makedirs(out_dir, exist_ok=True)
    with open(card_path, "w", encoding="utf-8") as f:
        json.dump(card, f, ensure_ascii=False, indent=1)
    print(f"{day}: {len(legs)} eligible legs, top 20 + {len(tickets)} league tickets + {len(games)} game tickets, "
          f"{card['staked']}u staked")
    return card


# ------------------------------------------------------------------------------------------ settle
def leg_result(D, leg):
    g = next((x for x in D.get("games", []) if x["id"] == leg["game"]), None)
    if not g or g.get("status") != "final":
        return "open", None
    pl = (D.get("players") or {}).get(f"{leg['league']}:{leg['pid']}")
    row = next((r for r in (pl or {}).get("log", []) if r[0] == D["day"]), None)
    if not row or row[5] != "played":
        return "void", None                         # DNP / not in the final box score -> void
    v = stat_of(row, MK[leg["market"]])
    if v is None:
        return "void", None
    if v == leg["line"]:
        return "void", v
    won = v > leg["line"] if leg["side"] == "Over" else v < leg["line"]
    return ("won" if won else "lost"), v


# ------------------------------------------------------------------------------------------ CLV
CLV_MIN_GAP_MIN = 10        # a closing snapshot must be scraped at least this long after the card was built


def _fair(ln, side):
    """no-vig probability of `side` at one two-way Soccerbet line (proportional margin removal)"""
    io, iu = 1 / ln["over"], 1 / ln["under"]
    po = io / (io + iu)
    return po if side == "Over" else 1 - po


def leg_clv(D, leg, built):
    """closing-line value of one leg: our price vs Soccerbet's last pre-tip line, margin removed.
    Same line: clv = odds * fair_close - 1. Line moved: the close's fair probability at its own main
    line is moved to our line with the model's distribution (P(our line) - P(close line)).
    Returns a dict, or {"status": "no close"} when no snapshot after the card build exists."""
    pr = next((p for p in D.get("props", []) if p.get("book", CARD_BOOK) == CARD_BOOK
               and p.get("league") == leg["league"] and p.get("game") == leg["game"]
               and p.get("pid") == leg["pid"] and p.get("market") == leg["market"]), None)
    if not pr:
        return {"status": "no close", "why": "prop not in the day file"}
    at = dt.datetime.fromisoformat(pr["scraped_at"])
    if at < dt.datetime.fromisoformat(built) + dt.timedelta(minutes=CLV_MIN_GAP_MIN):
        return {"status": "no close", "why": "no Soccerbet snapshot after the card was built"}
    tip = dt.datetime.fromisoformat(pr["kickoff"])
    two = [l for l in pr["lines"] if l.get("over") and l.get("under") and not l.get("milestone")]
    if not two:
        return {"status": "no close", "why": "line taken down before tip"}
    out = {"close_at": pr["scraped_at"], "min_before_tip": round((tip - at).total_seconds() / 60)}
    same = next((l for l in two if l["line"] == leg["line"]), None)
    main = next((l for l in two if l["line"] == pr["main"]), two[0])
    if same:
        fc = _fair(same, leg["side"])
        out.update(close_line=same["line"], close_odds=same["over"] if leg["side"] == "Over" else same["under"],
                   method="same line")
    else:
        pred = pr.get("pred")
        if not pred:
            return {"status": "no close", "why": "line moved and no model distribution to convert it"}
        po_our, _ = model_p_over(D, pred, leg["line"], False)
        po_cls, _ = model_p_over(D, pred, main["line"], False)
        shift = (po_our - po_cls) if leg["side"] == "Over" else (po_cls - po_our)
        fc = min(.99, max(.01, _fair(main, leg["side"]) + shift))
        out.update(close_line=main["line"], close_odds=main["over"] if leg["side"] == "Over" else main["under"],
                   method="line moved (model-converted)")
    out.update(status="ok", fair_close=round(fc, 4), clv=round(leg["odds"] * fc - 1, 4),
               beat=leg["odds"] * fc > 1)
    return out


def clv_summary(items):
    xs = [x["clv"] for x in items if x and x.get("status") == "ok"]
    return {"n": len(xs), "of": len(items), "avg": round(sum(xs) / len(xs), 4) if xs else None,
            "beat_pct": round(sum(x > 0 for x in xs) / len(xs), 3) if xs else None}


def settle_ticket(t, res, legs):
    st = [res[k][0] for k in t["legs"]]
    if "lost" in st:
        return "lost", 0.0, t["odds"]
    if "open" in st:
        return "open", None, t["odds"]
    odds = math.prod(legs[k]["odds"] for k in t["legs"] if res[k][0] == "won")
    if all(s == "void" for s in st):
        return "void", t["stake"], 1.0
    return "won", round(t["stake"] * odds, 2), round(odds, 2)


def settle(day, final_only=False):
    card_path = os.path.join(OUT, day, "card.json")
    if not os.path.exists(card_path):
        print(f"{day}: no card")
        return None
    with open(card_path, encoding="utf-8") as f:
        card = json.load(f)
    with open(os.path.join(DATA, f"{day}.json"), encoding="utf-8") as f:
        D = json.load(f)
    res = {k: leg_result(D, l) for k, l in card["legs"].items()}
    if final_only and any(s == "open" for s, _ in res.values()):
        print(f"{day}: games still to finish, not settled yet")
        return None
    clv = {k: leg_clv(D, l, card["built"]) for k, l in card["legs"].items()}
    out = {"day": day, "settled": dt.datetime.now(BEL).isoformat(timespec="seconds"),
           "legs": {k: {"status": s, "value": v, "clv": clv[k]} for k, (s, v) in res.items()},
           "singles": [], "tickets": [], "games": []}
    for s in card["top20"]:
        st, v = res[s["leg"]]
        odds = card["legs"][s["leg"]]["odds"]
        ret = s["stake"] * odds if st == "won" else s["stake"] if st == "void" else 0.0 if st == "lost" else None
        out["singles"].append({**s, "status": st, "value": v, "return": None if ret is None else round(ret, 2),
                               "clv": clv[s["leg"]].get("clv")})
    for grp in ("tickets", "games"):
        for t in card[grp]:
            st, ret, odds = settle_ticket(t, res, card["legs"])
            cs = [clv[k] for k in t["legs"]]
            tclv = (round(math.prod(1 + c["clv"] for c in cs) - 1, 4) if all(c.get("status") == "ok" for c in cs)
                    else None)                                   # ticket CLV only when every leg has a close
            out[grp].append({"name": t["name"], "status": st, "return": ret, "stake": t["stake"], "paid_odds": odds,
                             "clv": tclv})
    done = [x for g in ("singles", "tickets", "games") for x in out[g] if x["status"] != "open"]
    out["open"] = sum(1 for g in ("singles", "tickets", "games") for x in out[g] if x["status"] == "open")
    out["staked"] = round(sum(x["stake"] for x in done), 2)
    out["returned"] = round(sum(x["return"] for x in done), 2)
    out["profit"] = round(out["returned"] - out["staked"], 2)
    by = {}
    for g in ("singles", "tickets", "games"):
        xs = [x for x in out[g] if x["status"] != "open"]
        by[g] = {"won": sum(x["status"] == "won" for x in xs), "lost": sum(x["status"] == "lost" for x in xs),
                 "void": sum(x["status"] == "void" for x in xs), "staked": round(sum(x["stake"] for x in xs), 2),
                 "profit": round(sum(x["return"] - x["stake"] for x in xs), 2)}
    out["by"] = by
    out["clv"] = {"legs": clv_summary(list(clv.values())),
                  "singles": clv_summary([clv[x["leg"]] for x in card["top20"]])}
    with open(os.path.join(OUT, day, "results.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    write_ledger()
    still = f", {out['open']} still open" if out["open"] else ""
    c = out["clv"]["singles"]
    ctxt = (f", CLV {c['avg']:+.1%} on {c['n']}/{c['of']} singles ({c['beat_pct']:.0%} beat the close)" if c["n"]
            else ", CLV: no closing snapshot (run refresh_close.cmd before tip-off)")
    print(f"{day}: {by['singles']['won']}-{by['singles']['lost']} singles, profit {out['profit']:+.2f}u{still}{ctxt}")
    return out


def current_bankroll():
    p = os.path.join(OUT, "ledger.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f).get("bankroll") or START_BANKROLL
    return START_BANKROLL


def write_ledger():
    days, bank = [], START_BANKROLL
    for day in sorted(os.listdir(OUT)) if os.path.isdir(OUT) else []:
        rp = os.path.join(OUT, day, "results.json")
        if not os.path.exists(rp):
            continue
        with open(rp, encoding="utf-8") as f:
            r = json.load(f)
        bank += r["profit"]
        days.append({"day": day, "staked": r["staked"], "returned": r["returned"], "profit": r["profit"],
                     "bankroll": round(bank, 2), "open": r["open"], "by": r["by"],
                     "clv": (r.get("clv") or {}).get("singles")})
    staked = sum(d["staked"] for d in days)
    profit = sum(d["profit"] for d in days)
    cn = sum((d["clv"] or {}).get("n") or 0 for d in days)
    cs = sum(((d["clv"] or {}).get("avg") or 0) * ((d["clv"] or {}).get("n") or 0) for d in days)
    cb = sum(((d["clv"] or {}).get("beat_pct") or 0) * ((d["clv"] or {}).get("n") or 0) for d in days)
    led = {"start": START_BANKROLL, "bankroll": round(bank, 2), "staked": round(staked, 2), "profit": round(profit, 2),
           "roi": round(profit / staked, 4) if staked else None,
           "clv": {"n": cn, "avg": round(cs / cn, 4) if cn else None, "beat_pct": round(cb / cn, 3) if cn else None},
           "days": days}
    with open(os.path.join(OUT, "ledger.json"), "w", encoding="utf-8") as f:
        json.dump(led, f, ensure_ascii=False, indent=1)
    return led


def slate_day(now):
    d = now.date()
    return d - dt.timedelta(days=1) if now.hour < 10 else d


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    cmd = args[0] if args else "build"
    today = slate_day(dt.datetime.now(BEL))
    if cmd == "build":
        day = args[1] if len(args) > 1 else today.isoformat()
        rebuild_open(day) if "--open" in sys.argv else build(day, force="--force" in sys.argv)
    elif cmd == "settle":
        if len(args) > 1:
            settle(args[1])
        else:
            settle((today - dt.timedelta(days=1)).isoformat())
            settle(today.isoformat(), final_only=True)   # e.g. a 09:00 run after last night's games
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
