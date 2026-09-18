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
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

import tracker
import plot_graph
import rolexp
import richpresence
import mapinfo

# ---------------------------------------------------------------- palette --
APP_BG = "#080a0f"
CARD = plot_graph.BG          # matches the chart's own background exactly
CARD_ALT = "#11161d"
BORDER = "#21262d"
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


def list_sessions():
    files = glob.glob("session_*.csv")
    files.sort(key=os.path.getmtime, reverse=True)
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
    — only roles with a nonzero gain, only sessions that gained anything."""
    result = {}
    if not os.path.exists(tracker.XP_LOG_PATH):
        return result
    with open(tracker.XP_LOG_PATH, newline="") as f:
        for row in csv.DictReader(f):
            session_file = row.get("session_file")
            if not session_file:
                continue
            gains = {}
            for role in rolexp.ROLES:
                try:
                    v = int(row.get(role, 0) or 0)
                except ValueError:
                    v = 0
                if v:
                    gains[role] = v
            if gains:
                result[session_file] = gains
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
        draw_round_rect(self, 0, 0, self.w, self.h, 14, fill=CARD_ALT, outline=BORDER, width=1)
        self.create_text(18, 20, anchor="w", text=self.label.upper(), fill=MUTED, font=(FONT, 8, "bold"))
        self.create_text(18, self.h - 24, anchor="w", text=value, fill=accent, font=(FONT, 18, "bold"))


class StatusPill(tk.Canvas):
    def __init__(self, parent, width=280, height=34):
        super().__init__(parent, width=width, height=height, bg=parent["bg"], highlightthickness=0, bd=0)
        self.w, self.h = width, height
        self.set_idle()

    def _render(self, text, dot, fg, fill, border):
        self.delete("all")
        draw_round_rect(self, 0, 0, self.w, self.h, self.h / 2, fill=fill, outline=border, width=1)
        self.create_oval(14, self.h / 2 - 4, 22, self.h / 2 + 4, fill=dot, outline="")
        self.create_text(30, self.h / 2, anchor="w", text=text, fill=fg, font=(FONT, 9, "bold"))

    def set_idle(self, text="Not tracking"):
        self._render(text, MUTED, MUTED, CARD_ALT, BORDER)

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
            fill, border, fg = CARD_ALT, BORDER, MUTED
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
            fill, border = "#161b22", BORDER
        else:
            fill, border = CARD_ALT, CARD_ALT
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


# --------------------------------------------------------------------- app --
class App:
    def __init__(self, root):
        self.root = root
        root.title("Wardogs Profit Tracker")
        root.geometry("1180x720")
        root.configure(bg=APP_BG)
        root.minsize(880, 560)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.watch_thread = None
        self.watch_stop_event = None
        self.steam_ok = True
        self.live_session = None
        self.latest_status = {}
        self.selected_session = None
        self.cards = {}
        self._sessions_dirty = False

        self._graph_df = None
        self._animating = False
        self._reveal_token = None
        self._pulse_artists = []
        self._xp_view_signature = None
        self._pulse_after_id = None
        self._poll_after_id = None

        self._build_ui()
        self.refresh_sessions(select_latest=True)
        self._sync_watcher()
        self._poll()
        self._pulse_tick()

    # ---------- UI ----------
    def _build_ui(self):
        header = tk.Frame(self.root, bg=CARD, height=64)
        header.pack(side="top", fill="x")
        header.pack_propagate(False)

        title_box = tk.Frame(header, bg=CARD)
        title_box.pack(side="left", padx=(20, 26))
        tk.Label(title_box, text="WARDOGS", bg=CARD, fg=TEXT, font=(FONT, 13, "bold")).pack(anchor="w")
        tk.Label(title_box, text="Profit Tracker", bg=CARD, fg=MUTED, font=(FONT, 8, "bold")).pack(anchor="w")

        self.status_pill = StatusPill(header)
        self.status_pill.pack(side="right", padx=20)

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

        self.list_container = ScrollableList(sidebar, CARD)
        self.list_container.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        # ----- main column -----
        main_col = tk.Frame(body, bg=APP_BG)
        main_col.pack(side="left", fill="both", expand=True)

        stats_row = tk.Frame(main_col, bg=APP_BG)
        stats_row.pack(fill="x", padx=18, pady=18)
        self.tile_current = StatTile(stats_row, "Current Cash")
        self.tile_peak = StatTile(stats_row, "Peak Cash")
        self.tile_net = StatTile(stats_row, "Net")
        self.tile_rate = StatTile(stats_row, "Avg Rate")
        for tile in (self.tile_current, self.tile_peak, self.tile_net, self.tile_rate):
            tile.pack(side="left", padx=(0, 12))

        tabs_row = tk.Frame(main_col, bg=APP_BG)
        tabs_row.pack(fill="x", padx=18, pady=(0, 10))
        self.view_mode = "profit"
        self.tab_profit = TabButton(tabs_row, "Profit", lambda: self.set_view_mode("profit"))
        self.tab_xp = TabButton(tabs_row, "XP", lambda: self.set_view_mode("xp"))
        self.tab_profit.pack(side="left", padx=(0, 8))
        self.tab_xp.pack(side="left")
        self.tab_profit.set_active(True)

        chart_card = tk.Frame(main_col, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        chart_card.pack(fill="both", expand=True, padx=18, pady=(0, 18))

        self.fig, self.axes = plt.subplots(2, 1, figsize=(8, 6), sharex=True, facecolor=CARD)
        self.canvas = FigureCanvasTkAgg(self.fig, master=chart_card)
        self.canvas.get_tk_widget().configure(bg=CARD, highlightthickness=0)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=16, pady=16)

        self.xp_view = tk.Frame(chart_card, bg=CARD)

        self._show_placeholder("Select a match on the left — new ones appear here automatically")

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

    def _reset_tiles(self):
        self.tile_current.render("—", MUTED)
        self.tile_peak.render("—", MUTED)
        self.tile_net.render("—", MUTED)
        self.tile_rate.render("—", MUTED)

    # ---------- profit / XP tabs ----------
    def set_view_mode(self, mode):
        self.view_mode = mode
        self.tab_profit.set_active(mode == "profit")
        self.tab_xp.set_active(mode == "xp")
        if mode == "xp":
            self.canvas.get_tk_widget().pack_forget()
            self._render_xp_view()
            self.xp_view.pack(fill="both", expand=True, padx=16, pady=16)
        else:
            self.xp_view.pack_forget()
            self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=16, pady=16)

    def _render_xp_view(self):
        totals = rolexp.read_role_xp()

        if self.selected_session and self.selected_session == self.live_session and tracker.live_role_xp_before:
            baseline = tracker.live_role_xp_before
            gains = {r: totals.get(r, 0) - baseline.get(r, 0) for r in rolexp.ROLES}
            gains = {r: v for r, v in gains.items() if v}
        else:
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
    def refresh_sessions(self, select_latest=False):
        sessions = list_sessions()
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
        if self.selected_session and self.selected_session in self.cards:
            self.cards[self.selected_session].set_selected(False)
        self.selected_session = session_path
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

        # A reveal already in flight gets superseded — this render wins.
        self._reveal_token = object()

        if animate and len(df) > 2:
            self._play_reveal(df, csv_path, self._reveal_token)
        else:
            self._animating = False
            self._draw_frame(df, csv_path)

    def _draw_frame(self, df, csv_path):
        """Render one frame of the chart (either the final still image, or one
        step of the reveal animation) and update the stat tiles to match."""
        self._graph_df = df
        self._clear_fig_text()
        for ax in self.axes:
            ax.axis("on")
        plot_graph.draw(self.fig, self.axes, df, display_name(csv_path))
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
        reading to the last over ~1s, instead of just appearing fully drawn."""
        self._animating = True
        n = len(df)
        frame_count = min(28, n)
        cutoffs = sorted(set(int(round(v)) for v in np.linspace(2, n, frame_count)))
        if cutoffs[-1] != n:
            cutoffs.append(n)
        frame_delay = max(900 // len(cutoffs), 16)

        def step(i):
            if token is not self._reveal_token:
                return  # a newer selection/update came in — abandon this replay
            self._draw_frame(df.iloc[:cutoffs[i]], csv_path)
            if i + 1 < len(cutoffs):
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
        always runs, no manual start/stop. Retries on its own every poll if
        Steam isn't running yet (tracker.auto_track raises SystemExit,
        thread just exits, this notices and starts a fresh one)."""
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

        def run():
            try:
                tracker.auto_track(
                    2.0, stop_event=self.watch_stop_event,
                    on_session_start=on_session_start, on_update=on_update,
                    on_session_end=on_session_end,
                )
            except SystemExit:
                self.steam_ok = False  # _sync_watcher retries next poll regardless

        self.watch_thread = threading.Thread(target=run, daemon=True)
        self.watch_thread.start()
        self.steam_ok = True  # optimistic; run() corrects it within one poll if init fails

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
        elif not self.steam_ok:
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


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
