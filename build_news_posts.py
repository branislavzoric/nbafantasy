#!/usr/bin/env python3
"""
build_news_posts.py

One simple Instagram post per news story (PNG, 1080x1350, news_post_template.html):
story photo or team colours on top with the players in front, then a label line,
big highlighted numbers and one line of context. Content comes from
"News/<date>/posts.json" (facts from news.json); story photos are freely licensed
Wikimedia Commons images in "News/<date>/photos/" with credits.json (credit goes in
the caption). Players are ESPN headshots: "crop": "face" shows only the face in a
team-colour ring (use it when the headshot shows an old team's jersey).

Usage (add --el for "EuroLeague News/<date>/"):
  python build_news_posts.py [date]
Output: "News/<date>/posts/NN_<slug>.png" + .json (caption)
"""

import datetime as dt
import html
import json
import os
import shutil
import sys
import tempfile

from build_depth_chart import team_logo
from build_nightly_cards import headshot
from build_player_cards import (DEFAULT_COLORS, TEAM_COLORS, file_url, find_browser, font_css, make_dir,
                                render_png)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(PROJECT_DIR, "news_post_template.html")
TIMELINE_TEMPLATE = os.path.join(PROJECT_DIR, "news_timeline_template.html")
HERO_W, HERO_H = 1080, 864
NEWS_DIR = os.path.join(PROJECT_DIR, "News")
HASHTAGS = "#NBA #NBANews #FantasyBasketball #NBAFantasy"


def fig_html(f, default_team):
    e = html.escape
    path = headshot(str(f["player"]))
    img = f'<img src="{e(file_url(path))}" alt="">' if path else ""
    team = f.get("team") or default_team
    ring = TEAM_COLORS.get(team, DEFAULT_COLORS)[1] if team else "#c8102e"
    tag = ""
    if f.get("tag") or f.get("status"):
        status = f'<b>{e(f["status"])}</b>' if f.get("status") else ""
        tag = f'<div class="tag">{status}{e(f.get("tag", ""))}</div>'
    if f.get("crop") == "face":
        return f'<div class="fig face"><div class="ring" style="--rc:{ring}">{img}</div>{tag}</div>'
    return f'<div class="fig full">{img}{tag}</div>'


def stats_html(stats):
    e = html.escape
    if not stats:
        return ""
    items = "".join(f'<div class="stat"><b{(' class="quote long"' if len(s[0]) > 16 else " class=quote") if len(s) > 2 and s[2] == "quote" else ""}>{e(s[0])}</b>'
                    + (f'<span>{e(s[1])}</span>' if s[1] else "") + "</div>" for s in stats)
    return f'<div class="stats{" one" if len(stats) == 1 else ""}">{items}</div>'


def crop_photo(photos_dir, spec, work_dir):
    """Crop box [x, y, width] (source px) to the hero's 5:4 frame -> temp JPEG path."""
    from PIL import Image
    x, y, w = spec["box"]
    im = Image.open(os.path.join(photos_dir, spec["file"])).convert("RGB")
    out = im.crop((x, y, x + w, y + round(w * HERO_H / HERO_W))).resize((HERO_W, HERO_H), Image.LANCZOS)
    path = os.path.join(work_dir, "hero_" + spec["file"])
    out.save(path, quality=92)
    return path


def post_html(p, template, photos_dir, work_dir):
    e = html.escape
    c1, c2 = p.get("colors") or TEAM_COLORS.get(p["team"], DEFAULT_COLORS)
    bg = ""
    if p.get("photo"):
        bg = f'<img class="bg photo" src="{e(file_url(crop_photo(photos_dir, p["photo"], work_dir)))}" alt="">'
    elif p.get("bg"):
        bg = f'<img class="bg" src="{e(file_url(os.path.join(photos_dir, p["bg"] + ".jpg")))}" alt="">'
    figs = p.get("figs", [])
    figs_html = (f'<div class="figs n{len(figs)}">{"".join(fig_html(f, p.get("team")) for f in figs)}</div>'
                 if figs else "")
    if p.get("logo"):
        logo = team_logo(p["logo"])
        figs_html += f'<img class="logo" src="{e(file_url(logo))}" alt="">' if logo else ""
    return (template
            .replace("{{TITLE}}", e(p["label"]))
            .replace("{{VARS}}", f"--c1: {c1}; --c2: {c2};" + ("" if figs or p.get("logo") else " --bgb: .85;") + (" --bgb: .95;" if p.get("photo") else ""))
            .replace("{{TEXT_CLASS}}", "" if p["stats"] else " nostats")
            .replace("{{BG}}", bg)
            .replace("{{FIGS}}", figs_html)
            .replace("{{KICKER}}", e(p["kicker"]))
            .replace("{{DATE}}", e(p["date"]))
            .replace("{{LABEL}}", e(p["label"]))
            .replace("{{STATS}}", stats_html(p["stats"]))
            .replace("{{CONTEXT}}", e(p["context"]))
            .replace("{{SOURCE}}", "Source: " + e(p["source"])))


