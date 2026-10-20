#!/usr/bin/env python3
"""
build_top10_reel.py

"Last night's top 10" reel, enriched version (1080x1920 MP4, ~104 s):
  - poster (4 s, reel_poster_template.html): the night's top 3 as cut-out photos under
    a big title. It is frame 0, so Instagram uses it as the cover/thumbnail. Photos come
    from "Reels/posters/<date>/poster.json" (freely licensed, cut out with rembg; credits
    go in the caption); without it the ESPN headshots are used.
  - #10 -> #1, 9 s each: the player's card on top (animated in), the bottom half
    shows his photo (slow push-in) for 4.5 s, then his shot chart with the shots
    popping in one by one in the order he took them
  - players change with a diagonal wipe in the incoming team's colour
  - outro (10 s, reel_outro_template.html): the top 10 as a table with MIN, FPTS, PTS,
    REB, AST, STL, BLK
Music from "Reel Audio/tracks.json" (CC BY 4.0, credit goes in the caption).

Headlines come from "Nightly Text/<date>.json"; shots from the ESPN play-by-play
(see build_shot_chart.py). Run collect_boxscores.py first.

Usage:
  python build_top10_reel.py 2026-10-07 --track Galactic_Rap
Output: "Reels/recaps/<date>_top10_shots.mp4", queued in content/reels/pending
"""

import argparse
import datetime as dt
import html
import json
import math
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageDraw

from build_nightly_cards import ET, date_label, headshot, jerseys, load_text, load_top, team_key
from build_boxscore_chart import COUNTING, top_stats
from build_depth_chart import team_logo
from build_player_cards import (DEFAULT_COLORS, TEAM_COLORS, contrast, file_url, find_browser, font_css, make_dir,
                                render_png, team_vars)
from build_recap_reel import caption as recap_caption
from build_recap_reel import games_on, photo_body, rank_html
from build_reel import render_top_html
from build_shot_chart import HOOP_Y, load_shots, pct, zone
from build_team_reel import FFMPEG, FPS, encode

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CARD_TEMPLATE = os.path.join(PROJECT_DIR, "reel_top_template.html")
SLIDE_TEMPLATE = os.path.join(PROJECT_DIR, "recap_slides.html")
SHOT_TEMPLATE = os.path.join(PROJECT_DIR, "reel_shot_slide.html")
POSTER_TEMPLATE = os.path.join(PROJECT_DIR, "reel_poster_template.html")
OUTRO_TEMPLATE = os.path.join(PROJECT_DIR, "reel_outro_template.html")
POSTER_DIR = os.path.join(PROJECT_DIR, "Reels", "posters")
TRACKS_PATH = os.path.join(PROJECT_DIR, "Reel Audio", "tracks.json")
OUT_DIR = os.path.join(PROJECT_DIR, "Reels", "recaps")
QUEUE_DIR = os.path.join(PROJECT_DIR, "content", "reels")

W, H, HALF = 1080, 1920, 960
POSTER_S, PHOTO_S, CHART_S, OUTRO_S = 4.0, 4.5, 4.5, 10.0
POSTER_ZOOM = 0.035                        # slow push-in on the poster
OUTRO_ANIM_S = 2.0                         # outro table animates in over this, then holds
CARD_ANIM_S, CARD_ANIM_FPS = 2.8, 15      # card intro animation, rendered at 15 fps and doubled
WIPE_S, FADE_S = 0.45, 0.3
ZOOM = 0.04
WORKERS = 6

# Court box on the shot slide, in output pixels (CSS left 18 / top 64 / 360 x 338.4, at 2x)
COURT_BOX = (36, 128, 720, 676.8)
PX_FT = COURT_BOX[2] / 50
SS = 3                                     # supersampling for smooth markers
REVEAL = (0.3, 3.4)                       # shots appear across this part of the chart segment (s)
POP_S = 0.2
MAKE, MAKE_EDGE, MISS, MISS_EDGE = (47, 210, 122), (11, 61, 34), (255, 90, 79), (60, 10, 10)

# -----------------------------------------------------------------------------
# SLIDES
# -----------------------------------------------------------------------------

def player_vars(team):
    dark, accent = TEAM_COLORS.get(team, DEFAULT_COLORS)
    on = "#111111" if contrast(accent, "#111111") >= contrast(accent, "#ffffff") else "#ffffff"
    readable = accent if contrast(accent, dark) >= 3 else "#ffffff"
    return f"--tc:{dark};--ta:{accent};--tt:{readable};--on:{on}"


