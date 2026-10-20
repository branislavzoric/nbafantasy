#!/usr/bin/env python3
"""
build_boxscore_table.py

One team's traditional box score as a table, laid out like nba.com's game box
score (PNG, 1080x1350): starters (with position) first, then the bench by minutes,
DNPs with the reason, and a team totals row. Columns: MIN, FGM, FGA, FG%, 3PM, 3PA,
3P%, FTM, FTA, FT%, OREB, DREB, REB, AST, STL, BLK, TO, PF, PTS, +/-, USG% and Yahoo
default FPTS. USG% = player FGA / team FGA (the user's definition, not the NBA formula).
The team leader in PTS / REB / AST / STL / BLK / FPTS is highlighted.

Data: nba_games / nba_boxscores (ESPN) -- run collect_boxscores.py first.

Usage:
  python build_boxscore_table.py 2026-10-08 CLE    # both teams of CLE's game
  python build_boxscore_table.py                    # every game from yesterday
Output: "Box Score Charts/<date>/<AWAY>-at-<HOME>_<TEAM>_table.png"
"""

import datetime as dt
import html
import os
import shutil
import sys
import tempfile

from build_boxscore_chart import OUT_DIR, games_on, load_game, played
from build_depth_chart import team_logo
from build_nightly_cards import ET, date_label, team_key
from build_player_cards import TEAM_NAMES, file_url, find_browser, font_css, make_dir, render_png, team_vars

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(PROJECT_DIR, "boxscore_table_template.html")
TABLE_W, TABLE_H = 540, 675
TABLE_ROOM = 556  # CSS px available for the table rows
HEAD_H, DNP_H = 18, 16

# nba.com starters order: forwards, center, guards
STARTER_ORDER = {"F": 0, "SF": 0, "PF": 0, "FC": 1, "C": 1, "G": 2, "GF": 2, "SG": 2, "PG": 2}
COLUMNS = ["MIN", "FGM", "FGA", "FG%", "3PM", "3PA", "3P%", "FTM", "FTA", "FT%",
           "OREB", "DREB", "REB", "AST", "STL", "BLK", "TO", "PF", "PTS", "+/-", "USG%", "FPTS"]
LEADER_KEYS = {"PTS": "pts", "REB": "reb", "AST": "ast", "STL": "stl", "BLK": "blk", "FPTS": "fpts"}


def pct(made, att):
    if not att:
        return "–"
    return "100" if made == att else f"{100 * made / att:.1f}"


def cells(p, team_fga):
    """Column values for one player line (or the totals dict)."""
    pm = p.get("plus_minus")
    usg = f"{100 * p['fga'] / team_fga:.0f}" if team_fga else "–"
    return {"MIN": p["min"], "FGM": p["fgm"], "FGA": p["fga"], "FG%": pct(p["fgm"], p["fga"]),
            "3PM": p["tpm"], "3PA": p["tpa"], "3P%": pct(p["tpm"], p["tpa"]),
            "FTM": p["ftm"], "FTA": p["fta"], "FT%": pct(p["ftm"], p["fta"]),
            "OREB": p["oreb"], "DREB": p["dreb"], "REB": p["reb"], "AST": p["ast"], "STL": p["stl"],
            "BLK": p["blk"], "TO": p["tov"], "PF": p["pf"], "PTS": p["pts"],
            "+/-": "" if pm is None else (f"+{pm}" if pm > 0 else str(pm)), "USG%": usg, "FPTS": f"{p['fpts']:.1f}"}


def dnp_reason(p):
    return (p.get("dnp_reason") or "coach's decision").capitalize()


def short(name):
    """'Shai Gilgeous-Alexander' -> 'S. Gilgeous-Alexander' (suffixes kept)."""
    parts = name.split()
    return name if len(parts) < 2 else f"{parts[0][0]}. {' '.join(parts[1:])}"


