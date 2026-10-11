# db.py: the only file that talks to the database.
# The Discord bot (bot.py) and the website (app.py) both import these functions,
# so neither of them ever writes SQL directly.

# ----------------------------------------------------------------------
# 1.1 Imports and the database path
# ----------------------------------------------------------------------

import sqlite3            # Built into Python. Lets us use a SQLite database (a single file on disk).
import csv                # Built into Python. Reads the clubs.csv spreadsheet your teammate made.
import re                 # Regular expressions. Used later to turn club names into short ids ("usacs").
from pathlib import Path  # Handles file paths in a way that works on Windows, Mac and Linux.
from contextlib import closing  # Makes sure a connection gets closed even if an error happens.
import time               # Timestamps for the "scan now" signal.
import json               # The small progress files the website reads.

# Path(__file__) is the path of THIS file (db.py). .parent is the folder it's in (the project root).
# Building paths from here means the database is always found in the same place,
# no matter which folder you run Python from.
BASE_DIR = Path(__file__).parent

# The "/" operator joins path pieces: <project>/data/app.db
# SQLite creates this file automatically the first time we connect.
DB_PATH = BASE_DIR / "data" / "app.db"


# ----------------------------------------------------------------------
# 1.2 get_conn(): the one place that opens database connections
# ----------------------------------------------------------------------

def get_conn():
    """Open a new connection to the database with our standard settings.

    Every function in this file calls get_conn(), does its work, and then closes
    the connection. We never keep one connection open forever, because the bot and
    the website are two separate programs that share the same database file.
    """
    # Open (or create, if it doesn't exist yet) the database file.
    conn = sqlite3.connect(DB_PATH)

    # By default, rows come back as tuples, so you'd have to write row[1] to get the name.
    # sqlite3.Row lets us write row["name"] instead, and dict(row) turns a row into
    # a normal dictionary, which Flask can send to the browser as JSON.
    conn.row_factory = sqlite3.Row

    # SQLite does NOT check foreign keys unless you turn it on, and the setting resets
    # on every new connection. With it on, saving an event for a club_id that doesn't
    # exist raises an error instead of silently saving a broken event.
    conn.execute("PRAGMA foreign_keys = ON")

    # WAL ("write-ahead log") mode lets one program read while the other writes.
    # Without it, the website reading events while the bot saves one can fail
    # with "database is locked".
    conn.execute("PRAGMA journal_mode = WAL")

    return conn


# HOW EVERY FUNCTION BELOW USES THE DATABASE
#
#     with closing(get_conn()) as conn:
#         conn.execute("...SQL...", (values,))
#         conn.commit()
#
# - "with closing(...)" closes the connection when the block ends, even if an error
#   is raised inside it. Leaving connections open is what causes "database is locked".
# - conn.execute(sql, values) runs one SQL statement. The "?" marks in the SQL are
#   filled in from the values tuple, in order. NEVER build SQL with f-strings: if a
#   club name contained a quote, it would break the query (or worse, run someone's SQL).
# - conn.commit() saves changes. Reads (SELECT) don't need it. Writes (INSERT, UPDATE,
#   DELETE) are thrown away if you forget it.


# ----------------------------------------------------------------------
# 1.3 init_db(): create the tables
# ----------------------------------------------------------------------

