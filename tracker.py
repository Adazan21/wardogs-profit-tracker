"""
tracker.py — Wardogs match profit/loss tracker, via Steam Rich Presence.

Wardogs reports its own match state to Steam — the same mechanism that
shows "+$150 Profit" next to a friend's name in your Steam friends list.
This reads that data for yourself instead: `game_state` (menu vs.
`"playing"`) and `profit_loss` for the current match, polled every 2s.
No screen capture, no OCR, no calibration — see richpresence.py for how.

Each session CSV is also tagged with `map` and `faction`, captured once
per match (map from mapinfo.py, faction from Rich Presence) so match
history can eventually be broken down by where/who you played.

Role XP (Wardog/Infantry/Medic/Driver/Pilot/Support/Recon, from rolexp.py)
is watched every poll, independent of match start/end, and any gain gets
appended immediately as its own row to role_xp_log.csv (session_file blank
if it lands between matches). This used to snapshot once at match-start
and once at match-end instead, which missed gains on long matches: Wardogs
doesn't always finish rewriting the save file by the exact instant a match
starts or ends, so a snapshot taken right then could read the pre-levelup
value as if it were final. Polling continuously means whenever the game
actually writes the update — seconds or minutes later — the very next poll
catches it, instead of a single fragile snapshot that can miss it entirely.

Each row is also tagged with `life` — starts at 1, increments every time
a new "Player Spawned" breadcrumb (mapinfo.spawn_events()) shows up after
the match's own confirmed start time. The very first spawn always
corresponds to the initial insertion into the match itself (which is what
starts tracking in the first place), never a second life, so only spawns
strictly after that moment count.

Usage:
    python tracker.py                  # auto-detect match start/end, poll every 2s
    python tracker.py --interval 3     # poll every 3 seconds instead

Ctrl+C to stop. Then run plot_graph.py on the resulting CSV.
"""

import argparse
import csv
import os
import time
from datetime import datetime, timedelta, timezone

import mapinfo
import richpresence as rp_module
import rolexp

XP_LOG_PATH = "role_xp_log.csv"
LIFE_SPAWN_GRACE = timedelta(seconds=3)  # ignores the insertion spawn itself


def _append_xp_log(session_file, gains):
    """Appends one row of role-XP gains, tagged to whichever match was
    active when the change was actually detected (blank if it landed
    between matches — e.g. the save file finishing its write after you'd
    already returned to the menu)."""
    is_new = not os.path.exists(XP_LOG_PATH)
    with open(XP_LOG_PATH, "a", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["timestamp", "session_file"] + rolexp.ROLES)
        writer.writerow(
            [datetime.now().isoformat(timespec="seconds"), session_file or ""]
            + [gains.get(r, 0) for r in rolexp.ROLES]
        )
        print(f"Role XP gained: {gains}")


