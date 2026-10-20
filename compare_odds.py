#!/usr/bin/env python3
"""
compare_odds.py

Matches games and players across bookmakers (latest scrape of each book) and compares the
player-prop odds. Run scrape_odds.py first.

Matching
  games   : same kickoff (+-30 min) and similar team names ("Maccabi T.A." ~ "Maccabi Tel Aviv")
  players : within a matched game, by surname + first name/initial
            ("Dwayne Bacon" = "D.Bacon" = "Bacon, Dwayne"; "Ja.Butler" = "Jared Butler")
  Milestones count as overs: "15+" pays exactly like Over 14.5, so they're compared together.

Writes (schema "odds", rebuilt on every run):
  odds.cmp_games, odds.cmp_game_links, odds.cmp_players, odds.cmp_player_links
  view odds.compare  one row per game/player/market/side/line with every book's odds side by side
And "Odds/compare/<date>/":
  odds_comparison.csv  everything, books as columns + best price
  value_bets.csv       prices above the no-vig consensus of the other books
  arbitrage.csv        lines where the best over + best under across books guarantee a profit
  unmatched.csv        player names that couldn't be matched

Usage:
  python compare_odds.py [--min-books 3] [--min-edge 0.03]
"""

import argparse
import datetime as dt
import difflib
import os
import re
import unicodedata
from collections import defaultdict

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(PROJECT_DIR, "Odds", "compare")
BOOKS = ["soccerbet", "maxbet", "admiralbet", "mozzartbet", "superbet", "balkanbet"]
# Books with full player names go first so they define the players; initials attach to them
NAME_PRIORITY = ["soccerbet", "admiralbet", "maxbet", "superbet", "balkanbet", "mozzartbet"]
SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}
CLUB_NOISE = {"bc", "kk", "fc", "sk", "jk", "bk", "basket", "basketball", "belgrade", "istanbul",
              "piraeus", "mozzartbet", "the", "de"}

# -----------------------------------------------------------------------------
# NAMES
# -----------------------------------------------------------------------------

def plain(s):
    s = s if isinstance(s, str) else ""
    s = s.replace("đ", "d").replace("Đ", "D")
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def parse_player(name):
    """-> (first, surname, tokens): first is a full first name or an initial prefix ("ja"),
    surname the last real token, tokens every token after the first name.
    "T.F.Jekiri" -> ("t", "jekiri"), "Nigel Hayes-Davis" -> ("nigel", "davis", {"hayes", "davis"})."""
    s = plain(name)
    initial = None
    m = re.match(r"^([a-z]{1,3})\.\s*", s)
    if m:
        initial, s = m.group(1), s[m.end():]
        s = re.sub(r"^([a-z]{1,2}\.\s*)+", "", s)          # extra initials: T.F.Jekiri
    tokens = [t for t in re.split(r"[^a-z]+", s) if t and t not in SUFFIXES]
    if not tokens:
        return initial or "", "", set()
    if initial:
        return initial, tokens[-1], set(tokens)
    if len(tokens) == 1:
        return "", tokens[0], set(tokens)
    return tokens[0], tokens[-1], set(tokens[1:])


def same_surname(a, b):
    """a, b = (surname, tokens). Exact, part of a double-barrelled name, or a near spelling."""
    (sa, ta), (sb, tb) = a, b
    return (sa == sb or sa in tb or sb in ta
            or difflib.SequenceMatcher(None, sa, sb).ratio() >= 0.88)


def team_words(name):
    return {w for w in re.split(r"[^a-z0-9]+", plain(name)) if len(w) > 2 and w not in CLUB_NOISE}


def team_similarity(a, b):
    wa, wb = team_words(a), team_words(b)
    if not wa or not wb:
        return 0.0
    if wa & wb:
        return 1.0
    return max(difflib.SequenceMatcher(None, x, y).ratio() for x in wa for y in wb)

# -----------------------------------------------------------------------------
# LOAD + MATCH
# -----------------------------------------------------------------------------

def load(engine):
    with engine.connect() as conn:
        props = pd.read_sql(text("SELECT * FROM odds.player_props_latest"), conn)
        games = pd.read_sql(text("""
            SELECT g.* FROM odds.games g
            WHERE EXISTS (SELECT 1 FROM odds.player_props_latest p
                          WHERE p.source = g.source AND p.game_id = g.game_id)"""), conn)
    # A book with no fresh offer keeps its last run as "latest" -- drop games that have
    # already started (kickoff is local time)
    games = games[pd.to_datetime(games["kickoff"]) > pd.Timestamp.now()]
    keys = set(zip(games["source"], games["game_id"]))
    props = props[[k in keys for k in zip(props["source"], props["game_id"])]]
    return props, games


