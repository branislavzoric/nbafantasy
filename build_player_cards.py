#!/usr/bin/env python3
"""
build_player_cards.py

Builds a 2026-27 projection card (PNG, 1080x1350) for each of the top 200 players
by Yahoo rank, in the style of the Jokic card:
  - every number comes from Yahoo's projections (`yahoo_projections` table);
    season totals are divided by projected games to get per-game values
  - the summary is the player's ESPN fantasy outlook from
    https://fantasy.espn.com/basketball/players/projections?leagueFormatId=2
    (players without one get a summary written from the Yahoo numbers)
  - headshots come from headshots/<espn_player_id>.png, jersey numbers from ESPN
  - ranks are among every player in the Yahoo table

Run yahoo_projections.py (and espn_nba_projections.py for headshots) first.

Output:
  - "Player Cards/<rank>_<name>.png", rendered with headless Chrome/Edge
  - content/pending/<rank>_<name>.png + .json for the n8n caption/Instagram pipeline
    (players already in content/approved or content/posted are skipped)
"""

import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(PROJECT_DIR, "Player Cards")
TEMPLATE_PATH = os.path.join(PROJECT_DIR, "player_card_template.html")
HEADSHOT_DIR = os.path.join(PROJECT_DIR, "headshots")
CONTENT_DIR = os.path.join(PROJECT_DIR, "content")  # pending -> approved -> posted (n8n)
FONT_DIR = os.path.join(PROJECT_DIR, "fonts")  # local Google Fonts, so every render has them

TOP_N = int(os.environ.get("CARDS_TOP_N", "200"))
WORKERS = 6
CARD_W, CARD_H = 540, 675  # CSS px; rendered at 2x

ESPN_URL = ("https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/"
            "seasons/2027/segments/0/leaguedefaults/2")
ESPN_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json",
    "x-fantasy-source": "kona",
    "x-fantasy-platform": "kona-PROD-fba",
}

# Team -> (dark background, accent panel colour)
TEAM_COLORS = {
    "ATL": ("#26282A", "#E03A3E"), "BOS": ("#0B3B24", "#BA9653"), "BKN": ("#111111", "#FFFFFF"),
    "CHA": ("#1D1160", "#00A3AD"), "CHI": ("#1A1A1A", "#CE1141"), "CLE": ("#4A0F27", "#FDBB30"),
    "DAL": ("#002B5E", "#B8C4CA"), "DEN": ("#0E2240", "#FEC524"), "DET": ("#1D428A", "#C8102E"),
    "GSW": ("#1D428A", "#FFC72C"), "HOU": ("#1B1B1B", "#CE1141"), "IND": ("#002D62", "#FDBB30"),
    "LAC": ("#1D428A", "#C8102E"), "LAL": ("#552583", "#FDB927"), "MEM": ("#12173F", "#5D76A9"),
    "MIA": ("#1A1A1A", "#98002E"), "MIL": ("#00471B", "#EEE1C6"), "MIN": ("#0C2340", "#78BE20"),
    "NOP": ("#0C2340", "#C8102E"), "NYK": ("#0B2A5B", "#F58426"), "OKC": ("#002D62", "#EF3B24"),
    "ORL": ("#0B1F3A", "#0077C0"), "PHI": ("#002B5C", "#ED174C"), "PHX": ("#1D1160", "#E56020"),
    "POR": ("#1A1A1A", "#E03A3E"), "SAC": ("#1F1F1F", "#5A2D81"), "SAS": ("#111111", "#C4CED4"),
    "TOR": ("#1A1A1A", "#CE1141"), "UTA": ("#002B5C", "#F9A01B"), "WAS": ("#002B5C", "#E31837"),
}
DEFAULT_COLORS = ("#1F2330", "#8A8FA3")
# Headline highlight where the accent is white and would vanish next to white text
HIGHLIGHT_OVERRIDES = {"BKN": "#A2AAAD"}

TEAM_NAMES = {
    "ATL": "Atlanta Hawks", "BOS": "Boston Celtics", "BKN": "Brooklyn Nets",
    "CHA": "Charlotte Hornets", "CHI": "Chicago Bulls", "CLE": "Cleveland Cavaliers",
    "DAL": "Dallas Mavericks", "DEN": "Denver Nuggets", "DET": "Detroit Pistons",
    "GSW": "Golden State Warriors", "HOU": "Houston Rockets", "IND": "Indiana Pacers",
    "LAC": "LA Clippers", "LAL": "Los Angeles Lakers", "MEM": "Memphis Grizzlies",
    "MIA": "Miami Heat", "MIL": "Milwaukee Bucks", "MIN": "Minnesota Timberwolves",
    "NOP": "New Orleans Pelicans", "NYK": "New York Knicks", "OKC": "Oklahoma City Thunder",
    "ORL": "Orlando Magic", "PHI": "Philadelphia 76ers", "PHX": "Phoenix Suns",
    "POR": "Portland Trail Blazers", "SAC": "Sacramento Kings", "SAS": "San Antonio Spurs",
    "TOR": "Toronto Raptors", "UTA": "Utah Jazz", "WAS": "Washington Wizards",
}

