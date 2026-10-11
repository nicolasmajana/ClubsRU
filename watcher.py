# watcher.py: automatically checks every club's Instagram for new posts. Run it with:
#     python watcher.py          (checks forever, every WATCH_INTERVAL seconds)
#     python watcher.py --once   (checks one time and stops; handy for testing)
#
# Nobody has to search Instagram. Every few minutes the watcher:
#   1. goes through every club that has an Instagram handle in clubs.csv,
#   2. gets that club's newest posts,
#   3. skips posts it has already read (the seen_posts table in db.py),
#   4. sends each NEW post (caption + flyer image) to Gemini with ai.extract_events(),
#   5. saves any events it finds. They show up on the website and in Google Calendar.
#
# WHERE THE POSTS COME FROM (two "sources"):
#
#   A) Official Instagram API, for clubs that connected their account.
#      A club officer authorizes our app once (Instagram's own login screen), which gives an
#      access token. Tokens go in data/instagram_accounts.json (NOT committed: it's secret):
#          {"thehackru": {"access_token": "IGAA..."}}
#      This is the legitimate, production way: the club consents, and Instagram allows it.
#      NOTE: this path is written but not tested yet, because we don't have a real token.
#
#   B) Demo feed, for the hackathon demo (no Instagram login needed).
#      A folder per club in data/instagram_demo/<handle>/ stands in for that club's feed.
#      Each "post" is an image (flyer.png) and/or a caption (flyer.txt) with the same name.
#      Drop a new flyer into a club's folder and, on the next check, it's read and added
#      automatically, exactly like a brand-new Instagram post would be.
#
# We deliberately DON'T scrape Instagram: it's against Instagram's rules, and it gets
# blocked quickly, which would break the demo.

import os
import re
import sys
import json
import time
import hashlib                  # makes a short "fingerprint" of a web page's text
from html import unescape       # turns &amp; into & etc.
from pathlib import Path
from datetime import datetime, timedelta

import httpx                    # for calling the Instagram API (already installed with google-genai)
from dotenv import load_dotenv

import db
import ai

load_dotenv()

BASE_DIR = Path(__file__).parent
DEMO_DIR = BASE_DIR / "data" / "instagram_demo"
ACCOUNTS_FILE = BASE_DIR / "data" / "instagram_accounts.json"

# How often to check, in seconds. 60 is good for a live demo; in real life every 30-60 minutes is plenty.
INTERVAL = int(os.environ.get("WATCH_INTERVAL", "60"))
# Only look at each club's newest few posts, and ignore anything older than this.
# (The very first check would otherwise send a club's whole history to Gemini.)
POSTS_PER_CLUB = 5
MAX_AGE = timedelta(days=21)

IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


# ----------------------------------------------------------------------
# Source A: the official Instagram API
# ----------------------------------------------------------------------

def load_accounts():
    """Read data/instagram_accounts.json (handle -> access token). Empty if the file doesn't exist."""
    if not ACCOUNTS_FILE.exists():
        return {}
    return json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))


def api_posts(handle, access_token):
    """The club's newest posts through the official Instagram API.

    Each post comes back as a dict. "load_image" is a FUNCTION that downloads the image
    only when we actually need it (we skip downloading posts we've already seen).
    """
    response = httpx.get(
        "https://graph.instagram.com/me/media",
        params={
            "fields": "id,caption,media_type,media_url,thumbnail_url,timestamp,permalink",
            "limit": POSTS_PER_CLUB,
            "access_token": access_token,
        },
        timeout=20,
    )
    response.raise_for_status()   # turns an HTTP error (bad token, rate limit) into an exception

    posts = []
    for item in response.json().get("data", []):
        # Videos have a thumbnail_url; photos and carousels have a media_url.
        image_url = item.get("thumbnail_url") or item.get("media_url")
        posts.append({
            "source": "instagram",
            "post_id": item["id"],
            "caption": item.get("caption", ""),
            # Instagram sends times like "2026-10-08T21:15:00+0000"
            "posted_at": datetime.strptime(item["timestamp"], "%Y-%m-%dT%H:%M:%S%z"),
            "url": item.get("permalink"),
            # "lambda: ..." = a tiny function defined inline. "url=image_url" freezes the
            # current value, so each post's function downloads ITS OWN image.
            "load_image": (lambda url=image_url: download_image(url)) if image_url else None,
        })
    return posts


