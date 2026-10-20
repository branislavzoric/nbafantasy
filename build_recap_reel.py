#!/usr/bin/env python3
"""
build_recap_reel.py

"Last night's top 10" recap reel (1080x1920 MP4), counting down #10 -> #1 by
Yahoo default fantasy points (nba_boxscores.fpts):
  - top half: the player's card (headline, stat line, result) with his rank where
    the photo would be
  - bottom half: a big photo of the player on his team's colours
  - intro (title + the night's games) and outro (the full top 10, #1's photo)

Headlines come from "Nightly Text/<date>.json" (rule-based if missing), like the
nightly cards. Run collect_boxscores.py first.

Usage:
  python build_recap_reel.py                       # yesterday (US Eastern), silent
  python build_recap_reel.py 2026-10-06 --track Volatile_Reaction   # music + credit from Reel Audio/tracks.json
  python build_recap_reel.py 2026-10-06 --audio "Reel Audio/x.mp3" --credit "..."
Output: "Reels/recaps/<date>_top10.mp4" (+ preview), queued in content/reels/pending
"""

import argparse
import datetime as dt
import html
import json
import os
import shutil
import subprocess
import tempfile

from PIL import Image
from sqlalchemy import create_engine, text

from build_nightly_cards import (ET, TEAM_NAMES, date_label, headshot, jerseys, load_text, load_top,
                                 team_key)
from build_player_cards import file_url, find_browser, font_css, make_dir, render_png, team_vars
from build_reel import render_top_html
from build_team_reel import FFMPEG, HALF_H, HALF_W, encode, frames

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CARD_TEMPLATE = os.path.join(PROJECT_DIR, "reel_top_template.html")
SLIDE_TEMPLATE = os.path.join(PROJECT_DIR, "recap_slides.html")
OUT_DIR = os.path.join(PROJECT_DIR, "Reels", "recaps")
QUEUE_DIR = os.path.join(PROJECT_DIR, "content", "reels")
TRACKS_PATH = os.path.join(PROJECT_DIR, "Reel Audio", "tracks.json")

INTRO_SECONDS, PLAYER_SECONDS, OUTRO_SECONDS = 2.5, 3.5, 3.5


def games_on(game_date):
    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(text(
            "SELECT * FROM nba_games WHERE game_date = :d AND status = 'STATUS_FINAL' ORDER BY start_time"),
            {"d": game_date})]


def rank_html(rank):
    return f'<div class="rankbig"><small>#</small>{rank}</div>'


def photo_body(p, jersey):
    e = html.escape
    first, _, last = p["player_name"].partition(" ")
    path = headshot(p["player_id"])
    photo = (f'<img src="{e(file_url(path))}" alt="">' if path
             else f'<div class="nophoto">{e(first[:1] + last[:1])}</div>')
    team = team_key(p["team"])
    return (f'<div class="slide photo-slide"><div class="jersey">{e(jersey)}</div>'
            f'<div class="rank"><small>#</small>{p["rank"]}</div>{photo}'
            f'<div class="plate"><div><div class="first">{e(first)}</div>'
            f'<div class="last{" long" if len(last) > 12 else ""}">{e(last or first)}</div></div>'
            f'<div class="team"><b>{p["fpts"]:.1f}</b>FPTS · {e(TEAM_NAMES.get(team, team))}</div></div></div>')


def intro_top_body(game_date, games):
    e = html.escape
    chips = "".join(f'<span>{e(team_key(g["away_team"]))} {g["away_score"]} @ '
                    f'{e(team_key(g["home_team"]))} {g["home_score"]}</span>' for g in games)
    season = "Preseason" if games and games[0]["season_type"] == "preseason" else "NBA"
    return (f'<div class="slide title-slide"><div class="kicker">{season} · {e(date_label(game_date))}</div>'
            f'<h1>Last night\'s<br><span>top 10</span></h1>'
            f'<div class="sub">Ranked by fantasy points · Yahoo default scoring</div>'
            f'<div class="games">{chips}</div></div>')


def intro_bottom_body():
    return ('<div class="slide title-slide"><div class="big-count">10</div>'
            '<div class="sub">counting down to #1</div></div>')


def list_body(top, game_date):
    e = html.escape
    rows = "".join(f'<div class="row{" first" if p["rank"] == 1 else ""}"><b>#{p["rank"]}</b>'
                   f'<span class="nm">{e(p["player_name"])}</span>'
                   f'<span class="tm">{e(team_key(p["team"]))}</span>'
                   f'<span class="fp">{p["fpts"]:.1f}</span></div>' for p in top)
    return (f'<div class="slide list-slide"><div class="kicker">{e(date_label(game_date))} · fantasy points</div>'
            f'<h2>Last night\'s top 10</h2>{rows}</div>')


def caption(top, game_date, games, credit=None):
    season = "PRESEASON" if games and games[0]["season_type"] == "preseason" else "NBA"
    lines = [f"🏀 LAST NIGHT'S TOP 10 · {season}, {date_label(game_date)}",
             "Ranked by fantasy points (Yahoo default scoring)", ""]
    for p in sorted(top, key=lambda p: -p["rank"]):
        lines.append(f"#{p['rank']} {p['player_name']} ({team_key(p['team'])}) – {p['fpts']:.1f} FPTS · "
                     f"{p['pts']} PTS, {p['reb']} REB, {p['ast']} AST, {p['stl']} STL, {p['blk']} BLK")
    lines += ["", "Scores: " + " · ".join(f"{team_key(g['away_team'])} {g['away_score']} @ "
                                         f"{team_key(g['home_team'])} {g['home_score']}" for g in games)]
    if credit:
        lines += ["", f"🎵 {credit}"]
    lines += ["", "#NBA #FantasyBasketball #NBAFantasy #FantasyNBA"
              + (" #NBAPreseason" if season == "PRESEASON" else "")]
    return "\n".join(lines)[:2200]


