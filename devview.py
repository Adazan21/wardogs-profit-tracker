"""
devview.py — Private dashboard: every player's matches, uploaded by the
opt-in sync in cloudsync.py (see supabase_schema.sql for the backend and
app.py's ConsentDialog for what players are told before any of it runs).

Uses the Supabase *service_role* key, which bypasses Row-Level Security
entirely — this is the one place in the whole project allowed to read other
players' data. That key must never be embedded in the distributed app or
committed to git.

One-time setup:
    copy devview_secrets.json.example -> devview_secrets.json
    fill in "url" and "service_role_key" from Project Settings -> API

Usage:
    python devview.py
"""

import json
import os
import tkinter as tk

import pandas as pd
import requests

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

import plot_graph

# Same palette as app.py — duplicated rather than imported so this stays a
# lightweight standalone script (importing app.py would drag in tracker.py/
# richpresence.py/cloudsync.py/updater.py, none of which this needs).
APP_BG = "#080a0f"
CARD = plot_graph.BG
CARD_ALT = "#11161d"
BORDER = "#21262d"
TEXT = plot_graph.TEXT
MUTED = plot_graph.MUTED
ACCENT = plot_graph.CASH_LINE
FONT = "Segoe UI"

SECRETS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "devview_secrets.json")


def _load_secrets():
    if not os.path.exists(SECRETS_PATH):
        raise SystemExit(
            f"Missing {SECRETS_PATH}.\n"
            "Copy devview_secrets.json.example to devview_secrets.json and fill in "
            "your Supabase project's url + service_role key (Project Settings -> API)."
        )
    with open(SECRETS_PATH) as f:
        data = json.load(f)
    return data["url"], data["service_role_key"]