def load_poster(game_date):
    """Reels/posters/<date>/poster.json: {"players": {player_id: {"photo": path, "show": 0..1}},
    "credits": [caption lines]}. Photo paths are relative to that folder."""
    folder = os.path.join(POSTER_DIR, game_date.isoformat())
    try:
        with open(os.path.join(folder, "poster.json"), encoding="utf-8") as f:
            spec = json.load(f)
    except FileNotFoundError:
        return {}, []
    players = {str(k): {**v, "photo": os.path.join(folder, v["photo"])} for k, v in spec.get("players", {}).items()}
    return players, spec.get("credits", [])


def poster_body(top, game_date, games, photos):
    """Poster: title, then #2 / #1 / #3 as cut-outs with name plates."""
    e = html.escape
    season = "Preseason" if games and games[0]["season_type"] == "preseason" else "NBA"
    head = (f'<div class="head"><span class="kicker">{season} · {e(date_label(game_date))}</span>'
            f'<h1><small>Last night&#39;s</small><b>Top 10</b></h1></div>')
    figs, plates = [], []
    for p in (top[1], top[0], top[2]):
        team, r = team_key(p["team"]), p["rank"]
        dark, accent = TEAM_COLORS.get(team, DEFAULT_COLORS)
        style = f"--pc:{dark};--pa:{accent}"
        spec = photos.get(str(p["player_id"]))
        path = spec["photo"] if spec else headshot(str(p["player_id"]))
        if path:
            show = spec.get("show", 1) if spec else 1
            with Image.open(path) as im:
                ratio = im.height / im.width
            cut = (f'<div class="cut" style="aspect-ratio:1/{ratio * show:.3f}">'
                   f'<img src="{e(file_url(path))}" alt=""></div>')
        else:
            cut = f'<div class="ini">{e("".join(w[0] for w in p["player_name"].split()[:2]))}</div>'
        figs.append(f'<div class="fig r{r}" style="{style}"><span class="num">{r}</span>{cut}</div>')
        first, _, last = p["player_name"].partition(" ")
        line = " · ".join(f"{p[k]} {lab}" for k, lab in COUNTING if p[k] and k in top_stats(p))
        plates.append(f'<div class="plate r{r}" style="{style}"><span class="rk">#{r} · {e(team)}</span>'
                      f'<div class="nm"><small>{e(first)}</small><b>{e(last or first)}</b></div>'
                      f'<div class="fp">{p["fpts"]:.1f}<small>FPTS</small></div><div class="ln">{e(line)}</div></div>')
    foot = '<div class="foot">Fantasy points · Yahoo scoring · <b>#10 → #1 inside</b></div>'
    return f'{head}<div class="stage">{"".join(figs)}</div><div class="plates">{"".join(plates)}</div>{foot}'


def outro_body(top, game_date, games):
    """Closing table: the top 10 with MIN, FPTS, PTS, REB, AST, STL, BLK."""
    e = html.escape
    season = "Preseason" if games and games[0]["season_type"] == "preseason" else "NBA"
    chips = "".join(f'<span>{e(team_key(g["away_team"]))} <b>{g["away_score"]}</b> @ '
                    f'{e(team_key(g["home_team"]))} <b>{g["home_score"]}</b></span>' for g in games)
    head = (f'<div class="head"><div class="kicker in" style="--d:0">{season} · {e(date_label(game_date))}</div>'
            f'<h1 class="in" style="--d:.1">Last night&#39;s <span>top 10</span></h1>'
            f'<div class="games in" style="--d:.2">{chips}</div></div>')
    cols = [("min", "MIN"), ("fpts", "FPTS"), ("pts", "PTS"), ("reb", "REB"), ("ast", "AST"), ("stl", "STL"),
            ("blk", "BLK")]
    rows = ['<div class="r th in" style="--d:.3"><span></span><span></span><span class="n">Player</span>'
            + "".join(f"<span>{lab}</span>" for _, lab in cols) + "</div>"]
    for i, p in enumerate(top):
        team = team_key(p["team"])
        path = headshot(str(p["player_id"]))
        ini = "".join(w[0] for w in p["player_name"].split()[:2])
        face = f'<img src="{e(file_url(path))}" alt="">' if path else f'<span class="ini">{e(ini)}</span>'
        logo = team_logo(team)
        logo = f'<img src="{e(file_url(logo))}" alt="">' if logo else ""
        opp = team_key(p["away_team"] if p["home"] else p["home_team"])
        key = top_stats(p)
        vals = []
        for k, _ in cols:
            if k == "fpts":
                vals.append(f'<span class="v fp">{p["fpts"]:.1f}</span>')
            else:
                cls = "v" + (" z" if not p[k] else "") + (" hi" if k in key else "")
                vals.append(f'<span class="{cls}">{p[k]}</span>')
        rows.append(f'<div class="r slide{" top" if i < 3 else ""}" style="{player_vars(team)};--d:{.45 + .12 * i:.2f}">'
                    f'<span class="rk">{p["rank"]}</span><div class="face">{face}</div>'
                    f'<div class="who"><b>{e(p["player_name"])}</b><small>{logo}{e(team)} '
                    f'{"vs" if p["home"] else "@"} {e(opp)} · {"W" if p["won"] else "L"} '
                    f'{p["team_score"]}-{p["opp_score"]}</small></div>{"".join(vals)}</div>')
    foot = ('<div class="foot in" style="--d:1.8">FPTS = Yahoo default points · '
            '<b>Follow for every night&#39;s top 10</b></div>')
    return f'{head}<div class="board">{"".join(rows)}</div>{foot}'


