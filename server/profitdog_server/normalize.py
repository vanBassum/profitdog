"""Turning what the agent saw into what the domain can use.

This is the "normalization" half of collection, and it lives on the server for
one reason: it is interpretation, and interpretation gets things wrong. The
agent stores `"+$150 Profit"` because that is the string Wardogs published; this
module decides it means `150`. When a future Wardogs writes `"+$1.5k"` and the
regex stops matching, the fix is a server deploy and a re-read of history —
which works precisely because the original string is still in the database.

Nothing here is versioned by ruleset, deliberately. These are spellings, not
rules: what a faction code is called, how a money string is punctuated, which
community name a map goes by. A change in the economy needs a new ruleset; a
change in punctuation needs a better parser, applied to everything.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

_MONEY = re.compile(r"([+-]?)\$?([\d,]+)")


def parse_profit(raw: Any) -> int | None:
    """`'-$5,440'` -> `-5440`, `'+$150'` -> `150`, unparseable -> None.

    Returning None rather than 0 matters: a reading we could not parse is not
    a reading of zero, and the difference is a flat stretch in the curve versus
    a hole in it.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return int(raw)
    match = _MONEY.match(str(raw).strip())
    if not match:
        return None
    sign = -1 if match.group(1) == "-" else 1
    return sign * int(match.group(2).replace(",", ""))


# Wardogs' Rich Presence reports your TEAM (not squad — confirmed live: a squad
# change left this untouched, only switching teams changed it) as an internal
# codename. Verified against the game's three actual factions.
FACTION_NAMES = {
    "alpha": "Lonestar",
    "bravo": "Valkyra",
    "charlie": "Manticore",
}

#: Community / server-browser names for the raw map values the breadcrumbs use.
MAP_NAMES = {
    "Europe": "Ozeti",
    "Kavkazi": "Bakurani",
}


def faction_name(code: str | None) -> str | None:
    """Human-readable team name, or the raw code when it is not one we know."""
    if not code:
        return None
    return FACTION_NAMES.get(code, code)


def display_map_name(raw: str | None) -> str | None:
    if not raw:
        return None
    return MAP_NAMES.get(raw, raw)


def normalize_map(raw: str | None) -> str | None:
    """A map name, or None for the placeholders that mean 'we do not know'."""
    value = (raw or "").strip()
    if not value or value.lower() == "unknown":
        return None
    return value


def is_real_faction(code: str | None) -> bool:
    """Whether a faction value names a team rather than a placeholder.

    Wardogs publishes `unknown` during matchmaking and only names the team once
    it has assigned you one, so latching the first non-empty value would pin
    that placeholder to the whole match.
    """
    return bool(code) and str(code).strip().lower() not in ("", "unknown")


def parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def to_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def iso(value: datetime) -> str:
    return to_utc(value).astimezone(timezone.utc).isoformat(timespec="microseconds")
