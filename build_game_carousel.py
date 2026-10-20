#!/usr/bin/env python3
"""
build_game_carousel.py

Instagram carousel per game (3 slides, 1080x1350):
  1. cover: final score, quarter-by-quarter line score and a simple head-to-head
     list of basic team stats (game_cover_template.html)
  2. home team traditional box score table with USG%, nba.com layout (build_boxscore_table.py)
  3. away team table
Queued for the n8n carousel workflow in content/carousels/<queue>/ as
<date>_box_<AWAY>-at-<HOME>.json + <slug>_01..03_*.png (with the caption).

Data: nba_games / nba_boxscores plus the raw ESPN summary JSON in
"Box Scores/<date>/" (line score + team stats) -- run collect_boxscores.py first.

Usage:
  python build_game_carousel.py                  # every game from yesterday -> pending
  python build_game_carousel.py 2026-10-07 OKC   # one game
  python build_game_carousel.py 2026-10-07 --approve
"""

import argparse
import datetime as dt
import html
import json
import os
import shutil
import tempfile

from build_boxscore_chart import OUT_DIR, games_on, load_game, mood, played
from build_boxscore_table import TEMPLATE_PATH as TABLE_TEMPLATE, build_tables
from build_depth_chart import team_logo
from build_nightly_cards import ET, RAW_DIR, date_label, team_key
from build_player_cards import (DEFAULT_COLORS, TEAM_COLORS, TEAM_NAMES, contrast, file_url, find_browser,
                                font_css, make_dir, render_png)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
COVER_TEMPLATE = os.path.join(PROJECT_DIR, "game_cover_template.html")
QUEUE_DIR = os.path.join(PROJECT_DIR, "content", "carousels")
PANEL = "#16181d"

# Cover team stats, top to bottom: (made-attempted stat or None, value stat, label, lower is better)
TEAM_STATS = [
    ("fieldGoalsMade-fieldGoalsAttempted", "fieldGoalPct", "Field goals", False),
    ("threePointFieldGoalsMade-threePointFieldGoalsAttempted", "threePointFieldGoalPct", "3-pointers", False),
    ("freeThrowsMade-freeThrowsAttempted", "freeThrowPct", "Free throws", False),
    (None, "totalRebounds", "Rebounds", False),
    (None, "assists", "Assists", False),
    (None, "steals", "Steals", False),
    (None, "blocks", "Blocks", False),
    (None, "totalTurnovers", "Turnovers", True),
]

# -----------------------------------------------------------------------------
# DATA
# -----------------------------------------------------------------------------

def load_summary(game):
    with open(os.path.join(RAW_DIR, game["game_date"].isoformat(), f"{game['game_id']}.json"),
              encoding="utf-8") as f:
        summary = json.load(f)
    comp = summary["header"]["competitions"][0]
    sides = {}
    for c in comp["competitors"]:
        record = next((r["displayValue"] for r in c.get("record", []) if r.get("type") == "total"), "")
        sides[c["homeAway"]] = {"team": team_key(c["team"]["abbreviation"]), "score": int(c["score"]),
                                "quarters": [int(q["displayValue"]) for q in c.get("linescores", [])],
                                "record": record}
    venue = summary.get("gameInfo", {}).get("venue", {})
    if comp.get("neutralSite"):
        city = venue.get("address", {}).get("city")
        sides["home"]["venue"] = ", ".join(x for x in (venue.get("fullName"), city) if x)
    for t in summary["boxscore"]["teams"]:
        sides[t["homeAway"]]["stats"] = {s["name"]: s["displayValue"] for s in t["statistics"]}
    return sides["away"], sides["home"]


def bar_colour(team):
    dark, accent = TEAM_COLORS.get(team, DEFAULT_COLORS)
    return accent if contrast(accent, PANEL) >= 2.5 else "#ffffff"


def too_close(a, b):
    """Two hex colours that would be hard to tell apart side by side."""
    return sum((int(a[i:i + 2], 16) - int(b[i:i + 2], 16)) ** 2 for i in (1, 3, 5)) ** .5 < 110