# -----------------------------------------------------------------------------
# DATA
# -----------------------------------------------------------------------------

def load_yahoo():
    from sqlalchemy import create_engine, text
    engine = create_engine(os.environ["DATABASE_URL"])
    query = text("""
        SELECT yahoo_rank, espn_player_id, full_name, pro_team, positions, injury_status,
               gp, mpg, fg_pct, ft_pct, reb, ast, stl, blk, tov, pts,
               fpts_total, fpts_per_game, snapshot_at
        FROM yahoo_projections
        WHERE fpts_total IS NOT NULL
        ORDER BY yahoo_rank
    """)
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(query)]


def fetch_espn_players():
    """ESPN player id -> {"outlook", "jersey"} for everyone on the projections page."""
    session = requests.Session()
    session.headers.update(ESPN_HEADERS)
    players, offset = {}, 0
    while True:
        fantasy_filter = {"players": {
            "limit": 250, "offset": offset,
            "sortDraftRanks": {"sortPriority": 100, "sortAsc": True, "value": "STANDARD"},
        }}
        for attempt in range(6):
            resp = session.get(ESPN_URL, params={"view": "kona_player_info"},
                               headers={"x-fantasy-filter": json.dumps(fantasy_filter)}, timeout=60)
            if resp.status_code != 202:  # 202 = ESPN cache warming
                break
            time.sleep(2 * (attempt + 1))
        resp.raise_for_status()
        page = resp.json().get("players", [])
        for entry in page:
            p = entry["player"]
            players[p["id"]] = {"outlook": (p.get("seasonOutlook") or "").strip(),
                                "jersey": p.get("jersey") or ""}
        if len(page) < 250:
            return players
        offset += 250


def per_game(p):
    """Yahoo gives season totals; divide by projected games."""
    gp = p["gp"] or 0
    for stat in ("pts", "reb", "ast", "stl", "blk", "tov"):
        p[f"{stat}_pg"] = (p[stat] or 0) / gp if gp else 0.0
    return p


def add_ranks(players):
    """Rank among every projected player (1 = best; turnovers not ranked)."""
    keys = {"fppg": "fpts_per_game", "pts": "pts_pg", "reb": "reb_pg", "ast": "ast_pg",
            "stl": "stl_pg", "blk": "blk_pg"}
    for name, key in keys.items():
        ordered = sorted(players, key=lambda p: -(p[key] or 0))
        for i, p in enumerate(ordered, start=1):
            p.setdefault("ranks", {})[name] = i

# -----------------------------------------------------------------------------
# TEXT
# -----------------------------------------------------------------------------

def surname(full_name):
    parts = full_name.split()
    rest = parts[1:] or parts
    # Drop suffixes from the headline name: "Jaren Jackson Jr." -> "Jackson"
    while len(rest) > 1 and rest[-1].rstrip(".").upper() in {"JR", "SR", "II", "III", "IV"}:
        rest = rest[:-1]
    return " ".join(rest)


def ordinal(n):
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def load_json(name):
    path = os.path.join(PROJECT_DIR, name)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# Hand-written text, keyed by card name:
#   headlines.json: "text {highlighted} text"
#   outlooks.json:  fantasy outlook for players ESPN hasn't written one for
HEADLINES = load_json("headlines.json")
OUTLOOKS = load_json("outlooks.json")


