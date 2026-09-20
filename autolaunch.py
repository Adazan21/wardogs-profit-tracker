"""
autolaunch.py — Launch profitdog.exe automatically when Windows starts, and
make sure two copies never end up running at once regardless of how either
one was started (the Windows Run registry key it uses to auto-start, or a
plain double-click racing it).

The "run at login" toggle only does anything for the frozen exe
(`sys.frozen`) — there's no single stable command to hand Windows for
"python app.py" that would keep working once the source moves or the
interpreter changes, and running from source is a dev workflow anyway.
"""

import ctypes
import json
import os
import sys
import winreg

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "WardogsProfitTracker"

MUTEX_NAME = "Global\\WardogsProfitTrackerSingleInstance"
WINDOW_TITLE = "Wardogs Profit Tracker"
ERROR_ALREADY_EXISTS = 183
SW_RESTORE = 9

_mutex_handle = None  # kept alive for the process's lifetime; Windows frees it on exit


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


# ---------------------------------------------------------- run-at-login --
def supported():
    return getattr(sys, "frozen", False)


def is_enabled():
    if not supported():
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH) as key:
            value, _ = winreg.QueryValueEx(key, RUN_VALUE_NAME)
        return value == f'"{sys.executable}"'
    except OSError:
        return False


def set_enabled(enabled):
    """Also records that this preference has now been explicitly set, so
    apply_default_if_unset() won't later override a deliberate 'off'."""
    if not supported():
        return
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, f'"{sys.executable}"')
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE_NAME)
            except FileNotFoundError:
                pass
    _save_state({"user_configured": True})


def apply_default_if_unset():
    """Turns run-at-login on the first time this ever runs. Never overrides
    a preference the user (or a previous call to this) already set —
    including turning it back off — since the registry alone can't tell
    "never configured" apart from "explicitly disabled" (both just mean the
    value is absent), hence the separate local marker."""
    if not supported():
        return
    if _load_state().get("user_configured"):
        return
    set_enabled(True)


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
