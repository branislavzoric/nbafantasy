-- Players +/- : schema "props" (same nba_fantasy database as the "odds" schema).
--
-- What is stored where (no duplicated data):
--   NBA box scores  -> public.nba_games / public.nba_boxscores   (existing, collect_boxscores.py)
--   EL box scores   -> props.el_games / props.el_boxscores      (new, players_pm/ingest_el.py)
--   Bookmaker lines -> odds.scrape_runs / odds.games / odds.player_props (existing, insert-only:
--                      every scrape run is kept, so line history is never overwritten)
--   props.*         -> teams, players, name matching, job log, and the views that tie it together.
-- Re-runnable: every statement is IF NOT EXISTS / OR REPLACE.

CREATE SCHEMA IF NOT EXISTS props;

CREATE TABLE IF NOT EXISTS props.leagues (
    league  text PRIMARY KEY,          -- 'nba' | 'el'
    name    text NOT NULL,
    odds_league_id text NOT NULL       -- odds.scrape_runs.league_id
);
INSERT INTO props.leagues VALUES ('nba', 'NBA', 'nba'), ('el', 'EuroLeague', 'euroleague')
ON CONFLICT (league) DO NOTHING;

CREATE TABLE IF NOT EXISTS props.bookmakers (
    book    text PRIMARY KEY,          -- = odds.player_props.source
    name    text NOT NULL,
    active  boolean NOT NULL DEFAULT true
);
INSERT INTO props.bookmakers VALUES ('soccerbet', 'Soccerbet', true) ON CONFLICT (book) DO NOTHING;

