# ClubsRU

**Every Rutgers club event in one calendar, collected automatically from Instagram and Discord.**

🔗 **Live demo:** _link coming soon_ (online only while our demo laptop is running it)
🏆 Built at [HackRU Fall 2026](https://hackru-fall-2026.devpost.com/)

---

## The problem

At Rutgers, being an involved student is hard to keep track of. You join several organizations, each one announces its events in a different place, and on top of that you have classes. It's easy to lose track of what is happening and when, especially as a freshman trying to get involved, when everything seems to happen at the same time.

Today, the only way to stay informed is to check each club's Instagram and Discord one by one, ask friends, or wait for the next involvement fair. Some students don't use Instagram at all, yet for many organizations it's the only place events are announced.

## Our solution

**ClubsRU** is a simple tracker for Rutgers clubs:

- **Discover clubs.** Browse a list of popular Rutgers organizations, filter it by category and topic, or describe your interests and let Gemini recommend clubs that fit you.
- **Follow the ones you like.** Pick the clubs you care about.
- **Events are collected for you, automatically.** ClubsRU reads the places clubs already post:
  - **Discord:** a bot reads each club's announcements channel and adds new events to the database as they're posted.
  - **Instagram:** Google Gemini reads clubs' recent posts (captions and flyer images) and decides whether they announce a new event worth adding.
  - **Club websites:** checked for new events whenever the page changes.
- **One calendar.** All the events from the clubs you follow appear in a single calendar view, similar to Canvas, with filters for free food 🍕 and campus.
- **Syncs with Google Calendar.** One subscription link adds your clubs' events to your own Google Calendar (or Apple Calendar / Outlook), and it keeps updating on its own, so you never have to copy events by hand.

The result: student life gets simpler. It's easier to join different organizations, see which events you can go to, and spot which ones clash with everything else you have going on that day. And students no longer need an Instagram account just to stay involved.

## How it works

```
 Discord #announcements ──► bot.py ─────┐
 Instagram posts/flyers ──► watcher.py ─┼──► Gemini ──► database ──► website (app.py)
 Club websites ───────────► watcher.py ─┘   reads posts,             │
                                            returns clean events     ▼
                                                        Google Calendar feed (.ics)
```

- **Gemini** turns a messy post ("GBM this Wed 7:30 in Hill 114, pizza!! 🍕") into structured events: title, date, time, location, campus and free food. It handles flyers, relative dates ("this Friday"), multi-day events, and posts that aren't events at all.
- **Every post is read only once**, and duplicate events (the same event on Instagram and Discord) are merged.
- Only clubs that someone follows are scanned.

**Tech stack:** Python, Flask, SQLite, discord.py, Google Gemini API, FullCalendar, iCalendar feeds, Cloudflare Tunnel.

---

## Run it locally

You need **Python 3.12+** and **Git** on Windows (commands below are for Windows; on Mac/Linux use `source .venv/bin/activate` and `python3`).

### 1. Get the code and install
```bash
git clone https://github.com/nicolasmajana/HackRU-2026.git
cd HackRU-2026
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Add your keys
Copy `.env.example` to a new file named `.env` and fill in:

| Setting | Where to get it | Required? |
|---|---|---|
| `GEMINI_API_KEY` | [Google AI Studio](https://aistudio.google.com/apikey) (free) | Yes, unless you use fake mode |
| `DISCORD_TOKEN` | [Discord Developer Portal](https://discord.com/developers/applications) → your app → **Bot** → Reset Token. Also turn on **Message Content Intent** on the same page. | Only for the Discord bot |
| `GEMINI_FAKE` | `1` = fake events, no Gemini calls (good for trying it out). `0` = real Gemini. | Optional |

Never commit `.env`. It's already in `.gitignore`.

### 3. Start it
**Easiest:** double-click **`control_panel.pyw`**. It opens a small window where you can start and stop each part, see live logs, copy the public link, and trigger a scan.

**Or by hand**, each command in its own terminal:
```bash
python app.py        # the website → http://localhost:5000
python bot.py        # the Discord bot (needs DISCORD_TOKEN)
python watcher.py    # checks club Instagram feeds and websites
```

### 4. Try it
1. Open **http://localhost:5000** → **Discover Clubs** → follow a few clubs.
2. In the control panel, click **📥 Load sample flyers**. It puts real club flyers (from `data/flyers/`) into the demo Instagram feed and scans them.
3. Watch the bar at the bottom of the site while it scans, and the events appear on **My Calendar**.

**Discord:** invite your bot to a server, type `!link <club-id>` (type `!clubs` to see the ids), then post an announcement in a channel with "announcement" or "event" in its name.

### 5. Google Calendar sync (optional)
Google's servers have to download your calendar feed, so `localhost` won't work. Start a free Cloudflare tunnel (the control panel's **Public link** row does this for you):
```bash
winget install --id Cloudflare.cloudflared
cloudflared tunnel --url http://localhost:5000
```
Open the site through the `https://...trycloudflare.com` link it prints, then go to **My Calendar → Add to Google Calendar**. Google refreshes subscribed calendars every few hours.

---

## About the Instagram integration

ClubsRU doesn't scrape Instagram: that breaks Instagram's rules and gets blocked quickly. In production, clubs connect their account once through Instagram's official API (`data/instagram_accounts.json`). For the demo, folders in `data/instagram_demo/<club handle>/` act as each club's feed: drop in a flyer, and it's read and added automatically.

## Team

Built at HackRU Fall 2026 by Nicolas Majana and Maryam Alzahmi, with AI-assisted development (Claude).
