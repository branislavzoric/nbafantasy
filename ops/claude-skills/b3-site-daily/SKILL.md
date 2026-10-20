---
name: b3-site-daily
description: Daily update of the user's website b3basketball.com — research and fact-check 10 fresh NBA stories and 10 fresh EuroLeague stories with freely licensed photos ("news" mode), or settle last night's bets and build tonight's EuroLeague value bets/arbitrages for the Against the Odds tab ("odds" mode), then rebuild and deploy the site. Runs unattended from Windows Task Scheduler; also use when the user asks to "update the site", "refresh b3basketball", "daily site news" or similar.
---

# b3basketball.com daily update

Runs unattended (Windows Task Scheduler → `C:\Users\Zoric\Desktop\b3basketball-site\daily\run_daily.cmd news|odds`).
Nobody reviews before publishing, so **accuracy rules are strict**: every fact, number, date and quote must come from
an article you fetched today. If unsure, leave it out. Never post to Instagram/n8n from this skill.

Paths: project `C:\Users\Zoric\nbafantasy` (run commands from there; venv `venv/Scripts/python.exe`,
`PYTHONIOENCODING=utf-8` is already set by the runner). Site: `C:\Users\Zoric\Desktop\b3basketball-site`.
`<date>` = today (YYYY-MM-DD). Use simple single commands (no `cd x && ...` chains), as only these Bash forms are
allowed: `venv/Scripts/python.exe ...`, `npx -y wrangler@4 deploy ...`, `mkdir`, `cp`, `ls`, `cat`.

## Mode "news"

