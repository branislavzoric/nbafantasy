---
name: nightly-bets
description: Run the nightly betting flow for the user's account (~/nbafantasy) — settle the previous night's posted bets into a results carousel (won/lost status of every bet), scrape EuroLeague player-prop odds from the Serbian bookmakers, find tonight's top 10 value picks, the best 4-leg slip with total odds 10+, a 10+ leg slip and the 5 most profitable arbitrages, render a picks carousel and an arbitrage carousel, explain the picks, and post both carousels via n8n only after the user approves. Use when the user asks for "tonight's bets", "bet picks", "betting flow", "betslip", "arbitrages for tonight" or similar.
---

# Nightly bets

Project: `C:\Users\Zoric\nbafantasy` (venv `venv\Scripts\python.exe`, run from the project folder with
`PYTHONIOENCODING=utf-8`). `<date>` = today (YYYY-MM-DD). Output: `Odds/picks/<date>/`.

## 0. Settle the previous night's bets (results / "status of all bets")

The user wants the status of every posted bet shown. Before (or instead of) tonight's picks, settle the last
night that has `Odds/picks/<date>/picks.json`:
```
venv/Scripts/python.exe settle_bets.py <previous date> [--stake 10000]
```
- Pulls the official EuroLeague box scores (`api-live.euroleague.net/v2/competitions/E/seasons/E2026/games`
  for the game codes, `live.euroleague.net/api/Boxscore?gamecode=N&seasoncode=E2026`), cached in
  `Odds/results/<date>/`. It settles all 10 picks, both slips leg by leg, and the 5 arbitrages (with the posted stake split), then
  writes `results.json` and updates the running `Odds/results/ledger.json` (all-time record).
- Rules: Over/N+ wins if stat > line, Under if stat < line; DNP / not in the box = VOID (void legs count 1.00 on a slip);
  1Q/1H markets stay OPEN. 1 unit per pick/slip.
- Check the printout: any zero stat should come with real minutes, and odd name matches (e.g. "DJ Stewart" =
  Dewayne Stewart Jr.) should be right.
- Results carousel → `Odds/results/<date>/carousel_results/` (cover with record, profit, slips, arbs and the
  all-time line; all picks with WON/LOST pills and the actual stat; each slip leg by leg; the arbitrages) + `caption.json`.
  Make a contact sheet and look at it.
- Post it like the other carousels (step 5) as `<date>_bets_0_results.json` + `<date>_bets_0_results_NN_*.png`
  (the `0` sorts it before tonight's picks). Only post after the user says so.
- If the night's posted card came from `build_tonight_bets.py` (`Odds/tonight/<date>/tonight.json`: props + game
  lines + football + 4/5/6-leg slips), settle it with `settle_tonight.py <date>` instead (same outputs/ledger; football
  scores from ESPN's soccer scoreboard; NBA game lines from ESPN final scores and NBA player props from ESPN box
  scores, each leg on its own US Eastern game date, so re-run the next day when a ticket has a next-night leg).
  NBA props: `scrape_odds.py --nba` (all six books; Soccerbet, Superbet and Mozzart list them as of Oct 2026). For the "why it happened" slide, look at the printout (minutes, shooting,
  team totals, quarter scores in the cached box_*.json), write `Odds/results/<date>/analysis.json`
  (`{"headline", "sub", "points": [{"title", "text"}]}`, ~6 points, box-score facts only), then re-run.
- EuroLeague usually plays Tue-Fri. If no book has player props tonight (e.g. on a weekend), say so and offer the
  results carousel on its own.

## 1. Collect the odds

```
venv/Scripts/python.exe scrape_odds.py
venv/Scripts/python.exe compare_odds.py
```
- Bookmakers: soccerbet, maxbet, admiralbet, mozzartbet, superbet, balkanbet (modules in `odds_scrapers/`, rows go
  to the Postgres schema `odds`). New books are added as a module there plus an entry in `BOOKS`.
- Check the summary: which books returned odds and how many games/players matched. Mozzart sometimes lists the
  games before it has priced them ("no player odds"); that's not a bug, just say so.
- `compare_odds.py` drops games that have already started (a book without fresh props keeps its old run as "latest").
- If nothing is offered for tonight, tell the user and stop.

## 2. Analyse

```
venv/Scripts/python.exe build_bet_picks.py [--min-books 3] [--slip-odds 10] [--long-legs 10]
```
→ `Odds/picks/<date>/picks.json`. Method (explain it if asked):
- Fair price per line = the no-vig over/under consensus of the **other** books (leave-one-out). Edge = odds × fair
  probability − 1.
- **Top 10 picks**: one per player, odds 1.40–4.00, 3+ books in the consensus, ranked by edge × min(books, 4)/4;
  the best price is shown, with other books at the same price listed.
- **4-leg slip**: total odds ≥ 10 and the highest expected value. **A slip is one ticket at ONE bookmaker**, so all
  legs come from the same book; the legs are different players.
- **Long slip**: the 10 highest-edge legs (different players) at the book with the best EV.
- **Arbitrage**: the 5 highest-profit lines from compare_odds (best Over + best Under at different books).

## 3. Carousels

```
venv/Scripts/python.exe build_bet_carousels.py <date> [--stake 10000]
```
(template `bet_slides_template.html`, 1080x1350.)
- `carousel_picks/`: cover with the top-10 list, picks 1–5, picks 6–10, the 4-leg slip, the long slip
- `carousel_arbs/`: cover, then one slide per arbitrage with the stake split for the example stake
- captions in `carousels.json`. Every slide has the footer "18+ · odds as of HH:MM · not a guarantee, bet responsibly".

Make a contact sheet of both decks and look at it (overflowing lists, empty space, long names). Fix and rebuild.

## 4. Summary for the user

Give the games, which books had prices, a table of the 10 picks (pick, odds @ book, fair, edge, why = how the other
books price the same bet; use `Odds/compare/<date>/odds_comparison.csv` for each book's price), a 2–3 sentence
summary, both slips (odds and **win chance**), and the top 5 arbitrages. Be honest:
- Slips are long shots (Oct 9: 4 legs at odds 28.6 had a 5.5% chance; 10 legs had 0.06%). The EV figures assume the
  fair prices are right, and errors compound across legs. Same-game legs can be linked, and books may limit them.
- If one book dominates the picks, say why that may be (Superbet's props include overtime and its Overs run high;
  a lone outlier, often Admiral, can be a stale line).
- "N+" (milestone) Over legs in arbitrages: check the book's settlement/void rules.
- Prices move: suggest re-running close to tip-off.
Then wait. **Don't post until the user says to.**

## 5. Post (after the user approves)

Carousel workflow "NBA Fantasy - Post Carousel" (id `nbaCarouselWf001`) posts the lowest-sorted `.json` in
`content/carousels/approved/` (`{"slug", "images": [...], "caption"}`, images in the same folder) and moves it to
`content/carousels/posted/`.
1. Check `content/carousels/approved/` is empty.
2. Copy the slides as `<date>_bets_1_picks_NN_*.png` / `<date>_bets_2_arbs_NN_*.png` and write
   `<date>_bets_1_picks.json` and `<date>_bets_2_arbs.json` with images + caption from `carousels.json`
   (picks first, so it sorts first).
3. Run twice:
```
MSYS_NO_PATHCONV=1 docker exec -e N8N_RUNNERS_BROKER_PORT=5690 n8ndocker-n8n-1 n8n execute --id=nbaCarouselWf001
```
   Check `"status": "success"` each time and that both files moved to `content/carousels/posted/`.
