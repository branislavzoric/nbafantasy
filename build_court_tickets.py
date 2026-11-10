#!/usr/bin/env python3
"""
build_court_tickets.py

Draws each NBA prop ticket as a basketball court: one player per leg (ESPN headshot from headshots/<id>.png)
spread over a chalk half court, with his bet next to him, and the ticket's total odds at the bottom.
Bookmakers are never shown on the image (the book is kept in the JSON for settling).

Input: Odds/tonight/<date>/court_tickets.json
  {"date", "tickets": [{"name", "book", "legs": [{"player", "espn_id", "team", "opp", "market", "side", "line",
                                                  "odds", "proj", "last5", "hit", "note"}]}]}
Run after nba_prop_analysis.py:
  python build_court_tickets.py [YYYY-MM-DD]
Output: Odds/tonight/<date>/court_tickets/ticket_<n>.png (1080x1350)
"""

import datetime as dt
import glob
import html
import json
import math
import os
import sys

from build_player_cards import file_url, find_browser, font_css, make_dir, render_png

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
W, H = 1080, 1350
MARKET = {"Points": "PTS", "Rebounds": "REB", "Assists": "AST", "3PM": "3PM", "Steals": "STL", "Blocks": "BLK",
          "Turnovers": "TOV", "Pts+Reb": "PTS+REB", "Pts+Ast": "PTS+AST", "Reb+Ast": "REB+AST",
          "Pts+Reb+Ast": "PTS+REB+AST"}


def team_colors():
    """ESPN team colours from any saved box score: {abbreviation: '#rrggbb'}."""
    out = {}
    for path in sorted(glob.glob(os.path.join(PROJECT_DIR, "Box Scores", "20*", "*.json")))[-40:]:
        try:
            box = json.load(open(path, encoding="utf-8"))["boxscore"]["teams"]
        except Exception:
            continue
        for t in box:
            out[t["team"]["abbreviation"]] = "#" + t["team"].get("color", "888888")
    return out


def court_svg():
    """Chalk half court seen from above, basket at the top."""
    c = 'stroke="rgba(236,240,232,.55)" stroke-width="5" fill="none" stroke-linecap="round"'
    return f'''<svg class="court" viewBox="0 0 1000 1080" preserveAspectRatio="none">
  <rect x="20" y="20" width="960" height="1040" rx="6" {c}/>
  <rect x="340" y="20" width="320" height="380" {c}/>
  <circle cx="500" cy="400" r="120" {c}/>
  <path d="M380 400 A120 120 0 0 0 620 400" {c} stroke-dasharray="14 14"/>
  <line x1="440" y1="80" x2="560" y2="80" {c}/>
  <circle cx="500" cy="105" r="22" stroke="#ff6b9a" stroke-width="5" fill="none"/>
  <path d="M440 105 A60 60 0 0 0 560 105" {c}/>
  <path d="M80 20 L80 160 A440 440 0 0 0 920 160 L920 20" {c}/>
  <path d="M380 1060 A120 120 0 0 1 620 1060" {c}/>
</svg>'''


def leg_html(leg, i, n, colors):
    left = i % 2 == 0
    top = 250 + (i * (860 / max(n - 1, 1)) if n > 1 else 430)
    color = colors.get(leg["team"], "#86e3a6")
    photo = os.path.join(PROJECT_DIR, "headshots", f'{leg["espn_id"]}.png')
    img = f'<img src="{html.escape(file_url(photo))}" alt="">' if os.path.exists(photo) else ""
    side = "OVER" if leg["side"].lower() == "over" else "UNDER"
    e = html.escape
    return f'''<div class="slot {'l' if left else 'r'}" style="top:{top:.0f}px">
  <div class="disc" style="--team:{color}">{img}<span class="tm">{e(leg["team"])}</span></div>
  <div class="bet">
    <div class="who">{e(leg["player"])} <small>vs {e(leg["opp"] or "")}</small></div>
    <div class="pick"><b class="{side.lower()}">{side} {leg["line"]:g}</b> {MARKET.get(leg["market"], leg["market"])}<span class="odd">{leg["odds"]:.2f}</span></div>
    <div class="why">proj {leg["proj"]:g} · last 5: {e(leg["last5"])} · {e(leg["note"])}</div>
  </div>
</div>'''


