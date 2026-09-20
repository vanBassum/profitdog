"""The Python domain must agree with the TypeScript it replaced, to the dollar.

## Why these numbers are hard-coded

Every figure below was produced by the browser implementation — `kit.ts`,
`ledger.ts`, `annotations.ts` — against the same two recorded sessions, and
read out of it before it was deleted. They are not "what the port currently
returns"; they are what the thing being replaced returned.

That distinction is the entire value of this file. A port checked against its
own output tests nothing. A port checked against its predecessor's output is
the only evidence that moving the ledger out of the browser did not quietly
change what a kit cost, which lives broke even, or how much money a session
made. Every one of those numbers had already been argued over against real
matches; re-deriving them from scratch in a second language is exactly where
they would have silently drifted.

If a future ruleset legitimately changes any of these, it does so as `v2` and
this file keeps passing, because these matches are still read under `v1`. That
is what ruleset versioning is for, and this test is what proves it works.
"""

from __future__ import annotations

import uuid

from pathlib import Path

import pytest

from profitdog_server.db import open_database
from profitdog_server.domain import rulesets
from profitdog_server.domain.analysis import METRICS, analyse, select_points
from profitdog_server.domain.engine import derive_match, load_readings
from profitdog_server.importer import import_file, match_key_for_file
from .conftest import TEST_DATABASE_URL

FIXTURES = (
    Path(__file__).resolve().parents[1]
    / "ui" / "src" / "lib" / "__fixtures__"
)

# life -> (kit_cost, earned, other_outflow, profit, duration, break_even)
EUROPE = {
    1: (3260, 2230, 0, -1030, 246.3, None),
    2: (3660, 250, 90, -3500, 170.2, None),
    3: (390, 4100, 35, 3675, 498.6, 198.3),
    4: (3260, 2130, 0, -1130, 438.5, None),
    5: (640, 6473, 175, 5658, 564.6, 352.4),
    6: (5260, 15664, 6375, 4029, 1025.3, 827.1),
}
KAVKAZI = {
    1: (5260, 11420, 470, 5690, 983.2, 646.8),
    2: (2990, 4050, 0, 1060, 414.5, 414.5),
    3: (1870, 7445, 70, 5505, 510.6, 196.3),
    4: (3260, 13554, 400, 9894, 668.8, 420.5),
    5: (3110, 5440, 35, 2295, 494.6, 440.5),
}
EXPECTED = {"session-europe.csv": EUROPE, "session-kavkazi.csv": KAVKAZI}


@pytest.fixture(scope="module")
def imported():
    """Both recorded sessions, imported once for the whole module."""
    db = open_database(TEST_DATABASE_URL, schema="test_" + uuid.uuid4().hex[:16])
    reports = {}
    for name in EXPECTED:
        reports[name] = import_file(db, FIXTURES / name)
    try:
        yield db, reports
    finally:
        db.drop_schema()
        db.close()


def match_row(db, name: str):
    return db.query_one(
        "SELECT id, match_key, started_at, ended_at, map, faction, build, closed"
        " FROM matches WHERE match_key = %s",
        (match_key_for_file(Path(name)),),
    )


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_every_life_matches_the_typescript(imported, name):
    db, _ = imported
    view = derive_match(db, match_row(db, name), with_ledger=True)
    expected = EXPECTED[name]

    assert len(view.lives) == len(expected), "different number of lives"
    for life in view.lives:
        kit, earned, other, profit, duration, break_even = expected[life.life_number]
        where = f"{name} life {life.life_number}"
        assert life.kit_cost == kit, f"{where}: kit cost"
        assert life.earned == earned, f"{where}: earned"
        assert life.other_outflow == other, f"{where}: other outflow"
        assert life.profit == profit, f"{where}: profit"
        assert life.duration_sec == pytest.approx(duration, abs=0.05), f"{where}: length"
        if break_even is None:
            assert life.break_even_sec is None, f"{where}: broke even but should not"
        else:
            assert life.break_even_sec == pytest.approx(break_even, abs=0.05), (
                f"{where}: break-even"
            )


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_the_books_balance_on_real_data(imported, name):
    db, _ = imported
    view = derive_match(db, match_row(db, name), with_ledger=True)
    entry = rulesets.select(view.build, view.started_at)
    assert entry.module.ledger_invariant_errors(view.ledger) == []

    # The two independent routes to the same number: the decomposition, and
    # the curve's own end-to-end movement.
    readings = load_readings(db, view.id)
    assert view.profit == readings[-1].cash - readings[0].cash
    assert view.ledger.life_net + view.ledger.adjustment_total == view.profit


