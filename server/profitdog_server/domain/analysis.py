"""Metrics, correlation and the sentence that goes beside them.

Ported from `metrics.ts` and `correlation.ts`, which used to run in the
browser. Moving it here is not only about where the CPU cycles land: the
browser was re-deriving the domain model from raw CSV text on every page load,
which meant two implementations of the same accounting — one in TypeScript, one
now in Python — and two implementations disagree eventually. There is one now,
and the UI renders what it is given.

## Availability is a first-class answer

A metric returns either a number or *a reason there is no number*, never a
silent zero. That distinction is the whole difference between a chart that is
honest and one that is not:

- A life with no readable kit price has no kit cost. Plotting it at $0 invents
  a free kit and drags any correlation toward the origin.
- A life that never earned its kit back has no break-even time. Plotting it at
  the end of the life claims a recovery that never happened — and dropping it
  without saying so quietly restricts every break-even chart to the lives that
  did well, which is survivorship bias on the one metric where it would be
  least visible.

Both are excluded, both are counted, and the reason travels with the count.

## No ratios, ever

A $100 kit returning $1,000 is 900% and a $5,000 kit returning $15,000 is 200%,
so ranking by return recommends the cheap kit while the expensive one brought
home fourteen thousand more dollars. Dollars and seconds only.

## What the wording refuses to say

Pearson's r over these lives is a statement about these lives. Eleven of them
from two evenings cannot establish that spending more causes earning more, so
the wording says "corresponded with" and never "leads to", reports the count it
was computed from, and says outright when there is too little to read.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from .engine import LifeView

# ---------------------------------------------------------------------------
# Why a life might have no value for a metric
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reason:
    id: str
    singular: str
    plural: str

    def phrase(self, count: int) -> str:
        return f"{count} {self.singular if count == 1 else self.plural}"


NO_KIT_PRICE = Reason(
    "no-kit-price", "life with no readable kit price", "lives with no readable kit price"
)
NOT_RECOVERED = Reason(
    "not-recovered",
    "life that never earned its kit back",
    "lives that never earned their kit back",
)
NO_LENGTH = Reason(
    "no-length", "life with no measurable length", "lives with no measurable length"
)


# ---------------------------------------------------------------------------
# The metrics
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Metric:
    key: str
    label: str
    #: "money" | "duration" | "rate". Decides tick formatting and whether an
    #: `x = y` reference line means anything.
    unit: str
    #: Lower-case noun phrase, for the sentence beside the chart. "Earned"
    #: reads as a verb mid-sentence; "earnings" does not.
    phrase: str
    hint: str
    read: Callable[[LifeView], tuple[float | None, Reason | None]]

    def as_json(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "unit": self.unit,
            "phrase": self.phrase,
            "hint": self.hint,
        }


def _ok(value: float) -> tuple[float, None]:
    return value, None


def _no(reason: Reason) -> tuple[None, Reason]:
    return None, reason


def _per_minute(amount: float, duration_sec: float):
    """A life with no measurable length has no rate.

    Dividing by zero would put it at infinity; dividing by "close enough to
    zero" would put it somewhere absurd but finite, which is worse, because it
    would look like data.
    """
    if duration_sec <= 0:
        return _no(NO_LENGTH)
    return _ok(amount / (duration_sec / 60.0))


METRICS: dict[str, Metric] = {
    "kitCost": Metric(
        "kitCost", "Kit cost", "money", "kit cost",
        "The detected loadout cost, or the price you corrected it to",
        lambda l: _ok(l.kit_cost) if l.has_kit else _no(NO_KIT_PRICE),
    ),
    "spent": Metric(
        "spent", "Spent", "money", "spending",
        "Everything classified as leaving the account: kits plus other outflow",
        # Available even when the kit price is not: the curve recorded how much
        # left the account either way. What an unreadable kit costs us is the
        # *split* between kit and other outflow, not the total.
        lambda l: _ok(l.kit_cost + l.other_outflow),
    ),
    "earned": Metric(
        "earned", "Earned", "money", "earnings",
        "Every positive movement attributed to a detected life",
        lambda l: _ok(l.earned),
    ),
    "profit": Metric(
        "profit", "Profit", "money", "profit",
        "Earned minus everything the life spent",
        lambda l: _ok(l.profit),
    ),
    "lifeLength": Metric(
        "lifeLength", "Life length", "duration", "life length",
        "How long the life lasted, from its first reading to its last",
        lambda l: _ok(l.duration_sec),
    ),
    "breakEven": Metric(
        "breakEven", "Break-even time", "duration", "break-even time",
        "How long the life took to earn its kit back — lives that never did are left out",
        # Two different absences, kept apart: no price to recover, versus a
        # price that was never recovered.
        lambda l: (
            _no(NO_KIT_PRICE)
            if not l.has_kit
            else (_no(NOT_RECOVERED) if l.break_even_sec is None else _ok(l.break_even_sec))
        ),
    ),
    "earnedPerMin": Metric(
        "earnedPerMin", "Earned/min", "rate", "earnings per minute",
        "Earned, divided by how long the life lasted",
        lambda l: _per_minute(l.earned, l.duration_sec),
    ),
    "profitPerMin": Metric(
        "profitPerMin", "Profit/min", "rate", "profit per minute",
        "Profit, divided by how long the life lasted",
        lambda l: _per_minute(l.profit, l.duration_sec),
    ),
    "otherOutflow": Metric(
        "otherOutflow", "Other outflow", "money", "other outflow",
        "Negative movement during a life that was not part of its kit",
        lambda l: _ok(l.other_outflow),
    ),
}

#: Dropdown order: the money a life moved, then its clock, then its rates.
METRIC_KEYS = [
    "kitCost", "spent", "earned", "profit", "lifeLength",
    "breakEven", "earnedPerMin", "profitPerMin", "otherOutflow",
]

PRESETS = [
    {"id": "kit-earned", "label": "Kit cost → Earned", "x": "kitCost", "y": "earned"},
    {"id": "kit-survival", "label": "Kit cost → Survival", "x": "kitCost", "y": "lifeLength"},
    {"id": "kit-rate", "label": "Kit cost → Earned/min", "x": "kitCost", "y": "earnedPerMin"},
    {"id": "survival-earned", "label": "Survival → Earned", "x": "lifeLength", "y": "earned"},
    {"id": "spent-profit", "label": "Spent → Profit", "x": "spent", "y": "profit"},
]


def is_comparable(x: Metric, y: Metric) -> bool:
    """Whether an `x = y` line says anything about this pair.

    Only for two different metrics in the same unit. Across units the line
    would be drawing an equality between a dollar and a second, which is not
    false so much as meaningless — and a dashed line through a scatter is read
    as meaningful.
    """
    return x.unit == y.unit and x.key != y.key


# ---------------------------------------------------------------------------
# Selecting points
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Plotted:
    life: LifeView
    x: float
    y: float


@dataclass
class Selection:
    plotted: list[Plotted]
    excluded: list[tuple[Reason, int]]
    excluded_count: int
    total: int

    def summary(self) -> str | None:
        """"Left out: 2 lives with no readable kit price, 1 life that…"

        The running total is deliberately not repeated here — it is a figure of
        its own beside this, and saying three twice reads badly for the common
        case of a single reason.
        """
        if not self.excluded_count:
            return None
        return "Left out: " + ", ".join(r.phrase(n) for r, n in self.excluded) + "."


def select_points(lives: Sequence[LifeView], x: Metric, y: Metric) -> Selection:
    """The lives that can be drawn, and an account of the ones that cannot.

    A life needs a value on *both* axes to be a point. It is attributed to the
    x-axis's reason when both fail, because counting one life twice would make
    the exclusions add up to more lives than were filtered — the sort of
    arithmetic that makes a reader stop trusting the rest of the page.
    """
    plotted: list[Plotted] = []
    counts: dict[str, list] = {}

    for life in lives:
        xv, xr = x.read(life)
        yv, yr = y.read(life)
        if xv is not None and yv is not None:
            plotted.append(Plotted(life, xv, yv))
            continue
        reason = xr or yr
        assert reason is not None
        entry = counts.get(reason.id)
        if entry:
            entry[1] += 1
        else:
            counts[reason.id] = [reason, 1]

    excluded = sorted(
        ((reason, count) for reason, count in counts.values()),
        key=lambda pair: (-pair[1], pair[0].id),
    )
    return Selection(
        plotted=plotted,
        excluded=excluded,
        excluded_count=sum(count for _, count in excluded),
        total=len(lives),
    )


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------

#: How many lives before a pattern is worth describing at all. Not a
#: significance threshold — there is no hypothesis test here, and pretending
#: otherwise is exactly the overreach this module avoids.
MIN_POINTS = 4
#: Below this, |r| is noise at these counts.
FLAT = 0.2
#: Enough lives that the pattern is at least describing a habit.
ENOUGH_TO_NOT_CAVEAT = 12


@dataclass(frozen=True, slots=True)
class Correlation:
    r: float | None
    n: int
    slope: float | None
    intercept: float | None


def correlate(points: Iterable[Plotted]) -> Correlation:
    """Pearson's r and the least-squares line.

    `r` is null when it genuinely has no value rather than when it is merely
    uninteresting: fewer than two pairs, or no spread at all in one variable.
    Six lives that all bought the identical kit say nothing about whether kit
    price matters, and the formula's zero denominator agrees — returning 0
    there would dress "we cannot tell" up as "no relationship".
    """
    pts = list(points)
    n = len(pts)
    if n < 2:
        return Correlation(None, n, None, None)

    mean_x = sum(p.x for p in pts) / n
    mean_y = sum(p.y for p in pts) / n
    sxy = sxx = syy = 0.0
    for p in pts:
        dx = p.x - mean_x
        dy = p.y - mean_y
        sxy += dx * dy
        sxx += dx * dx
        syy += dy * dy

    if sxx == 0:
        return Correlation(None, n, None, None)
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    if syy == 0:
        return Correlation(None, n, slope, intercept)
    return Correlation(sxy / math.sqrt(sxx * syy), n, slope, intercept)


def _format_r(r: float) -> str:
    # A true minus sign, matching every other figure in the app; two decimals,
    # because a third would imply a precision eleven lives do not have.
    return f"r = {'−' if r < 0 else ''}{abs(r):.2f}"


def describe(
    correlation: Correlation, x: Metric, y: Metric, excluded: int = 0
) -> dict[str, str | None]:
    """The sentence that goes next to the number.

    Every branch describes what happened in these matches, never a rule about
    what will happen or why. "Corresponded with" is doing real work: it is the
    difference between reporting an association and claiming a mechanism, and
    with this much data only the former is honest.
    """
    r, n = correlation.r, correlation.n
    caveat = (
        f"Based on {n} {'life' if n == 1 else 'lives'} — a description of these "
        "matches, not a rule."
        if 0 < n < ENOUGH_TO_NOT_CAVEAT
        else None
    )

    if n == 0:
        return {
            "value": None,
            "reading": (
                f"No life in this period has both {x.phrase} and {y.phrase}."
                if excluded
                else "No lives in this period."
            ),
            "caveat": None,
            "direction": "unknown",
        }

    if r is None or n < MIN_POINTS:
        # `slope` is the tell for which axis is flat: the fit needs spread in x,
        # so no fit means x, and a fit with no r means y. Naming the wrong one
        # would send the reader to change the wrong dropdown.
        flat_axis = x.phrase if correlation.slope is None else y.phrase
        return {
            "value": None if r is None else _format_r(r),
            "reading": (
                "Too few lives to read a pattern."
                if n < MIN_POINTS
                else f"Every life here had about the same {flat_axis}, so there is "
                "nothing to compare."
            ),
            "caveat": None,
            "direction": "unknown",
        }

    if abs(r) < FLAT:
        return {
            "value": _format_r(r),
            "reading": f"{x.phrase[:1].upper()}{x.phrase[1:]} and {y.phrase} moved independently.",
            "caveat": caveat,
            "direction": "flat",
        }
    if r > 0:
        return {
            "value": _format_r(r),
            "reading": f"Higher {x.phrase} corresponded with higher {y.phrase}.",
            "caveat": caveat,
            "direction": "up",
        }
    return {
        "value": _format_r(r),
        "reading": f"Higher {x.phrase} corresponded with lower {y.phrase}.",
        "caveat": caveat,
        "direction": "down",
    }


def analyse(lives: Sequence[LifeView], x_key: str, y_key: str) -> dict:
    """Everything the Analysis page needs for one pairing."""
    x = METRICS[x_key]
    y = METRICS[y_key]
    selection = select_points(lives, x, y)
    correlation = correlate(selection.plotted)
    return {
        "x": x.as_json(),
        "y": y.as_json(),
        "comparable": is_comparable(x, y),
        "points": [
            {"x": p.x, "y": p.y, **p.life.as_json()} for p in selection.plotted
        ],
        "correlation": {
            "r": correlation.r,
            "n": correlation.n,
            "trend": (
                None
                if correlation.slope is None
                else {"slope": correlation.slope, "intercept": correlation.intercept}
            ),
        },
        "summary": describe(correlation, x, y, selection.excluded_count),
        "excluded": {
            "count": selection.excluded_count,
            "total": selection.total,
            "groups": [
                {"id": r.id, "count": n, "phrase": r.phrase(n)}
                for r, n in selection.excluded
            ],
            "summary": selection.summary(),
        },
    }
