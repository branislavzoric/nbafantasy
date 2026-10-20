"""Fetch ESPN football results for a day: FT + HT goals per team and scorers -> Odds/results/<day>/football_scores.json"""
import json
import os
import sys

import requests

sys.stdout.reconfigure(encoding="utf-8")
os.chdir(r"C:\Users\Zoric\nbafantasy")
day = sys.argv[1]
LG = {"Premier League": "eng.1", "LaLiga": "esp.1", "Serie A": "ita.1", "Bundesliga": "ger.1",
      "Ligue 1": "fra.1", "Eredivisie": "ned.1", "Primeira Liga": "por.1"}
out = []
for league, code in LG.items():
    sb = requests.get(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{code}/scoreboard",
                      params={"dates": day.replace("-", "")}, timeout=30).json()
    for ev in sb.get("events", []):
        c = ev["competitions"][0]
        side = {t["homeAway"]: t for t in c["competitors"]}
        hid = side["home"]["team"]["id"]
        g = {"league": league, "id": ev["id"], "home": side["home"]["team"]["displayName"],
             "away": side["away"]["team"]["displayName"], "completed": c["status"]["type"].get("completed"),
             "hg": int(side["home"]["score"]), "ag": int(side["away"]["score"]), "goals": []}
        summ = requests.get(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{code}/summary",
                            params={"event": ev["id"]}, timeout=30).json()
        for k in summ.get("keyEvents", []):
            if not k.get("scoringPlay"):
                continue
            clock = k.get("clock", {}).get("displayValue", "")
            period = k.get("period", {}).get("number")
            who = [p["athlete"]["displayName"] for p in k.get("participants", [])][:1]
            og = "own goal" in (k.get("type", {}).get("text", "") + k.get("text", "")).lower()
            g["goals"].append({"team": "home" if k.get("team", {}).get("id") == hid else "away",
                               "period": period, "clock": clock, "scorer": who[0] if who else None,
                               "own_goal": og, "text": k.get("text", "")[:140]})
        g["h1"] = sum(1 for x in g["goals"] if x["team"] == "home" and x["period"] == 1)
        g["a1"] = sum(1 for x in g["goals"] if x["team"] == "away" and x["period"] == 1)
        g["check"] = (sum(x["team"] == "home" for x in g["goals"]), sum(x["team"] == "away" for x in g["goals"])) == (g["hg"], g["ag"])
        out.append(g)
        print(f'{league:15} {g["home"]} {g["hg"]}-{g["ag"]} {g["away"]}  HT {g["h1"]}-{g["a1"]}  final={g["completed"]} ok={g["check"]}')
        for x in g["goals"]:
            print(f'      {x["team"]:4} P{x["period"]} {x["clock"]:7} {x["scorer"]}{" (OG)" if x["own_goal"] else ""}')
os.makedirs(f"Odds/results/{day}", exist_ok=True)
json.dump(out, open(f"Odds/results/{day}/football_scores.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