def headline(p):
    """(before, highlighted, after): the hand-written headline if there is one,
    otherwise picked from the player's standout projection."""
    custom = HEADLINES.get(card_name(p))
    if custom:
        m = re.fullmatch(r"(.*)\{(.+)\}(.*)", custom)
        return (m.group(1), m.group(2), m.group(3)) if m else (custom, "", "")
    last, r = surname(p["full_name"]), p["ranks"]
    pts, reb, ast = p["pts_pg"], p["reb_pg"], p["ast_pg"]
    doubles = sum(v >= 9.95 for v in (pts, reb, ast))
    if doubles == 3:
        return f"{last} projected to average a ", "triple-double", ""
    if r["fppg"] == 1:
        return f"{last} projected to ", "lead the league", " in fantasy points"
    if r["fppg"] <= 10:
        return f"{last} projected as a ", f"top-{5 if r['fppg'] <= 5 else 10}", " fantasy scorer"
    if doubles == 2:
        return f"{last} projected to average a ", "double-double", ""
    if pts >= 24.95:
        return f"{last} projected to score ", f"{pts:.1f} a night", ""
    for stat, label in (("reb", "rebounds"), ("ast", "assists"), ("blk", "blocks"), ("stl", "steals")):
        if r[stat] <= 10:
            return f"{last} projected ", f"#{r[stat]} in {label}", ""
    if r["fppg"] <= 50:
        return f"{last} projected as a ", "top-50", " fantasy scorer"
    return f"{last} projected for ", f"{p['fpts_per_game']:.1f} fantasy points", " a night"


def headline_size(text):
    n = len(text)
    return "s1" if n <= 36 else "s2" if n <= 50 else "s3"


def fallback_summary(p, total):
    """Used when ESPN has no outlook for the player."""
    r = p["ranks"]
    text = (f"Yahoo projects {p['full_name']} for {p['gp']} games and "
            f"{p['fpts_per_game']:.1f} fantasy points a night, {ordinal(r['fppg'])} "
            f"among {total} projected players, on {p['pts_pg']:.1f} points, "
            f"{p['reb_pg']:.1f} rebounds and {p['ast_pg']:.1f} assists per game.")
    labels = {"pts": "scoring", "reb": "rebounding", "ast": "assists", "stl": "steals", "blk": "blocks"}
    best = min(labels, key=lambda k: r[k])
    text += f" His best category is {labels[best]}, where he ranks {ordinal(r[best])}"
    if p["mpg"]:
        text += f", in a projected {p['mpg']:.1f} minutes a night"
    return text + ". ESPN has not published an outlook for him yet."


def summary_size(text):
    n = len(text)
    return "t1" if n <= 380 else "t2" if n <= 520 else "t3"

# -----------------------------------------------------------------------------
# COLOURS
# -----------------------------------------------------------------------------

def luminance(hex_color):
    rgb = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a, b):
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def team_vars(team):
    dark, accent = TEAM_COLORS.get(team, DEFAULT_COLORS)
    on_accent = "#111111" if contrast(accent, "#111111") >= contrast(accent, "#ffffff") else "#ffffff"
    # Accent used as text on the dark background only where it stays readable
    accent_text = HIGHLIGHT_OVERRIDES.get(team) or (accent if contrast(accent, dark) >= 3 else "#ffffff")
    return (f"--team: {dark}; --accent: {accent}; --on-accent: {on_accent}; "
            f"--accent-text: {accent_text};")

# -----------------------------------------------------------------------------
# RENDER
# -----------------------------------------------------------------------------

def fmt_pct(v):
    return "—" if v is None else f"{v:.3f}".lstrip("0")


def file_url(path):
    return "file:///" + path.replace("\\", "/")


def slug(name):
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")


def font_css():
    with open(os.path.join(FONT_DIR, "fonts.css"), encoding="utf-8") as f:
        css = f.read()
    return re.sub(r"url\(([^)]+\.woff2)\)",
                  lambda m: f"url({file_url(os.path.join(FONT_DIR, m.group(1)))})", css)


def summary_for(p, total):
    """(text, label): the ESPN outlook, else our own from outlooks.json, else a
    Yahoo-based summary."""
    if p["espn"].get("outlook"):
        return p["espn"]["outlook"], "ESPN Fantasy Outlook"
    if OUTLOOKS.get(card_name(p)):
        return OUTLOOKS[card_name(p)], "Fantasy Outlook"
    return fallback_summary(p, total), "Projection Summary"


def card_name(p):
    return f'{p["yahoo_rank"]:03d}_{slug(p["full_name"])}'


