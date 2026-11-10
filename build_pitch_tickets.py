#!/usr/bin/env python3
"""
build_pitch_tickets.py

Draws the football tickets from build_football_tickets.py as a tactics board: a chalk pitch where every leg is a
fixture card (both club crests, league, kick-off, the bet and its odds) and the legs are linked by dashed passing
lines in kick-off order, from the first leg in our half to a shot on goal. Bookmakers are never shown.

Themes (football_card.json ticket name -> title):
  safe4  THE BACK FOUR      safe5  MIDFIELD ENGINE
  long4  COUNTER-ATTACK     long5  TOP BINS
Club crests come from ESPN's soccer scoreboards (cached in logos/football/<espn id>.png); a club with no match
gets its initials.

Run after build_football_tickets.py:
  python build_pitch_tickets.py [YYYY-MM-DD]
Output: Odds/tonight/<date>/pitch_tickets/ticket_<name>.png (1080x1350) + pitch_tickets.json
"""

import datetime as dt
import html
import json
import math
import os
import re
import sys
import unicodedata

import requests

from build_player_cards import file_url, find_browser, font_css, make_dir, render_png

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CREST_DIR = os.path.join(PROJECT_DIR, "logos", "football")
W, H = 1080, 1350
ESPN_LEAGUES = {"Premier League": "eng.1", "LaLiga": "esp.1", "Serie A": "ita.1", "Bundesliga": "ger.1",
                "Ligue 1": "fra.1", "Eredivisie": "ned.1", "Primeira Liga": "por.1", "Belgian Pro League": "bel.1",
                "Champions League": "uefa.champions", "Europa League": "uefa.europa",
                "Conference League": "uefa.europa.conf", "Championship": "eng.2"}
THEMES = {"safe4": ("THE BACK FOUR", "Solid, low-risk legs: the safest 4-leg build-up of the day"),
          "safe5": ("MIDFIELD ENGINE", "Five short passes: the safest 5-leg ticket"),
          "long4": ("COUNTER-ATTACK", "Four quick passes for a big price: the best-value long shot"),
          "long5": ("TOP BINS", "Five legs aiming for the corner of the net: the 5-leg long shot")}
STOP = {"fc", "cf", "sc", "ac", "afc", "cd", "ud", "rc", "ssc", "as", "us", "sv", "vfl", "vfb", "tsg", "1", "bc",
        "kaa", "ksv", "sk", "rsc", "krc", "de", "club", "calcio", "olympique", "real", "sporting", "athletic"}


ALIAS = {"cologne": "koln", "koeln": "koln", "munich": "munchen", "turin": "torino", "milano": "milan",
         "lisbon": "lisboa", "rome": "roma", "naples": "napoli"}


