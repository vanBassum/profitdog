"""Choosing which rules a match is read under.

The scenario this exists for: an Early Access patch changes the economy. Every
match played before it must keep reading under the old rules, forever, with no
migration and no rewrite — because the facts did not change, only the game did.

A second ruleset is registered here rather than shipped, deliberately. No
economy change has been observed yet, so inventing a `v2` in production code
would be putting a fiction where the real one will go. The mechanism is what is
under test, and a test-local ruleset exercises it completely.
"""

from __future__ import annotations

import uuid

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from profitdog_server.db import open_database
from profitdog_server.domain import rulesets
from profitdog_server.domain.engine import derive_match
from profitdog_server.domain.rulesets import v1
from profitdog_server.importer import import_file, match_key_for_file
from .conftest import TEST_DATABASE_URL
from tests.test_domain_parity import EXPECTED, FIXTURES

PATCH_DAY = datetime(2026, 10, 1, tzinfo=timezone.utc)


def make_v2():
    """A stand-in for the next economy patch.

    It differs from v1 in one number — how long after an instalment a further
    fall still counts as the same kit purchase — which is exactly the kind of
    thing a patch that changes payout timing would invalidate. Everything else
    is v1's, because a real v2 would start as a copy too.
    """
    module = SimpleNamespace(
        VERSION="v2-test",
        CONFIRM_READS=v1.CONFIRM_READS,
        MISS_THRESHOLD=v1.MISS_THRESHOLD,
        LIFE_SPAWN_GRACE_SEC=v1.LIFE_SPAWN_GRACE_SEC,
        STALE_MATCH_GAP_SEC=v1.STALE_MATCH_GAP_SEC,
        MAX_PART_GAP_SEC=600.0,
        build_ledger=_v2_build_ledger,
        ledger_invariant_errors=v1.ledger_invariant_errors,
        apply_kit_override=v1.apply_kit_override,
        detect_kit=v1.detect_kit,
    )
    return module


def _v2_build_ledger(rows):
    """v1's walk, with a much longer instalment window.

    A real v2 differing only in a threshold would look exactly like this, which
    is why `build_ledger` takes the threshold as an argument.
    """
    return v1.build_ledger(rows, max_part_gap=600.0)