If `News/<date>/news.json` and `EuroLeague News/<date>/news.json` both exist, skip to Build & deploy.
(When the user asks for a later refresh the same day, add the new stories to the start of the existing file and renumber
the ids from 1, because id 1 leads the page. First check there's no `<date>/posts.json`, which maps story ids.)

1. **NBA (10 stories)** — follow steps 1–2 of the `nba-news` skill (WebSearch extended + WebFetch Hoops Rumors,
   NBA.com, Yahoo, ESPN; fact-check each kept story in its article). Prefer the last 24–48 h. Don't repeat a story
   already published in the previous 3 days of `News/` unless there is a real update (then say what's new).
2. **EuroLeague (10 stories)** — hubs: https://www.eurohoops.net/en/euroleague/ , https://basketnews.com/news/euroleague ,
   https://mediacentre.euroleague.net/ (round MVPs, press releases), https://en.wikipedia.org/wiki/2026%E2%80%9327_EuroLeague_regular_season
   (results/fixtures/standings; read with `offset` for later rounds). euroleaguebasketball.net often returns 429.
   Mix: last round's results/MVP, injuries, transfers, coaches' quotes, league business, tonight's preview.
3. Write `News/<date>/news.json` and `EuroLeague News/<date>/news.json` in the nba-news format
   (`{"collected", "status": "published", "stories": [{"id","category","date","headline","summary","quotes",
   "fantasy_angle","sources"}]}`). Optional per story:
   - `"photo": "p_name.jpg"` (file in `<date>/photos/`), `"team": "OKC"` (NBA logo tile if no photo)
   - `"timeline"` for injury round-ups: `{"axis": [start, end], "marker": [date, "Opening night"], "rows": [{"name",
     "team", "injury", "hurt", "back" (or null = out), "status"}]}`. **Only use dates stated in a source** (or carried
     from an earlier fact-checked timeline); drop a row rather than guess a date.
4. **Photos** (nba-news rules: Wikimedia Commons CC/PD only, never news-site photos or `headshots/`):
   ```
   venv/Scripts/python.exe commons_photos.py "News/<date>/photos" p_name="Player Name" ...
   ```
   Read `<dir>/candidates/sheet.jpg`, check the file names in `candidates/meta.json` are the right person, then
   `venv/Scripts/python.exe commons_photos.py "<dir>" --pick p_name=N ...` (writes credits.json). Reuse photos from
   earlier days (copy the jpg and its credits.json entry). Commons rate-limits: run the NBA and EuroLeague searches
   one after the other, not in parallel. No good photo → leave `photo` out (the site shows a tile).
5. **Social posts ("From the source")**: embed posts, never download or re-upload their images. On the site they
   show as a card with the post text; nothing loads from X/Instagram until the reader clicks "Show post" (no cookies
   before consent). Embeds are extra: they never replace the Commons photo (hero, tiles and share image stay Commons).
   - **Where to find them:** the source articles you fetched today. Hoops Rumors, Eurohoops and BasketNews embed
     the original posts, so grep the article HTML for `x.com/<user>/status/<id>` and `instagram.com/p/<code>` (or `/reel/`).
     Don't search X or Instagram for posts on your own.
   - **Which accounts:** (a) official accounts: the NBA/EuroLeague, clubs and club PR (e.g. @MavsPR, @ClippersPR),
     and the player's own account; (b) the reporter or beat writer whose post the source article itself cites as
     the origin of the news (e.g. @ShamsCharania for a contract, a beat writer for an injury update). Nothing else:
     no fan accounts, aggregators, memes, or posts the article doesn't cite.
   - **Verify each one:** `venv/Scripts/python.exe check_social.py <url> [<url> ...]` prints the posting account
     and text as a JSON line (or ERROR if the post is deleted or private). Keep a post only if the account is the
     one the article names and the text matches the story. Paste the printed object as-is:
     `"social": [{"platform","url","account","name","text","posted"}]` (an optional `verified` is fine). Max 2 per story.
     Don't edit the text, and don't use a post's wording as a fact unless the story's article confirms it.

## Mode "odds"

1. If yesterday has `Odds/picks/<yesterday>/picks.json` and no `Odds/results/<yesterday>/results.json`:
   `venv/Scripts/python.exe settle_bets.py <yesterday>` (see nightly-bets step 0; skip quietly if games aren't final).
   If yesterday has `Odds/tonight/<yesterday>/tonight.json`, run `venv/Scripts/python.exe settle_tonight.py <yesterday>`
   instead: it settles EuroLeague props/lines, NBA game lines (ESPN final scores) and **NBA player props** (ESPN box
   scores, saved to `Box Scores/<date>/`; DNP = void). Each NBA leg is settled on its own US Eastern game date, so
   a ticket with a leg from the next night stays "open": also re-run `settle_tonight.py` for the day before
   yesterday when its `results.json` still has open tickets or arbitrages (it overwrites results and the ledger entry).
2. `venv/Scripts/python.exe scrape_odds.py`, then `compare_odds.py`, then `build_bet_picks.py`.
   If no book offers EuroLeague props today (weekends, off days), stop after step 1 and just rebuild.

## Standings, results & fixtures (both modes, before building)

`venv/Scripts/python.exe collect_league_data.py` writes `League/nba.json` (ESPN: standings, last 7 / next 7 days) and
`League/euroleague.json` (official EuroLeague API: standings, last two rounds, next two). The site builds
`/standings`, `/schedule`, `/euroleague/standings` and `/euroleague/schedule`, linked from each section's sub-nav. If a
source fails, the previous snapshot is kept; say so in the summary.

## Fantasy (draft board)

`/fantasy` shows the ESPN draft board built from `ADP draft order/espn_draft_order.csv`, plus a PNG download.
Only refresh it when asked, or weekly before the season. To refresh: `venv/Scripts/python.exe espn_nba_projections.py`, then
`venv/Scripts/python.exe build_espn_draft_board.py`, then rebuild the site.

## Games (box scores, both modes, before building)

`collect_boxscores.py` fills `Box Scores/<date>/` (ESPN JSON). The site's **Games** tab builds `/games` (latest
night) and `/games/<date>` (last 7 nights); the old `/box-scores` URLs are redirect pages. Each game is one card with a
3-page carousel: 1 summary (line score + the game's highlights video), 2 full box score, 3 best performance (the
game's top Yahoo-FPTS player, with his own video if it differs from the game video). There is no night-wide top 10.
For video, write `Box Scores/<date>/highlights.json` = `{"games": {espn_game_id: yt}, "players": {espn_player_id: yt}}`
with `yt = {"id", "title", "channel"}`. Aim for one video per game and one for each game's best player; every
video must belong to that game. A `"night"` key is ignored.
**EuroLeague games** are on the same night page (EuroLeague first, then NBA): `collect_league_data.py` saves the official
box scores to `Box Scores/EuroLeague/<date>/<code>.json` and crests to `logos/el/`. EL videos go in
`Box Scores/EuroLeague/<date>/highlights.json` keyed by game code (EUROLEAGUE BASKETBALL channel); EL best performance = PIR.
**Recaps (the story):** each game's page 1 opens with a teaser from `recaps.json` next to highlights.json (NBA keyed
by ESPN id, EL by game code): `{"games": {id: {"headline", "text"}}}`. Headline ≤ ~40 chars; text 2–3 sentences:
how the game turned (quarter scores, runs, leads), the top performers with numbers, one notable absence or team stat.
Only facts from the box score (`DNP` reasons, line scores, team totals) or an article you fetched; check every number.
**Separate pages:** NBA at `/games` + `/games/<date>`, EuroLeague at `/games/euroleague` + `/games/euroleague/<date>`
(Games sub-nav switches). Date pills run oldest → newest everywhere (Games, Results, Results & fixtures).
**Upcoming days:** the date pills also list the next 7 calendar days (US Eastern date for NBA, CET for EL). Each
gets `/games[/euroleague]/<day>` built automatically from `League/*.json` fixtures: a preview (tip-off ET/CET,
records, last 3 results, each team's top scorer in its last box score, a link to the odds board on today's page) or a
"No games" notice with a link to the next game day. Run `collect_league_data.py` before building so these are fresh.
**Statements (page 3, replaces best performance):** `quotes.json` next to recaps.json, `{"games": {id: [{"who","role",
"context"?, "text","source","url","translated"?}]}}`. 1–4 per game, max 10; the popular post-game lines from coaches and
players (press conference, on-court interview), from the game's own reports (team sites, beat writers, EuroLeague/club
sites, Turkish/Italian/French/Spanish/Greek press for EL). **Verbatim only:** confirm each quote in the raw article text
(fetch the HTML and find the sentence; WebFetch summaries can paraphrase). Translations are ours: set `"translated":
"Turkish"` etc. and translate faithfully. Not from this game (practice, pregame, older game) = leave out. A game with no
quotes gets no Statements page. **Official channels only** (NBA, NBA on ESPN, team channels; EuroLeague
Basketball for EL stories): find ids with `curl -A "Mozilla/5.0" "https://www.youtube.com/results?search_query=..."`
and confirm each with `https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v=<id>&format=json`
(author_name + title). Never re-upload footage. News stories take the same `youtube` field.

## Odds board notes

The board reads the newest of `Odds/tonight/<date>/tonight.json` (`build_tonight_bets.py`: props + game lines +
football) or `Odds/picks/<date>/picks.json`. The page is **basketball only** and drawn as chalkboards. It shows
tonight.json's `basket` card: top-10 singles, three tickets, and basketball arbitrages.
- **Tickets:** a 4-leg slip, a safer ticket (total odds 5–20, highest win chance with positive value) and a long shot
  (20–100, best value).
- **Ticket rules:** each ticket is at one bookmaker, uses one leg per player and at most 2 legs per game, and the
  tickets don't share legs unless they have to.
- **NBA:** game lines (winner, handicap, total incl. OT) from all six books, via `NBA_SCRAPERS` in
  `odds_scrapers/basketball_lines.py` (saved to `Odds/nba_lines/<date>/raw_lines.json`). The "tonight" window runs to
  noon the next day, because NBA tips off after midnight CET. NBA player props: `scrape_odds.py --nba` (Soccerbet, Superbet, Mozzart so far); a run replaces each book's EuroLeague run as "latest", so scrape EuroLeague and NBA props in separate passes.
  `settle_tonight.py` settles NBA lines from ESPN final scores (US Eastern date = the board date).
  NBA bookmaker IDs (Superbet 2176, BalkanBet 27380) are hard-coded; Soccerbet, MaxBet, Admiral and Mozzart are found
  by league name. If NBA shows 0 games at a book once the regular season starts, recheck those IDs.
- **Rebuilding:** run `build_tonight_bets.py --no-scrape` to rebuild from the morning's odds. Then reset `built` to
  the morning scrape time, because the page shows it as "odds as of". Settle a tonight.json night with `settle_tonight.py <date>` (writes
results.json, ledger, and optionally reads a hand-written `analysis.json` "why it happened", shown on the site).
- **Results page:** `/against-the-odds/results` (latest night) and `/against-the-odds/results/<date>`: scoreboard
  tiles, why it happened, ticket cards, singles, arbitrages (basketball only; football picks are hidden). The odds page
  shows a one-line "Last night" strip linking there. In odds mode, after settling, write `Odds/results/<date>/analysis.json`
  (3–5 points, `{"points": [{"title","text"}]}`, facts from the settle output only).

## Build & deploy (both modes)

```
venv/Scripts/python.exe C:/Users/Zoric/Desktop/b3basketball-site/build_site.py
npx -y wrangler@4 deploy --config C:/Users/Zoric/Desktop/b3basketball-site/wrangler.jsonc
```
Then check with WebFetch that https://www.b3basketball.com/ (and /euroleague, /against-the-odds) show today's content.
End with a short summary: stories published (headlines), photos missing, odds board built or skipped, any errors.
