#!/usr/bin/env python3
"""
build_bet_carousels.py

Two Instagram carousels (1080x1350 slides, bet_slides_template.html) from
Odds/picks/<date>/picks.json (build_bet_picks.py):
  picks:     cover with the top-10 list, picks 1-5, picks 6-10, the 4-leg slip,
             the long slip
  arbitrage: cover, then one slide per arbitrage with the stake split for an
             example total stake
Output: Odds/picks/<date>/carousel_picks/NN_*.png, carousel_arbs/NN_*.png and
captions in carousels.json.

Usage:
  python build_bet_carousels.py [date] [--stake 10000]
"""

import argparse
import datetime as dt
import html
import json
import os
import shutil
import tempfile

from build_player_cards import find_browser, font_css, make_dir, render_png

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(PROJECT_DIR, "bet_slides_template.html")
PICKS_DIR = os.path.join(PROJECT_DIR, "Odds", "picks")
e = html.escape


def date_label(day):
    d = dt.date.fromisoformat(day)
    return f"{d.strftime('%a')} {d.strftime('%b')} {d.day}"


def foot(data):
    return (f'<div class="foot"><span><b>18+</b> · Odds as of {e(data["built"][11:])} and they change · '
            f'Not a guarantee, bet responsibly</span><span>EuroLeague player props</span></div>')


def bet_text(p):
    line = p["line"]
    ms = f'<i>{int(line + 0.5)}+</i>' if p.get("milestone") else ""
    return f'{e(p["market"])} {e(p["side"])} {line:g}{ms}'


def slide(kick, body, data, cls=""):
    return (f'<div class="slide {cls}"><div class="top"><span class="kick">{e(kick)}</span>'
            f'<span class="date">{e(date_label(data["date"]))}</span></div>{body}{foot(data)}</div>')


def cover_picks(data):
    games = "".join(f'<div><b>{e(k)}</b>{e(g)}</div>' for k, g in data["games"])
    rows = "".join(f'<div class="li"><span class="n">{i}</span><span class="p">{e(p["player"])}'
                   f'<small>{e(p["market"])} {e(p["side"])} {p["line"]:g}</small></span>'
                   f'<span class="o">{p["odds"]:.2f}</span><span class="e">+{p["edge_pct"]:.1f}%</span></div>'
                   for i, p in enumerate(data["picks"], 1))
    books = "".join(f'<span class="{"" if b["has_lines"] else "off"}">{e(b["book"])}</span>' for b in data["books"])
    n_books = sum(b["has_lines"] for b in data["books"])
    body = (f'<h1>Tonight\'s<br><span>top 10 picks</span></h1>'
            f'<div class="lede">Player props priced above the fair odds of the other {n_books - 1} books. '
            f'Edge = how much better the price is than fair.</div>'
            f'<div class="games">{games}</div><div class="list">{rows}</div><div class="books">{books}</div>')
    return slide("EuroLeague · bet picks", body, data)


def picks_slide(data, start):
    tickets = []
    for i, p in enumerate(data["picks"][start:start + 5], start + 1):
        also = f' · also {", ".join(p["also_at"])}' if p.get("also_at") else ""
        tickets.append(
            f'<div class="ticket"><span class="n">{i}</span><div class="who"><span class="pl">{e(p["player"])}</span>'
            f'<span class="bet">{bet_text(p)}</span>'
            f'<span class="meta">{e(p["team"] or "")}{" · " if p["team"] else ""}{e(p["game"])} · {e(p["kickoff"])}'
            f'{e(also)}</span></div>'
            f'<div class="px"><b>{p["odds"]:.2f}</b><span>{e(p["book_name"])} · fair {p["fair_odds"]:.2f}</span>'
            f'<em>+{p["edge_pct"]:.1f}% edge</em></div></div>')
    body = (f'<div class="head">Picks {start + 1}-{start + 5}<small>Best price at one book vs the fair odds '
            f'from the others · win chance = 1 / fair odds</small></div><div class="tickets">{"".join(tickets)}</div>')
    return slide("Top 10 picks", body, data)