def init_db():
    """Create the tables if they don't exist yet. Safe to run every time the app starts."""
    with closing(get_conn()) as conn:
        # executescript() runs several SQL statements in one go (separated by ";").
        conn.executescript("""
            -- One row per club. Filled from data/clubs.csv.
            CREATE TABLE IF NOT EXISTS clubs (
                id               TEXT PRIMARY KEY,   -- short slug like "usacs". PRIMARY KEY = unique + used to look rows up
                name             TEXT NOT NULL,      -- NOT NULL = this column can't be empty
                category         TEXT,
                description      TEXT,
                instagram        TEXT,
                discord_invite   TEXT,
                campus           TEXT,
                tags             TEXT,               -- stored like "coding;career;cs" (split on ";" when needed)
                getinvolved_url  TEXT,
                website          TEXT,               -- the club's own website (the watcher reads it)
                verified         TEXT,
                discord_guild_id TEXT                -- the Discord server linked to this club (bot fills it in)
            );

            -- One row per event. Filled by the Discord bot and the flyer upload page.
            CREATE TABLE IF NOT EXISTS events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,  -- SQLite picks the number for us: 1, 2, 3...
                club_id     TEXT NOT NULL REFERENCES clubs(id), -- REFERENCES = must match an existing club id
                title       TEXT NOT NULL,
                description TEXT,
                start       TEXT NOT NULL,   -- "2026-10-14T19:30" (Eastern time). Text in this format sorts by date correctly.
                "end"       TEXT,            -- quoted because END is a reserved word in SQL
                location    TEXT,
                campus      TEXT,
                free_food   INTEGER DEFAULT 0,  -- SQLite has no true/false type, so we use 0 and 1
                source      TEXT,               -- "discord", "instagram" or "upload"
                source_url  TEXT,               -- link back to the original post
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP,  -- filled in automatically when the row is inserted

                -- The same club can't have two events with the same title at the same time.
                -- This is what stops duplicates when a club posts on both Instagram and Discord.
                UNIQUE (club_id, title, start)
            );

            -- Which user follows which club. One row per (user, club) pair.
            CREATE TABLE IF NOT EXISTS follows (
                user_id TEXT NOT NULL,
                club_id TEXT NOT NULL REFERENCES clubs(id),
                PRIMARY KEY (user_id, club_id)   -- the PAIR must be unique, so no double-follows
            );

            -- Posts the Instagram watcher (watcher.py) has already read, so each post is
            -- sent to Gemini only ONCE, no matter how many times the watcher checks.
            CREATE TABLE IF NOT EXISTS seen_posts (
                source   TEXT NOT NULL,   -- "instagram", "instagram-demo" or "website"
                post_id  TEXT NOT NULL,   -- the post's id from that source
                club_id  TEXT,
                events_found INTEGER DEFAULT 0,
                seen_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (source, post_id)
            );
        """)

        # MIGRATION: databases created before we added the "website" column don't have it.
        # "CREATE TABLE IF NOT EXISTS" never changes an existing table, so add the column by hand.
        # PRAGMA table_info lists a table's columns; row[1] is each column's name.
        columns = [row[1] for row in conn.execute("PRAGMA table_info(clubs)")]
        if "website" not in columns:
            conn.execute("ALTER TABLE clubs ADD COLUMN website TEXT")
            conn.commit()


def instagram_handle(value):
    """'https://www.instagram.com/thehackru/' or '@thehackru' or 'thehackru' -> 'thehackru'"""
    value = (value or "").strip()
    # If it's a link, take the part right after "instagram.com/".
    match = re.search(r"instagram\.com/([A-Za-z0-9._]+)", value)
    if match:
        return match.group(1)
    return value.lstrip("@").strip("/")


# ----------------------------------------------------------------------
# 1.4 make_slug(): turn a club name into a short id
# ----------------------------------------------------------------------

def make_slug(name):
    """'Undergraduate Student Alliance of Computer Scientists (USACS)' -> 'usacs'
       'HackRU' -> 'hackru'
       'Women in Computer Science' -> 'women-in-computer-science'
    """
    # re.search looks for a pattern anywhere in the text.
    #   \(  and  \)   = literal parentheses (the backslash means "the actual ( character")
    #   (.*?)         = capture whatever is between them
    match = re.search(r"\((.*?)\)", name)
    if match:
        name = match.group(1)  # group(1) = the captured part, e.g. "USACS"

    slug = name.lower()
    # [^a-z0-9]+ = "one or more characters that are NOT a lowercase letter or digit".
    # Each run of those (spaces, &, apostrophes...) becomes a single "-".
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    # Remove any "-" left at the start or end (e.g. from a trailing "!").
    return slug.strip("-")


# ----------------------------------------------------------------------
# 1.5 load_clubs_from_csv(): import your teammate's spreadsheet
# ----------------------------------------------------------------------

CSV_PATH = BASE_DIR / "data" / "clubs.csv"


