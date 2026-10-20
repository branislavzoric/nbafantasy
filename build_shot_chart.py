#!/usr/bin/env python3
"""
build_shot_chart.py

Shot chart for one player in one game (PNG, 1080x1350): every field-goal attempt
on a half court (made = green dot, missed = red X), zone splits (paint, mid-range,
3PT, FT) and the fantasy line.

Shots come from the play-by-play in the raw ESPN summary JSON
("Box Scores/<date>/<game_id>.json"): plays with shootingPlay, the shooter as the
first participant, and ESPN's court coordinates (x = 0-50 ft across, y = feet
from the basket). Free throws have no location and are only counted.
Run collect_boxscores.py first.

Usage:
  python build_shot_chart.py 2026-10-07 "Tyrese Haliburton"
Output: "Shot Charts/<date>/<player-slug>.png"
"""

import datetime as dt
import html
import json
import os
import shutil
import sys
import tempfile

from sqlalchemy import create_engine, text

from build_nightly_cards import RAW_DIR, date_label, headshot, name_key, team_key
from build_player_cards import TEAM_NAMES, find_browser, file_url, font_css, make_dir, render_png, team_vars

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(PROJECT_DIR, "shot_chart_template.html")
OUT_DIR = os.path.join(PROJECT_DIR, "Shot Charts")
HOOP_Y = 5.25          # basket centre, feet from the baseline
HALF_COURT = 47.0

# -----------------------------------------------------------------------------
# DATA
# -----------------------------------------------------------------------------

def load_line(game_date, player):
    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(text("""
            SELECT b.*, g.home_team, g.away_team, g.home_score, g.away_score, g.season_type
            FROM nba_boxscores b JOIN nba_games g USING (game_id)
            WHERE b.game_date = :d"""), {"d": game_date}).mappings()]
    match = [r for r in rows if r["player_id"] == player or name_key(r["player_name"]) == name_key(player)]
    if not match:
        raise SystemExit(f"No box score for {player} on {game_date} -- run collect_boxscores.py first.")
    return match[0]


def load_shots(line):
    path = os.path.join(RAW_DIR, line["game_date"].isoformat(), f"{line['game_id']}.json")
    with open(path, encoding="utf-8") as f:
        plays = json.load(f).get("plays", [])
    shots = []
    for p in plays:
        parts = p.get("participants") or []
        if not p.get("shootingPlay") or not parts or parts[0]["athlete"]["id"] != str(line["player_id"]):
            continue
        if "free throw" in p["type"]["text"].lower():
            continue
        c = p.get("coordinate") or {}
        x, y = c.get("x"), c.get("y")
        if x is None or abs(x) > 1000:
            continue
        shots.append({"x": x, "y": y, "made": bool(p.get("scoringPlay")),
                      "three": p.get("pointsAttempted") == 3 or "three point" in p["text"].lower(),
                      "text": p["text"], "period": p["period"]["number"], "clock": p["clock"]["displayValue"],
                      "heave": "heave" in p["type"]["text"].lower()})
    if len(shots) > (line["fga"] or 0):
        # End-of-quarter heaves can be in the play-by-play but not the box score
        shots = [s for s in shots if not s["heave"]]
    return shots


def zone(s):
    if s["three"]:
        return "3PT"
    if abs(s["x"] - 25) <= 8 and s["y"] + HOOP_Y <= 19:
        return "Paint"
    return "Mid-range"

# -----------------------------------------------------------------------------
# RENDER
# -----------------------------------------------------------------------------

def shots_svg(shots):
    out = []
    # Misses first so makes sit on top
    for s in sorted(shots, key=lambda s: s["made"]):
        x = min(max(s["x"], 1), 49)
        y = min(s["y"] + HOOP_Y, HALF_COURT - 1)  # heaves from past half court sit on the line
        if s["made"]:
            out.append(f'<circle class="make" cx="{x}" cy="{y:.2f}" r=".95"/>')
        else:
            d = .7
            seg = f'M{x - d} {y - d:.2f} L{x + d} {y + d:.2f} M{x + d} {y - d:.2f} L{x - d} {y + d:.2f}'
            out.append(f'<path class="miss-bg" d="{seg}"/><path class="miss" d="{seg}"/>')
    return "".join(out)


