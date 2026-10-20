#!/usr/bin/env python3
"""
build_highlight_reel.py

One player's game in a 1080x1920 "stat highlight" reel (no game footage, so it's ours to post):
  - intro: Commons photo, name, triple-double / double-double badge, final score
  - the line: points / rebounds / assists counting up, then shooting + extras
  - how it happened: up to 5 key moments from the NBA.com play-by-play (first bucket,
    best quarters, double-double / triple-double moments) with his running stat line
  - outro: the line, the result, b3basketball.com, credits

Data: nba_api (NBA.com box score + play-by-play). The photo must be freely licensed
(Wikimedia Commons, credits.json next to it, as written by commons_photos.py); by default
the newest News/*/photos/p_<lastname>.jpg is used.

Usage:
  python build_highlight_reel.py "Nikola Jokic"                    # his latest game, silent
  python build_highlight_reel.py "Nikola Jokic" --date 2026-10-11 --track Galactic_Rap
  python build_highlight_reel.py "Nikola Jokic" --photo "News/2026-10-13/photos/p_jokic.jpg"
Output: "Reels/highlights/<date>_<player>.mp4" (+ preview), queued in content/reels/pending
"""

import argparse
import datetime as dt
import glob
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata

from PIL import Image
from nba_api.stats.endpoints import boxscoretraditionalv3, leaguegamefinder, playbyplayv3
from nba_api.stats.static import players as static_players

from build_player_cards import TEAM_NAMES, file_url, find_browser, font_css, make_dir, render_png, team_vars
from build_team_reel import FFMPEG, FPS, encode, zoomed

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(PROJECT_DIR, "highlight_reel_template.html")
OUT_DIR = os.path.join(PROJECT_DIR, "Reels", "highlights")
QUEUE_DIR = os.path.join(PROJECT_DIR, "content", "reels")
TRACKS_PATH = os.path.join(PROJECT_DIR, "Reel Audio", "tracks.json")
LOGO_DIR = os.path.join(PROJECT_DIR, "logos")
B3_LOGO = r"C:\Users\Zoric\Desktop\b3basketball-site\public\logo.jpg"

W, H = 540, 960  # CSS px; rendered at 2x
INTRO_SECONDS, STATS_SECONDS, MOMENT_SECONDS, OUTRO_SECONDS = 3.0, 4.0, 2.8, 3.5
COUNT_STEPS = 8            # count-up renders on the stats slide
COUNT_SECONDS = 1.2
FADE_SECONDS = 0.3
MAX_MOMENTS = 5
SEASON_TYPES = ("Regular Season", "Playoffs", "PlayIn", "Pre Season")
GAME_KINDS = {"001": "Preseason", "002": "Regular season", "004": "Playoffs", "005": "Play-In"}
QUARTER_WORDS = {1: "first quarter", 2: "second quarter", 3: "third quarter", 4: "fourth quarter"}

# -----------------------------------------------------------------------------
# DATA
# -----------------------------------------------------------------------------

def ascii_key(text):
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower().strip()


def find_player(name):
    key = ascii_key(name)
    hits = [p for p in static_players.get_players() if ascii_key(p["full_name"]) == key]
    if not hits:
        raise SystemExit(f"No NBA player called {name!r}")
    return max(hits, key=lambda p: p["is_active"])


def season_of(day):
    y = day.year if day.month >= 9 else day.year - 1
    return f"{y}-{(y + 1) % 100:02d}"


def latest_game(pid, on_or_before):
    """(game_id, game_date) of the player's latest game on or before the date."""
    games = []
    for season in {season_of(on_or_before), season_of(on_or_before - dt.timedelta(days=200))}:
        for kind in SEASON_TYPES:
            df = leaguegamefinder.LeagueGameFinder(player_or_team_abbreviation="P", player_id_nullable=pid,
                                                   season_nullable=season, season_type_nullable=kind,
                                                   timeout=60).get_data_frames()[0]
            games += [(r.GAME_ID, dt.date.fromisoformat(r.GAME_DATE)) for r in df.itertuples()]
    games = [g for g in games if g[1] <= on_or_before]
    if not games:
        raise SystemExit("No game found")
    return max(games, key=lambda g: g[1])