def load_clubs_from_csv(path=CSV_PATH):
    """Insert every club from the CSV, or update it if it's already in the database.
    Returns how many clubs were loaded."""
    # encoding="utf-8-sig": Excel/Google Sheets put an invisible marker at the start
    # of the file. Without "-sig", the first column would be named "﻿name".
    # newline="": the csv module wants this so line endings are handled correctly.
    with open(path, newline="", encoding="utf-8-sig") as f:
        # DictReader uses the first row (the header) as keys, so each row becomes
        # a dict like {"name": "HackRU", "category": "Tech", ...}
        rows = list(csv.DictReader(f))

    count = 0
    loaded_ids = []   # every club id in the CSV, used below to find clubs that were removed
    with closing(get_conn()) as conn:
        for row in rows:
            # .get("name") returns None instead of crashing if the column is missing.
            # "or ''" turns None into an empty string, and .strip() removes spaces.
            name = (row.get("name") or "").strip()
            if not name:
                continue  # skip blank lines in the spreadsheet

            club = {
                "id": make_slug(name),
                "name": name,
                "category": (row.get("category") or "").strip(),
                "description": (row.get("description") or "").strip(),
                # lstrip("@") removes a leading @ in case someone typed "@usacs"
                # Store just the handle ("thehackru"), whether the CSV has a link or "@handle".
                "instagram": instagram_handle(row.get("instagram")),
                "discord_invite": (row.get("discord_invite") or "").strip(),
                "campus": (row.get("campus") or "").strip(),
                "tags": (row.get("tags") or "").strip(),
                "getinvolved_url": (row.get("getinvolved_url") or "").strip(),
                # Optional "website" column in the CSV: the club's own site (not Instagram/Suitable).
                "website": (row.get("website") or "").strip(),
                "verified": (row.get("verified") or "").strip(),
            }

            # An "upsert": INSERT a new club, but if one with the same id already exists
            # (ON CONFLICT), UPDATE it instead. "excluded" = the row we just tried to insert.
            # So when your teammate fixes a description and you reload, it updates instead of crashing.
            # We deliberately DON'T touch discord_guild_id here, so reloading never unlinks the bot.
            #
            # ":name" style placeholders are filled from a dict by key, instead of "?" by position.
            conn.execute("""
                INSERT INTO clubs (id, name, category, description, instagram, discord_invite,
                                   campus, tags, getinvolved_url, website, verified)
                VALUES (:id, :name, :category, :description, :instagram, :discord_invite,
                        :campus, :tags, :getinvolved_url, :website, :verified)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    category = excluded.category,
                    description = excluded.description,
                    instagram = excluded.instagram,
                    discord_invite = excluded.discord_invite,
                    campus = excluded.campus,
                    tags = excluded.tags,
                    getinvolved_url = excluded.getinvolved_url,
                    website = excluded.website,
                    verified = excluded.verified
            """, club)
            count += 1
            loaded_ids.append(club["id"])

        # Remove clubs that are no longer in the CSV (e.g. your teammate renamed or deleted one),
        # so they stop showing on the site. Their follows go too. Clubs that already have events
        # are kept, so we never lose events the bot collected.
        # "?, ?, ?" is built to match how many ids we have: NOT IN (?, ?, ?, ...)
        placeholders = ", ".join("?" for _ in loaded_ids)
        stale = f"""SELECT id FROM clubs WHERE id NOT IN ({placeholders})
                    AND id NOT IN (SELECT club_id FROM events)"""
        # Safety: if the CSV came back empty (broken file), don't wipe every club.
        stale_rows = conn.execute(stale, loaded_ids).fetchall() if loaded_ids else []
        for (stale_id,) in stale_rows:
            conn.execute("DELETE FROM follows WHERE club_id = ?", (stale_id,))
            conn.execute("DELETE FROM clubs WHERE id = ?", (stale_id,))
            print(f"[db] removed club no longer in the CSV: {stale_id}")

        conn.commit()  # save all the inserts at once
    return count


# ----------------------------------------------------------------------
# 1.6 Reading clubs
# ----------------------------------------------------------------------

def get_clubs(user_id=None):
    """Every club, plus "followed": 1 or 0 for this user."""
    with closing(get_conn()) as conn:
        # LEFT JOIN keeps every club, even ones with no matching row in follows.
        # For clubs this user doesn't follow, f.user_id is NULL, so "IS NOT NULL" gives 0.
        rows = conn.execute("""
            SELECT c.*, f.user_id IS NOT NULL AS followed
            FROM clubs c
            LEFT JOIN follows f ON f.club_id = c.id AND f.user_id = ?
            ORDER BY c.name
        """, (user_id,)).fetchall()   # (user_id,) - the comma makes it a 1-item tuple
    # Turn sqlite3.Row objects into plain dicts so Flask can send them as JSON.
    return [dict(r) for r in rows]


