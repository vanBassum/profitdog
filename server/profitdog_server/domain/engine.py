"""From stored facts to answers, on every read.

## Nothing derived is stored

`derive_match` reads a match's samples, picks the ruleset that match was played
under, builds the ledger, applies any manual corrections on top, and returns
the result. It does this every time. Nothing it produces is written back.

That is deliberate, and it is fast enough for one match: the benchmark puts a
full ledger for a single match at about 2ms. Over a *thousand hours* of history
it was not — 1,351 matches took 3.2 seconds to derive together — so
`domain/cache.py` sits in front of this for the many-match reads, keyed on the
ruleset, a hash of that ruleset's own source, and a revision of the facts and
corrections that went in. `bench.py` is what decided that, not taste.

This function stayed exactly as it was. The cache calls it on a miss and stores
what it returns, so there is still one implementation of the derivation and the
property that matters is unchanged: every conclusion on screen is reproducible
from facts that never change, under a ruleset chosen per match. Delete every
cached row and the answers are identical — `tests/test_cache.py` asserts it.

## Corrections are applied last, and separately

An override is not a fact and not a derivation. It arrives here after the
ledger is built, re-labels spending inside one life, and cannot move that
life's profit — the curve already said how much money left the account. See
`apply_kit_override`. Keeping it to the end means a rule change does not have
to know corrections exist, and a correction does not have to be re-entered
when the rules change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from ..normalize import display_map_name, faction_name, normalize_map, parse_ts
from . import rulesets
from .rulesets.v1 import Adjustment, LifeLedger, Reading, SessionLedger


# ---------------------------------------------------------------------------
# Views — what the API hands out
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Unassigned:
    """Movement belonging to no life, summed separately by sign.

    Split rather than netted because the *name* depends on the split: all
    positive is "Unassigned income", both signs is "Unassigned movement", and a
    total of +$1,600 cannot tell those apart. Collapsing +$2,000 and −$400 into
    one figure hides the outflow completely.
    """

    positive: int = 0
    negative: int = 0
    total: int = 0
    count: int = 0

    @staticmethod
    def of(adjustments: Sequence[Adjustment]) -> "Unassigned":
        positive = sum(a.amount for a in adjustments if a.amount > 0)
        negative = sum(a.amount for a in adjustments if a.amount < 0)
        return Unassigned(
            positive=positive,
            negative=negative,
            total=positive + negative,
            count=len(adjustments),
        )

    def add(self, other: "Unassigned") -> "Unassigned":
        return Unassigned(
            self.positive + other.positive,
            self.negative + other.negative,
            self.total + other.total,
            self.count + other.count,
        )

    def as_json(self) -> dict[str, int]:
        return {
            "positive": self.positive,
            "negative": self.negative,
            "total": self.total,
            "count": self.count,
        }


@dataclass(frozen=True, slots=True)
class LifeView:
    """One life, flattened for a table, a chart or a correlation."""

    id: str
    match_key: str
    match_started_at: datetime
    started_at: datetime | None
    map: str | None
    build: str | None
    life_number: int
    kit_cost: int
    earned: int
    other_outflow: int
    profit: int
    duration_sec: float
    has_kit: bool
    break_even_sec: float | None
    cost_confidence: str

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "match_key": self.match_key,
            "match_started_at": self.match_started_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "map": self.map,
            "map_display": display_map_name(self.map),
            "build": self.build,
            "life_number": self.life_number,
            "kit_cost": self.kit_cost,
            "earned": self.earned,
            "other_outflow": self.other_outflow,
            "spent": self.kit_cost + self.other_outflow,
            "profit": self.profit,
            "duration_sec": self.duration_sec,
            "has_kit": self.has_kit,
            "break_even_sec": self.break_even_sec,
            "cost_confidence": self.cost_confidence,
        }


@dataclass(frozen=True, slots=True)
class MatchView:
    """One match, fully derived."""

    id: int
    match_key: str
    started_at: datetime
    ended_at: datetime | None
    map: str | None
    faction: str | None
    build: str | None
    closed: bool
    ruleset: str
    duration_sec: float
    kit_cost: int
    earned: int
    other_outflow: int
    unassigned: Unassigned
    profit: int
    lives: list[LifeView] = field(default_factory=list)
    ledger: SessionLedger | None = None

    @property
    def spent(self) -> int:
        return self.kit_cost + self.other_outflow

    def summary_json(self) -> dict[str, Any]:
        return {
            "match_key": self.match_key,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "map": self.map,
            "map_display": display_map_name(self.map),
            "faction": self.faction,
            "faction_display": faction_name(self.faction),
            "build": self.build,
            "live": not self.closed,
            "ruleset": self.ruleset,
            "lives": len(self.lives),
            "duration_sec": self.duration_sec,
            "kit_cost": self.kit_cost,
            "earned": self.earned,
            "other_outflow": self.other_outflow,
            "spent": self.spent,
            "unassigned": self.unassigned.as_json(),
            "profit": self.profit,
        }

    def detail_json(self) -> dict[str, Any]:
        ledger = self.ledger
        return {
            **self.summary_json(),
            "lives_detail": [life.as_json() for life in self.lives],
            "adjustments": [
                {
                    "id": a.id,
                    "from_sec": a.from_sec,
                    "to_sec": a.to_sec,
                    "amount": a.amount,
                    "kind": a.kind,
                    "confidence": a.confidence,
                    "source": a.source,
                    # The recorded state that justified the classification, when
                    # there was one. Shown rather than summarised: "unclassified"
                    # is a claim about evidence, and the evidence is the answer
                    # to "why do you say that".
                    "rp_state": a.rp_state,
                    "near_life": a.near_life_number,
                    "prev_map": a.prev_map,
                    "next_map": a.next_map,
                }
                for a in (ledger.adjustments if ledger else [])
            ],
            "curve": {
                "initial": ledger.initial_value if ledger else 0,
                "final": ledger.final_value if ledger else 0,
                "net": ledger.curve_net if ledger else 0,
            },
            "kit_marks": [
                {
                    "life": l.life_number,
                    "baseline": l.baseline,
                    "baseline_at_sec": l.baseline_at_sec,
                    "floor": l.floor,
                    "purchase_at_sec": l.purchase_at_sec,
                    "break_even_at_sec": l.break_even_at_sec,
                    "start_sec": l.start_sec,
                    "end_sec": l.end_sec,
                    "spends": [
                        {"at_sec": s.at_sec, "amount": s.amount,
                         "after_kit_sec": s.after_kit_sec}
                        for s in l.spends
                    ],
                }
                for l in (ledger.lives if ledger else [])
            ],
        }


# ---------------------------------------------------------------------------
# Loading facts
# ---------------------------------------------------------------------------


def load_readings(db, match_id: int) -> list[Reading]:
    rows = db.query(
        "SELECT elapsed_sec, cash, life, observed_at FROM cash_samples"
        " WHERE match_id = %s ORDER BY elapsed_sec",
        (match_id,),
    )
    return [
        Reading(
            elapsed_sec=float(r["elapsed_sec"]),
            cash=int(r["cash"]),
            life=int(r["life"]),
            timestamp=r["observed_at"],
        )
        for r in rows
    ]


def load_overrides(db, match_id: int) -> dict[int, float]:
    rows = db.query(
        "SELECT life_number, value FROM overrides"
        " WHERE scope = 'life_kit_cost' AND match_id = %s AND value IS NOT NULL",
        (match_id,),
    )
    return {int(r["life_number"]): float(r["value"]) for r in rows}


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


def derive_match(
    db,
    row,
    *,
    readings: Sequence[Reading] | None = None,
    overrides: dict[int, float] | None = None,
    with_ledger: bool = False,
) -> MatchView:
    """Everything derivable about one match, under its own ruleset."""
    match_id = int(row["id"])
    started_at = parse_ts(row["started_at"]) or datetime.fromtimestamp(0, timezone.utc)
    entry = rulesets.select(row["build"], started_at)
    rules = entry.module

    if readings is None:
        readings = load_readings(db, match_id)
    if overrides is None:
        overrides = load_overrides(db, match_id)

    ledger = rules.build_ledger(readings)
    priced: list[LifeLedger] = []
    for life in ledger.lives:
        value = overrides.get(life.life_number)
        priced.append(rules.apply_kit_override(life, value) if value else life)

    map_name = normalize_map(row["map"])
    lives = [
        LifeView(
            id=f"{row['match_key']}#{life.life_number}",
            match_key=str(row["match_key"]),
            match_started_at=started_at,
            started_at=started_at + _seconds(life.start_sec),
            map=map_name,
            build=row["build"],
            life_number=life.life_number,
            kit_cost=life.kit_cost,
            earned=life.gross_gained,
            other_outflow=life.other_spend,
            profit=life.net,
            duration_sec=max(life.end_sec - life.start_sec, 0.0),
            has_kit=life.kit_cost > 0,
            break_even_sec=life.break_even_in_life_sec,
            cost_confidence=(
                "manual"
                if life.life_number in overrides
                else ("derived" if life.kit_cost > 0 else "unknown")
            ),
        )
        for life in priced
    ]

    unassigned = Unassigned.of(ledger.adjustments)
    kit_cost = sum(l.kit_cost for l in priced)
    other = sum(l.other_spend for l in priced)
    earned = sum(l.gross_gained for l in priced)
    duration = (
        readings[-1].elapsed_sec - readings[0].elapsed_sec if readings else 0.0
    )

    return MatchView(
        id=match_id,
        match_key=str(row["match_key"]),
        started_at=started_at,
        ended_at=parse_ts(row["ended_at"]),
        map=map_name,
        faction=row["faction"],
        build=row["build"],
        closed=bool(row["closed"]),
        ruleset=entry.version,
        duration_sec=max(duration, 0.0),
        kit_cost=kit_cost,
        earned=earned,
        other_outflow=other,
        unassigned=unassigned,
        # The curve's own end-to-end movement, not a re-derivation of it. If
        # these two ever disagreed it would be a bug in the ruleset, which is
        # where the invariant is checked.
        profit=ledger.curve_net,
        lives=lives,
        ledger=ledger if with_ledger else None,
    )


def _seconds(value: float):
    from datetime import timedelta

    return timedelta(seconds=value)


MATCH_COLUMNS = (
    "SELECT id, match_key, started_at, ended_at, map, faction, build, closed"
    " FROM matches"
)


def derive_all(db, rows: Iterable[Any]) -> list[MatchView]:
    """Derive a set of matches, loading their facts in as few queries as it can.

    One query for every sample and one for every override, bucketed in memory,
    rather than two queries per match. At three thousand matches that is the
    difference between six thousand round trips and two.
    """
    rows = list(rows)
    if not rows:
        return []
    ids = [int(r["id"]) for r in rows]

    samples: dict[int, list[Reading]] = {i: [] for i in ids}
    for r in db.query(
        "SELECT match_id, elapsed_sec, cash, life, observed_at FROM cash_samples"
        " WHERE match_id = ANY(%s) ORDER BY match_id, elapsed_sec",
        (ids,),
    ):
        samples[int(r["match_id"])].append(
            Reading(
                elapsed_sec=float(r["elapsed_sec"]),
                cash=int(r["cash"]),
                life=int(r["life"]),
                timestamp=r["observed_at"],
            )
        )

    overrides: dict[int, dict[int, float]] = {i: {} for i in ids}
    for r in db.query(
        "SELECT match_id, life_number, value FROM overrides"
        " WHERE scope = 'life_kit_cost' AND value IS NOT NULL"
        " AND match_id = ANY(%s)",
        (ids,),
    ):
        overrides[int(r["match_id"])][int(r["life_number"])] = float(r["value"])

    return [
        derive_match(
            db, row, readings=samples[int(row["id"])], overrides=overrides[int(row["id"])]
        )
        for row in rows
    ]
