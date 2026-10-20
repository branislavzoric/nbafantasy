#!/usr/bin/env python3
"""
build_boxscore_chart.py

One game's box score as team views on the depth-chart court (PNG, 1080x1350 per
team): starters in the top half, the next five bench players by minutes in the
bottom half, the rest of the bench in the right panel (compact rows), DNPs listed
below, then a highlights box (top performer, efficiency, bench spark, cold night).

Every player shows Yahoo default fantasy points plus PTS/REB/AST/STL/BLK and
2PM-2PA, 3PM-3PA, FTM-FTA. Fire / cold emoji mark hot and cold nights (HOT_* /
COLD_* rules below). Data: nba_games / nba_boxscores (ESPN) -- run
collect_boxscores.py first.

Usage:
  python build_boxscore_chart.py 2026-10-07 OKC     # both teams of OKC's game
  python build_boxscore_chart.py                     # every game from yesterday
Output: "Box Score Charts/<date>/<AWAY>-at-<HOME>_<TEAM>.png"
"""

import datetime as dt
import html
import os
import shutil
import sys
import tempfile

from sqlalchemy import create_engine, text

from build_depth_chart import COURT_W, PX_PER_FT, logo_svg, name_size, short_name
from build_nightly_cards import ET, date_label, headshot, jerseys, team_key
from build_player_cards import TEAM_NAMES, file_url, find_browser, font_css, make_dir, render_png, team_vars

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(PROJECT_DIR, "boxscore_chart_template.html")
OUT_DIR = os.path.join(PROJECT_DIR, "Box Score Charts")
CHART_W, CHART_H = 540, 675

# 🔥: a big fantasy night, or a very productive one per minute
HOT_FPTS, HOT_PER_MIN, HOT_MIN_FOR_RATE = 30.0, 1.5, 10
# 🥶: real minutes but little production, or a bad shooting night that the rest
# of the line didn't make up for
COLD_MIN, COLD_PER_MIN, COLD_FGA, COLD_FG_PCT, COLD_SHOOTER_PER_MIN = 10, 0.36, 5, 0.25, 1.0
FIRE, ICE = "🔥", "🥶"

# Court spots in feet (50 x 94 court), filled G -> F -> C as PG, SG, SF, PF, C.
SPOTS = [(25, 34.5), (40, 21.5), (10, 21.5), (37.5, 7.5), (12.5, 7.5)]
TOKEN_W, TOKEN_H = 142, 66
POS_ORDER = {"PG": 0, "G": 0, "SG": 1, "GF": 1, "SF": 2, "F": 2, "PF": 3, "FC": 3, "C": 4}

# -----------------------------------------------------------------------------
# DATA
# -----------------------------------------------------------------------------

def load_game(game_date, team):
    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as conn:
        game = conn.execute(text(
            "SELECT * FROM nba_games WHERE game_date = :d AND (home_team = :t OR away_team = :t)"),
            {"d": game_date, "t": team}).mappings().first()
        if game is None:
            raise SystemExit(f"No {team} game on {game_date} -- run collect_boxscores.py first.")
        rows = [dict(r) for r in conn.execute(text(
            "SELECT * FROM nba_boxscores WHERE game_id = :g"), {"g": game["game_id"]}).mappings()]
    return dict(game), rows


def games_on(game_date):
    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as conn:
        return [r["home_team"] for r in conn.execute(text(
            "SELECT home_team FROM nba_games WHERE game_date = :d AND status = 'STATUS_FINAL' "
            "ORDER BY start_time"), {"d": game_date}).mappings()]


def played(p):
    return not p["did_not_play"] and p["min"] is not None


def mood(p):
    """'hot', 'cold' or ''."""
    fpts, mins = p["fpts"] or 0, p["min"] or 0
    if fpts >= HOT_FPTS or (mins >= HOT_MIN_FOR_RATE and fpts / mins >= HOT_PER_MIN):
        return "hot"
    if mins >= COLD_MIN and (fpts / mins < COLD_PER_MIN
                             or (p["fga"] >= COLD_FGA and p["fgm"] / p["fga"] <= COLD_FG_PCT
                                 and fpts / mins < COLD_SHOOTER_PER_MIN)):
        return "cold"
    return ""


