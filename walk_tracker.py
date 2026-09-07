#!/usr/bin/env python3
import datetime as dt
import os
import sqlite3
import time
from zoneinfo import ZoneInfo
import requests

BASE = "https://statsapi.mlb.com/api/v1"
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "8"))
DB_PATH = os.getenv("DB_PATH", "walks.db")
WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

if not WEBHOOK:
    raise SystemExit("Missing DISCORD_WEBHOOK_URL environment variable.")

session = requests.Session()
session.headers.update({"User-Agent": "MLB-Walk-Discord-Tracker/1.1"})

def get_json(url, params=None):
    r = session.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()

def schedule_for(date_str):
    data = get_json(
        f"{BASE}/schedule",
        {"sportId": 1, "date": date_str, "hydrate": "team,linescore"},
    )
    games = []
    for d in data.get("dates", []):
        games.extend(d.get("games", []))
    return games

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS walks (
            event_key TEXT PRIMARY KEY,
            game_pk INTEGER,
            game_date TEXT,
            batter TEXT,
            pitcher TEXT,
            inning INTEGER,
            half TEXT,
            away_team TEXT,
            home_team TEXT,
            away_score INTEGER,
            home_score INTEGER,
            event TEXT,
            captured_at TEXT
        )
    """)
    con.commit()
    return con

def is_walk(play):
    result = play.get("result", {})
    event_type = (result.get("eventType") or "").lower()
    event = (result.get("event") or "").lower()
    return event_type in {"walk", "intent_walk", "intentional_walk"} or event in {
        "walk", "intent walk", "intentional walk"
    }

def post_discord(content):
    r = session.post(
        WEBHOOK,
        json={"content": content, "username": "MLB Walk Tracker"},
        timeout=20,
    )
    r.raise_for_status()

def count_today(con, game_date):
    return con.execute(
        "SELECT COUNT(*) FROM walks WHERE game_date = ?",
        (game_date,),
    ).fetchone()[0]

def process_game(con, game, game_date):
    pbp = get_json(f"{BASE}/game/{game['gamePk']}/playByPlay")

    for play in pbp.get("allPlays", []):
        if not is_walk(play):
            continue

        about = play.get("about", {})
        matchup = play.get("matchup", {})
        result = play.get("result", {})
        at_bat_index = about.get("atBatIndex")
        key = f"{game['gamePk']}:{at_bat_index}"

        row = {
            "event_key": key,
            "game_pk": game["gamePk"],
            "game_date": game_date,
            "batter": matchup.get("batter", {}).get("fullName", "Unknown"),
            "pitcher": matchup.get("pitcher", {}).get("fullName", "Unknown"),
            "inning": about.get("inning"),
            "half": about.get("halfInning"),
            "away_team": game["teams"]["away"]["team"]["name"],
            "home_team": game["teams"]["home"]["team"]["name"],
            "away_score": result.get("awayScore"),
            "home_score": result.get("homeScore"),
            "event": result.get("event"),
            "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }

        cur = con.execute("""
            INSERT OR IGNORE INTO walks VALUES (
                :event_key,:game_pk,:game_date,:batter,:pitcher,:inning,:half,
                :away_team,:home_team,:away_score,:home_score,:event,:captured_at
            )
        """, row)
        con.commit()

        if cur.rowcount:
            total = count_today(con, game_date)
            half = "Top" if row["half"] == "top" else "Bottom"
            intentional = " (IBB)" if "intent" in (row["event"] or "").lower() else ""
            score = ""
            if row["away_score"] is not None and row["home_score"] is not None:
                score = (
                    f"\n**Score:** {row['away_team']} {row['away_score']} – "
                    f"{row['home_team']} {row['home_score']}"
                )

            msg = (
                f"🚶 **MLB WALK ALERT{intentional}**\n"
                f"**Batter:** {row['batter']}\n"
                f"**Pitcher:** {row['pitcher']}\n"
                f"**Game:** {row['away_team']} @ {row['home_team']}\n"
                f"**Inning:** {half} {row['inning']}"
                f"{score}\n"
                f"**Walks tracked today:** {total}"
            )

            print(msg, flush=True)
            post_discord(msg)

def main():
    con = init_db()
    eastern = ZoneInfo("America/New_York")
    current_date = None

    while True:
        try:
            now_et = dt.datetime.now(eastern)
            today_et = now_et.date()
            yesterday_et = today_et - dt.timedelta(days=1)

            today = today_et.isoformat()
            yesterday = yesterday_et.isoformat()

            if today != current_date:
                current_date = today
                print(f"Tracking MLB games using Eastern date: {today}", flush=True)

            # Check both today's and yesterday's MLB slates so late games
            # remain tracked after midnight Eastern.
            games = schedule_for(today) + schedule_for(yesterday)

            seen_game_pks = set()
            for game in games:
                game_pk = game["gamePk"]
                if game_pk in seen_game_pks:
                    continue
                seen_game_pks.add(game_pk)

                state = game.get("status", {}).get("detailedState", "")
                if state in {
                    "In Progress",
                    "Manager Challenge",
                    "Review",
                    "Delayed",
                }:
                    game_date = (game.get("gameDate") or today)[:10]
                    process_game(con, game, game_date)

        except Exception as exc:
            print(f"Temporary error: {exc}", flush=True)

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