def shot_slide_html(template, p, shots):
    e = html.escape
    fgm, fga = sum(s["made"] for s in shots), len(shots)
    side = (f'<div class="fg"><span>Field goals</span><b>{fgm}-{fga}</b><small>{pct(fgm, fga)}</small></div>')
    for name in ("Paint", "Mid-range", "3PT"):
        z = [s for s in shots if zone(s) == name]
        m = sum(s["made"] for s in z)
        side += (f'<div class="zone{" none" if not z else ""}"><span class="k">{name}</span>'
                 f'<b>{m}-{len(z)}</b><span>{pct(m, len(z))}</span></div>')
    side += (f'<div class="zone{" none" if not p["fta"] else ""}"><span class="k">Free throws</span>'
             f'<b>{p["ftm"]}-{p["fta"]}</b><span>{pct(p["ftm"], p["fta"])}</span></div>')
    foot = "".join(f'<div{" class=fp" if k == "FPTS" else ""}><b>{v}</b><span>{k}</span></div>' for k, v in (
        ("PTS", p["pts"]), ("REB", p["reb"]), ("AST", p["ast"]), ("STL", p["stl"]), ("BLK", p["blk"]),
        ("FPTS", f'{p["fpts"]:.1f}')))
    return (template.replace("{{TEAM_VARS}}", team_vars(team_key(p["team"])))
            .replace("{{RANK}}", str(p["rank"])).replace("{{NAME}}", e(p["player_name"]))
            .replace("{{SIDE}}", side).replace("{{FOOT}}", foot))

# -----------------------------------------------------------------------------
# ANIMATION
# -----------------------------------------------------------------------------

def ease(t):
    t = min(max(t, 0.0), 1.0)
    return 1 - (1 - t) ** 3


def zoomed(img, k):
    z = 1 + ZOOM * k
    w, h = img.width / z, img.height / z
    x0, y0 = (img.width - w) / 2, (img.height - h) / 2
    return img.resize(img.size, Image.BILINEAR, box=(x0, y0, x0 + w, y0 + h))