def split_units(players):
    """(starters, second unit, rest of the bench, DNPs)."""
    active = [p for p in players if played(p)]
    starters = [p for p in active if p["starter"]]
    bench = sorted((p for p in active if not p["starter"]), key=lambda p: (-p["min"], -(p["fpts"] or 0)))
    dnp = [p for p in players if not played(p)]
    return starters, bench[:5], bench[5:], dnp

# -----------------------------------------------------------------------------
# RENDER
# -----------------------------------------------------------------------------

def face_html(p):
    e = html.escape
    first, last = short_name(p["player_name"])
    path = headshot(str(p["player_id"]))
    inner = (f'<img src="{e(file_url(path))}" alt="">' if path
             else f'<span class="initials">{e(first[0] + last[0])}</span>')
    return f'<div class="face">{inner}</div>'


def display_name(p):
    return p["player_name"] if p.get("dup_last") else short_name(p["player_name"])[1]


COUNTING = (("pts", "PTS"), ("reb", "REB"), ("ast", "AST"), ("stl", "STL"), ("blk", "BLK"))
# Yahoo default points per unit -- used to rank which stats mattered most
FPTS_WEIGHT = {"pts": 1, "reb": 1.2, "ast": 1.5, "stl": 3, "blk": 3}


def top_stats(p, n=3):
    """The (up to) n counting stats that produced the most fantasy points. At least
    one shown stat stays plain so the highlight still means something."""
    ranked = sorted((k for k, _ in COUNTING if p[k]), key=lambda k: -p[k] * FPTS_WEIGHT[k])
    return set(ranked[:max(1, min(n, len(ranked) - 1))])


def add_usage(players):
    """USG% = the player's share of the team's field-goal attempts."""
    team_fga = sum(p["fga"] or 0 for p in players if played(p))
    for p in players:
        p["usg"] = 100 * p["fga"] / team_fga if team_fga and played(p) and p["fga"] else 0


def box_inner(p, jersey, first_line=False):
    """Name + fantasy points, the counting stats, and the shooting splits."""
    e = html.escape
    first, _ = short_name(p["player_name"])
    name = display_name(p)
    parts = [first if first_line and not p.get("dup_last") else "",
             p["position"] if p["position"] not in (None, "NA") else "", f"#{jersey}" if jersey else ""]
    label = " · ".join(x for x in parts if x)
    icon = {"hot": FIRE, "cold": ICE}.get(mood(p), "")
    shown = [(k, lab) for k, lab in COUNTING if p[k]]
    key = top_stats(p)
    stats = "".join(f'<div{" class=hi" if k in key else ""}><b>{p[k]}</b><span>{lab}</span></div>'
                    for k, lab in shown) or '<div class="none">No counting stats</div>'
    two_m, two_a = p["fgm"] - p["tpm"], p["fga"] - p["tpa"]
    shots = "".join(f'<span>{lab}<b>{m}-{a}</b></span>'
                    for lab, m, a in (("2P", two_m, two_a), ("3P", p["tpm"], p["tpa"]), ("FT", p["ftm"], p["fta"]))
                    if a)
    if p.get("usg"):
        shots += f'<span class="usg">USG<b>{p["usg"]:.0f}%</b></span>'
    shots += f'<span class="min">MIN<b>{p["min"]}</b></span>'
    badge = f'<i class="badge">{icon}</i>' if icon else ""
    return (f'{badge}<div class="top">{face_html(p)}<div class="who"><div class="pos">{e(label)}</div>'
            f'<div class="name{" xxlong" if len(name) > 15 else name_size(name)}">{e(name)}</div></div>'
            f'<div class="fp"><b>{p["fpts"]:.1f}</b><span>FPTS</span></div></div>'
            f'<div class="line" style="--n:{max(len(shown), 1)}">{stats}</div><div class="shoot">{shots}</div>')


def order_on_court(unit):
    return sorted(unit, key=lambda p: (POS_ORDER.get((p["position"] or "F").upper(), 2), -p["min"]))


def token_html(p, jersey, spot, second_unit):
    x, y = spot
    if second_unit:
        y = 94 - y
    left = min(max(x * PX_PER_FT - TOKEN_W / 2, 3), COURT_W - TOKEN_W - 3)
    top = y * PX_PER_FT - TOKEN_H / 2
    cls = " ".join(c for c in ("token", "" if second_unit else "starter", mood(p)) if c)
    return f'<div class="{cls}" style="left:{left:.1f}px;top:{top:.1f}px">{box_inner(p, jersey)}</div>'


