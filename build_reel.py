#!/usr/bin/env python3
"""
build_reel.py

Builds a 1080x1920 Instagram reel for one of a night's top-10 performances:
  - top half (1080x960): the player's stats in the nightly-card style, with a short
    intro animation (panel slides in, numbers count up), then held still
  - bottom half (1080x960): a highlight clip, scaled and centre-cropped to fit

Data and text are the same as the nightly cards (nba_boxscores + "Nightly Text/<date>.json").
The reel is as long as the clip (max 90 s). Clips without audio get a silent track.

Run collect_boxscores.py first (and write the Nightly Text for the date).

Output:
  - "Reels/<date>/<NN>_<name>.mp4" + a preview PNG
  - content/reels/pending/<date>_<NN>_<name>.mp4 + .json (with Instagram caption)

Usage:
  python build_reel.py 2026-10-05                 # #1 of that night, placeholder clip
  python build_reel.py 2026-10-05 3               # #3 of that night
  python build_reel.py 2026-10-05 1 --clip "Reel Clips/holland.mp4" --credit "Photos: ..."
"""

import argparse
import datetime as dt
import html
import json
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv

from build_nightly_cards import (ET, TEAM_NAMES, caption, card_name, card_text, date_label, headshot,
                                 jerseys, keep_hyphens, load_text, load_top, made_att, opponent_label,
                                 result_line, season_label, team_key)
from build_player_cards import file_url, find_browser, font_css, make_dir, render_png, team_vars

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(PROJECT_DIR, ".env"))

TEMPLATE_PATH = os.path.join(PROJECT_DIR, "reel_top_template.html")
OUT_DIR = os.path.join(PROJECT_DIR, "Reels")
QUEUE_DIR = os.path.join(PROJECT_DIR, "content", "reels")  # pending -> approved -> posted
DEFAULT_CLIP = os.path.join(PROJECT_DIR, "Reel Clips", "placeholder_16x9.mp4")
FFMPEG_DIR = os.path.join(PROJECT_DIR, "tools", "ffmpeg", "bin")
FFMPEG = os.path.join(FFMPEG_DIR, "ffmpeg.exe")
FFPROBE = os.path.join(FFMPEG_DIR, "ffprobe.exe")

TOP_W, TOP_H = 540, 480  # CSS px; rendered at 2x -> 1080x960
FPS = 30
INTRO_SECONDS = 2.8      # length of the top-half animation; the last frame is then held
MAX_SECONDS = 90
WORKERS = 6

# -----------------------------------------------------------------------------
# TOP HALF
# -----------------------------------------------------------------------------

def headline_size(text):
    n = len(text)
    return "s1" if n <= 34 else "s2" if n <= 46 else "s3"