def get_club(club_id):
    """One club by id, or None if it doesn't exist."""
    with closing(get_conn()) as conn:
        row = conn.execute("SELECT * FROM clubs WHERE id = ?", (club_id,)).fetchone()
    return dict(row) if row else None


# ----------------------------------------------------------------------
# 1.7 Follows
# ----------------------------------------------------------------------

def follow(user_id, club_id):
    with closing(get_conn()) as conn:
        # OR IGNORE: if the user already follows this club (the primary key already
        # exists), do nothing instead of raising an error.
        conn.execute("INSERT OR IGNORE INTO follows (user_id, club_id) VALUES (?, ?)",
                     (user_id, club_id))
        conn.commit()


def get_followed_club_ids():
    """Ids of every club that at least one user follows. The watcher and bot only scan these."""
    with closing(get_conn()) as conn:
        rows = conn.execute("SELECT DISTINCT club_id FROM follows").fetchall()
    return {row["club_id"] for row in rows}


def unfollow(user_id, club_id):
    with closing(get_conn()) as conn:
        conn.execute("DELETE FROM follows WHERE user_id = ? AND club_id = ?",
                     (user_id, club_id))
        conn.commit()


# ----------------------------------------------------------------------
# 1.8 save_event(): used by the bot and the upload page
# ----------------------------------------------------------------------

# Default values for the optional fields, so callers don't have to pass every key.
EVENT_DEFAULTS = {
    "description": "",
    "end": None,       # None becomes NULL in the database
    "location": "",
    "campus": "",
    "free_food": False,
    "source": "manual",
    "source_url": None,
}


def save_event(event):
    """Insert an event, or update it if the same club already has an event with the
    same title and start time. Returns the event's id.

    event = {"club_id": "usacs", "title": "...", "start": "2026-10-14T19:30", ...}
    """
    # {**A, **B} merges two dicts. Keys in B (the real event) win over A (the defaults).
    data = {**EVENT_DEFAULTS, **event}
    # int(bool(...)) turns True/False/"yes"/None into 1 or 0 for the database.
    data["free_food"] = int(bool(data["free_food"]))

    with closing(get_conn()) as conn:
        row = conn.execute("""
            INSERT INTO events (club_id, title, description, start, "end", location,
                                campus, free_food, source, source_url)
            VALUES (:club_id, :title, :description, :start, :end, :location,
                    :campus, :free_food, :source, :source_url)
            ON CONFLICT(club_id, title, start) DO UPDATE SET
                description = excluded.description,
                "end"       = excluded."end",
                location    = excluded.location,
                campus      = excluded.campus,
                free_food   = excluded.free_food
            RETURNING id
        """, data).fetchone()   # RETURNING id hands back the id of the inserted/updated row
        conn.commit()
    return row["id"]


# ----------------------------------------------------------------------
# 1.9 Reading events
# ----------------------------------------------------------------------

def get_events(user_id=None, followed_only=False):
    """All events (oldest first), each with its club's name attached.
    With followed_only=True, only events from clubs this user follows."""
    # JOIN attaches the matching club row to each event, so we can include c.name.
    sql = """
        SELECT e.*, c.name AS club_name, c.category AS club_category
        FROM events e
        JOIN clubs c ON c.id = e.club_id
    """
    params = ()
    if followed_only and user_id:
        # A subquery: "only events whose club is in the list of clubs this user follows"
        sql += " WHERE e.club_id IN (SELECT club_id FROM follows WHERE user_id = ?)"
        params = (user_id,)
    sql += " ORDER BY e.start"

    with closing(get_conn()) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def delete_event(event_id):
    """Remove an event (useful for cleaning up a bad extraction). True if something was deleted."""
    with closing(get_conn()) as conn:
        cur = conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
        conn.commit()
    return cur.rowcount > 0   # rowcount = how many rows the statement changed


# ----------------------------------------------------------------------
# For the Instagram watcher (watcher.py): remember which posts were already read
# ----------------------------------------------------------------------

def is_post_seen(source, post_id):
    with closing(get_conn()) as conn:
        row = conn.execute("SELECT 1 FROM seen_posts WHERE source = ? AND post_id = ?",
                           (source, post_id)).fetchone()
    return row is not None


def mark_post_seen(source, post_id, club_id, events_found):
    with closing(get_conn()) as conn:
        conn.execute("""INSERT OR REPLACE INTO seen_posts (source, post_id, club_id, events_found)
                        VALUES (?, ?, ?, ?)""", (source, post_id, club_id, events_found))
        conn.commit()


