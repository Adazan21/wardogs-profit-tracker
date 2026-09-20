"""
updater.py — Self-update the frozen .exe from GitHub Releases on launch.

Only does anything when running as the frozen exe (`sys.frozen`) — running
from source, there's nothing to replace; just `git pull`. Checks
GitHub's public releases API (no auth needed for a public repo), and if the
latest release's tag is newer than version.APP_VERSION, downloads its
`profitdog.exe` asset and hands off to a small detached helper script that
waits for this process to exit, swaps the file, and relaunches it — the
standard pattern for a Windows exe replacing itself, since a running exe
can't overwrite its own file directly.

Every failure here (no internet, GitHub unreachable/rate-limited, a
truncated download, no matching asset) is swallowed and simply retried next
launch — this must never be able to block or break a normal run of the app.
"""

import os
import subprocess
import sys
import tempfile
import time

import requests

from version import APP_VERSION

GITHUB_REPO = "Adazan21/wardogs-profit-tracker"
GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
ASSET_NAME = "profitdog.exe"
MIN_VALID_DOWNLOAD_BYTES = 1_000_000  # a real build is tens of MB; anything tinier is a bad response
REQUEST_TIMEOUT = 8
DOWNLOAD_TIMEOUT = 60


def _version_tuple(v):
    v = v.strip().lstrip("vV")
    parts = []
    for p in v.split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def check_for_update():
    """The latest release's JSON if it's newer than APP_VERSION, else None."""
    resp = requests.get(GITHUB_API, timeout=REQUEST_TIMEOUT,
                         headers={"Accept": "application/vnd.github+json"})
    if not resp.ok:
        return None
    release = resp.json()
    tag = release.get("tag_name", "")
    if not tag or _version_tuple(tag) <= _version_tuple(APP_VERSION):
        return None
    return release


def download_asset(release, dest_path):
    """Downloads this release's profitdog.exe asset to dest_path. Returns
    True only once the file is fully written and passes a basic size
    sanity check (guards against e.g. a GitHub rate-limit HTML page or a
    connection that dropped mid-download being mistaken for a real exe)."""
    asset = next((a for a in release.get("assets", []) if a.get("name") == ASSET_NAME), None)
    if not asset or not asset.get("browser_download_url"):
        return False

    tmp_path = dest_path + ".part"
    try:
        resp = requests.get(asset["browser_download_url"], timeout=DOWNLOAD_TIMEOUT, stream=True)
        if not resp.ok:
            return False
        total = 0
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                f.write(chunk)
                total += len(chunk)
        if total < MIN_VALID_DOWNLOAD_BYTES:
            return False
        os.replace(tmp_path, dest_path)
        return True
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def apply_update_and_restart(exe_path, new_path):
    """Hands off to a detached helper script that waits for this process
    (by PID) to exit, swaps the downloaded exe into place, relaunches it,
    then deletes itself — and exits this process immediately so the file
    lock on exe_path actually releases. Never returns."""
    pid = os.getpid()
    bat_path = os.path.join(tempfile.gettempdir(), "profitdog_update.bat")
    bat_contents = (
        "@echo off\r\n"
        "set /a count=0\r\n"
        ":wait\r\n"
        "set /a count+=1\r\n"
        f"if %count% GEQ 30 goto proceed\r\n"
        f'tasklist /FI "PID eq {pid}" 2>NUL | find "{pid}" >NUL\r\n'
        "if not errorlevel 1 (\r\n"
        "    timeout /t 1 /nobreak >NUL\r\n"
        "    goto wait\r\n"
        ")\r\n"
        ":proceed\r\n"
        f'move /Y "{new_path}" "{exe_path}" >NUL\r\n'
        f'start "" "{exe_path}"\r\n'
        'del "%~f0"\r\n'
    )
    with open(bat_path, "w") as f:
        f.write(bat_contents)

    subprocess.Popen(
        ["cmd", "/c", bat_path],
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
    )
    os._exit(0)


def run_auto_update_check(on_update_found=None):
    """Call once from a background thread shortly after launch. If a newer
    build is downloaded successfully, calls `on_update_found(tag)` (e.g. to
    show a status message) and restarts into it a couple seconds later.
    Returns normally, doing nothing, in every other case."""
    if not getattr(sys, "frozen", False):
        return
    try:
        release = check_for_update()
        if not release:
            return
        exe_path = sys.executable
        new_path = exe_path + ".new"
        if not download_asset(release, new_path):
            return
        if on_update_found:
            on_update_found(release.get("tag_name", "latest"))
        time.sleep(2)  # let the caller's UI message actually be seen before the app vanishes
        apply_update_and_restart(exe_path, new_path)
    except Exception:
        pass
