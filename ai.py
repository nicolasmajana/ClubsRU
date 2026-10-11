# ai.py: everything that talks to Google Gemini.
#
# The main function, extract_events(), takes a messy club post (an Instagram caption,
# a Discord announcement, and/or a flyer image) and returns clean event dictionaries
# that db.save_event() can store.

import os                        # Reads environment variables (our API key).
import re                        # Finds "retry in 15s" inside Gemini's rate-limit errors.
import time                      # time.sleep() for waiting between retries.
import threading                 # A Lock, so key rotation stays correct when calls happen at once.
from datetime import datetime, timedelta  # Dates and times. timedelta = "a length of time" (e.g. 1 day).
from zoneinfo import ZoneInfo    # Time zones. Rutgers is in "America/New_York".
from typing import Literal       # Lets us say "this field must be one of these exact values".

from dotenv import load_dotenv   # Loads the variables in .env into os.environ.
from pydantic import BaseModel, Field   # Describes the exact JSON shape we want back from Gemini.
from google import genai               # The official Gemini SDK.
from google.genai import types, errors

# Read .env so os.environ["GEMINI_API_KEY"] exists. Must run before we create the client.
load_dotenv()

# .get(name, default) uses the default when the variable isn't set.
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.7-flash")

# The FREE tier gives each model its own small daily quota (about 20 requests/day).
# If one model runs out, we fall back to the next one in this list, so the demo keeps working.
# dict.fromkeys(...) removes duplicates while keeping the order (in case MODEL is also listed).
MODELS = list(dict.fromkeys([MODEL, "gemini-3.7-flash", "gemini-3.5-flash", "gemini-3.8-flash",
                             "gemini-3.5-flash-lite", "gemini-flash-lite-latest"]))

EASTERN = ZoneInfo("America/New_York")

# FAKE MODE: put GEMINI_FAKE=1 in .env while building the bot and website.
# extract_events() then returns a made-up event WITHOUT calling Gemini, so you don't burn
# the daily quota. Set it to 0 to test the real thing and for the demo.
FAKE = os.environ.get("GEMINI_FAKE") == "1"

# API KEYS: we can have more than one (GEMINI_API_KEY, GEMINI_API_KEY_2, GEMINI_API_KEY_3...).
# Every request uses the NEXT key in turn: key 1, key 2, key 1, key 2...
# A "client" is the object that sends requests to Gemini; we make one per key.
API_KEYS = [os.environ[name] for name in ["GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3"]
            if os.environ.get(name)]
clients = [genai.Client(api_key=key) for key in API_KEYS] or [genai.Client(api_key=None)]

# The bot runs Gemini calls in background threads, so two calls could pick a key at the
# exact same moment. A Lock makes them take turns, so the rotation never skips or repeats.
_key_lock = threading.Lock()
_next_key = 0


def next_client():
    """Return the client for the next key in the rotation."""
    global _next_key   # "global" = we're changing the variable defined outside this function
    with _key_lock:
        chosen = clients[_next_key % len(clients)]
        _next_key += 1
    return chosen


# Kept so older test snippets that use ai.client still work (it's the first key's client).
client = clients[0]


# ----------------------------------------------------------------------
# 2.1 The shape of the answer we want (a "schema")
# ----------------------------------------------------------------------
# Instead of asking Gemini for free text and trying to parse it, we describe the exact
# structure with Pydantic classes. Gemini is then FORCED to answer with JSON in this
# shape ("structured output"), and the SDK turns that JSON back into Python objects.
# The Field(description=...) text is sent to Gemini too, so it knows what each field means.

class ExtractedEvent(BaseModel):
    title: str = Field(description='Short event name, e.g. "General Body Meeting" or "Intro to Git Workshop"')
    description: str = Field(description="1-2 sentence summary of what happens at the event")
    date: str = Field(description="YYYY-MM-DD")
    start_time: str = Field(description="HH:MM in 24-hour Eastern time, e.g. 19:30. Empty string if not stated.")
    end_time: str = Field(description="HH:MM in 24-hour time. Empty string if not stated.")
    location: str = Field(description='Building and room, e.g. "Hill 114". Empty string if not stated.')
    # Literal[...] = the value MUST be one of these. Keeps the campus filter clean later.
    campus: Literal["Busch", "Livingston", "College Ave", "Cook/Douglass", "Online", "Other", "Unknown"]
    free_food: bool = Field(description="True if any food or drinks are mentioned")


