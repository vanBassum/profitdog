"""The derived cache must be invisible except in the timings.

A cache is a second copy of a truth, and second copies hurt by disagreeing
silently. Every test here is a way of making the inputs move — a new sample, a
correction, a different ruleset, an edit to the rules themselves — with an
assertion that the cached answer moved with them or was not used.

The last test is the one that matters most: dropping the whole cache changes no
answer anywhere. If that holds, the cache can always be thrown away, and a
cache that can always be thrown away cannot become a source of truth by
accident.
"""

from __future__ import annotations

import uuid

from datetime import datetime, timezone
from pathlib import Path

import pytest

from profitdog_server.db import open_database
from profitdog_server.domain import rulesets
from profitdog_server.domain.analysis import analyse
from profitdog_server.domain.cache import MatchCache, drop, input_hash, stats
from profitdog_server.domain.engine import MATCH_COLUMNS, derive_all
from profitdog_server.domain.history import Filters, build_buckets, filter_matches, totals_of
from profitdog_server.importer import import_file
from .conftest import TEST_DATABASE_URL
from tests.test_domain_parity import EXPECTED, FIXTURES


@pytest.fixture()
def loaded():
    db = open_database(TEST_DATABASE_URL, schema="test_" + uuid.uuid4().hex[:16])
    for name in EXPECTED:
        import_file(db, FIXTURES / name)
    try:
        yield db
    finally:
        db.drop_schema()
        db.close()


def rows_of(db):
    return db.query(f"{MATCH_COLUMNS} ORDER BY started_at")


def snapshot(views):
    """Everything a reader could see, flattened for comparison."""
    return [
        (
            v.match_key, v.ruleset, v.profit, v.kit_cost, v.earned, v.other_outflow,
            v.duration_sec, v.unassigned.as_json(),
            [
                (l.id, l.kit_cost, l.earned, l.other_outflow, l.profit,
                 l.duration_sec, l.has_kit, l.break_even_sec, l.cost_confidence)
                for l in v.lives
            ],
        )
        for v in views
    ]


# ---------------------------------------------------------------------------
# Correctness
# ---------------------------------------------------------------------------


def test_a_cached_read_equals_an_uncached_one(loaded):
    rows = rows_of(loaded)
    direct = snapshot(derive_all(loaded, rows))

    cache = MatchCache(loaded)
    cold = snapshot(cache.derive_all(rows))
    warm = snapshot(cache.derive_all(rows))

    assert cold == direct
    assert warm == direct
    assert cache.hits > 0, "the second pass did not use the cache"


def test_dropping_the_cache_changes_no_answer(loaded):
    """The property that makes the cache safe to have at all."""
    rows = rows_of(loaded)
    cache = MatchCache(loaded)
    warm = cache.derive_all(rows)
    now = datetime.now(timezone.utc)

    before = (
        snapshot(warm),
        totals_of(warm),
        build_buckets(filter_matches(warm, Filters(range="all"), now), "day"),
        analyse([l for v in warm for l in v.lives], "kitCost", "earned"),
    )

    dropped = drop(loaded)
    assert dropped > 0
    assert stats(loaded)["rows"] == 0

    rebuilt = MatchCache(loaded).derive_all(rows)
    after = (
        snapshot(rebuilt),
        totals_of(rebuilt),
        build_buckets(filter_matches(rebuilt, Filters(range="all"), now), "day"),
        analyse([l for v in rebuilt for l in v.lives], "kitCost", "earned"),
    )
    assert before == after


