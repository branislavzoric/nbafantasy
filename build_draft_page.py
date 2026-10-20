#!/usr/bin/env python3
"""
build_draft_page.py

trebevicka23 draft review page: power rankings + one tab per team (court depth chart, strengths/weaknesses, pick grades).
Reads "Draft Analysis/trebevicka23_draft.json" (draft_analysis.py) and draft_notes.NOTES.

  python build_draft_page.py                  -> "Draft Analysis/trebevicka23_draft.html" (+ _site.html fragment)
  python build_draft_page.py --league boogie  -> "Draft Analysis/boogie_draft.html" (+ _site.html fragment)
"""

import json
import os
import re
import sys

from league_scoring import LEAGUES

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(PROJECT_DIR, "Draft Analysis", "trebevicka23_draft.json")
OUT = os.path.join(PROJECT_DIR, "Draft Analysis", "trebevicka23_draft.html")
HUES = [12, 205, 145, 280, 35, 330, 185, 95, 0, 240, 60, 160]


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


# league -> (file stem, page title, notes module)
PAGES = {"trebevicka": ("trebevicka23", "Trebevicka23", "draft_notes"),
         "boogie": ("boogie", "Boogie Nights", "draft_notes_boogie")}


def main():
    global SRC, OUT, SITE_OUT
    league = sys.argv[2] if sys.argv[1:2] == ["--league"] else "trebevicka"
    stem, title, mod = PAGES[league]
    NOTES = __import__(mod).NOTES
    SCORING_TEXT = LEAGUES[league]["text"]
    base = os.path.join(PROJECT_DIR, "Draft Analysis", stem + "_draft")
    SRC, OUT, SITE_OUT = base + ".json", base + ".html", base + "_site.html"
    d = json.load(open(SRC, encoding="utf-8"))
    teams = []
    for name, t in sorted(d["teams"].items(), key=lambda x: x[1]["rank"]):
        picks = [{k: p.get(k) for k in ("round", "overall", "player", "nba", "pos", "fppg", "gp", "proj_rank", "value",
                                         "slot", "injury", "pre", "note", "gp_note", "proj")} for p in t["picks"]]
        teams.append({"name": name, "slug": slug(name), "rank": t["rank"], "draft_slot": t["draft_slot"],
                      "season_pts": t["season_pts"], "lineup_fppg": t["lineup_fppg"], "games": t["lineup_games"],
                      "bench_lost": t["bench_lost"], "hue": HUES[t["draft_slot"] - 1], "picks": picks,
                      "notes": NOTES[name]})
    caps = [(p["player"].split()[-1], p["gp_note"]) for t in teams for p in t["picks"] if p.get("gp_note")]
    caps = "; ".join(f"{n}, {w}" for n, w in sorted(caps, key=lambda c: c[0]))
    data = {"teams": teams, "scoring": SCORING_TEXT, "caps": caps}
    page = TEMPLATE.replace("Trebevicka23", title).replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    open(OUT, "w", encoding="utf-8").write(page)
    dj = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    open(SITE_OUT, "w", encoding="utf-8").write(site_fragment(dj))
    print(OUT)
    print(SITE_OUT)


