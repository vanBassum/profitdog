"""Ruleset v1 — the rules every match recorded so far was played under.

Ported from the TypeScript that used to run in the browser (`kit.ts`,
`ledger.ts`, `annotations.ts`), unchanged in behaviour. The port is checked
against the same two recorded sessions the TypeScript was, and the numbers it
produces are asserted to the dollar in `tests/test_domain_parity.py`.

Everything in this module is a *conclusion*. Nothing here is stored; it is
recomputed from facts every time it is asked for, which is what allows a later
ruleset to disagree with it about matches that have already happened.

## Segmentation: where one match stops and the next begins

A match starts once `game_state` reports `playing` for `CONFIRM_READS`
consecutive polls, and ends once it does not for `MISS_THRESHOLD`. The
tolerance is not decoration: real data showed `game_state` flickering away from
`playing` for a poll or two during genuinely continuous play, and with zero
tolerance that silently fragmented single matches into several — cut profit
curves, wrong per-match totals.

## Kit detection: the first fall, and what may join it

The obvious rule — the deepest dip below where the life started — is wrong,
because a kit is not the only thing that takes money off the clock. Measured
across two real sessions:

    life 1  $5,260  at life start        kit
    life 1    $470  526s into the life   not a kit
    life 3    $390   12s into the life   a genuinely cheap kit
    life 4  $3,260  122s into the life   a kit, but late

A $390 kit and a $470 non-kit sit in the same range, so no size threshold
separates them; a real kit landed two minutes in, so no opening window catches
them all. What does hold is ordering: a kit is bought *before* you can earn
anything with it. So the purchase begins at the first fall, whatever its size,
and ends when money starts coming in.

Kits can be paid in instalments — observed live: −$3,860 then −$1,400 ten
seconds later — so a further fall joins the purchase if it lands within
`MAX_PART_GAP_SEC`. "Keep taking falls until earnings start" over-corrects: the
same session has a −$175 three minutes after a kit and a −$6,250 nearly two
minutes after another, and absorbing those put a $5,260 kit at $11,510.

## The decomposition, and the money it refuses to attribute

Every reading-to-reading change lands in exactly one bucket: the kit, gross
gains after it, other spending after it, or an *adjustment* — money that moved
outside any life's kit-to-close span and is deliberately left unattributed.

That last bucket is the reason this exists. An earlier model with nowhere to
put unexplained movement lost $1,567 of a real session, and hid a −$6,250
outgoing inside a life's "earnings". Both failures had the same cause, and the
invariants at the bottom of this file are what make them unrepresentable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

# ---------------------------------------------------------------------------
# Constants — the knobs a future ruleset would turn
# ---------------------------------------------------------------------------

VERSION = "v1"

#: Consecutive `playing` reads before a match is considered started.
CONFIRM_READS = 2
#: Consecutive non-`playing` reads before it is considered ended (~10s at 2s).
MISS_THRESHOLD = 5
#: Spawns within this of a match's confirmed start are the insertion itself,
#: not a second life.
LIFE_SPAWN_GRACE_SEC = 3.0
#: A silence wider than this ends a match rather than stretching it across the
#: gap. An agent that dies mid-match produces no run of non-`playing` reads to
#: trip `MISS_THRESHOLD` — just nothing, and then observations hours later.
STALE_MATCH_GAP_SEC = 120.0
#: How long after one instalment another still counts as the same purchase.
#: Observed: parts 10s apart; unrelated outgoings 104s and 180s after. The
#: bound sits comfortably between, and errs toward charging too little rather
#: than too much — an under-priced kit understates its own return, while an
#: over-priced one silently blames the kit for a loss it did not cause.
MAX_PART_GAP_SEC = 30.0


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reading:
    """One reading of the curve, as stored."""

    elapsed_sec: float
    cash: int
    life: int
    state: str | None = None
    map: str | None = None
    timestamp: str | None = None

    def in_play(self) -> bool:
        """A reading is part of a life only while the game says it is played."""
        state = (self.state or "").strip().lower()
        return state in ("", "playing")


# ---------------------------------------------------------------------------
# Kit detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KitPurchase:
    #: Index of the reading immediately before the fall began. -1 means the
    #: fall started from `lead_in` — the previous life's closing balance, which
    #: is the normal case for a respawn.
    from_index: int
    at_index: int
    level: int
    floor: int
    cost: int


def detect_kit(
    readings: Sequence[Reading],
    lead_in: Reading | None = None,
    *,
    max_part_gap: float = MAX_PART_GAP_SEC,
) -> KitPurchase | None:
    """The first contiguous fall, plus any instalment close behind it.

    `lead_in` is the previous life's closing balance, and passing it matters
    more than it looks. On a respawn the kit is deducted *between* the last
    reading of one life and the first of the next, so the drop is not inside
    the new life's own readings at all. Without it, every respawn's kit reads
    as free and the first mid-life outgoing gets charged as the kit instead.
    """
    series = list(readings) if lead_in is None else [lead_in, *readings]
    offset = 0 if lead_in is None else 1
    if len(series) < 2:
        return None

    start = -1
    for i in range(1, len(series)):
        if series[i].cash < series[i - 1].cash:
            start = i
            break
    if start == -1:
        return None

    end = start
    floor = series[start].cash
    last_part_at = series[start].elapsed_sec

    for i in range(start + 1, len(series)):
        # Money coming in means the purchase is over and the life is trading.
        if series[i].cash > series[i - 1].cash:
            break
        if series[i].cash >= floor:
            continue
        # A fall this long after the last instalment is its own event.
        if series[i].elapsed_sec - last_part_at > max_part_gap:
            break
        floor = series[i].cash
        end = i
        last_part_at = series[i].elapsed_sec

    level = series[start - 1].cash
    return KitPurchase(
        from_index=start - 1 - offset,
        at_index=end - offset,
        level=level,
        floor=floor,
        cost=level - floor,
    )


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpendEvent:
    at_sec: float
    timestamp: str | None
    amount: int
    after_kit_sec: float


@dataclass(frozen=True, slots=True)
class Adjustment:
    id: str
    from_sec: float
    to_sec: float
    from_timestamp: str | None
    to_timestamp: str | None
    amount: int
    kind: str
    rp_state: str | None
    prev_map: str | None
    next_map: str | None
    confidence: str
    source: str
    near_life_number: int | None


@dataclass(slots=True)
class LifeLedger:
    life_number: int
    start_sec: float
    end_sec: float
    baseline: int
    baseline_at_sec: float
    floor: int
    purchase_at_sec: float
    has_kit: bool
    kit_cost: int
    gross_gained: int
    other_spend: int
    spends: list[SpendEvent]
    net: int
    closing_value: int
    #: First moment within this life that the balance climbs back to
    #: `baseline`. None when the life ended still under water — earnings from a
    #: later life must never settle an earlier life's kit, and neither may an
    #: adjustment: a life recovers by earning, or not at all.
    break_even_at_sec: float | None = None
    break_even_in_life_sec: float | None = None


@dataclass(slots=True)
class SessionLedger:
    lives: list[LifeLedger] = field(default_factory=list)
    adjustments: list[Adjustment] = field(default_factory=list)
    initial_value: int = 0
    final_value: int = 0
    curve_net: int = 0
    life_net: int = 0
    adjustment_total: int = 0
    kit_cost_total: int = 0
    gross_gained_total: int = 0
    other_spend_total: int = 0


def _group_by_life(rows: Sequence[Reading]) -> list[list[Reading]]:
    """Contiguous runs of in-play rows sharing a life number, in play order."""
    groups: list[list[Reading]] = []
    for row in rows:
        if groups and groups[-1][0].life == row.life:
            groups[-1].append(row)
        else:
            groups.append([row])
    return groups


def _classify(
    span: Sequence[Reading],
    prev_map: str | None,
    next_map: str | None,
    is_trailing: bool,
) -> tuple[str, str, str]:
    """What caused a span, on the evidence in the span itself.

    Deliberately conservative. A span only earns a name when the data contains
    the thing that names it: a recorded non-`playing` state, or a change of
    map. Everything else is `unclassified`, however tempting the story — naming
    it "payout" on the strength of a plausible one is how money got lost the
    first time.
    """
    states = sorted(
        {
            (r.state or "").strip()
            for r in span
            if (r.state or "").strip() and (r.state or "").strip().lower() != "playing"
        }
    )

    if states and is_trailing:
        return (
            "terminal_adjustment",
            "observed",
            f"recorded after play stopped (game_state {', '.join(states)})",
        )
    if prev_map and next_map and prev_map != next_map:
        return (
            "map_transition",
            "observed",
            f"map changed from {prev_map} to {next_map}",
        )
    if states:
        return (
            "unclassified",
            "inferred",
            f"recorded outside playing (game_state {', '.join(states)})",
        )
    return (
        "unclassified",
        "unknown",
        "moved outside any life's kit-to-close span; this file records no "
        "game_state or map change that would say why",
    )


def _make_adjustment(
    ident: str,
    frm: Reading,
    to: Reading,
    span: Sequence[Reading],
    near_life: int | None,
    is_trailing: bool,
) -> Adjustment:
    prev_map = frm.map or None
    next_map = to.map or None
    kind, confidence, source = _classify(span, prev_map, next_map, is_trailing)
    states = sorted(
        {
            (r.state or "").strip()
            for r in span
            if (r.state or "").strip() and (r.state or "").strip().lower() != "playing"
        }
    )
    return Adjustment(
        id=ident,
        from_sec=frm.elapsed_sec,
        to_sec=to.elapsed_sec,
        from_timestamp=frm.timestamp,
        to_timestamp=to.timestamp,
        amount=to.cash - frm.cash,
        kind=kind,
        rp_state=", ".join(states) if states else None,
        prev_map=prev_map,
        next_map=next_map,
        confidence=confidence,
        source=source,
        near_life_number=near_life,
    )


def build_ledger(
    rows: Sequence[Reading], *, max_part_gap: float = MAX_PART_GAP_SEC
) -> SessionLedger:
    """Decompose one session's readings into lives and adjustments.

    `max_part_gap` is a parameter rather than only a constant so that a future
    ruleset differing from this one in nothing but a threshold can reuse the
    walk instead of copying it. A ruleset that changes the *shape* of the walk
    still gets its own module — that is what versioning is for.

    The walk is deliberately literal: it never infers a value it can compute.
    Each life's span runs from the reading immediately before its kit purchase
    to its last reading, and *every* gap between one such span and the next
    becomes an adjustment rather than being absorbed by whichever life it
    touches.
    """
    ledger = SessionLedger()
    if not rows:
        return ledger

    in_play = [r for r in rows if r.in_play()]
    if not in_play:
        return ledger

    groups = _group_by_life(in_play)
    lives: list[LifeLedger] = []
    adjustments: list[Adjustment] = []

    def rows_between(from_sec: float, to_sec: float) -> list[Reading]:
        return [r for r in rows if from_sec <= r.elapsed_sec <= to_sec]

    previous_accounted = rows[0]

    for index, group in enumerate(groups):
        previous_group = groups[index - 1] if index > 0 else None
        previous_last = previous_group[-1] if previous_group else None

        kit = detect_kit(group, previous_last, max_part_gap=max_part_gap)

        # `from_index < 0` means the fall began at the previous life's closing
        # reading — the ordinary respawn case.
        if kit is None:
            baseline_row = group[0]
        elif kit.from_index < 0:
            baseline_row = previous_last or group[0]
        else:
            baseline_row = group[kit.from_index]
        floor_index = 0 if kit is None else max(kit.at_index, 0)
        floor_row = group[floor_index]

        # Anything between the last accounted reading and this life's baseline
        # belongs to no life. Record it, do not donate it.
        if baseline_row.elapsed_sec > previous_accounted.elapsed_sec:
            amount = baseline_row.cash - previous_accounted.cash
            if amount != 0:
                adjustments.append(
                    _make_adjustment(
                        f"adj-{previous_accounted.elapsed_sec}-{baseline_row.elapsed_sec}",
                        previous_accounted,
                        baseline_row,
                        rows_between(
                            previous_accounted.elapsed_sec, baseline_row.elapsed_sec
                        ),
                        group[0].life,
                        False,
                    )
                )

        baseline = baseline_row.cash
        floor = floor_row.cash
        kit_cost = 0 if kit is None else baseline - floor

        gross_gained = 0
        other_spend = 0
        spends: list[SpendEvent] = []
        for i in range(floor_index + 1, len(group)):
            delta = group[i].cash - group[i - 1].cash
            if delta > 0:
                gross_gained += delta
            elif delta < 0:
                other_spend += -delta
                spends.append(
                    SpendEvent(
                        at_sec=group[i].elapsed_sec,
                        timestamp=group[i].timestamp,
                        amount=-delta,
                        after_kit_sec=group[i].elapsed_sec - floor_row.elapsed_sec,
                    )
                )

        last = group[-1]
        life = LifeLedger(
            life_number=group[0].life,
            start_sec=group[0].elapsed_sec,
            end_sec=last.elapsed_sec,
            baseline=baseline,
            baseline_at_sec=baseline_row.elapsed_sec,
            floor=floor,
            purchase_at_sec=floor_row.elapsed_sec,
            has_kit=kit_cost > 0,
            kit_cost=kit_cost,
            gross_gained=gross_gained,
            other_spend=other_spend,
            spends=spends,
            net=gross_gained - kit_cost - other_spend,
            closing_value=last.cash,
        )

        # Break-even: searched only in this life's own readings, and only after
        # its kit was paid for. A later life's payout reaching the same level
        # says nothing about whether this kit ever paid for itself.
        if life.kit_cost > 0:
            for row in group:
                if row.elapsed_sec <= life.purchase_at_sec:
                    continue
                if row.cash >= life.baseline:
                    life.break_even_at_sec = row.elapsed_sec
                    life.break_even_in_life_sec = row.elapsed_sec - life.start_sec
                    break

        lives.append(life)
        previous_accounted = last

    # Rows recorded after the final life stopped being played.
    final_row = rows[-1]
    if final_row.elapsed_sec > previous_accounted.elapsed_sec:
        amount = final_row.cash - previous_accounted.cash
        if amount != 0:
            adjustments.append(
                _make_adjustment(
                    f"adj-tail-{previous_accounted.elapsed_sec}",
                    previous_accounted,
                    final_row,
                    rows_between(previous_accounted.elapsed_sec, final_row.elapsed_sec),
                    groups[-1][0].life,
                    True,
                )
            )

    ledger.lives = lives
    ledger.adjustments = adjustments
    ledger.initial_value = rows[0].cash
    ledger.final_value = final_row.cash
    ledger.curve_net = final_row.cash - rows[0].cash
    ledger.life_net = sum(l.net for l in lives)
    ledger.adjustment_total = sum(a.amount for a in adjustments)
    ledger.kit_cost_total = sum(l.kit_cost for l in lives)
    ledger.gross_gained_total = sum(l.gross_gained for l in lives)
    ledger.other_spend_total = sum(l.other_spend for l in lives)
    return ledger


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


def ledger_invariant_errors(ledger: SessionLedger) -> list[str]:
    """The identities that must hold for the ledger to mean anything.

    Returns human-readable violations, empty when the books balance. Each life
    is checked two independent ways — its own arithmetic, and the curve's — so
    a decomposition that adds up internally but disagrees with the balance the
    game actually ended on is still caught. That is the exact shape of the bug
    that started all of this.
    """
    errors: list[str] = []

    for life in ledger.lives:
        from_parts = life.gross_gained - life.kit_cost - life.other_spend
        if from_parts != life.net:
            errors.append(
                f"life {life.life_number}: gross_gained - kit_cost - other_spend "
                f"= {from_parts}, but net = {life.net}"
            )
        from_curve = life.closing_value - life.baseline
        if from_curve != life.net:
            errors.append(
                f"life {life.life_number}: closing_value - baseline = {from_curve}, "
                f"but net = {life.net}"
            )
        if life.kit_cost < 0:
            errors.append(f"life {life.life_number}: negative kit_cost {life.kit_cost}")
        if life.other_spend < 0:
            errors.append(
                f"life {life.life_number}: negative other_spend {life.other_spend}"
            )
        if life.gross_gained < 0:
            errors.append(
                f"life {life.life_number}: negative gross_gained {life.gross_gained}"
            )

    net_from_totals = (
        ledger.gross_gained_total - ledger.kit_cost_total - ledger.other_spend_total
    )
    if net_from_totals != ledger.life_net:
        errors.append(
            f"sum(net) = {ledger.life_net}, but sum(gross_gained) - sum(kit_cost) "
            f"- sum(other_spend) = {net_from_totals}"
        )

    reconstructed = ledger.life_net + ledger.adjustment_total
    if reconstructed != ledger.curve_net:
        errors.append(
            f"curve_net = {ledger.curve_net}, but sum(net) + sum(adjustments) "
            f"= {reconstructed}"
        )

    return errors


# ---------------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------------


def apply_kit_override(life: LifeLedger, value: float) -> LifeLedger:
    """Re-price one life against a real kit price typed in by hand.

    An override re-labels spending; it cannot invent any. The curve is the
    record of how much money left the account during a life, so a corrected kit
    price moves dollars *between* `kit_cost` and `other_spend` and leaves `net`
    exactly where the curve put it. Saying "the kit was $4,000, not the $5,260
    we read off the fall" is a claim that $1,260 of that fall was something
    else — not a claim that the life kept $1,260 more.

    A price larger than everything the life was seen to spend is the one claim
    the curve can refuse, so it is clamped to that total.
    """
    if value is None or value <= 0:
        return life
    observed = life.kit_cost + life.other_spend
    kit_cost = int(min(value, observed))
    other_spend = observed - kit_cost
    return LifeLedger(
        life_number=life.life_number,
        start_sec=life.start_sec,
        end_sec=life.end_sec,
        baseline=life.baseline,
        baseline_at_sec=life.baseline_at_sec,
        floor=life.floor,
        purchase_at_sec=life.purchase_at_sec,
        has_kit=kit_cost > 0,
        kit_cost=kit_cost,
        gross_gained=life.gross_gained,
        other_spend=other_spend,
        spends=life.spends,
        # Unchanged by construction: gained - kit - other is gained - observed
        # however the two are split.
        net=life.gross_gained - kit_cost - other_spend,
        closing_value=life.closing_value,
        break_even_at_sec=life.break_even_at_sec,
        break_even_in_life_sec=life.break_even_in_life_sec,
    )


RULESET: dict[str, Any] = {
    "version": VERSION,
    "confirm_reads": CONFIRM_READS,
    "miss_threshold": MISS_THRESHOLD,
    "life_spawn_grace_sec": LIFE_SPAWN_GRACE_SEC,
    "stale_match_gap_sec": STALE_MATCH_GAP_SEC,
    "max_part_gap_sec": MAX_PART_GAP_SEC,
    "detect_kit": detect_kit,
    "build_ledger": build_ledger,
    "ledger_invariant_errors": ledger_invariant_errors,
    "apply_kit_override": apply_kit_override,
}
