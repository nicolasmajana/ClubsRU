# HackRU-2026

This is the link for the devpost: https://hackru-fall-2026.devpost.com/participants

## Running it

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in `GEMINI_API_KEY` and `DISCORD_TOKEN`.

Then run each of these in its own terminal:

```bash
python app.py     # the website: http://localhost:5000
python bot.py     # the Discord bot
```

`GEMINI_FAKE=1` in `.env` returns fake events without calling Gemini (the free tier is about 20 requests/day per model). Set it to `0` for real extraction.

## Google Calendar sync (needs a public URL)

Each user gets a personal calendar feed at `/calendar/<user-id>.ics`, shown under **Sync to your own calendar** on the site. Google's servers download that feed, so it can't be on `localhost`. Expose the site with a free Cloudflare tunnel:

```bash
winget install --id Cloudflare.cloudflared
cloudflared tunnel --url http://localhost:5000
```

It prints a URL like `https://something.trycloudflare.com`. Open the site through **that** URL and click **Add to Google Calendar**. The link changes every time you restart the tunnel, so subscribe again after a restart (or set `PUBLIC_URL` in `.env`).

Google re-checks subscribed calendars every few hours, so new events don't appear in Google Calendar instantly. They appear on the website within 15 seconds.