TEMPLATE = r"""<title>Trebevicka23 Draft Review</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@500;600;700;800&family=Barlow:wght@400;500;600;700&display=swap">
<style>
/* Layout: a draft-room scoreboard. Sticky team tab rail; each team tab = court depth chart beside the scouting report,
   then the pick ledger. Court stays maple in both themes; the page around it follows the theme. */
:root {
  --bg: #eef1f4; --panel: #ffffff; --ink: #14202b; --muted: #5b6875; --line: #d5dce3; --soft: #e3e8ed;
  --accent: #e2611b; --good: #13834f; --bad: #c2362b; --warn: #b07a00;
  --maple: #e6c38f; --maple2: #dcb47a; --courtline: #fffaf0; --apron: #1d2a36;
  --display: "Barlow Condensed", "Arial Narrow", "Roboto Condensed", sans-serif;
  --body: "Barlow", "Segoe UI", system-ui, sans-serif;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg: #0f161d; --panel: #17212b; --ink: #e8edf2; --muted: #93a1ae; --line: #2a3744; --soft: #1f2b37;
  --accent: #ff7a33; --good: #37c07f; --bad: #ff6a5c; --warn: #e3b13c; --apron: #0a1016; color-scheme: dark } }
:root[data-theme="dark"] {
  --bg: #0f161d; --panel: #17212b; --ink: #e8edf2; --muted: #93a1ae; --line: #2a3744; --soft: #1f2b37;
  --accent: #ff7a33; --good: #37c07f; --bad: #ff6a5c; --warn: #e3b13c; --apron: #0a1016; color-scheme: dark }
* { box-sizing: border-box }
body { background: var(--bg); color: var(--ink); font: 15px/1.5 var(--body); }
body::before { content: ""; position: fixed; inset: 0 0 auto; height: env(safe-area-inset-top, 0px); z-index: 10; background: var(--apron) }
.wrap { max-width: 1180px; margin: 0 auto; padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)); }
header.top { background: var(--apron); color: #f3f6f8; }
header.top .wrap { padding-block: 22px 18px; display: flex; flex-wrap: wrap; gap: 8px 24px; align-items: end; justify-content: space-between }
h1 { font: 800 clamp(30px, 6vw, 46px)/1 var(--display); letter-spacing: .01em; margin: 0; text-transform: uppercase; text-wrap: balance }
h1 b { color: var(--accent) }
.sub { color: #b7c3ce; font-size: 14px; max-width: 60ch }
nav.tabs { position: sticky; top: env(safe-area-inset-top, 0px); z-index: 5; background: var(--apron); border-top: 1px solid #2c3a47 }
nav.tabs .wrap { display: flex; gap: 6px; overflow-x: auto; padding-block: 8px; scrollbar-width: thin }
nav.tabs button { flex: none; font: 600 14px/1 var(--display); letter-spacing: .04em; text-transform: uppercase; color: #c9d3dc;
  background: transparent; border: 1px solid #344453; border-radius: 4px; padding: 8px 10px; cursor: pointer; display: flex; gap: 7px; align-items: center }
nav.tabs button .rk { font-weight: 800; color: #8fa0af; font-variant-numeric: tabular-nums }
nav.tabs button[aria-selected="true"] { background: var(--accent); border-color: var(--accent); color: #fff }
nav.tabs button[aria-selected="true"] .rk { color: #fff }
nav.tabs button:focus-visible, .lnk:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px }
main .wrap { padding-block: 22px 40px }
h2 { font: 700 24px/1.1 var(--display); text-transform: uppercase; letter-spacing: .02em; margin: 0 0 12px; text-wrap: balance }
h3 { font: 700 15px/1.2 var(--display); text-transform: uppercase; letter-spacing: .08em; margin: 0 0 8px; color: var(--muted) }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 16px; min-width: 0 }
.scroll { overflow-x: auto }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums }
th { font: 600 12px/1.2 var(--display); letter-spacing: .08em; text-transform: uppercase; color: var(--muted); text-align: left; padding: 8px 8px; border-bottom: 2px solid var(--line); white-space: nowrap }
td { padding: 8px; border-bottom: 1px solid var(--soft); vertical-align: top }
td.n, th.n { text-align: right; white-space: nowrap }
.rkbig { font: 800 26px/1 var(--display); color: var(--accent) }
.lnk { background: none; border: 0; padding: 0; font: inherit; color: var(--ink); font-weight: 700; cursor: pointer; text-align: left; text-decoration: underline; text-decoration-color: var(--line); text-underline-offset: 3px }
.chip { display: inline-block; font: 700 12px/1 var(--display); letter-spacing: .04em; padding: 3px 6px; border-radius: 3px; white-space: nowrap }
.v-pos { background: color-mix(in srgb, var(--good) 16%, transparent); color: var(--good) }
.v-neg { background: color-mix(in srgb, var(--bad) 16%, transparent); color: var(--bad) }
.v-0 { background: var(--soft); color: var(--muted) }
.inj { background: color-mix(in srgb, var(--warn) 20%, transparent); color: var(--warn) }
.out { background: color-mix(in srgb, var(--bad) 20%, transparent); color: var(--bad) }
.small { font-size: 13px; color: var(--muted) }
.grid2 { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 16px }
.method { columns: 2 320px; column-gap: 28px; font-size: 14px; color: var(--muted) }
.method p { margin: 0 0 10px; break-inside: avoid }
.method b { color: var(--ink) }
/* draft board */
.board { display: grid; grid-template-columns: 34px repeat(12, minmax(84px, 1fr)); gap: 3px; min-width: 1080px; font-size: 12px }
.board .hd { font: 700 11px/1.15 var(--display); text-transform: uppercase; letter-spacing: .04em; color: var(--muted); padding: 4px 3px; align-self: end }
.board .rd { font: 700 13px/1 var(--display); color: var(--muted); align-self: center; text-align: center }
.board .c { border-radius: 3px; padding: 5px 5px 4px; background: var(--soft); line-height: 1.15; min-height: 44px }
.board .c b { display: block; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis }
.board .c span { display: flex; justify-content: space-between; color: var(--muted); font-variant-numeric: tabular-nums; margin-top: 2px }
.board .c.g { background: color-mix(in srgb, var(--good) calc(var(--k) * 1%), var(--soft)) }
.board .c.r { background: color-mix(in srgb, var(--bad) calc(var(--k) * 1%), var(--soft)) }
.legend { display: flex; gap: 14px; flex-wrap: wrap; font-size: 13px; color: var(--muted); margin-top: 10px }
.legend i { display: inline-block; width: 12px; height: 12px; border-radius: 2px; vertical-align: -1px; margin-right: 5px }
/* team tab */
.thead { display: flex; flex-wrap: wrap; gap: 10px 26px; align-items: end; margin-bottom: 16px }
.thead .big { font: 800 64px/0.85 var(--display); color: var(--accent) }
.thead h2 { font-size: clamp(28px, 5vw, 40px); margin: 0 }
.stats { display: flex; flex-wrap: wrap; gap: 8px 22px }
.stat small { display: block; font: 600 11px/1.2 var(--display); letter-spacing: .08em; text-transform: uppercase; color: var(--muted) }
.stat b { font: 700 22px/1.1 var(--display); font-variant-numeric: tabular-nums }
.tlay { display: grid; grid-template-columns: minmax(0, 560px) minmax(0, 1fr); gap: 18px; align-items: start }
.verdict { font-size: 17px; line-height: 1.45; margin: 0 0 14px; font-weight: 500 }
ul.pts { margin: 0 0 14px; padding: 0; list-style: none; display: grid; gap: 8px }
ul.pts li { padding-left: 22px; position: relative }
ul.pts li::before { position: absolute; left: 0; top: 0; font: 800 15px/1.5 var(--display) }
ul.plus li::before { content: "+"; color: var(--good) }
ul.minus li::before { content: "−"; color: var(--bad) }
/* court */
.courtbox { background: var(--apron); border-radius: 8px; padding: 14px 12px; color: #f3f6f8 }
.court { position: relative; width: 100%; max-width: 100%; aspect-ratio: 50 / 94; margin: 0 auto }
.court svg { position: absolute; inset: 0; width: 100%; height: 100% }
.tok { position: absolute; transform: translate(-50%, -50%); width: 31%; background: #fbf7ef; color: #14202b; border-radius: 5px;
  box-shadow: 0 2px 6px rgba(0,0,0,.28); padding: 4px 6px 5px; text-align: center; border-top: 4px solid hsl(var(--h) 65% 42%) }
.tok .sl { font: 800 11px/1 var(--display); letter-spacing: .06em; color: hsl(var(--h) 60% 34%) }
.tok .nm { font: 700 clamp(12px, 2.6vw, 15px)/1.05 var(--display); text-transform: uppercase; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin: 2px 0 1px }
.tok .mt { font: 600 11px/1.1 var(--body); color: #4f5b66; font-variant-numeric: tabular-nums }
.tok .mt b { color: #14202b }
.tok .dot { position: absolute; top: -7px; right: -5px; font: 800 10px/1 var(--display); padding: 2px 4px; border-radius: 3px; background: #b07a00; color: #fff }
.tok .dot.o { background: #c2362b }
.unit { position: absolute; left: 6%; font: 800 11px/1 var(--display); letter-spacing: .12em; text-transform: uppercase; color: rgba(20,32,43,.55) }
.bench { margin-top: 12px; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px }
.bench .tok { position: relative; transform: none; width: auto; left: auto; top: auto }
.bench h3 { grid-column: 1 / -1; color: #b7c3ce; margin: 0 }
.ledger td .who { font-weight: 700 }
.ledger td .why { display: block; font-size: 12.5px; color: var(--muted); max-width: 46ch }
@media (max-width: 860px) { .tlay, .grid2 { grid-template-columns: minmax(0, 1fr) } .court { max-width: 460px } }
@media (prefers-reduced-motion: no-preference) { .tabpane { animation: fade .18s ease-out } @keyframes fade { from { opacity: .4 } to { opacity: 1 } } }
</style>

<header class="top"><div class="wrap">
  <div><h1>Trebevicka23 <b>draft review</b></h1></div>
  <div class="sub">12 teams, 13 rounds, 156 picks. Graded with a consensus of ESPN, Yahoo and FantasyPros 2026-27 projections under the league's scoring, played out over the real 2026-27 schedule.</div>
</div></header>
<nav class="tabs" aria-label="Teams"><div class="wrap" id="tabs" role="tablist"></div></nav>
<main><div class="wrap" id="view"></div></main>
<script type="application/json" id="data">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById("data").textContent);
const T = D.teams;
const el = (tag, cls, txt) => { const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };
const fmt = n => n.toLocaleString("en-US");
const last = n => { const p = n.replace(/ (Jr\.|Sr\.|II|III|IV)$/, "").split(" "); return p.length > 1 ? p.slice(1).join(" ") : n; };
const vchip = v => { const s = el("span", "chip " + (v >= 3 ? "v-pos" : v <= -3 ? "v-neg" : "v-0"), (v > 0 ? "+" : v < 0 ? "−" : "±") + Math.abs(v)); s.title = "Draft pick minus projected rank"; return s; };
const INJ = { DAY_TO_DAY: ["DTD", "inj"], OUT: ["OUT", "out"], INJURY_RESERVE: ["IR", "out"] };

function tabs(active) {
  const bar = document.getElementById("tabs"); bar.textContent = "";
  const mk = (id, label, rk) => {
    const b = el("button"); b.type = "button"; b.setAttribute("role", "tab"); b.setAttribute("aria-selected", id === active);
    b.id = "tab-" + id;
    if (rk) b.append(el("span", "rk", rk));
    b.append(document.createTextNode(label));
    b.onclick = () => go(id);
    bar.append(b);
  };
  mk("rankings", "Power rankings");
  T.forEach(t => mk(t.slug, t.name, "#" + t.rank));
  const cur = document.getElementById("tab-" + active); if (cur) cur.scrollIntoView({ block: "nearest", inline: "nearest" });
}

function go(id) {
  if (id !== "rankings" && !T.find(t => t.slug === id)) id = "rankings";
  tabs(id);
  const v = document.getElementById("view"); v.textContent = "";
  const pane = el("div", "tabpane"); v.append(pane);
  if (id === "rankings") rankings(pane); else team(pane, T.find(t => t.slug === id));
  try { history.replaceState(null, "", "#" + id); } catch (e) {}
  try { localStorage.setItem("tab", id); } catch (e) {}
  window.scrollTo({ top: 0 });
}

function rankings(p) {
  const top = T[0].season_pts;
  const sec = el("section", "panel"); p.append(sec);
  sec.append(el("h2", null, "Power rankings"));
  const sc = el("div", "scroll"); sec.append(sc);
  const tb = el("table"); sc.append(tb);
  tb.innerHTML = '<thead><tr><th>#</th><th>Team</th><th class="n">Draft slot</th><th class="n">Season pts</th><th class="n">Gap</th><th class="n">Full-strength FP/G</th><th class="n">Lineup games</th><th>Best value</th><th>Biggest reach</th></tr></thead>';
  const body = el("tbody"); tb.append(body);
  T.forEach(t => {
    const tr = el("tr");
    const c = (txt, cls) => { const td = el("td", cls, txt); tr.append(td); return td; };
    c(null).append(el("span", "rkbig", t.rank));
    const nm = c(null); const b = el("button", "lnk", t.name); b.type = "button"; b.onclick = () => go(t.slug); nm.append(b);
    nm.append(el("div", "small", (t.notes.verdict.match(/^.*?[.!?](?=\s+[A-Z]|$)/) || [t.notes.verdict])[0]));
    c(t.draft_slot, "n"); c(fmt(t.season_pts), "n"); c(t.rank === 1 ? "—" : "−" + fmt(top - t.season_pts), "n");
    c(t.lineup_fppg.toFixed(1), "n"); c(t.games, "n");
    const ps = t.picks.slice().sort((a, b) => b.value - a.value);
    const bv = c(null); bv.append(document.createTextNode(ps[0].player + " "), vchip(ps[0].value));
    const w = ps[ps.length - 1]; const rv = c(null); rv.append(document.createTextNode(w.player + " "), vchip(w.value));
    body.append(tr);
  });
  sec.append(el("p", "small", "Season pts = projected fantasy points from the 10 starting slots over the 2026-27 schedule (average of 120 simulated seasons). Teams within about 150 points of each other are effectively tied."));

  const bsec = el("section", "panel"); bsec.style.marginTop = "16px"; p.append(bsec);
  bsec.append(el("h2", null, "Draft board"));
  bsec.append(el("p", "small", "Each pick coloured by value: draft position minus projected rank. Green = taken later than the projections rank him, red = taken earlier."));
  const sc2 = el("div", "scroll"); bsec.append(sc2);
  const bd = el("div", "board"); sc2.append(bd);
  const order = T.slice().sort((a, b) => a.draft_slot - b.draft_slot);
  bd.append(el("div", "hd", "Rd"));
  order.forEach(t => { const h = el("div", "hd", t.name); bd.append(h); });
  for (let r = 1; r <= 13; r++) {
    bd.append(el("div", "rd", r));
    order.forEach(t => {
      const pk = t.picks.find(x => x.round === r);
      const k = Math.min(Math.abs(pk.value), 60) / 60 * 55 + 8;
      const c = el("div", "c" + (pk.value >= 3 ? " g" : pk.value <= -3 ? " r" : "")); c.style.setProperty("--k", k.toFixed(0));
      c.title = `${pk.player} · pick ${pk.overall} · projected rank ${pk.proj_rank} · ${pk.fppg} FP/G`;
      c.append(el("b", null, pk.player));
      const s = el("span"); s.append(el("i", null, "#" + pk.overall), el("i", null, (pk.value > 0 ? "+" : "") + pk.value)); c.append(s);
      bd.append(c);
    });
  }
  const lg = el("div", "legend");
  lg.innerHTML = '<span><i style="background:color-mix(in srgb,var(--good) 45%,var(--soft))"></i>value pick</span><span><i style="background:color-mix(in srgb,var(--bad) 45%,var(--soft))"></i>reach</span><span><i style="background:var(--soft)"></i>within 2 spots</span>';
  bsec.append(lg);

  const m = el("section", "panel"); m.style.marginTop = "16px"; p.append(m);
  m.append(el("h2", null, "How this was graded"));
  const md = el("div", "method"); m.append(md);
  [["Scoring.", " The league's Yahoo points scoring: " + D.scoring + ". The bonuses use each player's 2025-26 rate."],
   ["Projections.", " A consensus of ESPN, Yahoo and FantasyPros 2026-27 projections: the average of each source's FP/G and of its projected games. No single source wins every season, and averaging them cancels out each source's biases. That makes a consensus the most reliable choice for a draft review."],
   ["Injuries.", " Projected games are capped for injuries with a reported timeline: " + D.caps + ". Day-to-day tags come from ESPN's injury status."],
   ["Lineups.", " We assume Yahoo's default 13-man roster: PG, SG, G, SF, PF, F, C, C, Util, Util and 3 bench. Every day of the real 2026-27 schedule, the players whose NBA team plays are available with probability projected games ÷ team games. The best legal lineup is then set. Points from players left on the bench because the slots are full are lost."],
   ["Value.", " Value is draft position minus consensus projected rank among all players. Pick numbers follow the league's snake with third-round reversal: round 1 runs 1→12, rounds 2 and 3 run 12→1, then the order alternates. Preseason FP/G (from the October box scores) is shown for context only. Starters usually played limited minutes in preseason."],
   ["Limits.", " These are projections, not results. Roster moves, trades and the waiver wire will change everything. Close teams (gaps under ~150 points) are effectively tied."]
  ].forEach(([b, t]) => { const q = el("p"); q.append(el("b", null, b), document.createTextNode(t)); md.append(q); });
}

// court spots (x %, y %) for the 10 starting slots: starting five attacks the top basket, second unit the bottom one
const SPOTS = { PG: [50, 41], SG: [19, 33], SF: [81, 25], PF: [24, 16], C: [62, 8],
                G: [50, 59], Util: [19, 67], Util2: [81, 75], F: [24, 84], C2: [62, 92] };

function token(pk, slot, hue) {
  const t = el("div", "tok"); t.style.setProperty("--h", hue);
  t.append(el("div", "sl", slot + " · " + pk.pos.join("/")));
  const nm = el("div", "nm", last(pk.player)); nm.title = pk.player; t.append(nm);
  const mt = el("div", "mt"); mt.append(el("b", null, pk.fppg.toFixed(1)), document.createTextNode(" FP/G · " + pk.nba)); t.append(mt);
  const inj = INJ[pk.injury]; if (inj) { const d = el("span", "dot" + (inj[1] === "out" ? " o" : ""), inj[0]); t.append(d); }
  return t;
}

function courtSVG(hue) {
  const paint = `hsl(${hue} 45% 34%)`;
  return `<svg viewBox="-2 -2 54 98" preserveAspectRatio="none" aria-hidden="true">
  <rect x="-2" y="-2" width="54" height="98" fill="#1d2a36"/>
  <rect x="0" y="0" width="50" height="94" fill="#e6c38f"/>
  <g stroke="#cfa66c" stroke-width=".08" opacity=".7">${Array.from({length: 24}, (_, i) => `<line x1="${i * 2.1 + 1}" y1="0" x2="${i * 2.1 + 1}" y2="94"/>`).join("")}</g>
  <g fill="${paint}" opacity=".9"><rect x="17" y="0" width="16" height="19"/><rect x="17" y="75" width="16" height="19"/></g>
  <g fill="none" stroke="#fffaf0" stroke-width=".35">
    <rect x="0" y="0" width="50" height="94"/>
    <line x1="0" y1="47" x2="50" y2="47"/>
    <circle cx="25" cy="47" r="6"/><circle cx="25" cy="47" r="2" fill="${paint}"/>
    <rect x="17" y="0" width="16" height="19"/><rect x="17" y="75" width="16" height="19"/>
    <circle cx="25" cy="19" r="6"/><circle cx="25" cy="75" r="6"/>
    <path d="M3 0 V14 A23.75 23.75 0 0 0 47 14 V0"/>
    <path d="M3 94 V80 A23.75 23.75 0 0 1 47 80 V94"/>
    <path d="M21 5.25 A4 4 0 0 0 29 5.25"/><path d="M21 88.75 A4 4 0 0 1 29 88.75"/>
  </g>
  <g stroke="#fffaf0" stroke-width=".35"><line x1="22" y1="4" x2="28" y2="4"/><line x1="22" y1="90" x2="28" y2="90"/></g>
  <g fill="none" stroke="${paint}" stroke-width=".35"><circle cx="25" cy="5.25" r=".75"/><circle cx="25" cy="88.75" r=".75"/></g>
</svg>`;
}

function team(p, t) {
  const head = el("div", "thead"); p.append(head);
  head.append(el("div", "big", "#" + t.rank));
  const hw = el("div"); hw.style.minWidth = "0"; head.append(hw);
  hw.append(el("h2", null, t.name));
  const st = el("div", "stats"); hw.append(st);
  const top = T[0].season_pts;
  [["Draft slot", t.draft_slot], ["Season pts", fmt(t.season_pts)], ["Gap to #1", t.rank === 1 ? "—" : "−" + fmt(top - t.season_pts)],
   ["Full-strength FP/G", t.lineup_fppg.toFixed(1)], ["Lineup games", t.games], ["Lost to bench", fmt(t.bench_lost) + " pts"]]
    .forEach(([k, v]) => { const s = el("div", "stat"); s.append(el("small", null, k), el("b", null, v)); st.append(s); });

  const lay = el("div", "tlay"); p.append(lay);
  const cb = el("div", "courtbox"); lay.append(cb);
  const court = el("div", "court"); court.innerHTML = courtSVG(t.hue); cb.append(court);
  const u1 = el("div", "unit", "Starting five"); u1.style.top = "47.6%"; u1.style.transform = "translateY(-130%)"; court.append(u1);
  const u2 = el("div", "unit", "Second unit"); u2.style.top = "48.6%"; court.append(u2);
  const seen = {};
  t.picks.filter(x => x.slot !== "BN").forEach(pk => {
    let key = pk.slot; seen[key] = (seen[key] || 0) + 1;
    if (seen[key] === 2) key = key === "Util" ? "Util2" : key === "C" ? "C2" : key;
    const [x, y] = SPOTS[key];
    const tk = token(pk, pk.slot, t.hue); tk.style.left = x + "%"; tk.style.top = y + "%"; court.append(tk);
  });
  const bench = el("div", "bench"); cb.append(bench);
  bench.append(el("h3", null, "Bench"));
  t.picks.filter(x => x.slot === "BN").forEach(pk => bench.append(token(pk, "BN", t.hue)));
  cb.append(el("p", "small", "Best lineup when everyone is healthy. The starting five holds PG/SG/SF/PF/C; the second unit holds G, F, the second C and both Util slots."));
  cb.lastChild.style.cssText = "color:#b7c3ce;margin:10px 2px 0";

  const rep = el("div", "panel"); lay.append(rep);
  rep.append(el("p", "verdict", t.notes.verdict));
  rep.append(el("h3", null, "Strong points"));
  const pl = el("ul", "pts plus"); t.notes.strong.forEach(s => pl.append(el("li", null, s))); rep.append(pl);
  rep.append(el("h3", null, "Weak points"));
  const mi = el("ul", "pts minus"); t.notes.weak.forEach(s => mi.append(el("li", null, s))); rep.append(mi);

  const lg = el("section", "panel"); lg.style.marginTop = "16px"; p.append(lg);
  lg.append(el("h2", null, "Pick by pick"));
  const sc = el("div", "scroll"); lg.append(sc);
  const tb = el("table", "ledger"); sc.append(tb);
  tb.innerHTML = '<thead><tr><th class="n">Rd</th><th class="n">Pick</th><th>Player</th><th>Pos</th><th>Slot</th><th class="n">FP/G</th><th class="n">GP</th><th class="n">Proj rank</th><th class="n">Value</th><th class="n">Preseason</th><th class="n">ESPN · Yahoo · FPros</th></tr></thead>';
  const body = el("tbody"); tb.append(body);
  t.picks.forEach(pk => {
    const tr = el("tr");
    const c = (txt, cls) => { const td = el("td", cls, txt); tr.append(td); return td; };
    c(pk.round, "n"); c(pk.overall, "n");
    const w = c(null); w.append(el("span", "who", pk.player), document.createTextNode(" " + pk.nba + " "));
    const inj = INJ[pk.injury]; if (inj) w.append(el("span", "chip " + inj[1], inj[0]));
    const why = pk.gp_note || pk.note; if (why) w.append(el("span", "why", why));
    c(pk.pos.join(", ")); c(pk.slot);
    c(pk.fppg.toFixed(1), "n"); c(String(Math.round(pk.gp)), "n"); c(pk.proj_rank, "n");
    c(null, "n").append(vchip(pk.value));
    c(pk.pre ? `${pk.pre.fppg} (${pk.pre.g}g, ${pk.pre.mpg} min)` : "—", "n");
    const src = ["espn", "yahoo", "fantasypros"].map(k => pk.proj[k] ? pk.proj[k].fppg.toFixed(1) : "—").join(" · ");
    c(src, "n");
    body.append(tr);
  });
}

let start = (location.hash || "").slice(1);
if (!start) { try { start = localStorage.getItem("tab") || ""; } catch (e) {} }
go(start || "rankings");
window.addEventListener("hashchange", () => go(location.hash.slice(1)));
</script>
"""



