#!/usr/bin/env python3
"""
build_more_draft_boards.py

12-team snake draft boards of the top 156 (13 rounds), same look as build_book3_draft_board.py:

  The three projection boards use the user's Yahoo league scoring incl. DD/TD/TECH/EJCT bonuses (league_scoring.py)
  and are ranked by projected season total.
  fantasypros  FantasyPros 2026-27 projections CSV (Downloads/FantasyPros_NBA_Fantasy_Basketball_Overall_*_Projections.csv).
               It has no FGM/FGA/FTM/FTA/PF: FTA = FantasyPros PTS x the player's ESPN projected FTA/PTS ratio,
               FTM = FTA x FT%, FGM = (PTS - FTM - 3PM) / 2, FGA = FGM / FG%, PF = 2025-26 PF per minute x MIN.
  espn         ESPN 2026-27 projected totals (PF from the 2025-26 PF-per-minute rate; ESPN projects none).
  yahoo        Yahoo projections (Postgres yahoo_projections, from the saved league pages): Yahoo's Fan Pts + bonuses.
  adp          FantasyPros consensus ADP, https://www.fantasypros.com/nba/adp/overall.php (Yahoo + ESPN ADP and average).

Injury tags (OUT/IR/SUSP) come from the fresh ESPN file; card colours from ESPN's primary position when matched.

  python build_more_draft_boards.py [--league boogie] [fantasypros|espn|yahoo|adp ...]      (default: all four)

--league picks the scoring from league_scoring.LEAGUES (default trebevicka); a non-default league prefixes the
output files, e.g. boogie_espn_draft_order.csv.

Output (in "ADP draft order/"): [<league>_]<name>_draft_order.csv, _draft_board.html, _draft_board.png
"""

import csv
import datetime as dt
import glob
import html
import os
import re
import sys

import requests

from build_draft_board import TEAMS, TOTAL_PICKS, column_for, jersey_name, render_png
from league_scoring import LEAGUES, bonus, espn_players, key, score

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(PROJECT_DIR, "ADP draft order")
TEMPLATE_PATH = os.path.join(OUT_DIR, "book3_draft_board_template.html")
ESPN = os.path.join(PROJECT_DIR, "espn_players_2026_2027.csv")
DOWNLOADS = os.path.join(os.path.expanduser("~"), "Downloads")
ADP_URL = "https://www.fantasypros.com/nba/adp/overall.php"
ADP_RAW = os.path.join(OUT_DIR, "fantasypros_adp.html")
INJ = {"OUT": "OUT", "INJURY_RESERVE": "IR", "SUSPENSION": "SUSP"}      # day-to-day left off, as on the Book3 board
LEAGUE = LEAGUES["trebevicka"]
PREFIX = ""


def set_league(name):
    global LEAGUE, PREFIX
    LEAGUE = LEAGUES[name]
    assert LEAGUE["teams"] == TEAMS, "board layout is built for 12 teams"
    PREFIX = "" if name == "trebevicka" else f"{name}_"


def espn_info():
    with open(ESPN, encoding="utf-8-sig") as f:
        return {key(r["Player"]): {"injury": INJ.get(r["Status"], ""), "pos": (r["Pos"].split("/") or [""])[0]}
                for r in csv.DictReader(f) if r["Player"]}


def finish(rows):
    """rows sorted best first -> picks with rank, round, jersey name, ESPN injury and colour."""
    info = espn_info()
    picks = []
    for rank, r in enumerate(rows[:TOTAL_PICKS], start=1):
        e = info.get(key(r["full_name"]), {})
        picks.append(dict(r, pick=rank, round=(rank - 1) // TEAMS + 1, jersey=jersey_name(r["full_name"]),
                          injury=e.get("injury", ""), color_pos=e.get("pos") or (r["positions"] or ["NA"])[0]))
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


# ----------------------------------------------------------------------------- sources

