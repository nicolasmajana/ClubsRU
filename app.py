# app.py: the website. Run it with:  python app.py   then open http://localhost:5000
#
# Flask does two jobs here:
#   1. Serves the web page (templates/index.html + static/style.css + static/app.js).
#   2. Provides a JSON "API": URLs the page's JavaScript calls to get clubs and events,
#      follow clubs, and send flyers to Gemini.
#
# The bot (bot.py) runs separately. Both read and write the same database through db.py.

import os
import time
from datetime import datetime, timedelta

from flask import Flask, jsonify, request, render_template, abort, Response
from werkzeug.middleware.proxy_fix import ProxyFix
from icalendar import Calendar, Event
from dotenv import load_dotenv

import db
import ai

load_dotenv()

APP_NAME = os.environ.get("APP_NAME", "ClubsRU")   # change the name in ONE place (.env)

# The public address of the site (e.g. a cloudflared/ngrok tunnel URL), used to build the
# calendar subscription link. Empty = use whatever address the browser used.
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")

# Flask(__name__) creates the web app. By default it looks for HTML in "templates/"
# and for CSS/JS/images in "static/".
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024    # reject uploads over 10 MB

# When the site is reached through a tunnel (cloudflared/ngrok), the tunnel forwards the
# request to localhost. ProxyFix reads the "X-Forwarded-*" headers the tunnel adds, so
# request.host_url is the PUBLIC address (https://xyz.trycloudflare.com/), not localhost.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Make sure the tables exist and the latest clubs.csv is loaded every time the site starts.
db.init_db()
db.load_clubs_from_csv()


# ----------------------------------------------------------------------
# 4.1 The page
# ----------------------------------------------------------------------
# "@app.get('/')" is a ROUTE: when a browser asks for "/" (the home page),
# Flask runs this function and sends back whatever it returns.

@app.get("/")
def home():
    # render_template fills in {{ app_name }} inside templates/index.html.
    return render_template("index.html", app_name=APP_NAME, fake_mode=ai.FAKE,
                           public_url=PUBLIC_URL)


# ----------------------------------------------------------------------
# 4.2 Clubs and follows
# ----------------------------------------------------------------------
# Every visitor gets a random user_id that the browser remembers (see static/app.js).
# No passwords or accounts: perfect for a hackathon, and no personal data stored.

@app.get("/api/clubs")
def api_clubs():
    # request.args = the "?user_id=..." part of the URL
    user_id = request.args.get("user_id")
    # jsonify turns Python lists/dicts into JSON, the format JavaScript understands.
    return jsonify(db.get_clubs(user_id))


@app.post("/api/follow")
def api_follow():
    # request.get_json() = the JSON body the page sent, e.g.
    # {"user_id": "abc", "club_id": "usacs", "follow": true}
    data = request.get_json(silent=True) or {}
    user_id, club_id = data.get("user_id"), data.get("club_id")
    if not user_id or not db.get_club(club_id):
        # abort(400) stops here and answers "400 Bad Request" (see the error handler below).
        abort(400, "user_id and a valid club_id are required")

    if data.get("follow", True):
        db.follow(user_id, club_id)
    else:
        db.unfollow(user_id, club_id)
    return jsonify({"ok": True})


# ----------------------------------------------------------------------
# 4.3 Events
# ----------------------------------------------------------------------

@app.get("/api/events")
def api_events():
    user_id = request.args.get("user_id")
    followed_only = request.args.get("followed") == "1"
    return jsonify(db.get_events(user_id, followed_only=followed_only))


