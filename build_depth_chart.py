#!/usr/bin/env python3
"""
build_depth_chart.py

Draft 2026-27 depth chart for one team (PNG, 1080x1350), drawn on a court:
starters in the top half and the second unit in the bottom half (each in a
C/PF on the blocks, SF/SG on the wings, PG up top set), reserves on the bench
panel beside the court. 15 players in a 5-position x 3-unit layout.

  - players: the team's players in espn_players who are still on ESPN's current
    roster, by projected minutes per game (espn_player_lines, projected). Players
    without an ESPN projection fill the remaining spots; if that's still short of
    15, other rostered players (most experienced first) fill in, unprojected.
  - positions: every player gets one column. ESPN position eligibility is preferred;
    moving one spot over (e.g. a guard to SF) is allowed at a penalty. The chosen
    layout maximises minutes in the higher units, so the top-minute players start.
  - within a column, players are ordered by projected minutes.
  - depth_overrides.json can pin a position's starter by hand (e.g. BKN C).
  - fantasy points: Yahoo projected fantasy points per game (yahoo_projections),
    matched by ESPN id, else by name. Players Yahoo doesn't project show "—".
  - jersey numbers and injury tags come from ESPN's current team roster.
  - projected wins: projected_wins.json (rounded half up), shown under the bench.

Run espn_nba_projections.py and yahoo_projections.py first.

Usage:
  python build_depth_chart.py ATL
Output: "Depth Charts/<TEAM>.png"
"""

import html
import json
import os
import shutil
import sys
import tempfile
from decimal import ROUND_HALF_UP, Decimal

import requests
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from build_nightly_cards import headshot, name_key
from build_player_cards import (TEAM_NAMES, file_url, find_browser, font_css, make_dir, render_png,
                                team_vars)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(PROJECT_DIR, ".env"))

TEMPLATE_PATH = os.path.join(PROJECT_DIR, "depth_chart_template.html")
OUT_DIR = os.path.join(PROJECT_DIR, "Depth Charts")
LOGO_DIR = os.path.join(PROJECT_DIR, "logos")
WINS_PATH = os.path.join(PROJECT_DIR, "projected_wins.json")
OVERRIDES_PATH = os.path.join(PROJECT_DIR, "depth_overrides.json")  # {TEAM: {"starters": {"C": "name"}}}  # {"source_label", "wins": {TEAM: 44.3}}
LOGO_URL = "https://a.espncdn.com/i/teamlogos/nba/500/{team}.png"
ROSTER_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{team}/roster"

POSITIONS = ["PG", "SG", "SF", "PF", "C"]
UNITS = ["Starters", "Second Unit", "Reserves"]
UNIT_WEIGHTS = [3, 2, 1]        # how much a minute counts in each unit
OFF_POSITION_PENALTY = 12       # per step away from an eligible position
MAX_STEPS = 1                   # never move a player more than one position over
CHART_W, CHART_H = 540, 675     # CSS px; rendered at 2x

# Our team code -> ESPN's (for the roster URL)
ESPN_TEAM = {"GSW": "gs", "NYK": "ny", "NOP": "no", "SAS": "sa", "UTA": "utah", "WAS": "wsh"}
INJURY_LABELS = {"Day-To-Day": "DTD", "Out": "OUT", "Suspension": "SUSP"}
# ESPN roster position -> positions eligible on the chart
ROSTER_POSITIONS = {"PG": ["PG"], "SG": ["SG"], "SF": ["SF"], "PF": ["PF"], "C": ["C"],
                    "G": ["PG", "SG"], "F": ["SF", "PF"], "G-F": ["SG", "SF"], "F-G": ["SG", "SF"],
                    "F-C": ["PF", "C"], "C-F": ["PF", "C"]}

# -----------------------------------------------------------------------------
# DATA
# -----------------------------------------------------------------------------

