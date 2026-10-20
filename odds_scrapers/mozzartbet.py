"""
MozzartBet: POST JSON API on www.mozzartbet.com behind Cloudflare bot detection. Python's
requests is blocked by its TLS fingerprint, plain curl is not, so calls go through curl.
  /betting/get-special-offer   the special offers; "Igrači" holds the filter for player props
  /betting/matches             that filter -> one item per game with all player odds
                               (game names like "Broj poena D.Bacon", sides manje / više)
Header "medium: PREMATCH_WEB" is required. Players come as initial + surname, no team.
"""

import json
import os
import shutil
import subprocess
import tempfile

from .common import UA, local_from_ms, row

SITE = "https://www.mozzartbet.com"
COMPETITION = "evrolig"   # competition-name filter; "nba" for the NBA
PREFIXES = [   # longest first
    ("Poeni + skokovi + asistencije ", "Pts+Reb+Ast"), ("Poeni + skokovi ", "Pts+Reb"),
    ("Poeni + asistencije ", "Pts+Ast"), ("Skokovi + asistencije ", "Reb+Ast"),
    ("Broj poena ", "Points"), ("Broj skokova ", "Rebounds"), ("Broj asistencija ", "Assists"),
    ("Broj trojki ", "3PM"), ("Broj pogođenih trojki ", "3PM"),
]


class Curl:
    def __init__(self):
        self.curl = shutil.which("curl") or r"C:\Windows\System32\curl.exe"
        self.jar = os.path.join(tempfile.mkdtemp(), "cookies.txt")
        self.call("GET", "/sr/kladjenje", raw=True)        # picks up the Cloudflare cookies

    def call(self, method, path, body=None, raw=False):
        cmd = [self.curl, "-s", "--compressed", "-c", self.jar, "-b", self.jar, "-A", UA,
               "-H", f"Origin: {SITE}", "-H", f"Referer: {SITE}/sr/kladjenje", "-X", method]
        if body is not None:
            cmd += ["-H", "Content-Type: application/json", "-H", "medium: PREMATCH_WEB",
                    "--data-binary", json.dumps(body)]
        out = subprocess.run(cmd + [SITE + path], capture_output=True, timeout=60).stdout
        if raw:
            return out
        try:
            return json.loads(out.decode("utf-8"))
        except ValueError:
            raise SystemExit(f"Mozzart {path}: blocked or not JSON -- {out[:200]!r}")


def scrape():
    c = Curl()
    offers = c.call("POST", "/betting/get-special-offer", {}).get("specialOffer", [])
    offer = next((o for o in offers if o.get("name") == "Igrači"), None)
    if not offer:
        print("  No 'Igrači' offer right now")
        return [], []
    items, page = [], 0
    while True:
        data = c.call("POST", "/betting/matches", {**offer["filter"], "currentPage": page})
        batch = data.get("items") or []
        items += batch
        if len(batch) < offer["filter"].get("pageSize", 50):
            break
        page += 1
    items = [i for i in items if COMPETITION in i["competition"]["name"].lower()
             and "wnba" not in i["competition"]["name"].lower()]
    games = [{"id": i["id"], "home": i["home"]["name"], "away": i["visitor"]["name"],
              "kickoff": local_from_ms(i["startTime"])} for i in items]
    print(f"  {len(games)} games")

    rows, unknown = [], set()
    for i, g in zip(items, games):
        groups, detail = i.get("oddsGroup") or [], False
        if not groups:                             # NBA: the list has no odds, the match page has all of them
            groups, detail = (c.call("POST", f"/betting/match/{i['id']}", {}).get("match") or {}).get("oddsGroup") or [], True
        for group in groups:
            for o in group.get("odds") or []:
                if o.get("oddStatus") != "ACTIVE" or not o.get("value"):
                    continue
                gname = o["game"]["name"]
                hit = next(((p, m) for p, m in PREFIXES if gname.startswith(p)), None)
                side = {"više": "Over", "manje": "Under"}.get(o["subgame"]["name"].lower())
                if not hit or not side:
                    if not detail:                 # the match page also holds the game markets
                        unknown.add(gname.split(" ")[0] + " " + o["subgame"]["name"])
                    continue
                player = gname[len(hit[0]):].strip()
                rows.append(row(g, None, player, hit[1], side, float(o["specialOddValue"]), o["value"], g["id"]))
    if unknown:
        print("  Skipped:", ", ".join(sorted(unknown)))
    return rows, games