@app.post("/api/events")
def api_save_event():
    """Save an event the user reviewed on the "Add event" tab."""
    event = request.get_json(silent=True) or {}

    # Never trust data from the browser: check the required fields ourselves.
    if not db.get_club(event.get("club_id")):
        abort(400, "Pick a valid club")
    if not (event.get("title") or "").strip():
        abort(400, "Title is required")
    start = event.get("start") or ""
    # Either "2026-10-14T19:30" (a timed event) or "2026-10-14" (an all-day event).
    if not ai.is_valid_date(start[:10]) or (len(start) > 10 and not ai.is_valid_time(start[11:16])):
        abort(400, "Start must look like 2026-10-14T19:30 (or 2026-10-14 for all day)")

    # Only keep the fields we expect (ignore anything else the browser sent).
    allowed = ["club_id", "title", "description", "start", "end", "location",
               "campus", "free_food", "source", "source_url"]
    clean = {key: event[key] for key in allowed if key in event}
    clean["start"] = clean["start"][:16]
    clean.setdefault("source", "upload")

    event_id = db.save_event(clean)
    return jsonify({"ok": True, "id": event_id}), 201   # 201 = "Created"


@app.delete("/api/events/<int:event_id>")
def api_delete_event(event_id):
    # <int:event_id> in the URL becomes a Python int argument: /api/events/7 -> event_id=7
    if not db.delete_event(event_id):
        abort(404, "No such event")
    return jsonify({"ok": True})


# ----------------------------------------------------------------------
# 4.4 Flyer / caption -> Gemini (preview only, nothing is saved yet)
# ----------------------------------------------------------------------

@app.post("/api/extract")
def api_extract():
    """The page sends a form with: club_id, text (caption), and/or image (a flyer file).
    We return the events Gemini found so the user can CHECK and EDIT them before saving.
    (A human double-check is a good idea with AI: it can misread a flyer.)"""
    # Forms with files arrive as request.form (text fields) + request.files (uploads).
    club = db.get_club(request.form.get("club_id"))
    text = request.form.get("text", "")
    image = request.files.get("image")

    image_bytes = image.read() if image else None
    mime_type = image.mimetype if image else None
    if image_bytes and not mime_type.startswith("image/"):
        abort(400, "The file must be an image (PNG or JPG)")
    if not text.strip() and not image_bytes:
        abort(400, "Paste a caption or upload a flyer")

    try:
        events = ai.extract_events(
            text,
            image_bytes=image_bytes,
            mime_type=mime_type or "image/png",
            club_name=club["name"] if club else None,
        )
    except Exception as err:
        print(f"[app] Gemini error: {err}")
        abort(502, "Gemini couldn't read this right now. Try again in a minute.")

    return jsonify({"events": events})


# ----------------------------------------------------------------------
# 4.5 Errors as JSON
# ----------------------------------------------------------------------
# Without this, abort(400, "...") returns an HTML error page, which the JavaScript
# can't read. This sends {"error": "the message"} instead.

@app.get("/api/scan-status")
def api_scan_status():
    """What the background programs are doing right now, for the progress bar at the
    bottom of the page. The watcher and the bot each write a small status file (db.write_status)."""
    now = time.time()
    result = {}
    for name in ("watcher", "bot"):
        status = db.read_status(name)
        # A status that hasn't been updated in 3 minutes means that program isn't running.
        if status and now - status.get("updated", 0) < 180:
            result[name] = status
    result["total_events"] = len(db.get_events())
    return jsonify(result)


@app.post("/api/recommend")
def api_recommend():
    """STEP 8: {"interests": "I want to do software engineering"} -> recommended clubs."""
    data = request.get_json(silent=True) or {}
    interests = (data.get("interests") or "").strip()
    if not interests:
        abort(400, "Tell us a bit about what you're interested in")
    if len(interests) > 500:
        abort(400, "Please keep it under 500 characters")

    try:
        recs = ai.recommend_clubs(interests, db.get_clubs())
    except Exception as err:
        print(f"[app] Gemini error: {err}")
        abort(502, "Gemini couldn't answer right now. Try again in a minute.")
    return jsonify({"recommendations": recs})


