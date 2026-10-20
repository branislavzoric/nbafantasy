#!/usr/bin/env python3
"""
build_book3_draft_board.py

12-team snake draft board of the top 156 from the user's projections workbook (Book3.xlsx, sheet "Summary (3)":
Player, Team, Position, Projected rank, Round, AVG pro, Total pro). Projected games played = Total / AVG.
Cards: jersey name, positions, team, ESPN injury tag (OUT/IR/SUSP; day-to-day left off), then three numbers
(total FP, FP per game, games played) and the rank. Same look as build_espn_draft_board.py.

  python build_book3_draft_board.py [path/to/Book3.xlsx] [--no-rank]

Output (in "ADP draft order/"): book3_draft_order.csv, book3_draft_board.html, book3_draft_board.png
--no-rank: cards without the rank number, the three projections styled as a big total + two pills
           -> book3_draft_board_norank.html / .png
"""

import csv
import datetime as dt
import html
import os
import re
import sys
import unicodedata

import openpyxl

from build_draft_board import TEAMS, TOTAL_PICKS, column_for, jersey_name, render_png

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(PROJECT_DIR, "ADP draft order")
DEFAULT_SRC = os.path.join(os.path.expanduser("~"), "Downloads", "Book3.xlsx")
SHEET = "Summary (3)"
ESPN = os.path.join(PROJECT_DIR, "espn_players_2026_2027.csv")
TEMPLATE_PATH = os.path.join(OUT_DIR, "book3_draft_board_template.html")
CSV_PATH = os.path.join(OUT_DIR, "book3_draft_order.csv")
HTML_PATH = os.path.join(OUT_DIR, "book3_draft_board.html")
PNG_PATH = os.path.join(OUT_DIR, "book3_draft_board.png")
INJ = {"OUT": "OUT", "INJURY_RESERVE": "IR", "SUSPENSION": "SUSP"}      # DAY_TO_DAY deliberately left off


def key(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return " ".join("".join(c for c in s if c.isalnum() or c == " ").replace(" jr", "").replace(" sr", "").split())


def espn_status():
    with open(ESPN, encoding="utf-8-sig") as f:
        return {key(r["Player"]): INJ.get(r["Status"], "") for r in csv.DictReader(f)}


def load(src):
    ws = openpyxl.load_workbook(src, data_only=True)[SHEET]
    rows = [r for r in ws.iter_rows(min_row=3, values_only=True) if r[0]]
    rows.sort(key=lambda r: int(r[3]))
    status = espn_status()
    picks = []
    for name, team, pos, rank, _rnd, avg, total in (r[:7] for r in rows[:TOTAL_PICKS]):
        rank = int(rank)
        name = re.sub(r"\s*\(\d+\)\s*$", "", name)          # rookies carry their draft pick: "AJ Dybantsa (1)"
        positions = [p.strip() for p in str(pos).split(",") if p.strip()]
        picks.append({"pick": rank, "round": (rank - 1) // TEAMS + 1, "full_name": name.strip(),
                      "jersey": jersey_name(name.strip()), "team": str(team).strip(), "positions": positions,
                      "color_pos": (positions or ["NA"])[0], "injury": status.get(key(name), ""),
                      "fpts": float(total), "fppg": float(avg), "gp": round(float(total) / float(avg))})
    assert [p["pick"] for p in picks] == list(range(1, len(picks) + 1)), "ranks have gaps/duplicates"
    # same jersey surname on the board -> first initial ("P. GEORGE"); same initial too -> full name
    counts = {}
    for p in picks:
        counts[p["jersey"]] = counts.get(p["jersey"], 0) + 1
    short = {id(p): f'{p["full_name"][0].upper()}. {p["jersey"]}' for p in picks if counts[p["jersey"]] > 1}
    clash = {}
    for s in short.values():
        clash[s] = clash.get(s, 0) + 1
    for p in picks:
        s = short.get(id(p))
        if s:
            p["jersey"] = p["full_name"].upper() if clash[s] > 1 else s
    return picks


def render_card(p, rank=True):
    e = html.escape
    name = e(p["jersey"]).replace("-", "-<wbr>")
    injury = f'<span class="inj">{e(p["injury"])}</span>' if p["injury"] else ""
    return (f'<div class="card pos-{e(p["color_pos"])}" style="grid-column:{column_for(p["pick"]) + 1}"'
            f' title="{e(p["full_name"])}">'
            f'<div class="top"><span class="nm{" long" if len(p["jersey"]) > 12 else ""}">{name}</span></div>'
            f'<div class="meta">{e("/".join(p["positions"]))} · {e(p["team"])}{injury}</div>'
            + (f'<div class="foot"><div>{p["fpts"]:,.0f}</div><div>{p["fppg"]:.1f}</div><div>{p["gp"]}</div>'
               f'<div class="rank">{p["pick"]}</div></div></div>' if rank else
               f'<div class="nums"><b class="tot">{p["fpts"]:,.0f}</b><span class="pills"><i class="fpg">{p["fppg"]:.1f}</i>'
               f'<i class="gp">{p["gp"]}</i></span></div></div>'))


NORANK_NOTE = ("<footer>Each card: projected total fantasy points (big number), fantasy points per game (dark pill) and "
               "projected games played (light pill). Picks follow the projected ranking in snake order. OUT = ESPN injury "
               "status. Names are jersey surnames (an initial or full name when two players share one); colors follow "
               "each player's first-listed position.</footer>")


def render_html(picks, src, rank=True):
    rounds = max(p["round"] for p in picks)
    head = '<div class="hd">#</div>' + "".join(f'<div class="hd">{i}</div>' for i in range(1, TEAMS + 1))
    rows = [f'<div class="row">{head}</div>']
    for r in range(1, rounds + 1):
        rows.append(f'<div class="row"><div class="rd">{r}</div>{"".join(render_card(p, rank) for p in picks if p["round"] == r)}</div>')
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        t = f.read()
    updated = dt.datetime.fromtimestamp(os.path.getmtime(src)).strftime("projections from %b %d, %Y").replace(" 0", " ")
    if not rank:
        t = t[:t.index("<footer>")] + NORANK_NOTE + t[t.index("</footer>") + len("</footer>"):]
    return (t.replace("{{BOARD}}", "".join(rows)).replace("{{TOTAL}}", str(len(picks)))
             .replace("{{ROUNDS}}", str(rounds)).replace("{{UPDATED}}", updated))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    rank = "--no-rank" not in sys.argv
    src = args[0] if args else DEFAULT_SRC
    html_path, png_path = (HTML_PATH, PNG_PATH) if rank else (HTML_PATH.replace(".html", "_norank.html"),
                                                              PNG_PATH.replace(".png", "_norank.png"))
    picks = load(src)
    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Rank", "Round", "Player", "Jersey", "Team", "Pos", "Status", "Total FP", "FP/G", "GP"])
        for p in picks:
            w.writerow([p["pick"], p["round"], p["full_name"], p["jersey"], p["team"], "/".join(p["positions"]),
                        p["injury"], round(p["fpts"], 1), round(p["fppg"], 2), p["gp"]])
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(render_html(picks, src, rank))
    render_png(html_path, png_path, height=125 + max(p["round"] for p in picks) * (79 if rank else 86))
    if os.path.exists(html_path.replace(".html", "_render.html")):
        os.remove(html_path.replace(".html", "_render.html"))
    print(f"{len(picks)} picks -> {html_path}\n{png_path}\n{CSV_PATH}")
    print("injury tags:", ", ".join(f'{p["full_name"]} {p["injury"]}' for p in picks if p["injury"]) or "none")


if __name__ == "__main__":
    main()