def pct(m, a):
    return f"{100 * m / a:.0f}%" if a else "—"


def zones_html(shots, line):
    cells = []
    for name in ("Paint", "Mid-range", "3PT"):
        z = [s for s in shots if zone(s) == name]
        m = sum(s["made"] for s in z)
        cells.append((name, m, len(z)))
    cells.append(("Free throws", line["ftm"], line["fta"]))
    return "".join(f'<div class="zone{" none" if not a else ""}"><span class="k">{html.escape(k)}</span>'
                   f'<b>{m}-{a}</b><span>{pct(m, a)}</span></div>' for k, m, a in cells)


def render_html(line, shots, template):
    e = html.escape
    team = team_key(line["team"])
    home = line["team"] == line["home_team"]
    opp = team_key(line["away_team"] if home else line["home_team"])
    mine, theirs = ((line["home_score"], line["away_score"]) if home else (line["away_score"], line["home_score"]))
    first, _, last = line["player_name"].partition(" ")
    path = headshot(str(line["player_id"]))
    face = (f'<img src="{e(file_url(path))}" alt="">' if path
            else f'<span class="ini">{e(first[:1] + last[:1])}</span>')
    fgm, fga = sum(s["made"] for s in shots), len(shots)
    who = (f'<div class="who"><div class="face">{face}</div><div><div class="first">{e(first)}</div>'
           f'<div class="last">{e(last or first)}</div>'
           f'<div class="meta">{e(TEAM_NAMES.get(team, team))} · {"W" if mine > theirs else "L"} {mine}–{theirs} '
           f'{"vs" if home else "@"} {e(opp)} · {line["min"]} MIN</div></div>'
           f'<div class="fg"><b>{fgm}-{fga}</b><span>FG · {pct(fgm, fga)}</span></div></div>')
    stat_line = "".join(f'<span><b>{v}</b> {k}</span>' for k, v in (
        ("PTS", line["pts"]), ("REB", line["reb"]), ("AST", line["ast"]), ("STL", line["stl"]),
        ("BLK", line["blk"]), ("TO", line["tov"]), ("FPTS", f'{line["fpts"]:.1f}')))
    kicker = "Preseason" if line["season_type"] == "preseason" else "NBA"
    return (template
            .replace("{{TITLE}}", e(f"{line['player_name']} shot chart"))
            .replace("{{TEAM_VARS}}", team_vars(team))
            .replace("{{KICKER}}", kicker)
            .replace("{{NOTE}}", e(date_label(line["game_date"])))
            .replace("{{WHO}}", who)
            .replace("{{SHOTS}}", shots_svg(shots))
            .replace("{{ZONES}}", zones_html(shots, line))
            .replace("{{LINE}}", stat_line))


def main():
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_DIR, ".env"))
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    game_date, player = dt.date.fromisoformat(sys.argv[1]), sys.argv[2]
    line = load_line(game_date, player)
    shots = load_shots(line)
    fgm = sum(s["made"] for s in shots)
    if (fgm, len(shots)) != (line["fgm"], line["fga"]):
        print(f"Warning: play-by-play has {fgm}-{len(shots)} FG, box score says {line['fgm']}-{line['fga']}")
    browser = find_browser()
    if not browser:
        raise SystemExit("No Chrome/Edge found -- can't render.")
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        template = f.read().replace("{{FONTS}}", font_css())
    folder = os.path.join(OUT_DIR, game_date.isoformat())
    os.makedirs(folder, exist_ok=True)
    png = os.path.join(folder, f"{name_key(line['player_name'])}.png")
    work_dir = make_dir(tempfile.gettempdir())
    try:
        render_png(browser, render_html(line, shots, template), png, work_dir, 540, 675)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    print(f"{line['player_name']}: {fgm}-{len(shots)} FG -> {png}")
    for s in shots:
        print(f"  Q{s['period']} {s['clock']:>5}  ({s['x']:>2},{s['y']:>2})  {zone(s):9} "
              f"{'MADE' if s['made'] else 'miss'}  {s['text']}")


if __name__ == "__main__":
    main()