def timeline_html(p, template, photos_dir):
    """Return timetable: one bar per player from injury date to expected return."""
    e = html.escape
    start, end = (dt.date.fromisoformat(d) for d in p["axis"])
    span = (end - start).days
    pct = lambda d: 100 * ((dt.date.fromisoformat(d) if isinstance(d, str) else d) - start).days / span
    mark_day, mark_label = p["marker"]
    rows = []
    for r in p["rows"]:
        team = r["team"]
        dark, accent = TEAM_COLORS.get(team, DEFAULT_COLORS)
        path = os.path.join(photos_dir, r["photo"]) if r.get("photo") else ""
        ini = "".join(w[0] for w in r["name"].split()[:2])
        face = (f'<img src="{e(file_url(path))}" alt="">' if path and os.path.exists(path)
                else f'<span class="ini">{e(ini)}</span>')
        x0 = pct(r["hurt"])
        if r["back"]:
            x1 = pct(r["back"])
            bar = (f'<i class="bar" style="left:{x0:.1f}%;width:{x1 - x0:.1f}%"></i>'
                   f'<i class="dot" style="left:{x1:.1f}%"></i>')
            when = dt.date.fromisoformat(r["back"]).strftime("%b %-d") if os.name != "nt" else                 dt.date.fromisoformat(r["back"]).strftime("%b %#d")
            status = f'{e(r["status"])}<small>earliest return ~{when}</small>'
        else:
            bar = f'<i class="bar season" style="left:{x0:.1f}%;width:{100 - x0 - 2:.1f}%"></i>'
            status = f'{e(r["status"])}<small>back in 2027-28</small>'
        line = (f'<i class="open-line" style="left:{pct(mark_day):.1f}%;top:-4px;height:34px"></i>')
        rows.append(f'<div class="row" style="--tc:{accent}"><div class="ph">{face}</div><div class="info">'
                    f'<div class="nm"><b>{e(r["name"])}</b><span>{e(team)}</span></div>'
                    f'<div class="inj">{e(r["injury"])}</div>'
                    f'<div class="track"><i class="rail"></i>{bar}{line}</div>'
                    f'<div class="status">{status}</div></div></div>')
    ticks, d = [], start
    while d <= end:
        if d.day in (1, 15):
            ticks.append(f'<span style="left:{pct(d):.1f}%">{d.strftime("%b")} {d.day}</span>')
        d += dt.timedelta(days=1)
    ticks.append(f'<b class="open-tag" style="left:{pct(mark_day):.1f}%;top:18px">{e(mark_label)} · '
                 f'{dt.date.fromisoformat(mark_day).strftime("%b")} {dt.date.fromisoformat(mark_day).day}</b>')
    body = f'<div class="rows">{"".join(rows)}</div><div class="axis">{"".join(ticks)}</div>'
    return (template.replace("{{TITLE}}", e(p["label"])).replace("{{KICKER}}", e(p["kicker"]))
            .replace("{{DATE}}", e(p["date"])).replace("{{LABEL}}", e(p["label"]))
            .replace("{{SUB}}", "Preseason injuries · when they could be back")
            .replace("{{BODY}}", body).replace("{{CONTEXT}}", e(p["context"]))
            .replace("{{SOURCE}}", "Source: " + e(p["source"])))


def credit_line(c):
    return (f"\"{c['file'].removeprefix('File:').rsplit('.', 1)[0]}\" by {c['author']}, "
            f"{c['license']} (Wikimedia Commons)")


def caption(p, story, credits):
    lines = [f"🏀 {story['headline']}", "", story["summary"]]
    if story.get("fantasy_angle"):
        lines += ["", f"📊 Fantasy angle: {story['fantasy_angle']}"]
    lines += ["", f"Source: {p['source']}"]
    for c in credits:
        lines.append(f"📸 {credit_line(c)}")
    lines += ["", HASHTAGS]
    return "\n".join(lines)[:2200]


def main():
    global HASHTAGS
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    news_dir = NEWS_DIR
    if "--el" in sys.argv:                     # EuroLeague stories: "EuroLeague News/<date>/", logo "el/<code>"
        news_dir = os.path.join(PROJECT_DIR, "EuroLeague News")
        HASHTAGS = "#EuroLeague #Basketball #EuroLeagueNews #B3Basketball"
    day = args[0] if args else dt.date.today().isoformat()
    folder = os.path.join(news_dir, day)
    photos_dir = os.path.join(folder, "photos")
    with open(os.path.join(folder, "posts.json"), encoding="utf-8") as f:
        posts = json.load(f)["posts"]
    with open(os.path.join(folder, "news.json"), encoding="utf-8") as f:
        stories = {s["id"]: s for s in json.load(f)["stories"]}
    credits = {}
    if os.path.exists(os.path.join(photos_dir, "credits.json")):
        with open(os.path.join(photos_dir, "credits.json"), encoding="utf-8") as f:
            credits = json.load(f)
    browser = find_browser()
    if not browser:
        raise SystemExit("No Chrome/Edge found -- can't render.")
    with open(TEMPLATE, encoding="utf-8") as f:
        template = f.read().replace("{{FONTS}}", font_css())
    with open(TIMELINE_TEMPLATE, encoding="utf-8") as f:
        timeline_t = f.read().replace("{{FONTS}}", font_css())
    out_dir = os.path.join(folder, "posts")
    os.makedirs(out_dir, exist_ok=True)
    work_dir = make_dir(tempfile.gettempdir())
    try:
        for p in posts:
            name = f"{p['id']:02d}_{p['slug']}"
            png = os.path.join(out_dir, name + ".png")
            page = (timeline_html(p, timeline_t, photos_dir) if p.get("layout") == "timeline"
                    else post_html(p, template, photos_dir, work_dir))
            render_png(browser, page, png, work_dir, 540, 675)
            keys = ([p["photo"]["file"].rsplit(".", 1)[0].removesuffix("_hi")] if p.get("photo") else
                    [p["bg"]] if p.get("bg") else
                    [r["photo"].rsplit(".", 1)[0] for r in p.get("rows", []) if r.get("photo")])
            with open(os.path.join(out_dir, name + ".json"), "w", encoding="utf-8") as f:
                json.dump({"slug": name, "image": name + ".png", "story_id": p["id"],
                           "caption": caption(p, stories[p["id"]], [credits[k] for k in keys if k in credits])},
                          f, ensure_ascii=False, indent=2)
            print(png)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