def ticket_html(t, n_ticket, day, colors):
    legs = t["legs"]
    total = math.prod(l["odds"] for l in legs)
    chance = math.prod(l["p"] for l in legs)
    logo = os.path.join(PROJECT_DIR, "brand", "b3_logo_ball.png")
    d = dt.date.fromisoformat(day).strftime("%a %d %b").upper()
    slots = "".join(leg_html(l, i, len(legs), colors) for i, l in enumerate(legs))
    return f'''<style>{font_css()}
*{{box-sizing:border-box;margin:0}}
body{{width:{W}px;height:{H}px;background:#1f2622;color:#ecf0e8;font-family:'Barlow Condensed',sans-serif;
  overflow:hidden;position:relative}}
body:before{{content:"";position:absolute;inset:0;background:radial-gradient(circle at 50% 35%,rgba(134,227,166,.08),transparent 60%)}}
.court{{position:absolute;left:40px;top:150px;width:1000px;height:1080px}}
header{{position:absolute;left:40px;right:40px;top:30px;height:100px;display:flex;align-items:center;gap:22px}}
header img{{height:96px}}
header h1{{font-size:58px;font-weight:700;letter-spacing:1px;line-height:1}}
header h1 em{{font-style:normal;color:#ff6b9a}}
header p{{font-size:26px;color:#86e3a6;letter-spacing:2px;margin-top:4px}}
.slot{{position:absolute;display:flex;align-items:center;gap:18px;transform:translateY(-50%)}}
.slot.l{{left:60px}} .slot.r{{right:60px;flex-direction:row-reverse}}
.disc{{width:176px;height:176px;border-radius:50%;border:7px solid var(--team);background:#2b342f;
  position:relative;flex:none;box-shadow:0 0 0 4px #1f2622,0 10px 30px rgba(0,0,0,.5)}}
.disc img{{width:100%;height:100%;object-fit:cover;object-position:50% 15%;border-radius:50%}}
.disc .tm{{position:absolute;bottom:-14px;left:50%;transform:translateX(-50%);background:var(--team);
  font-size:22px;font-weight:700;padding:1px 12px;border-radius:12px;border:3px solid #1f2622}}
.bet{{background:rgba(20,26,23,.92);border:3px solid rgba(236,240,232,.25);border-radius:18px;padding:12px 20px;
  width:auto;max-width:640px}}
.r .bet{{text-align:right}}
.who{{font-size:36px;font-weight:700;line-height:1.05}}
.who small{{font-size:24px;color:#9fb0a6;font-weight:600}}
.pick{{font-size:34px;font-weight:600;margin-top:2px}}
.pick b.over{{color:#86e3a6}} .pick b.under{{color:#ff6b9a}}
.odd{{background:#ecf0e8;color:#1f2622;border-radius:10px;padding:0 12px;margin-left:14px;font-weight:700}}
.why{{font-size:22px;color:#9fb0a6;margin-top:2px}}
footer{{position:absolute;left:40px;right:40px;bottom:26px;height:96px;display:flex;align-items:center;
  justify-content:space-between;border-top:3px dashed rgba(236,240,232,.3);padding-top:12px}}
footer .tot{{font-size:64px;font-weight:700}} footer .tot span{{font-size:30px;color:#9fb0a6;margin-right:12px}}
footer .meta{{text-align:right;font-size:24px;color:#9fb0a6;line-height:1.25}}
</style>
<header><img src="{html.escape(file_url(logo))}" alt=""><div>
  <h1>{html.escape(t["name"])} <em>· {len(legs)}-LEG TICKET</em></h1><p>NBA PLAYER PROPS · {d}</p></div></header>
{court_svg()}
{slots}
<footer><div class="tot"><span>TOTAL ODDS</span>{total:.2f}</div>
  <div class="meta">model win chance {chance * 100:.0f}%<br>b3basketball.com · 18+ · bet responsibly</div></footer>'''


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else dt.date.today().isoformat()
    base = os.path.join(PROJECT_DIR, "Odds", "tonight", day)
    spec = json.load(open(os.path.join(base, "court_tickets.json"), encoding="utf-8"))
    out = os.path.join(base, "court_tickets")
    os.makedirs(out, exist_ok=True)
    work = make_dir(out)
    colors, browser = team_colors(), find_browser()
    for n, t in enumerate(spec["tickets"], 1):
        png = os.path.join(out, f"ticket_{n}.png")
        render_png(browser, ticket_html(t, n, day, colors), png, work, W, H)
        print(png)


if __name__ == "__main__":
    main()