def highlights_html(players, max_items=4):
    """Highlight lines, each about a different player. When space is short the
    least important go first (efficiency, then cold night, then bench spark)."""
    e = html.escape
    active = [p for p in players if played(p)]
    used, items = set(), {}

    def add(key, p, emoji, kicker, title, sub):
        used.add(p["player_id"])
        items[key] = (f'<div class="hl"><div class="em">{emoji}</div><div><div class="k">{e(kicker)}</div>'
                      f'<div class="t">{title}</div><div class="s">{e(sub)}</div></div></div>')

    def counting(p):
        parts = [f"{p[k]} {lab}" for k, lab in (("pts", "PTS"), ("reb", "REB"), ("ast", "AST"),
                                                 ("stl", "STL"), ("blk", "BLK")) if p[k]]
        return " · ".join(parts[:4])

    top = max(active, key=lambda p: p["fpts"])
    add("top", top, FIRE, "Top performer", f'{e(top["player_name"])} <em>{top["fpts"]:.1f}</em> FPTS',
        f'{counting(top)} in {top["min"]} min')

    shooters = [p for p in active if p["fga"] >= 6 and p["player_id"] not in used]
    if shooters:
        s = max(shooters, key=lambda p: (p["fgm"] / p["fga"], p["fgm"]))
        threes = f' · {s["tpm"]}-{s["tpa"]} 3PT' if s["tpm"] else ""
        add("eff", s, "🎯", "Efficiency", f'{e(s["player_name"])} <em>{s["fgm"]}-{s["fga"]}</em> FG',
            f'{s["pts"]} PTS{threes} in {s["min"]} min')

    bench = [p for p in active if not p["starter"] and p["player_id"] not in used]
    if bench:
        b = max(bench, key=lambda p: p["fpts"])
        add("bench", b, "⚡", "Bench spark", f'{e(b["player_name"])} <em>{b["fpts"]:.1f}</em> FPTS',
            f'{counting(b)} in {b["min"]} min')

    cold = [p for p in active if mood(p) == "cold" and p["player_id"] not in used]
    if cold:
        c = min(cold, key=lambda p: (p["fpts"] / p["min"], p["fgm"] - p["fga"]))
        add("cold", c, ICE, "Cold night", f'{e(c["player_name"])} <em>{c["fgm"]}-{c["fga"]}</em> FG',
            f'{c["pts"]} PTS, {c["fpts"]:.1f} FPTS in {c["min"]} min')

    keep = [k for k in ("top", "bench", "cold", "eff") if k in items][:max_items]
    shown = "".join(items[k] for k in ("top", "eff", "bench", "cold") if k in keep)
    legend = (f'<div class="legend"><span><i>{FIRE}</i> {HOT_FPTS:g}+ FPTS or {HOT_PER_MIN:g}+/min</span>'
              f'<span><i>{ICE}</i> cold night</span><span>USG = share of team FGA</span></div>')
    return f'<div class="highlights"><span class="label">Highlights</span>{shown}{legend}</div>'


def ranking_html(players):
    """Every player who played, by fantasy points (fills the panel when the bench is short)."""
    e = html.escape
    active = sorted((p for p in players if played(p)), key=lambda p: -p["fpts"])
    best = max(active[0]["fpts"], 1)
    rows = "".join(f'<div class="rk"><span class="n">{e(display_name(p))}</span>'
                   f'<div class="b"><i style="width:{max(p["fpts"], 0) / best * 100:.0f}%"></i></div>'
                   f'<b>{p["fpts"]:.1f}</b></div>' for p in active)
    return f'<div class="ranking"><span class="label">Fantasy points</span>{rows}</div>'


# Rough heights (CSS px) of the right panel's parts, to decide what fits.
SIDE_H, GAP = 616, 5
ROW_H, COMPACT_ROW_H = 63, 56
HL_BASE, HL_ITEM = 44, 46


