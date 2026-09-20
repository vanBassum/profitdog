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
import winreg
from datetime import datetime, timezone

import msgpack

APP_ID = "1867240"
_BREADCRUMB_TS_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


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
