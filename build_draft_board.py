#!/usr/bin/env python3
"""
build_draft_board.py

Builds a 12-team snake draft board of the top 156 players (13 rounds), ordered
by Yahoo's projected season total fantasy points. Every card shows:
  - jersey name (surname as printed on the jersey, incl. Jr./III)
  - positions and team
  - FP    (projected season total fantasy points -- the ranking key)
  - FP/G  (projected fantasy points per game)
  - GP    (projected games played)

Reads the `yahoo_projections` table (run yahoo_projections.py first).
The earlier ADP order read from "ADP draft.png" is kept in adp_picks.csv.

Output (in "ADP draft order/"):
  - draft_order.csv   (the 156 picks)
  - draft_board.html
  - draft_board.png   (rendered with headless Chrome/Edge)
"""

import csv
import html
import os
import subprocess

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(PROJECT_DIR, "ADP draft order")
TEMPLATE_PATH = os.path.join(OUT_DIR, "draft_board_template.html")
CSV_PATH = os.path.join(OUT_DIR, "draft_order.csv")
HTML_PATH = os.path.join(OUT_DIR, "draft_board.html")
PNG_PATH = os.path.join(OUT_DIR, "draft_board.png")

TEAMS = 12
TOTAL_PICKS = 156

# -----------------------------------------------------------------------------
# DATA
# -----------------------------------------------------------------------------

def load_yahoo():
    from sqlalchemy import create_engine, text
    engine = create_engine(os.environ["DATABASE_URL"])
    query = text("""
        SELECT y.full_name, y.pro_team, y.positions, y.injury_status,
               y.gp, y.fpts_total, y.fpts_per_game, y.yahoo_rank,
               e.positions AS espn_positions
        FROM yahoo_projections y
        LEFT JOIN espn_players e ON e.player_id = y.espn_player_id
        WHERE y.fpts_total IS NOT NULL
    """)
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(query)]


def jersey_name(full_name):
    """Jersey lettering is the surname, suffix included: 'Jaren Jackson Jr.' -> 'JACKSON JR.'"""
    parts = full_name.split()
    return " ".join(parts[1:] or parts).upper()


def build_picks(yahoo):
    ranked = sorted(yahoo, key=lambda y: (-y["fpts_total"], y["yahoo_rank"] or 0))
    picks = []
    for pick, y in enumerate(ranked[:TOTAL_PICKS], start=1):
        positions = y["positions"] or []
        picks.append({
            "pick": pick,
            "round": (pick - 1) // TEAMS + 1,
            "full_name": y["full_name"],
            "jersey": jersey_name(y["full_name"]),
            "team": y["pro_team"],
            "positions": positions,
            "color_pos": ((y["espn_positions"] or [])[:1] or positions[:1] or ["NA"])[0],
            "injury": y["injury_status"],
            "fpts": y["fpts_total"],
            "fppg": y["fpts_per_game"],
            "gp": y["gp"],
        })
    return picks

# -----------------------------------------------------------------------------
# OUTPUT
# -----------------------------------------------------------------------------

def save_csv(picks):
    # utf-8-sig so Excel opens it with the right encoding
    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Pick", "Round", "Player", "Jersey", "Team", "Pos", "Status", "FP", "FP/G", "GP"])
        for p in picks:
            w.writerow([p["pick"], p["round"], p["full_name"], p["jersey"], p["team"],
                        ",".join(p["positions"]), p["injury"] or "",
                        p["fpts"], p["fppg"], p["gp"]])


def forward(rnd):
    """Snake with third-round reversal: R1 1->12, R2 and R3 12->1, then alternating (R4 1->12, R5 12->1, ...)."""
    return rnd == 1 or (rnd >= 3 and rnd % 2 == 0)


def column_for(pick, teams=TEAMS):
    """Team slot (1-teams) of an overall pick in a 3RR snake draft."""
    rnd, idx = (pick - 1) // teams + 1, (pick - 1) % teams
    return idx + 1 if forward(rnd) else teams - idx


def render_card(p):
    e = html.escape
    # Let long hyphenated names break after the hyphen instead of mid-word
    name = e(p["jersey"]).replace("-", "-<wbr>")
    injury = f'<span class="inj">{e(p["injury"])}</span>' if p["injury"] else ""
    stats = "".join(
        f'<div><b>{value}</b><span>{label}</span></div>'
        for value, label in ((f'{p["fpts"]:,.0f}', "FP"), (f'{p["fppg"]:.1f}', "FP/G"), (p["gp"], "GP")))
    return (f'<div class="card pos-{e(p["color_pos"])}" style="grid-column:{column_for(p["pick"]) + 1}"'
            f' title="{e(p["full_name"])}">'
            f'<div class="top"><span class="nm{' long' if len(p['jersey']) > 11 else ''}">{name}</span><span class="pk">{p["pick"]}</span></div>'
            f'<div class="meta">{e(",".join(p["positions"]))} · {e(p["team"])}{injury}</div>'
            f'<div class="stats">{stats}</div></div>')


def render_html(picks):
    rounds = max(p["round"] for p in picks)
    head = '<div class="hd">#</div>' + "".join(f'<div class="hd">{i}</div>' for i in range(1, TEAMS + 1))
    rows = [f'<div class="row">{head}</div>']
    for r in range(1, rounds + 1):
        cards = "".join(render_card(p) for p in picks if p["round"] == r)
        rows.append(f'<div class="row"><div class="rd">{r}</div>{cards}</div>')
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        template = f.read()
    return (template.replace("{{BOARD}}", "".join(rows))
                    .replace("{{TOTAL}}", str(len(picks)))
                    .replace("{{ROUNDS}}", str(rounds)))


def render_png(html_path, png_path, height, width=1680):
    browsers = [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"]
    browser = next((b for b in browsers if os.path.exists(b)), None)
    if not browser:
        print("No Chrome/Edge found -- skipped PNG.")
        return
    # The published page gets its doctype from the host; add one for the local render.
    render_path = html_path.replace(".html", "_render.html")
    with open(html_path, encoding="utf-8") as f, open(render_path, "w", encoding="utf-8") as out:
        out.write('<!doctype html><meta charset="utf-8">' + f.read())
    subprocess.run([browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    "--force-device-scale-factor=2", "--virtual-time-budget=5000",
                    f"--screenshot={png_path}", f"--window-size={width},{height}",
                    "file:///" + render_path.replace("\\", "/")],
                   check=True, capture_output=True)
    os.remove(render_path)


def main():
    picks = build_picks(load_yahoo())
    print(f"{len(picks)} picks, ordered by Yahoo projected total fantasy points.")

    save_csv(picks)
    print(f"Saved {CSV_PATH}")
    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(render_html(picks))
    print(f"Saved {HTML_PATH}")

    rounds = max(p["round"] for p in picks)
    render_png(HTML_PATH, PNG_PATH, height=190 + rounds * 88)
    print(f"Saved {PNG_PATH}")


if __name__ == "__main__":
    main()
