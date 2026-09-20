"""
cloudsync.py — Optional, opt-in upload of match data to a shared Supabase
project, so the developer can see match stats across every player running
this app. See supabase_schema.sql for the backend schema/policies and
app.py's ConsentDialog for exactly what's disclosed before any of this runs.

The embedded key is insert-only, enforced server-side by Supabase Row-Level
Security (see supabase_schema.sql) — not just by convention here. Nothing in
this module can read data back.

This never touches the Steamworks DLL itself: `steam_id`/`persona_name` are
resolved once by tracker.py's watcher thread (the only thread that's allowed
to touch Steamworks — see its `on_identity` callback) and handed in here as
plain strings, so sync's network I/O can safely run on its own thread.

Every failure here — no consent, no internet, Supabase unreachable, a
malformed row — is swallowed and simply tried again next launch. Sync must
never be able to block or break the local, offline-first tracking app.py
already does regardless of any of this.
"""

import csv
import glob
import json
import os
import uuid

import requests

import cloud_config
import tracker
from version import APP_VERSION

REQUEST_TIMEOUT = 8
ROLES = ["Wardog", "Infantry", "Medic", "Driver", "Pilot", "Support", "Recon"]


def _appdata_dir():
    base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
    path = os.path.join(base, "WardogsProfitTracker")
    os.makedirs(path, exist_ok=True)
    return path


CONSENT_PATH = os.path.join(_appdata_dir(), "sync_consent.json")
STATE_PATH = os.path.join(_appdata_dir(), "sync_state.json")
IDENTITY_PATH = os.path.join(_appdata_dir(), "steam_identity.json")


# ---------------------------------------------------------------- consent --
def load_consent():
    """{'decided': bool, 'enabled': bool} — 'decided' is False only before
    the consent dialog has ever been answered."""
    try:
        with open(CONSENT_PATH) as f:
            data = json.load(f)
        return {"decided": bool(data.get("decided")), "enabled": bool(data.get("enabled"))}
    except (OSError, ValueError):
        return {"decided": False, "enabled": False}


def set_consent(enabled):
    with open(CONSENT_PATH, "w") as f:
        json.dump({"decided": True, "enabled": bool(enabled)}, f)


# --------------------------------------------------------------- identity --
def load_cached_identity():
    """(steam_id, persona_name) resolved on some earlier launch, or
    (None, None) if Wardogs has never actually been running while this app
    was open. Without this cache, every fresh launch would need Wardogs
    open at least once before it could sync anything at all — even a
    backlog of already-finished matches sitting on disk from a prior run,
    since identity by itself only ever lived in that earlier process's
    memory (see app.py's on_identity wiring)."""
    try:
        with open(IDENTITY_PATH) as f:
            data = json.load(f)
        return data.get("steam_id"), data.get("persona_name")
    except (OSError, ValueError):
        return None, None


def save_cached_identity(steam_id, persona_name):
    with open(IDENTITY_PATH, "w") as f:
        json.dump({"steam_id": steam_id, "persona_name": persona_name}, f)


# ------------------------------------------------------------------ state --
def _load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"synced_sessions": [], "xp_log_offset": 0}


def _save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)


# ---------------------------------------------------------------- network --
def _configured():
    return bool(cloud_config.SUPABASE_URL and cloud_config.SUPABASE_ANON_KEY)


def _headers():
    return {
        "apikey": cloud_config.SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {cloud_config.SUPABASE_ANON_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }


def _post(table, rows):
    if not rows:
        return True
    resp = requests.post(
        f"{cloud_config.SUPABASE_URL}/rest/v1/{table}",
        headers=_headers(), data=json.dumps(rows), timeout=REQUEST_TIMEOUT,
    )
    return resp.ok


# ----------------------------------------------------------------- upload --
def _upload_session(session_file, steam_id, persona_name):
    with open(session_file, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return True

    # The id is generated here rather than left to the database default so
    # the ticks below can reference it without ever needing the insert's
    # response body back — anon has no SELECT policy (by design, see
    # supabase_schema.sql), and Postgres's RETURNING clause requires SELECT
    # visibility of the new row under RLS even for an otherwise-permitted
    # INSERT, which fails with the same "violates row-level security
    # policy" error as a rejected insert. Requesting return=minimal (the
    # _headers() default) sidesteps that entirely instead of loosening the
    # policy just to get an id back.
    match_id = str(uuid.uuid4())
    match_row = {
        "id": match_id,
        "session_file": session_file,
        "steam_id": steam_id,
        "persona_name": persona_name,
        "map": rows[0].get("map") or None,
        "faction": rows[0].get("faction") or None,
        "started_at": rows[0]["timestamp"],
        "ended_at": rows[-1]["timestamp"],
        "final_profit": int(float(rows[-1]["cash"])),
        "max_life": max((int(r["life"]) for r in rows if r.get("life")), default=1),
        "app_version": APP_VERSION,
    }
    resp = requests.post(
        f"{cloud_config.SUPABASE_URL}/rest/v1/matches",
        headers=_headers(), data=json.dumps(match_row), timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code == 409:
        # session_file already exists server-side (e.g. local sync_state.json
        # was lost/reset) — matches+ticks go up together, so if the match
        # row is there, its ticks already are too. Nothing left to do.
        return True
    if not resp.ok:
        return False

    ticks = [
        {
            "match_id": match_id,
            "timestamp": r["timestamp"],
            "seconds_elapsed": float(r["seconds_elapsed"]),
            "cash": int(float(r["cash"])),
            "life": int(r["life"]) if r.get("life") else None,
        }
        for r in rows
    ]
    return _post("match_ticks", ticks)


def _upload_xp_rows(rows, steam_id, persona_name):
    events = []
    for r in rows:
        for role in ROLES:
            try:
                gained = int(r.get(role) or 0)
            except ValueError:
                gained = 0
            if gained:
                events.append({
                    "steam_id": steam_id,
                    "persona_name": persona_name,
                    "timestamp": r.get("timestamp"),
                    "session_file": r.get("session_file") or None,
                    "role": role,
                    "xp_gained": gained,
                })
    return _post("role_xp_events", events)


def sync_now(steam_id, persona_name):
    """Uploads anything not yet synced: every session_*.csv not in the local
    synced set, plus any new role_xp_log.csv rows since the last run. Safe
    to call every launch — already-synced data is skipped via local state,
    and every network/consent/config gap here just means "did nothing"."""
    if not _configured() or not steam_id:
        return
    consent = load_consent()
    if not consent["decided"] or not consent["enabled"]:
        return

    state = _load_state()
    synced = set(state.get("synced_sessions", []))

    for session_file in sorted(glob.glob("session_*.csv")):
        if session_file in synced:
            continue
        try:
            if _upload_session(session_file, steam_id, persona_name):
                synced.add(session_file)
        except requests.RequestException:
            pass  # try again next launch
    state["synced_sessions"] = sorted(synced)
    _save_state(state)

    if os.path.exists(tracker.XP_LOG_PATH):
        try:
            with open(tracker.XP_LOG_PATH, newline="") as f:
                all_rows = list(csv.DictReader(f))
        except OSError:
            all_rows = []
        offset = state.get("xp_log_offset", 0)
        new_rows = all_rows[offset:]
        if new_rows:
            try:
                if _upload_xp_rows(new_rows, steam_id, persona_name):
                    state["xp_log_offset"] = len(all_rows)
                    _save_state(state)
            except requests.RequestException:
                pass