@app.get("/calendar/<user_id>.ics")
def calendar_feed(user_id):
    """STEP 6: a calendar FEED (.ics file) with the events of every club this user follows.

    Google Calendar / Apple Calendar / Outlook can SUBSCRIBE to this URL. They download it
    again every few hours, so new events (and newly followed clubs) show up in the student's
    own calendar automatically, without them doing anything.

    .ics ("iCalendar") is the standard text format every calendar app understands:
        BEGIN:VEVENT
        SUMMARY:General Body Meeting · USACS
        DTSTART;TZID=America/New_York:20261014T193000
        ...
        END:VEVENT
    The icalendar library writes that format for us.
    """
    events = db.get_events(user_id, followed_only=True)

    cal = Calendar()
    # Required header fields. PRODID = "which program made this file".
    cal.add("prodid", f"-//{APP_NAME}//HackRU 2026//EN")
    cal.add("version", "2.0")
    # Name and color hints shown in Google/Apple Calendar's sidebar.
    cal.add("x-wr-calname", f"{APP_NAME}: my Rutgers clubs")
    cal.add("x-wr-timezone", "America/New_York")
    # Ask calendar apps to re-check hourly. (Google ignores this and checks every few hours.)
    cal.add("refresh-interval", timedelta(hours=1), parameters={"VALUE": "DURATION"})
    cal.add("x-published-ttl", "PT1H")

    for e in events:
        if len(e["start"]) == 10:
            # ALL-DAY event: calendar apps want plain dates, and the end date is the NEXT day.
            start = datetime.strptime(e["start"], "%Y-%m-%d").date()
            end = start + timedelta(days=1)
        else:
            start = to_eastern(e["start"])
            # Events with no end time last 1 hour.
            end = to_eastern(e["end"]) if e["end"] else start + timedelta(hours=1)

        ev = Event()
        # UID = a permanent unique id. Calendar apps use it to UPDATE an event instead of
        # adding a duplicate when the feed changes (e.g. the room moved).
        ev.add("uid", f"event-{e['id']}@{APP_NAME.lower()}")
        ev.add("summary", f"{'🍕 ' if e['free_food'] else ''}{e['title']} · {e['club_name']}")
        ev.add("dtstart", start)
        ev.add("dtend", end)
        ev.add("dtstamp", datetime.now(ai.EASTERN))
        where = ", ".join(part for part in [e["location"], e["campus"]] if part)
        if where:
            ev.add("location", where)
        details = "\n\n".join(part for part in [e["description"], e["source_url"]] if part)
        if details:
            ev.add("description", details)
        if e["source_url"]:
            ev.add("url", e["source_url"])
        cal.add_component(ev)

    # Adds the time zone definition (VTIMEZONE) for America/New_York, so every app
    # shows the right time, even across the daylight-saving switch on Nov 1.
    cal.add_missing_timezones()

    # to_ical() produces the .ics text. The mimetype tells the calendar app what it's getting.
    return Response(cal.to_ical(), mimetype="text/calendar",
                    headers={"Content-Disposition": "inline; filename=clubs.ics"})


def to_eastern(text):
    """'2026-10-14T19:30' (stored as Eastern time) -> a datetime that knows it's Eastern."""
    return datetime.strptime(text[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=ai.EASTERN)


@app.errorhandler(400)
@app.errorhandler(404)
@app.errorhandler(413)
@app.errorhandler(502)
def json_error(err):
    return jsonify({"error": err.description}), err.code


# ----------------------------------------------------------------------
# Start the server
# ----------------------------------------------------------------------

if __name__ == "__main__":
    if ai.FAKE:
        print("[app] GEMINI_FAKE=1: flyer extraction returns fake events")
    # Debug mode auto-restarts when you save a file and shows detailed error pages, BUT
    # those pages show our code (and a console) to anyone, including people using the
    # public tunnel link. So it's OFF unless you set FLASK_DEBUG=1 in .env for local coding.
    debug = os.environ.get("FLASK_DEBUG") == "1"
    app.run(debug=debug, port=int(os.environ.get("PORT", 5000)))
