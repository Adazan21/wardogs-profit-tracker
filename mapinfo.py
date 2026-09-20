"""
mapinfo.py — Best-effort current map name for Wardogs, read from the Sentry
crash-reporter's breadcrumb log inside the game's own install folder.

Wardogs' crash reporter (sentry-native) keeps a small rolling breadcrumb
buffer on disk while the game runs, in case it needs to attach recent
context to a crash report later. Those breadcrumbs happen to include an
Unreal "PostLoadMapWithWorld" event every time a map finishes loading — the
same map that's active during actual play. Reading it is on the same
footing as richpresence.py: local files the game itself already writes, no
memory reading, no packet capture.

Two things this has to work around:
- The breadcrumb folder name includes an ID minted fresh per game launch (a
  "*.run" folder under .sentry-native\\), so it's rediscovered on every call
  by picking whichever one was modified most recently.
- Breadcrumbs are plain concatenated MessagePack objects (not JSON, not
  wrapped in an array), so this streams them with `msgpack.Unpacker`.
"""

import glob
import os
import subprocess
import winreg
from datetime import datetime, timezone

import msgpack

APP_ID = "1867240"
_BREADCRUMB_TS_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"

# The real client process (there's also a separate launcher process that
# hands off to this one — checked for too, in case it stays resident) — see
# process_status()'s docstring for what this is actually used for.
WARDOGS_PROCESS_NAMES = ("WardogsClient-Win64-Shipping.exe", "WardogsLauncher-Shipping.exe")
STEAM_PROCESS_NAME = "steam.exe"


def process_status():
    """(steam_running, wardogs_running), both bools, from one `tasklist`
    snapshot. Fails open — (True, True) — if the process list can't be read
    for any reason, since a diagnostic hiccup here must never be able to
    stop the actual tracking feature from working.

    This exists so richpresence.py/tracker.py can tell whether the *real*
    Wardogs process is running independently of Steam's own Rich Presence
    state — merely opening a Steamworks session under Wardogs' App ID
    (unavoidable — it's the only way to read its Rich Presence at all) is
    itself what makes Steam report "Wardogs is running" to friends, so that
    can't be checked by asking Steam without causing the exact false status
    this is meant to prevent. tracker.py only opens that session while this
    reports the real process is actually up."""
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=3,
            creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return True, True
    steam_up = STEAM_PROCESS_NAME in out
    wardogs_up = any(name.lower() in out for name in WARDOGS_PROCESS_NAMES)
    return steam_up, wardogs_up


def _steam_install_path():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            return winreg.QueryValueEx(key, "SteamPath")[0].replace("/", "\\")
    except OSError:
        return r"C:\Program Files (x86)\Steam"


def _steam_library_paths():
    steam_path = _steam_install_path()
    libs = [steam_path]
    vdf_path = os.path.join(steam_path, "steamapps", "libraryfolders.vdf")
    try:
        with open(vdf_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith('"path"'):
                    libs.append(line.split('"')[3].replace("\\\\", "\\"))
    except OSError:
        pass
    return libs


_INSTALL_DIR_UNSET = object()
_install_dir_cache = _INSTALL_DIR_UNSET


def _find_install_dir():
    """Returns .../steamapps/common/Wardogs (the folder holding the Wardogs/
    and Engine/ subfolders), searched across every Steam library.

    Cached after the first call — this involves a registry read and parsing
    libraryfolders.vdf, and tracker.py calls into this every ~2s poll during
    a match, so re-discovering it every time would be a lot of needless
    disk/registry I/O for something that can't change mid-run."""
    global _install_dir_cache
    if _install_dir_cache is not _INSTALL_DIR_UNSET:
        return _install_dir_cache
    _install_dir_cache = _find_install_dir_uncached()
    return _install_dir_cache


def _find_install_dir_uncached():
    for lib in _steam_library_paths():
        manifest = os.path.join(lib, "steamapps", f"appmanifest_{APP_ID}.acf")
        if not os.path.exists(manifest):
            continue
        installdir = "Wardogs"
        try:
            with open(manifest, encoding="utf-8") as f:
                for line in f:
                    if '"installdir"' in line:
                        installdir = line.split('"')[3]
                        break
        except OSError:
            pass
        candidate = os.path.join(lib, "steamapps", "common", installdir)
        if os.path.isdir(candidate):
            return candidate
    return None


def _find_breadcrumb_files():
    """Breadcrumb file(s) for the most recently active game run, or [] if
    Wardogs isn't installed or hasn't been launched since crash-reporter
    setup. Sorted so callers see them in a stable order."""
    install_dir = _find_install_dir()
    if not install_dir:
        return []
    sentry_dir = os.path.join(install_dir, "Wardogs", ".sentry-native")
    run_dirs = glob.glob(os.path.join(sentry_dir, "*.run"))
    if not run_dirs:
        return []
    latest = max(run_dirs, key=os.path.getmtime)
    return sorted(glob.glob(os.path.join(latest, "__sentry-breadcrumb*")))


def _read_breadcrumbs(path):
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return []
    unpacker = msgpack.Unpacker(raw=False)
    unpacker.feed(raw)
    crumbs = []
    try:
        for obj in unpacker:
            crumbs.append(obj)
    except Exception:
        pass  # file was mid-write when read; keep whatever parsed cleanly
    return crumbs


# The breadcrumb's raw map value is Wardogs' internal/game-files world name,
# not what players actually call it — confirmed via wardogshub.gg/map/: the
# server browser and community use Ozeti/Bakurani/Zestafona, and "Europe"/
# "Kavkazi" are just the underlying world names those correspond to (Europe
# -> Ozeti explicitly per that source; Kavkazi -> Bakurani by matching
# region/theme, since both describe the same Eastern-Europe mountain map).
MAP_NAMES = {
    "Europe": "Ozeti",
    "Kavkazi": "Bakurani",
}


def display_map_name(raw):
    """Community/server-browser name for a raw map value from current_map(),
    or the raw value itself if there's no known alternate name."""
    return MAP_NAMES.get(raw, raw)


def current_map():
    """Best-effort current/most-recent map name, or None if unavailable."""
    best_ts, best_map = None, None
    for path in _find_breadcrumb_files():
        for crumb in _read_breadcrumbs(path):
            if crumb.get("category") == "Unreal" and crumb.get("message") == "PostLoadMapWithWorld":
                ts = crumb.get("timestamp")
                if ts and (best_ts is None or ts > best_ts):
                    best_ts, best_map = ts, crumb.get("data", {}).get("Map")
    return best_map


def _parse_breadcrumb_ts(raw):
    try:
        return datetime.strptime(raw, _BREADCRUMB_TS_FMT).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def spawn_events():
    """UTC datetimes of every 'Player Spawned' breadcrumb currently in the
    ring buffer, sorted oldest-first. Wardogs fires this on the initial
    match insertion and on every respawn into a new life — a caller that
    only cares about respawns should filter out anything at/before its own
    recorded match-start time itself, since the very first one always
    corresponds to spawning into the match, not a second life."""
    events = []
    for path in _find_breadcrumb_files():
        for crumb in _read_breadcrumbs(path):
            if crumb.get("category") == "Player" and crumb.get("message") == "Player Spawned":
                dt = _parse_breadcrumb_ts(crumb.get("timestamp"))
                if dt:
                    events.append(dt)
    return sorted(events)


if __name__ == "__main__":
    print("Current map:", current_map())
