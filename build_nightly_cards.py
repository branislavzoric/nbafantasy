#!/usr/bin/env python3
"""
build_nightly_cards.py

Builds "Top 10 last night" cards (PNG, 1080x1350): the 10 best fantasy
performances of one game date, ranked by Yahoo default points (nba_boxscores.fpts).
Same look as the projection cards (build_player_cards.py), with the game line
instead of the projection.

Text: headline + recap come from "Nightly Text/<date>.json", keyed by ESPN player
id ({"headline": "text {highlighted} text", "summary": "..."}). Players missing
there get a plain rule-based headline/summary from the box score.

Run collect_boxscores.py first.

Output:
  - "Nightly Cards/<date>/<NN>_<name>.png"
  - content/nightly/pending/<date>_<NN>_<name>.png + .json (with Instagram caption)
    for review; move approved ones to content/nightly/approved for posting.

Usage:
  python build_nightly_cards.py              # yesterday (US Eastern)
  python build_nightly_cards.py 2026-10-05
"""

import datetime as dt
import html
import json
import os
import re
import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import requests
from sqlalchemy import create_engine, text

from build_player_cards import (CARD_H, CARD_W, HEADSHOT_DIR, TEAM_NAMES, file_url, find_browser,
                                font_css, headline_size, make_dir, render_png, slug, summary_size,
                                surname, team_vars)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(PROJECT_DIR, "nightly_card_template.html")
TEXT_DIR = os.path.join(PROJECT_DIR, "Nightly Text")
OUT_DIR = os.path.join(PROJECT_DIR, "Nightly Cards")
QUEUE_DIR = os.path.join(PROJECT_DIR, "content", "nightly")  # pending -> approved -> posted (n8n)
RAW_DIR = os.path.join(PROJECT_DIR, "Box Scores")
ET = ZoneInfo("America/New_York")

TOP_N = 10
WORKERS = 5
HEADSHOT_URL = "https://a.espncdn.com/i/headshots/nba/players/full/{player_id}.png"

# ESPN abbreviation -> the one build_player_cards uses for colours/names
TEAM_ALIASES = {"GS": "GSW", "NY": "NYK", "NO": "NOP", "SA": "SAS", "UTAH": "UTA", "WSH": "WAS"}

# -----------------------------------------------------------------------------
# DATA
# -----------------------------------------------------------------------------

def team_key(abbr):
    return TEAM_ALIASES.get(abbr, abbr)


