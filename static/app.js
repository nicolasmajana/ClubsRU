// static/app.js: everything the page DOES. Runs in the visitor's browser (not in Python).
//
// The pattern used everywhere:
//   1. fetch("/api/...")      -> ask our Flask server (app.py) for data, or send it data
//   2. await response.json()  -> turn the answer into JavaScript objects
//   3. update the page        -> build HTML elements and put them on screen

// ======================================================================
// 5.1 Who is this visitor? (no accounts: a random id saved in the browser)
// ======================================================================
// localStorage keeps small values in this browser even after closing the tab.
// The first visit creates a random id; later visits reuse it, so your follows are remembered.
function getUserId() {
  let id = localStorage.getItem("user_id");
  if (!id) {
    id = crypto.randomUUID();   // e.g. "3f2b8c1e-..."
    localStorage.setItem("user_id", id);
  }
  return id;
}
const USER_ID = getUserId();

// Data we've loaded from the server. "let" = a variable that can change later.
let clubs = [];
let events = [];
let calendar;   // the FullCalendar object, created in setupCalendar()

// ======================================================================
// 5.2 Small helpers
// ======================================================================

// Shortcut: $("#calendar") instead of document.querySelector("#calendar")
const $ = (selector) => document.querySelector(selector);

// Call our API and return the JSON. Throws an Error with the server's message if it fails,
// so callers can show it (app.py sends errors as {"error": "..."}).
async function api(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

// Safely put text inside HTML. Without this, a club description containing "<script>"
// would RUN as code on the page (a security bug called XSS). This turns < into &lt; etc.
function esc(text) {
  const div = document.createElement("div");
  div.textContent = text ?? "";
  return div.innerHTML;
}

// Give each club its own consistent color: turn the club id into a number (a "hash"),
// then pick from the light shades of our design palette (they sit on the maroon day squares,
// with maroon text). Same id -> same color every time.
const CLUB_COLORS = ["#DFC694", "#F5EFE1", "#AFC6D6", "#FFE6B0", "#F2B8BD", "#E9D8B4"];
function clubColor(clubId) {
  let hash = 0;
  for (const ch of clubId) hash = (hash * 31 + ch.charCodeAt(0)) % 360;
  return CLUB_COLORS[hash % CLUB_COLORS.length];
}

// An ALL-DAY event is stored as just a date ("2026-10-14"), with no time.
const isAllDay = (start) => start.length === 10;

// "2026-10-14T19:30" -> "Wed, Oct 14 · 7:30 PM"     "2026-10-14" -> "Wed, Oct 14 · All day"
function prettyDate(start) {
  if (isAllDay(start)) {
    // Add a time before parsing: a bare date is read as UTC midnight, which is the
    // PREVIOUS evening in New Jersey and would show the wrong day.
    return new Date(start + "T12:00").toLocaleDateString("en-US", {
      weekday: "short", month: "short", day: "numeric",
    }) + " · All day";
  }
  return new Date(start).toLocaleString("en-US", {
    weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
  }).replace(/, (\d+:)/, " · $1");   // "Wed, Oct 14, 7:30 PM" -> "Wed, Oct 14 · 7:30 PM"
}

// Link that opens Google Calendar's "new event" screen, pre-filled.
function googleCalendarLink(ev) {
  // Google wants "20261014T193000". Remove "-" and ":" and add seconds.
  const fmt = (s) => s.replace(/[-:]/g, "") + "00";
  // All-day events use plain dates ("20261014/20261015"); the end is the NEXT day.
  const nextDay = (d) => {
    const x = new Date(d + "T12:00");
    x.setDate(x.getDate() + 1);
    return x.toISOString().slice(0, 10).replace(/-/g, "");
  };
  const dates = isAllDay(ev.start)
    ? `${ev.start.replace(/-/g, "")}/${nextDay(ev.start)}`
    : `${fmt(ev.start)}/${fmt(ev.end || addHour(ev.start))}`;
  const params = new URLSearchParams({
    action: "TEMPLATE",
    text: `${ev.title} · ${ev.club_name}`,
    dates,
    ctz: "America/New_York",
    details: [ev.description, ev.source_url].filter(Boolean).join("\n\n"),
    location: [ev.location, ev.campus].filter(Boolean).join(", "),
  });
  return `https://calendar.google.com/calendar/render?${params}`;
}

// "2026-10-14T19:30" -> "2026-10-14T20:30" (events without an end time last 1 hour)
function addHour(start) {
  const d = new Date(start);
  d.setHours(d.getHours() + 1);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// ======================================================================
// 5.3 Tabs
// ======================================================================
function showTab(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("hidden", p.id !== `tab-${name}`));
  // FullCalendar measures its size when drawn; if it was hidden, tell it to re-measure.
  if (name === "calendar" && calendar) calendar.updateSize();
}

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => showTab(tab.dataset.tab));
});
// Links like <a data-goto="discover"> also switch tabs.
document.addEventListener("click", (e) => {
  const link = e.target.closest("[data-goto]");
  if (link) { e.preventDefault(); showTab(link.dataset.goto); }
});

