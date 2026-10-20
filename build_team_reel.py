#!/usr/bin/env python3
"""
build_team_reel.py

"Projected top 6" reel for one team (1080x1920 MP4):
  - top half: half court with the team name, the 5 projected starters in formation
    and the 6th man beside the court; the player featured below is highlighted
  - bottom half: an intro slide (logo, projected wins), then one slide per player
    (photo, projected minutes, fantasy points and per-game stats), then the intro again

Players come from the depth chart (build_depth_chart.py): its 5 starters plus the
bench player with the most Yahoo projected fantasy points as 6th man.
Starters can be pinned in depth_overrides.json. Minutes: ESPN projections. FPTS and per-game
stats: Yahoo projections (season totals / projected GP). Projected wins:
projected_wins.json.

Audio: silent unless --audio is given (a file you have the rights to use). Songs from
Instagram's music library can only be added in the Instagram app.

Usage:
  python build_team_reel.py ATL
  python build_team_reel.py ATL --audio "Reel Audio/track.mp3" --credit "Track by ... (CC BY 4.0)"
Output: "Reels/teams/<TEAM>_top6.mp4" (+ preview PNG), queued in content/reels/pending
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

import build_depth_chart as dc
from build_nightly_cards import name_key
from build_player_cards import (TEAM_NAMES, file_url, find_browser, font_css, make_dir, render_png,
                                team_vars)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
TOP_TEMPLATE = os.path.join(PROJECT_DIR, "reel_court_top.html")
SLIDE_TEMPLATE = os.path.join(PROJECT_DIR, "reel_player_slide.html")
OUT_DIR = os.path.join(PROJECT_DIR, "Reels", "teams")
QUEUE_DIR = os.path.join(PROJECT_DIR, "content", "reels")
FFMPEG = os.path.join(PROJECT_DIR, "tools", "ffmpeg", "bin", "ffmpeg.exe")

HALF_W, HALF_H = 540, 480  # CSS px per half; rendered at 2x -> 1080x960
FPS = 30
INTRO_SECONDS, PLAYER_SECONDS, OUTRO_SECONDS = 2.5, 4.0, 2.0
FADE_SECONDS = 0.35        # crossfade between slides / highlight states
ZOOM = 0.035               # slow push-in on each slide

# Half court (50 x 47 ft) at 7.6 px/ft; token centres in feet, basket at the top.
PX_PER_FT = 7.6
COURT_W = 380
TOKEN_W, TOKEN_H = 132, 50
SPOTS = {"C": (12, 8.5), "PF": (38, 9.5), "SF": (9.5, 22), "SG": (40.5, 23), "PG": (25, 34)}

# -----------------------------------------------------------------------------
# DATA
# -----------------------------------------------------------------------------

def top_six(team):
    """[(player, role_pos, role_label)] for the 5 starters (PG..C) and the 6th man:
    the bench player with the most Yahoo projected fantasy points."""
    players = dc.load_players(team, dc.current_roster(team))
    columns = dc.arrange(players, dc.load_overrides(team))
    starters = [(columns[pos][0], pos, f"Starting {pos}") for pos in dc.POSITIONS]
    starter_ids = {p["player_id"] for p, _, _ in starters}
    bench = [p for p in players if p["player_id"] not in starter_ids]
    sixth = max(bench, key=lambda p: (p["fpts"] is not None, p["fpts"] or 0, p["mpg"] or 0))
    sixth_pos = next(pos for pos in dc.POSITIONS if sixth in columns[pos])
    return starters + [(sixth, sixth_pos, "6th man")]


def add_yahoo_stats(six):
    """Yahoo per-game stats (season totals / projected GP) onto each player."""
    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(text(
            "SELECT espn_player_id, full_name, gp, pts, reb, ast, stl, blk FROM yahoo_projections"))]
    by_id = {r["espn_player_id"]: r for r in rows if r["espn_player_id"]}
    by_name = {name_key(r["full_name"]): r for r in rows}
    for p, _, _ in six:
        y = by_id.get(p["player_id"]) or by_name.get(name_key(p["full_name"]))
        for k in ("pts", "reb", "ast", "stl", "blk"):
            p[f"y_{k}"] = (y[k] / y["gp"]) if y and y["gp"] and y[k] is not None else None

# -----------------------------------------------------------------------------
# HTML
# -----------------------------------------------------------------------------

def token_html(p, pos, state, left, top):
    cls = "token" + {"on": " on", "dim": " dim"}.get(state, "")
    return (f'<div class="{cls}" style="left:{left:.1f}px;top:{top:.1f}px">'
            f'{dc.face_html(p)}<div class="info">{dc.player_info(p, pos)}</div></div>')


def top_html(template, team, six, featured):
    """featured: index into six, or None for the intro state (nobody highlighted)."""
    def state(i):
        return "" if featured is None else ("on" if i == featured else "dim")

    tokens = []
    for i, (p, pos, _) in enumerate(six[:5]):
        x, y = SPOTS[pos]
        left = min(max(x * PX_PER_FT - TOKEN_W / 2, 4), COURT_W - TOKEN_W - 4)
        tokens.append(token_html(p, pos, state(i), left, y * PX_PER_FT - TOKEN_H / 2))
    p, pos, _ = six[5]
    sixth = token_html(p, pos, state(5), (HALF_W - TOKEN_W) / 2, 418)
    return (template
            .replace("{{TEAM_VARS}}", team_vars(team))
            .replace("{{TEAM_NAME}}", html.escape(TEAM_NAMES.get(team, team)))
            .replace("{{COURT_TOKENS}}", "".join(tokens))
            .replace("{{SIXTH_TOKEN}}", sixth))


def wins_text(team):
    raw = dc.WINS.get("wins", {}).get(team)
    return None if raw is None else (dc.round_half_up(raw), raw)


def intro_body(team):
    e = html.escape
    logo = dc.team_logo(team)
    logo_img = f'<img src="{e(file_url(logo))}" alt="">' if logo else ""
    wins = wins_text(team)
    wins_block = (f'<div class="wins"><b>{wins[0]}</b><span>PROJECTED WINS · {wins[0]}–{82 - wins[0]}</span></div>'
                  if wins else "")
    return (f'<div class="slide intro"><div class="logo">{logo_img}</div>'
            f'<div class="title"><div class="kicker">2026-27 projections</div>'
            f'<h1>Projected<br>top 6</h1></div>{wins_block}'
            f'<div class="cta">Starting five + 6th man</div></div>')


def player_body(p, role, n, total):
    e = html.escape
    first, last = dc.short_name(p["full_name"])
    photo_path = dc.headshot(str(p["player_id"]))
    photo = f'<img src="{e(file_url(photo_path))}" alt="">' if photo_path else ""
    size = " xlong" if len(last) > 13 else " long" if len(last) > 9 else ""
    meta = " · ".join(x for x in (f"#{p['jersey']}" if p.get("jersey") else "",
                                  "/".join(p["positions"] or []), p.get("injury") or "") if x)
    strip = "".join(f'<div><b>{dc.fmt(p[f"y_{k}"])}</b><span>{k.upper()}</span></div>'
                    for k in ("pts", "reb", "ast", "stl", "blk"))
    return (f'<div class="slide"><div class="row-top"><span class="role">{e(role)}</span>'
            f'<span class="count"><b>{n}</b> / {total}</span></div>'
            f'<div class="photo"><div class="jersey">{e(p.get("jersey") or "")}</div>{photo}</div>'
            f'<div class="who"><div class="first">{e(first)}</div><div class="last{size}">{e(last)}</div>'
            f'<div class="meta">{e(meta)}</div></div>'
            f'<div class="big"><div><b>{dc.fmt(p["mpg"])}</b><span>MINUTES / GAME</span></div>'
            f'<div class="fp"><b>{dc.fmt(p["fpts"])}</b><span>FANTASY PTS / GAME</span></div></div>'
            f'<div class="strip">{strip}</div>'
            f'<div class="note">Per game · minutes: ESPN projection · stats &amp; fantasy points: Yahoo projection</div>'
            f'</div>')


def slide_html(template, team, body):
    return (template.replace("{{TEAM_VARS}}", team_vars(team))
            .replace("{{TEAM_NAME}}", html.escape(TEAM_NAMES.get(team, team)))
            .replace("{{BODY}}", body))

# -----------------------------------------------------------------------------
# VIDEO
# -----------------------------------------------------------------------------

def zoomed(img, k):
    """Push in by ZOOM over the slide (k = 0..1), keeping the centre."""
    z = 1 + ZOOM * k
    w, h = img.width / z, img.height / z
    x0, y0 = (img.width - w) / 2, (img.height - h) / 2
    return img.resize(img.size, Image.LANCZOS, box=(x0, y0, x0 + w, y0 + h))


def frames(tops, slides, timeline):
    """timeline: [(top_index, slide_index, seconds)]. Crossfade into each segment."""
    fade = round(FADE_SECONDS * FPS)
    prev_top = prev_bottom = None
    for top_i, slide_i, seconds in timeline:
        n = round(seconds * FPS)
        for f in range(n):
            top = tops[top_i]
            bottom = zoomed(slides[slide_i], f / max(n - 1, 1))
            if f < fade and prev_top is not None:
                a = (f + 1) / (fade + 1)
                top = Image.blend(prev_top, top, a)
                bottom = Image.blend(prev_bottom, bottom, a)
            frame = Image.new("RGB", (1080, 1920))
            frame.paste(top, (0, 0))
            frame.paste(bottom, (0, 960))
            yield frame
        prev_top, prev_bottom = tops[top_i], zoomed(slides[slide_i], 1)


def encode(frame_iter, duration, mp4_path, audio=None):
    cmd = [FFMPEG, "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "1080x1920", "-r", str(FPS), "-i", "-"]
    if audio:
        cmd += ["-i", audio]
        # Normalise to a consistent social-video loudness (-16 LUFS, peaks <= -1.5 dB).
        afilter = (f"atrim=0:{duration},loudnorm=I=-16:TP=-1.5:LRA=11,afade=t=in:d=0.3,"
                   f"afade=t=out:st={max(duration - 1.5, 0):.2f}:d=1.5")
    else:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
        afilter = f"atrim=0:{duration}"
    cmd += ["-filter_complex", f"[1:a]{afilter}[a]", "-map", "0:v", "-map", "[a]",
            "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p", "-profile:v", "high",
            "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-t", f"{duration:.3f}",
            "-movflags", "+faststart", mp4_path]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for frame in frame_iter:
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise SystemExit("ffmpeg failed")

# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------

def caption(team, six, credit=None):
    name = TEAM_NAMES.get(team, team)
    wins = wins_text(team)
    lines = [f"🏀 {name.upper()} · PROJECTED TOP 6 (2026-27)", ""]
    for p, pos, role in six:
        lines.append(f"{'6th' if role == '6th man' else pos}: {p['full_name']} – "
                     f"{dc.fmt(p['mpg'])} MIN · {dc.fmt(p['fpts'])} FPTS")
    if wins:
        lines += ["", f"Projected wins: {wins[0]} ({wins[0]}–{82 - wins[0]}) · BetMGM win total {wins[1]:g}"]
    lines += ["", "Minutes: ESPN projections · Stats & fantasy points: Yahoo projections"]
    if credit:
        lines += ["", f"🎵 {credit}"]
    lines += [
              "", f"#NBA #FantasyBasketball #NBAFantasy #FantasyNBA #{team}"]
    return "\n".join(lines)[:2200]


def main():
    ap = argparse.ArgumentParser(description="Projected top-6 reel for a team.")
    ap.add_argument("team")
    ap.add_argument("--audio", help="audio file you have the rights to use (default: silent)")
    ap.add_argument("--credit", help="music credit line for the caption (e.g. a CC BY attribution)")
    args = ap.parse_args()
    team = args.team.upper()
    browser = find_browser()
    if not browser:
        raise SystemExit("No Chrome/Edge found -- can't render.")

    six = top_six(team)
    add_yahoo_stats(six)
    fonts = font_css()
    with open(TOP_TEMPLATE, encoding="utf-8") as f:
        top_t = f.read().replace("{{FONTS}}", fonts)
    with open(SLIDE_TEMPLATE, encoding="utf-8") as f:
        slide_t = f.read().replace("{{FONTS}}", fonts)

    work_dir = make_dir(tempfile.gettempdir())
    try:
        def render(name, page):
            path = os.path.join(work_dir, f"{name}.png")
            render_png(browser, page, path, work_dir, HALF_W, HALF_H)
            return Image.open(path).convert("RGB")

        print(f"Rendering {TEAM_NAMES.get(team, team)} top 6...")
        tops = [render("top_intro", top_html(top_t, team, six, None))]
        tops += [render(f"top_{i}", top_html(top_t, team, six, i)) for i in range(6)]
        slides = [render("slide_intro", slide_html(slide_t, team, intro_body(team)))]
        slides += [render(f"slide_{i}", slide_html(slide_t, team, player_body(p, role, i + 1, 6)))
                   for i, (p, _, role) in enumerate(six)]
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    timeline = ([(0, 0, INTRO_SECONDS)] + [(i + 1, i + 1, PLAYER_SECONDS) for i in range(6)]
                + [(0, 0, OUTRO_SECONDS)])
    duration = sum(s for _, _, s in timeline)
    os.makedirs(OUT_DIR, exist_ok=True)
    mp4 = os.path.join(OUT_DIR, f"{team}_top6.mp4")
    print(f"Encoding {duration:.1f}s video...")
    encode(frames(tops, slides, timeline), duration, mp4, args.audio)
    preview = mp4.replace(".mp4", "_preview.png")
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", str(INTRO_SECONDS + 1.5), "-i", mp4,
                    "-frames:v", "1", preview], check=True)

    for folder in ("pending", "approved", "posted"):
        os.makedirs(os.path.join(QUEUE_DIR, folder), exist_ok=True)
    slug = f"{dt.date.today().isoformat()}_00_{team.lower()}-top6"
    shutil.copyfile(mp4, os.path.join(QUEUE_DIR, "pending", f"{slug}.mp4"))
    with open(os.path.join(QUEUE_DIR, "pending", f"{slug}.json"), "w", encoding="utf-8") as f:
        json.dump({"slug": slug, "video": f"{slug}.mp4", "series": "team_top6", "team": team,
                   "players": [p["full_name"] for p, _, _ in six], "duration": duration,
                   "audio": os.path.basename(args.audio) if args.audio else None,
                   "credit": args.credit, "caption": caption(team, six, args.credit)},
                  f, ensure_ascii=False, indent=2)
    print(f"Saved {mp4}")
    for p, pos, role in six:
        print(f"  {role:12} {p['full_name']:26} {dc.fmt(p['mpg'])} MIN  {dc.fmt(p['fpts'])} FPTS")


if __name__ == "__main__":
    main()
