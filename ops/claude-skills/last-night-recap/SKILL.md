---
name: last-night-recap
description: Build the "Last night's top 10" Instagram recap reel for the user's NBA fantasy account (~/nbafantasy) — collect last night's box scores, rank the top 10 by Yahoo default fantasy points, write sports-journalist headlines, make a top-3 poster thumbnail from freely licensed photos, render the reel (poster -> #10..#1 countdown with shot charts -> stats table), and post it via n8n only after the user approves. Use when the user asks for "last night recap", "top 10 from last night", "nightly reel" or similar.
---

# Last night recap

Project: `C:\Users\Zoric\nbafantasy` (Python venv at `venv\Scripts\python.exe`, run scripts from the project
folder with `PYTHONIOENCODING=utf-8`). n8n runs in Docker (`n8ndocker-n8n-1`).

"Last night" = yesterday's US Eastern game date (the scripts default to it). If the user names a date, use that.

The reel (`build_top10_reel.py`, about 104 s, 1080x1920):
1. **Poster** (4 s, `reel_poster_template.html`): "Last night's top 10" title and the top 3 as cut-out photos in
   jerseys (#2 left, #1 centre, #3 right) with name plates. It is frame 0, so Instagram uses it as the
   cover/thumbnail. **The thumbnail matters to the user. Make it look good.**
2. **Countdown #10 -> #1**, 9 s each: animated card on top; the bottom half shows the photo, then the shot chart.
3. **Outro** (10 s, `reel_outro_template.html`): the top 10 as a table with MIN, FPTS, PTS, REB, AST, STL, BLK.

## 1. Collect the box scores

```
venv/Scripts/python.exe collect_boxscores.py
```
Catches up from the last stored date through yesterday. Check the output: if there were no games, or
fewer than 10 players with fantasy points, tell the user and stop.

## 2. Look at the top 10

```python
import datetime as dt, build_nightly_cards as n
from dotenv import load_dotenv; load_dotenv(".env")
for p in n.load_top(dt.date.fromisoformat("YYYY-MM-DD")):
    print(p["rank"], p["player_name"], p["player_id"], p["team"], "vs" if p["home"] else "@", p["opponent"],
          "W" if p["won"] else "L", p["team_score"], p["opp_score"], "start" if p["starter"] else "bench",
          p["min"], p["pts"], p["fgm"], p["fga"], p["tpm"], p["tpa"], p["ftm"], p["fta"], p["reb"], p["ast"],
          p["stl"], p["blk"], p["tov"], p["plus_minus"], p["fpts"], p["yahoo_rank"])
```

## 3. Write the headlines

Write `Nightly Text/<date>.json`, keyed by ESPN player id (as a string):
```json
{"<player_id>": {"player": "Name", "headline": "Mara goes {perfect} in 18 minutes", "summary": "..."}}
```
Rules (the user's):
- Sports-journalist voice. Keep the headline short (about 35 characters or less; it sits on the reel card).
  `{...}` marks the highlighted phrase. Catchy beats a raw stat line.
- Summary: 2-3 sentences, same voice.
- Use only facts from the box score or the Yahoo rank. Don't invent context (injuries, trades, rookie status).
  Check any "most of the night" or "game-high" claim against all players in the data
  (`build_boxscore_chart.load_game(date, TEAM)[1]`, `min` can be None).
- If two players share a surname, use the first name too.

## 4. Poster photos (top 3)

The user wants the top 3 **in jerseys**, from a **free source**. Use Wikimedia Commons (CC/PD). Don't use press
photos (Getty/AP/Imagn carry copyright-strike risk). Don't use the ESPN headshots for the poster; they're
only the fallback.
- Search: `https://commons.wikimedia.org/w/api.php?action=query&list=search&srnamespace=6&srsearch=<name>`
  (send a User-Agent, print with UTF-8 because names like Şengün break cp1252). Then `prop=imageinfo&iiprop=url|extmetadata&iiurlwidth=1600`
  for the license, artist and thumbnail. Commons rate-limits hard: originals return 429, so use thumbnails and wait 4-20 s
  between requests.
- Make a contact sheet and look at the candidates. Prefer the **current team's jersey**, standing or portrait
  shots with the head and torso visible. Commons photos are often old. If no current-team photo exists, use a neutral kit
  (All-Star, national team) rather than an old team's jersey (it looks wrong on the poster), and tell the user.
- Save to `Reels/posters/<date>/photos/`. Cut out with rembg (in the venv):
  `rembg.remove(img, session=rembg.new_session("isnet-general-use"))`. Crop away other people *before*
  cutting out, then trim to the alpha bbox (threshold ~40). Fade any hard crop edge in the alpha.
- Write `Reels/posters/<date>/poster.json`:
  ```json
  {"players": {"<espn_id>": {"photo": "photos/x_trim.png", "show": 1.0, "source": "<commons page>"}},
   "credits": ["Name: Artist / Wikimedia Commons (CC BY-SA 4.0)"]}
  ```
  `show` = the fraction of the cut-out height to show from the top. Credits are added to the caption automatically.
- Preview the poster alone before the full build (render `poster_body` through `reel_poster_template.html`
  at 540x960). Check that every face is visible, none hidden behind #1, and that the title and plates sit inside the
  3:4 grid crop (y 240-1680 px). Positions are CSS in the template (`.fig.r1/.r2/.r3` left/right/width/bottom,
  `.num` positions). Tweak them per poster.

## 5. Pick the music

Royalty-free tracks with their CC BY 4.0 credits are in `Reel Audio/tracks.json` (Kevin MacLeod /
incompetech.com). The user wants **catchy** music, and a different track from the last recap. Check the `audio` field
in the newest `content/reels/posted/*_top10-recap.json`. New tracks can be downloaded from
`https://incompetech.com/music/royalty-free/mp3-royaltyfree/<Name>.mp3` (add them to tracks.json with the credit line).
Used so far: Volatile_Reaction, Galactic_Rap, Rollin_at_5, Funkorama.
If the user asks for a commercial song, explain that it can't go through the API (Instagram mutes or blocks it; it
can only be added in the Instagram app) and offer a track from the list.

## 6. Build the reel

```
venv/Scripts/python.exe build_top10_reel.py YYYY-MM-DD --track <TrackName>
```
Output: `Reels/recaps/<date>_top10_shots.mp4` (+ `_preview.png` = frame 0), queued in
`content/reels/pending/<date>_00_top10-recap.{mp4,json}` with the caption. Takes a few minutes.

## 7. Review before showing the user

Extract frames with `tools/ffmpeg/bin/ffmpeg.exe` (0 s poster, ~6.5 s #10 photo, ~11 s #10 chart, ~87 s #1,
~100 s outro), stack them with `hstack`/`vstack`, and look at them. Check for text overflow, wrong teams and old
ESPN headshots in the countdown, and mention any outdated photos. The card FPTS counts up during the first ~1.3 s,
so an early frame can show a lower number.

## 8. Report and wait

Tell the user: the games and scores, the top-10 table (#, player, team, FPTS, headline), the poster photo sources
(and any compromise, e.g. a non-current jersey), the track, and anything to check.
**Do not post** until the user explicitly says to post it.

## 9. Post (only after the user says "post it")

```
mv content/reels/pending/<date>_00_top10-recap.* content/reels/approved/
MSYS_NO_PATHCONV=1 docker exec -e N8N_RUNNERS_BROKER_PORT=5690 n8ndocker-n8n-1 n8n execute --id=nbaReelPostWf001 --rawOutput
```
(The extra broker port avoids clashing with the running n8n server.) First make sure `content/reels/approved/` holds
nothing else, because the workflow posts the oldest file there. Confirm success: the run status is success, the
files moved to `content/reels/posted/`, and there is an `IG Publish` id. Report the media id and remind the user to
check the reel in the app for a mistaken music copyright flag (appeal with the CC BY link if so).
