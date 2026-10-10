# app.py: the website. Run it with:  python app.py   then open http://localhost:5000
#
# Flask does two jobs here:
#   1. Serves the web page (templates/index.html + static/style.css + static/app.js).
#   2. Provides a JSON "API": URLs the page's JavaScript calls to get clubs and events,
#      follow clubs, and send flyers to Gemini.
#
# The bot (bot.py) runs separately. Both read and write the same database through db.py.

import os

from flask import Flask, jsonify, request, render_template, abort
from dotenv import load_dotenv

import db
import ai

load_dotenv()

APP_NAME = os.environ.get("APP_NAME", "ScarletSync")   # change the name in ONE place (.env)

# Flask(__name__) creates the web app. By default it looks for HTML in "templates/"
# and for CSS/JS/images in "static/".
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024    # reject uploads over 10 MB

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
    return render_template("index.html", app_name=APP_NAME, fake_mode=ai.FAKE)


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
    if not ai.is_valid_date((event.get("start") or "")[:10]) or not ai.is_valid_time((event.get("start") or "")[11:16]):
        abort(400, "Start must look like 2026-10-14T19:30")

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
    # debug=True: auto-restarts when you save a file and shows detailed errors.
    # Turn it off for anything public.
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