def download_image(url):
    response = httpx.get(url, timeout=20)
    response.raise_for_status()
    mime = response.headers.get("content-type", "image/jpeg").split(";")[0]
    return response.content, mime


# ----------------------------------------------------------------------
# Source B: the demo feed folder
# ----------------------------------------------------------------------

def demo_posts(handle):
    """Posts from data/instagram_demo/<handle>/. Files with the same name are one post:
    'gbm.png' + 'gbm.txt' = a flyer with a caption."""
    folder = DEMO_DIR / handle
    if not folder.is_dir():
        return []

    # Group the files by name without the extension ("stem"): {"gbm": [gbm.png, gbm.txt]}
    groups = {}
    for file in folder.iterdir():
        if file.suffix.lower() in IMAGE_TYPES or file.suffix.lower() in (".txt", ".url"):
            groups.setdefault(file.stem, []).append(file)

    posts = []
    for stem, files in groups.items():
        caption_file = next((f for f in files if f.suffix.lower() == ".txt"), None)
        # Optional "<name>.url" file = the real Instagram post's link ("View original post").
        link_file = next((f for f in files if f.suffix.lower() == ".url"), None)
        image_file = next((f for f in files if f.suffix.lower() in IMAGE_TYPES), None)
        # The "post time" is when the file was saved into the folder (its modified time).
        newest = max(f.stat().st_mtime for f in files)
        posts.append({
            "source": "instagram-demo",
            "post_id": f"{handle}/{stem}",
            "caption": caption_file.read_text(encoding="utf-8") if caption_file else "",
            "posted_at": datetime.fromtimestamp(newest, tz=ai.EASTERN),
            "url": (link_file.read_text(encoding="utf-8").strip() if link_file
                    else f"https://www.instagram.com/{handle}/"),
            "load_image": (lambda f=image_file: (f.read_bytes(), IMAGE_TYPES[f.suffix.lower()]))
                          if image_file else None,
        })

    # Newest first, like a real feed.
    posts.sort(key=lambda p: p["posted_at"], reverse=True)
    return posts[:POSTS_PER_CLUB]


# ----------------------------------------------------------------------
# Source C: the club's own website
# ----------------------------------------------------------------------
# Many clubs list upcoming events on their website. We download the page, keep only the
# visible text, and treat the whole page as one "post". The trick: the post id includes a
# FINGERPRINT (hash) of the text. Same text = same id = already seen = no Gemini call.
# When the club edits the page (adds an event), the text changes, the fingerprint changes,
# and it's read again. Because events are deduped in db.save_event(), events that were
# already on the page don't get duplicated.
#
# Limitation: pages built entirely with JavaScript (like Rutgers' Suitable/getINVOLVED
# pages) look empty to a program, so they're skipped.

WEB_EVERY = timedelta(minutes=int(os.environ.get("WEB_CHECK_MINUTES", "30")))  # be polite to club websites
_last_web_check = {}   # website URL -> when we last downloaded it
MAX_PAGE_CHARS = 15000  # long pages get cut, so one page can't eat a huge Gemini request