def match_games(games):
    """-> canonical games list + {(source, game_id): cgame index}"""
    canon, link = [], {}
    order = sorted(games.to_dict("records"), key=lambda g: (NAME_PRIORITY.index(g["source"]), g["kickoff"]))
    for g in order:
        best, best_score = None, 0.0
        for i, c in enumerate(canon):
            if abs((c["kickoff"] - g["kickoff"]).total_seconds()) > 1800:
                continue
            score = team_similarity(c["home"], g["home"]) + team_similarity(c["away"], g["away"])
            if score > best_score:
                best, best_score = i, score
        if best is not None and best_score >= 1.6:
            link[(g["source"], g["game_id"])] = best
        else:
            canon.append({"home": g["home"], "away": g["away"], "kickoff": g["kickoff"]})
            link[(g["source"], g["game_id"])] = len(canon) - 1
    return canon, link


def side_of_team(team, cgame):
    if not team:
        return None
    h, a = team_similarity(team, cgame["home"]), team_similarity(team, cgame["away"])
    return "home" if h > a and h >= 0.8 else "away" if a > h and a >= 0.8 else None


def match_players(props, canon, glink):
    """-> canonical players list + {(source, game_id, player): cplayer index} + unmatched rows."""
    players, plink, unmatched = [], {}, []
    names = (props[["source", "game_id", "player", "team"]].drop_duplicates()
             .assign(prio=lambda d: d["source"].map(NAME_PRIORITY.index))
             .sort_values(["prio", "player"]).to_dict("records"))
    by_game = defaultdict(list)                    # cgame -> indices into players
    for n in names:
        cg = glink.get((n["source"], n["game_id"]))
        if cg is None:
            continue
        first, surname, tokens = parse_player(n["player"])
        side = side_of_team(n["team"], canon[cg])

        def candidates(first, surname, tokens):
            out = []
            for i in by_game[cg]:
                p = players[i]
                if not same_surname((p["surname"], p["tokens"]), (surname, tokens)):
                    continue
                # "Ja" ~ "Jared", "Nate" ~ "Nathan", "Will" ~ "William": same first letter is enough,
                # a longer shared prefix ranks higher when two players share a surname
                first_ok = not first or not p["first"] or p["first"][0] == first[0]
                side_ok = not side or not p["side"] or side == p["side"]
                if first_ok and side_ok:
                    out.append((len(os.path.commonprefix([p["first"], first])), p["surname"] == surname, i))
            return sorted(out, reverse=True)

        cands = candidates(first, surname, tokens)
        if not cands and first and len(first) > 3:       # "Badio Brancou" for Brancou Badio
            cands = candidates(surname, first, {first})
        if cands:
            if len(cands) > 1 and cands[0][:2] == cands[1][:2]:
                unmatched.append({**n, "reason": "ambiguous: " + " / ".join(players[c[2]]["name"] for c in cands)})
                continue
            i = cands[0][2]
            p = players[i]
            if not p["side"] and side:
                p["side"] = side
            if len(p["first"]) <= 3 and len(first) > len(p["first"]):   # replace an initial with a full name
                p.update(first=first, name=n["player"])
            plink[(n["source"], n["game_id"], n["player"])] = i
        else:
            players.append({"cgame": cg, "name": n["player"], "first": first, "surname": surname,
                            "tokens": tokens, "side": side})
            by_game[cg].append(len(players) - 1)
            plink[(n["source"], n["game_id"], n["player"])] = len(players) - 1
    # Players only one initials-only book knows can't be checked against anything -> report them
    seen_by = defaultdict(set)
    for (src, _, _), i in plink.items():
        seen_by[i].add(src)
    for (src, gid, name), i in plink.items():
        if seen_by[i] == {src} and src in ("mozzartbet", "balkanbet") and len(players[i]["first"]) <= 3:
            unmatched.append({"source": src, "game_id": gid, "player": name, "team": None,
                              "reason": "only this book lists the player"})
    return players, plink, unmatched