def test_the_per_life_identity_holds_everywhere(imported):
    db, _ = imported
    for name in EXPECTED:
        for life in derive_match(db, match_row(db, name)).lives:
            assert life.profit == life.earned - life.kit_cost - life.other_outflow


def test_eleven_lives_across_the_two_sessions(imported):
    db, _ = imported
    lives = [
        life
        for name in EXPECTED
        for life in derive_match(db, match_row(db, name)).lives
    ]
    assert len(lives) == 11

    # Three of them never earned their kit back. That count is what the
    # Analysis page's break-even exclusions are measured against, and it is the
    # number the TypeScript produced.
    never = [l for l in lives if l.has_kit and l.break_even_sec is None]
    assert len(never) == 3
    for life in never:
        assert life.earned < life.kit_cost


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------


def test_a_correction_moves_the_split_but_never_the_profit(imported):
    db, _ = imported
    row = match_row(db, "session-europe.csv")
    before = {l.life_number: l for l in derive_match(db, row).lives}
    target = max(before.values(), key=lambda l: l.kit_cost)

    after = {
        l.life_number: l
        for l in derive_match(db, row, overrides={target.life_number: 1}).lives
    }
    corrected = after[target.life_number]
    original = before[target.life_number]

    assert corrected.kit_cost == 1, "the correction did not reach the derivation"
    # An override re-labels spending; it cannot invent any.
    assert corrected.other_outflow == original.other_outflow + (
        original.kit_cost - corrected.kit_cost
    )
    assert corrected.profit == original.profit
    assert corrected.earned == original.earned
    assert corrected.cost_confidence == "manual"

    # And the facts are untouched: a third derivation with no override is
    # identical to the first.
    again = {l.life_number: l for l in derive_match(db, row).lives}
    assert again[target.life_number].kit_cost == original.kit_cost


def test_a_correction_larger_than_the_life_spent_is_clamped(imported):
    db, _ = imported
    row = match_row(db, "session-kavkazi.csv")
    original = derive_match(db, row).lives[0]
    observed = original.kit_cost + original.other_outflow

    corrected = derive_match(db, row, overrides={1: 10_000_000}).lives[0]
    # The one claim the curve can refuse: you cannot have spent more than it
    # recorded leaving the account.
    assert corrected.kit_cost == observed
    assert corrected.other_outflow == 0
    assert corrected.profit == original.profit


# ---------------------------------------------------------------------------
# Analysis, over the same lives
# ---------------------------------------------------------------------------


def test_break_even_excludes_the_unrecovered_and_says_so(imported):
    db, _ = imported
    lives = [
        life
        for name in EXPECTED
        for life in derive_match(db, match_row(db, name)).lives
    ]

    selection = select_points(lives, METRICS["kitCost"], METRICS["breakEven"])
    assert len(selection.plotted) == 8
    assert selection.excluded_count == 3
    assert selection.summary() == (
        "Left out: 3 lives that never earned their kit back."
    )

    # The same lives plot perfectly well on axes that do have values for them:
    # the exclusion belongs to the metric, not to the life.
    elsewhere = select_points(lives, METRICS["kitCost"], METRICS["earned"])
    assert len(elsewhere.plotted) == 11
    assert elsewhere.excluded_count == 0


def test_the_correlation_reads_the_way_it_used_to(imported):
    db, _ = imported
    lives = [
        life
        for name in EXPECTED
        for life in derive_match(db, match_row(db, name)).lives
    ]

    result = analyse(lives, "kitCost", "earned")
    assert result["correlation"]["n"] == 11
    assert result["correlation"]["r"] == pytest.approx(0.42, abs=0.005)
    assert result["summary"]["reading"] == (
        "Higher kit cost corresponded with higher earnings."
    )
    assert result["comparable"] is True

    # Life length against earnings was the strong one.
    survival = analyse(lives, "lifeLength", "earned")
    assert survival["correlation"]["r"] == pytest.approx(0.91, abs=0.005)
    assert survival["comparable"] is False, "dollars against seconds has no diagonal"


def test_no_wording_anywhere_claims_causation(imported):
    db, _ = imported
    lives = [
        life
        for name in EXPECTED
        for life in derive_match(db, match_row(db, name)).lives
    ]
    import re

    causal = re.compile(
        r"\b(causes?|caused|because|leads? to|makes?|means that|proves?|significant|guarantee)\b",
        re.I,
    )
    for x in METRICS:
        for y in METRICS:
            summary = analyse(lives, x, y)["summary"]
            text = " ".join(filter(None, [summary["value"], summary["reading"], summary["caveat"]]))
            assert not causal.search(text), f"{x} vs {y} overclaimed: {text}"
