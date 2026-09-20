"""Which rules a match is read under, and why it is not simply "the latest".

## The problem this exists for

Wardogs is in Early Access. When a patch changes the economy — kit prices, how
payouts land, when `game_state` flips — every rule in `v1` stops describing the
game. But it does not stop describing *the matches that were played under it*.
A match from last month was played under last month's economy, and reading it
under this month's rules would produce a confident, wrong answer: kits priced
against instalment timing the game no longer uses, adjustments classified by
state transitions that no longer happen.

So rules are versioned, old implementations are kept forever, and a match is
read under the rules it was played under. No migration, no rewrite of history:
the facts never change, and which lens is held up to them is decided per match,
at read time.

## Selection: build first, date as a fallback

**By build.** The game build is the only thing that actually determines which
rules applied, so it is checked first. A ruleset declares the builds it covers;
a match carrying one of them is read under it. This is exact, and it is right
even when matches arrive out of order — an agent that was offline for a week
delivers old facts *now*, and a date-based rule would read them under today's
economy.

**By date, when there is no build.** Nothing local publishes the build today —
not Rich Presence, not the breadcrumbs, not the save file — so every match
recorded so far has `build = NULL`. For those, the ruleset effective at the
match's start time is used. That is a weaker signal (a patch lands at a moment
nobody recorded) and it is why build is threaded through every fact even though
nothing fills it in yet: the moment a source exposes it, selection gets exact
without either half of the system changing shape.

## Adding a ruleset

Copy `v1.py` to `v2.py`, change what the patch changed, and register it below
with the builds it covers and the date it took effect. Do not edit `v1.py`.
Every match already in the database keeps reading under v1 — that is the whole
point, and `tests/test_rulesets.py` holds it to it.
"""

from __future__ import annotations

import hashlib
import inspect
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import cache
from types import ModuleType
from typing import Iterable

from . import v1


@dataclass(frozen=True)
class RulesetEntry:
    version: str
    module: ModuleType
    #: Exact game builds this ruleset covers. The primary selector.
    builds: frozenset[str] = field(default_factory=frozenset)
    #: When these rules took effect, for matches with no recorded build.
    effective_from: datetime = datetime(1970, 1, 1, tzinfo=timezone.utc)
    note: str = ""

    @property
    def fingerprint(self) -> str:
        """A hash of the rules' own source.

        The version string says which rules these are; this says whether they
        have been edited. Cached conclusions are keyed on both, so correcting a
        bug in `v1.py` invalidates everything built from it without anyone
        having to remember to clear a cache — which is the failure mode that
        makes caches untrustworthy.
        """
        return _fingerprint_of(self.module)


@cache
def _fingerprint_of(module: ModuleType) -> str:
    try:
        source = inspect.getsource(module)
    except (OSError, TypeError):  # a module built at runtime, as in tests
        source = repr(sorted(vars(module)))
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]


#: Registered rulesets, oldest first.
#:
#: `v1` claims no builds and an effective date at the epoch, which makes it the
#: fallback for everything: every match recorded so far has no build, and every
#: one of them was played under these rules. A future entry claims the builds
#: it covers and the date it landed, and v1 keeps everything before it.
REGISTRY: list[RulesetEntry] = [
    RulesetEntry(
        version=v1.VERSION,
        module=v1,
        builds=frozenset(),
        effective_from=datetime(1970, 1, 1, tzinfo=timezone.utc),
        note="Rules observed across the recorded sessions of 2026-09-19.",
    ),
]

DEFAULT_VERSION = v1.VERSION


def register(entry: RulesetEntry) -> None:
    """Add a ruleset. Ordering by effective date is maintained here."""
    if any(e.version == entry.version for e in REGISTRY):
        raise ValueError(f"ruleset {entry.version} is already registered")
    REGISTRY.append(entry)
    REGISTRY.sort(key=lambda e: e.effective_from)


def unregister(version: str) -> None:
    """Remove a ruleset. For tests; production code never un-registers."""
    global REGISTRY
    REGISTRY = [e for e in REGISTRY if e.version != version]


def versions() -> list[str]:
    return [e.version for e in REGISTRY]


def by_version(version: str) -> RulesetEntry:
    for entry in REGISTRY:
        if entry.version == version:
            return entry
    raise KeyError(f"no ruleset {version!r}; known: {', '.join(versions())}")


def select(
    build: str | None = None,
    started_at: datetime | None = None,
    *,
    registry: Iterable[RulesetEntry] | None = None,
) -> RulesetEntry:
    """The ruleset a match should be read under.

    Build wins whenever it is present and claimed by somebody. Otherwise the
    latest ruleset whose effective date is at or before the match's start —
    and, failing even that (a match older than every registered ruleset), the
    earliest one, because reading an old match under the oldest rules available
    is the least wrong answer and refusing to read it at all is the most.
    """
    entries = sorted(
        registry if registry is not None else REGISTRY,
        key=lambda e: e.effective_from,
    )
    if not entries:  # pragma: no cover - the registry always has v1
        raise RuntimeError("no rulesets registered")

    if build:
        for entry in entries:
            if build in entry.builds:
                return entry

    if started_at is not None:
        moment = (
            started_at
            if started_at.tzinfo
            else started_at.replace(tzinfo=timezone.utc)
        )
        chosen = None
        for entry in entries:
            if entry.effective_from <= moment:
                chosen = entry
        if chosen is not None:
            return chosen
        return entries[0]

    return entries[-1]


__all__ = [
    "REGISTRY",
    "DEFAULT_VERSION",
    "RulesetEntry",
    "select",
    "register",
    "unregister",
    "by_version",
    "versions",
    "v1",
]
