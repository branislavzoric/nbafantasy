#!/usr/bin/env python3
"""
build_tonight_carousels.py

Two Instagram carousels (1080x1350, bet_slides_template.html) from Odds/tonight/<date>/tonight.json
(build_tonight_bets.py), each split into sections with an explainer slide in front of every section:
  EuroLeague: cover · 1 value picks (props + game lines) · 2 game lines · 3 slips (4/5/6 legs) · 4 arbitrage
  Football:   cover · 1 value picks (Champions League, Championship) · 2 slips & arbitrage
Output: Odds/tonight/<date>/carousel_euroleague/NN_*.png, carousel_football/NN_*.png, carousels.json

Usage:
  python build_tonight_carousels.py [date] [--stake 10000]
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
TONIGHT_DIR = os.path.join(PROJECT_DIR, "Odds", "tonight")
BOOKS = {"soccerbet": "Soccerbet", "maxbet": "MaxBet", "admiralbet": "Admiral", "mozzartbet": "Mozzart",
         "superbet": "Superbet", "balkanbet": "Balkanbet"}
FOOT_MIN_EDGE = 1.0          # football picks under +1% are just fair prices
e = html.escape

EXTRA_CSS = """<style>
.sec { margin-top: 30px; display: grid; gap: 18px; }
.sec .no { font-family: var(--display); font-weight: 800; font-size: 120px; line-height: .8; color: var(--accent, var(--pop)); }
.sec h2 { margin: 0; font-family: var(--display); font-weight: 800; font-size: 54px; line-height: .88; text-transform: uppercase; }
.sec p { margin: 0; font-size: 17px; font-weight: 600; line-height: 1.45; color: rgba(255,255,255,.82); }
.sec ul { margin: 0; padding: 0; list-style: none; display: grid; gap: 10px; }
.sec li { position: relative; padding: 12px 14px 12px 34px; border-radius: 8px; background: rgba(255,255,255,.06);
  font-size: 15px; font-weight: 600; line-height: 1.4; color: rgba(255,255,255,.85); }
.sec li::before { content: ""; position: absolute; left: 14px; top: 19px; width: 8px; height: 8px; border-radius: 2px;
  background: var(--accent, var(--pop)); }