# ----------------------------------------------------------------------
# "Scan now" signal: the control panel asks the bot and the watcher to look for events
# ----------------------------------------------------------------------
# The panel, bot and watcher are separate programs, so they can't call each other's
# functions. Instead the panel writes a small file, and the others check its timestamp
# every few seconds: a newer timestamp than last time = "someone pressed Scan".

SCAN_FLAG = BASE_DIR / "data" / "scan_request.txt"


# Live progress ("scanning club 12 of 37") so the website can show a progress bar.
# Each program writes its OWN small JSON file, so two programs never write the same file.

def write_status(name, data):
    path = BASE_DIR / "data" / f"status_{name}.json"
    temp = path.with_suffix(".tmp")
    # Write to a temp file, then swap it in: the website never reads a half-written file.
    # On Windows the swap FAILS ("Access is denied") if the website is reading the file at
    # that exact moment, so try a few times. This file only feeds the progress bar, so if
    # it still fails we just skip this update: it must NEVER crash the watcher or the bot.
    for _ in range(5):
        try:
            temp.write_text(json.dumps(data), encoding="utf-8")
            temp.replace(path)
            return
        except OSError:
            time.sleep(0.05)


def read_status(name):
    try:
        return json.loads((BASE_DIR / "data" / f"status_{name}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Missing, mid-swap ("Permission denied" on Windows) or half-written: no status this time.
        return None


def request_scan():
    SCAN_FLAG.write_text(str(time.time()), encoding="utf-8")


def new_scan_request(last_seen):
    """Returns the request's timestamp if there's a NEWER request than last_seen, else None."""
    try:
        stamp = float(SCAN_FLAG.read_text(encoding="utf-8").strip() or 0)
    except (FileNotFoundError, ValueError):
        return None
    return stamp if stamp > last_seen else None


# ----------------------------------------------------------------------
# For step 3 (Discord bot): which club is a Discord server linked to?
# ----------------------------------------------------------------------

def link_guild(club_id, guild_id):
    """Connect a Discord server (guild) to a club. Returns True if the club exists."""
    with closing(get_conn()) as conn:
        # A server can only belong to one club, so unlink it from any other club first.
        conn.execute("UPDATE clubs SET discord_guild_id = NULL WHERE discord_guild_id = ?",
                     (guild_id,))
        cur = conn.execute("UPDATE clubs SET discord_guild_id = ? WHERE id = ?",
                           (guild_id, club_id))
        conn.commit()
    return cur.rowcount > 0


def get_club_by_guild(guild_id):
    """The club linked to this Discord server, or None."""
    with closing(get_conn()) as conn:
        row = conn.execute("SELECT * FROM clubs WHERE discord_guild_id = ?",
                           (guild_id,)).fetchone()
    return dict(row) if row else None


# ----------------------------------------------------------------------
# 1.10 Test block: only runs when you type "python db.py"
# (NOT when another file does "import db")
# ----------------------------------------------------------------------

if __name__ == "__main__":
    init_db()
    print("Loaded", load_clubs_from_csv(), "clubs")

    print("\nFirst 3 clubs:")
    for club in get_clubs()[:3]:
        print(" ", club["id"], "|", club["name"])

    # Dedupe test: save the same event twice with a different location.
    # There should be ONE event afterwards, with the NEW location.
    test_event = {"club_id": "usacs", "title": "TEST General Body Meeting",
                  "start": "2026-10-14T19:30", "location": "Hill 114", "free_food": True,
                  "source": "test"}
    first_id = save_event(test_event)
    second_id = save_event({**test_event, "location": "CoRE 101"})
    tests = [e for e in get_events() if e["title"].startswith("TEST")]
    print("\nDedupe test:", "PASS" if first_id == second_id and len(tests) == 1
          and tests[0]["location"] == "CoRE 101" else "FAIL", tests)

    # Follow test
    follow("test-user", "usacs")
    followed = [c["id"] for c in get_clubs("test-user") if c["followed"]]
    mine = get_events("test-user", followed_only=True)
    print("Follow test:", "PASS" if followed == ["usacs"] and all(e["club_id"] == "usacs" for e in mine)
          else "FAIL", followed)

    # Clean up the test data so it doesn't show on the real calendar.
    delete_event(first_id)
    unfollow("test-user", "usacs")
    print("\nCleaned up test data. db.py works!")