def current_roster(team):
    """ESPN player id -> {"jersey", "injury"} for the team's roster today."""
    r = requests.get(ROSTER_URL.format(team=ESPN_TEAM.get(team, team.lower())), timeout=30)
    r.raise_for_status()
    roster = {}
    for a in r.json()["athletes"]:
        status = next((i.get("status") for i in a.get("injuries", []) if i.get("status")), None)
        roster[int(a["id"])] = {"jersey": a.get("jersey") or "",
                                "injury": INJURY_LABELS.get(status, status),
                                "name": a["displayName"],
                                "roster_pos": (a.get("position") or {}).get("abbreviation", ""),
                                "experience": (a.get("experience") or {}).get("years", 0)}
    return roster


def load_players(team, roster):
    engine = create_engine(os.environ["DATABASE_URL"])
    query = text("""
        SELECT p.player_id, p.full_name, p.positions, l.min AS mpg, l.fetched_at
        FROM espn_players p
        LEFT JOIN espn_player_lines l ON l.player_id = p.player_id AND l.line_type = 'projected'
        WHERE p.pro_team = :team
    """)
    with engine.connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(query, {"team": team})]
        yahoo = [dict(r._mapping) for r in conn.execute(text(
            "SELECT espn_player_id, full_name, fpts_per_game FROM yahoo_projections"))]
    yahoo_by_id = {y["espn_player_id"]: y["fpts_per_game"] for y in yahoo if y["espn_player_id"]}
    yahoo_by_name = {name_key(y["full_name"]): y["fpts_per_game"] for y in yahoo}
    players = []
    for p in rows:
        if p["player_id"] not in roster:
            continue  # no longer on the team
        p.update(roster[p["player_id"]])
        p["fpts"] = yahoo_by_id.get(p["player_id"]) or yahoo_by_name.get(name_key(p["full_name"]))
        players.append(p)
    players.sort(key=lambda p: -(p["mpg"] or 0))
    if len(players) < 15:
        # Not in ESPN's fantasy data: fill from the roster, most experienced first.
        have = {p["player_id"] for p in players}
        extras = sorted((pid for pid in roster if pid not in have),
                        key=lambda pid: -(roster[pid]["experience"] or 0))
        for pid in extras[:15 - len(players)]:
            r = roster[pid]
            players.append({"player_id": pid, "full_name": r["name"], "mpg": None, "fpts": None,
                            "fetched_at": None, "positions": ROSTER_POSITIONS.get(r["roster_pos"], ["SF"]),
                            **r})
    return players[:15]

# -----------------------------------------------------------------------------
# DEPTH CHART
# -----------------------------------------------------------------------------

def steps_away(p, pos):
    """0 if the player is eligible at pos, else the distance to his nearest eligible position."""
    eligible = [POSITIONS.index(x) for x in (p["positions"] or []) if x in POSITIONS] or [2]
    return min(abs(POSITIONS.index(pos) - e) for e in eligible)


def load_overrides(team):
    """{position: player name} starters pinned by hand in depth_overrides.json."""
    if not os.path.exists(OVERRIDES_PATH):
        return {}
    with open(OVERRIDES_PATH, encoding="utf-8") as f:
        return json.load(f).get(team, {}).get("starters", {})


def arrange(players, starters=None):
    """Assign every player one position (3 per position), maximising
    sum(unit weight x minutes) - off-position penalties. Exhaustive search with
    pruning; small enough for a 15-man roster.
    starters: {position: player name} pinned as that position's starter."""
    per_pos = len(players) // len(POSITIONS)
    columns = {pos: [] for pos in POSITIONS}
    for pos, name in (starters or {}).items():
        pinned = next((p for p in players if name_key(p["full_name"]) == name_key(name)), None)
        if pinned is None:
            raise SystemExit(f"Override: {name} is not in the 15-man group -- check depth_overrides.json")
        columns[pos].append(pinned)
    pinned_ids = {c[0]["player_id"] for c in columns.values() if c}
    players = [p for p in players if p["player_id"] not in pinned_ids]
    options = [[pos for pos in POSITIONS if steps_away(p, pos) <= MAX_STEPS] for p in players]
    best = {"score": float("-inf"), "columns": None}
    # Players are sorted by minutes, so within a column the order players are added
    # is their depth order: the k-th player placed in a column lands in unit k.
    # (Pinned starters are already in place, so everyone else lands behind them.)

    def bound(i):
        # Optimistic: every remaining player in the best open unit, no penalty.
        open_unit = min(len(c) for c in columns.values())
        return sum(UNIT_WEIGHTS[min(open_unit, 2)] * (p["mpg"] or 0) for p in players[i:])

    def search(i, score):
        if score + bound(i) <= best["score"]:
            return
        if i == len(players):
            best["score"], best["columns"] = score, {k: list(v) for k, v in columns.items()}
            return
        p = players[i]
        for pos in options[i]:
            col = columns[pos]
            if len(col) >= per_pos:
                continue
            gain = UNIT_WEIGHTS[len(col)] * (p["mpg"] or 0) - OFF_POSITION_PENALTY * steps_away(p, pos)
            col.append(p)
            search(i + 1, score + gain)
            col.pop()

    search(0, 0.0)
    if best["columns"] is None:
        raise SystemExit("Could not fit the roster into 5 positions x 3 -- check positions.")
    return best["columns"]