def main():
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_DIR, ".env"))
    ap = argparse.ArgumentParser(description="Last night's top-10 recap reel.")
    ap.add_argument("date", nargs="?", help="game date (US Eastern), default yesterday")
    ap.add_argument("--audio", help="audio file you have the rights to use (default: silent)")
    ap.add_argument("--credit", help="music credit line for the caption")
    ap.add_argument("--track", help="track name from Reel Audio/tracks.json (sets --audio and --credit)")
    args = ap.parse_args()
    if args.track:
        with open(TRACKS_PATH, encoding="utf-8") as f:
            track = json.load(f)["tracks"][args.track]
        args.audio = os.path.join(os.path.dirname(TRACKS_PATH), track["file"])
        args.credit = track["credit"]
    game_date = (dt.date.fromisoformat(args.date) if args.date
                 else dt.datetime.now(ET).date() - dt.timedelta(days=1))
    browser = find_browser()
    if not browser:
        raise SystemExit("No Chrome/Edge found -- can't render.")

    top = load_top(game_date)
    if len(top) < 10:
        raise SystemExit(f"Only {len(top)} performances for {game_date} -- run collect_boxscores.py first.")
    texts = load_text(game_date)
    missing = [p["player_name"] for p in top if p["player_id"] not in texts]
    if missing:
        print(f"No written headline (rule-based used): {', '.join(missing)}")
    games = games_on(game_date)
    jersey_of = jerseys(game_date)
    fonts = font_css()
    with open(CARD_TEMPLATE, encoding="utf-8") as f:
        card_t = f.read().replace("{{FONTS}}", fonts)
    with open(SLIDE_TEMPLATE, encoding="utf-8") as f:
        slide_t = f.read().replace("{{FONTS}}", fonts)

    def slide(body, team=None):
        return slide_t.replace("{{TEAM_VARS}}", team_vars(team) if team else "").replace("{{BODY}}", body)

    countdown = sorted(top, key=lambda p: -p["rank"])  # #10 first
    work_dir = make_dir(tempfile.gettempdir())
    try:
        def render(name, page):
            path = os.path.join(work_dir, f"{name}.png")
            render_png(browser, page, path, work_dir, HALF_W, HALF_H)
            return Image.open(path).convert("RGB")

        print(f"Rendering top 10 for {game_date}...")
        tops = [render("intro_top", slide(intro_top_body(game_date, games)))]
        slides = [render("intro_bottom", slide(intro_bottom_body()))]
        for p in countdown:
            jersey = jersey_of.get(p["player_id"], "")
            card = render_top_html(p, card_t, texts, jersey, game_date, photo=rank_html(p["rank"]))
            tops.append(render(f"card_{p['rank']}", card))
            slides.append(render(f"photo_{p['rank']}", slide(photo_body(p, jersey), team_key(p["team"]))))
        tops.append(render("outro_top", slide(list_body(top, game_date))))
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    timeline = ([(0, 0, INTRO_SECONDS)] + [(i, i, PLAYER_SECONDS) for i in range(1, 11)]
                + [(11, 10, OUTRO_SECONDS)])  # outro: full list over #1's photo
    duration = sum(s for _, _, s in timeline)
    os.makedirs(OUT_DIR, exist_ok=True)
    mp4 = os.path.join(OUT_DIR, f"{game_date.isoformat()}_top10.mp4")
    print(f"Encoding {duration:.1f}s video...")
    encode(frames(tops, slides, timeline), duration, mp4, args.audio)
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", str(INTRO_SECONDS + 1.5), "-i", mp4,
                    "-frames:v", "1", mp4.replace(".mp4", "_preview.png")], check=True)

    for folder in ("pending", "approved", "posted"):
        os.makedirs(os.path.join(QUEUE_DIR, folder), exist_ok=True)
    slug = f"{game_date.isoformat()}_00_top10-recap"
    shutil.copyfile(mp4, os.path.join(QUEUE_DIR, "pending", f"{slug}.mp4"))
    with open(os.path.join(QUEUE_DIR, "pending", f"{slug}.json"), "w", encoding="utf-8") as f:
        json.dump({"slug": slug, "video": f"{slug}.mp4", "series": "nightly_recap_reel",
                   "game_date": game_date.isoformat(), "duration": duration,
                   "players": [f"#{p['rank']} {p['player_name']}" for p in top],
                   "audio": os.path.basename(args.audio) if args.audio else None, "credit": args.credit,
                   "caption": caption(top, game_date, games, args.credit)}, f, ensure_ascii=False, indent=2)
    print(f"Saved {mp4}")
    for p in countdown:
        print(f"  #{p['rank']:<2} {p['player_name']:22} {team_key(p['team']):4} {p['fpts']:5.1f}")


if __name__ == "__main__":
    main()