// ======================================================================
// 5.4 Calendar tab
// ======================================================================
function setupCalendar() {
  // FullCalendar is the library loaded in index.html.
  calendar = new FullCalendar.Calendar($("#calendar"), {
    initialView: window.innerWidth < 700 ? "listWeek" : "dayGridMonth",
    headerToolbar: { left: "prev,next today", center: "title", right: "dayGridMonth,timeGridWeek,listWeek" },
    height: "auto",
    nowIndicator: true,
    eventDisplay: "block",   // every event is a little colored tag (not a dot)
    // Instead of a fixed list, we give FullCalendar a FUNCTION. It calls it whenever it
    // needs events (on load and when we call calendar.refetchEvents()), so filters apply instantly.
    events: (info, success) => success(filteredEvents().map(toCalendarEvent)),
    eventClick: (info) => openEvent(info.event.extendedProps.raw),
  });
  calendar.render();
}

// Convert one of our events into the format FullCalendar expects.
function toCalendarEvent(ev) {
  return {
    id: String(ev.id),
    title: (ev.free_food ? "🍕 " : "") + ev.title,
    start: ev.start,
    end: ev.end || undefined,
    allDay: isAllDay(ev.start),   // shows in the all-day row instead of at a fake time
    color: clubColor(ev.club_id),
    textColor: "#7A0E18",
    extendedProps: { raw: ev },   // keep the full event so the popup can show everything
  };
}

// Apply the checkboxes / campus dropdown to the event list.
function filteredEvents() {
  const followedIds = new Set(clubs.filter((c) => c.followed).map((c) => c.id));
  const onlyFollowed = $("#only-followed").checked;
  const onlyFood = $("#only-food").checked;
  const campus = $("#campus-filter").value;

  return events.filter((ev) =>
    (!onlyFollowed || followedIds.has(ev.club_id)) &&
    (!onlyFood || ev.free_food) &&
    (!campus || ev.campus === campus)
  );
}

function refreshCalendar() {
  if (!calendar) return;
  calendar.refetchEvents();
  const shown = filteredEvents();
  $("#event-count").textContent = `${shown.length} event${shown.length === 1 ? "" : "s"}`;
  const followsNone = !clubs.some((c) => c.followed);
  $("#empty-hint").classList.toggle("hidden", !($("#only-followed").checked && followsNone));
}

// Any filter change -> redraw.
["#only-followed", "#only-food", "#campus-filter"].forEach((sel) =>
  $(sel).addEventListener("change", refreshCalendar)
);

// The popup with full event details.
function openEvent(ev) {
  $("#ev-title").textContent = ev.title;
  $("#ev-club").textContent = ev.club_name;
  $("#ev-when").textContent = "🕒 " + prettyDate(ev.start) + (ev.end ? ` to ${prettyDate(ev.end).split(" · ")[1]}` : "");
  $("#ev-where").textContent = "📍 " + ([ev.location, ev.campus].filter(Boolean).join(", ") || "Location TBA");
  $("#ev-food").classList.toggle("hidden", !ev.free_food);
  $("#ev-desc").textContent = ev.description || "";
  $("#ev-gcal").href = googleCalendarLink(ev);
  $("#ev-source").classList.toggle("hidden", !ev.source_url);
  if (ev.source_url) $("#ev-source").href = ev.source_url;
  $("#event-dialog").showModal();
}
$("#ev-close").addEventListener("click", () => $("#event-dialog").close());

