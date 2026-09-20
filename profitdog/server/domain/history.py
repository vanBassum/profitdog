"""Filters, buckets and totals over many matches.

Ported from `history.ts`. The filters are the same predicate the History page
always used, and Analysis shares it — a date range or a map means the same
thing on both pages because it is the same code, which is what lets a reader
narrow on one, switch, and trust they are looking at the same matches.

The cumulative line is computed over the *filtered* set, which is the honest
reading of a filtered view: it answers "how did these matches add up", not
"what was my balance on this date". Anything else would need matches the
filters have excluded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

from .engine import MatchView, Unassigned

RANGE_DAYS = {"7d": 7, "30d": 30, "all": None}


@dataclass(frozen=True, slots=True)
class Filters:
    range: str = "30d"
    map: str = "all"
    build: str = "all"
    grouping: str = "match"
    search: str = ""

    @staticmethod
    def from_query(params) -> "Filters":
        get = params.get
        rng = get("range") or "30d"
        grouping = get("group") or "match"
        return Filters(
            range=rng if rng in RANGE_DAYS else "30d",
            map=get("map") or "all",
            build=get("build") or "all",
            grouping=grouping if grouping in ("match", "day", "week") else "match",
            search=(get("q") or "").strip(),
        )


def range_start(range_key: str, now: datetime) -> datetime | None:
    """Inclusive lower bound for a range, or None for all time."""
    days = RANGE_DAYS.get(range_key)
    if days is None:
        return None
    start = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return start - timedelta(days=days - 1)


def match_predicate(filters: Filters, now: datetime):
    start = range_start(filters.range, now)
    needle = filters.search.lower()

    def keep(match: MatchView) -> bool:
        if start is not None and match.started_at.astimezone() < start:
            return False
        if filters.map != "all" and match.map != filters.map:
            return False
        if filters.build != "all":
            unknown = not match.build
            if (not unknown) if filters.build == "unknown" else match.build != filters.build:
                return False
        if needle:
            haystack = " ".join(
                filter(
                    None,
                    [
                        match.map or "",
                        match.build or "",
                        match.match_key,
                        match.started_at.astimezone().strftime("%d/%m/%Y"),
                    ],
                )
            ).lower()
            if needle not in haystack:
                return False
        return True

    return keep


def filter_matches(
    matches: Sequence[MatchView], filters: Filters, now: datetime | None = None
) -> list[MatchView]:
    keep = match_predicate(filters, now or datetime.now(timezone.utc))
    return [m for m in matches if keep(m)]


def totals_of(matches: Iterable[MatchView]) -> dict:
    unassigned = Unassigned()
    kit = earned = other = profit = lives = count = 0
    duration = 0.0
    for match in matches:
        unassigned = unassigned.add(match.unassigned)
        kit += match.kit_cost
        earned += match.earned
        other += match.other_outflow
        profit += match.profit
        lives += len(match.lives)
        duration += match.duration_sec
        count += 1
    return {
        "matches": count,
        "lives": lives,
        "duration_sec": duration,
        "kit_cost": kit,
        "earned": earned,
        "other_outflow": other,
        "spent": kit + other,
        "unassigned": unassigned.as_json(),
        "profit": profit,
    }


def _start_of_day(value: datetime) -> datetime:
    return value.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)


def _start_of_week(value: datetime) -> datetime:
    """Monday-based, matching how a week is read in most of the world."""
    day = _start_of_day(value)
    return day - timedelta(days=day.weekday())


def _bucket_label(anchor: datetime, grouping: str) -> str:
    # Built by hand rather than with a `%-d`/`%#d` format: the no-pad day is
    # spelled differently on every platform and this runs on all of them.
    day = f"{anchor.day} {anchor.strftime('%b')}"
    return f"Week of {day}" if grouping == "week" else day


def build_buckets(matches: Sequence[MatchView], grouping: str) -> list[dict]:
    """Group the filtered matches and run the cumulative total through them.

        cumulative[n] = cumulative[n - 1] + bucket[n].profit
    """
    ordered = sorted(matches, key=lambda m: m.started_at)
    groups: dict[str, list[MatchView]] = {}
    anchors: dict[str, datetime] = {}

    for match in ordered:
        if grouping == "match":
            key, anchor = match.match_key, match.started_at
        elif grouping == "day":
            anchor = _start_of_day(match.started_at)
            key = f"day:{anchor.isoformat()}"
        else:
            anchor = _start_of_week(match.started_at)
            key = f"week:{anchor.isoformat()}"
        groups.setdefault(key, []).append(match)
        anchors.setdefault(key, anchor)

    running = 0
    buckets = []
    for key, members in groups.items():
        totals = totals_of(members)
        running += totals["profit"]
        maps = {m.map for m in members}
        builds = {m.build for m in members}
        buckets.append(
            {
                "key": key,
                "label": _bucket_label(anchors[key], grouping),
                "started_at": anchors[key].isoformat(),
                "match_keys": [m.match_key for m in members],
                **totals,
                "cumulative_profit": running,
                # Only when every match in the bucket agrees: an aggregate that
                # claims one map while holding three is worse than claiming none.
                "map": maps.pop() if len(maps) == 1 else None,
                "build": builds.pop() if len(builds) == 1 else None,
            }
        )

    # Two matches the same evening would both read "19 Sep", which tells the
    # reader nothing about which bar is which. Fall back to the clock only when
    # the date alone is genuinely ambiguous, so the common case keeps the
    # shorter label.
    if grouping == "match":
        labels = [b["label"] for b in buckets]
        if len(set(labels)) != len(labels):
            for bucket, match in zip(buckets, ordered):
                bucket["label"] = match.started_at.astimezone().strftime("%H:%M")

    return buckets


def build_markers(buckets: Sequence[dict]) -> list[dict]:
    """Where the game build changed, chronologically.

    Only a transition between two *known* builds counts. A run of unknown
    buckets is passed over without resetting what the last known build was, so
    a gap in metadata cannot manufacture a boundary.
    """
    markers = []
    last_known: str | None = None
    for index, bucket in enumerate(buckets):
        build = bucket.get("build")
        if not build:
            continue
        if last_known is not None and build != last_known:
            markers.append({"bucket_index": index, "build": build, "label": f"CL {build}"})
        last_known = build
    return markers


def covered_range(matches: Sequence[MatchView]) -> dict | None:
    """The span the filtered set actually covers, for the date caption.

    Not the same as the span the filter asks for: "30 days" with three matches
    in it covers three days, and saying so is the difference between an
    empty-looking chart being confusing and being obvious.
    """
    if not matches:
        return None
    times = [m.started_at for m in matches]
    return {"from": min(times).isoformat(), "to": max(times).isoformat()}


def best_match_key(matches: Sequence[MatchView]) -> str | None:
    """The largest profit in dollars — never the biggest ratio."""
    if len(matches) < 2:
        return None
    best = max(matches, key=lambda m: m.profit)
    return best.match_key if best.profit > 0 else None