def fold(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return [ALIAS.get(w, w) for w in re.sub(r"[^a-z0-9 ]", " ", s).split() if w]


def same_team(a, b):
    """Book name vs ESPN name: a shared distinctive word ("Hamburger" ~ "Hamburg SV", "Koln" ~ "FC Cologne")."""
    fa, fb = fold(a), fold(b)
    if fa == fb:
        return True
    ka, kb = set(fa) - STOP, set(fb) - STOP
    if ka & kb:
        return True
    return any(x[:5] == y[:5] and len(x) >= 5 and len(y) >= 5 for x in ka for y in kb)


def espn_teams(day, leagues):
    """[(league, home, away, home_logo, away_logo)] for the leagues on the tickets."""
    out = []
    for lg in leagues:
        code = ESPN_LEAGUES.get(lg)
        if not code:
            continue
        try:
            sb = requests.get(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{code}/scoreboard",
                              params={"dates": day.replace("-", "")}, timeout=30).json()
        except Exception:
            continue
        for ev in sb.get("events", []):
            side = {c["homeAway"]: c["team"] for c in ev["competitions"][0]["competitors"]}
            out.append((lg, side["home"], side["away"]))
    return out


def crest(team):
    """ESPN team dict -> local crest path (downloaded once)."""
    os.makedirs(CREST_DIR, exist_ok=True)
    path = os.path.join(CREST_DIR, f'{team["id"]}.png')
    if not os.path.exists(path) and team.get("logo"):
        r = requests.get(team["logo"], timeout=30)
        if r.ok:
            open(path, "wb").write(r.content)
    return path if os.path.exists(path) else None


def match_crests(leg, espn):
    home, away = leg["match"].split(" – ")
    for lg, h, a in espn:
        if lg == leg["league"] and same_team(home, h["displayName"]) and same_team(away, a["displayName"]):
            return crest(h), crest(a)
    return None, None


def badge(path, name):
    if path:
        return f'<img class="crest" src="{html.escape(file_url(path))}" alt="">'
    initials = "".join(w[0] for w in fold(name) if w not in STOP)[:3].upper() or name[:2].upper()
    return f'<span class="crest none">{html.escape(initials)}</span>'


def pitch_svg(segs):
    line = 'stroke="rgba(240,245,236,.6)" stroke-width="5" fill="none"'
    passes = "".join(f'<line x1="{x1:.0f}" y1="{y1:.0f}" x2="{x2:.0f}" y2="{y2:.0f}" class="pass"/>'
                     for x1, y1, x2, y2 in segs)
    return f'''<svg class="pitch" viewBox="0 0 1000 1060" preserveAspectRatio="none">
  <defs><marker id="ar" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto">
    <path d="M0 0 L10 5 L0 10 z" fill="#ff6b9a"/></marker></defs>
  <rect x="20" y="20" width="960" height="1020" rx="4" {line}/>
  <line x1="20" y1="1040" x2="980" y2="1040" {line}/>
  <rect x="230" y="20" width="540" height="190" {line}/>
  <rect x="370" y="20" width="260" height="70" {line}/>
  <rect x="420" y="2" width="160" height="18" stroke="#ecf0e8" stroke-width="5" fill="rgba(236,240,232,.15)"/>
  <circle cx="500" cy="150" r="5" fill="rgba(240,245,236,.7)"/>
  <path d="M410 210 A110 110 0 0 0 590 210" {line}/>
  <path d="M380 1040 A120 120 0 0 1 620 1040" {line}/>
  <circle cx="500" cy="1040" r="6" fill="rgba(240,245,236,.7)"/>
  <g stroke="#ff6b9a" stroke-width="6" stroke-dasharray="4 16" stroke-linecap="round" marker-end="url(#ar)">{passes}</g>
</svg>'''


def card_html(leg, x, y, side):
    home, away = leg["match"].split(" – ")
    ko = leg["kickoff"][11:16]
    e = html.escape
    return f'''<div class="card {side}" style="left:{x}px;top:{y}px">
  <div class="fix">{badge(leg.get("home_crest"), home)}<b>{e(home)}</b><i>vs</i><b>{e(away)}</b>{badge(leg.get("away_crest"), away)}</div>
  <div class="meta">{e(leg["league"])} · {ko}</div>
  <div class="bet"><span>{e(leg["bet"])}</span><em>{leg["odds"]:.2f}</em></div>
</div>'''


def ticket_html(t, day):
    title, sub = THEMES[t["name_key"]]
    legs = sorted(t["legs"], key=lambda l: l["kickoff"])
    n = len(legs)
    top, bottom = 300, 1110                       # card centres, page px: last leg nearest the goal
    cards, centres = [], []
    for i, leg in enumerate(legs):
        cy = bottom - i * (bottom - top) / max(n - 1, 1)
        cx = 360 if i % 2 == 0 else 720
        cards.append(card_html(leg, cx - 330, cy - 78, "l" if i % 2 == 0 else "r"))
        centres.append((cx, cy))
    # passes run in the gaps between cards: top edge of one card -> bottom edge of the next (pitch coords = page - offset)
    segs = []
    for (x1, y1), (x2, y2) in zip(centres, centres[1:]):
        segs.append(((x1 + x2) / 2 - 90 * (1 if x2 > x1 else -1) - 40, y1 - 84 - 150, (x1 + x2) / 2 + 90 * (1 if x2 > x1 else -1) - 40, y2 + 84 - 150))
    lx, ly = centres[-1]
    segs.append((lx - 40, ly - 84 - 150, 500, 30))     # the shot: into the goal
    logo = os.path.join(PROJECT_DIR, "brand", "b3_logo_ball.png")
    total = math.prod(l["odds"] for l in legs)
    d = dt.date.fromisoformat(day).strftime("%a %d %b").upper()
    return f'''<style>{font_css()}
*{{box-sizing:border-box;margin:0}}
body{{width:{W}px;height:{H}px;color:#ecf0e8;font-family:'Barlow Condensed',sans-serif;overflow:hidden;position:relative;
  background:#1f2622}}
.turf{{position:absolute;left:40px;top:150px;width:1000px;height:1060px;border-radius:6px;
  background:repeating-linear-gradient(180deg,#1f4a33 0 106px,#245539 106px 212px)}}
.pitch{{position:absolute;left:40px;top:150px;width:1000px;height:1060px}}
.pitch .pass{{stroke:#ff6b9a}}
header{{position:absolute;left:40px;right:40px;top:28px;height:100px;display:flex;align-items:center;gap:22px}}
header img{{height:96px}}
header h1{{font-size:58px;font-weight:700;line-height:1;letter-spacing:1px}} header h1 em{{font-style:normal;color:#ff6b9a}}
header p{{font-size:25px;color:#86e3a6;letter-spacing:2px;margin-top:4px}}
.card{{position:absolute;width:660px;height:156px;background:rgba(18,24,21,.93);border:3px solid rgba(236,240,232,.28);
  border-radius:18px;padding:12px 18px;box-shadow:0 10px 26px rgba(0,0,0,.45)}}
.fix{{display:flex;align-items:center;gap:10px;font-size:30px;font-weight:700;white-space:nowrap;overflow:hidden}}
.fix b{{overflow:hidden;text-overflow:ellipsis;max-width:220px}}
.fix i{{font-style:normal;color:#9fb0a6;font-size:22px;font-weight:600}}
.crest{{width:54px;height:54px;object-fit:contain;flex:none;filter:drop-shadow(0 2px 3px rgba(0,0,0,.6))}}
.crest.none{{display:inline-flex;align-items:center;justify-content:center;border-radius:50%;background:#33403a;
  font-size:20px;border:2px solid #86e3a6}}
.meta{{font-size:21px;color:#86e3a6;letter-spacing:1px;text-transform:uppercase;margin-top:2px}}
.bet{{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:4px;font-size:29px;font-weight:600}}
.bet span{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.bet em{{font-style:normal;background:#ecf0e8;color:#1f2622;border-radius:10px;padding:0 12px;font-weight:700;flex:none}}
.goal{{position:absolute;left:470px;top:120px;font-size:22px;font-weight:700;color:#ff6b9a;letter-spacing:3px}}
footer{{position:absolute;left:40px;right:40px;bottom:24px;height:100px;display:flex;align-items:center;justify-content:space-between}}
footer .tot{{font-size:64px;font-weight:700}} footer .tot span{{font-size:30px;color:#9fb0a6;margin-right:12px}}
footer .meta2{{text-align:right;font-size:23px;color:#9fb0a6;line-height:1.25;max-width:560px}}
</style>
<header><img src="{html.escape(file_url(logo))}" alt=""><div><h1>{html.escape(title)} <em>· {n} LEGS</em></h1>
  <p>FOOTBALL TICKET · {d}</p></div></header>
<div class="turf"></div>
{pitch_svg(segs)}
<div class="goal">GOAL</div>
{"".join(cards)}
<footer><div class="tot"><span>TOTAL ODDS</span>{total:.2f}</div>
  <div class="meta2">{html.escape(sub)}<br>win chance {t["hit_prob"] * 100:.0f}% · b3basketball.com · 18+</div></footer>'''


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else dt.date.today().isoformat()
    base = os.path.join(PROJECT_DIR, "Odds", "tonight", day)
    card = json.load(open(os.path.join(base, "football_card.json"), encoding="utf-8"))
    tickets = []
    for t in card["tickets"]:
        key = f'{t["name"]}{len(t["legs"])}'
        if key in THEMES:
            tickets.append({**t, "name_key": key, "title": THEMES[key][0]})
    espn = espn_teams(day, {l["league"] for t in tickets for l in t["legs"]})
    for t in tickets:
        for l in t["legs"]:
            l["home_crest"], l["away_crest"] = match_crests(l, espn)
            if not l["home_crest"]:
                print("  no crest:", l["league"], l["match"])
    out = os.path.join(base, "pitch_tickets")
    os.makedirs(out, exist_ok=True)
    work = make_dir(out)
    browser = find_browser()
    for t in tickets:
        png = os.path.join(out, f'ticket_{t["name_key"]}.png')
        render_png(browser, ticket_html(t, day), png, work, W, H)
        t["png"] = os.path.relpath(png, base)
        print(png)
    json.dump({"date": day, "tickets": tickets}, open(os.path.join(base, "pitch_tickets.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