// ======================================================================
// 5.5 Discover tab: club cards with Follow buttons
// ======================================================================
function renderClubs() {
  const search = $("#club-search").value.toLowerCase();
  const category = $("#category-filter").value;

  const visible = clubs.filter((c) =>
    (!category || c.category === category) &&
    // search across name, description and tags
    `${c.name} ${c.description} ${c.tags}`.toLowerCase().includes(search)
  );

  // Build one card per club. `...${}...` is a template string (like a Python f-string).
  $("#club-grid").innerHTML = visible.map((c) => `
    <article class="club">
      <h3>${esc(c.name)}</h3>
      <div>
        <span class="chip">${esc(c.category)}</span>
        ${c.campus ? `<span class="chip">${esc(c.campus)}</span>` : ""}
      </div>
      <p>${esc(c.description)}</p>
      <div class="row">
        <button class="follow ${c.followed ? "following" : ""}" data-club="${esc(c.id)}">
          ${c.followed ? "✓ Following" : "+ Follow"}
        </button>
        ${c.instagram ? `<a class="button" href="https://instagram.com/${esc(c.instagram)}" target="_blank" rel="noopener">Instagram</a>` : ""}
        ${c.discord_invite ? `<a class="button" href="${esc(c.discord_invite)}" target="_blank" rel="noopener">Discord</a>` : ""}
      </div>
    </article>`).join("") || `<p class="muted">No clubs match.</p>`;
}

// Follow or unfollow a club, then redraw everything that shows follow status.
async function toggleFollow(clubId) {
  const club = clubs.find((c) => c.id === clubId);
  const follow = !club.followed;

  await api("/api/follow", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: USER_ID, club_id: club.id, follow }),
  });
  club.followed = follow ? 1 : 0;
  renderClubs();
  renderRecommendations();
  refreshCalendar();
}

// One click listener per area handles every Follow button inside it ("event delegation").
// Used by both the club grid and the recommendation list.
for (const area of ["#club-grid", "#recommend-results"]) {
  $(area).addEventListener("click", (e) => {
    const button = e.target.closest(".follow");
    if (button) toggleFollow(button.dataset.club);
  });
}

$("#club-search").addEventListener("input", renderClubs);
$("#category-filter").addEventListener("change", renderClubs);

// ----------------------------------------------------------------------
// STEP 8: "Not sure where to start?" recommendations
// ----------------------------------------------------------------------
let recommendations = null;   // the last answer: [{club_id, reason}, ...] (null = not asked yet)

$("#recommend-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const interests = $("#interests").value.trim();
  if (!interests) return;

  const button = $("#recommend-btn");
  button.disabled = true;
  button.textContent = "Thinking...";
  try {
    const data = await api("/api/recommend", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ interests }),
    });
    recommendations = data.recommendations;
    renderRecommendations();
  } catch (err) {
    $("#recommend-results").innerHTML = `<p class="message error">${esc(err.message)}</p>`;
  } finally {
    button.disabled = false;
    button.textContent = "✨ Recommend";
  }
});

function renderRecommendations() {
  if (recommendations === null) return;   // nothing asked yet, nothing to show
  if (recommendations.length === 0) {
    $("#recommend-results").innerHTML = `<p class="message muted">No strong matches. Try describing it differently, or browse the clubs below.</p>`;
    return;
  }
  // Numbered list, best match first. Each row: club name, Gemini's reason, Follow button.
  $("#recommend-results").innerHTML = `<ol class="recs">${recommendations.map((r) => {
    const c = clubs.find((club) => club.id === r.club_id);
    if (!c) return "";
    return `
      <li class="rec">
        <div>
          <strong>${esc(c.name)}</strong> <span class="chip">${esc(c.category)}</span>
          <p>${esc(r.reason)}</p>
        </div>
        <button class="follow ${c.followed ? "following" : ""}" data-club="${esc(c.id)}">
          ${c.followed ? "✓ Following" : "+ Follow"}
        </button>
      </li>`;
  }).join("")}</ol>`;
}