def render_top_html(p, template, texts, jersey, game_date, photo=None):
    """photo: HTML for the right panel instead of the headshot (e.g. a big rank)."""
    e = html.escape
    team = team_key(p["team"])
    before, hl, after, _, _ = card_text(p, texts)
    meta = " · ".join(x for x in (TEAM_NAMES.get(team, team), p["position"] or "",
                                  f"#{jersey}" if jersey else "") if x)

    if photo is None:
        photo_path = headshot(p["player_id"])
        if photo_path:
            photo = f'<img class="photo" src="{e(file_url(photo_path))}" alt="">'
        else:
            initials = "".join(w[0] for w in p["player_name"].split()[:2]).upper()
            photo = f'<div class="nophoto">{e(initials)}</div>'

    # Big numbers: the five scoring stats, the best one (by fantasy value) highlighted
    weights = {"pts": 1, "reb": 1.2, "ast": 1.5, "stl": 3, "blk": 3}
    lead = max(weights, key=lambda k: (p[k] or 0) * weights[k])
    big = "".join(f'<div class="{"lead" if k == lead else ""}"><b data-count="{p[k] or 0}">{p[k] or 0}</b>'
                  f'<span>{label}</span></div>'
                  for k, label in (("pts", "PTS"), ("reb", "REB"), ("ast", "AST"), ("stl", "STL"), ("blk", "BLK")))
    pm = p["plus_minus"]
    strip = "".join(f'<div><b>{v}</b><span>{k}</span></div>' for k, v in (
        ("MIN", p["min"]), ("FG", made_att(p["fgm"], p["fga"])), ("3PT", made_att(p["tpm"], p["tpa"])),
        ("FT", made_att(p["ftm"], p["fta"])), ("TO", p["tov"]),
        ("+/-", "—" if pm is None else f'{"+" if pm > 0 else ""}{pm}')))
    badges = (f'<span class="{"win" if p["won"] else ""}">{result_line(p)} {opponent_label(p)}</span>'
              f'<span>{"Starter" if p["starter"] else "Bench"}</span>')

    return (template
            .replace("{{TEAM_VARS}}", team_vars(team))
            .replace("{{RANK}}", str(p["rank"]))
            .replace("{{GAME_LABEL}}", e(f"{season_label(p)} · {game_date.strftime('%b')} {game_date.day}"))
            .replace("{{NAME}}", e(p["player_name"]))
            .replace("{{META}}", e(meta))
            .replace("{{HEADLINE_SIZE}}", headline_size(before + hl + after))
            .replace("{{HEADLINE}}", f'{keep_hyphens(e(before))}<span class="hl">{keep_hyphens(e(hl))}</span>'
                                     f'{keep_hyphens(e(after))}')
            .replace("{{JERSEY}}", e(jersey))
            .replace("{{FPTS}}", f'{p["fpts"]:.1f}')
            .replace("{{PHOTO}}", photo)
            .replace("{{BIG}}", big)
            .replace("{{STRIP}}", strip)
            .replace("{{BADGES}}", badges)
            .replace("{{SOURCE}}", "Yahoo default points · ESPN box score"))


def render_frames(browser, html_text, frame_dir, work_dir):
    """One PNG per video frame of the intro animation."""
    n = round(INTRO_SECONDS * FPS) + 1

    def frame(i):
        page = f"<script>window.FRAME_T = {i / FPS:.4f};</script>" + html_text
        render_png(browser, page, os.path.join(frame_dir, f"{i:04d}.png"), work_dir, TOP_W, TOP_H)

    with ThreadPoolExecutor(WORKERS) as pool:
        list(pool.map(frame, range(n)))
    return n

# -----------------------------------------------------------------------------
# VIDEO
# -----------------------------------------------------------------------------

def probe(clip):
    out = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration:stream=codec_type",
                          "-of", "json", clip], check=True, capture_output=True, text=True).stdout
    info = json.loads(out)
    has_audio = any(s.get("codec_type") == "audio" for s in info.get("streams", []))
    return float(info["format"]["duration"]), has_audio


def compose(frame_dir, clip, mp4_path):
    duration, has_audio = probe(clip)
    duration = min(duration, MAX_SECONDS)
    cmd = [FFMPEG, "-y", "-loglevel", "error",
           "-framerate", str(FPS), "-i", os.path.join(frame_dir, "%04d.png"),
           "-i", clip]
    if not has_audio:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
    cmd += ["-filter_complex",
            f"[0:v]tpad=stop_mode=clone:stop_duration={duration},setsar=1[top];"
            f"[1:v]fps={FPS},scale=1080:960:force_original_aspect_ratio=increase,crop=1080:960,setsar=1[bot];"
            "[top][bot]vstack=inputs=2,format=yuv420p[v]",
            "-map", "[v]", "-map", "1:a" if has_audio else "2:a",
            "-t", f"{duration:.3f}", "-r", str(FPS),
            "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-profile:v", "high",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
            "-movflags", "+faststart", mp4_path]
    subprocess.run(cmd, check=True)
    return duration


