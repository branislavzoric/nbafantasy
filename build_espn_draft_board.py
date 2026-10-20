#!/usr/bin/env python3
"""
build_espn_draft_board.py

12-team snake draft board of ESPN's top 156 (13 rounds), in ESPN's own fantasy ranking order
(which includes rookies and injured players ESPN still ranks, e.g. players without a projection line).
Cards show only the jersey name, positions, team, ESPN injury status and the rank number,
colored by primary position like build_draft_board.py.

Reads espn_players_2026_2027.csv (run espn_nba_projections.py first for fresh ranks).
Output (in "ADP draft order/"): espn_draft_order.csv, espn_draft_board.html, espn_draft_board.png
"""

import csv
import datetime as dt
import html
import os

from build_draft_board import TEAMS, TOTAL_PICKS, column_for, jersey_name, render_png

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(PROJECT_DIR, "ADP draft order")
SRC = os.path.join(PROJECT_DIR, "espn_players_2026_2027.csv")
TEMPLATE_PATH = os.path.join(OUT_DIR, "espn_draft_board_template.html")
CSV_PATH = os.path.join(OUT_DIR, "espn_draft_order.csv")
HTML_PATH = os.path.join(OUT_DIR, "espn_draft_board.html")
PNG_PATH = os.path.join(OUT_DIR, "espn_draft_board.png")
INJ = {"OUT": "OUT", "DAY_TO_DAY": "DTD", "INJURY_RESERVE": "IR", "SUSPENSION": "SUSP"}


def load_espn():
    with open(SRC, encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if r["Player"] and r["Rank"]]
    rows.sort(key=lambda r: int(r["Rank"]))
    picks = []
    for r in rows[:TOTAL_PICKS]:
        rank = int(r["Rank"])
        positions = [p for p in r["Pos"].split("/") if p]
        picks.append({"pick": rank, "round": (rank - 1) // TEAMS + 1, "full_name": r["Player"],
                      "jersey": jersey_name(r["Player"]), "team": r["Team"], "positions": positions,
                      "color_pos": (positions or ["NA"])[0], "injury": INJ.get(r["Status"], "")})
    # same jersey surname on the board (George, Murray, ...) -> first initial too: "P. GEORGE"
    counts = {}
    for p in picks:
        counts[p["jersey"]] = counts.get(p["jersey"], 0) + 1
    short = {id(p): f'{p["full_name"][0].upper()}. {p["jersey"]}' for p in picks if counts[p["jersey"]] > 1}
    clash = {}
    for s in short.values():
        clash[s] = clash.get(s, 0) + 1
    for p in picks:                                   # same initial too (Amen/Ausar Thompson) -> full name
        s = short.get(id(p))
        if s:
            p["jersey"] = p["full_name"].upper() if clash[s] > 1 else s
    ranks = [p["pick"] for p in picks]
    assert ranks == list(range(1, len(picks) + 1)), "ESPN ranks have gaps/duplicates"
    return picks


def render_card(p):
    e = html.escape
    name = e(p["jersey"]).replace("-", "-<wbr>")
    injury = f'<span class="inj">{e(p["injury"])}</span>' if p["injury"] else ""
    return (f'<div class="card pos-{e(p["color_pos"])}" style="grid-column:{column_for(p["pick"]) + 1}"'
            f' title="{e(p["full_name"])}">'
            f'<div class="top"><span class="nm{" long" if len(p["jersey"]) > 12 else ""}">{name}</span></div>'
            f'<div class="meta">{e("/".join(p["positions"]))} · {e(p["team"])}{injury}</div>'
            f'<div class="rank">{p["pick"]}</div></div>')


def render_html(picks):
    rounds = max(p["round"] for p in picks)
    head = '<div class="hd">#</div>' + "".join(f'<div class="hd">{i}</div>' for i in range(1, TEAMS + 1))
    rows = [f'<div class="row">{head}</div>']
    for r in range(1, rounds + 1):
        rows.append(f'<div class="row"><div class="rd">{r}</div>{"".join(render_card(p) for p in picks if p["round"] == r)}</div>')
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        t = f.read()
    updated = dt.datetime.fromtimestamp(os.path.getmtime(SRC)).strftime("ESPN data from %b %d, %Y").replace(" 0", " ")
    return (t.replace("{{BOARD}}", "".join(rows)).replace("{{TOTAL}}", str(len(picks)))
             .replace("{{ROUNDS}}", str(rounds)).replace("{{UPDATED}}", updated))


def main():
    picks = load_espn()
    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Rank", "Round", "Player", "Jersey", "Team", "Pos", "Status"])
        for p in picks:
            w.writerow([p["pick"], p["round"], p["full_name"], p["jersey"], p["team"], "/".join(p["positions"]), p["injury"]])
    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(render_html(picks))
    render_png(HTML_PATH, PNG_PATH, height=125 + max(p["round"] for p in picks) * 76)
    print(f"{len(picks)} picks -> {HTML_PATH}\n{PNG_PATH}\n{CSV_PATH}")


if __name__ == "__main__":
    main()