// ======================================================================
// 5.6 Add Event tab: flyer/caption -> Gemini -> review -> save
// ======================================================================
const CAMPUSES = ["", "Busch", "Livingston", "College Ave", "Cook/Douglass", "Online", "Other"];

$("#extract-form").addEventListener("submit", async (e) => {
  e.preventDefault();   // stop the browser's default "reload the page" form behavior
  const button = $("#extract-btn");
  const results = $("#extract-results");
  button.disabled = true;
  button.textContent = "Reading with Gemini...";
  results.innerHTML = "";

  try {
    // FormData packs the form's fields AND the uploaded file, ready to send.
    const data = await api("/api/extract", { method: "POST", body: new FormData(e.target) });
    if (data.events.length === 0) {
      results.innerHTML = `<p class="message muted">No event found in that post.</p>`;
    } else {
      results.innerHTML = data.events.map(previewForm).join("");
    }
  } catch (err) {
    results.innerHTML = `<p class="message error">${esc(err.message)}</p>`;
  } finally {
    // "finally" runs whether it worked or failed: always re-enable the button.
    button.disabled = false;
    button.textContent = "✨ Extract with Gemini";
  }
});

// An editable card for one extracted event. The user fixes anything Gemini got wrong, then saves.
function previewForm(ev) {
  return `
    <form class="card preview">
      <label>Title <input name="title" value="${esc(ev.title)}" required></label>
      <div class="two">
        <label>Starts <input type="datetime-local" name="start" value="${esc(isAllDay(ev.start) ? ev.start + "T12:00" : ev.start)}" required></label>
        <label>Ends <input type="datetime-local" name="end" value="${esc(ev.end || "")}"></label>
      </div>
      <div class="two">
        <label>Location <input name="location" value="${esc(ev.location)}"></label>
        <label>Campus
          <select name="campus">
            ${CAMPUSES.map((c) => `<option ${c === ev.campus ? "selected" : ""}>${c}</option>`).join("")}
          </select>
        </label>
      </div>
      <label>Description <textarea name="description" rows="2">${esc(ev.description)}</textarea></label>
      <label class="toggle"><input type="checkbox" name="all_day" ${isAllDay(ev.start) ? "checked" : ""}> All day (no time on the flyer)</label>
      <label class="toggle"><input type="checkbox" name="free_food" ${ev.free_food ? "checked" : ""}> 🍕 Free food</label>
      <button type="submit" class="primary">Save to calendar</button>
    </form>`;
}

// Saving a reviewed preview card.
$("#extract-results").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const f = Object.fromEntries(new FormData(form));   // form fields -> {title: ..., start: ...}

  try {
    await api("/api/events", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        club_id: $("#add-club").value,
        title: f.title,
        // All day: keep only the date part ("2026-10-14") and no end time.
        start: f.all_day ? f.start.slice(0, 10) : f.start,
        end: f.all_day ? null : f.end || null,
        location: f.location,
        campus: f.campus,
        description: f.description,
        free_food: Boolean(f.free_food),   // unchecked checkboxes are simply missing
        source: "upload",
      }),
    });
    form.outerHTML = `<p class="message">✅ Saved "${esc(f.title)}" to the calendar.</p>`;
    await loadEvents();
  } catch (err) {
    form.insertAdjacentHTML("beforeend", `<p class="message error">${esc(err.message)}</p>`);
  }
});