def slip_slide(data, s, title, long_=False):
    n = len(s["legs"])
    size = "--lp:4px;--lf:14px;--bf:9.5px;--of:13px" if long_ else ""
    legs = "".join(f'<div class="sl"><div><div class="pl">{e(l["player"])}</div>'
                   f'<div class="bt">{bet_text(l)}</div></div><span class="o">{l["odds"]:.2f}</span></div>'
                   for l in s["legs"])
    body = (f'<div class="head">{e(title)}<small>All legs at {e(s["book_name"])}, one ticket, '
            f'{n} different players</small></div>'
            f'<div class="slip" style="{size}"><div class="sh"><b>{n}-leg slip</b><span>{e(s["book_name"])}</span></div>'
            f'{legs}<div class="tot"><span class="k">Total odds</span><span class="v">{s["total_odds"]:,.2f}</span></div></div>'
            f'<div class="facts"><div><span>Win chance</span><b>{100 * s["hit_prob"]:.{2 if s["hit_prob"] < .01 else 1}f}%</b></div>'
            f'<div><span>100 RSD returns</span><b>{100 * s["total_odds"]:,.0f}</b></div>'
            f'<div><span>Math edge</span><b>{s["ev_pct"]:+.0f}%</b></div></div>'
            f'<div class="note">Highest expected value we found with total odds of 10+. Edges multiply across '
            f'legs, so do errors in the fair prices. Legs in the same game can be linked, and some books '
            f'limit same-game combos.</div>')
    return slide("Bet slip", body, data)


def cover_arbs(data, stake):
    games = "".join(f'<div><b>{e(k)}</b>{e(g)}</div>' for k, g in data["games"])
    rows = "".join(f'<div class="li"><span class="n">{i}</span><span class="p">{e(a["player"])}'
                   f'<small>{e(a["market"])} {a["line"]:g}</small></span>'
                   f'<span class="o">{a["over_odds"]:.2f}/{a["under_odds"]:.2f}</span>'
                   f'<span class="e">+{a["profit_pct"]:.1f}%</span></div>'
                   for i, a in enumerate(data["arbitrage"], 1))
    body = (f'<h1>Top 5<br><span>arbitrages</span></h1>'
            f'<div class="lede">Back the Over at one book and the Under at another. Split the stake right and '
            f'both sides pay the same, whatever happens.</div>'
            f'<div class="games">{games}</div><div class="list">{rows}</div>')
    return slide("EuroLeague · arbitrage", body, data, "arb")


def arb_slide(data, a, i, stake):
    io, iu = 1 / a["over_odds"], 1 / a["under_odds"]
    so = round(stake * io / (io + iu) / 10) * 10
    su = stake - so
    ret = min(so * a["over_odds"], su * a["under_odds"])
    ms = '<i>N+ milestone bet</i>' if a["over_milestone"] else ""
    body = (f'<div class="arbcard"><div class="mk">#{i} · {e(a["team"] or "")}{" · " if a["team"] else ""}'
            f'{e(a["game"])} · {e(a["kickoff"])}</div>'
            f'<div class="pl">{e(a["player"])}</div><div class="mk">{e(a["market"])} {a["line"]:g}</div>'
            f'<div class="profit"><b>+{a["profit_pct"]:.1f}%</b><span>guaranteed return on the total stake</span></div>'
            f'<div class="legs"><div class="legx"><span>Over {a["line"]:g}</span><b>{a["over_odds"]:.2f}</b>'
            f'<span class="bk">{e(a["over_book"])}</span><span class="st">Stake {so:,.0f}</span>{ms}</div>'
            f'<div class="legx"><span>Under {a["line"]:g}</span><b>{a["under_odds"]:.2f}</b>'
            f'<span class="bk">{e(a["under_book"])}</span><span class="st">Stake {su:,.0f}</span></div></div>'
            f'<div class="ret">With <b>{stake:,.0f} RSD</b> in total, either side returns at least '
            f'<b>{ret:,.0f}</b> (<b>+{ret - stake:,.0f}</b>).'
            f'{" The Over is an N+ bet: check that the book settles it like a normal over (void rules)." if a["over_milestone"] else ""}'
            f' Prices move fast, so place both legs together.</div></div>')
    return slide("Arbitrage", body, data, "arb")