def website_posts(url):
    now = datetime.now(ai.EASTERN)
    # Websites change rarely, so download each one at most every WEB_EVERY (not every minute).
    if url in _last_web_check and now - _last_web_check[url] < WEB_EVERY:
        return []
    _last_web_check[url] = now

    response = httpx.get(url, timeout=20, follow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0 (club event calendar; HackRU 2026)"})
    response.raise_for_status()
    text = page_text(response.text)[:MAX_PAGE_CHARS]
    if len(text) < 200:
        return []   # basically empty: probably a JavaScript-only page

    fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return [{
        "source": "website",
        "post_id": f"{url}#{fingerprint}",
        "caption": text,
        "posted_at": now,
        "url": url,
        "load_image": None,
    }]


def page_text(html):
    """Turn HTML into readable text: drop scripts/styles, then all tags, then extra spaces."""
    html = re.sub(r"<(script|style|noscript)\b.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", html)          # remove every <tag>
    text = unescape(text)                         # "&amp;" -> "&", "&nbsp;" -> " "
    return re.sub(r"\s+", " ", text).strip()      # collapse runs of whitespace


# ----------------------------------------------------------------------
# One check of every club
# ----------------------------------------------------------------------

def check_all_clubs():
    """Look at every club once. Returns how many new events were saved."""
    accounts = load_accounts()
    total_events = 0
    new_posts = 0
    clubs = db.get_clubs()
    # How many clubs we can ACTUALLY read right now: a demo folder, a connected
    # Instagram account, or a website. (A handle alone isn't readable without the club's consent.)
    readable = sum(1 for c in clubs if (c["instagram"] and ((DEMO_DIR / c["instagram"]).is_dir()
                                                           or c["instagram"] in accounts))
                   or c.get("website"))

    def progress(i, current, running=True):
        db.write_status("watcher", {
            "running": running, "what": "Instagram + club websites",
            "current": current, "done": i, "total": len(clubs),
            "new_posts": new_posts, "new_events": total_events,
            "readable_clubs": readable, "updated": time.time(),
            "next_check": time.time() + INTERVAL if not running else None,
        })

    for i, club in enumerate(clubs):
        progress(i, club["name"])
        handle = club["instagram"]
        posts = []

        # Instagram: the official API if this club connected its account, plus the demo folder.
        if handle:
            try:
                posts += demo_posts(handle)
                if handle in accounts:
                    posts += api_posts(handle, accounts[handle]["access_token"])
            except Exception as err:
                print(f"[watcher] couldn't read @{handle}: {err}")

        # The club's own website, if it has one.
        if club.get("website"):
            try:
                posts += website_posts(club["website"])
            except Exception as err:
                print(f"[watcher] couldn't read {club['website']}: {err}")

        for post in posts:
            if db.is_post_seen(post["source"], post["post_id"]):
                continue   # already read this one: no Gemini call, no duplicate

            # Too old? Remember it as seen so we never look at it again, but don't process it.
            if datetime.now(ai.EASTERN) - post["posted_at"] > MAX_AGE:
                db.mark_post_seen(post["source"], post["post_id"], club["id"], 0)
                continue

            new_posts += 1
            progress(i, club["name"])   # update "events found" right away
            total_events += process_post(club, post)

    progress(len(clubs), "", running=False)
    return total_events


def process_post(club, post):
    """Send one new post to Gemini and save what it finds. Returns the number of events saved."""
    image_bytes, mime_type = (post["load_image"]() if post["load_image"] else (None, None))
    where = "website" if post["source"] == "website" else f"@{club['instagram']}"
    print(f"[watcher] new post from {where} ({club['id']}): {post['post_id'][:70]}")

    try:
        events = ai.extract_events(
            post["caption"],
            image_bytes=image_bytes,
            mime_type=mime_type or "image/png",
            club_name=club["name"],
            posted_at=post["posted_at"],
        )
    except Exception as err:
        # Don't mark it as seen: we'll try this post again on the next check.
        print(f"[watcher]   Gemini failed, will retry next round: {err}")
        return 0

    for event in events:
        event["club_id"] = club["id"]
        event["source"] = "website" if post["source"] == "website" else "instagram"
        event["source_url"] = post["url"]
        db.save_event(event)
        print(f"[watcher]   + {event['title']} ({event['start']})")
    if not events:
        print("[watcher]   no event in this post")

    db.mark_post_seen(post["source"], post["post_id"], club["id"], len(events))
    return len(events)


# ----------------------------------------------------------------------
# Run forever (or once)
# ----------------------------------------------------------------------

if __name__ == "__main__":
    db.init_db()
    run_once = "--once" in sys.argv
    if ai.FAKE:
        print("[watcher] GEMINI_FAKE=1: using fake events, Gemini is NOT being called")
    clubs = db.get_clubs()
    print(f"[watcher] watching {sum(1 for c in clubs if c['instagram'])} Instagram accounts "
          f"and {sum(1 for c in clubs if c.get('website'))} club websites"
          + ("" if run_once else f", checking every {INTERVAL}s (Ctrl+C to stop)"))

    last_scan = db.new_scan_request(0) or 0   # ignore requests made before we started
    while True:
        found = check_all_clubs()
        stamp = datetime.now().strftime("%H:%M:%S")
        print(f"[watcher] {stamp} check done: {found} new event(s)")
        if run_once:
            break

        # Wait INTERVAL seconds, but in 1-second steps, so a "Scan now" from the
        # control panel starts the next check right away instead of after the wait.
        for _ in range(INTERVAL):
            time.sleep(1)
            request = db.new_scan_request(last_scan)
            if request:
                last_scan = request
                _last_web_check.clear()   # a manual scan re-reads websites too
                print("[watcher] scan requested from the control panel")
                break
