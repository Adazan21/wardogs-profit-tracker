"""
app.py — Wardogs Profit Tracker desktop app.

A small dashboard around tracker.py / plot_graph.py. Reads match state and
profit/loss straight from Wardogs' own Steam Rich Presence — just needs
Steam and the game running, nothing to set up. Auto-detects when a match
starts and ends, no manual "start tracking" step. Browse past matches as
cards, watch stat tiles + an interactive graph update live, and hover the
chart for a tooltip at any point in time.

Usage:
    python app.py
"""

import csv
import ctypes
import glob
import math
import os
import sys
import threading
import time
from datetime import datetime
import tkinter as tk

import numpy as np
from PIL import Image, ImageTk

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
import matplotlib.transforms as mtransforms
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

import appdata
import tracker
import plot_graph
import rolexp
import richpresence
import mapinfo
import cloudsync
import updater
import autolaunch
import news

# ---------------------------------------------------------------- palette --
# Panels are told apart purely by these background shades — no outlined
# borders anywhere in the chrome (the one exception: a colored border as a
# meaningful selection/active indicator, e.g. the current tab or match
# card, never as plain decoration).
APP_BG = "#080a0f"
CARD = plot_graph.BG          # matches the chart's own background exactly
CARD_ALT = "#11161d"
TEXT = plot_graph.TEXT
MUTED = plot_graph.MUTED
ACCENT = plot_graph.CASH_LINE
GOOD = plot_graph.RATE_POS
BAD = plot_graph.RATE_NEG
GOLD = "#ffd60a"

FONT = "Segoe UI"
POLL_MS = 2000

# rolexp.ROLES uses the internal save-file tag name ("Infantry"); Wardogs'
# own UI displays that track as "ASSAULT" — icons cropped from that same
# in-game unlock screen, so filenames follow the display name, not the tag.
ROLE_ICON_FILES = {
    "Infantry": "assault",
    "Medic": "medic",
    "Recon": "recon",
    "Support": "support",
    "Driver": "driver",
    "Pilot": "pilot",
}
ROLE_DISPLAY_NAMES = {"Infantry": "Assault"}
ROLE_ICON_SIZE = 28
_role_icon_cache = {}
_ui_icon_cache = {}


def _icons_dir():
    # Bundled by profitdog.spec's `datas` entry and extracted alongside the
    # frozen app — same sys._MEIPASS pattern richpresence.py uses for its DLL.
    base = sys._MEIPASS if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "icons")


def role_display_name(role):
    return ROLE_DISPLAY_NAMES.get(role, role)


def role_icon(role):
    """PhotoImage for a role's icon, or None (e.g. 'Wardog' has no icon —
    it's the overall career level, not one of the six role tracks). Source
    PNGs are 64x64 — resized here to the actual on-screen size, since
    Tkinter Labels don't scale images and would otherwise just show a
    cropped, zoomed-in fragment of the full-res icon."""
    if role not in _role_icon_cache:
        name = ROLE_ICON_FILES.get(role)
        path = os.path.join(_icons_dir(), f"role_{name}.png") if name else None
        if path and os.path.exists(path):
            img = Image.open(path).convert("RGBA").resize((ROLE_ICON_SIZE, ROLE_ICON_SIZE), Image.LANCZOS)
            _role_icon_cache[role] = ImageTk.PhotoImage(img)
        else:
            _role_icon_cache[role] = None
    return _role_icon_cache[role]


_app_icon_cache = {}


def app_icon_image(size):
    """The app's own icon (icons/app_icon.png), resized for in-UI use —
    e.g. next to the header wordmark — separate from _set_window_icon's
    .ico use for the title bar/taskbar."""
    if size not in _app_icon_cache:
        path = os.path.join(_icons_dir(), "app_icon.png")
        if os.path.exists(path):
            img = Image.open(path).convert("RGBA").resize((size, size), Image.LANCZOS)
            _app_icon_cache[size] = ImageTk.PhotoImage(img)
        else:
            _app_icon_cache[size] = None
    return _app_icon_cache[size]


def ui_icon(name):
    """PhotoImage for a small header-button glyph (icons/icon_<name>.png —
    generated to match MUTED exactly, at the size they're drawn, since
    system emoji (the previous ⚙/📰) render as full-color glyphs that
    clash with the rest of the palette rather than blending into it."""
    if name not in _ui_icon_cache:
        path = os.path.join(_icons_dir(), f"icon_{name}.png")
        if os.path.exists(path):
            _ui_icon_cache[name] = ImageTk.PhotoImage(Image.open(path).convert("RGBA"))
        else:
            _ui_icon_cache[name] = None
    return _ui_icon_cache[name]


SORT_CYCLE = ["recent", "profit_desc", "profit_asc"]
SORT_LABELS = {
    "recent": "↕  Most recent",
    "profit_desc": "↕  Profit: high → low",
    "profit_asc": "↕  Profit: low → high",
}


def list_sessions(sort_mode="recent"):
    files = glob.glob("session_*.csv")
    if sort_mode == "recent":
        files.sort(key=os.path.getmtime, reverse=True)
    else:
        # A session with no readings yet (just detected, before the first
        # value lands) has no summary to sort by — treated as net 0 rather
        # than excluded, so it still shows up somewhere sensible instead of
        # vanishing from the list under a non-default sort.
        nets = {f: (session_summary(f) or {}).get("net", 0) for f in files}
        files.sort(key=lambda f: nets[f], reverse=(sort_mode == "profit_desc"))
    return files


def fmt_money(value):
    """'$1,234' / '-$1,234' — sign-aware, since profit_loss can be negative
    (Rich Presence tracking) unlike the old OCR-based absolute cash total."""
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.0f}"


def display_name(csv_path):
    name = os.path.basename(csv_path)
    if name.startswith("session_") and name.endswith(".csv"):
        name = name[len("session_"):-len(".csv")]
    return name


def friendly_session_time(csv_path):
    """'Sep 17, 7:19 PM' from a session_YYYYMMDD_HHMMSS.csv filename — falls
    back to the raw name for anything that doesn't match (e.g. old test/
    leftover files)."""
    try:
        dt = datetime.strptime(display_name(csv_path), "%Y%m%d_%H%M%S")
        return dt.strftime("%b %d, %I:%M %p").replace(" 0", " ")
    except ValueError:
        return display_name(csv_path)


