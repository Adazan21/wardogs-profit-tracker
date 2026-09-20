"""
autolaunch.py — Launch profitdog.exe automatically the moment Wardogs
itself starts, and make sure two copies of the main app never end up
running at once regardless of how either one was started (the watcher
below, or a plain double-click racing it).

Windows has no built-in "run this when that other program starts" trigger
that doesn't need admin rights (Task Scheduler's process-start trigger
needs Audit Process Creation enabled via Group Policy), so this instead
registers a tiny headless *watcher* to run at login (the Windows Run
registry key) — `run_watcher()`, entered via the frozen exe's own `--watch`
flag rather than a separate binary. It opens no window, just polls for the
real Wardogs process and launches the normal GUI the moment it appears,
then keeps watching so it can do this again next time Wardogs starts too
(not just once per Windows session).

The toggle only does anything for the frozen exe (`sys.frozen`) — there's
no single stable command to hand Windows for "python app.py" that would
keep working once the source moves or the interpreter changes, and running
from source is a dev workflow anyway.
"""

import ctypes
import json
import os
import subprocess
import sys
import time
import winreg

import mapinfo

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "WardogsProfitTracker"

MUTEX_NAME = "Global\\WardogsProfitTrackerSingleInstance"
WATCHER_MUTEX_NAME = "Global\\WardogsProfitTrackerWatcherSingleInstance"
WINDOW_TITLE = "Wardogs Profit Tracker"
ERROR_ALREADY_EXISTS = 183
SW_RESTORE = 9
WATCH_POLL_SECONDS = 5

_mutex_handle = None  # kept alive for the process's lifetime; Windows frees it on exit
_watcher_mutex_handle = None


def _appdata_dir():
    base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
    path = os.path.join(base, "WardogsProfitTracker")
    os.makedirs(path, exist_ok=True)
    return path


STATE_PATH = os.path.join(_appdata_dir(), "autolaunch_state.json")


def _load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)


# ------------------------------------------------------------- watch-mode --
def supported():
    return getattr(sys, "frozen", False)


def _watch_command():
    return f'"{sys.executable}" --watch'


def is_enabled():
    if not supported():
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH) as key:
            value, _ = winreg.QueryValueEx(key, RUN_VALUE_NAME)
        return value == _watch_command()
    except OSError:
        return False


def set_enabled(enabled):
    """Also records that this preference has now been explicitly set, so
    apply_default_if_unset() won't later override a deliberate 'off' — this
    is the toggle a Settings checkbox calls either way."""
    if not supported():
        return
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, _watch_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE_NAME)
            except FileNotFoundError:
                pass
    _save_state({"user_configured": True})


def apply_default_if_unset():
    """Turns the watcher on the first time this ever runs. Never overrides
    a preference the user (or a previous call to this) already set —
    including turning it back off — since the registry alone can't tell
    "never configured" apart from "explicitly disabled" (both just mean the
    value is absent), hence the separate local marker."""
    if not supported():
        return
    if _load_state().get("user_configured"):
        return
    set_enabled(True)


def _relaunch_gui_command():
    """The command that opens the normal GUI (never with --watch) — used
    both by the real watcher and by manual `python app.py --watch` testing
    from source, so that case relaunches the actual dev script rather than
    a bare interpreter with no arguments."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, os.path.abspath(sys.argv[0])]


def run_watcher():
    """Headless loop (no window) — waits for the real Wardogs process to
    start, then launches the full profitdog GUI. Keeps running afterward
    rather than exiting, so this also catches every later Wardogs launch in
    the same Windows session, not just the first. Relies entirely on the
    GUI's own single-instance guard (acquire_single_instance_lock) to avoid
    opening a duplicate if it's already running — this doesn't duplicate
    that check itself."""
    global _watcher_mutex_handle
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, WATCHER_MUTEX_NAME)
    if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        return  # a watcher is already running in this session — nothing to do
    _watcher_mutex_handle = handle

    was_running = False
    while True:
        _, wardogs_up = mapinfo.process_status()
        if wardogs_up and not was_running:
            subprocess.Popen(_relaunch_gui_command())
        was_running = wardogs_up
        time.sleep(WATCH_POLL_SECONDS)


# ------------------------------------------------------- single instance --
def acquire_single_instance_lock():
    """True if this is the only running copy (and holds the lock for the
    rest of the process's life). False if another copy is already
    running — after trying to bring its window to the front, so a
    duplicate launch (auto-start racing a manual open, a double-click,
    etc.) does something visible instead of silently no-op'ing."""
    global _mutex_handle
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    already_running = ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS
    if already_running:
        hwnd = ctypes.windll.user32.FindWindowW(None, WINDOW_TITLE)
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE)
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        return False
    _mutex_handle = handle
    return True