class ExtractionResult(BaseModel):
    # A post can announce several events (e.g. "this week: GBM Tuesday, social Friday"),
    # so we always get a list. An empty list means "this post isn't an event".
    events: list[ExtractedEvent]


# ----------------------------------------------------------------------
# 2.2 The instructions (the "prompt")
# ----------------------------------------------------------------------
# Gemini doesn't know Rutgers buildings, so we tell it which campus each one is on.
CAMPUS_HINTS = """Rutgers-New Brunswick buildings by campus:
- Busch: Hill Center (Hill), CoRE, Busch Student Center, SERC, ARC, Engineering Building, Werblin, Allison Road Classrooms
- Livingston: Livingston Student Center (Coffee House), Tillett, Lucy Stone, Beck Hall, Livingston Dining Commons
- College Ave: College Avenue Student Center (CASC), Scott Hall, Murray Hall, Brower, Voorhees, Alexander Library
- Cook/Douglass: Cook Student Center, Douglass Student Center, Loree, Hickman Hall, Foran Hall"""


def build_prompt(text, club_name, posted_at):
    # strftime formats a date as text: "%A, %B %d, %Y" -> "Saturday, October 10, 2026".
    # Gemini needs the post's date to turn "this Friday" or "tmrw" into a real date.
    # Include the TIME too: a post made Saturday at 9 PM saying "this Saturday 8am"
    # can't mean today (8 AM already passed), so it must mean next Saturday.
    posted = posted_at.strftime("%A, %B %d, %Y at %I:%M %p")
    club_line = f' by the club "{club_name}"' if club_name else ""

    # An f-string (f"...") inserts variables wherever you write {variable}.
    # Triple quotes let the string span several lines.
    return f"""You extract events from Rutgers University student organization posts
(Instagram captions, flyers, Discord announcements).

The post was published on {posted}{club_line}.
- Resolve relative dates ("tomorrow", "tmrw", "this Friday", "next Tuesday") relative to that publish date.
- If no year is given, use the next date on or after the publish date.
- An event can't start before the post was published. If a weekday like "this Saturday" would land
  on a time that has already passed, use the following week instead.
- Only include real, upcoming events someone can attend (meetings, workshops, socials, trips, study sessions, competitions).
- Do NOT include: congratulations, recaps of past events, memes, or general recruitment without a specific date.
- If the post contains no event, return an empty events list.
- free_food is true if any food or drink is mentioned (pizza, snacks, candy, cider, boba, "refreshments"...).

{CAMPUS_HINTS}

Post:
\"\"\"
{text or "(no text, read the attached image)"}
\"\"\""""


# ----------------------------------------------------------------------
# 2.3 extract_events(): the main function
# ----------------------------------------------------------------------

def extract_events(text="", image_bytes=None, mime_type="image/png", club_name=None, posted_at=None):
    """Turn a post into a list of event dicts ready for db.save_event().

    text        : the caption or Discord message
    image_bytes : the raw bytes of a flyer image (optional)
    mime_type   : the image type, e.g. "image/png" or "image/jpeg"
    club_name   : which club posted it (helps Gemini)
    posted_at   : when the post was made (a datetime). Defaults to right now.

    Returns e.g. [{"title": "...", "start": "2026-10-14T19:30", "end": None, ...}]
    The caller adds "club_id" and "source" before saving.
    """
    if not text.strip() and not image_bytes:
        return []  # nothing to read

    # datetime.now(EASTERN) = current time in New Jersey. If the post time came from
    # Discord (UTC), .astimezone() converts it so "tomorrow" means tomorrow in NJ.
    posted_at = (posted_at or datetime.now(EASTERN)).astimezone(EASTERN)

    # Fake mode: skip Gemini entirely while building/testing other parts (saves quota).
    if FAKE:
        return fake_extract(text, posted_at)

    # "contents" is everything we send: the prompt, plus the image if there is one.
    contents = [build_prompt(text, club_name, posted_at)]
    if image_bytes:
        # Part.from_bytes wraps the raw image so Gemini can "see" it (Gemini Vision).
        contents.append(types.Part.from_bytes(data=image_bytes, mime_type=mime_type))

    config = types.GenerateContentConfig(
        response_mime_type="application/json",  # answer in JSON...
        response_schema=ExtractionResult,       # ...in exactly this shape
        temperature=0.1,                        # low = consistent, not creative. We want accuracy.
        # Gemini 3 models "think" before answering. The default (high) can take 30+ seconds;
        # LOW answers in about 3 seconds and is plenty for reading a flyer. Matters for the live demo.
        thinking_config=types.ThinkingConfig(thinking_level="LOW"),
    )

    response = call_gemini(contents, config)

    # response.parsed is an ExtractionResult object (the SDK converted the JSON for us).
    result = response.parsed
    if result is None:
        return []

    events = []
    for e in result.events:
        # Skip anything without a valid date. Better to miss one than to save garbage.
        if not is_valid_date(e.date):
            continue
        start_time = e.start_time if is_valid_time(e.start_time) else "12:00"
        events.append({
            "title": e.title.strip(),
            "description": e.description.strip(),
            # Combine into the format db.py stores: "2026-10-14T19:30"
            "start": f"{e.date}T{start_time}",
            "end": f"{e.date}T{e.end_time}" if is_valid_time(e.end_time) else None,
            "location": e.location.strip(),
            "campus": "" if e.campus == "Unknown" else e.campus,
            "free_food": e.free_food,
        })
    return events