def preview(mp4_path, png_path, at):
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", str(at), "-i", mp4_path,
                    "-frames:v", "1", png_path], check=True)

# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------

def export_pending(p, texts, game_date, mp4_path, clip, duration, credit=None):
    for folder in ("pending", "approved", "posted"):
        os.makedirs(os.path.join(QUEUE_DIR, folder), exist_ok=True)
    name = f"{game_date.isoformat()}_{card_name(p)}"
    before, hl, after, summary, _ = card_text(p, texts)
    cap = caption(p, texts, game_date).replace("🏀 TOP 10 LAST NIGHT |", "🎬 LAST NIGHT |", 1)
    if credit:
        cap = cap.replace("\n\n#NBA", f"\n\n📸 {credit}\n\n#NBA", 1)
    data = {
        "slug": name,
        "video": f"{name}.mp4",
        "series": "nightly_reel",
        "game_date": game_date.isoformat(),
        "rank": p["rank"],
        "player": p["player_name"],
        "player_id": p["player_id"],
        "team": team_key(p["team"]),
        "fpts": p["fpts"],
        "headline": before + hl + after,
        "summary": summary,
        "clip": os.path.relpath(clip, PROJECT_DIR),
        "duration": round(duration, 2),
        "credit": credit,
        "caption": cap,
    }
    pending = os.path.join(QUEUE_DIR, "pending")
    shutil.copyfile(mp4_path, os.path.join(pending, f"{name}.mp4"))
    with open(os.path.join(pending, f"{name}.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return os.path.join(pending, f"{name}.mp4")


def main():
    ap = argparse.ArgumentParser(description="Build a split-screen reel for a nightly top-10 player.")
    ap.add_argument("date", nargs="?", help="game date (US Eastern), default yesterday")
    ap.add_argument("rank", nargs="?", type=int, default=1, help="rank in that night's top 10 (default 1)")
    ap.add_argument("--clip", default=DEFAULT_CLIP, help="highlight clip for the bottom half")
    ap.add_argument("--credit", help="photo/video credit line added to the caption")
    args = ap.parse_args()

    game_date = (dt.date.fromisoformat(args.date) if args.date
                 else dt.datetime.now(ET).date() - dt.timedelta(days=1))
    if not os.path.exists(FFMPEG):
        raise SystemExit(f"ffmpeg not found at {FFMPEG}")
    if not os.path.exists(args.clip):
        raise SystemExit(f"Clip not found: {args.clip}")
    browser = find_browser()
    if not browser:
        raise SystemExit("No Chrome/Edge found -- can't render frames.")

    top = load_top(game_date)
    p = next((x for x in top if x["rank"] == args.rank), None)
    if not p:
        raise SystemExit(f"No #{args.rank} for {game_date} -- run collect_boxscores.py first.")
    texts = load_text(game_date)

    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        template = f.read().replace("{{FONTS}}", font_css())
    html_text = render_top_html(p, template, texts, jerseys(game_date).get(p["player_id"], ""), game_date)

    out_dir = os.path.join(OUT_DIR, game_date.isoformat())
    os.makedirs(out_dir, exist_ok=True)
    mp4_path = os.path.join(out_dir, f"{card_name(p)}.mp4")
    work_dir = make_dir(tempfile.gettempdir())
    try:
        frame_dir = make_dir(work_dir)
        print(f"Rendering intro frames for #{p['rank']} {p['player_name']}...")
        n = render_frames(browser, html_text, frame_dir, work_dir)
        print(f"  {n} frames. Composing video...")
        duration = compose(frame_dir, args.clip, mp4_path)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    preview(mp4_path, os.path.join(out_dir, f"{card_name(p)}_preview.png"), min(5, duration / 2))
    queued = export_pending(p, texts, game_date, mp4_path, args.clip, duration, args.credit)
    print(f"Saved {mp4_path} ({duration:.1f}s)")
    print(f"Queued {queued}")


if __name__ == "__main__":
    main()