def side_vars(away, home):
    """Team colours for both sides; when the accents clash the home side goes white."""
    a_bar, h_bar = bar_colour(away), bar_colour(home)
    if too_close(a_bar, h_bar):
        h_bar = "#ffffff" if not too_close(a_bar, "#ffffff") else "#9aa3ad"
    return (f"--away: {TEAM_COLORS.get(away, DEFAULT_COLORS)[0]}; --away-accent: {a_bar}; "
            f"--home: {TEAM_COLORS.get(home, DEFAULT_COLORS)[0]}; --home-accent: {h_bar};")

# -----------------------------------------------------------------------------
# RENDER
# -----------------------------------------------------------------------------

def board_html(away, home):
    e = html.escape

    def side(s, other):
        logo = team_logo(s["team"])
        img = f'<img src="{e(file_url(logo))}" alt="">' if logo else ""
        won = s["score"] > other["score"]
        rec = f'<span class="rec">Preseason {e(s["record"])}</span>' if s["record"] else ""
        return (f'<div class="side {"won" if won else "lost"}">{img}'
                f'<span class="nm">{e(TEAM_NAMES.get(s["team"], s["team"]))}</span>{rec}'
                f'<span class="pts">{s["score"]}</span><span class="res">{"Win" if won else "Loss"}</span></div>')

    ot = len(away["quarters"]) - 4
    final = "Final" + (f"/{'' if ot == 1 else ot}OT" if ot > 0 else "")
    return (f'<div class="board">{side(away, home)}<div class="mid"><span class="final">{final}</span>'
            f'<span class="at">AWAY · HOME</span></div>{side(home, away)}</div>')


def lines_html(away, home):
    e = html.escape
    n = len(away["quarters"])
    heads = [f"Q{i + 1}" if i < 4 else ("OT" if i == 4 else f"{i - 3}OT") for i in range(n)]

    def row(s, other):
        cells = "".join(f'<td{" class=hi" if q > o else ""}>{q}</td>'
                        for q, o in zip(s["quarters"], other["quarters"]))
        return (f'<tr class="{"won" if s["score"] > other["score"] else "lost"}"><td>{e(s["team"])}</td>'
                f'{cells}<td class="t">{s["score"]}</td></tr>')

    return (f'<div class="lines"><table><tr><th></th>{"".join(f"<th>{h}</th>" for h in heads)}<th>T</th></tr>'
            f'{row(away, home)}{row(home, away)}</table></div>')


def stats_html(away, home):
    e = html.escape
    rows = []
    for split, name, label, low in TEAM_STATS:
        a, h = away["stats"].get(name), home["stats"].get(name)
        if a is None or h is None:
            continue
        av, hv = float(a), float(h)
        a_lead, h_lead = (av < hv, hv < av) if low else (av > hv, hv > av)

        def val(side, v, lead, s):
            sub = f'<small>{e(s["stats"][split])}</small>' if split else ""
            text = f"{v:.1f}<em>%</em>" if split else e(s["stats"][name])
            return f'<span class="v {side}{" lead" if lead else ""}"><b>{text}</b>{sub}</span>'

        rows.append(f'<div class="row">{val("a", av, a_lead, away)}<span class="lab">{e(label)}</span>'
                    f'{val("h", hv, h_lead, home)}</div>')
    head = (f'<div class="row th"><span class="v a"><b>{e(away["team"])}</b></span><span class="lab">Team stats</span>'
            f'<span class="v h"><b>{e(home["team"])}</b></span></div>')
    return f'<div class="stats">{head}{"".join(rows)}</div>'


def cover_html(game, rows, away, home, template):
    kicker = "Preseason" if game["season_type"] == "preseason" else "NBA"
    return (template
            .replace("{{TITLE}}", html.escape(f"{away['team']} at {home['team']}"))
            .replace("{{VARS}}", side_vars(away["team"], home["team"]))
            .replace("{{KICKER}}", kicker)
            .replace("{{NOTE}}", html.escape(" · ".join(x for x in (date_label(game["game_date"]),
                                                                    home.get("venue")) if x)))
            .replace("{{BOARD}}", board_html(away, home))
            .replace("{{LINES}}", lines_html(away, home))
            .replace("{{STATS}}", stats_html(away, home)))


