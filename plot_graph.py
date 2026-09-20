"""
plot_graph.py — Graph a Wardogs cash-tracking session.

Usage:
    python plot_graph.py session_20260915_201530.csv
    python plot_graph.py session_20260915_201530.csv --live   # auto-refresh while tracker.py runs

Shows:
  - Cash over time (raw), with a shaded fill and peak marker
  - Rolling average earn rate ($/min), shaded green when profitable / red when not
"""

import argparse
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.animation as animation
import matplotlib.ticker as mticker

import mapinfo
import richpresence

BG = "#0d1117"
PANEL = "#0d1117"
GRID = "#30363d"
TEXT = "#c9d1d9"
MUTED = "#8b949e"
CASH_LINE = "#2dd4bf"
CASH_FILL = "#2dd4bf"
RATE_POS = "#3fb950"
RATE_NEG = "#f85149"

plt.rcParams.update({
    "figure.facecolor": BG,
    "axes.facecolor": PANEL,
    "axes.edgecolor": GRID,
    "axes.labelcolor": TEXT,
    "text.color": TEXT,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "font.size": 10.5,
    "font.family": "sans-serif",
})


def load_df(df):
    """Adds the derived columns draw() needs (minutes_elapsed, rate_per_min)
    to an already-loaded DataFrame with at least seconds_elapsed/cash — split
    out from load() so devview.py can plot rows pulled from Supabase instead
    of a local session CSV, through the exact same drawing code."""
    if df.empty:
        return df
    df["minutes_elapsed"] = df["seconds_elapsed"] / 60
    # Earn rate: change in cash per change in time, smoothed
    df["cash_delta"] = df["cash"].diff()
    df["time_delta_min"] = df["minutes_elapsed"].diff()
    df["rate_per_min"] = (df["cash_delta"] / df["time_delta_min"]).rolling(3, min_periods=1).mean()
    return df


def load(csv_path):
    return load_df(pd.read_csv(csv_path))


def _map_faction_tag(df):
    """'Kavkazi · alpha' from a loaded df's map/faction columns — absent
    entirely on older CSVs without them, or blank cells (map read failed,
    Rich Presence hadn't reported faction yet, etc.)."""
    parts = []
    if "map" in df.columns:
        m = df["map"].iloc[0]
        if isinstance(m, str) and m:
            parts.append(mapinfo.display_map_name(m))
    if "faction" in df.columns:
        fac = df["faction"].iloc[0]
        if isinstance(fac, str) and fac:
            parts.append(richpresence.faction_name(fac))
    return " · ".join(parts)


def _fmt_money(v, suffix=""):
    """'$1,234' / '-$1,234' — sign-aware, since profit_loss (Rich Presence
    tracking) can be negative unlike the old OCR-based absolute cash total."""
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.0f}{suffix}"


def _style_axis(ax):
    ax.grid(color=GRID, alpha=0.6, linewidth=0.7, linestyle="-")
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(GRID)