def load_xp_log():
    """{'session_20260916_224408.csv': {'Wardog': 2, ...}} from role_xp_log.csv
    — only roles with a nonzero gain, only sessions that gained anything.
    A session can have several rows now (gains get logged the moment
    they're detected, not just once at match-end — see tracker.py), so
    this sums them rather than keeping only the last one. Rows with a
    blank session_file (the gain landed between matches) aren't
    attributable to any card and are skipped here."""
    result = {}
    if not os.path.exists(tracker.XP_LOG_PATH):
        return result
    with open(tracker.XP_LOG_PATH, newline="") as f:
        for row in csv.DictReader(f):
            session_file = row.get("session_file")
            if not session_file:
                continue
            for role in rolexp.ROLES:
                try:
                    v = int(row.get(role, 0) or 0)
                except ValueError:
                    v = 0
                if v:
                    result.setdefault(session_file, {})[role] = result.get(session_file, {}).get(role, 0) + v
    return result


def fmt_xp(gains):
    if not gains:
        return None
    return ", ".join(f"+{v} {role_display_name(r)}" for r, v in gains.items())


def session_summary(csv_path):
    """Cheap last-row read for the sidebar card — avoids a full pandas load per card."""
    try:
        with open(csv_path, newline="") as f:
            reader = csv.reader(f)
            next(reader, None)
            rows = [row for row in reader if row]
        if not rows:
            return None
        first_cash = float(rows[0][2])
        last_cash = float(rows[-1][2])
        duration_min = float(rows[-1][1]) / 60
        map_name = rows[0][3] if len(rows[0]) > 3 and rows[0][3] else None
        faction = rows[0][4] if len(rows[0]) > 4 and rows[0][4] else None
        return {
            "final_cash": last_cash,
            "net": last_cash - first_cash,
            "duration_min": duration_min,
            "map": map_name,
            "faction": faction,
        }
    except Exception:
        return None


# ------------------------------------------------------------ draw helpers --
def round_rect_points(x1, y1, x2, y2, r):
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    return [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]


def draw_round_rect(canvas, x1, y1, x2, y2, radius=12, **kwargs):
    return canvas.create_polygon(round_rect_points(x1, y1, x2, y2, radius), smooth=True, **kwargs)


# ------------------------------------------------------------------ widgets --
class StatTile(tk.Canvas):
    def __init__(self, parent, label, width=168, height=78):
        super().__init__(parent, width=width, height=height, bg=parent["bg"], highlightthickness=0, bd=0)
        self.w, self.h = width, height
        self.label = label
        self.render("—", MUTED)

    def render(self, value, accent=TEXT):
        self.delete("all")
        self.create_text(2, 20, anchor="w", text=self.label.upper(), fill=MUTED, font=(FONT, 8, "bold"))
        self.create_text(2, self.h - 24, anchor="w", text=value, fill=accent, font=(FONT, 18, "bold"))


class StatusPill(tk.Canvas):
    def __init__(self, parent, width=280, height=34):
        super().__init__(parent, width=width, height=height, bg=parent["bg"], highlightthickness=0, bd=0)
        self.w, self.h = width, height
        self.set_idle()

    def _render(self, text, dot, fg, fill, border=""):
        self.delete("all")
        draw_round_rect(self, 0, 0, self.w, self.h, self.h / 2, fill=fill, outline=border, width=1)
        self.create_oval(14, self.h / 2 - 4, 22, self.h / 2 + 4, fill=dot, outline="")
        self.create_text(30, self.h / 2, anchor="w", text=text, fill=fg, font=(FONT, 9, "bold"))

    def set_idle(self, text="Not tracking"):
        self._render(text, MUTED, MUTED, CARD_ALT)

    def set_watching(self):
        self._render("Watching for a match…", ACCENT, ACCENT, "#0d1b26", "#173042")

    def set_active(self, text):
        self._render(text, GOOD, GOOD, "#0d2318", "#1a3d24")


class TabButton(tk.Canvas):
    def __init__(self, parent, text, on_click, width=90, height=32):
        super().__init__(parent, width=width, height=height, bg=parent["bg"],
                          highlightthickness=0, bd=0, cursor="hand2")
        self.text = text
        self.w, self.h = width, height
        self.active = False
        self._render()
        self.bind("<Button-1>", lambda _e: on_click())

    def _render(self):
        self.delete("all")
        if self.active:
            fill, border, fg = "#132530", ACCENT, ACCENT
        else:
            fill, border, fg = CARD_ALT, "", MUTED
        draw_round_rect(self, 1, 1, self.w - 1, self.h - 1, self.h / 2, fill=fill, outline=border, width=1.2)
        self.create_text(self.w / 2, self.h / 2, text=self.text, fill=fg, font=(FONT, 9, "bold"))

    def set_active(self, val):
        self.active = val
        self._render()


class MatchCard(tk.Canvas):
    def __init__(self, parent, session_path, on_click, xp_gain=None, width=226, height=92):
        super().__init__(parent, width=width, height=height, bg=parent["bg"],
                          highlightthickness=0, bd=0, cursor="hand2")
        self.session_path = session_path
        self.on_click = on_click
        self.w, self.h = width, height
        self.selected = False
        self.live = False
        self.summary = session_summary(session_path)
        self.xp_gain = xp_gain
        self._render()
        self.bind("<Enter>", lambda _e: self._render(hover=True))
        self.bind("<Leave>", lambda _e: self._render(hover=False))
        self.bind("<Button-1>", lambda _e: self.on_click(self.session_path))

    def _render(self, hover=False):
        self.delete("all")
        if self.selected:
            fill, border = "#132530", ACCENT
        elif hover:
            fill, border = "#161b22", ""
        else:
            fill, border = CARD_ALT, ""
        draw_round_rect(self, 1, 1, self.w - 1, self.h - 1, 10, fill=fill, outline=border, width=1.2)

        map_faction = None
        if self.summary:
            map_name = self.summary.get("map")
            map_name = mapinfo.display_map_name(map_name) if map_name else None
            faction = self.summary.get("faction")
            faction = richpresence.faction_name(faction) if faction else None
            map_faction = " · ".join(p for p in (map_name, faction) if p) or None

        heading = map_faction or friendly_session_time(self.session_path)
        self.create_text(14, 17, anchor="w", text=heading, fill=TEXT, font=(FONT, 10, "bold"))
        if self.live:
            self.create_oval(self.w - 22, 10, self.w - 14, 18, fill=GOOD, outline="")

        if map_faction:
            self.create_text(14, 36, anchor="w", text=friendly_session_time(self.session_path),
                              fill=MUTED, font=(FONT, 8))

        if self.summary:
            sub = f'{self.summary["duration_min"]:.0f}m · {fmt_money(self.summary["final_cash"])}'
            net = self.summary["net"]
            net_color = GOOD if net >= 0 else BAD
            self.create_text(14, 56, anchor="w", text=sub, fill=MUTED, font=(FONT, 8))
            self.create_text(self.w - 14, 56, anchor="e",
                              text=f'{"+" if net >= 0 else ""}{net:,.0f}',
                              fill=net_color, font=(FONT, 9, "bold"))
        else:
            self.create_text(14, 40, anchor="w", text="no data yet", fill=MUTED, font=(FONT, 8, "italic"))

        if self.xp_gain:
            self.create_text(14, 76, anchor="w", text=fmt_xp(self.xp_gain), fill=GOLD, font=(FONT, 8, "bold"))

    def set_selected(self, val):
        self.selected = val
        self._render()

    def refresh_summary(self):
        self.summary = session_summary(self.session_path)
        self.xp_gain = load_xp_log().get(self.session_path)
        self._render()