def render_html(p, template, total, as_of):
    e = html.escape
    espn = p["espn"]
    before, hl, after = headline(p)
    team = p["pro_team"] or "FA"
    positions = "/".join(p["positions"] or [])
    jersey = espn.get("jersey", "")
    meta = " · ".join(x for x in (TEAM_NAMES.get(team, team), positions, f"#{jersey}" if jersey else "") if x)
    injury = f'<span class="inj">{e(p["injury_status"])}</span>' if p["injury_status"] else ""

    headshot = os.path.join(HEADSHOT_DIR, f'{p["espn_player_id"]}.png')
    if p["espn_player_id"] and os.path.exists(headshot):
        photo = f'<img class="photo" src="{e(file_url(headshot))}" alt="">'
    else:
        initials = "".join(w[0] for w in p["full_name"].split()[:2]).upper()
        photo = f'<div class="nophoto">{e(initials)}</div>'

    summary, label = summary_for(p, total)

    big = "".join(f'<div><b>{v:.1f}</b><span>{k}</span></div>'
                  for k, v in (("POINTS", p["pts_pg"]), ("REBOUNDS", p["reb_pg"]), ("ASSISTS", p["ast_pg"])))
    strip = "".join(f'<div><b>{v}</b><span>{k}</span></div>' for k, v in (
        ("GP", p["gp"]), ("MIN", "—" if p["mpg"] is None else f'{p["mpg"]:.1f}'),
        ("FG%", fmt_pct(p["fg_pct"])), ("FT%", fmt_pct(p["ft_pct"])),
        ("STL", f'{p["stl_pg"]:.1f}'), ("BLK", f'{p["blk_pg"]:.1f}'), ("TO", f'{p["tov_pg"]:.1f}')))

    r = p["ranks"]
    badges = [f'<span class="main">Yahoo rank #{p["yahoo_rank"]}</span>', f'<span>#{r["fppg"]} in FPTS/G</span>']
    badges += [f'<span>#{r[k]} in {label}</span>' for k, label in
               (("pts", "PTS"), ("reb", "REB"), ("ast", "AST"), ("stl", "STL"), ("blk", "BLK")) if r[k] <= 50]

    source = (f"Source: Yahoo Fantasy projections, points scoring. "
              f"{'Outlook: ESPN Fantasy. ' if espn.get('outlook') else ''}Ranks among {total} projected players.")
    return (template
            .replace("{{TEAM_VARS}}", team_vars(team))
            .replace("{{RANK}}", str(p["yahoo_rank"]))
            .replace("{{NAME}}", e(p["full_name"]))
            .replace("{{META}}", e(meta))
            .replace("{{INJURY}}", injury)
            .replace("{{HEADLINE_SIZE}}", headline_size(before + hl + after))
            .replace("{{HEADLINE}}", f'{e(before)}<span class="hl">{e(hl)}</span>{e(after)}')
            .replace("{{BIG}}", big)
            .replace("{{JERSEY}}", e(jersey))
            .replace("{{FPPG}}", f'{p["fpts_per_game"]:.1f}')
            .replace("{{PHOTO}}", photo)
            .replace("{{SUMMARY_LABEL}}", label)
            .replace("{{SUMMARY_SIZE}}", summary_size(summary))
            .replace("{{SUMMARY}}", e(summary))
            .replace("{{STRIP}}", strip)
            .replace("{{BADGES}}", "".join(badges))
            .replace("{{SOURCE}}", e(source))
            .replace("{{DATE}}", f"Data as of {as_of}"))