def table_html(players):
    e = html.escape
    active = [p for p in players if played(p)]
    starters = sorted((p for p in active if p["starter"]),
                      key=lambda p: STARTER_ORDER.get((p["position"] or "F").upper(), 0))
    bench = sorted((p for p in active if not p["starter"]), key=lambda p: (-p["min"], -p["fpts"]))
    dnp = [p for p in players if not played(p)]
    team_fga = sum(p["fga"] or 0 for p in active)
    best = {col: max((p[k] for p in active), default=0) for col, k in LEADER_KEYS.items()}

    def row(p, starter, i):
        vals = cells(p, team_fga)
        pos = (f'<i>{e(p["position"])}</i>' if starter and p["position"] not in (None, "NA") else "")
        tds = []
        for col in COLUMNS:
            v = vals[col]
            cls = []
            if col in LEADER_KEYS and best[col] > 0 and p[LEADER_KEYS[col]] == best[col]:
                cls.append("lead")
            if col == "USG%":
                cls.append("us")
            if col == "FPTS":
                cls.append("fp")
            if col == "+/-" and v[:1] in "+-" and v:
                cls.append("pos" if v[0] == "+" else "neg")
            tds.append(f'<td class="{" ".join(cls)}">{e(str(v))}</td>' if cls else f"<td>{e(str(v))}</td>")
        return (f'<tr class="pl{" z" if i % 2 else ""}"><th>{e(short(p["player_name"]))}{pos}</th>'
                f'{"".join(tds)}</tr>')

    cols = '<colgroup><col class="c-name">' + "".join(
        f'<col class="c-{"fp" if c in ("FPTS", "USG%") else "n"}">' for c in COLUMNS) + "</colgroup>"
    head = "".join(f"<td>{c}</td>" for c in COLUMNS)
    body = [f'<tr class="head"><th>Starters</th>{head}</tr>']
    body += [row(p, True, i) for i, p in enumerate(starters)]
    body.append(f'<tr class="head sub"><th>Bench</th>{head}</tr>')
    body += [row(p, False, i) for i, p in enumerate(bench)]
    body += [f'<tr class="dnp"><th>{e(short(p["player_name"]))}</th><td colspan="{len(COLUMNS)}">'
             f'DNP – {e(dnp_reason(p))}</td></tr>' for p in dnp]

    tot = {k: sum(p[k] or 0 for p in active) for k in
           ("fgm", "fga", "tpm", "tpa", "ftm", "fta", "oreb", "dreb", "reb", "ast", "stl", "blk", "tov", "pf",
            "pts", "fpts")}
    tot["min"] = sum(p["min"] for p in active)
    tot["plus_minus"] = None
    tv = cells(tot, team_fga)
    body.append(f'<tr class="tot"><th>Totals</th>{"".join(f"<td>{e(str(tv[c]))}</td>" for c in COLUMNS)}</tr>')
    # player rows share the room left after the header rows and the shorter DNP rows
    room = TABLE_ROOM - 2 * HEAD_H - len(dnp) * DNP_H
    row_h = min(30, room / (len(active) + 1))
    return f'<table style="--row-h:{row_h:.1f}px;--dnp-h:{DNP_H}px">{cols}{"".join(body)}</table>'


def render_html(team, game, players, template):
    e = html.escape
    home = game["home_team"] == players[0]["team"]
    mine, theirs = (game["home_score"], game["away_score"]) if home else (game["away_score"], game["home_score"])
    opp = team_key(game["away_team"] if home else game["home_team"])
    logo = team_logo(team)
    kicker = "Preseason box score" if game["season_type"] == "preseason" else "Box score"
    return (template
            .replace("{{TEAM_VARS}}", team_vars(team))
            .replace("{{TITLE}}", e(f"{team} box score"))
            .replace("{{TEAM_NAME}}", e(TEAM_NAMES.get(team, team)))
            .replace("{{LOGO}}", f'<img src="{e(file_url(logo))}" alt="">' if logo else "")
            .replace("{{KICKER}}", kicker)
            .replace("{{RESULT}}", f'<b>{"W" if mine > theirs else "L"}</b> {mine}–{theirs} '
                                   f'{"vs" if home else "@"} {e(opp)}')
            .replace("{{NOTE}}", f"{e(date_label(game['game_date']))} · Final")
            .replace("{{TABLE}}", table_html(players)))


def build_tables(game_date, team, browser, template):
    game, rows = load_game(game_date, team)
    away, home = team_key(game["away_team"]), team_key(game["home_team"])
    folder = os.path.join(OUT_DIR, game_date.isoformat())
    os.makedirs(folder, exist_ok=True)
    outputs = {}
    for side in (game["away_team"], game["home_team"]):
        code = team_key(side)
        players = [r for r in rows if r["team"] == side]
        png = os.path.join(folder, f"{away}-at-{home}_{code}_table.png")
        work_dir = make_dir(tempfile.gettempdir())
        try:
            render_png(browser, render_html(code, game, players, template), png, work_dir, TABLE_W, TABLE_H)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
        print(f"{code}: {png}")
        outputs[code] = png
    return outputs


def main():
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_DIR, ".env"))
    args = sys.argv[1:]
    game_date = (dt.date.fromisoformat(args[0]) if args
                 else dt.datetime.now(ET).date() - dt.timedelta(days=1))
    teams = [a.upper() for a in args[1:]] or games_on(game_date)
    browser = find_browser()
    if not browser:
        raise SystemExit("No Chrome/Edge found -- can't render.")
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        template = f.read().replace("{{FONTS}}", font_css())
    for team in teams:
        build_tables(game_date, team, browser, template)


if __name__ == "__main__":
    main()
