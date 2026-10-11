# bot.py: the Discord bot. Run it with:  python bot.py
#
# How clubs use it:
#   1. A club officer adds the bot to their Discord server (invite link from the Developer Portal).
#   2. They type  !link <club-id>  once (e.g. "!link usacs") to connect the server to their club.
#   3. From then on, every message posted in a channel with "announcement" or "event" in its name
#      is read by Gemini, and any events found are saved to the calendar automatically.
#
# This runs as its OWN program, separate from the website (app.py). Both share the database through db.py.

import os
import asyncio   # Python's tool for doing several things at once without freezing. discord.py is built on it.
import time      # timestamps for the progress file the website reads
from datetime import datetime, timedelta, timezone

import discord   # The discord.py library.
from dotenv import load_dotenv

import db        # Our database functions (step 1).
import ai        # Our Gemini extraction (step 2).

load_dotenv()
TOKEN = os.environ.get("DISCORD_TOKEN")

# The bot only reads channels whose name contains one of these words,
# e.g. #announcements, #club-announcements, #events, #upcoming-events.
WATCHED_WORDS = ("announcement", "event")


# ----------------------------------------------------------------------
# 3.1 Create the bot and say what it's allowed to see ("intents")
# ----------------------------------------------------------------------
# Intents = which kinds of information Discord sends to the bot.
# default() covers servers, channels and messages, but NOT what the messages say.
# message_content is a "privileged" intent: you must ALSO switch it on in the
# Developer Portal (Bot tab -> Message Content Intent). If either one is off,
# message.content is always an empty string, and the bot silently does nothing.
intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)


# ----------------------------------------------------------------------
# 3.2 Events: functions Discord calls when something happens
# ----------------------------------------------------------------------
# "@client.event" registers the function below it. discord.py calls it by name:
#   on_ready    -> when the bot finishes logging in
#   on_message  -> every time a message is posted in any channel the bot can see
# "async def" makes it a coroutine: a function that can pause (with "await") while it waits
# for Discord or Gemini, letting the bot keep handling other messages in the meantime.

_background_started = False


@client.event
async def on_ready():
    global _background_started
    print(f"[bot] Logged in as {client.user} | in {len(client.guilds)} server(s)")
    for guild in client.guilds:
        club = db.get_club_by_guild(str(guild.id))
        status = f"linked to {club['name']}" if club else "NOT linked yet (type !link <club-id> there)"
        print(f"[bot]   {guild.name}: {status}")

    # on_ready can run again after a reconnect, so only start the background work once.
    if not _background_started:
        _background_started = True
        # Catch up on announcements posted while the bot was OFF.
        asyncio.create_task(catch_up("startup"))
        # Keep listening for the control panel's "Scan now" button.
        asyncio.create_task(watch_for_scan_requests())


@client.event
async def on_message(message):
    # Ignore other bots (including ourselves, or the bot would answer its own replies forever)
    # and direct messages (message.guild is None in DMs).
    if message.author.bot or message.guild is None:
        return

    content = message.content.strip()

    # Commands start with "!". Everything else might be an announcement.
    if content.startswith("!link"):
        await handle_link(message, content)
    elif content.startswith("!clubs"):
        await handle_clubs(message)
    elif is_watched_channel(message.channel):
        await handle_announcement(message)


# ----------------------------------------------------------------------
# 3.3 Commands
# ----------------------------------------------------------------------

async def handle_link(message, content):
    """!link <club-id>: connect this Discord server to a club in our database."""
    # Only server admins may link, so a random member can't hijack the server's club.
    if not message.author.guild_permissions.manage_guild:
        await message.reply("Only server admins (Manage Server permission) can link this server.")
        return

    # "!link usacs".split() -> ["!link", "usacs"]
    parts = content.split()
    if len(parts) < 2:
        await message.reply("Usage: `!link <club-id>`, for example `!link usacs`. Type `!clubs` to see all ids.")
        return

    club_id = parts[1].lower()
    club = db.get_club(club_id)
    if club is None:
        await message.reply(f"No club with id `{club_id}`. Type `!clubs` to see all ids.")
        return

    # Discord ids are big numbers. We store them as text so nothing gets rounded.
    db.link_guild(club_id, str(message.guild.id))
    await message.reply(
        f"✅ Linked this server to **{club['name']}**.\n"
        f"Posts in channels named like #announcements or #events will now sync to the calendar 📅"
    )


async def handle_clubs(message):
    """!clubs: list every club id, so admins know what to type after !link."""
    ids = [c["id"] for c in db.get_clubs()]
    # Discord messages max out at 2000 characters, so cut the list if it ever gets long.
    text = "**Club ids:** " + ", ".join(f"`{i}`" for i in ids)
    await message.reply(text[:1990])


# ----------------------------------------------------------------------
# 3.4 Announcements -> Gemini -> database
# ----------------------------------------------------------------------

def is_watched_channel(channel):
    # getattr(..., "name", "") because some channel types (like threads in old versions) may lack a name.
    name = getattr(channel, "name", "").lower()
    return any(word in name for word in WATCHED_WORDS)