# ----------------------------------------------------------------------------- b3basketball.com fragment

SITE_OUT = os.path.join(PROJECT_DIR, "Draft Analysis", "trebevicka23_draft_site.html")
SKIP_SEL = ("body::before", "header.top", "h1", "h1 b", ".sub")


def scope_css(css, root=".drv"):
    """Prefix every selector with the wrapper class; light theme only (the site has no dark mode)."""
    out, i = [], 0
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)

    def blocks(s):
        res, depth, start, j = [], 0, 0, 0
        while j < len(s):
            if s[j] == "{":
                depth += 1
            elif s[j] == "}":
                depth -= 1
                if depth == 0:
                    res.append(s[start:j + 1].strip())
                    start = j + 1
            j += 1
        return res

    def pref(sel):
        sels = []
        for x in sel.split(","):
            x = x.strip()
            if x in SKIP_SEL:
                continue
            if x in (":root", "body", "*"):
                sels.append(root if x != "*" else f"{root}, {root} *")
            else:
                sels.append(f"{root} {x}")
        return ", ".join(sels)

    for b in blocks(css):
        head, body = b.split("{", 1)
        head = head.strip()
        if head.startswith("@media (prefers-color-scheme") or head.startswith(":root[data-theme"):
            continue
        if head.startswith("@media"):
            inner = []
            for ib in blocks(body.rsplit("}", 1)[0]):
                ih, ibody = ib.split("{", 1)
                if ih.strip().startswith("@keyframes"):
                    inner.append(ib)
                elif pref(ih):
                    inner.append(pref(ih) + " {" + ibody)
            out.append(head + " { " + " ".join(inner) + " }")
            continue
        p = pref(head)
        if p:
            out.append(p + " {" + body)
    return "\n".join(out)