# -----------------------------------------------------------------------------
# RENDER
# -----------------------------------------------------------------------------

# Where each position stands on the court, in feet from the top-left corner
# (court is 50 x 94 ft). Starters attack the top basket; the second unit mirrors
# them in the bottom half.
SPOTS = {"C": (12.5, 9), "PF": (37.5, 10), "SF": (10, 22.5), "SG": (40, 23.5), "PG": (25, 35.5)}
PX_PER_FT = 5.92
TOKEN_W, TOKEN_H = 124, 46
COURT_W = 296


def team_logo(team):
    """Local copy of the team's logo (ESPN), downloaded on first use."""
    path = os.path.join(LOGO_DIR, f"{team}.png")
    if not os.path.exists(path):
        os.makedirs(LOGO_DIR, exist_ok=True)
        r = requests.get(LOGO_URL.format(team=ESPN_TEAM.get(team, team.lower())), timeout=30)
        if r.status_code != 200 or not r.content:
            return None
        with open(path, "wb") as f:
            f.write(r.content)
    return path


def short_name(full_name):
    first, _, last = full_name.partition(" ")
    return first, last or first


def off_position(p, pos):
    """'PF→C' when the player is playing out of position, else ''."""
    if not steps_away(p, pos):
        return ""
    natural = min((x for x in p["positions"] or [] if x in POSITIONS),
                  key=lambda x: abs(POSITIONS.index(x) - POSITIONS.index(pos)), default="?")
    return f"{natural}→{pos}"


def face_html(p):
    e = html.escape
    first, last = short_name(p["full_name"])
    photo_path = headshot(str(p["player_id"]))
    inner = (f'<img src="{e(file_url(photo_path))}" alt="">' if photo_path
             else f'<span class="initials">{e(first[0] + last[0])}</span>')
    return f'<div class="face">{inner}</div>'


def fmt(v):
    return "—" if v is None else f"{v:.1f}"


def player_info(p, pos):
    e = html.escape
    _, last = short_name(p["full_name"])
    if p.get("dup_last"):
        last = p["full_name"]  # e.g. three "Williams" on one team: show full names
    label = off_position(p, pos) or pos
    jersey = f" · #{e(p['jersey'])}" if p["jersey"] else ""
    inj = f'<span class="inj">{e(p["injury"])}</span>' if p["injury"] else ""
    return (f'<div class="pos">{e(label)}{jersey}{inj}</div>'
            f'<div class="name{name_size(last)}">{e(last)}</div>'
            f'<div class="stats"><span><b>{fmt(p["mpg"])}</b>MIN</span>'
            f'<span><b class="fp">{fmt(p["fpts"])}</b>FPTS</span></div>')


def name_size(last):
    return " xlong" if len(last) > 13 else " long" if len(last) > 10 else ""


def token_html(p, pos, unit):
    x, y = SPOTS[pos]
    if unit == 1:
        y = 94 - y
    left = min(max(x * PX_PER_FT - TOKEN_W / 2, 4), COURT_W - TOKEN_W - 4)
    top = y * PX_PER_FT - TOKEN_H / 2
    return (f'<div class="token{" starter" if unit == 0 else ""}" style="left:{left:.1f}px;top:{top:.1f}px">'
            f'{face_html(p)}<div class="info">{player_info(p, pos)}</div></div>')


