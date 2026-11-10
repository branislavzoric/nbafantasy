"""Settle Odds/tonight/<day>/football_board.json (build_board_tickets.py: 15 football tickets, some with a Soccerbet
Extrabet leg) from Odds/results/<day>/football_scores.json (tools_fb/fb_scores.py) -> Odds/results/<day>/football_board.json.
Usage: python tools_fb/settle_fb_board.py <day>"""
import datetime as dt
import json
import os
import re
import sys
import unicodedata

sys.stdout.reconfigure(encoding="utf-8")
os.chdir(r"C:\Users\Zoric\nbafantasy")
day = sys.argv[1]
board = json.load(open(f"Odds/tonight/{day}/football_board.json", encoding="utf-8"))
scores = json.load(open(f"Odds/results/{day}/football_scores.json", encoding="utf-8"))
NAMES = {"Inter Milano": "Internazionale", "PSG": "Paris Saint-Germain", "Sporting Lisbon": "Sporting CP",
         "Feyenoord Rotterdam": "Feyenoord", "LASK Linz": "LASK", "Bayern Munich": "Bayern Munich"}


def fold(s):
    return set(re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower())) - {
        "fc", "sc", "sv", "the", "city", "town", "united", "de", "as", "07", "04", "1", "cf", "club"}


def find(match):
    for a, b in NAMES.items():
        match = match.replace(a, b)
    w = fold(match)
    g = max(scores, key=lambda s: len(w & fold(s["home"] + " " + s["away"])))
    return g if len(w & fold(g["home"] + " " + g["away"])) >= 2 and g.get("completed") else None


def basic(bet, home, hg, ag):
    if " & " in bet:                       # combo: "Benfica to win & 2+ goals", "X or draw & X 2+ goals", "... & 0-3 goals"
        return all(basic(part, home, hg, ag) for part in bet.split(" & "))
    if m := re.fullmatch(r"(\d+)\+ goals", bet):
        return hg + ag >= int(m.group(1))
    if m := re.fullmatch(r"(\d+)-(\d+) goals", bet):
        return int(m.group(1)) <= hg + ag <= int(m.group(2))
    if m := re.fullmatch(r"(.+) (\d+)\+ goals", bet):
        return (hg if m.group(1) == home else ag) >= int(m.group(2))
    if bet.startswith("Both teams NOT"):
        return hg == 0 or ag == 0
    if bet.startswith("Both teams"):
        return hg > 0 and ag > 0
    if m := re.match(r"(Over|Under) ([\d.]+) goals", bet):
        return (hg + ag > float(m.group(2))) if m.group(1) == "Over" else (hg + ag < float(m.group(2)))
    if bet == "Draw":
        return hg == ag
    if bet.startswith("No draw"):
        return hg != ag
    if bet.endswith(" or draw"):
        return hg >= ag if bet[:-8] == home else ag >= hg
    if bet.endswith(" to win"):
        return hg > ag if bet[:-7] == home else ag > hg
    raise ValueError(bet)


def extrabet(l, g):
    sel = dict(kv.split("=", 1) for kv in l["selection"].split(","))
    hg, ag = g["hg"], g["ag"]
    ok = any(not k["own_goal"] and k["scorer"] and fold(sel["participant"]) <= fold(k["scorer"]) for k in g["goals"])
    if "team" in sel:
        ok &= (hg > ag) if fold(sel["team"]) & fold(g["home"]) else (ag > hg)
    if "total" in sel:
        ok &= (hg + ag > float(sel["total"])) if sel.get("order", "+") == "+" else (hg + ag < float(sel["total"]))
    if "BTS" in l["type"]:
        ok &= hg > 0 and ag > 0
    return ok


def settle_leg(l):
    g = find(l["match"])
    if g is None:
        return {**l, "status": "open", "stat_text": "not final / not found"}
    home = l["match"].split(" – ")[0]
    ok = extrabet(l, g) if l["source"] == "Extrabet" else basic(l["bet"], home, g["hg"], g["ag"])
    who = ", ".join(k["scorer"] + (" (OG)" if k["own_goal"] else "") for k in g["goals"] if k["scorer"])
    return {**l, "status": "won" if ok else "lost", "stat_text": f'{g["hg"]}-{g["ag"]}' + (f" · {who}" if who and l["source"] == "Extrabet" else "")}


slips = []
for t in board["tickets"]:
    legs = [settle_leg(l) for l in t["legs"]]
    states = {l["status"] for l in legs}
    st = "lost" if "lost" in states else "open" if "open" in states else "won"
    slips.append({"name": t["name"], "label": t["label"], "book_name": t["book"], "total_odds": t["total_odds"],
                  "hit_prob": t["hit_prob"], "legs": legs, "hits": sum(l["status"] == "won" for l in legs), "status": st,
                  "profit": round(t["total_odds"] - 1, 2) if st == "won" else -1 if st == "lost" else 0})
    print(f'{st.upper():5} {slips[-1]["hits"]}/{len(legs)} {t["label"]} @ {t["total_odds"]}')
    for l in legs:
        print(f'      {l["status"]:4} {l["match"]} | {l["bet"]} | {l["stat_text"]}')
out = {"date": day, "settled": dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "slips": slips,
       "won": sum(s["status"] == "won" for s in slips), "lost": sum(s["status"] == "lost" for s in slips),
       "profit": round(sum(s["profit"] for s in slips), 2)}
json.dump(out, open(f"Odds/results/{day}/football_board.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print({k: out[k] for k in ("won", "lost", "profit")})