.sec li b { color: #fff; }
.toc { margin-top: 14px; display: grid; gap: 6px; }
.toc div { display: grid; grid-template-columns: 30px 1fr; gap: 8px; align-items: baseline; padding: 7px 10px;
  border-radius: 6px; background: rgba(255,255,255,.05); }
.toc b { font-family: var(--display); font-weight: 800; font-size: 22px; color: var(--accent, var(--pop)); }
.toc span { font-family: var(--display); font-weight: 700; font-size: 19px; text-transform: uppercase; }
.toc small { display: block; font-family: var(--font); font-size: 10.5px; font-weight: 600; text-transform: none; color: var(--muted); }
.games.two div { font-size: 15px; }
.games .lg { grid-column: 1 / -1; font-family: var(--font); font-size: 10px; font-weight: 800; letter-spacing: .1em;
  color: var(--accent, var(--pop)); margin-top: 4px; }
.slide.foot-deck { --accent: #5fd4ff; }
.ticket .pl.sm { font-size: 21px; }
.empty { margin-top: 18px; padding: 18px; border-radius: 10px; border: 1.5px dashed rgba(255,255,255,.25);
  font-size: 14px; font-weight: 600; line-height: 1.45; color: rgba(255,255,255,.8); }
</style>"""


def date_label(day):
    d = dt.date.fromisoformat(day)
    return f"{d.strftime('%a')} {d.strftime('%b')} {d.day}"


def book(b):
    return BOOKS.get(b, b)


def slide(kick, body, data, tag, cls=""):
    foot = (f'<div class="foot"><span><b>18+</b> · Odds as of {e(data["built"][11:])} and they change · '
            f'Not a guarantee, bet responsibly</span><span>{e(tag)}</span></div>')
    return (f'{EXTRA_CSS}<div class="slide {cls}"><div class="top"><span class="kick">{e(kick)}</span>'
            f'<span class="date">{e(date_label(data["date"]))}</span></div>{body}{foot}</div>')


def section(no, title, text, bullets):
    lis = "".join(f"<li>{b}</li>" for b in bullets)
    return (f'<div class="sec"><div class="no">{no:02d}</div><h2>{title}</h2><p>{text}</p>'
            f'<ul>{lis}</ul></div>')


def who(p):
    """Big line + bet line for a pick or slip leg."""
    if p["source"] == "EL props":
        bet = p["bet"].replace(p["player"] + " ", "").replace(" (N+ line)", "")
        parts = bet.split()                         # Over 3.5 Rebounds -> Rebounds Over 3.5
        bet = f'{" ".join(parts[2:])} {parts[0]} {parts[1]}'
        badge = '<i>N+ line</i>' if "(N+ line)" in p["bet"] else ""
        return e(p["player"]), f"{e(bet)}{badge}", e(p["match"])
    return e(p["match"]), e(p["bet"]), e(p["league"])


def ticket(i, p):
    big, bet, where = who(p)
    also = f' · also {", ".join(p["also_at"])}' if p.get("also_at") else ""
    sm = " sm" if len(big) > 20 else ""
    return (f'<div class="ticket"><span class="n">{i}</span><div class="who"><span class="pl{sm}">{big}</span>'
            f'<span class="bet">{bet}</span><span class="meta">{where} · {e(p["kickoff"][11:16])}{e(also)}</span></div>'
            f'<div class="px"><b>{p["odds"]:.2f}</b><span>{e(book(p["book"]))} · fair {p["fair_odds"]:.2f}</span>'
            f'<em>+{p["edge_pct"]:.1f}% edge</em></div></div>')


def tickets_slide(kick, head, sub, picks, data, tag, start=1, cls="", note=""):
    body = (f'<div class="head">{e(head)}<small>{e(sub)}</small></div>'
            f'<div class="tickets">{"".join(ticket(i, p) for i, p in enumerate(picks, start))}</div>'
            + (f'<div class="empty">{note}</div>' if note else ""))
    return slide(kick, body, data, tag, cls)


def slip_slide(s, n, data, tag, label=None, sub=None):
    size = {5: "--lp:8px;--lf:19px;--bf:11.5px;--of:18px", 6: "--lp:6px;--lf:17px;--bf:10.5px;--of:16px"}.get(min(n, 6), "")
    label = label or f"{n}-leg slip"
    sub = sub or f"One ticket at {e(s['book'])} · {n} different games · none of these bets is on the other tickets"
    legs = "".join(f'<div class="sl"><div><div class="pl">{who(l)[0]}</div><div class="bt">{who(l)[1]} · '
                   f'{e(l["match"]) if l["source"] == "EL props" else e(l["league"])}</div></div>'
                   f'<span class="o">{l["odds"]:.2f}</span></div>' for l in s["legs"])
    hit = 100 * s["hit_prob"]
    body = (f'<div class="head">{e(label)}<small>{sub}</small></div>'
            f'<div class="slip" style="{size}"><div class="sh"><b>{e(label)}</b><span>{e(s["book"])}</span></div>'
            f'{legs}<div class="tot"><span class="k">Total odds</span><span class="v">{s["total_odds"]:,.2f}</span></div></div>'
            f'<div class="facts"><div><span>Win chance</span><b>{hit:.1f}%</b></div>'
            f'<div><span>100 RSD returns</span><b>{100 * s["total_odds"]:,.0f}</b></div>'
            f'<div><span>Math edge</span><b>{100 * s["ev"]:+.0f}%</b></div></div>')
    return slide("Slips", body, data, tag)


def arb_slide(a, i, stake, data, tag):
    inv = sum(1 / l["odds"] for l in a["legs"])
    stakes = [round(stake * (1 / l["odds"]) / inv / 10) * 10 for l in a["legs"]]
    stakes[-1] = stake - sum(stakes[:-1])
    ret = min(s * l["odds"] for s, l in zip(stakes, a["legs"]))
    player, _, rest = a["market"].rpartition(" ")
    player, _, mk = player.rpartition(" ")
    ms = any("N+" in l["sel"] for l in a["legs"])
    legs = "".join(f'<div class="legx"><span>{e(l["sel"].replace(" (N+)", ""))} {e(rest)}</span><b>{l["odds"]:.2f}</b>'
                   f'<span class="bk">{e(l["book"])}</span><span class="st">Stake {s:,.0f}</span>'
                   f'{"<i>N+ milestone bet</i>" if "N+" in l["sel"] else ""}</div>' for l, s in zip(a["legs"], stakes))
    body = (f'<div class="arbcard"><div class="mk">#{i} · {e(a["match"])}</div>'
            f'<div class="pl">{e(player)}</div><div class="mk">{e(mk)} {e(rest)}</div>'
            f'<div class="profit"><b>+{a["profit_pct"]:.1f}%</b><span>locked-in return on the total stake</span></div>'
            f'<div class="legs">{legs}</div>'
            f'<div class="ret">With <b>{stake:,.0f} RSD</b> in total, either side returns at least <b>{ret:,.0f}</b> '
            f'(<b>+{ret - stake:,.0f}</b>).{" N+ = a milestone bet: check that the book settles it like a normal Over." if ms else ""}'
            f' Prices move fast, so place both legs together.</div></div>')
    return slide("Arbitrage", body, data, tag, "arb")

# -----------------------------------------------------------------------------

def basket_tickets(d):
    """Tonight's basketball tickets (4-leg slip, safer 5-20, long shot 20-100) when the 4/5/6 slips found nothing."""
    if any(d["slips"].get(str(n)) for n in (4, 5, 6)):
        return []
    return [t for t in (d.get("basket") or {}).get("tickets", []) if t.get("legs")]


def euroleague_deck(d, stake):
    tag = "EuroLeague · props + game lines"
    # women's EuroLeague games ("Galatasaray W") come in with the lines; the board is men's only
    d = {**d, "el_games": [g for g in d["el_games"] if not (g[1].endswith(" W") or g[2].endswith(" W"))]}
    picks, lines = d["el_picks"], d["per_source"]["EL lines"]
    games = "".join(f'<div><b>{e(k)}</b>{e(h)} – {e(a)}</div>' for k, h, a in d["el_games"])
    toc = "".join(f'<div><b>{n}</b><span>{t}<small>{s}</small></span></div>' for n, t, s in (
        (1, "Value picks", "Top 10 prices that beat the market"),
        (2, "Game lines", "Winner, handicap and total points"),
        (3, "Slips", "4-leg, safer and long-shot tickets" if basket_tickets(d) else "4, 5 and 6 legs, no bet repeated"),
        (4, "Arbitrage", "Both sides at different books, profit either way")))
    cover = (f'<h1>EuroLeague<br><span>tonight</span></h1>'
             f'<div class="lede">{len(d["el_games"])} games, every line compared across 6 Serbian bookmakers.</div>'
             f'<div class="games">{games}</div><div class="toc">{toc}</div>')
    s1 = section(1, "Value picks",
                 "Every bookmaker prices the same bet a little differently. We take the other books' odds, remove "
                 "their margin and get a <b>fair price</b>. A value pick is a price above that fair price.",
                 ["<b>Odds @ book</b>: where the best price is right now",
                  "<b>Fair</b>: what the bet is worth by the rest of the market. Win chance = 1 / fair",
                  "<b>Edge</b>: how much better than fair, e.g. +10% = 10% above fair",
                  "Player props + game lines, one pick per player. <b>N+ line</b> = a milestone bet (e.g. 2+ threes)"])
    s2 = section(2, "Game lines",
                 "The same method on the full-game markets: winner, handicap and total points. "
                 "All of them include overtime.",
                 ["Game lines are the most traded markets, so the edges here are <b>smaller</b> than on props",
                  "<b>Handicap</b>: the team's points are adjusted by the number (−6.5 = must win by 7+)",
                  "<b>Total</b>: combined points of both teams, overtime included"])
    s3 = section(3, "Slips",
                 "Three separate tickets. Each one is placed at <b>one bookmaker</b>, "
                 + ("with at most two legs per game: a 4-leg slip, a <b>safer</b> ticket (odds 5-20) and a "
                    "<b>long shot</b> (odds 20-100)." if basket_tickets(d) else "with one leg per game."),
                 ["<b>No bet appears on two tickets</b>, and no player is on two tickets",
                  "<b>Win chance</b> = all legs hitting, from the fair prices. These are long shots",
                  "<b>Math edge</b> assumes the fair prices are right. Errors multiply across legs"])
    s4 = section(4, "Arbitrage",
                 "Back the Over at one bookmaker and the Under at another. With the right split, "
                 "both sides pay the same, so the result doesn't matter.",
                 [f"Stakes on the slides are for <b>{stake:,.0f} RSD</b> in total",
                  "Big gaps usually mean one price is about to move: <b>place both legs together</b>",
                  "<b>N+</b> legs are milestone bets: check the book's void rules"])
    slides = [("01_cover", slide("EuroLeague · tonight's tips", cover, d, tag)),
              ("02_section-value", slide("Section 1", s1, d, tag)),
              ("03_picks-1-5", tickets_slide("Value picks", "Picks 1-5", "Best price at one book vs the fair odds "
                                             "from the others", picks[:5], d, tag)),
              ("04_picks-6-10", tickets_slide("Value picks", "Picks 6-10", "Best price at one book vs the fair odds "
                                              "from the others", picks[5:10], d, tag, 6)),
              ("05_section-lines", slide("Section 2", s2, d, tag)),
              ("06_game-lines", tickets_slide("Game lines", "Game lines", "Winner · handicap · total points, "
                                              "incl. overtime", lines[:5], d, tag)),
              ("07_section-slips", slide("Section 3", s3, d, tag))]
    if basket_tickets(d):
        for t in basket_tickets(d):
            n = len(t["legs"])
            sub = (f"One ticket at {e(t['book'])} · {n} legs · total odds "
                   + (f"{t['range'][0]}-{t['range'][1]}" if t.get("range") else "as high as the value allows")
                   + " · no bet on two tickets")
            slides.append((f"{len(slides) + 1:02d}_slip-{t['name']}", slip_slide(t, n, d, tag, t["label"], sub)))
    for n in (4, 5, 6):
        s = d["slips"].get(str(n))
        if s and not basket_tickets(d):
            slides.append((f"{len(slides) + 1:02d}_slip-{n}", slip_slide(s, n, d, tag)))
    slides.append((f"{len(slides) + 1:02d}_section-arbs", slide("Section 4", s4, d, tag, "arb")))
    for i, a in enumerate(d["arbitrage"][:5], 1):
        slides.append((f"{len(slides) + 1:02d}_arb-{i}", arb_slide(a, i, stake, d, tag)))
    return slides


def football_deck(d):
    tag = "Football · Champions League + Championship"
    picks = {lg: [p for p in ps if p["edge_pct"] >= FOOT_MIN_EDGE] for lg, ps in d["football_picks"].items()}
    by_lg = {}
    for k, lg, h, a in d["football_games"]:
        by_lg.setdefault(lg, []).append(f'<div><b>{e(k)}</b>{e(h)} – {e(a)}</div>')
    games = "".join(f'<div class="lg">{e(lg.upper())}</div>{"".join(rows)}' for lg, rows in by_lg.items())
    n_picks = sum(len(v) for v in picks.values())
    cover = (f'<h1>Football<br><span>tonight</span></h1>'
             f'<div class="lede">{len(d["football_games"])} matches, priced at 6 Serbian bookmakers. '
             f'{n_picks} value picks made the cut.</div><div class="games two">{games}</div>')
    s1 = section(1, "Value picks",
                 "We compare 1X2, double chance, over/under 2.5 goals and both teams to score across the books, "
                 "remove the margin and keep the prices that beat the <b>fair price</b>.",
                 ["Football markets are tight: tonight's edges are <b>small (1-3%)</b>, so stakes should be too",
                  "Only picks at least <b>+1%</b> above fair are shown, one per match",
                  "<b>Win chance</b> = 1 / fair odds"])
    s2 = section(2, "Slips & arbitrage",
                 "No football slip tonight: no single bookmaker had four value legs in different matches, and we "
                 "don't build tickets out of bets without an edge.",
                 ["<b>No arbitrage</b> in these matches either: the books agree too closely",
                  "Looking for slips? See tonight's <b>EuroLeague</b> post: 4, 5 and 6 legs",
                  "Prices change before kickoff: check the odds before you bet"])
    sub = "Best price at one book vs the fair odds from the others"
    slides = [("01_cover", slide("Football · tonight's tips", cover, d, tag, "foot-deck")),
              ("02_section-value", slide("Section 1", s1, d, tag, "foot-deck"))]
    for lg, ps in picks.items():
        if ps:
            n_games = sum(1 for g in d["football_games"] if g[1] == lg)
            note = (f"Only {len(ps)} of tonight's {n_games} {lg} matches had a price at least 1% above fair. "
                    f"The rest are priced almost identically at every book, so there's no value to point at."
                    if len(ps) < 4 else "")
            slides.append((f"{len(slides) + 1:02d}_{lg.lower().replace(' ', '-')}",
                           tickets_slide("Value picks", lg, sub, ps, d, tag, cls="foot-deck", note=note)))
    slides.append((f"{len(slides) + 1:02d}_section-slips", slide("Section 2", s2, d, tag, "foot-deck")))
    return slides, picks


def caption_el(d, stake):
    L = [f"🏀 EuroLeague tonight · {date_label(d['date'])}", "",
         "1️⃣ VALUE PICKS: the best price at one book vs the fair odds of the others (margin removed).", ""]
    for i, p in enumerate(d["el_picks"], 1):
        bet = p["bet"].replace(" (N+ line)", " (N+)") if p["source"] == "EL props" else f"{p['match']}: {p['bet']}"
        L.append(f"{i}. {bet} @ {p['odds']:.2f} {book(p['book'])} (+{p['edge_pct']:.1f}%)")
    L += ["", "2️⃣ GAME LINES (incl. OT):"]
    for p in d["per_source"]["EL lines"][:5]:
        L.append(f"• {p['match']}: {p['bet']} @ {p['odds']:.2f} {book(p['book'])} (+{p['edge_pct']:.1f}%)")
    L += ["", "3️⃣ SLIPS (one book each, no bet repeated across tickets):"]
    for t in basket_tickets(d):
        L.append(f"🎟️ {t['label']} @ {t['book']}: {len(t['legs'])} legs, odds {t['total_odds']:,.2f}, win chance ~{100 * t['hit_prob']:.1f}%")
    for n in (4, 5, 6):
        s = d["slips"].get(str(n))
        if s:
            L.append(f"🎟️ {n} legs @ {s['book']}: odds {s['total_odds']:,.2f}, win chance ~{100 * s['hit_prob']:.1f}%")
    L += ["", "4️⃣ ARBITRAGE: Over at one book + Under at another = same return either way."]
    for a in d["arbitrage"][:5]:
        legs = " / ".join(f"{l['sel']} {l['odds']:.2f} {l['book']}" for l in a["legs"])
        L.append(f"• {a['market']}: {legs} → +{a['profit_pct']:.1f}%")
    L += ["", f"Arb stakes on the slides are for {stake:,.0f} RSD. N+ = milestone bet, check void rules.",
          "18+. Odds change and nothing here is a guarantee. Bet only what you can afford to lose.", "",
          "#EuroLeague #PlayerProps #SportsBetting #BettingTips #Basketball"]
    return "\n".join(L)[:2200]


def caption_foot(d, picks):
    L = [f"⚽ Football tonight · Champions League + Championship · {date_label(d['date'])}", "",
         "VALUE PICKS: the best price at one book vs the fair odds of the others (margin removed).", ""]
    for lg, ps in picks.items():
        if ps:
            L.append(f"🏆 {lg}")
            L += [f"• {p['match']}: {p['bet']} @ {p['odds']:.2f} {book(p['book'])} · fair {p['fair_odds']:.2f} · "
                  f"+{p['edge_pct']:.1f}%" for p in ps]
            L.append("")
    L += ["Football edges are small tonight (1-3%), and there's no football slip or arbitrage: "
          "the books agree too closely. Slips are in our EuroLeague post.", "",
          "18+. Odds change and nothing here is a guarantee. Bet only what you can afford to lose.", "",
          "#ChampionsLeague #UCL #Championship #FootballTips #SportsBetting"]
    return "\n".join(L)[:2200]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", default=dt.date.today().isoformat())
    ap.add_argument("--stake", type=float, default=10000)
    args = ap.parse_args()
    folder = os.path.join(TONIGHT_DIR, args.date)
    with open(os.path.join(folder, "tonight.json"), encoding="utf-8") as f:
        d = json.load(f)
    with open(TEMPLATE, encoding="utf-8") as f:
        template = f.read().replace("{{FONTS}}", font_css())
    foot_slides, foot_picks = football_deck(d)
    decks = {"carousel_euroleague": euroleague_deck(d, args.stake), "carousel_football": foot_slides}
    browser = find_browser()
    work_dir = make_dir(tempfile.gettempdir())
    try:
        for deck, slides in decks.items():
            out = os.path.join(folder, deck)
            shutil.rmtree(out, ignore_errors=True)
            os.makedirs(out)
            for name, body in slides:
                page = template.replace("{{TITLE}}", e(name)).replace("{{BODY}}", body)
                render_png(browser, page, os.path.join(out, name + ".png"), work_dir, 540, 675)
            print(f"{deck}: {len(slides)} slides -> {out}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    with open(os.path.join(folder, "carousels.json"), "w", encoding="utf-8") as f:
        json.dump({"euroleague": {"images": [n + ".png" for n, _ in decks["carousel_euroleague"]],
                                  "caption": caption_el(d, args.stake)},
                   "football": {"images": [n + ".png" for n, _ in decks["carousel_football"]],
                                "caption": caption_foot(d, foot_picks)}}, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
