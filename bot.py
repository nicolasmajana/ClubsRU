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
from datetime import datetime

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

@client.event
async def on_ready():
    print(f"[bot] Logged in as {client.user} | in {len(client.guilds)} server(s)")
    for guild in client.guilds:
        club = db.get_club_by_guild(str(guild.id))
        status = f"linked to {club['name']}" if club else "NOT linked yet (type !link <club-id> there)"
        print(f"[bot]   {guild.name}: {status}")


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


async def handle_announcement(message):
    # Which club does this server belong to? If nobody ran !link yet, do nothing.
    club = db.get_club_by_guild(str(message.guild.id))
    if club is None:
        return

    # Is there a flyer? Take the first attachment that's an image.
    image_bytes, mime_type = None, None
    for attachment in message.attachments:
        if attachment.content_type and attachment.content_type.startswith("image/"):
            image_bytes = await attachment.read()   # download the image from Discord
            mime_type = attachment.content_type
            break

    if not message.content.strip() and image_bytes is None:
        return  # e.g. a sticker or a file that isn't an image

    try:
        # Shows "Bot is typing..." in the channel while Gemini works. Nice touch for the demo.
        async with message.channel.typing():
            # ai.extract_events is a normal (blocking) function that waits ~3 seconds for Gemini.
            # Calling it directly would FREEZE the whole bot for those seconds.
            # asyncio.to_thread runs it in a background thread, and "await" waits for the result
            # without blocking everything else.
            events = await asyncio.to_thread(
                ai.extract_events,
                message.content,
                image_bytes=image_bytes,
                mime_type=mime_type or "image/png",
                club_name=club["name"],
                posted_at=message.created_at,   # when the message was posted (resolves "this Friday")
            )
    except Exception as err:
        # Never let one bad message crash the bot. Log it and mark the message.
        print(f"[bot] Gemini failed for message {message.id}: {err}")
        await message.add_reaction("⚠️")
        return

    if not events:
        print(f"[bot] {club['name']}: no event in message {message.id}")
        return  # chatter, congrats, memes... stay quiet

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
    # mention_author=False: reply without pinging the person who posted.
    await message.reply("Added to the club calendar:\n" + "\n".join(lines), mention_author=False)
    print(f"[bot] {club['name']}: saved {len(events)} event(s)")


def pretty_time(start):
    """'2026-10-14T19:30' -> 'Wed Oct 14, 7:30 PM'"""
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