class ScrollableList(tk.Frame):
    """A vertically scrollable column of widgets (used for the match card list)."""

    def __init__(self, parent, bg):
        super().__init__(parent, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.scrollbar = tk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self.inner.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(self.window, width=e.width))
        self.canvas.bind("<Enter>", lambda _e: self.canvas.bind_all("<MouseWheel>", self._on_wheel))
        self.canvas.bind("<Leave>", lambda _e: self.canvas.unbind_all("<MouseWheel>"))

    def _on_wheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def clear(self):
        for child in self.inner.winfo_children():
            child.destroy()


class ConsentDialog(tk.Toplevel):
    """First-run (and reopenable-from-Settings) opt-in for uploading match
    data to the developer's private database — see cloudsync.py. Shown once
    before any sync ever happens; declining leaves the app exactly as it
    was before this feature existed."""

    BODY_TEXT = (
        "This app can optionally upload your match data to a private database "
        "the developer uses to see stats across players.\n\n"
        "What's sent: your cash-over-time curve, map, faction, match duration, "
        "life count, and role XP gained — plus your Steam ID and display name, "
        "so matches can be tagged to you.\n\n"
        "Who can see it: only the developer. The key this app uses can only "
        "submit data — it cannot read anyone else's matches back, including "
        "your own once sent.\n\n"
        "This is entirely optional. The app works fully offline either way, "
        "and you can change this anytime from Settings (the gear icon up top)."
    )

    def __init__(self, parent, on_done):
        super().__init__(parent)
        self.title("Match data sync")
        self.configure(bg=CARD)
        self.resizable(False, False)
        self.transient(parent)
        self.on_done = on_done

        pad = dict(padx=24)
        tk.Label(self, text="Share your matches?", bg=CARD, fg=TEXT,
                 font=(FONT, 14, "bold")).pack(anchor="w", pady=(20, 6), **pad)
        tk.Label(self, text=self.BODY_TEXT, bg=CARD, fg=MUTED, font=(FONT, 9),
                 justify="left", wraplength=420).pack(anchor="w", **pad)

        btn_row = tk.Frame(self, bg=CARD)
        btn_row.pack(fill="x", pady=(18, 20), **pad)

        def choose(enabled):
            cloudsync.set_consent(enabled)
            self.destroy()
            self.on_done(enabled)

        tk.Button(btn_row, text="Enable sync", command=lambda: choose(True),
                  bg=ACCENT, fg="#001018", relief="flat", bd=0, padx=16, pady=8,
                  font=(FONT, 9, "bold"), cursor="hand2").pack(side="left")
        tk.Button(btn_row, text="No thanks", command=lambda: choose(False),
                  bg=CARD_ALT, fg=MUTED, relief="flat", bd=0, padx=16, pady=8,
                  font=(FONT, 9, "bold"), cursor="hand2").pack(side="left", padx=(10, 0))

        self.protocol("WM_DELETE_WINDOW", lambda: choose(False))
        self.grab_set()


class SettingsDialog(tk.Toplevel):
    """Reopenable anytime from the header's gear button: the sync toggle
    (see ConsentDialog for the full first-run disclosure) plus general app
    preferences."""

    def __init__(self, parent, on_sync_changed):
        super().__init__(parent)
        self.title("Settings")
        self.configure(bg=CARD)
        self.resizable(False, False)
        self.transient(parent)
        self.on_sync_changed = on_sync_changed

        pad = dict(padx=24)
        tk.Label(self, text="Settings", bg=CARD, fg=TEXT,
                 font=(FONT, 14, "bold")).pack(anchor="w", pady=(20, 14), **pad)

        cb_kwargs = dict(bg=CARD, fg=TEXT, selectcolor=CARD_ALT, activebackground=CARD,
                          activeforeground=TEXT, font=(FONT, 10), anchor="w", highlightthickness=0)

        self.sync_var = tk.BooleanVar(value=cloudsync.load_consent()["enabled"])
        tk.Checkbutton(self, text="Share my matches with the developer", variable=self.sync_var,
                        command=self._on_sync_toggle, **cb_kwargs).pack(anchor="w", fill="x", **pad)
        tk.Label(self, bg=CARD, fg=MUTED, font=(FONT, 8), justify="left",
                 text="Uploads your cash curve, map, faction, and role XP, tagged with\n"
                      "your Steam ID/name. Only the developer can read it back."
                 ).pack(anchor="w", pady=(2, 16), **pad)

        if autolaunch.supported():
            self.autolaunch_var = tk.BooleanVar(value=autolaunch.is_enabled())
            tk.Checkbutton(self, text="Launch automatically when Wardogs starts",
                            variable=self.autolaunch_var, command=self._on_autolaunch_toggle,
                            **cb_kwargs).pack(anchor="w", fill="x", pady=(0, 20), **pad)

        tk.Button(self, text="Close", command=self.destroy, bg=CARD_ALT, fg=MUTED,
                  relief="flat", bd=0, padx=16, pady=8, font=(FONT, 9, "bold"),
                  cursor="hand2").pack(anchor="e", pady=(0, 20), padx=24)

        self.grab_set()

    def _on_sync_toggle(self):
        enabled = self.sync_var.get()
        cloudsync.set_consent(enabled)
        if self.on_sync_changed:
            self.on_sync_changed(enabled)

    def _on_autolaunch_toggle(self):
        autolaunch.set_enabled(self.autolaunch_var.get())


