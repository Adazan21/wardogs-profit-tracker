"""
richpresence.py — Read Wardogs' own Steam Rich Presence for match state and
profit/loss, as an alternative to OCR-based tracking in tracker.py.

Wardogs publishes its own state via Steamworks Rich Presence — the exact
mechanism Steam uses to show "+$150 Profit" next to a friend's name in your
friends list. Reading it for yourself needs the raw Steamworks "flat API" in
steam_api64.dll directly via ctypes: the `steamworkspy` package on PyPI is a
convenient source for that DLL, but its own Python wrapper doesn't expose
rich presence at all, so this talks to the DLL's exports directly instead.

No login, no credentials, no packet inspection — this only reads local
Steam client state for the currently logged-in user, the same category of
access the Steam friends-list UI itself uses.

Usage:
    import richpresence
    rp = richpresence.RichPresence()
    rp.init()
    data = rp.read()   # {'game_state': 'playing', 'profit_loss': '-$150', ...}
    rp.shutdown()
"""

import ctypes
import os
import re
import sys

APP_ID = 1867240  # Wardogs, from steamapps/appmanifest_1867240.acf

# steamworkspy ships the raw steam_api64.dll as a redistributable runtime
# component (same DLL every Steam game ships with its own build) — reused
# here rather than requiring a separate download.
_DLL_CANDIDATES = []
if getattr(sys, "frozen", False):
    # Bundled by profitdog.spec's `binaries` entry, extracted alongside the
    # frozen app — ctypes.WinDLL can't be auto-detected by PyInstaller's
    # static analysis the way a normal `import` can, so it has to be listed
    # there explicitly.
    _DLL_CANDIDATES.append(os.path.join(sys._MEIPASS, "steam_api64.dll"))
_DLL_CANDIDATES.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "steam_api64.dll"))
try:
    import steamworkspy
    _DLL_CANDIDATES.append(os.path.join(os.path.dirname(steamworkspy.__file__), "steam_api64.dll"))
except ImportError:
    pass


def _find_dll():
    for path in _DLL_CANDIDATES:
        if os.path.exists(path):
            return path
    raise FileNotFoundError(
        "steam_api64.dll not found. Install the steamworkspy package "
        "(`pip install steamworkspy`) or place steam_api64.dll next to this file."
    )


def parse_profit(raw):
    """'-$5,440' -> -5440, '+$150' -> 150, None/unparseable -> None."""
    if not raw:
        return None
    m = re.match(r"([+-]?)\$?([\d,]+)", raw.strip())
    if not m:
        return None
    sign = -1 if m.group(1) == "-" else 1
    return sign * int(m.group(2).replace(",", ""))


# Wardogs' Rich Presence reports your TEAM (not squad — confirmed live: a
# squad change left this untouched, only switching teams changed it) as an
# internal codename. Verified against the game's three actual factions:
# Lonestar (blue), Valkyra (red), Manticore (green).
FACTION_NAMES = {
    "alpha": "Lonestar",
    "bravo": "Valkyra",
    "charlie": "Manticore",
}


def faction_name(code):
    """Human-readable team name for a `faction` Rich Presence value, or the
    raw code itself if unrecognized (e.g. 'unknown' during matchmaking)."""
    return FACTION_NAMES.get(code, code)


class RichPresence:
    def __init__(self, app_id=APP_ID, dll_path=None):
        self.app_id = app_id
        self.dll_path = dll_path or _find_dll()
        self.dll = None
        self.friends = None
        self.user = None
        self.steam_id = None

    def init(self):
        """Returns True on success, False if Steam isn't running or init fails."""
        appid_path = os.path.join(os.getcwd(), "steam_appid.txt")
        with open(appid_path, "w") as f:
            f.write(str(self.app_id))

        dll = ctypes.WinDLL(self.dll_path)
        dll.SteamAPI_Init.restype = ctypes.c_bool
        dll.SteamAPI_SteamFriends_v017.restype = ctypes.c_void_p
        dll.SteamAPI_SteamUser_v021.restype = ctypes.c_void_p
        dll.SteamAPI_ISteamUser_GetSteamID.restype = ctypes.c_uint64
        dll.SteamAPI_ISteamUser_GetSteamID.argtypes = [ctypes.c_void_p]
        dll.SteamAPI_ISteamFriends_GetFriendRichPresenceKeyCount.restype = ctypes.c_int32
        dll.SteamAPI_ISteamFriends_GetFriendRichPresenceKeyCount.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        dll.SteamAPI_ISteamFriends_GetFriendRichPresenceKeyByIndex.restype = ctypes.c_char_p
        dll.SteamAPI_ISteamFriends_GetFriendRichPresenceKeyByIndex.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_int32]
        dll.SteamAPI_ISteamFriends_GetFriendRichPresence.restype = ctypes.c_char_p
        dll.SteamAPI_ISteamFriends_GetFriendRichPresence.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_char_p]
        dll.SteamAPI_ISteamFriends_RequestFriendRichPresence.restype = None
        dll.SteamAPI_ISteamFriends_RequestFriendRichPresence.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        dll.SteamAPI_ISteamFriends_GetPersonaName.restype = ctypes.c_char_p
        dll.SteamAPI_ISteamFriends_GetPersonaName.argtypes = [ctypes.c_void_p]
        dll.SteamAPI_RunCallbacks.restype = None
        dll.SteamAPI_Shutdown.restype = None

        if not dll.SteamAPI_Init():
            return False

        self.dll = dll
        self.friends = dll.SteamAPI_SteamFriends_v017()
        self.user = dll.SteamAPI_SteamUser_v021()
        self.steam_id = dll.SteamAPI_ISteamUser_GetSteamID(self.user)
        return True

    def read(self):
        """Poll current rich presence. Returns {} while in a menu (Wardogs
        only sets keys once you're actually in a match)."""
        dll = self.dll
        dll.SteamAPI_RunCallbacks()
        dll.SteamAPI_ISteamFriends_RequestFriendRichPresence(self.friends, self.steam_id)
        dll.SteamAPI_RunCallbacks()

        count = dll.SteamAPI_ISteamFriends_GetFriendRichPresenceKeyCount(self.friends, self.steam_id)
        data = {}
        for i in range(count):
            key = dll.SteamAPI_ISteamFriends_GetFriendRichPresenceKeyByIndex(self.friends, self.steam_id, i)
            if key is None:
                continue
            val = dll.SteamAPI_ISteamFriends_GetFriendRichPresence(self.friends, self.steam_id, key)
            data[key.decode()] = val.decode() if val else None
        return data

    def persona_name(self):
        """The logged-in user's current Steam display name, or None."""
        if not self.dll or not self.friends:
            return None
        raw = self.dll.SteamAPI_ISteamFriends_GetPersonaName(self.friends)
        return raw.decode() if raw else None

    def shutdown(self):
        if self.dll:
            self.dll.SteamAPI_Shutdown()
