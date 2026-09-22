"""appdata.py — where this app's own files live on disk.

A double-clicked exe's working directory is wherever the user happened to
save it (often Downloads), and several parts of the app write files using
plain relative paths (tracker.py's session CSVs and role_xp_log.csv,
richpresence.py's steam_appid.txt) — dumping those next to the exe would
clutter whatever folder it's sitting in. Instead, app.py's main() chdir's
into this directory once at startup, so all of that existing relative-path
code lands here instead, without needing to touch every call site.
"""

import os

APP_DIR_NAME = "WardogsProfitTracker"


def data_dir():
    base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
    path = os.path.join(base, APP_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path
