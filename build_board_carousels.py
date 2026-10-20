#!/usr/bin/env python3
"""
build_board_carousels.py

Instagram carousels (1080x1350) of the four draft boards made by build_more_draft_boards.py:
cover + one slide per two rounds (rounds 1-2 ... 11-12, then 13 with the method note) = 8 slides per board.
Each row: overall pick, snake slot, jersey name, position/team (position colour bar), and FP + FP/G (projection boards)
or Yahoo/ESPN/average ADP (ADP board).

  python build_board_carousels.py [--approve] [board ...]       (default boards: yahoo espn fantasypros adp)

Output: "ADP draft order/carousel_<board>/NN_*.png" and content/carousels/<pending|approved>/<date>_boards_<n>_<board>.json
with the PNGs copied next to it (the format the n8n carousel workflow nbaCarouselWf001 posts).
"""

import csv
import datetime as dt
import html
import json
import os
import shutil
import subprocess
import sys

from build_draft_board import TEAMS, column_for, forward

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DRAFT_DIR = os.path.join(PROJECT_DIR, "ADP draft order")
CONTENT = os.path.join(PROJECT_DIR, "content", "carousels")
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
SITE = "b3basketball.com/fantasy"

SCORING = ("FGM +1 · FGA −0.45 · FTM +1 · FTA −0.75 · PTS/REB/AST +1 · STL/BLK +1.25 · TO/PF −1 · "
           "DD +2.5 · TD +5 · TECH −2.5 · EJ −5")