def _get(url, key, path, params=None):
    resp = requests.get(
        f"{url}/rest/v1/{path}",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        params=params, timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _match_label(m, with_player=False):
    started = (m.get("started_at") or "")[:16].replace("T", " ")
    profit = m.get("final_profit")
    profit_txt = f"${profit:,}" if profit is not None else "?"
    prefix = f'{m.get("persona_name") or m["steam_id"]}  ' if with_player else ""
    return f'{prefix}{started}  {m.get("map") or "?"}  {profit_txt}'


def _section_label(parent, text):
    return tk.Label(parent, text=text, bg=parent["bg"], fg=MUTED, font=(FONT, 8, "bold"))


def _styled_listbox(parent):
    return tk.Listbox(
        parent, bg=CARD_ALT, fg=TEXT, selectbackground="#132530", selectforeground=ACCENT,
        highlightthickness=0, bd=0, relief="flat", font=(FONT, 9), activestyle="none",
    )


class TabButton(tk.Button):
    def __init__(self, parent, text, command):
        super().__init__(
            parent, text=text, command=command, font=(FONT, 8, "bold"),
            relief="flat", bd=0, cursor="hand2", padx=10, pady=6,
        )
        self.set_active(False)

    def set_active(self, active):
        if active:
            self.configure(bg="#132530", fg=ACCENT, activebackground="#132530", activeforeground=ACCENT)
        else:
            self.configure(bg=CARD_ALT, fg=MUTED, activebackground=CARD_ALT, activeforeground=TEXT)


class DevView:
    def __init__(self, root):
        self.root = root
        root.title("Wardogs — Dev Match Viewer")
        root.geometry("1100x680")
        root.configure(bg=APP_BG)
        root.minsize(820, 520)

        self.url, self.key = _load_secrets()
        self._players = []
        self._matches = []
        self._all_matches = []

        left = tk.Frame(root, width=300, bg=CARD)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        self.stats_label = tk.Label(left, text="", bg=CARD, fg=MUTED, font=(FONT, 9, "bold"))
        self.stats_label.pack(anchor="w", padx=14, pady=(16, 8))

        tabs = tk.Frame(left, bg=CARD)
        tabs.pack(fill="x", padx=14, pady=(0, 10))
        self.tab_player_btn = TabButton(tabs, "By Player", lambda: self.set_tab("player"))
        self.tab_all_btn = TabButton(tabs, "All Matches", lambda: self.set_tab("all"))
        self.tab_player_btn.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self.tab_all_btn.pack(side="left", expand=True, fill="x")

        # ----- "By Player" tab: players list -> that player's matches -----
        self.player_tab = tk.Frame(left, bg=CARD)
        _section_label(self.player_tab, "PLAYERS").pack(anchor="w", padx=14, pady=(0, 4))
        self.player_list = _styled_listbox(self.player_tab)
        self.player_list.pack(fill="both", expand=True, padx=14)
        self.player_list.bind("<<ListboxSelect>>", self._on_player_select)

        _section_label(self.player_tab, "MATCHES").pack(anchor="w", padx=14, pady=(16, 4))
        self.match_list = _styled_listbox(self.player_tab)
        self.match_list.pack(fill="both", expand=True, padx=14)
        self.match_list.bind("<<ListboxSelect>>", self._on_match_select)

        # ----- "All Matches" tab: every match, every player, most recent first -----
        self.all_tab = tk.Frame(left, bg=CARD)
        _section_label(self.all_tab, "ALL MATCHES · MOST RECENT FIRST").pack(anchor="w", padx=14, pady=(0, 4))
        self.all_match_list = _styled_listbox(self.all_tab)
        self.all_match_list.pack(fill="both", expand=True, padx=14)
        self.all_match_list.bind("<<ListboxSelect>>", self._on_all_match_select)

        tk.Button(
            left, text="Refresh", command=self.refresh, bg=CARD_ALT, fg=MUTED,
            activebackground=CARD_ALT, activeforeground=TEXT, relief="flat", bd=0,
            cursor="hand2", font=(FONT, 8, "bold"), pady=8,
        ).pack(fill="x", padx=14, pady=14)

        right = tk.Frame(root, bg=APP_BG)
        right.pack(side="left", fill="both", expand=True)
        self.fig, self.axes = plt.subplots(2, 1, figsize=(8, 6), sharex=True, facecolor=CARD)
        self.canvas = FigureCanvasTkAgg(self.fig, master=right)
        self.canvas.get_tk_widget().configure(bg=CARD, highlightthickness=0)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=14, pady=14)

        self._show_placeholder("Pick a match on the left")
        self.set_tab("all")
        self.refresh()

    def _show_placeholder(self, message):
        for ax in self.axes:
            ax.clear()
            ax.set_facecolor(CARD)
            ax.axis("off")
        self.fig.text(0.5, 0.5, message, ha="center", va="center", color=MUTED, fontsize=12)
        self.canvas.draw_idle()

    def set_tab(self, tab):
        self.player_tab.pack_forget()
        self.all_tab.pack_forget()
        is_player = tab == "player"
        self.tab_player_btn.set_active(is_player)
        self.tab_all_btn.set_active(not is_player)
        (self.player_tab if is_player else self.all_tab).pack(fill="both", expand=True)

    def refresh(self):
        self.load_players()
        self.load_all_matches()
        player_word = "player" if len(self._players) == 1 else "players"
        match_word = "match" if len(self._all_matches) == 1 else "matches"
        self.stats_label.configure(
            text=f"{len(self._players)} {player_word}  ·  {len(self._all_matches)} {match_word} logged"
        )

    def load_players(self):
        rows = _get(self.url, self.key, "matches", params={"select": "steam_id,persona_name"})
        seen = {}
        for r in rows:
            seen.setdefault(r["steam_id"], r.get("persona_name") or r["steam_id"])
        self._players = sorted(seen.items(), key=lambda kv: kv[1].lower())
        self.player_list.delete(0, "end")
        for _steam_id, name in self._players:
            self.player_list.insert("end", name)

    def load_all_matches(self):
        self._all_matches = _get(
            self.url, self.key, "matches",
            params={"order": "started_at.desc", "select": "*"},
        )
        self.all_match_list.delete(0, "end")
        for m in self._all_matches:
            self.all_match_list.insert("end", _match_label(m, with_player=True))
        if not self._all_matches:
            self._show_placeholder("No matches uploaded yet")

    def _on_player_select(self, _event):
        sel = self.player_list.curselection()
        if not sel:
            return
        steam_id, _name = self._players[sel[0]]
        self._matches = _get(
            self.url, self.key, "matches",
            params={"steam_id": f"eq.{steam_id}", "order": "started_at.desc", "select": "*"},
        )
        self.match_list.delete(0, "end")
        for m in self._matches:
            self.match_list.insert("end", _match_label(m))

    def _on_match_select(self, _event):
        sel = self.match_list.curselection()
        if sel:
            self._show_match(self._matches[sel[0]])

    def _on_all_match_select(self, _event):
        sel = self.all_match_list.curselection()
        if sel:
            self._show_match(self._all_matches[sel[0]])

    def _show_match(self, match):
        ticks = _get(
            self.url, self.key, "match_ticks",
            params={"match_id": f'eq.{match["id"]}', "order": "timestamp.asc", "select": "*"},
        )
        if not ticks:
            return
        for ax in self.axes:
            ax.axis("on")
        df = plot_graph.load_df(pd.DataFrame(ticks))
        label = f'{match.get("persona_name") or match["steam_id"]} — {(match.get("started_at") or "")[:16]}'
        plot_graph.draw(self.fig, self.axes, df, label)
        self.canvas.draw_idle()


def main():
    root = tk.Tk()
    DevView(root)
    root.mainloop()


if __name__ == "__main__":
    main()