def bench_html(p, pos):
    e = html.escape
    first, _ = short_name(p["full_name"])
    return (f'<div class="bench-row">{face_html(p)}<div class="info">'
            f'{player_info(p, pos).replace('<div class="name', f'<div class="first">{e(first)}</div><div class="name', 1)}'
            f'</div></div>')


def logo_svg(team):
    """Team logo in the centre circle (court feet), or the team code if no logo."""
    path = team_logo(team)
    if path:
        return (f'<image href="{html.escape(file_url(path))}" x="19.9" y="41.9" width="10.2" height="10.2" '
                f'preserveAspectRatio="xMidYMid meet"/>')
    return (f'<text x="25" y="48.6" text-anchor="middle" font-size="4.4" font-weight="800" '
            f'fill="#222">{html.escape(team)}</text>')


def render_html(team, columns, template):
    e = html.escape
    tokens = [token_html(columns[pos][u], pos, u) for u in (0, 1) for pos in POSITIONS]
    tokens.append(f'<div class="unit-label" style="top:{44 * PX_PER_FT - 7:.0f}px">Starters</div>')
    tokens.append(f'<div class="unit-label" style="top:{50 * PX_PER_FT - 7:.0f}px">Second unit</div>')
    bench = "".join(bench_html(columns[pos][2], pos) for pos in POSITIONS)
    return (template
            .replace("{{TEAM_VARS}}", team_vars(team))
            .replace("{{TEAM_NAME}}", e(TEAM_NAMES.get(team, team)))
            .replace("{{LOGO}}", logo_svg(team))
            .replace("{{COURT_TOKENS}}", "".join(tokens))
            .replace("{{BENCH}}", bench)
            .replace("{{WINS}}", wins_html(team)))


def round_half_up(x):
    """Normal rounding (44.5 -> 45), not Python's round-half-to-even."""
    return int(Decimal(str(x)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def load_wins():
    if not os.path.exists(WINS_PATH):
        return {}
    with open(WINS_PATH, encoding="utf-8") as f:
        return json.load(f)


WINS = load_wins()


def wins_html(team):
    """Projected wins block for the bench panel; empty if we have no projection."""
    e = html.escape
    raw = WINS.get("wins", {}).get(team)
    if raw is None:
        return ""
    wins = round_half_up(raw)
    return (f'<div class="wins"><span class="label">Projected wins</span><b>{wins}</b>'
            f'<span class="rec">{wins}–{82 - wins} record</span>'
            f'<span class="src">{e(WINS.get("source_label", ""))} {raw:g}</span></div>')


def main():
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    team = sys.argv[1].upper()
    browser = find_browser()
    if not browser:
        raise SystemExit("No Chrome/Edge found -- can't render PNGs.")

    roster = current_roster(team)
    players = load_players(team, roster)
    if len(players) < 15:
        raise SystemExit(f"Only {len(players)} {team} players with ESPN data -- need 15.")
    columns = arrange(players, load_overrides(team))
    lasts = [short_name(p["full_name"])[1] for p in players]
    for p, last in zip(players, lasts):
        p["dup_last"] = lasts.count(last) > 1

    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        template = f.read().replace("{{FONTS}}", font_css())
    os.makedirs(OUT_DIR, exist_ok=True)
    png = os.path.join(OUT_DIR, f"{team}.png")
    work_dir = make_dir(tempfile.gettempdir())
    try:
        render_png(browser, render_html(team, columns, template), png, work_dir, CHART_W, CHART_H)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    print(f"{TEAM_NAMES.get(team, team)} depth chart -> {png}")
    for u, unit in enumerate(UNITS):
        line = "  ".join(f"{pos} {columns[pos][u]['full_name']} ({columns[pos][u]['mpg'] or '-'})"
                         for pos in POSITIONS)
        print(f"  {unit:12} {line}")


if __name__ == "__main__":
    main()