def auto_track(interval=2.0, stop_event=None, on_session_start=None, on_update=None,
                on_session_end=None, on_identity=None, on_state=None,
                confirm_reads=2, miss_threshold=5):
    """Watch Rich Presence and auto-detect match start/end.

    Only opens a Steamworks session — under Wardogs' own App ID, the only
    way to read its Rich Presence at all — while the real Wardogs process
    is actually running, confirmed independently via mapinfo.process_status()
    (the OS process list), not inferred from Steam itself. Merely holding
    that session open is what makes Steam report "Wardogs is running" to
    friends, regardless of whether the real game is — so doing it
    unconditionally (the old behavior) meant just having this tracker open
    showed that status the whole time, even between matches or before
    Wardogs had ever been launched. The session is torn down the instant
    the real process disappears, including a crash or force-quit, not just
    on a clean exit — which also means a match that ends because Wardogs
    itself died now gets closed out properly instead of possibly sitting on
    a stale "playing" reading forever.

    `on_state(state)` fires whenever the coarse status changes, `state` one
    of `"no_steam"` (Steam itself isn't running), `"waiting"` (Steam's up,
    Wardogs isn't), or `"watching"` (actively reading Rich Presence) — for
    a caller's status display. `on_identity(steam_id, persona_name)` fires
    once, the first time a session is actually opened, from this same
    thread — the only thread that ever touches the Steamworks DLL. Callers
    needing that identity elsewhere (e.g. cloud sync) should hand the plain
    strings off to their own thread from inside that callback rather than
    opening a second concurrent Steamworks session, which Steamworks
    doesn't support safely.

    A match "starts" once `game_state` reports `"playing"` for
    `confirm_reads` consecutive polls, and "ends" once it doesn't for
    `miss_threshold` consecutive polls. Real data showed `game_state` can
    flicker away from "playing" for a poll or two during genuinely
    continuous play (a Steamworks callback timing quirk, not a real menu
    visit) — with zero tolerance this silently fragmented single matches
    into several session files, which is worse than it sounds: cut
    profit/loss curves, wrong per-match totals. `miss_threshold` defaults
    to 5 (~10s), long enough to ride out that flicker but still end
    promptly on an actual match exit.

    Runs until `stop_event` is set (or forever if omitted) — unlike before,
    never raises SystemExit; a Steam/Wardogs outage just means it keeps
    waiting and retrying on its own. Callbacks fire with just plain data
    (filenames/numbers), never touch UI directly, so this is safe to drive
    from a background thread under a GUI.
    """
    rp = None
    identity_sent = False
    last_state = None

    def set_state(new_state):
        nonlocal last_state
        if new_state != last_state:
            last_state = new_state
            if on_state:
                on_state(new_state)

    in_match = False
    filename = None
    f = None
    writer = None
    start_time = None
    last_value = None
    playing_streak = 0
    miss_streak = 0
    session_map = None
    session_faction = None
    match_start_dt = None
    life_number = 1
    known_spawn_dts = set()
    known_xp = rolexp.read_role_xp()  # baseline — a bad/empty read here just means we start watching from whatever the next good read is

    def end_match(reason):
        nonlocal f, writer, filename, in_match, playing_streak
        f.close()
        ended_file = filename
        print(f"Match ended ({reason}). Session saved to {ended_file}")
        f, writer, filename = None, None, None
        in_match = False
        playing_streak = 0
        if on_session_end:
            on_session_end(ended_file)

    try:
        while stop_event is None or not stop_event.is_set():
            steam_up, wardogs_up = mapinfo.process_status()

            if not wardogs_up:
                if rp is not None:
                    if in_match:
                        end_match("Wardogs process exited")
                    rp.shutdown()
                    rp = None
                set_state("waiting" if steam_up else "no_steam")
                (stop_event.wait(interval) if stop_event is not None else time.sleep(interval))
                continue

            if rp is None:
                rp = rp_module.RichPresence()
                if not rp.init():
                    rp = None
                    set_state("no_steam")
                    (stop_event.wait(interval) if stop_event is not None else time.sleep(interval))
                    continue
                print(f"Wardogs is running — watching Rich Presence (AppID {rp.app_id}). Polling every {interval}s.")
                if on_identity and not identity_sent:
                    on_identity(str(rp.steam_id), rp.persona_name())
                    identity_sent = True

            set_state("watching")
            data = rp.read()
            state = data.get("game_state")

            # Independent of match state — see the module docstring for why
            # this isn't a start/end snapshot.
            current_xp = rolexp.read_role_xp()
            if current_xp:
                gains = {r: current_xp.get(r, 0) - known_xp.get(r, 0) for r in rolexp.ROLES}
                gains = {r: v for r, v in gains.items() if v > 0}
                if gains:
                    _append_xp_log(filename if in_match else None, gains)
                known_xp = current_xp

            if not in_match:
                playing_streak = playing_streak + 1 if state == "playing" else 0
                if playing_streak >= confirm_reads:
                    filename = f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
                    f = open(filename, "a", newline="")
                    writer = csv.writer(f)
                    writer.writerow(["timestamp", "seconds_elapsed", "cash", "map", "faction", "life"])
                    start_time = time.time()
                    last_value = None
                    in_match = True
                    miss_streak = 0
                    session_map = None
                    session_faction = None
                    match_start_dt = datetime.now(timezone.utc)
                    life_number = 1
                    known_spawn_dts = set()
                    print(f"\nMatch detected. Logging to {filename}")
                    if on_session_start:
                        on_session_start(filename)

            if in_match:
                if state == "playing":
                    miss_streak = 0
                    profit = rp_module.parse_profit(data.get("profit_loss"))
                    elapsed = round(time.time() - start_time, 1)
                    if session_map is None:
                        session_map = mapinfo.current_map()
                    if session_faction is None:
                        session_faction = data.get("faction")
                    for dt in mapinfo.spawn_events():
                        if dt not in known_spawn_dts and dt > match_start_dt + LIFE_SPAWN_GRACE:
                            known_spawn_dts.add(dt)
                            life_number += 1
                            print(f"New life detected -> life {life_number}")
                    if profit is not None and profit != last_value:
                        ts = datetime.now().isoformat(timespec="seconds")
                        writer.writerow([ts, elapsed, profit, session_map or "", session_faction or "", life_number])
                        f.flush()
                        print(f"[{elapsed:>7.1f}s] profit_loss = {profit}")
                        last_value = profit
                        if on_update:
                            on_update(filename, elapsed, profit)
                else:
                    miss_streak += 1
                    if miss_streak >= miss_threshold:
                        end_match(f"game_state={state!r}")

            (stop_event.wait(interval) if stop_event is not None else time.sleep(interval))
    except KeyboardInterrupt:
        pass
    finally:
        if f:
            f.close()
            print(f"\nStopped mid-match. Session saved to {filename}")
        if rp is not None:
            rp.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=2.0, help="Seconds between polls")
    args = parser.parse_args()
    auto_track(args.interval)