def save_links(engine, canon, glink, players, plink):
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS odds.cmp_games (cgame_id int PRIMARY KEY, home text, away text,
                                                       kickoff timestamp, built_at timestamptz DEFAULT now());
            CREATE TABLE IF NOT EXISTS odds.cmp_game_links (source text, game_id bigint, cgame_id int,
                                                            PRIMARY KEY (source, game_id));
            CREATE TABLE IF NOT EXISTS odds.cmp_players (cplayer_id int PRIMARY KEY, cgame_id int,
                                                         player text, team text);
            CREATE TABLE IF NOT EXISTS odds.cmp_player_links (source text, game_id bigint, player text,
                                                              cplayer_id int, PRIMARY KEY (source, game_id, player));
            TRUNCATE odds.cmp_games, odds.cmp_game_links, odds.cmp_players, odds.cmp_player_links;"""))
        conn.execute(text("INSERT INTO odds.cmp_games (cgame_id, home, away, kickoff) VALUES (:i, :home, :away, :kickoff)"),
                     [{"i": i, **c} for i, c in enumerate(canon)])
        conn.execute(text("INSERT INTO odds.cmp_game_links VALUES (:s, :g, :c)"),
                     [{"s": s, "g": g, "c": c} for (s, g), c in glink.items()])
        conn.execute(text("INSERT INTO odds.cmp_players VALUES (:i, :g, :n, :t)"),
                     [{"i": i, "g": p["cgame"], "n": p["name"],
                       "t": canon[p["cgame"]][p["side"]] if p["side"] else None} for i, p in enumerate(players)])
        conn.execute(text("INSERT INTO odds.cmp_player_links VALUES (:s, :g, :n, :c)"),
                     [{"s": s, "g": g, "n": n, "c": c} for (s, g, n), c in plink.items()])
        cols = ",\n".join(f"  max(odds) FILTER (WHERE source = '{b}') AS {b}" for b in BOOKS)
        conn.execute(text(f"""
            DROP VIEW IF EXISTS odds.compare;
            CREATE VIEW odds.compare AS
            WITH p AS (
              SELECT cl.cplayer_id, pp.source, pp.market,
                     pp.side ~ '^[0-9]+\\+$' AS milestone,
                     CASE WHEN pp.side ~ '^[0-9]+\\+$' THEN 'Over' ELSE pp.side END AS side,
                     CASE WHEN pp.side ~ '^[0-9]+\\+$' THEN replace(pp.side, '+', '')::numeric - 0.5
                          ELSE pp.line END AS line,
                     pp.odds
              FROM odds.player_props_latest pp
              JOIN odds.cmp_player_links cl
                ON cl.source = pp.source AND cl.game_id = pp.game_id AND cl.player = pp.player),
            pb AS (   -- one price per book: a milestone ("15+" = Over 14.5) only fills a gap,
                      -- the book's real over/under at the same line wins
              SELECT p.* FROM p
              WHERE NOT (milestone AND EXISTS (
                  SELECT 1 FROM p q WHERE q.cplayer_id = p.cplayer_id AND q.source = p.source
                    AND q.market = p.market AND q.side = p.side AND q.line = p.line AND NOT q.milestone)))
            SELECT g.home || ' vs ' || g.away AS game, g.kickoff, pl.player, pl.team,
                   p.market, p.side, p.line,
            {cols},
                   max(odds) AS best_odds, count(DISTINCT source) AS books,
                   string_agg(DISTINCT source, ',') FILTER (WHERE milestone) AS milestone_books
            FROM pb p JOIN odds.cmp_players pl USING (cplayer_id) JOIN odds.cmp_games g USING (cgame_id)
            GROUP BY 1, 2, 3, 4, 5, 6, 7"""))

# -----------------------------------------------------------------------------
# ANALYSIS
# -----------------------------------------------------------------------------

def analyse(cmp, min_books, min_edge):
    books = [b for b in BOOKS if b in cmp.columns]
    cmp = cmp.copy()
    cmp["best_book"] = cmp[books].idxmax(axis=1)
    cmp["worst_odds"] = cmp[books].min(axis=1)

    # No-vig fair probability per book from its own over/under pair at the same line
    ou = cmp[cmp["side"].isin(["Over", "Under"])]
    key = ["game", "player", "market", "line"]
    over = ou[ou["side"] == "Over"].set_index(key)[books]
    under = ou[ou["side"] == "Under"].set_index(key)[books]
    over, under = over.align(under, join="inner")
    p_over = (1 / over) / (1 / over + 1 / under)            # NaN where a book lacks either side
    fair = pd.DataFrame({"fair_over": p_over.mean(axis=1), "pair_books": p_over.notna().sum(axis=1)})

    # Value: a book's price vs the consensus of the OTHER books (leave-one-out, so a book's own
    # outlier price doesn't drag the "fair" line toward itself)
    value = []
    for (game, player, market, line), row in fair.iterrows():
        for side in ("Over", "Under"):
            sel = cmp[(cmp.game == game) & (cmp.player == player) & (cmp.market == market)
                      & (cmp.line == line) & (cmp.side == side)]
            if sel.empty:
                continue
            r = sel.iloc[0]
            for b in books:
                price = r[b]
                if pd.isna(price):
                    continue
                others = p_over.loc[(game, player, market, line)].drop(b).dropna()
                if len(others) < min_books - 1:
                    continue
                p = others.mean() if side == "Over" else 1 - others.mean()
                edge = price * p - 1
                if edge >= min_edge:
                    value.append({"game": game, "player": player, "market": market, "side": side,
                                  "line": line, "book": b, "odds": price, "fair_odds": round(1 / p, 2),
                                  "via_milestone": b in str(r["milestone_books"] or "").split(","),
                                  "edge_pct": round(100 * edge, 1), "books_in_consensus": len(others)})
    value = pd.DataFrame(value).sort_values("edge_pct", ascending=False) if value else pd.DataFrame()

    # Arbitrage: best over and best under (any books) at the same line
    ou = cmp[cmp["side"].isin(["Over", "Under"])].copy()
    ou["best_is_milestone"] = [b in str(m or "").split(",") for b, m in zip(ou.best_book, ou.milestone_books)]
    best_o = ou[ou.side == "Over"].set_index(key)[["best_odds", "best_book", "best_is_milestone"]]
    best_u = ou[ou.side == "Under"].set_index(key)[["best_odds", "best_book"]]
    arb = best_o.join(best_u, lsuffix="_over", rsuffix="_under", how="inner")
    arb["margin_pct"] = 100 * (1 / arb.best_odds_over + 1 / arb.best_odds_under - 1)
    arb = arb[arb.margin_pct < 0].sort_values("margin_pct").reset_index()
    arb["profit_pct"] = (-arb.margin_pct / (100 + arb.margin_pct) * 100).round(2)
    return cmp, value, arb


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-books", type=int, default=3, help="books needed for a consensus line")
    ap.add_argument("--min-edge", type=float, default=0.03, help="edge needed to flag a value bet")
    args = ap.parse_args()
    load_dotenv(os.path.join(PROJECT_DIR, ".env"))
    engine = create_engine(os.environ["DATABASE_URL"])

    props, games = load(engine)
    games = games[games["away"].fillna("") != ""]          # outright "games" like "Evroliga 26/27"
    if props.empty or games.empty:
        raise SystemExit("No upcoming player props from any book -- nothing to compare.")
    canon, glink = match_games(games)
    players, plink, unmatched = match_players(props, canon, glink)
    save_links(engine, canon, glink, players, plink)
    with engine.connect() as conn:
        cmp = pd.read_sql(text("SELECT * FROM odds.compare ORDER BY kickoff, game, player, market, side, line"), conn)
    cmp, value, arb = analyse(cmp, args.min_books, args.min_edge)

    folder = os.path.join(OUT_DIR, dt.date.today().isoformat())
    os.makedirs(folder, exist_ok=True)
    for name, df in (("odds_comparison", cmp), ("value_bets", value), ("arbitrage", arb),
                     ("unmatched", pd.DataFrame(unmatched))):
        path = os.path.join(folder, f"{name}.csv")
        try:
            df.to_csv(path, index=False, encoding="utf-8-sig")
        except PermissionError:
            print(f"Warning: {path} is open in another program -- not updated")

    # ---- summary ----
    per_game = defaultdict(set)
    for (src, gid), cg in glink.items():
        per_game[cg].add(src)
    print(f"{len(canon)} games matched:")
    for i, c in enumerate(canon):
        n = sum(1 for p in players if p["cgame"] == i)
        print(f"  {c['kickoff']:%d.%m %H:%M}  {c['home']} vs {c['away']}: "
              f"{len(per_game[i])} books, {n} players")
    multi = defaultdict(set)
    for (src, _, _), i in plink.items():
        multi[i].add(src)
    print(f"{len(players)} players; {sum(len(s) >= 2 for s in multi.values())} offered by 2+ books, "
          f"{sum(len(s) >= 5 for s in multi.values())} by 5+. Unmatched/flagged names: {len(unmatched)}")
    print(f"{len(cmp)} comparable lines, {len(value)} value bets (edge >= {args.min_edge:.0%}), "
          f"{len(arb)} arbitrage lines")
    if len(value):
        print("\nTop value bets (price vs the other books' no-vig consensus):")
        for _, r in value.head(12).iterrows():
            ms = f"  ({int(r.line + 0.5)}+ milestone)" if r.via_milestone else ""
            print(f"  +{r.edge_pct:>4}%  {r.player:<24} {r.market:<12} {r.side:<5} {r.line:>5}  "
                  f"{r.odds:>5} @ {r.book:<10} (fair {r.fair_odds}){ms}")
    if len(arb):
        print("\nArbitrage (best over + best under across books):")
        for _, r in arb.head(10).iterrows():
            print(f"  {r.profit_pct:>4}%  {r.player:<24} {r.market:<12} {r.line:>5}  "
                  f"O {r.best_odds_over} @ {r.best_book_over}{' (milestone)' if r.best_is_milestone else ''}"
                  f" / U {r.best_odds_under} @ {r.best_book_under}")
    print(f"\nCSVs -> {folder}")


if __name__ == "__main__":
    main()