-- ---------------------------------------------------------------- EuroLeague box scores
CREATE TABLE IF NOT EXISTS props.el_games (
    game_id     text PRIMARY KEY,      -- 'E2026_81'
    season_code text NOT NULL,         -- 'E2026' = 2026-27
    game_code   int  NOT NULL,
    phase       text,                  -- RS / PI / PO / FF
    round       int,
    start_utc   timestamptz NOT NULL,
    home_code   text NOT NULL,
    away_code   text NOT NULL,
    home_score  int,
    away_score  int,
    played      boolean NOT NULL,
    box_final   boolean NOT NULL DEFAULT false,   -- box score fetched after the game ended
    fetched_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS el_games_start ON props.el_games (start_utc);

CREATE TABLE IF NOT EXISTS props.el_boxscores (
    game_id     text NOT NULL REFERENCES props.el_games (game_id) ON DELETE CASCADE,
    player_id   text NOT NULL,         -- EuroLeague person code, e.g. 'P011157'
    player_name text NOT NULL,         -- 'MOTLEY, JOHNATHAN' as the box score writes it
    team_code   text NOT NULL,
    starter     boolean NOT NULL,
    played      boolean NOT NULL,      -- minutes > 0; false = in the box score but did not play (DNP)
    minutes     numeric(5,2),
    pts int, fgm2 int, fga2 int, tpm int, tpa int, ftm int, fta int,
    oreb int, dreb int, reb int, ast int, stl int, blk int, tov int, pf int, pir int,
    PRIMARY KEY (game_id, player_id)
);
CREATE INDEX IF NOT EXISTS el_box_player ON props.el_boxscores (player_id);

-- ---------------------------------------------------------------- teams + players
CREATE TABLE IF NOT EXISTS props.teams (
    league  text NOT NULL REFERENCES props.leagues,
    code    text NOT NULL,             -- ESPN abbreviation (NBA) / EuroLeague club code (EL)
    name    text NOT NULL,
    short   text,
    logo    text,
    PRIMARY KEY (league, code)
);

-- Every way a source writes a team ("Crvena Zvezda", "Denver Nuggets") -> team code
CREATE TABLE IF NOT EXISTS props.team_aliases (
    league  text NOT NULL,
    alias   text NOT NULL,             -- normalised (lowercase ascii)
    code    text NOT NULL,
    PRIMARY KEY (league, alias)
);

CREATE TABLE IF NOT EXISTS props.players (
    league     text NOT NULL REFERENCES props.leagues,
    player_id  text NOT NULL,          -- ESPN athlete id (NBA) / EuroLeague person code (EL)
    name       text NOT NULL,          -- display name, "First Last"
    team_code  text,                   -- team of the most recent game
    last_game  date,
    PRIMARY KEY (league, player_id)
);

-- Bookmaker player name -> stats player. player_id NULL = could not be matched (shown as
-- "no history" on the site, never guessed). method: exact / initial / fuzzy / manual.
CREATE TABLE IF NOT EXISTS props.player_aliases (
    league     text NOT NULL,
    book       text NOT NULL,
    book_name  text NOT NULL,
    book_team  text,
    player_id  text,
    method     text,
    score      numeric(4,3),
    first_seen timestamptz NOT NULL DEFAULT now(),
    last_seen  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (league, book, book_name)
);

-- ---------------------------------------------------------------- job log / freshness
CREATE TABLE IF NOT EXISTS props.job_runs (
    run_id      serial PRIMARY KEY,
    job         text NOT NULL,          -- el_stats / nba_stats / lines / match / export
    league      text,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status      text NOT NULL DEFAULT 'running',   -- running / ok / partial / failed
    rows        int,
    message     text
);
CREATE INDEX IF NOT EXISTS job_runs_job ON props.job_runs (job, started_at DESC);

-- ---------------------------------------------------------------- unified game log
-- ---------------------------------------------------------------- NBA play-by-play derived data
-- (players_pm.pbp; rebuilt from the cached ESPN summaries in "Players PM/raw/nba", never hand-edited)

-- Where a box-score row came from: 'box' = ESPN box score (default), 'pbp' = ESPN listed the player
-- without an athlete id and blank stats, so the row was rebuilt from the official play-by-play.
ALTER TABLE public.nba_boxscores ADD COLUMN IF NOT EXISTS box_source text NOT NULL DEFAULT 'box';

-- One row per stretch of play with an unchanged 5-man unit for one team.
CREATE TABLE IF NOT EXISTS props.nba_stints (
    game_id      text NOT NULL,
    team         text NOT NULL,
    stint_no     int  NOT NULL,
    period       int  NOT NULL,
    start_s      int  NOT NULL,              -- game seconds elapsed (0 = tip-off)
    end_s        int  NOT NULL,
    players      text[] NOT NULL,            -- ESPN athlete ids on the floor, sorted
    pts_for      int, pts_against int,
    poss_for     numeric(6,2), poss_against numeric(6,2),   -- FGA - OREB + TOV + 0.44*FTA
    PRIMARY KEY (game_id, team, stint_no)
);
CREATE INDEX IF NOT EXISTS nba_stints_players ON props.nba_stints USING gin (players);

-- Per player-game advanced numbers from the play-by-play (on-court only).
CREATE TABLE IF NOT EXISTS props.player_game_adv (
    game_id        text NOT NULL,
    player_id      text NOT NULL,
    team           text NOT NULL,
    sec_played     int,                      -- exact, from substitutions
    sec_box        int,                      -- box score minutes * 60 (ESPN rounds to whole minutes)
    poss_on        numeric(6,2),             -- team possessions while on the floor
    player_use     numeric(6,2),             -- FGA + 0.44*FTA + TOV (play-by-play)
    team_use_on    numeric(6,2),             -- the same for the whole team while he was on the floor
    usg_pct        numeric(5,2),             -- 100 * player_use / team_use_on
    oreb_chances   int,                      -- own team missed shots (FG + live last FT) while on
    dreb_chances   int,                      -- opponent missed shots while on
    tm_fgm_on      int,                      -- teammates' made FG while on (assist-chance proxy)
    tm_fga_on      int,
    opp_fga_on     int,
    pbp_ok         boolean NOT NULL,         -- |sec_played - sec_box| <= 90
    PRIMARY KEY (game_id, player_id)
);

-- Official NBA injury report (official.nba.com PDFs, published every 15 min). Insert-only snapshots.
CREATE TABLE IF NOT EXISTS props.nba_injury_report (
    report_ts    timestamp NOT NULL,         -- report time, US Eastern, as printed
    game_date    date NOT NULL,
    game_time    text,
    matchup      text,                       -- 'BOS@CHA'
    team         text NOT NULL,              -- ESPN abbreviation
    player_name  text NOT NULL,              -- 'First Last' (the PDF prints 'Last, First')
    player_id    text,                       -- ESPN athlete id, NULL if not matched
    status       text NOT NULL,              -- Out / Doubtful / Questionable / Probable / Available / Not Yet Submitted
    reason       text,
    source_url   text NOT NULL,
    fetched_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (report_ts, team, player_name)
);
CREATE INDEX IF NOT EXISTS nba_ir_game ON props.nba_injury_report (game_date, team);

-- Data-quality runs (players_pm.validate)
CREATE TABLE IF NOT EXISTS props.data_checks (
    checked_at  timestamptz NOT NULL DEFAULT now(),
    league      text NOT NULL,
    check_name  text NOT NULL,
    ok          boolean NOT NULL,
    value       numeric,
    detail      jsonb,
    PRIMARY KEY (checked_at, league, check_name)
);

-- One row per player per FINAL game, NBA + EL, regular season / play-in / playoffs only.
-- status: 'played' or 'dnp' (in the box score, 0 minutes). DNP rows never count as Over/Under.
-- All-Star games (non-NBA team codes) are excluded. Columns after 'position' were added 2026-11-06
-- (CREATE OR REPLACE VIEW can only append columns).
CREATE OR REPLACE VIEW props.player_games AS
SELECT 'nba'::text                                   AS league,
       b.game_id,
       CASE WHEN g.season IS NULL THEN NULL
            ELSE (g.season - 1)::text || '-' || right(g.season::text, 2) END AS season,
       g.season_type                                  AS phase,
       g.start_time                                   AS start_utc,
       b.game_date,
       b.player_id,
       b.player_name,
       b.team                                         AS team_code,
       b.opponent                                     AS opp_code,
       (b.team = g.home_team)                         AS is_home,
       CASE WHEN b.did_not_play OR COALESCE(b.min, 0) = 0 THEN 'dnp' ELSE 'played' END AS status,
       b.dnp_reason,
       b.starter,
       b.min::numeric                                 AS minutes,
       b.pts, b.reb, b.ast, b.tpm, b.stl, b.blk, b.tov,
       b.fga, b.fta, b.tpa, b.oreb, b.pf, b.position,
       b.fgm, b.ftm, b.dreb, b.plus_minus,
       b.box_source,
       a.sec_played, a.usg_pct, a.poss_on, a.oreb_chances, a.dreb_chances, a.tm_fgm_on, a.pbp_ok
FROM public.nba_boxscores b
JOIN public.nba_games g USING (game_id)
LEFT JOIN props.player_game_adv a ON a.game_id = b.game_id AND a.player_id = b.player_id
WHERE g.status = 'STATUS_FINAL' AND g.season_type <> 'preseason'
  AND EXISTS (SELECT 1 FROM props.teams t WHERE t.league = 'nba' AND t.code = g.home_team)
  AND EXISTS (SELECT 1 FROM props.teams t WHERE t.league = 'nba' AND t.code = g.away_team)
UNION ALL
SELECT 'el',
       b.game_id,
       substr(g.season_code, 2)::int::text || '-' || right((substr(g.season_code, 2)::int + 1)::text, 2),
       g.phase,
       g.start_utc,
       (g.start_utc AT TIME ZONE 'Europe/Belgrade')::date,
       b.player_id,
       b.player_name,
       b.team_code,
       CASE WHEN b.team_code = g.home_code THEN g.away_code ELSE g.home_code END,
       (b.team_code = g.home_code),
       CASE WHEN b.played AND COALESCE(b.minutes, 0) > 0 THEN 'played' ELSE 'dnp' END,
       NULL,
       b.starter,
       b.minutes,
       b.pts, b.reb, b.ast, b.tpm, b.stl, b.blk, b.tov,
       COALESCE(b.fga2, 0) + COALESCE(b.tpa, 0), b.fta, b.tpa, b.oreb, b.pf, NULL::text,
       COALESCE(b.fgm2, 0) + COALESCE(b.tpm, 0), b.ftm, b.dreb, NULL::int,
       'box',
       NULL::int, NULL::numeric, NULL::numeric, NULL::int, NULL::int, NULL::int, NULL::boolean
FROM props.el_boxscores b
JOIN props.el_games g USING (game_id)
WHERE g.box_final;

-- Current Soccerbet (or any active book) lines: the newest run per book + league,
-- Over/Under side by side. Milestones ("15+") become Over (N - 0.5) with no Under price.
CREATE OR REPLACE VIEW props.current_lines AS
WITH latest AS (
    SELECT DISTINCT ON (r.source, r.league_id) r.run_id, r.source, r.league_id, r.scraped_at
    FROM odds.scrape_runs r
    JOIN props.bookmakers b ON b.book = r.source AND b.active
    WHERE r.odds > 0
    ORDER BY r.source, r.league_id, r.run_id DESC
), lines AS (
    SELECT l.league_id, p.source AS book, l.scraped_at, p.game_id, p.team, p.player, p.market,
           CASE WHEN p.side IN ('Over', 'Under') THEN p.line
                ELSE (regexp_replace(p.side, '\+$', ''))::numeric - 0.5 END     AS line,
           (p.side NOT IN ('Over', 'Under'))                                    AS milestone,
           CASE WHEN p.side = 'Under' THEN 'Under' ELSE 'Over' END              AS side,
           p.odds
    FROM odds.player_props p
    JOIN latest l ON l.run_id = p.run_id
    WHERE p.market NOT IN ('Double-double', '1Q Points', '1H Points')
      AND (p.side IN ('Over', 'Under') OR p.side ~ '^\d+\+$')
)
SELECT lg.league, x.book, x.scraped_at, x.game_id, g.home, g.away, g.kickoff,
       x.team, x.player, x.market, x.line, x.milestone,
       max(x.odds) FILTER (WHERE x.side = 'Over')  AS over_odds,
       max(x.odds) FILTER (WHERE x.side = 'Under') AS under_odds
FROM lines x
JOIN props.leagues lg ON lg.odds_league_id = x.league_id
LEFT JOIN odds.games g ON g.source = x.book AND g.game_id = x.game_id
GROUP BY lg.league, x.book, x.scraped_at, x.game_id, g.home, g.away, g.kickoff,
         x.team, x.player, x.market, x.line, x.milestone;

-- ---------------------------------------------------------------- prediction engine inputs/outputs
-- Game markets (total, spread, team totals, moneyline) per Soccerbet scrape: insert-only snapshots,
-- so line movement can be shown and, once enough history exists, used as a model feature.
CREATE TABLE IF NOT EXISTS props.game_lines (
    scraped_at   timestamptz NOT NULL DEFAULT now(),
    league       text NOT NULL,
    book         text NOT NULL,
    book_game    text NOT NULL,
    home         text NOT NULL,          -- bookmaker team names
    away         text NOT NULL,
    kickoff      timestamp NOT NULL,     -- Belgrade local, as odds.games
    total        numeric(6,1), total_over numeric(6,2), total_under numeric(6,2),
    spread_home  numeric(5,1), spread_home_odds numeric(6,2), spread_away_odds numeric(6,2),
    home_total   numeric(6,1), away_total numeric(6,1),
    ml_home      numeric(6,2), ml_away numeric(6,2),
    PRIMARY KEY (book, book_game, scraped_at)
);

-- Injury report snapshots (NBA: ESPN; the EuroLeague has no public injury feed).
CREATE TABLE IF NOT EXISTS props.injuries (
    fetched_at  timestamptz NOT NULL,
    league      text NOT NULL,
    player_id   text NOT NULL,
    player_name text,
    team_code   text,
    status      text,                    -- Out / Doubtful / Questionable / Day-To-Day / ...
    detail      text,
    reported_at timestamptz,
    PRIMARY KEY (fetched_at, league, player_id)
);

-- Every model prediction that was published (insert-only), so the live record can be audited
-- against the closing line and the result later, not only the backtest.
CREATE TABLE IF NOT EXISTS props.predictions (
    created_at    timestamptz NOT NULL DEFAULT now(),
    model_version text NOT NULL,
    day           date NOT NULL,
    league        text NOT NULL,
    game_id       text NOT NULL,
    player_id     text NOT NULL,
    market        text NOT NULL,
    mu            numeric(7,3) NOT NULL,   -- expected value (given he plays)
    var           numeric(9,3) NOT NULL,
    min_hat       numeric(5,2),
    line          numeric(5,1),            -- main book line at prediction time
    p_over        numeric(5,4),
    p_under       numeric(5,4),
    detail        jsonb,
    PRIMARY KEY (model_version, day, league, game_id, player_id, market, created_at)
);
