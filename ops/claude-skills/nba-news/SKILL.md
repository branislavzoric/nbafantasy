---
name: nba-news
description: Build the NBA news section for the user's Instagram account (~/nbafantasy) — find 10 interesting/trending NBA stories (ESPN, Yahoo Sports, Hoops Rumors…), fact-check them, save them for review, render one simple 1080x1350 post per story with story-fitting freely licensed photos, and post them as separate posts 5 minutes apart via n8n only after the user approves. Use when the user asks for "NBA news", "news posts", "today's news", "trending NBA stories" or similar.
---

# NBA news posts

Project: `C:\Users\Zoric\nbafantasy` (venv `venv\Scripts\python.exe`, run from the project folder with
`PYTHONIOENCODING=utf-8`). `<date>` below = today (YYYY-MM-DD). Everything for a day lives in `News/<date>/`.

## 1. Find the stories

Use WebSearch (extended) with several queries in one turn (today's NBA news, injuries, contracts/extensions,
trades/rumors), then WebFetch the hubs for headlines and dates:
- https://www.hoopsrumors.com/ · https://sports.yahoo.com/nba/news/ · https://www.espn.com/nba/

Pick **10** stories. Prefer today and yesterday; up to about a week old is OK if it's strong. Skip anything
older than ~7 days (say which big older stories were skipped). Mix the categories: big names, drama, comebacks,
contracts, league business, off-court, plus one fantasy-relevant injury story.

## 2. Fact-check

WebFetch the actual article for every story you keep and take numbers, dates and quotes from it. Search results
and hub summaries can be wrong or conflicting (e.g. contract figures); when sources disagree, use the article or
leave the number out. Don't add facts that aren't in a source (no guessed teammates, no paraphrase presented as a
quote). Use the publication date of the article.

## 3. Save for review

Write `News/<date>/news.json`:
```json
{"collected": "<date>", "status": "pending_review", "stories": [
  {"id": 1, "category": "Preseason", "date": "2026-10-08", "headline": "...", "summary": "2-4 sentences",
   "quotes": ["\"...\" - Name"], "fantasy_angle": "... or null", "sources": ["url", "..."]}]}
```
Show the user the 10 stories (one line each with date) plus sources, and wait for their review.

## 4. Pictures (user's rules)

- **Don't use the fantasy headshots** (`headshots/`, ESPN) for news. The user wants real photos that fit the story
  (e.g. an angry Embiid for a "Stop the lies" headline).
- **Don't use photos from news sites** (Getty/AP/Imagn on ESPN, Yahoo…): copyright strikes. The user was told this;
  if they want this week's game photos, the option is a paid editorial licence.
- Use **Wikimedia Commons** CC/PD images: player action shots or scenery for non-player stories (arena, city,
  Lake Como…). Search via the API (`generator=search`, `filetype:bitmap`, `prop=imageinfo`, `iiprop=url|size|extmetadata`),
  keep only CC / Public domain licences, make a contact sheet of thumbnails and pick by eye.
  Prefer current-team or neutral kits (Team USA, All-Star); older photos often show old teams, so mention that.
- Commons rate-limits hard: send a User-Agent, pause 5-20 s between requests, use `iiurlwidth` thumbs (1280-3000);
  originals often return 429. Save as `News/<date>/photos/p_<name>.jpg` and record
  `{file, page, license, author}` in `photos/credits.json` under the key `p_<name>` (or the bg name). Credits are
  added to captions automatically.

## 5. Write the posts

`News/<date>/posts.json` → `{"posts": [...]}`, one per story (`id` = story id):
```json
{"id": 2, "slug": "lebron-embiid-stop-the-lies", "kicker": "Drama", "date": "Oct 7", "team": "PHI",
 "photo": {"file": "p_embiid.jpg", "box": [x, y, width]},
 "label": "LeBron on the report that Embiid was late to practice",
 "stats": [["\"Stop the lies\"", "", "quote"]],
 "context": "one or two sentences", "source": "Yahoo Sports · ESPN"}
```
- `photo.box` = crop in source pixels (cropped to the 5:4 hero); check crops on a contact sheet first.
- Scenery instead of a player: `"bg": "<name>"` (photos/<name>.jpg); a logo over it: `"logo": "LAL"`;
  no photo: team colours (`"team"`) or `"colors": [dark, accent]`.
- `stats`: one or two `[big value, caption]` items (yellow big text), or one `[text, caption, "quote"]`.
  **Make the big text catchy, not a raw stat**: the user disliked "1/1 FG" for LeBron's Sixers debut, which
  became "The Philly era begins" with the stat line underneath.
- Injury stories: `"layout": "timeline"` = return timetable (injury date, opening-night marker, earliest return):
  `"axis": [start, end], "marker": [date, "Opening night"], "rows": [{"name", "team", "photo", "injury", "hurt",
  "back" (date or null = out for season), "status"}]`.

## 6. Build and review

```
venv/Scripts/python.exe build_news_posts.py <date>
```
→ `News/<date>/posts/NN_<slug>.png` (1080x1350) + `.json` (`slug`, `image`, caption = headline, summary,
fantasy angle, source, 📸 photo credits, hashtags). Make a contact sheet of all 10 and look at it: cropped heads,
text overlapping the photo, wrapped big text, old-team jerseys. Fix and rebuild, then show the user and wait.

## 7. Post (only when the user approves)

The user wants **separate posts, 5 minutes apart**, in id order. They go through the single-post n8n workflow
"NBA Fantasy - Post to Instagram" (id `8DEfqsR5VIAwy56c`), which posts the lowest-sorted `.json` in
`content/approved/` (uses its `caption` and `image`) and moves it to `content/posted/`.

1. Check `content/approved/` is empty (it's shared with the projection cards).
2. `cp News/<date>/posts/*.png News/<date>/posts/*.json content/approved/`
3. Run in the background (Bash `run_in_background`), logging to `News/<date>/posting.log`:
```
for i in $(seq 1 10); do start=$(date +%s)
  MSYS_NO_PATHCONV=1 docker exec -e N8N_RUNNERS_BROKER_PORT=5690 n8ndocker-n8n-1 n8n execute --id=8DEfqsR5VIAwy56c
  [ $i -lt 10 ] && sleep $((300 - ($(date +%s) - start))); done
```
   (log the file being posted, the `"status"` line and the remaining queue count each round).
4. Watch the log with Monitor and tell the user as posts go live; report any `error` status immediately and stop.