def side_layout(rest, dnp_text):
    """(compact rows?, highlight items that fit, show the FPTS ranking?)"""
    compact = len(rest) >= 7
    used = 18 + GAP + len(rest) * ((COMPACT_ROW_H if compact else ROW_H) + GAP)
    if dnp_text:
        used += -(-len(dnp_text) // 44) * 10 + GAP
    items = max(1, min(4, (SIDE_H - used - HL_BASE) // HL_ITEM))
    return compact, items, len(rest) <= 2


def render_html(team, game, players, jersey_of, template):
    e = html.escape
    starters, second, rest, dnp = split_units(players)
    add_usage(players)
    lasts = [short_name(p["player_name"])[1] for p in players]
    for p, last in zip(players, lasts):
        p["dup_last"] = lasts.count(last) > 1

    tokens = [token_html(p, jersey_of.get(str(p["player_id"]), ""), spot, False)
              for p, spot in zip(order_on_court(starters), SPOTS)]
    tokens += [token_html(p, jersey_of.get(str(p["player_id"]), ""), spot, True)
               for p, spot in zip(order_on_court(second), SPOTS)]
    tokens.append(f'<div class="unit-label" style="top:{44 * PX_PER_FT - 7:.0f}px">Starters</div>')
    tokens.append(f'<div class="unit-label" style="top:{50 * PX_PER_FT - 7:.0f}px">Second unit</div>')

    bench = "".join(f'<div class="bench-row {mood(p)}">{box_inner(p, jersey_of.get(str(p["player_id"]), ""), True)}</div>'
                    for p in rest)
    dnp_text = ", ".join(display_name(p) for p in dnp)
    dnp_html = f'<div class="dnp"><b>DNP</b>{e(dnp_text)}</div>' if dnp else ""
    compact, n_items, ranking = side_layout(rest, dnp_text)
    if not rest:
        bench = '<div class="dnp">Everyone who played is on the court.</div>'

    home = game["home_team"] == team
    mine, theirs = (game["home_score"], game["away_score"]) if home else (game["away_score"], game["home_score"])
    opp = team_key(game["away_team"] if home else game["home_team"])
    score = (f'<span class="wl">{"W" if mine > theirs else "L"}</span>'
             f'{mine}–{theirs} {"vs" if home else "@"} {e(opp)}')
    kicker = "Preseason box score" if game["season_type"] == "preseason" else "Box score"
    return (template
            .replace("{{TEAM_VARS}}", team_vars(team))
            .replace("{{TEAM_NAME}}", e(TEAM_NAMES.get(team, team)))
            .replace("{{KICKER}}", kicker)
            .replace("{{SCORE}}", score)
            .replace("{{NOTE}}", f"{e(date_label(game['game_date']))} · Final")
            .replace("{{LOGO}}", logo_svg(team))
            .replace("{{COURT_TOKENS}}", "".join(tokens))
            .replace("{{SIDE_CLASS}}", " compact" if compact else "")
            .replace("{{BENCH}}", bench)
            .replace("{{DNP}}", dnp_html + (ranking_html(players) if ranking else ""))
            .replace("{{HIGHLIGHTS}}", highlights_html(players, n_items)))


def build_game(game_date, team, browser, template, jersey_of):
    game, rows = load_game(game_date, team)
    away, home = team_key(game["away_team"]), team_key(game["home_team"])
    folder = os.path.join(OUT_DIR, game_date.isoformat())
    os.makedirs(folder, exist_ok=True)
    outputs = []
    for side in (game["away_team"], game["home_team"]):
        code = team_key(side)
        players = [r for r in rows if r["team"] == side]
        png = os.path.join(folder, f"{away}-at-{home}_{code}.png")
        work_dir = make_dir(tempfile.gettempdir())
        try:
            render_png(browser, render_html(code, game, players, jersey_of, template), png, work_dir,
                       CHART_W, CHART_H)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
        starters, second, rest, dnp = split_units(players)
        print(f"{code}: {png}  ({len(starters)} starters, {len(second)} second unit, "
              f"{len(rest)} more bench, {len(dnp)} DNP)")
        outputs.append(png)
    return outputs


def main():
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_DIR, ".env"))
    args = sys.argv[1:]
    game_date = (dt.date.fromisoformat(args[0]) if args
                 else dt.datetime.now(ET).date() - dt.timedelta(days=1))
    teams = [a.upper() for a in args[1:]] or games_on(game_date)
    browser = find_browser()
    if not browser:
        raise SystemExit("No Chrome/Edge found -- can't render.")
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        template = f.read().replace("{{FONTS}}", font_css())
    jersey_of = {str(k): v for k, v in jerseys(game_date).items()}
    for team in teams:
        build_game(game_date, team, browser, template, jersey_of)


if __name__ == "__main__":
    main()
