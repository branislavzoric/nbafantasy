#!/usr/bin/env python3
"""
settle_court_tickets.py

Settles the NBA court tickets (build_court_tickets.py) from ESPN box scores: each leg won/lost/void (DNP = void,
a voided leg drops out of the ticket's odds), each ticket won/lost/open.
  python settle_court_tickets.py <YYYY-MM-DD>
Output: Odds/tonight/<date>/court_results.json
"""

import json
import math
import os
import sys

from settle_tonight import nba_day_of, nba_prop_leg

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


def main():
    day = sys.argv[1]
    base = os.path.join(PROJECT_DIR, "Odds", "tonight", day)
    spec = json.load(open(os.path.join(base, "court_tickets.json"), encoding="utf-8"))
    for t in spec["tickets"]:
        for l in t["legs"]:
            l.update(nba_prop_leg(l["match"], l["player"], l["market"], l["side"], l["line"], nba_day_of(l["kickoff"])))
        st = [l["status"] for l in t["legs"]]
        t["status"] = "lost" if "lost" in st else "open" if "open" in st else "won"
        t["settled_odds"] = round(math.prod(l["odds"] for l in t["legs"] if l["status"] != "void"), 2)
        print(f'{t["name"]:14} {t["status"].upper():5} odds {t["settled_odds"]}')
        for l in t["legs"]:
            print(f'   {l["status"]:5} {l["player"]:24} {l["side"]} {l["line"]:g} {l["market"]:12} -> {l.get("stat")}  {l.get("line_detail", l.get("note", ""))}')
    json.dump(spec, open(os.path.join(base, "court_results.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
