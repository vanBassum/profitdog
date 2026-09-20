"""
rolexp.py — Read Wardogs' local role-XP save file.

Wardogs tracks per-role progress (Wardog, Infantry, Medic, Driver, Pilot,
Support, Recon) in a small local save file, rewritten every time it
changes: %LOCALAPPDATA%\\Wardogs\\Saved\\SaveGames\\PlayerRoleProgress.sav.
It's UTF-16 text holding one JSON object keyed by an internal save-slot ID,
wrapping the actual `"(TagName=\"Meta.Role.X\")": <xp>` pairs — this reads
past the wrapper key (which isn't guaranteed stable) straight to the role
values.

No loadout/weapon data lives here or anywhere else found locally (see
mapinfo.py's docstring and README's known limitations) — this is XP only.
"""

import json
import os
import re

ROLE_TAG_RE = re.compile(r"Meta\.Role\.(\w+)")
ROLES = ["Wardog", "Infantry", "Medic", "Driver", "Pilot", "Support", "Recon"]


def _save_path():
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    return os.path.join(local_appdata, "Wardogs", "Saved", "SaveGames", "PlayerRoleProgress.sav")


def read_role_xp():
    """{'Wardog': 82, 'Infantry': 14, ...} or {} if the save doesn't exist
    yet / can't be read (e.g. Wardogs is mid-write to it)."""
    path = _save_path()
    try:
        with open(path, encoding="utf-16-le") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}

    if not raw:
        return {}
    inner = next(iter(raw.values()), {})
    result = {}
    for tag_key, xp in inner.items():
        m = ROLE_TAG_RE.search(tag_key)
        if m:
            result[m.group(1)] = xp
    return result


if __name__ == "__main__":
    print(read_role_xp())