BOARDS = {
    "yahoo": {"title": "Yahoo", "kind": "Projections", "path": "/yahoo",
              "src": "Yahoo 2026-27 projections (Oct 16)"},
    "espn": {"title": "ESPN", "kind": "Projections", "path": "",
             "src": "ESPN 2026-27 projections"},
    "fantasypros": {"title": "FantasyPros", "kind": "Projections", "path": "/fantasypros",
                    "src": "FantasyPros 2026-27 projections"},
    "adp": {"title": "ADP", "kind": "Draft position", "path": "/adp",
            "src": "FantasyPros consensus ADP (Yahoo + ESPN drafts)"},
}
POS = {"PG": "#f4a3ad", "SG": "#f6c296", "SF": "#95dccb", "PF": "#b9a1ec", "C": "#cdc8f6", "NA": "#d9dbe2"}

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{width:1080px;height:1350px;background:#0b0b0d;color:#fff;font-family:Roboto,Arial,sans-serif;overflow:hidden;position:relative}
.d{font-family:'Barlow Condensed','Arial Narrow',sans-serif;text-transform:uppercase}
.top{position:absolute;left:60px;right:60px;top:52px;display:flex;justify-content:space-between;align-items:flex-end;border-bottom:4px solid #ff4f8b;padding-bottom:18px}
.top h1{font:700 64px/0.95 'Barlow Condensed';text-transform:uppercase;letter-spacing:.01em}
.top h1 span{color:#ff4f8b}
.top .rd{font:700 30px/1 'Barlow Condensed';text-transform:uppercase;color:#84e2a6;letter-spacing:.06em;text-align:right}
.top .rd small{display:block;color:rgba(255,255,255,.55);font-size:20px;margin-top:6px;letter-spacing:.08em}
.rounds{position:absolute;left:60px;right:60px;top:200px;bottom:96px;display:flex;flex-direction:column;gap:22px}
.round{flex:1;display:flex;flex-direction:column;min-height:0}
.rh{font:700 26px/1 'Barlow Condensed';text-transform:uppercase;letter-spacing:.1em;color:rgba(255,255,255,.6);margin-bottom:8px;display:flex;justify-content:space-between}
.rh b{color:#fff}
.rows{flex:1;min-height:0;display:grid;grid-template-rows:repeat(12,minmax(0,1fr));gap:4px}
.r{display:grid;grid-template-columns:62px 46px 1fr 246px;min-height:0;align-items:center;background:#18181c;border-radius:6px;overflow:hidden;position:relative}
.r::before{content:"";position:absolute;left:0;top:0;bottom:0;width:8px;background:var(--c)}
.pk{font:700 26px/1 'Barlow Condensed';text-align:center;padding-left:8px}
.sl{font:700 20px/1 'Barlow Condensed';color:rgba(255,255,255,.5);text-align:center}
.nm{min-width:0;display:flex;align-items:baseline;gap:12px;overflow:hidden}
.nm b{font:700 27px/1 'Barlow Condensed';text-transform:uppercase;letter-spacing:.02em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.nm span{font:500 15px/1 Roboto;color:rgba(255,255,255,.6);display:flex;gap:6px;align-items:center;white-space:nowrap;flex:none}
.nm i{font-style:normal;font-weight:800;font-size:12px;background:#b3263a;color:#fff;border-radius:3px;padding:1px 4px}
.st,.cols{display:grid;grid-template-columns:repeat(3,1fr);padding-right:10px}
.st div{text-align:center}
.st b{display:block;font:700 26px/1 'Barlow Condensed'}
.cols{width:246px;font:700 17px/1 'Barlow Condensed';letter-spacing:.1em;color:rgba(255,255,255,.5)}
.cols span{text-align:center}
.rh .lab{display:flex;gap:18px;align-items:baseline}
.st .hi b{color:#84e2a6}
.foot{position:absolute;left:60px;right:60px;bottom:40px;display:flex;justify-content:space-between;font:600 18px/1 Roboto;color:rgba(255,255,255,.5)}
.foot b{color:#ff4f8b;font-weight:700}
.note{font:400 19px/1.45 Roboto;color:rgba(255,255,255,.75);background:#18181c;border-left:6px solid #84e2a6;padding:18px 22px;margin-top:6px}
.note b{color:#fff}
/* cover */
.cv{position:absolute;inset:0;padding:90px 70px;display:flex;flex-direction:column}
.cv .k{font:700 30px/1 'Barlow Condensed';text-transform:uppercase;letter-spacing:.2em;color:#84e2a6}
.cv h1{font:700 190px/.86 'Barlow Condensed';text-transform:uppercase;margin-top:26px}
.cv h1 span{color:#ff4f8b;display:block}
.cv h2{font:700 54px/1 'Barlow Condensed';text-transform:uppercase;margin-top:24px;letter-spacing:.02em}
.cv .pod{margin-top:auto;display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
.cv .pod div{min-width:0;background:#18181c;border-top:8px solid var(--c);padding:18px 18px 16px;border-radius:6px}
.cv .pod small{font:700 22px/1 'Barlow Condensed';color:rgba(255,255,255,.5);letter-spacing:.1em}
.cv .pod b{display:block;font:700 36px/1 'Barlow Condensed';text-transform:uppercase;margin:6px 0 8px;overflow-wrap:anywhere}
.cv .pod span{font:500 18px/1 Roboto;color:rgba(255,255,255,.65)}
.cv .meta{margin-top:28px;font:400 21px/1.5 Roboto;color:rgba(255,255,255,.7)}
.cv .meta b{color:#fff}
.cv .sw{margin-top:26px;display:flex;justify-content:space-between;font:700 26px/1 'Barlow Condensed';text-transform:uppercase;letter-spacing:.08em}
.cv .sw span{color:#ff4f8b}
"""
HEAD = ('<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
        'family=Barlow+Condensed:wght@600;700&family=Roboto:wght@400;500;600;700;800&display=swap"><style>' + CSS + '</style>')


def load(name):
    with open(os.path.join(DRAFT_DIR, f"{name}_draft_order.csv"), encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def stats(p, adp):
    if adp:
        cells = [(p["Yahoo ADP"], "YAHOO", ""), (p["ESPN ADP"], "ESPN", ""), (p["ADP"], "AVG", "hi")]
    else:
        cells = [(f'{float(p["Total FP"]):.0f}', "FP", "hi"), (f'{float(p["FP/G"]):.1f}', "FP/G", ""), (p["GP"], "GP", "")]
    return "".join(f'<div class="{c}"><b>{html.escape(v)}</b></div>' for v, l, c in cells)


def row(p, adp):
    e = html.escape
    rank = int(p["Rank"])
    inj = f'<i>{e(p["Status"])}</i>' if p["Status"] else ""
    color = POS.get(p.get("Color") or "NA", POS["NA"])
    return (f'<div class="r" style="--c:{color}"><div class="pk">{rank}</div>'
            f'<div class="sl">T{column_for(rank)}</div>'
            f'<div class="nm"><b>{e(p["Player"])}</b><span>{e(p["Pos"])} · {e(p["Team"])}{inj}</span></div>'
            f'<div class="st">{stats(p, adp)}</div></div>')


def round_block(picks, r, adp):
    rp = sorted((p for p in picks if int(p["Round"]) == r), key=lambda p: int(p["Rank"]))
    direction = "teams 1 → 12" if forward(r) else "teams 12 → 1"
    labels = ("YAHOO", "ESPN", "AVG") if adp else ("FP", "FP/G", "GP")
    cols = "".join(f"<span>{l}</span>" for l in labels)
    return (f'<div class="round"><div class="rh"><span class="lab"><b>Round {r}</b><span>picks {rp[0]["Rank"]}–{rp[-1]["Rank"]} · {direction}</span></span>'
            f'<span class="cols">{cols}</span></div>'
            f'<div class="rows">{"".join(row(p, adp) for p in rp)}</div></div>')


def note(name):
    if name == "adp":
        return ('<div class="note"><b>How to read it:</b> average draft position in real Yahoo and ESPN drafts, '
                'collected by FantasyPros. AVG is the mean of the two; a dash means that site has no ADP for the player. '
                'Snake with third-round reversal: round 1 runs team 1 → 12, rounds 2-3 run 12 → 1, then it alternates.</div>')
    return ('<div class="note"><b>Scoring:</b> ' + SCORING + '. No site projects double-doubles, triple-doubles, '
            'technicals or ejections, so those use each player\'s 2025-26 per-game rate × projected games '
            '(rookies get none). Snake with third-round reversal: round 1 runs team 1 → 12, rounds 2-3 run 12 → 1, then it alternates.</div>')


def page(name, inner, label="", sub=""):
    b = BOARDS[name]
    top = (f'<div class="top"><h1>{b["title"]} <span>draft board</span></h1>'
           f'<div class="rd">{label}<small>{sub}</small></div></div>') if label else ""
    foot = f'<div class="foot"><span>Top 156 · 12-team snake · 3rd-round reversal</span><span><b>{SITE}{b["path"]}</b></span></div>'
    return HEAD + top + inner + foot


def cover(name, picks):
    b, adp = BOARDS[name], name == "adp"
    pod = ""
    for p in picks[:3]:
        val = f'ADP {p["ADP"]}' if adp else f'{float(p["Total FP"]):.0f} FP · {float(p["FP/G"]):.1f}/G'
        pod += (f'<div style="--c:{POS.get(p.get("Color") or "NA")}"><small>#{p["Rank"]} · {html.escape(p["Player"].split()[0])}</small>'
                f'<b>{html.escape(" ".join(p["Player"].split()[1:]) or p["Player"])}</b><span>{val}</span></div>')
    meta = (f'<b>Source:</b> {b["src"]}.<br>' +
            ('<b>Ranked by</b> average draft position.' if adp else
             '<b>Ranked by</b> projected season fantasy points, points-league scoring incl. double-doubles, '
             'triple-doubles and technicals.'))
    return (HEAD + f'<div class="cv"><div class="k">Fantasy basketball 2026-27</div>'
            f'<h1>{b["title"]}<span>draft board</span></h1><h2>Top 156 · 12-team snake · 13 rounds</h2>'
            f'<div class="pod">{pod}</div><div class="meta">{meta}</div>'
            f'<div class="sw"><span>{SITE}{b["path"]}</span>Swipe for every round →</div></div>')


def render(html_text, png):
    tmp = png.replace(".png", ".html")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html_text)
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
                    "--virtual-time-budget=6000", f"--screenshot={png}", "--window-size=1080,1350",
                    "file:///" + tmp.replace("\\", "/")], check=True, capture_output=True)
    os.remove(tmp)


def caption(name, picks):
    b, adp = BOARDS[name], name == "adp"
    top = "\n".join(f'{p["Rank"]}. {p["Player"]} ({p["Team"]})' + ("" if adp else f' {float(p["Total FP"]):.0f} FP')
                    for p in picks[:5])
    how = ("Ranked by average draft position in Yahoo and ESPN drafts (FantasyPros consensus)." if adp else
           f"Ranked by projected 2026-27 fantasy points from {b['src']}, points-league scoring: {SCORING}. "
           "Double-doubles, triple-doubles and technicals are projected from each player's 2025-26 per-game rate.")
    return (f"📋 {b['title']} draft board: the top 156 for 2026-27 as a 12-team snake draft, 13 rounds.\n\n"
            f"{how}\n\nTop 5:\n{top}\n\nSwipe for every round. Full board: {SITE}{b['path']}\n\n"
            "#fantasybasketball #nbafantasy #fantasydraft #draftboard #nba #yahoofantasy #espnfantasy")


def build(name, n, queue, date):
    picks = load(name)
    adp = name == "adp"
    out = os.path.join(DRAFT_DIR, f"carousel_{name}")
    os.makedirs(out, exist_ok=True)
    slides = [("01_cover.png", cover(name, picks))]
    for i, (a, b2) in enumerate([(1, 2), (3, 4), (5, 6), (7, 8), (9, 10), (11, 12)], start=2):
        inner = f'<div class="rounds">{round_block(picks, a, adp)}{round_block(picks, b2, adp)}</div>'
        slides.append((f"{i:02d}_rounds-{a}-{b2}.png", page(name, inner, f"Rounds {a}–{b2}", f"picks {(a - 1) * TEAMS + 1}–{b2 * TEAMS}")))
    inner = f'<div class="rounds">{round_block(picks, 13, adp)}<div style="flex:none">{note(name)}</div></div>'
    slides.append(("08_round-13.png", page(name, inner, "Round 13", "picks 145–156")))
    for fn, h in slides:
        render(h, os.path.join(out, fn))
    slug = f"{date}_boards_{n}_{name}"
    dst = os.path.join(CONTENT, queue)
    imgs = []
    for fn, _ in slides:
        shutil.copy(os.path.join(out, fn), os.path.join(dst, f"{slug}_{fn}"))
        imgs.append(f"{slug}_{fn}")
    with open(os.path.join(dst, f"{slug}.json"), "w", encoding="utf-8") as f:
        json.dump({"slug": slug, "images": imgs, "caption": caption(name, picks)}, f, ensure_ascii=False, indent=2)
    print(f"{name}: {len(imgs)} slides -> {queue}/{slug}.json")


def main():
    queue = "approved" if "--approve" in sys.argv else "pending"
    names = [a for a in sys.argv[1:] if not a.startswith("--")] or list(BOARDS)
    date = dt.date.today().isoformat()
    for n, name in enumerate(names, start=1):
        build(name, n, queue, date)


if __name__ == "__main__":
    main()