// ======================================================================
// 6. Calendar subscription (Google Calendar sync)
// ======================================================================
function setupSync() {
  // Our feed lives at /calendar/<your id>.ics on the server (see calendar_feed in app.py).
  // PUBLIC_URL (from .env) wins if set, e.g. a tunnel address. Otherwise use this page's address.
  const base = window.PUBLIC_URL || location.origin;
  const feedUrl = `${base}/calendar/${USER_ID}.ics`;
  // webcal:// is the same address with a different "scheme": it tells the computer
  // "open this in the calendar app and SUBSCRIBE", instead of downloading the file once.
  const webcalUrl = feedUrl.replace(/^https?:\/\//, "webcal://");

  $("#feed-url").value = feedUrl;
  // Google Calendar's "add calendar from URL" screen, pre-filled with our feed.
  $("#google-sub").href = `https://calendar.google.com/calendar/r?cid=${encodeURIComponent(webcalUrl)}`;
  $("#webcal-sub").href = webcalUrl;

  // Google can't download a feed from "localhost" (that means "this laptop" to Google's servers).
  const isLocal = /localhost|127\.0\.0\.1/.test(base);
  $("#localhost-warning").classList.toggle("hidden", !isLocal);

  $("#copy-feed").addEventListener("click", async () => {
    await navigator.clipboard.writeText(feedUrl);
    $("#copy-feed").textContent = "Copied!";
    setTimeout(() => ($("#copy-feed").textContent = "Copy link"), 1500);
  });
}

// ======================================================================
// 5.7 Loading data and starting up
// ======================================================================
async function loadClubs() {
  clubs = await api(`/api/clubs?user_id=${encodeURIComponent(USER_ID)}`);

  // Fill the category dropdown (Discover) and the club dropdown (Add Event) once.
  if ($("#category-filter").options.length === 1) {
    const categories = [...new Set(clubs.map((c) => c.category).filter(Boolean))].sort();
    $("#category-filter").innerHTML += categories.map((c) => `<option>${esc(c)}</option>`).join("");
    $("#add-club").innerHTML = clubs.map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join("");
  }
  renderClubs();
}

async function loadEvents() {
  // We load ALL events and filter in the browser, so toggling "Only clubs I follow"
  // is instant (no new request).
  events = await api("/api/events");
  refreshCalendar();
}


// ======================================================================
// Background scan bar: shows what the watcher / Discord bot are doing
// ======================================================================
// The watcher and bot write their progress to small files; /api/scan-status reads them.
// Every 3 seconds we ask for it and update the bar at the bottom of the page.
async function updateScanBar() {
  let status;
  try {
    status = await api("/api/scan-status");
  } catch {
    return;   // server restarting: try again next time
  }
  const bar = $("#scan-bar");
  const w = status.watcher;
  const b = status.bot;
  // Something scanning right now? Prefer showing that.
  const active = [w, b].find((s) => s && s.running);

  if (active) {
    const percent = active.total ? Math.round((active.done / active.total) * 100) : 0;
    const where = active.what === "Discord announcements" ? "Discord" : "Instagram & websites";
    $("#scan-text").textContent =
      `🔍 Scanning ${where}: ${active.current || "..."} (${Math.min(active.done + 1, active.total)} of ${active.total} followed)` +
      (active.new_events ? ` · ${active.new_events} new event${active.new_events === 1 ? "" : "s"}` : "");
    $("#scan-fill").style.width = `${percent}%`;
    bar.classList.remove("hidden", "idle");
  } else if (w || b) {
    // Idle: a short summary, and when the next automatic check happens.
    const next = w && w.next_check ? Math.max(0, Math.round(w.next_check - Date.now() / 1000)) : null;
    const followed = w ? w.total : 0;   // the watcher only scans followed clubs
    $("#scan-text").textContent = followed === 0
      ? "Follow clubs on the Discover tab to start scanning for their events"
      : `✓ Scanning ${followed} followed club${followed === 1 ? "" : "s"} · ${status.total_events} event${status.total_events === 1 ? "" : "s"} found` +
        (next !== null ? ` · next check in ${next}s` : "");
    bar.classList.remove("hidden");
    bar.classList.add("idle");
  } else {
    bar.classList.add("hidden");   // neither program is running
  }
}

async function start() {
  setupCalendar();
  setupSync();
  await loadClubs();
  await loadEvents();
  // Check for new events every 15 seconds, so an announcement the Discord bot just
  // picked up appears on screen without refreshing. Great for the live demo.
  setInterval(loadEvents, 15000);
  updateScanBar();
  setInterval(updateScanBar, 3000);
}

start();
