"""What to say about a linked PC: is it there, and is it the current build?

Two questions, one answer, in one place. They are asked twice over -- by the
server-rendered **Get the agent** page and by the React header, which polls
`/api/agents` -- and two implementations of "is that PC online?" would drift
apart the first time either threshold was tuned. So the rows come out of the
database and go through here on the way to both.

Nothing in this module reads the clock or the environment on its own. `now` and
`current_version` are passed in, because a judgement that silently depends on
ambient state is a judgement that cannot be tested, and both of these are
exactly the things a test needs to move around.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from profitdog_protocol import OFFLINE_AFTER_SEC

#: What a checkout calls itself. It is a real answer, not a missing one: a
#: build that was never released should not be told it is out of date against
#: a release it predates, and should not be called current either.
DEV_VERSION_SUFFIX = "+dev"


@dataclass(frozen=True)
class AgentStatus:
    """One linked PC, as both the page and the API describe it."""

    agent_id: str
    label: str | None
    version: str | None
    last_seen_at: str | None
    #: Whether the server has heard from this PC recently enough to believe it
    #: is running. Computed here, from the server's own clock, and never in the
    #: browser: `last_seen_at` was written by this machine, so comparing it
    #: against a viewer's clock would fold their clock skew into the answer.
    online: bool
    #: Seconds since the last contact, or None if it has never been heard from.
    silent_for: float | None
    #: "current", "outdated", "dev", or "unknown". Never "newer": an agent
    #: ahead of the server is the server being behind, and telling someone to
    #: downgrade is not advice worth rendering.
    version_state: str
    #: The build this server considers current, if it knows of one.
    current_version: str | None

    def to_json(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "label": self.label,
            "version": self.version,
            "last_seen_at": self.last_seen_at,
            "online": self.online,
            "silent_for": self.silent_for,
            "version_state": self.version_state,
            "current_version": self.current_version,
        }


def _parse(stamp: str | None) -> datetime | None:
    """A stored timestamp, or None if it is missing or unreadable.

    Unreadable is not an error worth raising. The caller's next move is to say
    "never heard from", which is the honest thing to say about a row whose
    timestamp cannot be read.
    """
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    # Rows written before the column was consistently tz-aware read as naive;
    # they were UTC when they were written, so say so rather than compare a
    # naive datetime against an aware one and raise.
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _version_parts(version: str) -> tuple[int, ...] | None:
    """The leading dotted numbers of a version, for ordering.

    Only the numeric prefix, and only when every part of it is a number:
    `1.4.0` orders, `1.4.0-rc1` orders on `1.4.0`, and anything that does not
    start with a number does not order at all. Returning None then means "these
    two cannot be compared", which the caller reports as unknown rather than
    guessing at.
    """
    head = version.strip().lstrip("v").split("+", 1)[0].split("-", 1)[0]
    parts = head.split(".")
    try:
        return tuple(int(part) for part in parts if part != "")
    except ValueError:
        return None


def version_state(reported: str | None, current: str | None) -> str:
    """Whether a reported build is the current one.

    The four answers, and why each exists:

    - `unknown` -- one side did not say. An agent too old to report a version,
      or a server that was not told which build is current, and in both cases
      the honest output is the version string on its own with no verdict.
    - `dev` -- a checkout. It is neither current nor out of date, and offering
      somebody a "1.4.0 available" link for the tree they are editing is
      noise.
    - `outdated` -- the server knows of a higher version than this PC reports.
    - `current` -- they match, or the PC is somehow ahead, which means the
      server is the one behind and there is nothing to tell the player.
    """
    if not reported or not current:
        return "unknown"
    if reported.endswith(DEV_VERSION_SUFFIX):
        return "dev"
    if reported == current:
        return "current"
    mine, theirs = _version_parts(reported), _version_parts(current)
    if mine is None or theirs is None:
        # Two strings that differ but cannot be ordered. They are not the same
        # build, and that is all that can be said without inventing an order.
        return "outdated"
    return "outdated" if theirs > mine else "current"


def describe(row: dict, *, current_version: str | None, now: datetime) -> AgentStatus:
    """One `agents` row, judged."""
    seen = _parse(row.get("last_seen_at"))
    silent_for = None if seen is None else max(0.0, (now - seen).total_seconds())
    reported = row.get("agent_version") or None
    return AgentStatus(
        agent_id=str(row.get("agent_id") or ""),
        label=row.get("label") or None,
        version=reported,
        last_seen_at=row.get("last_seen_at") or None,
        online=silent_for is not None and silent_for < OFFLINE_AFTER_SEC,
        silent_for=silent_for,
        version_state=version_state(reported, current_version),
        current_version=current_version or None,
    )


def describe_all(rows, *, current_version: str | None,
                 now: datetime | None = None) -> list[AgentStatus]:
    now = now or datetime.now(timezone.utc)
    return [describe(dict(row), current_version=current_version, now=now) for row in rows]


def since_text(silent_for: float | None) -> str:
    """How long ago, in the words somebody would use out loud.

    Deliberately coarse. The reader is answering "is it running?", and the
    difference between 94 and 97 seconds does not bear on that; the difference
    between minutes and days does.
    """
    if silent_for is None:
        return "never"
    if silent_for < 90:
        return "just now"
    minutes = silent_for / 60
    if minutes < 60:
        return "%d minutes ago" % round(minutes)
    hours = minutes / 60
    if hours < 24:
        count = round(hours)
        return "an hour ago" if count == 1 else "%d hours ago" % count
    days = round(hours / 24)
    return "yesterday" if days == 1 else "%d days ago" % days


__all__ = [
    "AgentStatus",
    "describe",
    "describe_all",
    "since_text",
    "version_state",
]