async def handle_announcement(message, live=True):
    """Read one announcement and save its events. Returns how many events were saved.
    live=True  -> a brand-new message: show "typing..." and reply in the channel.
    live=False -> catching up on an OLD message: just react 📅 quietly (no reply spam)."""
    # Which club does this server belong to? If nobody ran !link yet, do nothing.
    club = db.get_club_by_guild(str(message.guild.id))
    if club is None:
        return 0

    # Each Discord message is read only once, ever (same seen_posts table as the watcher).
    if db.is_post_seen("discord", str(message.id)):
        return 0

    # Is there a flyer? Take the first attachment that's an image.
    image_bytes, mime_type = None, None
    for attachment in message.attachments:
        if attachment.content_type and attachment.content_type.startswith("image/"):
            image_bytes = await attachment.read()   # download the image from Discord
            mime_type = attachment.content_type
            break

    if not message.content.strip() and image_bytes is None:
        db.mark_post_seen("discord", str(message.id), club["id"], 0)
        return 0  # e.g. a sticker or a file that isn't an image

    # ai.extract_events is a normal (blocking) function that waits ~3 seconds for Gemini.
    # Calling it directly would FREEZE the whole bot for those seconds.
    # asyncio.to_thread runs it in a background thread, and "await" waits for the result
    # without blocking everything else.
    extract = asyncio.to_thread(
        ai.extract_events,
        message.content,
        image_bytes=image_bytes,
        mime_type=mime_type or "image/png",
        club_name=club["name"],
        posted_at=message.created_at,   # when the message was posted (resolves "this Friday")
    )
    try:
        if live:
            # Shows "Bot is typing..." in the channel while Gemini works. Nice touch for the demo.
            async with message.channel.typing():
                events = await extract
        else:
            events = await extract
    except Exception as err:
        # Never let one bad message crash the bot. Log it and mark the message.
        # (Not marked as seen, so the next scan tries it again.)
        print(f"[bot] Gemini failed for message {message.id}: {err}")
        await message.add_reaction("⚠️")
        return 0

    db.mark_post_seen("discord", str(message.id), club["id"], len(events))
    if not events:
        print(f"[bot] {club['name']}: no event in message {message.id}")
        return 0  # chatter, congrats, memes... stay quiet

    lines = []
    for event in events:
        # Add the fields extract_events doesn't know about, then save.
        event["club_id"] = club["id"]
        event["source"] = "discord"
        event["source_url"] = message.jump_url   # a link that opens this exact Discord message
        db.save_event(event)
        where = f" @ {event['location']}" if event["location"] else ""
        lines.append(f"• **{event['title']}**, {pretty_time(event['start'])}{where}")

    await message.add_reaction("📅")
    if live:
        # mention_author=False: reply without pinging the person who posted.
        await message.reply("Added to the club calendar:\n" + "\n".join(lines), mention_author=False)
    print(f"[bot] {club['name']}: saved {len(events)} event(s)")
    return len(events)


# ----------------------------------------------------------------------
# Catching up: read announcements the bot missed
# ----------------------------------------------------------------------
# Discord only sends the bot NEW messages while it's running. Anything posted while it was
# off would be lost, so on startup (and when the panel's "Scan now" is pressed) we read the
# last few messages of every announcement channel. Already-read ones are skipped
# (seen_posts), so each message still costs at most ONE Gemini request.

CATCH_UP_MESSAGES = 25           # how far back to look in each channel
CATCH_UP_MAX_AGE_DAYS = 21       # ignore anything older than 3 weeks


async def catch_up(reason):
    found = 0
    oldest = datetime.now(timezone.utc) - timedelta(days=CATCH_UP_MAX_AGE_DAYS)
    # Every (club, channel) pair we'll read, so we can report "channel 2 of 3".
    channels = [(db.get_club_by_guild(str(g.id)), ch) for g in client.guilds for ch in g.text_channels
                if is_watched_channel(ch) and db.get_club_by_guild(str(g.id))]

    def progress(i, current, running=True):
        # Progress file the website reads for its "scanning..." bar.
        db.write_status("bot", {
            "running": running, "what": "Discord announcements", "current": current,
            "done": i, "total": len(channels), "new_events": found,
            "readable_clubs": len({club["id"] for club, _ in channels}), "updated": time.time(),
        })

    for i, (club, channel) in enumerate(channels):
        progress(i, f"{club['name']} #{channel.name}")
        try:
            # channel.history = the channel's past messages, newest first.
            # "async for" is a for-loop that can pause while Discord sends the next batch.
            async for message in channel.history(limit=CATCH_UP_MESSAGES, after=oldest):
                if message.author.bot or message.content.startswith("!"):
                    continue
                found += await handle_announcement(message, live=False)
        except discord.Forbidden:
            print(f"[bot] can't read history in #{channel.name} (missing Read Message History permission)")
    progress(len(channels), "", running=False)
    print(f"[bot] catch-up ({reason}) done: {found} new event(s)")


async def watch_for_scan_requests():
    """Every 3 seconds, check whether the control panel asked for a scan."""
    last = db.new_scan_request(0) or 0   # ignore requests made before the bot started
    while True:
        await asyncio.sleep(3)
        stamp = db.new_scan_request(last)
        if stamp:
            last = stamp
            print("[bot] scan requested from the control panel")
            await catch_up("scan button")


def pretty_time(start):
    """'2026-10-14T19:30' -> 'Wed Oct 14, 7:30 PM'   and   '2026-10-14' -> 'Wed Oct 14 (all day)'"""
    if len(start) == 10:
        return datetime.strptime(start, "%Y-%m-%d").strftime("%a %b %d (all day)").replace(" 0", " ")
    dt = datetime.strptime(start, "%Y-%m-%dT%H:%M")
    # %-I (hour without leading zero) doesn't work on Windows, so strip the zero ourselves.
    return dt.strftime("%a %b %d, %I:%M %p").replace(" 0", " ")


# ----------------------------------------------------------------------
# 3.5 Start the bot
# ----------------------------------------------------------------------

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing from .env (see step 3 setup instructions).")
    db.init_db()   # make sure the tables exist
    if ai.FAKE:
        print("[bot] GEMINI_FAKE=1: using fake events, Gemini is NOT being called")
    # client.run() logs in and keeps the bot running until you press Ctrl+C.
    client.run(TOKEN)
