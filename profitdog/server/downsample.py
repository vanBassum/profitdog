"""Fitting a long curve into a chart without lying about its shape.

## Why not every nth point

A match at two-second polling is a few hundred readings; an hour is 1,800; the
benchmark's longest are longer still. Sending all of them to draw a line a few
hundred pixels wide is waste, but the obvious fix — keep every nth reading —
is the one thing that must not happen here.

This curve's most important features are its *extremes*. A kit purchase is a
single sharp fall, often across one or two readings, and it is the number the
whole application is built to recover. Stride sampling drops exactly those:
take every fourth reading and a kit bought between two of them disappears, the
curve is smooth where the game made a cliff, and the chart shows a life that
never bought anything.

So the curve is bucketed by time, and each bucket contributes its minimum and
its maximum, in the order they occurred. Every trough and every peak survives
at full depth; what is lost is only the wandering in between. The first and
last readings are always kept, so the ends are exact.

This is for *drawing only*. Derivation never sees a decimated curve — the
ledger always reads every stored sample, and `tests/test_api.py` holds it to
that by checking a downsampled response against the full one.
"""

from __future__ import annotations

from typing import Sequence


def downsample(rows: Sequence, limit: int) -> list[dict]:
    """At most `limit` points, preserving every local extreme.

    `rows` are `(elapsed_sec, cash, life)` triples in time order. The result is
    never longer than `limit` and never drops the deepest or highest reading in
    any bucket.
    """
    points = [
        {"t": float(r[0]), "c": int(r[1]), "l": int(r[2])} for r in rows
    ]
    if limit <= 0 or len(points) <= limit:
        return points

    # Two points per bucket (a low and a high), plus the two endpoints.
    buckets = max((limit - 2) // 2, 1)
    span = points[-1]["t"] - points[0]["t"]
    if span <= 0:
        return points[:limit]

    width = span / buckets
    start = points[0]["t"]
    out: list[dict] = [points[0]]
    index = 1
    last = len(points) - 1

    for bucket in range(buckets):
        edge = start + (bucket + 1) * width
        lowest = highest = None
        while index < last and points[index]["t"] < edge:
            point = points[index]
            if lowest is None or point["c"] < lowest["c"]:
                lowest = point
            if highest is None or point["c"] > highest["c"]:
                highest = point
            index += 1
        if lowest is None:
            continue
        if lowest is highest:
            out.append(lowest)
        else:
            # In the order they happened, so the line still reads left to right.
            out.extend(
                [lowest, highest] if lowest["t"] <= highest["t"] else [highest, lowest]
            )

    out.append(points[last])
    return out
