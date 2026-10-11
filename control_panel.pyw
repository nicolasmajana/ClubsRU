# control_panel.pyw: a small window to turn the project's programs on and off.
#
# Double-click this file (or run:  .venv\Scripts\pythonw.exe control_panel.pyw).
# The ".pyw" ending means Windows opens it WITHOUT a black console window.
#
# It shows the 4 programs, whether each one is running (green) or stopped (gray),
# Start/Stop buttons, the public tunnel link, a Fake/Real Gemini switch, and logs.
#
# It also notices programs started OUTSIDE the panel (e.g. in a VS Code terminal),
# so the lights are always true, and Stop works on those too.

import os
import re
import sys
import shutil
import subprocess
import threading
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))   # so "import db" works even when double-clicked
import db                       # only uses Python's built-in modules, so any Python can load it

PYTHON = BASE / ".venv" / "Scripts" / "python.exe"
CLOUDFLARED = Path(r"C:\Program Files (x86)\cloudflared\cloudflared.exe")
LOG_DIR = BASE / "data" / "logs"
ENV_FILE = BASE / ".env"

# Windows flag: start child programs without opening a console window for each one.
NO_WINDOW = 0x08000000

# The programs. "match" is a pattern that finds the program in the list of running processes.
PROGRAMS = {
    "website": {
        "label": "Website",
        "what": "app.py: the site at localhost:5000",
        "cmd": [str(PYTHON), "app.py"],
        "match": re.compile(r'python\.exe.*[\\/ "]app\.py', re.I),
    },
    "bot": {
        "label": "Discord bot",
        "what": "bot.py: reads #announcements",
        "cmd": [str(PYTHON), "bot.py"],
        "match": re.compile(r'python\.exe.*[\\/ "]bot\.py', re.I),
    },
    "watcher": {
        "label": "Watcher",
        "what": "watcher.py: Instagram + club websites",
        "cmd": [str(PYTHON), "watcher.py"],
        "match": re.compile(r'python\.exe.*[\\/ "]watcher\.py', re.I),
    },
    "tunnel": {
        "label": "Public link",
        "what": "cloudflared: needed for Google Calendar",
        "cmd": [str(CLOUDFLARED), "tunnel", "--no-autoupdate", "--url", "http://localhost:5000"],
        "match": re.compile(r'cloudflared\.exe.*\btunnel\b', re.I),
    },
}


# ----------------------------------------------------------------------
# Finding, starting and stopping processes
# ----------------------------------------------------------------------

def list_processes():
    """Every running python.exe / cloudflared.exe as (pid, parent_pid, command_line).
    Windows doesn't give command lines through simple tools, so we ask PowerShell."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='cloudflared.exe'\" | "
          "ForEach-Object { \"$($_.ProcessId)|$($_.ParentProcessId)|$($_.CommandLine)\" }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                             text=True, timeout=15, creationflags=NO_WINDOW).stdout
    except Exception:
        return []
    procs = []
    for line in out.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3 and parts[0].isdigit():
            procs.append((int(parts[0]), int(parts[1] or 0), parts[2]))
    return procs


def running_roots(name, procs):
    """PIDs of the TOP process for this program. (The website runs as a small chain of
    processes; stopping the top one with /T stops the whole chain.)"""
    pattern = PROGRAMS[name]["match"]
    pids = {pid for pid, _, cmd in procs if pattern.search(cmd)}
    parents = {pid: parent for pid, parent, _ in procs}
    return [pid for pid in pids if parents.get(pid) not in pids]


def start_program(name):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = open(LOG_DIR / f"{name}.log", "w", encoding="utf-8")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    subprocess.Popen(PROGRAMS[name]["cmd"], cwd=BASE, stdout=log, stderr=subprocess.STDOUT,
                     env=env, creationflags=NO_WINDOW)


def stop_pids(pids):
    # taskkill /T = also stop child processes, /F = force.
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       creationflags=NO_WINDOW)


# ----------------------------------------------------------------------
# Fake mode switch (edits GEMINI_FAKE in .env)
# ----------------------------------------------------------------------

def read_fake_mode():
    if not ENV_FILE.exists():
        return True
    match = re.search(r"(?m)^GEMINI_FAKE=(\S*)", ENV_FILE.read_text(encoding="utf-8-sig"))
    return bool(match and match.group(1) == "1")


def write_fake_mode(fake):
    text = ENV_FILE.read_text(encoding="utf-8-sig") if ENV_FILE.exists() else ""
    line = f"GEMINI_FAKE={'1' if fake else '0'}"
    if re.search(r"(?m)^GEMINI_FAKE=", text):
        text = re.sub(r"(?m)^GEMINI_FAKE=.*$", line, text)
    else:
        text = text.rstrip("\n") + "\n" + line + "\n"
    # Plain UTF-8 WITHOUT the invisible marker (BOM) that breaks reading the first line.
    ENV_FILE.write_text(text, encoding="utf-8")


def tunnel_url():
    """The trycloudflare link, read from the tunnel's log (only if the panel started it)."""
    log = LOG_DIR / "tunnel.log"
    if not log.exists():
        return ""
    found = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", log.read_text(encoding="utf-8", errors="ignore"))
    return found[-1] if found else ""