# ----------------------------------------------------------------------
# STEP 8: recommend clubs for a student's interests
# ----------------------------------------------------------------------

class Recommendation(BaseModel):
    club_id: str = Field(description="Exactly one of the ids from the catalog")
    reason: str = Field(description="One specific sentence, speaking to the student ('You'd...'), "
                                    "connecting their goals to what this club actually does")


class RecommendationResult(BaseModel):
    recommendations: list[Recommendation]


# Remember answers we already got, so the same question doesn't spend quota twice
# (e.g. during the demo, or when someone clicks the button again).
# Key = the interests text in lowercase. This resets when the program restarts.
_recommend_cache = {}


def recommend_clubs(interests, clubs, limit=5):
    """Pick the clubs that best fit what the student says they want.

    interests : free text, e.g. "I want to become a software engineer and make friends"
    clubs     : the list from db.get_clubs()
    Returns   : [{"club_id": "usacs", "reason": "You'd ..."}, ...] best match first
    """
    interests = interests.strip()
    if not interests:
        return []

    key = interests.lower()
    if key in _recommend_cache:
        return _recommend_cache[key]

    if FAKE:
        result = fake_recommend(interests, clubs, limit)
    else:
        # The "catalog": one line per club, so Gemini knows what every club is about.
        # This is why your teammate's descriptions and tags matter so much.
        catalog = "\n".join(
            f"- id: {c['id']} | {c['name']} | {c['category']} | tags: {c['tags']} | {c['description']}"
            for c in clubs
        )
        prompt = f"""A Rutgers student describes what they're looking for:
\"\"\"{interests}\"\"\"

Pick up to {limit} clubs from this catalog that fit them best, best match first.
Think about their goals (career, skills, friends, culture, fun), not just matching keywords.
For each one, write one specific, friendly sentence to the student explaining why it fits.
Only use ids that appear in the catalog.

Catalog:
{catalog}"""

        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=RecommendationResult,
            temperature=0.3,   # a little variety in the wording is fine here
            thinking_config=types.ThinkingConfig(thinking_level="LOW"),
        )
        parsed = call_gemini([prompt], config).parsed
        valid_ids = {c["id"] for c in clubs}
        # Keep only real club ids (never trust the AI blindly), no duplicates, max `limit`.
        result, seen = [], set()
        for r in (parsed.recommendations if parsed else []):
            if r.club_id in valid_ids and r.club_id not in seen:
                seen.add(r.club_id)
                result.append({"club_id": r.club_id, "reason": r.reason})
        result = result[:limit]

    _recommend_cache[key] = result
    return result


def fake_recommend(interests, clubs, limit):
    """Fake mode: rank clubs by how many of the student's words appear in their
    name/description/tags. Simple keyword matching, no Gemini."""
    words = {w for w in re.findall(r"[a-z]+", interests.lower()) if len(w) > 3}
    scored = []
    for c in clubs:
        text = f"{c['name']} {c['description']} {c['tags']}".lower()
        score = sum(1 for w in words if w in text)
        if score:
            scored.append((score, c))
    # sort by score, highest first. key=lambda pair: pair[0] means "sort using the score".
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [{"club_id": c["id"], "reason": f"[FAKE] Matches {score} of your keywords."}
            for score, c in scored[:limit]]


# ----------------------------------------------------------------------
# 2.4 Helpers
# ----------------------------------------------------------------------

