#!/usr/bin/env python3
"""
build_carousels.py

Bundles projection cards into one Instagram carousel per draft round (12 cards per
round: round 6 = #61-72 ... round 13 = #145-156) and queues them for the n8n
workflow "NBA Fantasy - Post Carousel" (content/carousels/{pending,approved,posted}).

Each round gets content/carousels/<queue>/round_NN.json (caption + image list) and a
copy of its 12 card PNGs named round_NN_MM_<slug>.png.

Usage:
  python build_carousels.py 6 13              # rounds 6..13 -> pending
  python build_carousels.py 6 13 --approve    # straight to approved
"""

import argparse
import json
import os
import shutil

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CARDS_DIR = os.path.join(PROJECT_DIR, "content", "pending")
QUEUE_DIR = os.path.join(PROJECT_DIR, "content", "carousels")
PER_ROUND = 12


def load_card(rank):
    names = [f for f in os.listdir(CARDS_DIR) if f.startswith(f"{rank:03d}_") and f.endswith(".json")]
    if not names:
        raise SystemExit(f"No card #{rank} in {CARDS_DIR}")
    with open(os.path.join(CARDS_DIR, names[0]), encoding="utf-8") as f:
        return json.load(f)


def caption(rnd, cards):
    first, last = cards[0]["rank"], cards[-1]["rank"]
    lines = [f"🏀 2026-27 FANTASY RANKINGS · ROUND {rnd} (#{first}–{last})",
             "Yahoo default points projections · swipe for every card ➡️", ""]
    lines += [f"#{c['rank']} {c['player']} ({c['team']}) – {c['fppg']} FPTS/G · "
              f"{c['pts']} PTS, {c['reb']} REB, {c['ast']} AST" for c in cards]
    teams = " ".join(dict.fromkeys(f"#{c['team']}" for c in cards))
    lines += ["", "Who's your sleeper in this round? 👇", "",
              f"#NBA #FantasyBasketball #NBAFantasy #FantasyNBA #FantasyDraft {teams}"]
    return "\n".join(lines)[:2200]


def main():
    ap = argparse.ArgumentParser(description="Bundle projection cards into round carousels.")
    ap.add_argument("first_round", type=int)
    ap.add_argument("last_round", type=int)
    ap.add_argument("--approve", action="store_true", help="queue in approved instead of pending")
    args = ap.parse_args()
    out = os.path.join(QUEUE_DIR, "approved" if args.approve else "pending")
    for folder in ("pending", "approved", "posted"):
        os.makedirs(os.path.join(QUEUE_DIR, folder), exist_ok=True)

    for rnd in range(args.first_round, args.last_round + 1):
        ranks = range((rnd - 1) * PER_ROUND + 1, rnd * PER_ROUND + 1)
        cards = [load_card(r) for r in ranks]
        slug = f"round_{rnd:02d}"
        images = []
        for i, c in enumerate(cards, 1):
            name = f"{slug}_{i:02d}_{c['slug']}.png"
            shutil.copyfile(os.path.join(CARDS_DIR, c["image"]), os.path.join(out, name))
            images.append(name)
        with open(os.path.join(out, f"{slug}.json"), "w", encoding="utf-8") as f:
            json.dump({"slug": slug, "round": rnd, "ranks": [ranks[0], ranks[-1]],
                       "cards": [c["slug"] for c in cards], "images": images,
                       "caption": caption(rnd, cards)}, f, ensure_ascii=False, indent=2)
        print(f"Round {rnd:2}: #{ranks[0]}-{ranks[-1]}  {cards[0]['player']} ... {cards[-1]['player']}")


if __name__ == "__main__":
    main()