def find_browser():
    browsers = [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"]
    return next((b for b in browsers if os.path.exists(b)), None)


def render_png(browser, html_text, png_path, work_dir, width=CARD_W, height=CARD_H):
    html_path = os.path.join(work_dir, os.path.basename(png_path).replace(".png", ".html"))
    with open(html_path, "w", encoding="utf-8") as f:
        f.write('<!doctype html><meta charset="utf-8">' + html_text)
    if os.path.exists(png_path):
        os.remove(png_path)  # so a failed render can't leave the old card in place
    for attempt in range(3):  # headless Chrome occasionally hangs; retry with a fresh profile
        # Separate profile so parallel runs don't collide. Not tempfile.mkdtemp: on Windows
        # its locked-down folder is unreadable to Chrome's sandbox and nothing gets rendered.
        profile = make_dir(work_dir)
        try:
            subprocess.run([browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                            f"--user-data-dir={profile}", "--allow-file-access-from-files",
                            "--force-device-scale-factor=2", "--virtual-time-budget=5000",
                            f"--screenshot={png_path}", f"--window-size={width},{height}",
                            file_url(html_path)],
                           check=True, capture_output=True, timeout=60)
        except subprocess.TimeoutExpired:
            if attempt == 2:
                raise
            continue
        # Newer Chrome can return before the screenshot is written; wait for it to land.
        for _ in range(60):
            if os.path.exists(png_path) and os.path.getsize(png_path) > 0:
                time.sleep(0.5)
                return
            time.sleep(0.5)
    raise RuntimeError(f"Chrome did not render {png_path}")


def make_dir(parent):
    path = os.path.join(parent, uuid.uuid4().hex[:12])
    os.makedirs(path)
    return path


def export_pending(players, total, as_of):
    """Copies each card into content/pending with a JSON file of the data the caption
    is written from. Cards already approved or posted are left alone."""
    pending = os.path.join(CONTENT_DIR, "pending")
    for folder in ("pending", "approved", "posted"):
        os.makedirs(os.path.join(CONTENT_DIR, folder), exist_ok=True)
    done = {os.path.splitext(f)[0] for folder in ("approved", "posted")
            for f in os.listdir(os.path.join(CONTENT_DIR, folder))}

    exported = 0
    for p in players:
        name = card_name(p)
        if name in done:
            continue
        summary, label = summary_for(p, total)
        before, hl, after = headline(p)
        team = p["pro_team"] or "FA"
        data = {
            "slug": name,
            "image": f"{name}.png",
            "rank": p["yahoo_rank"],
            "player": p["full_name"],
            "team": team,
            "team_name": TEAM_NAMES.get(team, team),
            "positions": "/".join(p["positions"] or []),
            "injury_status": p["injury_status"],
            "headline": before + hl + after,
            "gp": p["gp"],
            "mpg": p["mpg"],
            "fppg": round(p["fpts_per_game"], 1),
            "fpts_total": round(p["fpts_total"], 1),
            "pts": round(p["pts_pg"], 1), "reb": round(p["reb_pg"], 1), "ast": round(p["ast_pg"], 1),
            "stl": round(p["stl_pg"], 1), "blk": round(p["blk_pg"], 1), "tov": round(p["tov_pg"], 1),
            "fg_pct": p["fg_pct"], "ft_pct": p["ft_pct"],
            "ranks": p["ranks"],
            "ranked_players": total,
            "summary": summary,
            "summary_source": {"ESPN Fantasy Outlook": "espn_outlook",
                               "Fantasy Outlook": "custom_outlook"}.get(label, "yahoo_numbers"),
            "data_as_of": as_of,
        }
        data["caption"] = "\n".join([
            f"🏀 #{p['yahoo_rank']} OVERALL | {data['headline']}",
            "",
            f"{data['player']} ({team}) – 2026-27 projection:",
            f"{data['pts']} PTS · {data['reb']} REB · {data['ast']} AST · {data['stl']} STL · {data['blk']} BLK",
            f"{data['fppg']} fantasy pts/game (#{p['ranks']['fppg']} in FPTS/G)",
            "",
            summary,
            "",
            f"#NBA #FantasyBasketball #NBAFantasy #{team} #FantasyNBA",
        ])[:2200]
        shutil.copyfile(os.path.join(OUT_DIR, f"{name}.png"), os.path.join(pending, f"{name}.png"))
        with open(os.path.join(pending, f"{name}.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        exported += 1
    print(f"Exported {exported} cards to {pending} ({len(players) - exported} already approved/posted)")


def main():
    browser = find_browser()
    if not browser:
        raise SystemExit("No Chrome/Edge found -- can't render PNGs.")

    yahoo = [per_game(p) for p in load_yahoo()]
    add_ranks(yahoo)
    total = len(yahoo)
    as_of = max(p["snapshot_at"] for p in yahoo).strftime("%b %d, %Y").replace(" 0", " ")

    print("Fetching ESPN outlooks...")
    espn = fetch_espn_players()
    top = yahoo[:TOP_N]
    for p in top:
        p["espn"] = espn.get(p["espn_player_id"], {})

    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        template = f.read().replace("{{FONTS}}", font_css())
    os.makedirs(OUT_DIR, exist_ok=True)
    work_dir = make_dir(tempfile.gettempdir())

    def build(p):
        png = os.path.join(OUT_DIR, f"{card_name(p)}.png")
        render_png(browser, render_html(p, template, total, as_of), png, work_dir)
        return png

    try:
        with ThreadPoolExecutor(WORKERS) as pool:
            for i, png in enumerate(pool.map(build, top), start=1):
                if i % 20 == 0 or i == len(top):
                    print(f"  {i}/{len(top)} cards")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    no_outlook = [p["full_name"] for p in top if not p["espn"].get("outlook")]
    no_photo = [p["full_name"] for p in top
                if not os.path.exists(os.path.join(HEADSHOT_DIR, f'{p["espn_player_id"]}.png'))]
    print(f"Saved {len(top)} cards to {OUT_DIR}")
    export_pending(top, total, as_of)
    print(f"{len(no_outlook)} without an ESPN outlook (used a Yahoo-based summary).")
    if no_photo:
        print(f"No headshot (initials shown): {', '.join(no_photo)}")


if __name__ == "__main__":
    main()