def median(xs, default):
    xs = sorted(x for x in xs if x)
    return xs[len(xs) // 2] if xs else default


def with_bonus(row, e):
    """Adds projected DD/TD/TECH/EJ (2025-26 rate x GP) and their points to a row with base fpts + gp."""
    counts, pts = bonus(e, row["gp"], LEAGUE["bonus"])
    row.update({k: round(v, 1) for k, v in counts.items()}, bonus=pts, base=row["fpts"])
    row["fpts"] += pts
    row["fppg"] = row["fpts"] / row["gp"]
    return row


def pf_rate(e, espn):
    """2025-26 personal fouls per minute (league median when the player has no line)."""
    last = (e or {}).get("last") or {}
    if last.get("min"):
        return (last.get("pf") or 0) / last["min"]
    return median([(v["last"].get("pf") or 0) / v["last"]["min"] for v in espn.values()
                   if (v["last"] or {}).get("min")], 0.08)


def load_fantasypros():
    src = max(glob.glob(os.path.join(DOWNLOADS, "FantasyPros_NBA_Fantasy_Basketball_Overall_*_Projections.csv")),
              key=os.path.getmtime)
    espn = espn_players()
    ratio_med = median([v["proj"]["fta"] / v["proj"]["pts"] for v in espn.values()
                        if (v["proj"] or {}).get("pts") and v["proj"].get("fta") is not None], 0.25)
    rows = []
    with open(src, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            gp = int(r["GP"] or 0)
            if not r["Player"] or not gp:
                continue
            n = lambda c: float(r[c] or 0)
            e = espn.get(key(r["Player"]))
            proj = (e or {}).get("proj") or {}
            ratio = proj["fta"] / proj["pts"] if proj.get("pts") and proj.get("fta") is not None else ratio_med
            fta = n("PTS") * ratio
            ftm = fta * n("FT%")
            fgm = max(n("PTS") - ftm - n("3PM"), 0) / 2
            stats = {"pts": n("PTS"), "reb": n("REB"), "ast": n("AST"), "stl": n("STL"), "blk": n("BLK"),
                     "tov": n("TO"), "fta": fta, "ftm": ftm, "fgm": fgm, "fga": fgm / n("FG%") if n("FG%") else 0,
                     "pf": pf_rate(e, espn) * n("MIN")}
            rows.append(with_bonus({"full_name": r["Player"].strip(), "team": r["Team"],
                                    "positions": r["Positions"].split()[0].split(","),   # "C,PF OUT" -> status dropped (ESPN's is used)
                                    "fpts": score(stats, LEAGUE["weights"]), "gp": gp}, e))
    rows.sort(key=lambda r: -r["fpts"])
    return finish(rows), src


def load_espn():
    espn = espn_players()
    rows = []
    for e in espn.values():
        proj = e["proj"] or {}
        gp = int(proj.get("gp") or 0)
        if not gp:
            continue
        stats = dict(proj, pf=pf_rate(e, espn) * (proj.get("min") or 0))
        rows.append(with_bonus({"full_name": e["name"], "team": e["team"], "positions": e["positions"],
                                "fpts": score(stats, LEAGUE["weights"]), "gp": gp}, e))
    rows.sort(key=lambda r: -r["fpts"])
    return finish(rows), os.path.join(PROJECT_DIR, "espn_raw_stats.json")


def load_yahoo():
    """Yahoo's projected stat lines (yahoo_projections.py export) scored with the league's weights.
    For trebevicka this equals Yahoo's own Fan Pts column."""
    src = os.path.join(PROJECT_DIR, "yahoo_projections_2026_2027.csv")
    espn = espn_players()
    rows = []
    with open(src, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            gp = int(r["GP"] or 0)
            if not r["Player"] or not gp:
                continue
            n = lambda c: float(r[c] or 0)
            stats = {"fgm": n("FGM"), "fga": n("FGA"), "ftm": n("FTM"), "fta": n("FTA"), "pts": n("PTS"), "reb": n("REB"),
                     "ast": n("AST"), "stl": n("STL"), "blk": n("BLK"), "tov": n("TO"), "pf": n("PF")}
            rows.append(with_bonus({"full_name": r["Player"], "team": r["Team"],
                                    "positions": [q for q in r["Pos"].split("/") if q],
                                    "fpts": score(stats, LEAGUE["weights"]), "gp": gp, "yrank": int(r["Rank"] or 0)},
                                   espn.get(key(r["Player"]))))
    rows.sort(key=lambda r: (-r["fpts"], r["yrank"]))
    return finish(rows), src


def load_adp():
    page = requests.get(ADP_URL, timeout=30, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"}).text
    with open(ADP_RAW, "w", encoding="utf-8") as f:
        f.write(page)
    table = page[page.index('id="data"'):]
    table = table[:table.index("</table>")]
    rows = []
    for tr in re.split(r"<tr>", table.split("<tbody>", 1)[1])[1:]:
        m = re.search(r'fp-player-name="([^"]+)".*?<small>\((\w+)\s*-\s*([^)]*)\)</small>', tr, re.S)
        tds = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if not m or len(tds) < 5:
            continue
        num = lambda s: float(s) if re.fullmatch(r"[\d.]+", s) else None
        rows.append({"full_name": html.unescape(m.group(1)), "team": m.group(2),
                     "positions": [p for p in m.group(3).split(",") if p.strip()],
                     "rank": int(tds[0]), "yahoo": num(tds[2]), "espn": num(tds[3]), "adp": num(tds[4])})
    rows.sort(key=lambda r: r["rank"])
    assert len(rows) >= TOTAL_PICKS, f"only {len(rows)} ADP rows parsed"
    return finish(rows), ADP_RAW


# ----------------------------------------------------------------------------- output

BOARDS = {
    "fantasypros": {"load": load_fantasypros, "title": "FantasyPros Projections Draft Board",
                    "sub": "ranked by projected fantasy points (FantasyPros 2026-27 projections, league scoring incl. DD/TD/TECH)"},
    "espn": {"load": load_espn, "title": "ESPN Projections Draft Board",
             "sub": "ranked by projected fantasy points (ESPN 2026-27 projections, league scoring incl. DD/TD/TECH)"},
    "yahoo": {"load": load_yahoo, "title": "Yahoo Projections Draft Board",
              "sub": "ranked by projected fantasy points (Yahoo 2026-27 projections, league scoring incl. DD/TD/TECH)"},
    "adp": {"load": load_adp, "title": "ADP Draft Board",
            "sub": "FantasyPros consensus ADP (average of Yahoo and ESPN draft position)"},
}
CARD_CSS = """
.foot { grid-template-columns: 1.3fr 1fr .8fr; }
.top .pk { font-size: .62rem; font-weight: 700; color: var(--card-muted); line-height: 1.3; }
</style>"""
ADP_CSS = """
.adp { display: grid; grid-template-columns: .8fr .8fr 1.25fr; gap: 3px; align-self: end; margin-top: 3px; }
.adp div { background: var(--card-cell); border-radius: 4px; padding: 1px 0; display: flex; flex-direction: column;
  align-items: center; justify-content: center; font-size: .7rem; font-weight: 800; line-height: 1.1; min-width: 0;
  letter-spacing: -.02em; }
.adp div small { font-size: .5rem; font-weight: 700; color: var(--card-muted); letter-spacing: 0; }
</style>"""
SNAKE = "Snake with third-round reversal: round 1 runs team 1 → 12, rounds 2 and 3 run 12 → 1, then it alternates."


def fmt(v):
    return "–" if v is None else (f"{v:.0f}" if v == int(v) else f"{v:.1f}")


def render_card(p, name):
    e = html.escape
    jersey = e(p["jersey"]).replace("-", "-<wbr>")
    injury = f'<span class="inj">{e(p["injury"])}</span>' if p["injury"] else ""
    if name == "adp":
        foot = (f'<div class="adp"><div><small>Y</small>{fmt(p["yahoo"])}</div><div><small>E</small>{fmt(p["espn"])}</div>'
                f'<div><small>AVG</small>{fmt(p["adp"])}</div></div>')
    else:
        foot = f'<div class="foot"><div>{p["fpts"]:,.0f}</div><div>{p["fppg"]:.1f}</div><div>{p["gp"]}</div></div>'
    return (f'<div class="card pos-{e(p["color_pos"])}" style="grid-column:{column_for(p["pick"]) + 1}"'
            f' title="{e(p["full_name"])}">'
            f'<div class="top"><span class="nm{" long" if len(p["jersey"]) > 12 else ""}">{jersey}</span>'
            f'<span class="pk">#{p["pick"]}</span></div>'
            f'<div class="meta">{e("/".join(p["positions"]))} · {e(p["team"])}{injury}</div>{foot}</div>')


def render_html(picks, name, src):
    b = dict(BOARDS[name])
    if PREFIX:
        b["title"] = f'{LEAGUE["name"]} · {b["title"]}'
        b["sub"] = b["sub"].replace("league scoring", f'{LEAGUE["name"]} scoring')
    rounds = max(p["round"] for p in picks)
    head = '<div class="hd">#</div>' + "".join(f'<div class="hd">{i}</div>' for i in range(1, TEAMS + 1))
    rows = [f'<div class="row">{head}</div>']
    for r in range(1, rounds + 1):
        rows.append(f'<div class="row"><div class="rd">{r}</div>{"".join(render_card(p, name) for p in picks if p["round"] == r)}</div>')
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        t = f.read()
    when = dt.datetime.fromtimestamp(os.path.getmtime(src)).strftime("data from %b %d, %Y").replace(" 0", " ")
    t = (t.replace("<title>Projections Draft Board</title>", f"<title>{b['title']}</title>")
          .replace("Projections Draft Board · Top 156", f"{b['title']} · Top 156")
          .replace("ranked by projected fantasy points (Book3 projections, 2026-27)", b["sub"])
          .replace("12-team snake ·", "12-team snake, third-round reversal ·")
          .replace("</style>", CARD_CSS, 1))
    nums = ("Yahoo ADP (Y) · ESPN ADP (E) · average ADP" if name == "adp" else
            "projected total fantasy points · fantasy points per game · projected games played")
    t = re.sub(r"<footer>.*?</footer>", f"<footer>Each card: {nums}; #N top right = overall pick. {SNAKE} OUT = ESPN "
               "injury status. Names are jersey surnames (an initial or full name when two players share one); colors "
               "follow each player's ESPN position.</footer>", t, flags=re.S)
    if name == "adp":
        t = t.replace("</style>", ADP_CSS, 1)
    return (t.replace("{{BOARD}}", "".join(rows)).replace("{{TOTAL}}", str(len(picks)))
             .replace("{{ROUNDS}}", str(rounds)).replace("{{UPDATED}}", when))


def build(name):
    picks, src = BOARDS[name]["load"]()
    csv_path = os.path.join(OUT_DIR, f"{PREFIX}{name}_draft_order.csv")
    html_path = os.path.join(OUT_DIR, f"{PREFIX}{name}_draft_board.html")
    png_path = os.path.join(OUT_DIR, f"{PREFIX}{name}_draft_board.png")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if name == "adp":
            w.writerow(["Rank", "Round", "Player", "Jersey", "Team", "Pos", "Status", "Color", "Yahoo ADP", "ESPN ADP", "ADP"])
            for p in picks:
                w.writerow([p["pick"], p["round"], p["full_name"], p["jersey"], p["team"], "/".join(p["positions"]),
                            p["injury"], p["color_pos"], fmt(p["yahoo"]), fmt(p["espn"]), fmt(p["adp"])])
        else:
            w.writerow(["Rank", "Round", "Player", "Jersey", "Team", "Pos", "Status", "Color", "Total FP", "FP/G", "GP",
                        "Base FP", "Bonus FP", "DD", "TD", "TECH", "EJ"])
            for p in picks:
                w.writerow([p["pick"], p["round"], p["full_name"], p["jersey"], p["team"], "/".join(p["positions"]),
                            p["injury"], p["color_pos"], round(p["fpts"], 1), round(p["fppg"], 2), p["gp"],
                            round(p["base"], 1), round(p["bonus"], 1), p["dd"], p["td"], p["tech"], p["ej"]])
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(render_html(picks, name, src))
    render_png(html_path, png_path, height=125 + max(p["round"] for p in picks) * (88 if name == "adp" else 79))
    print(f"{name}: {len(picks)} picks -> {png_path}")
    print("  injury tags:", ", ".join(f'{p["full_name"]} {p["injury"]}' for p in picks if p["injury"]) or "none")
    print("  top 5:", ", ".join(p["full_name"] for p in picks[:5]))


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--league" in args:
        i = args.index("--league")
        set_league(args[i + 1])
        del args[i:i + 2]
    for n in args or list(BOARDS):
        build(n)