def draw(fig, axes, df, csv_path, quick=False, full_df=None):
    """Draws the chart for `df` (which may be a prefix of the full match, for
    the reveal animation / scrub slider).

    `quick=True` skips the expensive parts that don't need to happen every
    animation frame — axis chrome (grid/spines/labels/title/formatters),
    layout, and hover rebinding — and only touches the data-dependent
    artists (line, fill, peak marker, life markers), which are tracked on
    `fig._data_artists` and swapped out in place instead of a full
    `ax.clear()`. This is what makes scrubbing/replaying feel smooth instead
    of janky: no per-frame layout recompute, and no axis rescaling (see
    `full_df` below).

    `full_df`, when given, is the complete match — used to compute fixed
    axis limits so a mid-reveal/scrub frame showing only a prefix of the
    data doesn't make the whole plot rescale and "jump" every frame. Falls
    back to `df` itself (i.e. normal autoscaling) when omitted.
    """
    ax1, ax2 = axes
    range_df = full_df if full_df is not None else df

    if not quick:
        ax1.clear()
        ax2.clear()
        _style_axis(ax1)
        _style_axis(ax2)
        ax1.set_ylabel("Cash")
        ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: _fmt_money(v)))
        ax2.set_xlabel("Minutes into match")
        ax2.set_ylabel("Earn rate")
        ax2.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: _fmt_money(v, "/min")))
    else:
        for artist in getattr(fig, "_data_artists", []):
            try:
                artist.remove()
            except Exception:
                pass
    fig._data_artists = []

    x = df["minutes_elapsed"]
    cash = df["cash"]
    rate = df["rate_per_min"]
    full_x = range_df["minutes_elapsed"]
    full_cash = range_df["cash"]
    full_rate = range_df["rate_per_min"]
    baseline = full_cash.min()

    # --- Top panel: cash over time ---
    (line1,) = ax1.plot(x, cash, color=CASH_LINE, linewidth=2.2, solid_joinstyle="round")
    fill1 = ax1.fill_between(x, cash, baseline, color=CASH_FILL, alpha=0.12)
    fig._data_artists += [line1, fill1]

    peak_idx = cash.idxmax()
    peak_dot = ax1.scatter([x[peak_idx]], [cash[peak_idx]], color="#ffd60a", zorder=5, s=35, edgecolor=BG, linewidth=1)
    peak_ann = ax1.annotate(
        f"peak {_fmt_money(cash[peak_idx])}",
        xy=(x[peak_idx], cash[peak_idx]),
        xytext=(0, 10),
        textcoords="offset points",
        ha="center",
        color="#ffd60a",
        fontsize=9,
        fontweight="bold",
    )
    fig._data_artists += [peak_dot, peak_ann]

    main_title = f"Wardogs cash tracker — {csv_path}"
    tag = _map_faction_tag(range_df)
    if tag:
        main_title += f"  ·  {tag}"

    # The stats readout used to be text overlaid directly on the plot,
    # which ended up sitting on top of the line, a life-marker label, or
    # the peak annotation often enough to matter (cash starts at 0 and
    # commonly ends up near either edge of its own range). A second title
    # row fixes that — but as a *second line of this same axes title*
    # (loc="right" alongside the main title collided for anything but a
    # short filename, and a separate fig.suptitle() turned out to share
    # fig.texts with app.py's placeholder-message cleanup, which wiped it
    # every redraw and broke hover). One Title artist, two lines, is the
    # option that's actually safe here — still always in the margin above
    # the axes, so it can't cover the data either way.
    current = cash.iloc[-1]
    net = cash.iloc[-1] - cash.iloc[0]
    minutes = max(x.iloc[-1], 0.01)
    avg_rate = net / minutes
    stats_line = (
        f"{_fmt_money(current)}   net {'+' if net >= 0 else ''}{net:,.0f}   "
        f"{'+' if avg_rate >= 0 else ''}{avg_rate:,.0f}/min"
    )
    ax1.set_title(f"{main_title}\n{stats_line}", color=TEXT, fontsize=12, fontweight="bold", pad=14)

    # --- Bottom panel: earn rate ---
    (line2,) = ax2.plot(x, rate, color=TEXT, linewidth=1, alpha=0.5)
    fill_pos = ax2.fill_between(x, rate, 0, where=(rate >= 0), color=RATE_POS, alpha=0.35, interpolate=True)
    fill_neg = ax2.fill_between(x, rate, 0, where=(rate < 0), color=RATE_NEG, alpha=0.35, interpolate=True)
    fig._data_artists += [line2, fill_pos, fill_neg]

    if not quick:
        # y=0 never moves, and ax2 isn't cleared in quick mode either, so
        # this only needs to exist once per full draw, not every frame.
        ax2.axhline(0, color=MUTED, linewidth=0.8)

    fig._data_artists += _draw_life_markers(ax1, ax2, df)

    # Fixed axis limits sized to the FULL match, not just what's currently
    # revealed — otherwise the plot silently rescales every single frame,
    # which reads as jittery/unstable even when each frame renders fine.
    pad_x = max(full_x.max() * 0.02, 0.5)
    ax1.set_xlim(full_x.min() - pad_x, full_x.max() + pad_x)
    ymin, ymax = full_cash.min(), full_cash.max()
    pad_y = max((ymax - ymin) * 0.08, 50)
    ax1.set_ylim(ymin - pad_y, ymax + pad_y)
    rmin, rmax = full_rate.min(skipna=True), full_rate.max(skipna=True)
    if pd.notna(rmin) and pd.notna(rmax):
        pad_r = max((rmax - rmin) * 0.1, 10)
        ax2.set_ylim(rmin - pad_r, rmax + pad_r)

    if not quick:
        fig.tight_layout()
        _attach_hover(fig, ax1, ax2, range_df)


def _draw_life_markers(ax1, ax2, df):
    """Vertical marker + label at each point `life` increments — i.e. every
    respawn into a new life mid-match (see tracker.py/mapinfo.py). Absent
    entirely on older CSVs without a `life` column. Returns the artists it
    created so the caller can track/remove them on the next quick frame."""
    artists = []
    if "life" not in df.columns:
        return artists
    life = df["life"].fillna(1)
    x = df["minutes_elapsed"]
    for idx in df.index[life.diff().fillna(0) > 0]:
        xi = x[idx]
        v1 = ax1.axvline(xi, color=MUTED, linewidth=1, linestyle=":", alpha=0.7, zorder=3)
        v2 = ax2.axvline(xi, color=MUTED, linewidth=1, linestyle=":", alpha=0.7, zorder=3)
        ann = ax1.annotate(
            f"Life {int(life[idx])}",
            xy=(xi, 1), xycoords=("data", "axes fraction"),
            xytext=(4, -4), textcoords="offset points",
            ha="left", va="top", color=MUTED, fontsize=8, fontweight="bold", zorder=6,
        )
        artists += [v1, v2, ann]
    return artists