def site_fragment(data_json):
    css = TEMPLATE.split("<style>", 1)[1].split("</style>", 1)[0]
    css = scope_css(css).replace('"Segoe UI", system-ui', 'Roboto, "Segoe UI", system-ui').replace('--body: "Barlow",', "--body:")
    css += ("\n.drv { font-size: 15px; scroll-margin-top: 64px; margin-top: 18px }"
            "\n.drv nav.tabs { top: 64px; border-top: 0; border-radius: 6px }"
            "\n.drv main .wrap, .drv nav.tabs .wrap { padding-left: 0; padding-right: 0 }"
            "\n.drv nav.tabs .wrap { padding-inline: 8px }"
            "\n.drv .stats .stat { background: none; border: 0; padding: 0; color: var(--ink) }"
            "\n.drv .stat b { color: var(--ink) } .drv .stat small { color: var(--muted) }")
    js = TEMPLATE.split('<script>\n', 1)[1].rsplit("</script>", 1)[0]
    js = js.replace("window.scrollTo({ top: 0 });",
                    'if (window.__drvInit) document.getElementById("drv").scrollIntoView(); window.__drvInit = 1;')
    js = js.replace('localStorage.setItem("tab", id)', 'localStorage.setItem("drvtab", id)').replace(
        'localStorage.getItem("tab")', 'localStorage.getItem("drvtab")')
    return (f"<style>\n{css}\n</style>\n"
            '<div class="drv" id="drv"><nav class="tabs" aria-label="Teams"><div class="wrap" id="tabs" role="tablist"></div></nav>'
            '<main><div class="wrap" id="view"></div></main></div>\n'
            f'<script type="application/json" id="data">{data_json}</script>\n<script>\n{js}</script>\n')


if __name__ == "__main__":
    main()
