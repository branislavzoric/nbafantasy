"""Settle Odds/tonight/<day>/football_card.json from Odds/results/<day>/football_scores.json -> results.json + ledger."""
import datetime as dt
import json
import os
import re
import sys
import unicodedata

sys.stdout.reconfigure(encoding="utf-8")
os.chdir(r"C:\Users\Zoric\nbafantasy")
day = sys.argv[1]
card = json.load(open(f"Odds/tonight/{day}/football_card.json", encoding="utf-8"))
scores = json.load(open(f"Odds/results/{day}/football_scores.json", encoding="utf-8"))


def fold(s):
    return set(re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower())) - {
        "fc", "sc", "sv", "the", "city", "town", "united", "de", "as", "07", "04", "1"}


def find(match):
    w = fold(match.replace("Inter Milano", "Internazionale").replace("PSG", "Paris Saint-Germain"))
    g = max(scores, key=lambda s: len(w & fold(s["home"] + " " + s["away"])))
    assert len(w & fold(g["home"] + " " + g["away"])) >= 2, (match, g["home"], g["away"])
    return g


def rng(n, s):
    """'2+' / '1-3' / '0-2' -> bool"""
    if s.endswith("+"):
        return n >= int(s[:-1])
    a, b = map(int, s.split("-"))
    return a <= n <= b


def cond(c, g, home, away):
    """One clause of a bet description, evaluated on the match."""
    hg, ag, h1, a1 = g["hg"], g["ag"], g["h1"], g["a1"]
    h2, a2 = hg - h1, ag - a1
    c = c.strip()
    if c.startswith("NOT (") and c.endswith(")"):
        return not all(cond(x, g, home, away) for x in c[5:-1].split(" & "))
    if c == "no draw":
        return hg != ag
    if c == "Both teams to score":
        return hg > 0 and ag > 0
    if c == "both teams score":
        return hg > 0 and ag > 0
    if c == "both teams score in the 1st half":
        return h1 > 0 and a1 > 0
    if m := re.fullmatch(r"(.+) or draw", c):
        return hg >= ag if m.group(1) == home else ag >= hg
    if m := re.fullmatch(r"(.+) win", c):
        return hg > ag if m.group(1) == home else ag > hg
    if m := re.fullmatch(r"([\d+-]+) goals( in the (1st|2nd) half)?", c):
        n = {None: hg + ag, "1st": h1 + a1, "2nd": h2 + a2}[m.group(3)]
        return rng(n, m.group(1))
    if m := re.fullmatch(r"(.+) ([\d+-]+) goals( in the (1st|2nd) half)?", c):
        team = m.group(1)
        assert team in (home, away), c
        t = {None: (hg, ag), "1st": (h1, a1), "2nd": (h2, a2)}[m.group(4)]
        return rng(t[0] if team == home else t[1], m.group(2))
    raise ValueError(f"unhandled clause: {c!r}")


def score_text(g):
    return f'{g["hg"]}-{g["ag"]} (HT {g["h1"]}-{g["a1"]})'


def settle_leg(l):
    g = find(l["match"])
    home, away = l["match"].split(" – ")
    ok = all(cond(c, g, home, away) for c in split_bet(l["bet"]))
    return {**l, "player": l["match"], "text": l["bet"], "kind": "football_ticket", "book_name": l["book"],
            "status": "won" if ok else "lost", "stat_text": score_text(g)}


def split_bet(b):
    out, depth, cur = [], 0, ""
    for part in b.split(" & "):
        cur = part if not cur else cur + " & " + part
        depth = cur.count("(") - cur.count(")")
        if depth == 0:
            out.append(cur)
            cur = ""
    return out


slips = []
for t in card["tickets"] + card["dc_tickets"]:
    legs = [settle_leg(l) for l in t["legs"]]
    hits = sum(l["status"] == "won" for l in legs)
    st = "won" if hits == len(legs) else "lost"
    slips.append({"n": len(legs), "label": t["label"].replace("�", "·"), "book_name": t["book"], "total_odds": t["total_odds"],
                  "hit_prob": t["hit_prob"], "legs": legs, "hits": hits, "status": st,
                  "profit": round(t["total_odds"] - 1, 2) if st == "won" else -1})
    print(f'{st.upper():5} {hits}/{len(legs)} {t["label"]} @ {t["total_odds"]}')
    for l in legs:
        print(f'      {l["status"]:4} {l["match"]} | {l["bet"]} | {l["stat_text"]}')

picks = []
for x in card["extrabet"]:
    g = find(x["match"])
    home, away = x["match"].split(" – ")
    sel = dict(kv.split("=", 1) for kv in x["selection"].split(","))
    scorer = sel["participant"]
    scored = any(not k["own_goal"] and k["scorer"] and fold(scorer) <= fold(k["scorer"]) for k in g["goals"])
    ok = scored
    if "team" in sel:
        team_home = fold(sel["team"]) & fold(g["home"])
        ok &= (g["hg"] > g["ag"]) if team_home else (g["ag"] > g["hg"])
    if "total" in sel:
        ok &= g["hg"] + g["ag"] > float(sel["total"])
    who = ", ".join(k["scorer"] + (" (OG)" if k["own_goal"] else "") for k in g["goals"]) or "no goals"
    st = "won" if ok else "lost"
    picks.append({**x, "player": x["match"], "text": x["bet"], "kind": "extrabet", "book_name": "Soccerbet",
                  "status": st, "profit": round(x["odds"] - 1, 2) if ok else -1,
                  "stat_text": f'{g["hg"]}-{g["ag"]} · {scorer.split()[-1]} {"scored" if scored else "didn’t score"}',
                  "scorers": who})
    print(f'{st:4} {x["bet"]} @ {x["odds"]} | {g["hg"]}-{g["ag"]} | {who}')

res = {"date": day, "settled": dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "stake": 10000, "kind": "football_card",
       "games": [], "football_scores": scores, "picks": picks, "lines": [], "football": [], "slips": slips, "arbitrage": []}
json.dump(res, open(f"Odds/results/{day}/results.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
w, l = sum(p["status"] == "won" for p in picks), sum(p["status"] == "lost" for p in picks)
pr = round(sum(p["profit"] for p in picks), 2)
led = json.load(open("Odds/results/ledger.json", encoding="utf-8"))
led["nights"][day] = {"won": w, "lost": l, "void": 0, "profit": pr, "roi": round(pr / max(w + l, 1) * 100, 1),
                      "slips_won": sum(s["status"] == "won" for s in slips),
                      "slip_profit": round(sum(s["profit"] for s in slips), 2),
                      "arb_profit": 0, "arb_stake": 0, "arbs": 0, "kind": "football"}
json.dump(led, open("Odds/results/ledger.json", "w", encoding="utf-8"), indent=1)
print(led["nights"][day])