def _attach_hover(fig, ax1, ax2, df):
    """Crosshair + tooltip that tracks the mouse and shows the cash/rate at that point in time."""
    # draw() re-clears the axes on every call (live mode, selection changes, etc.), which
    # deletes the artists below but NOT the canvas event bindings — so any hover callbacks
    # from a previous draw() must be disconnected here, or they pile up and reference
    # artists that no longer exist.
    for cid in getattr(fig, "_hover_cids", []):
        fig.canvas.mpl_disconnect(cid)

    x = df["minutes_elapsed"].to_numpy()
    cash = df["cash"].to_numpy()
    rate = df["rate_per_min"].to_numpy()

    vline1 = ax1.axvline(x[0], color=MUTED, linewidth=0.8, linestyle="--", alpha=0, zorder=4)
    vline2 = ax2.axvline(x[0], color=MUTED, linewidth=0.8, linestyle="--", alpha=0, zorder=4)
    dot1 = ax1.scatter([x[0]], [cash[0]], s=45, color=CASH_LINE, edgecolor=BG, linewidth=1.2, zorder=6, alpha=0)
    dot2 = ax2.scatter([x[0]], [0], s=40, color=TEXT, edgecolor=BG, linewidth=1.2, zorder=6, alpha=0)

    # Two tooltips, one per axes — an artist parented to a hidden axes never
    # renders regardless of its own visibility flag, which only ever
    # mattered once app.py started letting either panel be shown on its own
    # (the Profit/$-per-min tabs). Picking by ax1.get_visible() rather than
    # always using tooltip1 keeps this identical to the old single-tooltip
    # behavior for every existing caller (devview.py, standalone use, and
    # app.py's default dual-panel view all always have ax1 visible), and
    # only switches to ax2's copy in the one new case where ax1 is hidden.
    tooltip_kwargs = dict(
        xytext=(12, 12), textcoords="offset points",
        bbox=dict(boxstyle="round,pad=0.4", facecolor="#161b22", edgecolor=GRID),
        color=TEXT, fontsize=9, family="monospace", zorder=7,
    )
    tooltip1 = ax1.annotate("", xy=(0, 0), **tooltip_kwargs)
    tooltip2 = ax2.annotate("", xy=(0, 0), **tooltip_kwargs)
    tooltip1.set_visible(False)
    tooltip2.set_visible(False)

    def hide():
        vline1.set_alpha(0)
        vline2.set_alpha(0)
        dot1.set_alpha(0)
        dot2.set_alpha(0)
        tooltip1.set_visible(False)
        tooltip2.set_visible(False)
        fig.canvas.draw_idle()

    def on_move(event):
        if event.inaxes not in (ax1, ax2) or event.xdata is None:
            if tooltip1.get_visible() or tooltip2.get_visible():
                hide()
            return
        idx = int(np.argmin(np.abs(x - event.xdata)))
        vline1.set_xdata([x[idx], x[idx]])
        vline2.set_xdata([x[idx], x[idx]])
        vline1.set_alpha(0.6)
        vline2.set_alpha(0.6)
        dot1.set_offsets([[x[idx], cash[idx]]])
        dot1.set_alpha(1)

        r = rate[idx]
        if np.isnan(r):
            rate_txt = "--"
        else:
            dot2.set_offsets([[x[idx], r]])
            dot2.set_alpha(1)
            rate_txt = f"{r:+,.0f}/min"

        text = f"t = {x[idx]:.1f}m\ncash  {_fmt_money(cash[idx])}\nrate  {rate_txt}"
        active, other = (tooltip1, tooltip2) if ax1.get_visible() else (tooltip2, tooltip1)
        active.xy = (x[idx], cash[idx]) if active is tooltip1 else (x[idx], 0 if np.isnan(r) else r)
        active.set_text(text)
        active.set_visible(True)
        other.set_visible(False)
        fig.canvas.draw_idle()

    fig._hover_cids = [
        fig.canvas.mpl_connect("motion_notify_event", on_move),
        fig.canvas.mpl_connect("figure_leave_event", lambda _e: hide()),
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path")
    parser.add_argument("--live", action="store_true", help="Auto-refresh every 3s (use while tracker.py is running)")
    args = parser.parse_args()

    fig, axes = plt.subplots(2, 1, figsize=(10, 6.5), sharex=True, facecolor=BG)

    if args.live:
        def update(frame):
            df = load(args.csv_path)
            if not df.empty:
                draw(fig, axes, df, args.csv_path)
        ani = animation.FuncAnimation(fig, update, interval=3000)
        plt.show()
    else:
        df = load(args.csv_path)
        if df.empty:
            print("No data yet in this CSV.")
            return
        draw(fig, axes, df, args.csv_path)
        plt.show()


if __name__ == "__main__":
    main()