def test_a_new_sample_invalidates_the_match_that_gained_it(loaded):
    rows = rows_of(loaded)
    cache = MatchCache(loaded)
    cache.derive_all(rows)
    before = {v.match_key: v.profit for v in cache.derive_all(rows)}

    target = rows[0]
    # `SELECT MAX(elapsed_sec), cash` with no GROUP BY used to work here, on
    # a SQLite extension where a bare column beside MAX() comes from the
    # winning row. PostgreSQL rejects it, and rightly: in standard SQL that
    # query does not name which row the `cash` should come from.
    last = loaded.query_one(
        "SELECT elapsed_sec AS t, cash FROM cash_samples WHERE match_id = %s"
        " ORDER BY elapsed_sec DESC LIMIT 1",
        (int(target["id"]),),
    )
    with loaded.write() as conn:
        conn.execute(
            "INSERT INTO cash_samples (match_id, elapsed_sec, cash, life,"
            " observed_at, received_at, ingested_at) VALUES (%s, %s, %s, 1, 't', 't', 't')",
            (int(target["id"]), float(last["t"]) + 2.0, int(last["cash"]) + 777),
        )

    after = {v.match_key: v.profit for v in cache.derive_all(rows_of(loaded))}
    assert after[target["match_key"]] == before[target["match_key"]] + 777
    # And only that match was rebuilt.
    others = set(before) - {target["match_key"]}
    assert all(after[k] == before[k] for k in others)


def test_a_correction_invalidates_its_match(loaded):
    rows = rows_of(loaded)
    cache = MatchCache(loaded)
    cache.derive_all(rows)
    target = rows[0]
    before = next(
        v for v in cache.derive_all(rows) if v.match_key == target["match_key"]
    )

    with loaded.write() as conn:
        conn.execute(
            "INSERT INTO overrides (scope, match_id, life_number, value, created_at)"
            " VALUES ('life_kit_cost', %s, 1, 1.0, 't')",
            (int(target["id"]),),
        )

    after = next(
        v for v in cache.derive_all(rows) if v.match_key == target["match_key"]
    )
    assert after.lives[0].kit_cost == 1
    assert after.lives[0].cost_confidence == "manual"
    assert after.profit == before.profit, "a correction moved the profit"


def test_editing_a_ruleset_invalidates_everything_built_from_it(loaded, monkeypatch):
    """The failure mode that makes caches untrustworthy, made impossible.

    A bug fixed in `v1.py` without renaming it would otherwise leave every
    cached row confidently stale. The fingerprint is a hash of the ruleset's
    own source, so the edit itself is the invalidation.
    """
    rows = rows_of(loaded)
    cache = MatchCache(loaded)
    cache.derive_all(rows)
    hits_before = cache.hits
    cache.derive_all(rows)
    assert cache.hits > hits_before, "warm pass did not hit"

    entry = rulesets.by_version("v1")
    monkeypatch.setattr(
        type(entry), "fingerprint", property(lambda self: "edited-rules")
    )
    fresh = MatchCache(loaded)
    fresh.derive_all(rows)
    assert fresh.hits == 0, "a rules edit was served from cache"
    assert fresh.misses == len(rows)


def test_a_live_match_is_never_cached(db, ingestor, agent):
    """It changes every two seconds; caching it writes a row per poll."""
    agent.play([0, 0, -5260, -4000, 1200])
    ingestor.ingest(agent.drain())
    assert db.scalar("SELECT COUNT(*) FROM matches WHERE closed = 0") == 1

    cache = MatchCache(db)
    cache.derive_all(rows_of(db))
    cache.derive_all(rows_of(db))
    assert db.scalar("SELECT COUNT(*) FROM derived_matches") == 0
    assert cache.hits == 0

    # Once it ends, it caches like anything else.
    agent.status("stopped")
    ingestor.ingest(agent.drain())
    cache.derive_all(rows_of(db))
    assert db.scalar("SELECT COUNT(*) FROM derived_matches") == 1


def test_the_input_hash_reacts_to_every_input(loaded):
    base = input_hash(100, 500, 12345, {})
    assert base != input_hash(101, 500, 12345, {})   # a sample appended
    assert base != input_hash(100, 501, 12345, {})   # a different sample
    assert base != input_hash(100, 500, 12346, {})   # a value changed
    assert base != input_hash(100, 500, 12345, {1: 2.0})  # a correction added
    assert base == input_hash(100, 500, 12345, {})   # and is stable otherwise


def test_a_cached_row_records_what_it_was_built_from(loaded):
    MatchCache(loaded).derive_all(rows_of(loaded))
    row = loaded.query_one(
        "SELECT ruleset, fingerprint, input_hash, built_at FROM derived_matches"
    )
    assert row["ruleset"] == "v1"
    assert row["fingerprint"] == rulesets.by_version("v1").fingerprint
    assert row["input_hash"] and row["built_at"]
