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


class DevView:
    def __init__(self, root):
        self.root = root
        root.title("Wardogs — Dev Match Viewer")
        root.geometry("1100x680")

        self.url, self.key = _load_secrets()
        self._players = []
        self._matches = []

        left = tk.Frame(root, width=280)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        tk.Label(left, text="PLAYERS", font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=10, pady=(10, 4))
        self.player_list = tk.Listbox(left, exportselection=False)
        self.player_list.pack(fill="both", expand=True, padx=10)
        self.player_list.bind("<<ListboxSelect>>", self._on_player_select)

        tk.Label(left, text="MATCHES", font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=10, pady=(16, 4))
        self.match_list = tk.Listbox(left, exportselection=False)
        self.match_list.pack(fill="both", expand=True, padx=10)
        self.match_list.bind("<<ListboxSelect>>", self._on_match_select)

        tk.Button(left, text="Refresh", command=self.load_players).pack(fill="x", padx=10, pady=10)

        right = tk.Frame(root)
        right.pack(side="left", fill="both", expand=True)
        self.fig, self.axes = plt.subplots(2, 1, figsize=(8, 6), sharex=True)
        self.canvas = FigureCanvasTkAgg(self.fig, master=right)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=10, pady=10)

        self.load_players()

    def load_players(self):
        rows = _get(self.url, self.key, "matches", params={"select": "steam_id,persona_name"})
        seen = {}
        for r in rows:
            seen.setdefault(r["steam_id"], r.get("persona_name") or r["steam_id"])
        self._players = sorted(seen.items(), key=lambda kv: kv[1].lower())
        self.player_list.delete(0, "end")
        for _steam_id, name in self._players:
            self.player_list.insert("end", name)

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
            started = (m.get("started_at") or "")[:16].replace("T", " ")
            profit = m.get("final_profit")
            profit_txt = f"${profit:,}" if profit is not None else "?"
            self.match_list.insert("end", f'{started}  {m.get("map") or "?"}  {profit_txt}')

    def _on_match_select(self, _event):
        sel = self.match_list.curselection()
        if not sel:
            return
        match = self._matches[sel[0]]
        ticks = _get(
            self.url, self.key, "match_ticks",
            params={"match_id": f'eq.{match["id"]}', "order": "timestamp.asc", "select": "*"},
        )
        if not ticks:
            return
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