def call_gemini(contents, config):
    """Send the request. If Gemini says "busy" or "quota used up", try again or move on
    to the next model in MODELS.

    Errors we handle (err.code is the HTTP status code):
      429 RESOURCE_EXHAUSTED: over the free quota. Google says how long to wait:
          "retry in 15.3s" (per-minute limit) or "retry in 3h2m..." (daily limit).
      500 / 503: Gemini is overloaded right now.
    Anything else (400 bad request, 403 bad API key) would fail every time, so we raise it.
    """
    last_error = None
    for model in MODELS:
        for attempt in range(2):   # at most 2 tries per model
            try:
                # next_client() = the next API key in the rotation (every request switches).
                return next_client().models.generate_content(model=model, contents=contents, config=config)
            except (errors.ServerError, errors.ClientError) as err:
                code = getattr(err, "code", None)
                if code not in (429, 500, 503):
                    raise
                last_error = err

                # Short wait ("retry in 15.3s")? Wait it out and retry the same model.
                # Long wait (hours, i.e. the daily quota is gone) or second failure? Next model.
                match = re.search(r"retry in ([\d.]+)s", str(err))
                wait = float(match.group(1)) + 1 if match else 2
                if attempt == 0 and wait <= 20:
                    print(f"[ai] {model} busy ({code}), retrying in {wait:.0f}s...")
                    time.sleep(wait)
                else:
                    print(f"[ai] {model} unavailable ({code}), trying the next model...")
                    break
    # Every model failed: raise the last error so the caller can show a message.
    raise last_error or RuntimeError("No Gemini models are configured")


def fake_extract(text, posted_at):
    """Stand-in for Gemini (fake mode). Returns one predictable event: "tomorrow at 7 PM",
    titled with the first few words of the post. Posts with "congrats" return nothing,
    so you can still test the "not an event" path."""
    if "congrats" in text.lower():
        return []
    day = (posted_at + timedelta(days=1)).strftime("%Y-%m-%d")
    first_words = " ".join(text.split()[:5]) or "Flyer event"
    return [{
        "title": f"[FAKE] {first_words}",
        "description": "Fake event (GEMINI_FAKE=1). Turn fake mode off for real extraction.",
        "start": f"{day}T19:00",
        "end": f"{day}T20:00",
        "location": "Hill 114",
        "campus": "Busch",
        "free_food": "pizza" in text.lower(),
    }]


def is_valid_date(s):
    """True for "2026-10-14", False for "", "Oct 14", "2026-13-40"..."""
    try:
        datetime.strptime(s, "%Y-%m-%d")   # strptime = parse text into a date, or raise ValueError
        return True
    except (ValueError, TypeError):
        return False


def is_valid_time(s):
    """True for "19:30", False for "", "7:30pm"..."""
    try:
        datetime.strptime(s, "%H:%M")
        return len(s) == 5
    except (ValueError, TypeError):
        return False


# ----------------------------------------------------------------------
# 2.5 Test block: "python ai.py" runs the demo posts through Gemini
# ----------------------------------------------------------------------

if __name__ == "__main__":
    # Pretend everything was posted today, as if the clubs had just announced it.
    samples = [
        ("USACS", "@everyone 🚨 GENERAL BODY MEETING this Wednesday!! 7:30pm in Hill 114 on Busch. "
                  "We're doing a resume roast + pizza 🍕 bring your resume (or don't, there's still pizza). See you there!!"),
        ("USACS", "hey yall, intro to git & github workshop next tues (10/20) 8-9pm, CoRE 101. bring a laptop. no experience needed"),
        ("Rutgers Anime Club", "🎃 Halloween social!!! Friday Oct 30th, Livingston Student Center Coffee House, 9pm–11pm. "
                               "costume contest w/ prizes, free candy + cider. members & friends welcome"),
        ("Rutgers Outdoors Club", "Reminder: hiking trip to Delaware Water Gap this Saturday. Bus leaves College Ave Student Center "
                                  "at 8am sharp, back around 6pm. $10 for members, sign up form in #trips"),
        ("USACS", "Study night tmrw (Thursday) 6-9 in Alexander Library, 3rd floor. We'll have snacks and upperclassmen "
                  "helping with CS 111/112 + Calc 1"),
        ("USACS", "congrats to our members who got internships this summer!! so proud of you all 💪"),
    ]

    print(f"Model: {MODEL} | today: {datetime.now(EASTERN):%A %Y-%m-%d}\n")
    for club, post in samples:
        print(f"[{club}] {post[:70]}...")
        found = extract_events(post, club_name=club)
        if not found:
            print("   -> no events (correct for the 'congrats' post)")
        for ev in found:
            food = " 🍕" if ev["free_food"] else ""
            print(f"   -> {ev['title']} | {ev['start']} to {ev['end']} | {ev['location']} ({ev['campus']}){food}")
        print()