def chart_frame(base, shots, t):
    """Shot chart at time t (s) into its segment: shots pop in one by one."""
    n = len(shots)
    if not n:
        return base
    start, end = REVEAL
    step = (end - start) / max(n, 1)
    bx, by, bw, bh = COURT_BOX
    layer = Image.new("RGBA", (round(bw * SS), round(bh * SS)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    for i, s in enumerate(shots):
        k = (t - (start + i * step)) / POP_S
        if k <= 0:
            continue
        a = ease(k)
        scale = 1 + 0.9 * (1 - a)
        x = min(max(s["x"], 1), 49) * PX_FT * SS
        y = min(s["y"] + HOOP_Y, 46) * PX_FT * SS
        alpha = round(255 * min(k * 1.5, 1))
        if s["made"]:
            r = 0.95 * PX_FT * SS * scale
            draw.ellipse((x - r, y - r, x + r, y + r), fill=MAKE + (alpha,), outline=MAKE_EDGE + (alpha,),
                         width=round(0.2 * PX_FT * SS))
        else:
            d = 0.72 * PX_FT * SS * scale
            for col, wdt in ((MISS_EDGE, 0.85), (MISS, 0.45)):
                for seg in (((x - d, y - d), (x + d, y + d)), ((x + d, y - d), (x - d, y + d))):
                    draw.line(seg, fill=col + (alpha,), width=round(wdt * PX_FT * SS))
    layer = layer.resize((round(bw), round(bh)), Image.LANCZOS)
    out = base.copy()
    out.paste(layer, (round(bx), round(by)), layer)
    return out


def wipe(prev, new, k, color):
    """Diagonal wipe from prev to new (k = 0..1) with a coloured leading edge."""
    k = ease(k)
    slant, band = 500, 70
    edge = -slant - band + k * (W + slant + 2 * band)
    mask = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(mask)
    d.polygon([(-10, 0), (edge + slant, 0), (edge, H), (-10, H)], fill=255)
    out = Image.composite(new, prev, mask)
    d2 = ImageDraw.Draw(out)
    d2.polygon([(edge + slant, 0), (edge + slant + band, 0), (edge + band, H), (edge, H)], fill=color)
    return out


def stack(top, bottom):
    frame = Image.new("RGB", (W, H))
    frame.paste(top, (0, 0))
    frame.paste(bottom, (0, HALF))
    return frame

# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------

def main():
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_DIR, ".env"))
    ap = argparse.ArgumentParser(description="Top-10 reel with shot charts.")
    ap.add_argument("date", nargs="?", help="game date (US Eastern), default yesterday")
    ap.add_argument("--track", help="track name from Reel Audio/tracks.json")
    args = ap.parse_args()
    audio = credit = None
    if args.track:
        with open(TRACKS_PATH, encoding="utf-8") as f:
            track = json.load(f)["tracks"][args.track]
        audio = os.path.join(os.path.dirname(TRACKS_PATH), track["file"])
        credit = track["credit"]
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
    card_t = open(CARD_TEMPLATE, encoding="utf-8").read().replace("{{FONTS}}", fonts)
    slide_t = open(SLIDE_TEMPLATE, encoding="utf-8").read().replace("{{FONTS}}", fonts)
    shot_t = open(SHOT_TEMPLATE, encoding="utf-8").read().replace("{{FONTS}}", fonts)
    poster_t = open(POSTER_TEMPLATE, encoding="utf-8").read().replace("{{FONTS}}", fonts)
    outro_t = open(OUTRO_TEMPLATE, encoding="utf-8").read().replace("{{FONTS}}", fonts)
    photos, photo_credits = load_poster(game_date)
    if not photos:
        print("No poster.json -- the poster uses ESPN headshots.")

    def slide(body, team=None):
        return slide_t.replace("{{TEAM_VARS}}", team_vars(team) if team else "").replace("{{BODY}}", body)

    countdown = sorted(top, key=lambda p: -p["rank"])  # #10 first
    for p in countdown:
        p["shots"] = load_shots(p)
        fgm = sum(s["made"] for s in p["shots"])
        if (fgm, len(p["shots"])) != (p["fgm"], p["fga"]):
            print(f"  note: {p['player_name']} play-by-play {fgm}-{len(p['shots'])} vs box {p['fgm']}-{p['fga']}")

    work_dir = make_dir(tempfile.gettempdir())
    try:
        def render(name, page):
            path = os.path.join(work_dir, f"{name}.png")
            full = name.startswith(("poster", "outro"))
            render_png(browser, page, path, work_dir, 540, 960 if full else 480)
            return path

        print(f"Rendering slides for {game_date}...")
        jobs = {"poster": poster_t.replace("{{VARS}}", player_vars(team_key(top[0]["team"])))
                .replace("{{BODY}}", poster_body(top, game_date, games, photos))}
        outro = outro_t.replace("{{BODY}}", outro_body(top, game_date, games))
        n_outro = round(OUTRO_ANIM_S * CARD_ANIM_FPS) + 1
        jobs.update({f"outro_{i:03d}": f"<script>window.FRAME_T = {i / CARD_ANIM_FPS:.4f};</script>" + outro
                     for i in range(n_outro)})
        n_anim = round(CARD_ANIM_S * CARD_ANIM_FPS) + 1
        for p in countdown:
            r, team = p["rank"], team_key(p["team"])
            card = render_top_html(p, card_t, texts, jersey_of.get(p["player_id"], ""), game_date,
                                   photo=rank_html(r))
            for i in range(n_anim):
                jobs[f"card_{r}_{i:03d}"] = f"<script>window.FRAME_T = {i / CARD_ANIM_FPS:.4f};</script>" + card
            jobs[f"photo_{r}"] = slide(photo_body(p, jersey_of.get(p["player_id"], "")), team)
            jobs[f"chart_{r}"] = shot_slide_html(shot_t, p, p["shots"])
        with ThreadPoolExecutor(WORKERS) as pool:
            paths = dict(zip(jobs, pool.map(lambda kv: render(*kv), jobs.items())))
        print(f"  {len(paths)} renders")

        def img(name):
            return Image.open(paths[name]).convert("RGB")

        def frame_iter():
            prev = None
            # Poster: frame 0 is the exact poster (the reel's cover), then a slow push-in
            poster = img("poster")
            n_poster = round(POSTER_S * FPS)
            for f in range(n_poster):
                prev = zoomed(poster, ease(f / n_poster) * POSTER_ZOOM / ZOOM) if f else poster
                yield prev
            for p in countdown:
                r = p["rank"]
                accent = TEAM_COLORS.get(team_key(p["team"]), DEFAULT_COLORS)[1]
                cards = [img(f"card_{r}_{i:03d}") for i in range(n_anim)]
                photo, chart = img(f"photo_{r}"), img(f"chart_{r}")
                n_photo, n_chart = round(PHOTO_S * FPS), round(CHART_S * FPS)
                n_wipe, n_fade = round(WIPE_S * FPS), round(FADE_S * FPS)
                last_photo, prev_hold = None, prev
                for f in range(n_photo + n_chart):
                    t = f / FPS
                    top_img = cards[min(int(t * CARD_ANIM_FPS), n_anim - 1)]
                    if f < n_photo:
                        bottom = zoomed(photo, f / (n_photo + n_fade))
                        last_photo = bottom
                    else:
                        bottom = chart_frame(chart, p["shots"], (f - n_photo) / FPS)
                        if f - n_photo < n_fade:
                            bottom = Image.blend(last_photo, bottom, (f - n_photo + 1) / (n_fade + 1))
                    frame = stack(top_img, bottom)
                    if f < n_wipe and prev_hold is not None:
                        frame = wipe(prev_hold, frame, (f + 1) / n_wipe, accent)
                    yield frame
                    prev = frame
            # Outro: the stat table slides in, then holds
            outros = [img(f"outro_{i:03d}") for i in range(n_outro)]
            n_out = round(OUTRO_S * FPS)
            last = prev
            for f in range(n_out):
                frame = outros[min(int(f / FPS * CARD_ANIM_FPS), n_outro - 1)]
                if f < n_fade:
                    frame = Image.blend(last, frame, (f + 1) / (n_fade + 1))
                yield frame

        duration = POSTER_S + len(countdown) * (PHOTO_S + CHART_S) + OUTRO_S
        os.makedirs(OUT_DIR, exist_ok=True)
        mp4 = os.path.join(OUT_DIR, f"{game_date.isoformat()}_top10_shots.mp4")
        print(f"Encoding {duration:.1f}s video...")
        encode(frame_iter(), duration, mp4, audio)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", "0", "-i", mp4,
                    "-frames:v", "1", mp4.replace(".mp4", "_preview.png")], check=True)
    for folder in ("pending", "approved", "posted"):
        os.makedirs(os.path.join(QUEUE_DIR, folder), exist_ok=True)
    slug = f"{game_date.isoformat()}_00_top10-recap"
    shutil.copyfile(mp4, os.path.join(QUEUE_DIR, "pending", f"{slug}.mp4"))
    text = recap_caption(top, game_date, games, credit)
    if photo_credits:
        text = text.replace("\n\n#NBA", "\n📸 " + " · ".join(photo_credits) + "\n\n#NBA", 1)
    text = text.replace("Ranked by fantasy points (Yahoo default scoring)",
                        "Ranked by fantasy points (Yahoo default scoring) · shot chart for every player 🎯", 1)
    with open(os.path.join(QUEUE_DIR, "pending", f"{slug}.json"), "w", encoding="utf-8") as f:
        json.dump({"slug": slug, "video": f"{slug}.mp4", "series": "nightly_recap_reel_shots",
                   "game_date": game_date.isoformat(), "duration": duration,
                   "players": [f"#{p['rank']} {p['player_name']}" for p in top],
                   "audio": os.path.basename(audio) if audio else None, "credit": credit,
                   "caption": text}, f, ensure_ascii=False, indent=2)
    print(f"Saved {mp4}")


if __name__ == "__main__":
    main()