def clock_seconds(clock):
    m = re.match(r"PT(\d+)M([\d.]+)S", clock or "")
    return int(m.group(1)) * 60 + float(m.group(2)) if m else 0.0


def clock_text(clock):
    s = clock_seconds(clock)
    return f"{int(s // 60)}:{int(s % 60):02d}"


def shot_text(a):
    sub, dist = (a.get("subType") or "").lower(), a.get("shotDistance") or 0
    if a.get("shotValue") == 3:
        return f"a {dist}-ft three" if dist else "a three"
    for word, text in (("dunk", "a dunk"), ("putback", "a putback"), ("tip", "a tip-in"), ("layup", "a layup"),
                       ("finger roll", "a finger roll"), ("hook", "a hook shot"), ("float", "a floater")):
        if word in sub:
            return text
    return f"a {dist}-ft jumper" if dist else "a jumper"


def game_story(box, pbp, pid):
    """Totals check + key moments from the play-by-play."""
    bs = box["boxScoreTraditional"]
    side = next(s for s in ("homeTeam", "awayTeam") if any(p["personId"] == pid for p in bs[s]["players"]))
    me = bs[side]
    opp = bs["awayTeam" if side == "homeTeam" else "homeTeam"]
    player = next(p for p in me["players"] if p["personId"] == pid)
    actions = pbp["game"]["actions"]
    pbp_name = next(a["playerName"] for a in actions if a.get("personId") == pid)
    # Descriptions spell names without diacritics ("(Jokic 1 AST)"), playerName keeps them
    ast_re = re.compile(rf"\({re.escape(ascii_key(pbp_name))} (\d+) ast\)")
    home = side == "homeTeam"

    tot = {"pts": 0, "reb": 0, "ast": 0}
    score = (0, 0)                    # (us, them)
    events, by_q = [], {}
    first_bucket = None
    for a in actions:
        if a.get("scoreHome") and a.get("scoreAway"):
            h, w = int(a["scoreHome"]), int(a["scoreAway"])
            score = (h, w) if home else (w, h)
        q = a["period"]
        qs = by_q.setdefault(q, {"pts": 0, "reb": 0, "ast": 0, "fgm": 0, "fga": 0, "helped": []})
        hit = None
        if a.get("personId") == pid:
            m = re.search(r"\((\d+) PTS\)", a["description"])
            if m and a["actionType"] in ("Made Shot", "Free Throw"):
                qs["pts"] += int(m.group(1)) - tot["pts"]
                tot["pts"] = int(m.group(1))
                hit = "pts"
                if first_bucket is None:
                    first_bucket = a
            if a["actionType"] in ("Made Shot", "Missed Shot"):
                qs["fga"] += 1
                qs["fgm"] += a["actionType"] == "Made Shot"
            m = re.search(r"\(Off:(\d+) Def:(\d+)\)", a["description"])
            if m and a["actionType"] == "Rebound":
                n = int(m.group(1)) + int(m.group(2))
                qs["reb"] += n - tot["reb"]
                tot["reb"] = n
                hit = "reb"
        elif a.get("teamId") == me["teamId"] and a["actionType"] == "Made Shot":
            m = ast_re.search(ascii_key(a["description"]))
            if m:
                qs["ast"] += int(m.group(1)) - tot["ast"]
                tot["ast"] = int(m.group(1))
                qs["helped"].append(a["playerName"])
                hit = "ast"
        if hit:
            events.append({"a": a, "stat": hit, "tot": dict(tot), "score": score})
        qs["end"] = {"tot": dict(tot), "score": score}

    st = player["statistics"]
    exact = (tot["pts"], tot["reb"], tot["ast"]) == (st["points"], st["reboundsTotal"], st["assists"])
    if not exact:
        print(f"  play-by-play totals {tot} != box score; tracker hidden")

    def lead(score):
        us, them = score
        word = "lead" if us > them else "trail"
        return f"{me['teamName']} {word} {us}–{them}" if us != them else f"Tied at {us}–{them}"

    moments = []

    def add(rank, q, clock, when, title, detail, tot, hit=None):
        moments.append({"rank": rank, "key": (q, -clock_seconds(clock)), "when": when, "title": title,
                        "detail": detail, "tot": tot, "hit": hit})

    # Double-double / triple-double: the event that took the 2nd / 3rd category to 10.
    tens = 0
    for e in events:
        n = sum(v >= 10 for v in e["tot"].values())
        if n > tens:
            tens = n
            if tens < 2:
                continue
            a, v = e["a"], e["tot"][e["stat"]]
            what = {"pts": f"Reaches {v} points", "reb": f"{v}th rebound", "ast": f"{v}th assist"}[e["stat"]]
            if e["stat"] == "ast":
                what += f": {a['playerName']} hits {shot_text(a)}"
            elif e["stat"] == "pts":
                what += f" on {shot_text(a)}" if a["actionType"] == "Made Shot" else " at the line"
            add(0, a["period"], a["clock"], f"Q{a['period']} · {clock_text(a['clock'])}",
                "Triple-double" if tens == 3 else "Double-double", f"{what} · {lead(e['score'])}", e["tot"], e["stat"])
    if first_bucket:
        a = first_bucket
        e = next(e for e in events if e["a"] is a)
        how = shot_text(a) if a["actionType"] == "Made Shot" else "a free throw"
        m = re.search(r"\((\w[\w' .-]*) \d+ AST\)", a["description"])
        add(2, a["period"], a["clock"], f"Q{a['period']} · {clock_text(a['clock'])}", "First bucket",
            how[0].upper() + how[1:] + (f", assisted by {m.group(1)}" if m else ""), e["tot"], "pts")
    regular = {q: s for q, s in by_q.items() if q <= 4}
    if regular:
        q, s = max(regular.items(), key=lambda kv: kv[1]["pts"])
        if s["pts"] >= 8:
            add(1, q, "PT00M00.00S", f"Q{q}", f"{s['pts']} points in Q{q}",
                f"{s['fgm']} of {s['fga']} from the field in the {QUARTER_WORDS[q]}", s["end"]["tot"], "pts")
        q, s = max(regular.items(), key=lambda kv: kv[1]["ast"])
        if s["ast"] >= 4:
            names = list(dict.fromkeys(s["helped"]))
            add(1, q, "PT00M00.00S", f"Q{q}", f"{s['ast']} assists in Q{q}",
                "Set up " + (", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else names[0]),
                s["end"]["tot"], "ast")
        q, s = max(regular.items(), key=lambda kv: kv[1]["reb"])
        if s["reb"] >= 6:
            add(3, q, "PT00M00.00S", f"Q{q}", f"{s['reb']} rebounds in Q{q}",
                f"Owned the glass in the {QUARTER_WORDS[q]}", s["end"]["tot"], "reb")
    moments = sorted(sorted(moments, key=lambda m: m["rank"])[:MAX_MOMENTS], key=lambda m: m["key"])

    return {"me": me, "opp": opp, "player": player, "stats": st, "moments": moments, "exact": exact,
            "home": home}

# -----------------------------------------------------------------------------
# HTML
# -----------------------------------------------------------------------------

def name_parts(player):
    last = player["familyName"]
    size = " xlong" if len(last) > 11 else " long" if len(last) > 8 else ""
    return player["firstName"], last, size


def badge(st):
    tens = sum(st[k] >= 10 for k in ("points", "reboundsTotal", "assists", "steals", "blocks"))
    return "Triple-double" if tens >= 3 else "Double-double" if tens == 2 else f"{st['points']} points"


def logo(tri):
    path = os.path.join(LOGO_DIR, f"{tri}.png")
    return f'<img src="{html.escape(file_url(path))}" alt="">' if os.path.exists(path) else "<span></span>"


def score_html(g):
    me, opp = g["me"], g["opp"]
    home, away = (me, opp) if g["home"] else (opp, me)
    hp, ap = home["statistics"]["points"], away["statistics"]["points"]
    return (f'<div class="score">{logo(away["teamTricode"])}'
            f'<div class="t{" lose" if ap < hp else ""}">{away["teamTricode"]}<b>{ap}</b></div>'
            f'<div class="mid">Final</div>'
            f'<div class="t away{" lose" if hp < ap else ""}">{home["teamTricode"]}<b>{hp}</b></div>'
            f'{logo(home["teamTricode"])}</div>')


def brand(text):
    return f'<div class="brand"><img src="{html.escape(file_url(B3_LOGO))}" alt=""><span>{html.escape(text)}</span></div>'


def intro_body(g, photo, credit, when):
    first, last, size = name_parts(g["player"])
    e = html.escape
    return (f'<div class="slide intro"><div class="photo" style="background-image:url(\'{e(file_url(photo))}\')"></div>'
            f'<div class="shade"></div><div class="credit">{e(credit)}</div>'
            f'<div class="text"><div class="kicker">{e(when)}</div><div class="first">{e(first)}</div>'
            f'<div class="last{size}">{e(last)}</div><div class="badge">{e(badge(g["stats"]))}</div></div>'
            f'{score_html(g)}</div>')


def result_text(g):
    me, opp = g["me"], g["opp"]
    us, them = me["statistics"]["points"], opp["statistics"]["points"]
    verb = "beat" if us > them else "fall to"
    return f"{me['teamName']} {verb} the {opp['teamName']} {max(us, them)}–{min(us, them)}"


def stats_body(g, k):
    st = g["stats"]
    rows = "".join(f'<div class="{"hit" if k >= 1 and st[key] >= 10 else ""}"><b>{round(st[key] * k)}</b>'
                   f'<span>{label}</span></div>'
                   for key, label in (("points", "Points"), ("reboundsTotal", "Rebounds"), ("assists", "Assists")))
    pm = st["plusMinusPoints"]
    cells = [(f"{st['fieldGoalsMade']}/{st['fieldGoalsAttempted']}", "FG"),
             (f"{st['threePointersMade']}/{st['threePointersAttempted']}", "3PT"),
             (f"{st['freeThrowsMade']}/{st['freeThrowsAttempted']}", "FT"),
             (str(st["steals"]), "STL"), (str(st["blocks"]), "BLK"), (f"{pm:+.0f}" if pm else "0", "+/-")]
    strip = "".join(f"<div><b>{v}</b><span>{lbl}</span></div>" for v, lbl in cells)
    mins = st["minutes"].split(":")[0]
    return (f'<div class="slide stats"><div class="hdr"><div class="kicker">The line</div>'
            f'<h2>{html.escape(g["player"]["familyName"])} vs. {html.escape(g["opp"]["teamName"])}</h2>'
            f'<div class="sub">{mins} minutes · {html.escape(result_text(g))}</div></div>'
            f'<div class="bigrows">{rows}</div>'
            f'<div class="strip" style="opacity:{max(0, min(1, (k - .6) / .4)):.2f}">{strip}</div>'
            f'{brand("Stats: NBA.com")}</div>')


def moments_body(g, step):
    e = html.escape
    items = []
    for i, m in enumerate(g["moments"]):
        state = "on" if i == step else "past" if i < step else "next"
        items.append(f'<li class="{state}"><div class="when">{e(m["when"])}</div>'
                     f'<div class="what"><b>{e(m["title"])}</b><span>{e(m["detail"])}</span></div></li>')
    m = g["moments"][step]
    tracker = ""
    if g["exact"]:
        cells = "".join(f'<div class="{"hit" if m["hit"] == k else ""}"><b>{m["tot"][k]}</b><span>{k.upper()}</span></div>'
                        for k in ("pts", "reb", "ast"))
        tracker = f'<div class="tracker"><div class="lbl">His line<br>so far</div>{cells}</div>'
    return (f'<div class="slide moments"><div class="hdr"><div class="kicker">How it happened</div>'
            f'<h2>{e(g["player"]["familyName"])}\'s night</h2></div>'
            f'<ol class="tl">{"".join(items)}</ol>{tracker}{brand("Play-by-play: NBA.com")}</div>')


def outro_body(g, photo, credit, music):
    st = g["stats"]
    e = html.escape
    credits = [f"Stats: NBA.com", f"Photo: {credit}"] + ([f"Music: {music}"] if music else [])
    return (f'<div class="slide outro"><div class="face" style="background-image:url(\'{e(file_url(photo))}\')"></div>'
            f'<div class="line">{st["points"]} <i>·</i> {st["reboundsTotal"]} <i>·</i> {st["assists"]}</div>'
            f'<div class="labels">PTS · REB · AST</div><div class="res">{e(result_text(g))}</div>'
            f'<div class="cta"><img src="{e(file_url(B3_LOGO))}" alt=""><div>More on <b>b3basketball.com</b></div></div>'
            f'<div class="credits">{"<br>".join(e(c) for c in credits)}</div></div>')


def page(template, team, body):
    return template.replace("{{TEAM_VARS}}", team_vars(team)).replace("{{BODY}}", body)

# -----------------------------------------------------------------------------
# PHOTO
# -----------------------------------------------------------------------------

def default_photo(player):
    key = re.sub(r"[^a-z]", "", ascii_key(player["familyName"]))
    found = sorted(glob.glob(os.path.join(PROJECT_DIR, "*News", "*", "photos", f"p_{key}.jpg")))
    return found[-1] if found else None


def photo_credit(photo):
    cred_file = os.path.join(os.path.dirname(photo), "credits.json")
    key = os.path.splitext(os.path.basename(photo))[0]
    credits = json.load(open(cred_file, encoding="utf-8")) if os.path.exists(cred_file) else {}
    c = credits.get(key)
    if not c:
        raise SystemExit(f"No credit for {photo} in credits.json -- only freely licensed photos (commons_photos.py)")
    return f"{c['author']} · {c['license']} · Wikimedia Commons"

# -----------------------------------------------------------------------------
# VIDEO
# -----------------------------------------------------------------------------

def frames(segments):
    """segments: [(image, seconds, zoom)]. Crossfade into each segment unless it's a count-up step."""
    fade = round(FADE_SECONDS * FPS)
    prev = None
    for img, seconds, zoom, cut in segments:
        n = round(seconds * FPS)
        for f in range(n):
            frame = zoomed(img, f / max(n - 1, 1)) if zoom else img
            if f < fade and prev is not None and not cut:
                frame = Image.blend(prev, frame, (f + 1) / (fade + 1))
            yield frame
        prev = zoomed(img, 1) if zoom else img


def short_credit(credit):
    """Music credit short enough for the outro slide (the full line goes in the caption)."""
    if not credit:
        return None
    short = credit.split(" Licensed")[0].strip(" ·")
    return short + (" · CC BY 4.0" if "Attribution 4.0" in credit else "")


def caption(g, when, credit, music):
    st, p = g["stats"], g["player"]
    lines = [f"🏀 {p['firstName']} {p['familyName']}: {st['points']} PTS · {st['reboundsTotal']} REB · "
             f"{st['assists']} AST" + (f" — {badge(st).lower()}" if badge(st)[0].isalpha() else ""),
             f"{result_text(g)} ({when})", ""]
    lines += [f"{m['when']}: {m['title']} — {m['detail']}" for m in g["moments"]]
    lines += ["", f"{st['fieldGoalsMade']}/{st['fieldGoalsAttempted']} FG in {st['minutes'].split(':')[0]} minutes",
              "", "More on b3basketball.com", "", f"Stats: NBA.com · Photo: {credit}"]
    if music:
        lines += [f"🎵 {music}"]
    tag = re.sub(r"[^A-Za-z]", "", ascii_key(p["familyName"]).title())
    lines += ["", f"#NBA #{g['me']['teamName']} #{tag} #{p['firstName']}{tag} #NBAHighlights"]
    return "\n".join(lines)[:2200]


def main():
    ap = argparse.ArgumentParser(description="Stat-highlight reel of one player's game.")
    ap.add_argument("player")
    ap.add_argument("--date", help="game date YYYY-MM-DD (default: his latest game)")
    ap.add_argument("--photo", help="freely licensed photo with a credits.json entry next to it")
    ap.add_argument("--audio", help="audio file you have the rights to use (default: silent)")
    ap.add_argument("--credit", help="music credit line")
    ap.add_argument("--track", help="track name from Reel Audio/tracks.json (sets --audio and --credit)")
    args = ap.parse_args()
    if args.track:
        with open(TRACKS_PATH, encoding="utf-8") as f:
            track = json.load(f)["tracks"][args.track]
        args.audio = os.path.join(os.path.dirname(TRACKS_PATH), track["file"])
        args.credit = track["credit"]
    browser = find_browser()
    if not browser:
        raise SystemExit("No Chrome/Edge found -- can't render.")

    info = find_player(args.player)
    pid = info["id"]
    day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    game_id, game_date = latest_game(pid, day)
    if args.date and game_date != day:
        raise SystemExit(f"{info['full_name']} didn't play on {day} (latest before: {game_date})")
    print(f"{info['full_name']}: game {game_id} on {game_date}")
    box = boxscoretraditionalv3.BoxScoreTraditionalV3(game_id=game_id, timeout=60).get_dict()
    pbp = playbyplayv3.PlayByPlayV3(game_id=game_id, timeout=60).get_dict()
    g = game_story(box, pbp, pid)
    team = g["me"]["teamTricode"]

    photo = os.path.abspath(args.photo) if args.photo else default_photo(g["player"])
    if not photo:
        raise SystemExit("No photo: pick one with commons_photos.py and pass --photo")
    credit = photo_credit(photo)
    when = f"{GAME_KINDS.get(game_id[:3], 'NBA')} · {game_date:%a, %b} {game_date.day}, {game_date.year}"

    with open(TEMPLATE, encoding="utf-8") as f:
        template = f.read().replace("{{FONTS}}", font_css())
    work_dir = make_dir(tempfile.gettempdir())
    try:
        def render(name, body):
            path = os.path.join(work_dir, f"{name}.png")
            render_png(browser, page(template, team, body), path, work_dir, W, H)
            return Image.open(path).convert("RGB")

        print("Rendering slides...")
        intro = render("intro", intro_body(g, photo, credit, when))
        counts = [render(f"stats_{i}", stats_body(g, i / COUNT_STEPS)) for i in range(COUNT_STEPS + 1)]
        steps = [render(f"moment_{i}", moments_body(g, i)) for i in range(len(g["moments"]))]
        outro = render("outro", outro_body(g, photo, credit, short_credit(args.credit)))
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    step_s = COUNT_SECONDS / COUNT_STEPS
    segments = [(intro, INTRO_SECONDS, True, False)]
    segments += [(img, step_s, False, i > 0) for i, img in enumerate(counts[:-1])]
    segments += [(counts[-1], STATS_SECONDS - COUNT_SECONDS, False, True)]
    segments += [(img, MOMENT_SECONDS, False, False) for img in steps]
    segments += [(outro, OUTRO_SECONDS, True, False)]
    duration = sum(s for _, s, _, _ in segments)

    os.makedirs(OUT_DIR, exist_ok=True)
    slug = f"{game_date.isoformat()}_{re.sub(r'[^a-z]+', '-', ascii_key(info['full_name']))}"
    mp4 = os.path.join(OUT_DIR, f"{slug}.mp4")
    print(f"Encoding {duration:.1f}s video...")
    encode(frames(segments), duration, mp4, args.audio)
    preview = mp4.replace(".mp4", "_preview.png")
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-ss", "0.5", "-i", mp4, "-frames:v", "1", preview], check=True)

    for folder in ("pending", "approved", "posted"):
        os.makedirs(os.path.join(QUEUE_DIR, folder), exist_ok=True)
    qslug = f"{dt.date.today().isoformat()}_00_{slug}-highlight"
    shutil.copyfile(mp4, os.path.join(QUEUE_DIR, "pending", f"{qslug}.mp4"))
    with open(os.path.join(QUEUE_DIR, "pending", f"{qslug}.json"), "w", encoding="utf-8") as f:
        json.dump({"slug": qslug, "video": f"{qslug}.mp4", "series": "player_highlight", "team": team,
                   "players": [info["full_name"]], "game_id": game_id, "game_date": game_date.isoformat(),
                   "duration": duration, "audio": os.path.basename(args.audio) if args.audio else None,
                   "credit": args.credit, "caption": caption(g, when, credit, args.credit)},
                  f, ensure_ascii=False, indent=2)
    print(f"Saved {mp4}")
    for m in g["moments"]:
        print(f"  {m['when']:12} {m['title']:22} {m['detail']}")


if __name__ == "__main__":
    main()
