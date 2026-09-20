# Wardogs Profit Tracker

Tracks your Wardogs match profit/loss over time and graphs it — cash curve
and earn rate ($/min), live while you play or after the fact.

Reads the number straight from the game itself via Steam Rich Presence —
the same mechanism that shows "+$150 Profit" next to a friend's name in
your Steam friends list, just read for yourself instead. No screenshots,
no OCR, no calibration, no misreads: the exact value Wardogs reports,
polled every 2 seconds.

## How it works

Wardogs publishes its own match state to Steam as it plays — `game_state`
(a menu, or `"playing"`) and `profit_loss` for the current match. Reading
your own Rich Presence needs the raw Steamworks "flat API" in
`steam_api64.dll` via `ctypes`: the `steamworkspy` package on PyPI is a
convenient source for that DLL (it ships as a redistributable runtime
component with every Steam game), but its own Python wrapper doesn't
expose Rich Presence at all — so `richpresence.py` talks to the DLL's raw
exports directly instead. See that file for the details.

Each session is also tagged with the **map** (`Frontend`, `Kavkazi`, ...)
and **faction** you played, so match history isn't just an anonymous
profit curve. `faction` comes from Rich Presence like everything else
above. `map` doesn't — Rich Presence never reports it — so `mapinfo.py`
reads it instead from Wardogs' own crash reporter (sentry-native), which
keeps a small rolling log of recent events (map loads, settings changes,
server login/logout) on disk next to the game install, in case it needs
to attach context to a future crash report. Same category of access as
Rich Presence: a local file the game already writes for its own purposes,
just read instead of only used at crash time. See `mapinfo.py` for the
path-discovery and parsing details (it's MessagePack, not JSON).

No login, no credentials, no packet inspection — this only reads local
Steam client state for the currently-logged-in user, the same category of
access the Steam friends-list UI itself uses.

**Known gap:** Wardogs doesn't expose a per-life signal through Rich
Presence — only overall match state and running profit/loss. If you die
and keep playing the same match (multiple lives), that won't show up as
a distinct event; it just shows in `profit_loss` moving normally.

## One-time setup

1. Install Python 3.10+ if you don't have it: https://www.python.org/downloads/
2. Install the Python dependencies:
   ```
   pip install -r requirements.txt
   ```
3. That's it. `richpresence.py` reuses the `steam_api64.dll` bundled
   inside the `steamworkspy` package, and Wardogs' Steam App ID (1867240,
   from `steamapps/appmanifest_1867240.acf`) is hardcoded in
   `richpresence.py`. Just have Steam and Wardogs running.

## Desktop app (recommended)

```
python app.py
```

One window for everything. It just runs — no buttons to start, stop, or
configure:

- **Auto-detects match start/end** by watching `game_state`. A match
  starts once it reports `"playing"` for 2 consecutive polls, and ends
  once it doesn't for 5 consecutive polls (~10s) — that tolerance matters:
  real data showed `game_state` can flicker away from `"playing"` for a
  poll or two during genuinely continuous play (a Steamworks callback
  timing quirk), and with zero tolerance that silently fragmented single
  matches into multiple session files. A new match shows up as a card in
  the sidebar on its own; nothing to click. Tune `confirm_reads` /
  `miss_threshold` on `tracker.auto_track()` if it's ever too eager or too
  slow to detect a real start/end for you.
- Click any match in the sidebar and the graph **replays it from the
  start** — the line sweeps from the first reading to the last instead of
  just appearing. A match still in progress gets a pulsing marker on its
  latest point.
- **Hover the chart** to see the exact cash and earn rate at that point in
  time, with a crosshair across both panels.
- The status pill shows "Steam not running" if it can't connect — just
  start Steam (and Wardogs) and it picks up on its own within a couple
  seconds, still no restart needed.

### Standalone .exe

A prebuilt `dist/profitdog.exe` is included — just double-click it, no
Python install needed. To rebuild it after changing the code:

```
pip install pyinstaller
python -m PyInstaller profitdog.spec
```

The output lands in `dist/profitdog.exe`. `profitdog.spec` bundles
`steam_api64.dll` explicitly (PyInstaller's static analysis can't see a
`ctypes.WinDLL` load the way it sees a normal `import`, so it has to be
listed there) and excludes a handful of large unrelated packages
(torch/scipy/etc.) that PyInstaller's auto-discovery otherwise pulls in
on some machines, bloating a ~40 MB exe into 150+ MB for libraries this
app never uses.

## Track a match from the command line

```
python tracker.py
```

- Auto-detects match start/end the same way the desktop app does —
  no session name needed.
- Saves to `session_<timestamp>.csv`, auto-named per detected match.
- `--interval 3` to poll every 3 seconds instead of the 2s default.
- Press `Ctrl+C` to stop.

