"""
render_common.py - shared helpers for rendering Instagram slides with headless Chrome.

Used by build_news_posts.py, build_bet_carousels.py, build_football_bets.py, settle_bets.py and
settle_tonight.py. Moved here on 2026-11-11 from the removed fantasy scripts (build_player_cards.py,
build_nightly_cards.py, build_depth_chart.py, build_odds_page.py), unchanged.
"""

import os
import re
import subprocess
import time
import uuid

import requests

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(PROJECT_DIR, "fonts")  # local Google Fonts, so every render has them
HEADSHOT_DIR = os.path.join(PROJECT_DIR, "headshots")
LOGO_DIR = os.path.join(PROJECT_DIR, "logos")

CARD_W, CARD_H = 540, 675  # CSS px; rendered at 2x

HEADSHOT_URL = "https://a.espncdn.com/i/headshots/nba/players/full/{player_id}.png"
LOGO_URL = "https://a.espncdn.com/i/teamlogos/nba/500/{team}.png"
ESPN_TEAM = {"GSW": "gs", "NYK": "ny", "NOP": "no", "SAS": "sa", "UTA": "utah", "WAS": "wsh"}

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

BOOK_LABELS = {"soccerbet": "Soccerbet", "maxbet": "MaxBet", "admiralbet": "Admiral",
               "mozzartbet": "Mozzart", "superbet": "Superbet", "balkanbet": "Balkanbet"}


def file_url(path):
    return "file:///" + path.replace("\\", "/")


def font_css():
    with open(os.path.join(FONT_DIR, "fonts.css"), encoding="utf-8") as f:
        css = f.read()
    css = re.sub(r"url\(([^)]+\.woff2)\)",
                 lambda m: f"url({file_url(os.path.join(FONT_DIR, m.group(1)))})", css)
    # the B3 Basketball Instagram theme rides along with the fonts, so every slide renderer gets it
    with open(os.path.join(PROJECT_DIR, "ig_theme.css"), encoding="utf-8") as f:
        return css + "\n" + f.read()


def find_browser():
    browsers = [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"]
    return next((b for b in browsers if os.path.exists(b)), None)


def make_dir(parent):
    path = os.path.join(parent, uuid.uuid4().hex[:12])
    os.makedirs(path)
    return path


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


def team_logo(team):
    """Local copy of the team's logo (ESPN), downloaded on first use."""
    path = os.path.join(LOGO_DIR, f"{team}.png")
    if not os.path.exists(path):
        os.makedirs(LOGO_DIR, exist_ok=True)
        r = requests.get(LOGO_URL.format(team=ESPN_TEAM.get(team, team.lower())), timeout=30)
        if r.status_code != 200 or not r.content:
            return None
        with open(path, "wb") as f:
            f.write(r.content)
    return path