@pytest.fixture()
def two_rulesets():
    entry = rulesets.RulesetEntry(
        version="v2-test",
        module=make_v2(),
        builds=frozenset({"41300", "41301"}),
        effective_from=PATCH_DAY,
        note="test double for the next economy patch",
    )
    rulesets.register(entry)
    yield entry
    rulesets.unregister("v2-test")


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def test_build_wins_over_date(two_rulesets):
    # A match played long before the patch, but *carrying* a post-patch build,
    # is read under the new rules. Build is the only thing that actually
    # determines which economy applied; the date is a proxy for it.
    chosen = rulesets.select("41300", datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert chosen.version == "v2-test"


def test_an_old_build_keeps_its_old_rules_after_a_patch(two_rulesets):
    # The whole point. A match from before the patch reads under v1 no matter
    # how long ago it was or how many rulesets have landed since.
    for when in (
        datetime(2026, 9, 19, tzinfo=timezone.utc),
        PATCH_DAY - timedelta(seconds=1),
    ):
        assert rulesets.select(None, when).version == "v1"


def test_date_is_the_fallback_when_no_build_was_recorded(two_rulesets):
    # Every match recorded so far has no build, because nothing local publishes
    # one — so this is the path real data actually takes.
    assert rulesets.select(None, PATCH_DAY).version == "v2-test"
    assert rulesets.select(None, PATCH_DAY + timedelta(days=30)).version == "v2-test"
    assert rulesets.select("", PATCH_DAY - timedelta(days=1)).version == "v1"


def test_an_unclaimed_build_falls_through_to_the_date(two_rulesets):
    # A build nobody has classified yet is not a reason to refuse to read the
    # match; it is a reason to fall back to the weaker signal.
    assert rulesets.select("99999", datetime(2026, 9, 1, tzinfo=timezone.utc)).version == "v1"
    assert rulesets.select("99999", PATCH_DAY + timedelta(days=1)).version == "v2-test"


def test_a_match_older_than_every_ruleset_reads_under_the_earliest(two_rulesets):
    ancient = datetime(1999, 1, 1, tzinfo=timezone.utc)
    # Reading it under the oldest rules available is the least wrong answer;
    # refusing to read it at all is the most.
    assert rulesets.select(None, ancient).version == "v1"


def test_a_naive_timestamp_is_treated_as_utc(two_rulesets):
    assert rulesets.select(None, datetime(2026, 9, 19)).version == "v1"


def test_registering_the_same_version_twice_is_refused(two_rulesets):
    with pytest.raises(ValueError, match="already registered"):
        rulesets.register(two_rulesets)


def test_v1_is_always_available_and_is_the_default():
    assert "v1" in rulesets.versions()
    assert rulesets.DEFAULT_VERSION == "v1"
    assert rulesets.by_version("v1").module is v1
    with pytest.raises(KeyError):
        rulesets.by_version("nope")


# ---------------------------------------------------------------------------
# Selection, end to end
# ---------------------------------------------------------------------------


@pytest.fixture()
def imported_db():
    db = open_database(TEST_DATABASE_URL, schema="test_" + uuid.uuid4().hex[:16])
    for name in EXPECTED:
        import_file(db, FIXTURES / name)
    try:
        yield db
    finally:
        db.drop_schema()
        db.close()


#: Sentinel for "leave this column alone", so that `build=None` can mean
#: "record that we do not know the build" — which is the normal case.
KEEP = object()


def match_row(db, name, build=KEEP, started_at=KEEP):
    key = match_key_for_file(__import__("pathlib").Path(name))
    if build is not KEEP or started_at is not KEEP:
        with db.write() as conn:
            if build is not KEEP:
                conn.execute(
                    "UPDATE matches SET build = %s WHERE match_key = %s", (build, key)
                )
            if started_at is not KEEP:
                conn.execute(
                    "UPDATE matches SET started_at = %s WHERE match_key = %s",
                    (started_at.isoformat(), key),
                )
    return db.query_one(
        "SELECT id, match_key, started_at, ended_at, map, faction, build, closed"
        " FROM matches WHERE match_key = %s",
        (key,),
    )


def test_a_real_match_reports_which_ruleset_read_it(imported_db):
    view = derive_match(imported_db, match_row(imported_db, "session-europe.csv"))
    assert view.ruleset == "v1"


def test_the_same_facts_read_differently_under_different_rules(imported_db, two_rulesets):
    """The mechanism doing its job on real data.

    Same match, same stored samples, nothing migrated — only the lens changes,
    and the kit prices change with it because a longer instalment window
    absorbs outgoings v1 keeps separate.
    """
    name = "session-europe.csv"
    under_v1 = derive_match(imported_db, match_row(imported_db, name))
    assert under_v1.ruleset == "v1"

    under_v2 = derive_match(
        imported_db, match_row(imported_db, name, build="41300")
    )
    assert under_v2.ruleset == "v2-test"

    v1_kits = {l.life_number: l.kit_cost for l in under_v1.lives}
    v2_kits = {l.life_number: l.kit_cost for l in under_v2.lives}
    assert v1_kits != v2_kits, "the two rulesets produced identical kits"

    # And v1's answer is unchanged — the facts were never touched.
    again = derive_match(imported_db, match_row(imported_db, name, build=None))
    assert {l.life_number: l.kit_cost for l in again.lives} == v1_kits


def test_history_can_hold_matches_read_under_different_rulesets(imported_db, two_rulesets):
    """Two matches, two economies, one database. Neither needs the other's rules."""
    old = derive_match(imported_db, match_row(imported_db, "session-europe.csv"))
    new = derive_match(
        imported_db, match_row(imported_db, "session-kavkazi.csv", build="41301")
    )
    assert old.ruleset == "v1"
    assert new.ruleset == "v2-test"
    # Both still balance under the rules that read them.
    for view, version in ((old, "v1"), (new, "v2-test")):
        detail = derive_match(
            imported_db,
            match_row(imported_db,
                      "session-europe.csv" if version == "v1" else "session-kavkazi.csv"),
            with_ledger=True,
        )
        entry = rulesets.by_version(detail.ruleset)
        assert entry.module.ledger_invariant_errors(detail.ledger) == []