def name_key(name):
    return slug(re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", "", name, flags=re.I))


def load_top(game_date):
    engine = create_engine(os.environ["DATABASE_URL"])
    query = text("""
        SELECT b.*, g.home_team, g.away_team, g.home_score, g.away_score
        FROM nba_boxscores b JOIN nba_games g USING (game_id)
        WHERE b.game_date = :d AND b.fpts IS NOT NULL
        ORDER BY b.fpts DESC, b.min ASC
        LIMIT :n
    """)
    with engine.connect() as conn:
        top = [dict(r._mapping) for r in conn.execute(query, {"d": game_date, "n": TOP_N})]
        yahoo = [dict(r._mapping) for r in conn.execute(text(
            "SELECT espn_player_id, full_name, yahoo_rank FROM yahoo_projections"))]
    # Yahoo rank by ESPN id, falling back to the name (some ids didn't match)
    by_id = {str(y["espn_player_id"]): y["yahoo_rank"] for y in yahoo if y["espn_player_id"]}
    by_name = {name_key(y["full_name"]): y["yahoo_rank"] for y in yahoo}
    for rank, p in enumerate(top, start=1):
        p["rank"] = rank
        p["yahoo_rank"] = by_id.get(p["player_id"]) or by_name.get(name_key(p["player_name"]))
        home = p["team"] == p["home_team"]
        own, opp = ((p["home_score"], p["away_score"]) if home else (p["away_score"], p["home_score"]))
        p.update(home=home, team_score=own, opp_score=opp, won=own > opp)
    return top


def jerseys(game_date):
    """ESPN player id -> jersey number, from the raw box score JSON."""
    out = {}
    folder = os.path.join(RAW_DIR, game_date.isoformat())
    for name in os.listdir(folder) if os.path.isdir(folder) else []:
        if not name[:1].isdigit():          # highlights/recaps/quotes.json live here too
            continue
        with open(os.path.join(folder, name), encoding="utf-8") as f:
            summary = json.load(f)
        for team in summary["boxscore"].get("players", []):
            for block in team["statistics"]:
                for a in block["athletes"]:
                    out[a["athlete"]["id"]] = a["athlete"].get("jersey") or ""
    return out


def headshot(player_id):
    """Local headshot path, downloading it from ESPN if we don't have it yet."""
    path = os.path.join(HEADSHOT_DIR, f"{player_id}.png")
    if not os.path.exists(path):
        r = requests.get(HEADSHOT_URL.format(player_id=player_id), timeout=30)
        if r.status_code != 200 or not r.content:
            return None
        with open(path, "wb") as f:
            f.write(r.content)
    return path

# -----------------------------------------------------------------------------
# TEXT
# -----------------------------------------------------------------------------

def load_text(game_date):
    path = os.path.join(TEXT_DIR, f"{game_date.isoformat()}.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def result_line(p):
    """'W 109-107' / 'L 103-127', from the player's team's side."""
    return f"{'W' if p['won'] else 'L'} {p['team_score']}-{p['opp_score']}"


def opponent_label(p):
    return f"{'vs' if p['home'] else '@'} {team_key(p['opponent'])}"


def fallback_headline(p):
    last = surname(p["player_name"])
    big = sorted((("pts", "points"), ("reb", "rebounds"), ("ast", "assists")), key=lambda s: -(p[s[0]] or 0))
    doubles = sum((p[s] or 0) >= 10 for s in ("pts", "reb", "ast", "stl", "blk"))
    if doubles >= 3:
        return f"{last} records a ", "triple-double", ""
    if doubles == 2:
        return f"{last} posts a ", "double-double", f" in {p['min']} minutes"
    return f"{last} piles up ", f"{p['fpts']:.1f} fantasy points", ""


def fallback_summary(p):
    team = TEAM_NAMES.get(team_key(p["team"]), p["team"])
    opp = TEAM_NAMES.get(team_key(p["opponent"]), p["opponent"])
    return (f"{p['player_name']} finished with {p['pts']} points, {p['reb']} rebounds and "
            f"{p['ast']} assists in {p['min']} minutes {'as a starter' if p['starter'] else 'off the bench'}, "
            f"adding {p['stl']} steals and {p['blk']} blocks. The {team.split()[-1]} "
            f"{'beat' if p['won'] else 'lost to'} the {opp} {max(p['team_score'], p['opp_score'])}-"
            f"{min(p['team_score'], p['opp_score'])}.")


def card_text(p, texts):
    """(before, highlighted, after, summary, written) -- written = from Nightly Text."""
    t = texts.get(p["player_id"]) or {}
    if t.get("headline"):
        m = re.fullmatch(r"(.*)\{(.+)\}(.*)", t["headline"])
        before, hl, after = (m.group(1), m.group(2), m.group(3)) if m else (t["headline"], "", "")
    else:
        before, hl, after = fallback_headline(p)
    return before, hl, after, t.get("summary") or fallback_summary(p), bool(t)

# -----------------------------------------------------------------------------
# RENDER
# -----------------------------------------------------------------------------

def keep_hyphens(escaped):
    """Stop the headline wrapping inside '5-for-6' or 'double-double'."""
    return re.sub(r"\S+-\S+", lambda m: f'<span class="nw">{m.group(0)}</span>', escaped)


def card_name(p):
    return f"{p['rank']:02d}_{slug(p['player_name'])}"


def season_label(p):
    return {"preseason": "Preseason", "regular": "Regular Season", "playoffs": "Playoffs",
            "play-in": "Play-In"}.get(p["season_type"], p["season_type"].title())


def date_label(game_date):
    return game_date.strftime("%b %d, %Y").replace(" 0", " ")


def made_att(m, a):
    return "—" if m is None else f"{m}-{a}"


def render_html(p, template, texts, jersey, game_date):
    e = html.escape
    team = team_key(p["team"])
    before, hl, after, summary, _ = card_text(p, texts)
    meta = " · ".join(x for x in (TEAM_NAMES.get(team, team), p["position"] or "",
                                  f"#{jersey}" if jersey else "") if x)

    photo_path = headshot(p["player_id"])
    if photo_path:
        photo = f'<img class="photo" src="{e(file_url(photo_path))}" alt="">'
    else:
        initials = "".join(w[0] for w in p["player_name"].split()[:2]).upper()
        photo = f'<div class="nophoto">{e(initials)}</div>'

    big = "".join(f'<div><b>{v}</b><span>{k}</span></div>'
                  for k, v in (("POINTS", p["pts"]), ("REBOUNDS", p["reb"]), ("ASSISTS", p["ast"])))
    strip = "".join(f'<div><b>{v}</b><span>{k}</span></div>' for k, v in (
        ("MIN", p["min"]), ("FG", made_att(p["fgm"], p["fga"])), ("3PT", made_att(p["tpm"], p["tpa"])),
        ("FT", made_att(p["ftm"], p["fta"])), ("STL", p["stl"]), ("BLK", p["blk"]), ("TO", p["tov"])))

    pm = p["plus_minus"]
    badges = [f'<span class="{"win" if p["won"] else ""}">{result_line(p)} {opponent_label(p)}</span>',
              f'<span>{"Starter" if p["starter"] else "Bench"}</span>']
    if pm is not None:
        badges.append(f'<span>+/- {"+" if pm > 0 else ""}{pm}</span>')
    if p["yahoo_rank"]:
        badges.append(f'<span>Yahoo rank #{p["yahoo_rank"]}</span>')

    source = ("Source: ESPN box score. Yahoo default points: "
              "PTS 1, REB 1.2, AST 1.5, STL 3, BLK 3, TO −1.")
    return (template
            .replace("{{TEAM_VARS}}", team_vars(team))
            .replace("{{RANK}}", str(p["rank"]))
            .replace("{{GAME_LABEL}}", e(f"{season_label(p)} · {game_date.strftime('%b')} {game_date.day}"))
            .replace("{{NAME}}", e(p["player_name"]))
            .replace("{{META}}", e(meta))
            .replace("{{HEADLINE_SIZE}}", headline_size(before + hl + after))
            .replace("{{HEADLINE}}", f'{keep_hyphens(e(before))}<span class="hl">{keep_hyphens(e(hl))}</span>{keep_hyphens(e(after))}')
            .replace("{{BIG}}", big)
            .replace("{{JERSEY}}", e(jersey))
            .replace("{{FPTS}}", f'{p["fpts"]:.1f}')
            .replace("{{PHOTO}}", photo)
            .replace("{{SUMMARY_LABEL}}", "Game Recap")
            .replace("{{SUMMARY_SIZE}}", "t0" if len(summary) <= 340 else summary_size(summary))
            .replace("{{SUMMARY}}", e(summary))
            .replace("{{STRIP}}", strip)
            .replace("{{BADGES}}", "".join(badges))
            .replace("{{SOURCE}}", e(source))
            .replace("{{DATE}}", date_label(game_date)))


def caption(p, texts, game_date):
    before, hl, after, summary, _ = card_text(p, texts)
    team = team_key(p["team"])
    home, away = (p["team"], p["opponent"]) if p["home"] else (p["opponent"], p["team"])
    home_score, away_score = (p["team_score"], p["opp_score"]) if p["home"] else (p["opp_score"], p["team_score"])
    return "\n".join([
        f"🏀 TOP 10 LAST NIGHT | #{p['rank']} {p['player_name']}",
        before + hl + after,
        "",
        f"{p['pts']} PTS · {p['reb']} REB · {p['ast']} AST · {p['stl']} STL · {p['blk']} BLK · {p['tov']} TO",
        f"{made_att(p['fgm'], p['fga'])} FG · {made_att(p['tpm'], p['tpa'])} 3PT · "
        f"{made_att(p['ftm'], p['fta'])} FT · {p['min']} MIN",
        f"🔥 {p['fpts']:.1f} fantasy points (Yahoo default scoring)",
        f"{team_key(away)} {away_score} @ {team_key(home)} {home_score} · "
        f"{season_label(p)}, {date_label(game_date)}",
        "",
        summary,
        "",
        f"#NBA #FantasyBasketball #NBAFantasy #FantasyNBA #{team}"
        + (" #NBAPreseason" if p["season_type"] == "preseason" else ""),
    ])[:2200]


def export_pending(top, texts, game_date):
    for folder in ("pending", "approved", "posted"):
        os.makedirs(os.path.join(QUEUE_DIR, folder), exist_ok=True)
    pending = os.path.join(QUEUE_DIR, "pending")
    done = {os.path.splitext(f)[0] for folder in ("approved", "posted")
            for f in os.listdir(os.path.join(QUEUE_DIR, folder))}
    out_dir = os.path.join(OUT_DIR, game_date.isoformat())
    exported = 0
    for p in top:
        name = f"{game_date.isoformat()}_{card_name(p)}"
        if name in done:
            continue
        before, hl, after, summary, written = card_text(p, texts)
        data = {
            "slug": name,
            "image": f"{name}.png",
            "series": "nightly_top10",
            "game_date": game_date.isoformat(),
            "rank": p["rank"],
            "player": p["player_name"],
            "player_id": p["player_id"],
            "team": team_key(p["team"]),
            "team_name": TEAM_NAMES.get(team_key(p["team"]), p["team"]),
            "opponent": team_key(p["opponent"]),
            "result": f"{result_line(p)} {opponent_label(p)}",
            "season_type": p["season_type"],
            "starter": p["starter"],
            "fpts": p["fpts"],
            **{c: p[c] for c in ("min", "pts", "reb", "ast", "stl", "blk", "tov", "fgm", "fga",
                                 "tpm", "tpa", "ftm", "fta", "oreb", "dreb", "pf", "plus_minus")},
            "yahoo_rank": p["yahoo_rank"],
            "headline": before + hl + after,
            "summary": summary,
            "text_source": "written" if written else "rule_based",
            "caption": caption(p, texts, game_date),
        }
        shutil.copyfile(os.path.join(out_dir, f"{card_name(p)}.png"), os.path.join(pending, f"{name}.png"))
        with open(os.path.join(pending, f"{name}.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        exported += 1
    print(f"Exported {exported} cards to {pending} ({len(top) - exported} already approved/posted)")


def main():
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_DIR, ".env"))
    game_date = (dt.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1
                 else dt.datetime.now(ET).date() - dt.timedelta(days=1))
    browser = find_browser()
    if not browser:
        raise SystemExit("No Chrome/Edge found -- can't render PNGs.")

    top = load_top(game_date)
    if not top:
        raise SystemExit(f"No box scores for {game_date} -- run collect_boxscores.py first.")
    texts = load_text(game_date)
    jersey_of = jerseys(game_date)

    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        template = f.read().replace("{{FONTS}}", font_css())
    out_dir = os.path.join(OUT_DIR, game_date.isoformat())
    os.makedirs(out_dir, exist_ok=True)
    work_dir = make_dir(tempfile.gettempdir())

    def build(p):
        png = os.path.join(out_dir, f"{card_name(p)}.png")
        html_text = render_html(p, template, texts, jersey_of.get(p["player_id"], ""), game_date)
        render_png(browser, html_text, png, work_dir)
        return png

    try:
        with ThreadPoolExecutor(WORKERS) as pool:
            list(pool.map(build, top))
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    print(f"Top {len(top)} for {game_date} -> {out_dir}")
    for p in top:
        print(f"  #{p['rank']:<2} {p['player_name']:22} {team_key(p['team']):4} {p['fpts']:5.1f} fpts")
    unwritten = [p["player_name"] for p in top if p["player_id"] not in texts]
    if unwritten:
        print(f"No written text in Nightly Text/{game_date}.json (rule-based used): {', '.join(unwritten)}")
    export_pending(top, texts, game_date)


if __name__ == "__main__":
    main()
