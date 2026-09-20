"""A cache for derived matches, added because a measurement demanded one.

## The measurement

`profitdog_server.bench` over a thousand hours of play — 1,351 matches, 7,429
lives, 1.34M readings — put a full re-derivation of every match at **3.2
seconds**. One match on its own takes about 2ms, so nothing is slow; there are
simply a lot of them, and the history and analysis pages need all of them at
once. Three seconds to load a page is not a page.

Derive-on-read was the right thing to try first and the right thing to keep
for a single match, which is why `derive_match` is untouched and the cache sits
beside it rather than inside it.

## What makes this cache safe to have

A cache is a second copy of a truth, and the way second copies hurt is by
disagreeing silently. Three properties stop that here:

1. **Every row records what it was built from.** The ruleset version, a hash of
   that ruleset's own source, and a hash of the facts and corrections that went
   in. A row whose key does not match today's inputs is not used — it is
   rebuilt. There is no expiry, no invalidation callback to forget to call, and
   no way to be stale without being detected.

2. **Nothing is only in the cache.** Every row is reproducible from facts.
   `DELETE FROM derived_matches` is a supported operation whose entire cost is
   one slow page, and `tests/test_cache.py` asserts that every answer is
   identical with the cache dropped, cold, and warm.

3. **Open matches are never cached.** A live match changes every two seconds,
   so caching it would mean writing a row per poll to save 2ms. They are
   derived directly, which is also what keeps the live page honest.

## Why the input hash is cheap

Hashing a thousand readings per match would cost more than deriving them.
Readings are immutable and append-only, so `(count, max rowid, sum of cash)`
identifies the set exactly — an append changes all three, and nothing else can
change at all. Corrections *are* mutable, so those go in whole. Both come from
one grouped query across every match rather than a query per match.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from ..db import utc_now
from ..normalize import parse_ts
from . import rulesets
from .engine import LifeView, MatchView, Unassigned, derive_match, load_readings


def input_hash(
    sample_count: int,
    max_sample_id: int,
    cash_sum: int,
    overrides: dict[int, float],
) -> str:
    payload = f"{sample_count}:{max_sample_id}:{cash_sum}:{sorted(overrides.items())}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _encode(view: MatchView) -> str:
    return json.dumps(
        {
            "summary": view.summary_json(),
            "duration_sec": view.duration_sec,
            "kit_cost": view.kit_cost,
            "earned": view.earned,
            "other_outflow": view.other_outflow,
            "profit": view.profit,
            "unassigned": view.unassigned.as_json(),
            "ruleset": view.ruleset,
            "lives": [life.as_json() for life in view.lives],
        },
        separators=(",", ":"),
    )


def _decode(row, payload: dict) -> MatchView:
    started_at = parse_ts(row["started_at"]) or datetime.fromtimestamp(0, timezone.utc)
    lives = [
        LifeView(
            id=life["id"],
            match_key=life["match_key"],
            match_started_at=parse_ts(life["match_started_at"]) or started_at,
            started_at=parse_ts(life["started_at"]),
            map=life["map"],
            build=life["build"],
            life_number=life["life_number"],
            kit_cost=life["kit_cost"],
            earned=life["earned"],
            other_outflow=life["other_outflow"],
            profit=life["profit"],
            duration_sec=life["duration_sec"],
            has_kit=life["has_kit"],
            break_even_sec=life["break_even_sec"],
            cost_confidence=life["cost_confidence"],
        )
        for life in payload["lives"]
    ]
    unassigned = payload["unassigned"]
    return MatchView(
        id=int(row["id"]),
        match_key=str(row["match_key"]),
        started_at=started_at,
        ended_at=parse_ts(row["ended_at"]),
        map=row["map"],
        faction=row["faction"],
        build=row["build"],
        closed=bool(row["closed"]),
        ruleset=payload["ruleset"],
        duration_sec=payload["duration_sec"],
        kit_cost=payload["kit_cost"],
        earned=payload["earned"],
        other_outflow=payload["other_outflow"],
        unassigned=Unassigned(
            unassigned["positive"], unassigned["negative"],
            unassigned["total"], unassigned["count"],
        ),
        profit=payload["profit"],
        lives=lives,
        ledger=None,
    )


class MatchCache:
    """Derives many matches, reusing what has not changed."""

    def __init__(self, db) -> None:
        self.db = db
        self.hits = 0
        self.misses = 0

    # -- inputs ----------------------------------------------------------

    def _revisions(self, ids: Sequence[int]) -> dict[int, tuple[int, int, int]]:
        """`(count, max rowid, sum of cash)` per match, in one query."""
        rows = self.db.query(
            "SELECT match_id, COUNT(*) AS n, MAX(id) AS top, SUM(cash) AS total"
            " FROM cash_samples GROUP BY match_id"
        )
        found = {
            int(r["match_id"]): (int(r["n"]), int(r["top"] or 0), int(r["total"] or 0))
            for r in rows
        }
        return {i: found.get(i, (0, 0, 0)) for i in ids}

    def _overrides(self, ids: Sequence[int]) -> dict[int, dict[int, float]]:
        out: dict[int, dict[int, float]] = {i: {} for i in ids}
        for row in self.db.query(
            "SELECT match_id, life_number, value FROM overrides"
            " WHERE scope = 'life_kit_cost' AND value IS NOT NULL"
        ):
            match_id = int(row["match_id"])
            if match_id in out:
                out[match_id][int(row["life_number"])] = float(row["value"])
        return out

    # -- derivation ------------------------------------------------------

    def derive_all(self, rows: Iterable[Any]) -> list[MatchView]:
        rows = list(rows)
        if not rows:
            return []
        ids = [int(r["id"]) for r in rows]
        revisions = self._revisions(ids)
        overrides = self._overrides(ids)

        cached: dict[int, Any] = {}
        for row in self.db.query(
            "SELECT match_id, ruleset, fingerprint, input_hash, payload"
            " FROM derived_matches"
        ):
            cached[int(row["match_id"])] = row

        views: list[MatchView] = []
        rebuilt: list[tuple] = []
        now = utc_now()

        for row in rows:
            match_id = int(row["id"])
            started_at = parse_ts(row["started_at"]) or datetime.fromtimestamp(
                0, timezone.utc
            )
            entry = rulesets.select(row["build"], started_at)
            count, top, total = revisions[match_id]
            key = input_hash(count, top, total, overrides[match_id])

            hit = cached.get(match_id)
            if (
                hit is not None
                and hit["ruleset"] == entry.version
                and hit["fingerprint"] == entry.fingerprint
                and hit["input_hash"] == key
            ):
                self.hits += 1
                views.append(_decode(row, json.loads(hit["payload"])))
                continue

            self.misses += 1
            view = derive_match(
                self.db,
                row,
                readings=load_readings(self.db, match_id),
                overrides=overrides[match_id],
            )
            views.append(view)
            # A live match is re-derived every time on purpose: it changes
            # every two seconds, so caching it would write a row per poll to
            # save two milliseconds.
            if row["closed"]:
                rebuilt.append(
                    (match_id, entry.version, entry.fingerprint, key, _encode(view), now)
                )

        if rebuilt:
            with self.db.write() as conn, conn.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO derived_matches (match_id, ruleset, fingerprint,"
                    " input_hash, payload, built_at) VALUES (%s,%s,%s,%s,%s,%s)"
                    " ON CONFLICT (match_id) DO UPDATE SET"
                    " ruleset = excluded.ruleset, fingerprint = excluded.fingerprint,"
                    " input_hash = excluded.input_hash, payload = excluded.payload,"
                    " built_at = excluded.built_at",
                    rebuilt,
                )
        return views


def drop(db) -> int:
    """Throw the whole cache away. Supported, and costs one slow page load."""
    with db.write() as conn:
        return conn.execute("DELETE FROM derived_matches").rowcount


def stats(db) -> dict:
    return {
        "rows": int(db.scalar("SELECT COUNT(*) FROM derived_matches") or 0),
        "matches": int(db.scalar("SELECT COUNT(*) FROM matches") or 0),
        "oldest": db.scalar("SELECT MIN(built_at) FROM derived_matches"),
        "rulesets": [
            dict(r)
            for r in db.query(
                "SELECT ruleset, COUNT(*) AS rows FROM derived_matches GROUP BY ruleset"
            )
        ],
    }