def caption_picks(data):
    lines = [f"🏀 EuroLeague tonight: top 10 player-prop picks ({date_label(data['date'])})", "",
             "Each pick is the best price at one book compared with the fair odds from the other books "
             "(margin removed). Edge = how much better than fair.", ""]
    for i, p in enumerate(data["picks"], 1):
        lines.append(f"{i}. {p['player']}: {p['market']} {p['side']} {p['line']:g} @ {p['odds']:.2f} "
                     f"({p['book_name']}) · fair {p['fair_odds']:.2f} · +{p['edge_pct']:.1f}%")
    for s, name in ((data["slip_4"], "4-leg slip"), (data["slip_long"], f"{len(data['slip_long']['legs'])}-leg slip")):
        lines += ["", f"🎟️ {name} @ {s['book_name']}: total odds {s['total_odds']:,.2f}, "
                      f"win chance about {100 * s['hit_prob']:.2g}%"]
    lines += ["", "18+. Odds change and nothing here is a guarantee. Bet only what you can afford to lose.", "",
              "#EuroLeague #PlayerProps #SportsBetting #BettingTips #Basketball"]
    return "\n".join(lines)[:2200]


def caption_arbs(data, stake):
    lines = [f"🏀 EuroLeague tonight: top 5 arbitrages ({date_label(data['date'])})", "",
             "Over at one book + Under at another = the same return either way.", ""]
    for i, a in enumerate(data["arbitrage"], 1):
        lines.append(f"{i}. {a['player']} {a['market']} {a['line']:g}: Over {a['over_odds']:.2f} @ {a['over_book']}"
                     f"{' (N+)' if a['over_milestone'] else ''} / Under {a['under_odds']:.2f} @ {a['under_book']} "
                     f"→ +{a['profit_pct']:.1f}%")
    lines += ["", f"Stakes on the slides are for {stake:,.0f} RSD in total. Prices move fast and books can limit "
                  "accounts that arb. N+ legs: check the settlement rules.", "",
              "18+. Bet responsibly.", "", "#EuroLeague #Arbitrage #SportsBetting #MatchedBetting #Basketball"]
    return "\n".join(lines)[:2200]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", default=dt.date.today().isoformat())
    ap.add_argument("--stake", type=float, default=10000)
    args = ap.parse_args()
    folder = os.path.join(PICKS_DIR, args.date)
    with open(os.path.join(folder, "picks.json"), encoding="utf-8") as f:
        data = json.load(f)
    with open(TEMPLATE, encoding="utf-8") as f:
        template = f.read().replace("{{FONTS}}", font_css())
    decks = {
        "carousel_picks": [("01_cover", cover_picks(data)), ("02_picks-1-5", picks_slide(data, 0)),
                           ("03_picks-6-10", picks_slide(data, 5)),
                           ("04_slip-4-legs", slip_slide(data, data["slip_4"], "4-leg slip · 10+ odds")),
                           ("05_slip-long", slip_slide(data, data["slip_long"],
                                                       f"{len(data['slip_long']['legs'])}-leg slip", True))],
        "carousel_arbs": [("01_cover", cover_arbs(data, args.stake))]
                         + [(f"{i + 1:02d}_arb-{i}", arb_slide(data, a, i, args.stake))
                            for i, a in enumerate(data["arbitrage"], 1)],
    }
    browser = find_browser()
    work_dir = make_dir(tempfile.gettempdir())
    try:
        for deck, slides in decks.items():
            out = os.path.join(folder, deck)
            os.makedirs(out, exist_ok=True)
            for name, body in slides:
                page = template.replace("{{TITLE}}", e(name)).replace("{{BODY}}", body)
                render_png(browser, page, os.path.join(out, name + ".png"), work_dir, 540, 675)
            print(f"{deck}: {len(slides)} slides -> {out}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    with open(os.path.join(folder, "carousels.json"), "w", encoding="utf-8") as f:
        json.dump({"picks": {"images": [n + ".png" for n, _ in decks["carousel_picks"]],
                             "caption": caption_picks(data)},
                   "arbs": {"images": [n + ".png" for n, _ in decks["carousel_arbs"]],
                            "caption": caption_arbs(data, args.stake)}}, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