# --------------------------------------------------------------------- app --
class App:
    def __init__(self, root):
        self.root = root
        root.title("")  # the header already shows WARDOGS / Profit Tracker; a caption-bar label was just noise
        _center_window(root, 1820, 1080)
        root.configure(bg=APP_BG)
        root.minsize(880, 560)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.watch_thread = None
        self.watch_stop_event = None
        self.watch_state = "waiting"
        self.live_session = None
        self.latest_status = {}
        self.selected_session = None
        self.cards = {}
        self._sessions_dirty = False
        self.sort_mode = "recent"

        self._graph_df = None
        self._animating = False
        self._reveal_token = None
        self._pulse_artists = []
        self._xp_view_signature = None
        self._pulse_after_id = None
        self._poll_after_id = None
        self._slider_full_df = None
        self._slider_csv_path = None
        self._user_scrubbing = False
        self._steam_identity = None

        self._build_ui()
        self.refresh_sessions(select_latest=True)
        self._sync_watcher()
        self._poll()
        self._pulse_tick()
        self._init_cloud_sync()
        self._init_auto_update()

    # ---------- UI ----------
    def _build_ui(self):
        header = tk.Frame(self.root, bg=CARD, height=64)
        header.pack(side="top", fill="x")
        header.pack_propagate(False)

        title_box = tk.Frame(header, bg=CARD)
        title_box.pack(side="left", padx=(20, 26))
        icon_img = app_icon_image(30)
        if icon_img:
            icon_label = tk.Label(title_box, image=icon_img, bg=CARD)
            icon_label.image = icon_img
            icon_label.pack(side="left", padx=(0, 10))
        text_box = tk.Frame(title_box, bg=CARD)
        text_box.pack(side="left")
        tk.Label(text_box, text="WARDOGS", bg=CARD, fg=TEXT, font=(FONT, 13, "bold")).pack(anchor="w")
        tk.Label(text_box, text="Profit Tracker", bg=CARD, fg=MUTED, font=(FONT, 8, "bold")).pack(anchor="w")

        self.status_pill = StatusPill(header)
        self.status_pill.pack(side="right", padx=20)

        self.settings_button = tk.Button(
            header, text=" Settings", image=ui_icon("settings"), compound="left",
            command=self._open_settings,
            bg=CARD, fg=MUTED, relief="flat", bd=0, cursor="hand2",
            font=(FONT, 8, "bold"), activebackground=CARD, activeforeground=TEXT,
        )
        self.settings_button.pack(side="right", padx=(0, 4))

        self.news_button = tk.Button(
            header, text=" News", image=ui_icon("news"), compound="left",
            command=self.show_news,
            bg=CARD, fg=MUTED, relief="flat", bd=0, cursor="hand2",
            font=(FONT, 8, "bold"), activebackground=CARD, activeforeground=TEXT,
        )
        self.news_button.pack(side="right", padx=(0, 4))

        body = tk.Frame(self.root, bg=APP_BG)
        body.pack(side="top", fill="both", expand=True)

        # ----- sidebar -----
        sidebar = tk.Frame(body, bg=CARD, width=258)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        side_header = tk.Frame(sidebar, bg=CARD)
        side_header.pack(fill="x", padx=16, pady=(18, 8))
        tk.Label(side_header, text="MATCHES", bg=CARD, fg=MUTED, font=(FONT, 9, "bold")).pack(side="left")
        tk.Button(side_header, text="⟳", command=lambda: self.refresh_sessions(), bg=CARD, fg=MUTED,
                  relief="flat", bd=0, cursor="hand2", font=(FONT, 11),
                  activebackground=CARD, activeforeground=TEXT).pack(side="right")

        self.sort_button = tk.Button(
            sidebar, text=SORT_LABELS[self.sort_mode], command=self._cycle_sort,
            bg=CARD_ALT, fg=MUTED, relief="flat", bd=0, cursor="hand2",
            font=(FONT, 8, "bold"), activebackground=CARD_ALT, activeforeground=TEXT,
            anchor="w", padx=8, pady=5,
        )
        self.sort_button.pack(fill="x", padx=16, pady=(0, 10))

        self.list_container = ScrollableList(sidebar, CARD)
        self.list_container.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        # ----- main column -----
        main_col = tk.Frame(body, bg=APP_BG)
        main_col.pack(side="left", fill="both", expand=True)

        # Everything below is the normal "looking at a match" view, grouped
        # under one frame so News (a separate main function, not a tab
        # alongside Profit/$-per-min/XP — those are all just different
        # views of the same match) can swap the whole thing out in place
        # rather than opening a separate window.
        self.match_view = tk.Frame(main_col, bg=APP_BG)
        self.match_view.pack(fill="both", expand=True)

        stats_row = tk.Frame(self.match_view, bg=APP_BG)
        stats_row.pack(fill="x", padx=18, pady=18)
        self.tile_current = StatTile(stats_row, "Current Cash")
        self.tile_peak = StatTile(stats_row, "Peak Cash")
        self.tile_net = StatTile(stats_row, "Net")
        self.tile_rate = StatTile(stats_row, "Avg Rate")
        for tile in (self.tile_current, self.tile_peak, self.tile_net, self.tile_rate):
            tile.pack(side="left", padx=(0, 12))

        tabs_row = tk.Frame(self.match_view, bg=APP_BG)
        tabs_row.pack(fill="x", padx=18, pady=(0, 10))
        self.view_mode = "profit"
        self.tab_profit = TabButton(tabs_row, "Profit", lambda: self.set_view_mode("profit"))
        self.tab_rate = TabButton(tabs_row, "$/min", lambda: self.set_view_mode("rate"))
        self.tab_xp = TabButton(tabs_row, "XP", lambda: self.set_view_mode("xp"))
        self.tab_profit.pack(side="left", padx=(0, 8))
        self.tab_rate.pack(side="left", padx=(0, 8))
        self.tab_xp.pack(side="left")
        self.tab_profit.set_active(True)

        chart_card = tk.Frame(self.match_view, bg=CARD, highlightthickness=0, bd=0)
        chart_card.pack(fill="both", expand=True, padx=18, pady=(0, 18))

        self.fig, self.axes = plt.subplots(2, 1, figsize=(8, 6), sharex=True, facecolor=CARD)
        self.canvas = FigureCanvasTkAgg(self.fig, master=chart_card)
        self.canvas.get_tk_widget().configure(bg=CARD, highlightthickness=0)

        self.time_slider = tk.Scale(
            chart_card, from_=0, to=100, orient="horizontal",
            bg=CARD, fg=TEXT, troughcolor=CARD_ALT, highlightthickness=0,
            bd=0, sliderrelief="flat", showvalue=0, activebackground=ACCENT,
            command=self._on_scrub,
        )
        self.time_slider.configure(state="disabled")
        self.time_slider.bind("<Button-1>", self._on_scrub_press)
        self.time_slider.bind("<ButtonRelease-1>", self._on_scrub_release)
        self.time_slider.pack(side="bottom", fill="x", padx=16, pady=(0, 14))

        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=16, pady=(16, 4))

        self.xp_view = tk.Frame(chart_card, bg=CARD)

        # ----- news panel (swapped in over match_view, not a popup) -----
        self.news_panel = tk.Frame(main_col, bg=APP_BG)
        self._build_news_panel(self.news_panel)

        self._show_placeholder("Select a match on the left — new ones appear here automatically")

    def _build_news_panel(self, parent):
        header = tk.Frame(parent, bg=APP_BG)
        header.pack(fill="x", padx=18, pady=(18, 10))
        tk.Label(header, text="News & Updates", bg=APP_BG, fg=TEXT,
                 font=(FONT, 14, "bold")).pack(side="left")
        tk.Button(
            header, text="← Back", command=self.show_match_view, bg=CARD, fg=MUTED,
            activebackground=CARD, activeforeground=TEXT, relief="flat", bd=0,
            cursor="hand2", font=(FONT, 9, "bold"), padx=12, pady=6,
        ).pack(side="right")

        self.news_list_container = ScrollableList(parent, APP_BG)
        self.news_list_container.pack(fill="both", expand=True, padx=18, pady=(0, 18))

    def _render_news(self):
        self.news_list_container.clear()
        posts = news.fetch_news()
        if not posts:
            tk.Label(self.news_list_container.inner, text="No news yet — check back later.",
                      bg=APP_BG, fg=MUTED, font=(FONT, 9, "italic")).pack(anchor="w", pady=10)
            return
        for post in posts:
            card = tk.Frame(self.news_list_container.inner, bg=CARD)
            card.pack(fill="x", pady=(0, 10))
            post_header = tk.Frame(card, bg=CARD)
            post_header.pack(fill="x", padx=16, pady=(14, 2))
            tk.Label(post_header, text=post.get("title") or "(untitled)", bg=CARD, fg=TEXT,
                      font=(FONT, 12, "bold"), wraplength=440, justify="left").pack(side="left")
            when = (post.get("created_at") or "")[:10]
            tk.Label(post_header, text=when, bg=CARD, fg=MUTED, font=(FONT, 8)).pack(side="right")
            tk.Label(card, text=post.get("content") or "", bg=CARD, fg=MUTED,
                      font=(FONT, 9), justify="left", wraplength=520).pack(anchor="w", padx=16, pady=(0, 14))

    def show_news(self):
        self.match_view.pack_forget()
        self.news_panel.pack(fill="both", expand=True)
        self._render_news()

    def show_match_view(self):
        self.news_panel.pack_forget()
        self.match_view.pack(fill="both", expand=True)

    def _clear_fig_text(self):
        for txt in list(self.fig.texts):
            txt.remove()

    def _show_placeholder(self, message):
        self._clear_fig_text()
        for ax in self.axes:
            ax.clear()
            ax.set_facecolor(CARD)
            ax.axis("off")
        self.fig.text(0.5, 0.5, message, ha="center", va="center", color=MUTED, fontsize=12)
        self.canvas.draw_idle()
        self._reset_tiles()
        self._slider_full_df = None
        self.time_slider.configure(state="disabled", to=100)
        self.time_slider.set(0)  # safe no-op in _on_scrub: _slider_full_df is None above

    def _reset_tiles(self):
        self.tile_current.render("—", MUTED)
        self.tile_peak.render("—", MUTED)
        self.tile_net.render("—", MUTED)
        self.tile_rate.render("—", MUTED)

    # ---------- profit / rate / XP tabs ----------
    def set_view_mode(self, mode):
        self.view_mode = mode
        self.tab_profit.set_active(mode == "profit")
        self.tab_rate.set_active(mode == "rate")
        self.tab_xp.set_active(mode == "xp")
        if mode == "xp":
            self.canvas.get_tk_widget().pack_forget()
            self.time_slider.pack_forget()
            self._render_xp_view()
            self.xp_view.pack(fill="both", expand=True, padx=16, pady=16)
        else:
            self.xp_view.pack_forget()
            self.time_slider.pack(side="bottom", fill="x", padx=16, pady=(0, 14))
            self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=16, pady=(16, 4))
            self._apply_panel_split()
            self.canvas.draw_idle()

    def _apply_panel_split(self):
        """Profit/rate tabs each show one panel full-height rather than the
        usual stacked pair — plot_graph.draw() always populates both axes
        of the same figure regardless of tab; this only controls which is
        visible and how much space it gets. Reasserted after every draw
        (see _draw_frame), not just on tab switch, since a full (non-quick)
        redraw re-runs plot_graph.draw()'s own tight_layout() under the
        normal equal split, and resets sharex's default of only the bottom
        panel showing x-tick labels — both of which would otherwise
        silently undo this the next time a match is selected.

        Expanding the visible axes to the union of both axes' current
        (already correctly tight_layout'd, equally-split) positions, rather
        than trying to get tight_layout itself to redo the split under a
        near-zero ratio for the hidden one, sidesteps a real edge case:
        tight_layout's margin/tick-label spacing reservations don't scale
        down cleanly at extreme ratios, which clipped the visible chart's
        own axis off the bottom of the figure entirely."""
        ax1, ax2 = self.axes
        show_cash = self.view_mode != "rate"
        full_pos = mtransforms.Bbox.union([ax1.get_position(), ax2.get_position()])
        ax1.set_visible(show_cash)
        ax2.set_visible(not show_cash)
        ax1.tick_params(labelbottom=show_cash)
        ax1.set_xlabel("Minutes into match" if show_cash else "")
        (ax1 if show_cash else ax2).set_position(full_pos)

    def _render_xp_view(self):
        totals = rolexp.read_role_xp()
        # Gains are logged the instant they're detected now, live match or
        # not (see tracker.py) — load_xp_log() already sums a session's
        # rows, so this works the same whether the match is still going or
        # long over.
        gains = load_xp_log().get(self.selected_session) if self.selected_session else None

        # Rebuilding this view (destroy + recreate every label/icon) is
        # visibly flickery, and _poll() calls this every 2s — so skip it
        # entirely unless the data actually changed since the last render.
        signature = (self.selected_session, tuple(sorted(totals.items())), tuple(sorted((gains or {}).items())))
        if signature == self._xp_view_signature:
            return
        self._xp_view_signature = signature

        for child in self.xp_view.winfo_children():
            child.destroy()

        tk.Label(self.xp_view, text="ROLE XP", bg=CARD, fg=MUTED, font=(FONT, 9, "bold")).pack(anchor="w", pady=(0, 16))

        roles = sorted(rolexp.ROLES, key=lambda r: totals.get(r, 0), reverse=True)

        if not totals:
            tk.Label(self.xp_view, text="No role XP data yet — play a match with Wardogs running.",
                      bg=CARD, fg=MUTED, font=(FONT, 9, "italic")).pack(anchor="w")
            return

        for role in roles:
            row = tk.Frame(self.xp_view, bg=CARD)
            row.pack(fill="x", pady=7)
            icon = role_icon(role)
            icon_box = tk.Frame(row, width=ROLE_ICON_SIZE + 4, height=ROLE_ICON_SIZE + 4, bg=CARD)
            icon_box.pack_propagate(False)
            icon_box.pack(side="left", padx=(0, 10))
            if icon:
                icon_slot = tk.Label(icon_box, image=icon, bg=CARD)
                icon_slot.image = icon  # keep a reference — Tkinter drops unreferenced PhotoImages
                icon_slot.pack(expand=True)
            tk.Label(row, text=role_display_name(role), bg=CARD, fg=TEXT, font=(FONT, 13, "bold"),
                     width=10, anchor="w").pack(side="left")
            tk.Label(row, text=str(totals.get(role, "—")), bg=CARD, fg=TEXT, font=(FONT, 13), anchor="w").pack(side="left", padx=(8, 0))
            gain = gains.get(role) if gains else None
            if gain:
                tk.Label(row, text=f"+{gain} this match", bg=CARD, fg=GOLD, font=(FONT, 9, "bold")).pack(side="left", padx=(16, 0))

    # ---------- session list ----------
    def _cycle_sort(self):
        self.sort_mode = SORT_CYCLE[(SORT_CYCLE.index(self.sort_mode) + 1) % len(SORT_CYCLE)]
        self.sort_button.configure(text=SORT_LABELS[self.sort_mode])
        self.refresh_sessions()

    def refresh_sessions(self, select_latest=False):
        sessions = list_sessions(self.sort_mode)
        xp_log = load_xp_log()
        self.list_container.clear()
        self.cards = {}
        for path in sessions:
            card = MatchCard(self.list_container.inner, path, self.on_select_session, xp_gain=xp_log.get(path))
            card.pack(fill="x", pady=4)
            card.live = (path == self.live_session)
            card.set_selected(path == self.selected_session)
            self.cards[path] = card
        if select_latest and sessions and self.selected_session is None:
            self.on_select_session(sessions[0])

    def on_select_session(self, session_path):
        self.show_match_view()
        if self.selected_session and self.selected_session in self.cards:
            self.cards[self.selected_session].set_selected(False)
        self.selected_session = session_path
        self._user_scrubbing = False
        if session_path in self.cards:
            self.cards[session_path].set_selected(True)
        self.render_graph(session_path, animate=True)

    def render_graph(self, csv_path, animate=False):
        if not os.path.exists(csv_path):
            return
        df = plot_graph.load(csv_path)
        if df.empty:
            self._graph_df = None
            self._show_placeholder(f"No readings logged yet for {display_name(csv_path)}")
            return

        self._slider_full_df = df
        self._slider_csv_path = csv_path
        self.time_slider.configure(state="normal", to=max(len(df) - 1, 0))

        if self._user_scrubbing and not animate:
            # A live match grew while the user is mid-drag reviewing an
            # earlier point — data's refreshed for when they let go, but
            # don't yank the view out from under them.
            return

        # A reveal already in flight gets superseded — this render wins.
        self._reveal_token = object()

        if animate and len(df) > 2:
            self._play_reveal(df, csv_path, self._reveal_token)
        else:
            self._animating = False
            self._set_slider(len(df) - 1)
            self._draw_frame(df, csv_path)

    def _set_slider(self, idx):
        self.time_slider.set(idx)

    def _on_scrub_press(self, _event):
        self._user_scrubbing = True

    def _on_scrub_release(self, _event):
        self._user_scrubbing = False

    def _on_scrub(self, value):
        """Slider `command` callback — fires both on real user drag and on
        our own programmatic `.set()` calls (_play_reveal syncing the thumb
        as it plays). Gated on `_user_scrubbing`, which only a genuine
        mouse-press on the widget sets — NOT a "did we just call .set()"
        flag, because Tkinter can invoke `command` on a deferred/idle pass
        rather than synchronously inside `.set()`, which made a
        synchronous-toggle flag here race: by the time the deferred call
        landed, the flag had already been reset, so a reveal's own sync
        calls were misread as the user taking over and the animation
        silently aborted a few frames in."""
        if not self._user_scrubbing:
            return
        if self._slider_full_df is None or self._slider_full_df.empty:
            return
        idx = max(0, min(int(float(value)), len(self._slider_full_df) - 1))
        self._reveal_token = object()  # user grabbed it — abandon any auto-reveal
        self._animating = False
        self._draw_frame(
            self._slider_full_df.iloc[:idx + 1], self._slider_csv_path,
            quick=True, full_df=self._slider_full_df,
        )

    def _draw_frame(self, df, csv_path, quick=False, full_df=None):
        """Render one frame of the chart (either the final still image, one
        step of the reveal animation, or a slider scrub) and update the stat
        tiles to match. `quick`/`full_df` — see plot_graph.draw()."""
        self._graph_df = df
        # Cheap regardless of quick mode, and both matter for recovering
        # from the placeholder state (axis off + leftover fig.text) — a
        # reveal's very first frame is often quick=True, so gating these
        # behind `not quick` left stale placeholder text/hidden axes
        # showing underneath the newly-drawn chart.
        self._clear_fig_text()
        for ax in self.axes:
            ax.axis("on")
        plot_graph.draw(self.fig, self.axes, df, display_name(csv_path), quick=quick, full_df=full_df, show_title=False)
        self._apply_panel_split()
        self.canvas.draw_idle()

        current = df["cash"].iloc[-1]
        peak = df["cash"].max()
        net = df["cash"].iloc[-1] - df["cash"].iloc[0]
        minutes = max(df["minutes_elapsed"].iloc[-1], 0.01)
        avg_rate = net / minutes

        self.tile_current.render(fmt_money(current), TEXT)
        self.tile_peak.render(fmt_money(peak), GOLD)
        self.tile_net.render(f'{"+" if net >= 0 else ""}{net:,.0f}', GOOD if net >= 0 else BAD)
        self.tile_rate.render(f'{"+" if avg_rate >= 0 else ""}{avg_rate:,.0f}/min', GOOD if avg_rate >= 0 else BAD)

        if self.view_mode == "xp":
            self._render_xp_view()

    def _play_reveal(self, df, csv_path, token):
        """Replay the match from the start: the line sweeps from the first
        reading to the last over ~1s, instead of just appearing fully drawn.
        Keeps the slider thumb in sync so it reads as "the slider playing
        itself" rather than a separate animation the user then has to
        reconcile with a static control."""
        self._animating = True
        n = len(df)
        frame_count = min(45, n)
        cutoffs = sorted(set(int(round(v)) for v in np.linspace(2, n, frame_count)))
        if cutoffs[-1] != n:
            cutoffs.append(n)
        frame_delay = max(900 // len(cutoffs), 16)

        def step(i):
            if token is not self._reveal_token:
                return  # a newer selection/update came in — abandon this replay
            cutoff = cutoffs[i]
            is_last = i + 1 >= len(cutoffs)
            self._draw_frame(df.iloc[:cutoff], csv_path, quick=not is_last, full_df=df)
            self._set_slider(cutoff - 1)
            if not is_last:
                self.root.after(frame_delay, lambda: step(i + 1))
            else:
                self._animating = False

        step(0)

    # ---------- live pulse ----------
    def _pulse_tick(self):
        for artist in self._pulse_artists:
            try:
                artist.remove()
            except Exception:
                pass
        self._pulse_artists = []

        show_pulse = (
            not self._animating
            and self.live_session is not None
            and self.selected_session == self.live_session
            and self._graph_df is not None
            and not self._graph_df.empty
        )
        if show_pulse:
            df = self._graph_df
            x = float(df["minutes_elapsed"].iloc[-1])
            y = float(df["cash"].iloc[-1])
            period = 1.6
            phase = (time.time() % period) / period
            pulse = 0.5 - 0.5 * math.cos(phase * 2 * math.pi)  # 0 -> 1 -> 0

            ax1 = self.axes[0]
            dot = ax1.scatter([x], [y], s=50, color=GOOD, edgecolor=CARD, linewidth=1.2, zorder=9)
            ring = ax1.scatter([x], [y], s=80 + 260 * pulse, facecolor="none",
                                edgecolor=GOOD, linewidth=1.8, alpha=max(0.5 * (1 - pulse), 0.05), zorder=8)
            self._pulse_artists = [dot, ring]
            self.canvas.draw_idle()

        self._pulse_after_id = self.root.after(60, self._pulse_tick)

    # ---------- tracking ----------
    def _sync_watcher(self):
        """Start the auto-detect watcher as soon as it can run — it just
        always runs, no manual start/stop. tracker.auto_track() now runs
        (and retries on its own, forever) until stopped, so this is mostly
        just a safety net in case the thread ever dies from a genuine
        unhandled exception."""
        running = self.watch_thread is not None and self.watch_thread.is_alive()
        if not running:
            self._start_watcher()

    def _start_watcher(self):
        self.watch_stop_event = threading.Event()

        def on_session_start(filename):
            self.live_session = filename
            self.latest_status = {"elapsed": 0, "cash": None}
            self._sessions_dirty = True

        def on_update(filename, elapsed, cash):
            if filename == self.live_session:
                self.latest_status = {"elapsed": elapsed, "cash": cash}

        def on_session_end(_filename):
            self.live_session = None
            self._sessions_dirty = True
            # The one-shot sync at identity-resolution time (see
            # _on_steam_identity) typically fires right as Wardogs launches
            # — before this match's own CSV exists — so without also
            # syncing here, the match that's actually in progress would
            # never get picked up until some *later* app run.
            self._trigger_sync()

        def run():
            tracker.auto_track(
                2.0, stop_event=self.watch_stop_event,
                on_session_start=on_session_start, on_update=on_update,
                on_session_end=on_session_end, on_identity=self._on_steam_identity,
                on_state=self._on_watch_state,
            )

        self.watch_thread = threading.Thread(target=run, daemon=True)
        self.watch_thread.start()

    def _on_watch_state(self, state):
        """Fired from the watcher thread whenever its coarse status changes
        — see tracker.auto_track's docstring for the possible values.
        Just stashes it; _poll() picks it up on the main thread, same
        pattern as live_session/latest_status below."""
        self.watch_state = state

    # ---------- cloud sync (opt-in) ----------
    def _init_cloud_sync(self):
        consent = cloudsync.load_consent()
        if not consent["decided"]:
            # Give the window a moment to actually appear first, rather than
            # a modal dialog popping up before the user's even seen the app.
            self.root.after(500, lambda: ConsentDialog(self.root, self._on_consent_changed))

        # Identity resolved on some earlier launch (cached to disk the first
        # time Wardogs was actually seen running — see _on_steam_identity)
        # lets this launch sync any backlog immediately, without needing
        # Wardogs open again just to re-confirm who's uploading.
        cached_steam_id, cached_persona_name = cloudsync.load_cached_identity()
        if cached_steam_id:
            self._steam_identity = (cached_steam_id, cached_persona_name)
            self._trigger_sync()

    def _open_settings(self):
        SettingsDialog(self.root, self._on_consent_changed)

    def _on_consent_changed(self, enabled):
        if enabled:
            self._trigger_sync()

    def _on_steam_identity(self, steam_id, persona_name):
        """Fired once by tracker.auto_track's watcher thread, right after
        Steam init succeeds — see that function's docstring for why identity
        is only ever read there and handed off as plain strings, rather than
        this opening a second concurrent Steamworks session."""
        self._steam_identity = (steam_id, persona_name)
        cloudsync.save_cached_identity(steam_id, persona_name)
        self._trigger_sync()

    def _trigger_sync(self):
        """Safe to call more than once (e.g. once when identity resolves,
        again if the user enables sync afterwards, whichever order those
        happen in) — cloudsync.sync_now() tracks what's already synced
        locally, so a redundant call just no-ops quickly."""
        if self._steam_identity:
            threading.Thread(target=cloudsync.sync_now, args=self._steam_identity, daemon=True).start()

    # ---------- auto-update ----------
    def _init_auto_update(self):
        threading.Thread(target=self._run_update_check, daemon=True).start()

    def _run_update_check(self):
        def announce(tag):
            # Runs on the update-check thread — hop to the main thread before
            # touching any Tkinter widget.
            self.root.after(0, lambda: self.status_pill.set_active(f"Updating to {tag} — restarting…"))
        updater.run_auto_update_check(on_update_found=announce)

    # ---------- polling loop ----------
    def _poll(self):
        self._sync_watcher()

        if self.view_mode == "xp":
            self._render_xp_view()

        if self._sessions_dirty:
            self._sessions_dirty = False
            self.refresh_sessions()
            if self.live_session:
                self.on_select_session(self.live_session)

        if self.live_session:
            elapsed = self.latest_status.get("elapsed", 0)
            cash = self.latest_status.get("cash")
            if cash is not None:
                mins, secs = divmod(int(elapsed), 60)
                sign = "-" if cash < 0 else ""
                self.status_pill.set_active(f"Tracking — {mins}:{secs:02d} — {sign}${abs(cash):,.0f}")
            else:
                self.status_pill.set_watching()
            if self.selected_session == self.live_session and not self._animating:
                self.render_graph(self.live_session, animate=False)
            if self.live_session in self.cards:
                self.cards[self.live_session].refresh_summary()
        elif self.watch_state == "no_steam":
            self.status_pill.set_idle("Steam not running")
        else:
            self.status_pill.set_watching()

        self._poll_after_id = self.root.after(POLL_MS, self._poll)

    def on_close(self):
        if self.watch_stop_event:
            self.watch_stop_event.set()
        # Both loops reschedule themselves via self.root.after() — cancel the
        # pending ones or they fire once more after destroy() against a dead
        # interpreter ("invalid command name ... _pulse_tick/_poll").
        if self._pulse_after_id:
            self.root.after_cancel(self._pulse_after_id)
        if self._poll_after_id:
            self.root.after_cancel(self._poll_after_id)
        self.root.destroy()


def _center_window(root, width, height):
    """Tkinter's default placement for a plain `geometry("WxH")` (no
    position) isn't actually screen-centered — it's left up to the window
    manager, which on Windows tends to land oddly (e.g. hugging the bottom
    of the screen once the window's tall enough). Centers against the
    Windows *work area* (SPI_GETWORKAREA) — the usable desktop excluding
    the taskbar — rather than the full screen resolution; centering
    against the full screen leaves the window looking low, since the
    taskbar eats into the bottom of the space the window actually has to
    sit in."""
    root.update_idletasks()
    try:
        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        SPI_GETWORKAREA = 0x0030
        rect = RECT()
        if not ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
            raise OSError
        x = rect.left + (rect.right - rect.left - width) // 2
        y = rect.top + (rect.bottom - rect.top - height) // 2
    except Exception:
        x = (root.winfo_screenwidth() - width) // 2
        y = (root.winfo_screenheight() - height) // 2
    root.geometry(f"{width}x{height}+{max(x, 0)}+{max(y, 0)}")


def _set_window_icon(root):
    """Without this, Tkinter shows the generic Tk feather icon in the
    title bar/taskbar — one of the fastest tells that a window is "just a
    Python script" rather than a real app."""
    try:
        root.iconbitmap(default=os.path.join(_icons_dir(), "app_icon.ico"))
    except tk.TclError:
        pass  # e.g. icon file missing — cosmetic only, never worth failing startup over


def _blend_titlebar_with_theme(root):
    """Windows draws the title bar/window border white/light by default
    regardless of the app's own theme — a stark banner sitting on top of
    an otherwise all-dark window. This goes past just "dark" to make the
    OS chrome disappear into the app: DWMWA_CAPTION_COLOR and
    DWMWA_BORDER_COLOR (Windows 11 only) set the caption and window-edge
    color to CARD exactly — the same color as the app's own header, right
    below it — so there's no visible seam between OS frame and app content
    at all. DWMWA_USE_IMMERSIVE_DARK_MODE is set unconditionally too, as a
    fallback for Windows 10 (no per-color API there, but plain dark still
    reads far closer to "invisible" than the OS default white)."""
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())

        dark = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE: 20 on Win11/Win10 20H1+, 19 on older Win10
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(dark), ctypes.sizeof(dark)) == 0:
                break

        # COLORREF is 0x00BBGGRR, not the usual 0xRRGGBB.
        r, g, b = int(CARD[1:3], 16), int(CARD[3:5], 16), int(CARD[5:7], 16)
        card_colorref = ctypes.c_int((b << 16) | (g << 8) | r)
        DWMWA_BORDER_COLOR = 34
        DWMWA_CAPTION_COLOR = 35
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_BORDER_COLOR, ctypes.byref(card_colorref), ctypes.sizeof(card_colorref))
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_CAPTION_COLOR, ctypes.byref(card_colorref), ctypes.sizeof(card_colorref))
    except Exception:
        pass  # cosmetic only — never worth failing startup over


def main():
    # tracker.py's session CSVs/role_xp_log.csv and richpresence.py's
    # steam_appid.txt all use plain relative paths, which otherwise land
    # wherever this process's cwd happens to be — for a double-clicked exe,
    # that's wherever the user saved it (often Downloads), which would
    # clutter it with CSVs forever. Landing here instead makes all of that
    # existing relative-path code resolve into the app's own data folder
    # without needing to touch every call site.
    os.chdir(appdata.data_dir())

    if "--watch" in sys.argv:
        autolaunch.run_watcher()  # headless — waits for Wardogs, then launches the GUI; never returns
        return
    if not autolaunch.acquire_single_instance_lock():
        return  # another copy is already running — its window was just brought to front instead
    autolaunch.apply_default_if_unset()
    root = tk.Tk()
    _set_window_icon(root)
    _blend_titlebar_with_theme(root)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