def caption(game, rows, away, home):
    win, lose = (away, home) if away["score"] > home["score"] else (home, away)
    season = "PRESEASON" if game["season_type"] == "preseason" else "NBA"
    lines = [f"🏀 FINAL · {TEAM_NAMES.get(win['team'], win['team'])} {win['score']}, "
             f"{TEAM_NAMES.get(lose['team'], lose['team'])} {lose['score']}",
             f"{season} · {date_label(game['game_date'])} · {away['team']} @ {home['team']}"
             + (f" (at {home['venue']})" if home.get("venue") else ""), ""]
    lead = int(lose["stats"].get("largestLead", 0) or 0)
    if lead >= 15:
        lines += [f"{lose['team']} led by as many as {lead} and still lost.", ""]
    lines.append("Fantasy leaders (Yahoo default points):")
    top = sorted((p for p in rows if played(p)), key=lambda p: -p["fpts"])[:5]
    for p in top:
        icon = " 🔥" if mood(p) == "hot" else ""
        lines.append(f"• {p['player_name']} ({team_key(p['team'])}) – {p['fpts']:.1f} FPTS · {p['pts']} PTS, "
                     f"{p['reb']} REB, {p['ast']} AST, {p['stl']} STL, {p['blk']} BLK{icon}")
    lines += ["", "Swipe ➡️ for both teams' full box scores.", "",
              f"#NBA #FantasyBasketball #NBAFantasy #FantasyNBA #BoxScore #{away['team']} #{home['team']}"
              + (" #NBAPreseason" if season == "PRESEASON" else "")]
    return "\n".join(lines)[:2200]


def build(game_date, team, browser, cover_t, table_t, queue):
    game, rows = load_game(game_date, team)
    away, home = load_summary(game)
    away["raw"], home["raw"] = game["away_team"], game["home_team"]
    a, h = away["team"], home["team"]
    folder = os.path.join(OUT_DIR, game_date.isoformat())
    os.makedirs(folder, exist_ok=True)

    cover = os.path.join(folder, f"{a}-at-{h}_cover.png")
    work_dir = make_dir(tempfile.gettempdir())
    try:
        render_png(browser, cover_html(game, rows, away, home, cover_t), cover, work_dir, 540, 675)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    build_tables(game_date, team, browser, table_t)  # -> <a>-at-<h>_<TEAM>_table.png

    slug = f"{game_date.isoformat()}_box_{a}-at-{h}"
    out = os.path.join(QUEUE_DIR, queue)
    images = []
    slides = [(cover, "cover"), (os.path.join(folder, f"{a}-at-{h}_{h}_table.png"), h),
              (os.path.join(folder, f"{a}-at-{h}_{a}_table.png"), a)]
    for i, (src, label) in enumerate(slides, 1):
        name = f"{slug}_{i:02d}_{label}.png"
        shutil.copyfile(src, os.path.join(out, name))
        images.append(name)
    with open(os.path.join(out, f"{slug}.json"), "w", encoding="utf-8") as f:
        json.dump({"slug": slug, "series": "game_box_score", "game_id": game["game_id"],
                   "game_date": game_date.isoformat(), "images": images,
                   "caption": caption(game, rows, away, home)}, f, ensure_ascii=False, indent=2)
    print(f"Queued {slug} ({queue}): cover, {h} (home) table, {a} (away) table")


def main():
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_DIR, ".env"))
    ap = argparse.ArgumentParser(description="Box score carousel per game.")
    ap.add_argument("date", nargs="?", help="game date (US Eastern), default yesterday")
    ap.add_argument("teams", nargs="*", help="one team per game to build (default: every game)")
    ap.add_argument("--approve", action="store_true", help="queue in approved instead of pending")
    args = ap.parse_args()
    game_date = (dt.date.fromisoformat(args.date) if args.date
                 else dt.datetime.now(ET).date() - dt.timedelta(days=1))
    browser = find_browser()
    if not browser:
        raise SystemExit("No Chrome/Edge found -- can't render.")
    fonts = font_css()
    with open(COVER_TEMPLATE, encoding="utf-8") as f:
        cover_t = f.read().replace("{{FONTS}}", fonts)
    with open(TABLE_TEMPLATE, encoding="utf-8") as f:
        table_t = f.read().replace("{{FONTS}}", fonts)
    queue = "approved" if args.approve else "pending"
    for folder in ("pending", "approved", "posted"):
        os.makedirs(os.path.join(QUEUE_DIR, folder), exist_ok=True)
    for team in [t.upper() for t in args.teams] or games_on(game_date):
        build(game_date, team, browser, cover_t, table_t, queue)


if __name__ == "__main__":
    main()