## Graph the results

After a match:

```
python plot_graph.py session_20260915_201530.csv
```

Or watch it update live in a second window while `tracker.py` is still running:

```
python plot_graph.py session_20260915_201530.csv --live
```

Shows:
- **Top panel** — profit/loss over time (can start negative — Wardogs
  reports it as a loss until you're back in the black)
- **Bottom panel** — rolling earn rate ($/min), so you can see which parts
  of the match were actually profitable
- Hover anywhere on either panel for a crosshair + tooltip with the exact
  time, cash, and earn rate at that point.

## Optional: sharing matches with the developer

The app can optionally upload your match data (cash curve, map, faction,
duration, life count, role XP, plus your Steam ID/display name) to a
private database the developer uses to see stats across players. It's
**off by default** — you'll see a one-time dialog on first launch asking to
opt in, and a "Sync" button in the header lets you turn it on/off anytime.
Nobody but the developer can read this data back, including other players:
the key the app uses can only submit rows, never read them (enforced
server-side, see `supabase_schema.sql`). Declining, or never touching the
Sync button, leaves the app exactly as it's always worked — fully local,
nothing sent anywhere.

Setting up your own backend for this (only needed if you're maintaining a
fork, not for normal use):
1. Create a free project at [supabase.com](https://supabase.com).
2. Run `supabase_schema.sql` in its SQL Editor (creates the tables + the
   insert-only security policies).
3. Copy the `anon` key from Project Settings → API into `cloud_config.py`.
4. To view uploaded matches yourself: copy `devview_secrets.json.example`
   to `devview_secrets.json`, fill in the `service_role` key (**never**
   commit this file or put it in `cloud_config.py`), then `python devview.py`.

## Releasing an update

The standalone `.exe` checks GitHub Releases on every launch and
self-updates if a newer one exists (`updater.py`) — running from source
(`python app.py`) never does this, since there's nothing to replace; just
`git pull`. To ship a new version:

1. Bump `APP_VERSION` in `version.py` (e.g. `"1.1.0"`).
2. Rebuild: `python -m PyInstaller profitdog.spec`.
3. Tag and publish a GitHub Release whose tag matches (a `v` prefix is
   fine — `v1.1.0`), with `dist/profitdog.exe` attached as a release
   asset named exactly `profitdog.exe`:
   ```
   git tag v1.1.0
   git push origin v1.1.0
   gh release create v1.1.0 dist/profitdog.exe --title v1.1.0 --notes "..."
   ```
Players on an older version pick it up next time they launch the app —
downloaded in the background, swapped in, and relaunched automatically
(a few seconds' pause with a "Updating…" status), no action needed on
their end.

## Notes / known limitations

- Needs Steam (and Wardogs) actually running — this reads local Steam
  client state, not something that works standalone.
- Tied to Wardogs specifically: the Steam App ID and the exact Rich
  Presence key names (`game_state`, `profit_loss`, `faction`,
  `in_profit_or_loss`) are hardcoded in `richpresence.py`, since those
  come from Wardogs' own code, not a generic Steamworks feature. If a
  future Wardogs update renames or restructures these, this would need
  updating to match — the easiest way to check is `richpresence.py`'s
  `RichPresence.read()` printed as a dict while in a match.
- No per-life granularity (see "Known gap" above) — only match-level
  profit/loss and start/end.
- `map` needs Wardogs' crash-reporter breadcrumb file to exist, which is
  only there once the game has been launched at least once this install
  (it's per-run, not persisted long-term). If it's ever missing, `map`
  just logs blank for that session — nothing else is affected.
- Still no loadout/weapon/kill data from any local source found so far
  (checked Rich Presence, the crash-reporter breadcrumbs, and Wardogs'
  save/log folders — none of them expose it).
- Two processes (the actual game and this tracker) both initialize a
  local Steamworks session concurrently under the same App ID — this is
  a normal, supported pattern for companion/overlay tools and caused no
  issues in testing, but is worth knowing if something ever seems off
  with Steam itself while both are running.
- If a match ever *does* still get split into two session files (the
  `miss_threshold` tolerance above should prevent this, but it was seen
  once before that fix existed), the tell is a new session's first
  `cash` value landing close to the previous session's last value, with
  only a few seconds between them — a real new match always restarts
  fresh. Fixing it is a manual CSV merge: append the second file's rows
  to the first, recomputing `seconds_elapsed` for the appended rows as
  `previous_last_elapsed + (real timestamp gap in seconds)`, keep the
  `timestamp` column as-is (and the first file's `map`/`faction`, since
  a real split keeps the same match), then delete the second file.

## License

GPL-3.0 — see [LICENSE](LICENSE). Free to use, modify, and redistribute;
anything you distribute based on this code must stay open source under
the same license.