# ----------------------------------------------------------------------
# The window
# ----------------------------------------------------------------------

GREEN, GRAY, AMBER = "#1a9e4b", "#9a9aa2", "#d99a00"


class Panel(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ClubsRU control panel")
        self.geometry("640x640")
        self.minsize(560, 480)
        self.status = {name: [] for name in PROGRAMS}   # name -> running PIDs
        self.busy = set()                                # programs currently starting/stopping
        self.log_choice = tk.StringVar(value="website")
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh_status()
        self.refresh_log()

    # ---------- layout ----------
    def _build(self):
        pad = {"padx": 12, "pady": 4}
        ttk.Label(self, text="ClubsRU: what's running", font=("Segoe UI", 14, "bold")).pack(anchor="w", **pad)

        table = ttk.Frame(self)
        table.pack(fill="x", padx=12)
        self.rows = {}
        for i, (name, p) in enumerate(PROGRAMS.items()):
            dot = tk.Label(table, text="●", fg=GRAY, font=("Segoe UI", 16))
            dot.grid(row=i, column=0, padx=(0, 6))
            ttk.Label(table, text=p["label"], font=("Segoe UI", 10, "bold"), width=12).grid(row=i, column=1, sticky="w")
            state = ttk.Label(table, text="checking...", width=14)
            state.grid(row=i, column=2, sticky="w")
            ttk.Label(table, text=p["what"], foreground="#666").grid(row=i, column=3, sticky="w", padx=6)
            button = ttk.Button(table, text="Start", width=8, command=lambda n=name: self.toggle(n))
            button.grid(row=i, column=4, padx=4, pady=3)
            ttk.Radiobutton(table, text="log", value=name, variable=self.log_choice,
                            command=self.refresh_log).grid(row=i, column=5)
            self.rows[name] = (dot, state, button)
        table.columnconfigure(3, weight=1)

        actions = ttk.Frame(self)
        actions.pack(fill="x", **pad)
        ttk.Button(actions, text="Start all", command=self.start_all).pack(side="left")
        ttk.Button(actions, text="Stop all", command=self.stop_all).pack(side="left", padx=6)
        ttk.Button(actions, text="Open website", command=lambda: webbrowser.open("http://localhost:5000")).pack(side="left")

        # Public link row
        link = ttk.LabelFrame(self, text="Public link (for Google Calendar / sharing)")
        link.pack(fill="x", **pad)
        self.url_var = tk.StringVar(value="Not running")
        ttk.Entry(link, textvariable=self.url_var, state="readonly").pack(side="left", fill="x", expand=True, padx=6, pady=6)
        ttk.Button(link, text="Copy", command=self.copy_url).pack(side="left")
        ttk.Button(link, text="Open", command=self.open_url).pack(side="left", padx=6)

        # Find events now
        find = ttk.LabelFrame(self, text="Find events")
        find.pack(fill="x", **pad)
        ttk.Button(find, text="🔍 Scan for events now", command=self.scan_now).pack(side="left", padx=6, pady=6)
        ttk.Button(find, text="📥 Load sample flyers", command=self.load_flyers).pack(side="left")
        self.scan_msg = ttk.Label(find, text="", foreground="#666")
        self.scan_msg.pack(side="left", padx=8)

        # Fake / real Gemini
        gem = ttk.LabelFrame(self, text="Gemini")
        gem.pack(fill="x", **pad)
        self.real_var = tk.BooleanVar(value=not read_fake_mode())
        ttk.Checkbutton(gem, text="Use REAL Gemini (unchecked = fake mode, no quota used)",
                        variable=self.real_var, command=self.toggle_fake).pack(side="left", padx=6, pady=6)

        # Log viewer
        ttk.Label(self, text="Log (pick a program with the 'log' buttons above):").pack(anchor="w", padx=12)
        self.log_box = tk.Text(self, height=12, wrap="none", font=("Consolas", 9), bg="#111", fg="#ddd")
        self.log_box.pack(fill="both", expand=True, padx=12, pady=(2, 12))

    # ---------- status (checked in the background every 3 seconds) ----------
    def refresh_status(self):
        def work():
            procs = list_processes()
            result = {name: running_roots(name, procs) for name in PROGRAMS}
            self.after(0, lambda: self.show_status(result))
        threading.Thread(target=work, daemon=True).start()
        self.after(3000, self.refresh_status)

    def show_status(self, result):
        self.status = result
        for name, (dot, state, button) in self.rows.items():
            if name in self.busy:
                continue
            running = bool(result[name])
            dot.config(fg=GREEN if running else GRAY)
            state.config(text="RUNNING" if running else "stopped")
            button.config(text="Stop" if running else "Start", state="normal")
        if result["tunnel"]:
            self.url_var.set(tunnel_url() or "Running (started outside the panel; link is in that terminal)")
        else:
            self.url_var.set("Not running")

    # ---------- buttons ----------
    def toggle(self, name):
        if name == "tunnel" and not CLOUDFLARED.exists():
            messagebox.showerror("cloudflared missing", f"Not found at:\n{CLOUDFLARED}")
            return
        running = self.status.get(name)
        dot, state, button = self.rows[name]
        self.busy.add(name)
        dot.config(fg=AMBER)
        state.config(text="stopping..." if running else "starting...")
        button.config(state="disabled")

        def work():
            if running:
                stop_pids(running)
            else:
                start_program(name)
            # Give it a moment, then let the normal status check take over again.
            self.after(2500, lambda: self.busy.discard(name))
        threading.Thread(target=work, daemon=True).start()

    def start_all(self):
        for name in PROGRAMS:
            if not self.status.get(name) and name not in self.busy:
                self.toggle(name)

    def stop_all(self):
        for name in PROGRAMS:
            if self.status.get(name) and name not in self.busy:
                self.toggle(name)

    def toggle_fake(self):
        write_fake_mode(not self.real_var.get())
        running = [PROGRAMS[n]["label"] for n in ("website", "bot", "watcher") if self.status.get(n)]
        mode = "REAL Gemini" if self.real_var.get() else "fake mode"
        if running and messagebox.askyesno("Restart needed",
                f"Switched to {mode}.\n\nPrograms only read this setting when they start.\n"
                f"Restart {', '.join(running)} now?"):
            for name in ("website", "bot", "watcher"):
                if self.status.get(name):
                    stop_pids(self.status[name])
            self.after(2500, lambda: [start_program(n) for n in ("website", "bot", "watcher")
                                      if PROGRAMS[n]["label"] in running])

    def scan_now(self, skip_confirm=False):
        """Ask the bot and the watcher to look for new events right now."""
        missing = [PROGRAMS[n]["label"] for n in ("bot", "watcher") if not self.status.get(n)]
        if missing and not messagebox.askyesno("Not running",
                f"{' and '.join(missing)} {'is' if len(missing) == 1 else 'are'} not running, so "
                f"{'it' if len(missing) == 1 else 'they'} can't scan.\n\nStart {'it' if len(missing) == 1 else 'them'} now?"):
            return
        mode = "fake mode (no quota used)" if read_fake_mode() else "REAL Gemini"
        if not skip_confirm and not messagebox.askyesno("Scan for events?",
                "This checks right now for posts that haven't been read yet:\n"
                "  • recent messages in linked Discord #announcements channels\n"
                "  • the demo Instagram folders (and connected Instagram accounts)\n"
                "  • club websites\n\n"
                f"Using {mode}. Each NEW post uses 1 Gemini request;\n"
                "posts already read are skipped for free.\n\nStart the scan?"):
            return
        for name in ("bot", "watcher"):
            if not self.status.get(name):
                start_program(name)   # a freshly started bot/watcher scans on its own at startup
        db.request_scan()
        self.scan_msg.config(text="Scanning... watch the bot / watcher logs.")
        self.log_choice.set("watcher")
        self.after(15000, lambda: self.scan_msg.config(text=""))

    def load_flyers(self):
        """Copy the partner's test flyers (data/flyers) into each club's demo Instagram folder,
        using the @handles in data/flyers/answers.txt. Then scan."""
        answers = BASE / "data" / "flyers" / "answers.txt"
        if not answers.exists():
            messagebox.showerror("No flyers", f"Not found:\n{answers}")
            return
        # Lines look like: "01_rupa_spookeasy.png | RUPA (@ruparutgers) | https://..."
        found = re.findall(r"(?m)^(\S+\.(?:png|jpe?g|webp)) \| .*?\(@([\w.]+)\)(?: \| (https://\S+))?",
                           answers.read_text(encoding="utf-8", errors="ignore"))
        links = {f: link for f, _, link in found}
        pairs = [(f, h) for f, h, _ in found]
        handles = {c["instagram"] for c in db.get_clubs() if c["instagram"]}
        todo = [(f, h) for f, h in pairs if h in handles and (BASE / "data" / "flyers" / f).exists()
                and not (BASE / "data" / "instagram_demo" / h / f).exists()]
        if not todo:
            messagebox.showinfo("Nothing to add", "All sample flyers are already in the demo feed.")
            return
        cost = "no quota (fake mode)" if read_fake_mode() else f"about {len(todo)} Gemini requests"
        if not messagebox.askyesno("Load sample flyers?",
                f"Add {len(todo)} flyers to the demo Instagram feed and scan them?\n\n"
                + "\n".join(f"  • @{h}: {f}" for f, h in todo)
                + f"\n\nThis uses {cost}."):
            return
        for file, handle in todo:
            folder = BASE / "data" / "instagram_demo" / handle
            folder.mkdir(parents=True, exist_ok=True)
            shutil.copy(BASE / "data" / "flyers" / file, folder / file)   # new file time = "just posted"
            if links.get(file):   # the real post's link, for "View original post"
                (folder / file).with_suffix(".url").write_text(links[file], encoding="utf-8")
        self.scan_now(skip_confirm=True)

    def copy_url(self):
        url = self.url_var.get()
        if url.startswith("https://"):
            self.clipboard_clear()
            self.clipboard_append(url)

    def open_url(self):
        url = self.url_var.get()
        if url.startswith("https://"):
            webbrowser.open(url)

    # ---------- log viewer ----------
    def refresh_log(self):
        name = self.log_choice.get()
        log = LOG_DIR / f"{name}.log"
        if log.exists():
            lines = log.read_text(encoding="utf-8", errors="ignore").splitlines()[-60:]
            text = "\n".join(lines) or "(no output yet)"
        else:
            text = "(no log: this program wasn't started from the panel)"
        at_bottom = self.log_box.yview()[1] > 0.95
        self.log_box.delete("1.0", "end")
        self.log_box.insert("end", text)
        if at_bottom:
            self.log_box.see("end")
        self.after(2000, self.refresh_log)

    def on_close(self):
        if any(self.status.values()):
            answer = messagebox.askyesnocancel("Quit", "Stop all programs before closing?\n\n"
                                               "Yes = stop everything\nNo = leave them running")
            if answer is None:
                return
            if answer:
                for pids in self.status.values():
                    stop_pids(pids)
        self.destroy()


if __name__ == "__main__":
    if not PYTHON.exists():
        messagebox.showerror("Setup needed", f"Python environment not found:\n{PYTHON}")
        sys.exit(1)
    Panel().mainloop()
