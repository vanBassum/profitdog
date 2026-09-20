"""Ingestion: idempotency, the acknowledgement cursor, and segmentation.

The theme is that the network is allowed to misbehave. Batches may arrive
twice, out of order, with gaps, from a restarted agent, or interleaved with a
server restart — and none of it may change what ends up in the database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from profitdog.server.ingest import Ingestor
from profitdog.server.segmentation import Segmenter


def counts(db) -> dict[str, int]:
    return {
        table: int(db.scalar(f"SELECT COUNT(*) FROM {table}"))
        for table in ("matches", "cash_samples", "source_events", "xp_events", "api_events")
    }


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_the_same_batch_twice_changes_nothing(ingestor, db, agent):
    agent.map("Kavkazi")
    agent.play([0, 0, -5260, -5010, -4850, 1200])
    batch = agent.batch()

    first = ingestor.ingest(batch)
    after_first = counts(db)
    second = ingestor.ingest(batch)

    assert first.accepted == len(batch.facts)
    assert first.duplicates == 0
    assert second.accepted == 0
    assert second.duplicates == len(batch.facts)
    assert counts(db) == after_first, "a redelivery wrote something"
    # And the agent is told the same thing both times, so it can forget.
    assert second.acked_through == first.acked_through


def test_overlapping_batches_keep_only_what_is_new(ingestor, db, agent):
    agent.play([0, 0, -3000, -2000, -1000, 500])
    facts = agent.batch().facts

    ingestor.ingest(agent.batch(facts[:4]))
    before = counts(db)["cash_samples"]
    # The agent never got the ack, so it resends from the beginning.
    ack = ingestor.ingest(agent.batch(facts))

    assert ack.duplicates == 4
    assert ack.accepted == len(facts) - 4
    assert counts(db)["cash_samples"] > before


def test_a_restarted_agent_continues_its_sequence(ingestor, db, agent):
    agent.play([0, 0, -1000, -500])
    ingestor.ingest(agent.drain())

    # New boot, same agent, sequence carries on: exactly what the outbox does.
    agent.boot_id = "boot-2"
    agent.play([200, 900])
    ack = ingestor.ingest(agent.drain())

    assert ack.duplicates == 0
    boots = db.query("SELECT boot_id FROM agent_boots ORDER BY id")
    assert [b["boot_id"] for b in boots] == ["boot-1", "boot-2"]
    # One match, not two: a restart is not a match boundary.
    assert counts(db)["matches"] == 1


# ---------------------------------------------------------------------------
# The acknowledgement cursor
# ---------------------------------------------------------------------------


def test_the_cursor_only_advances_over_contiguous_facts(ingestor, db, agent):
    agent.play([0, 0, -1000, -900, -800, -700, -600, -500])
    facts = agent.batch().facts

    # Deliver with a hole: everything except sequence 4.
    with_gap = [f for f in facts if f.agent_seq != 4]
    ack = ingestor.ingest(agent.batch(with_gap))
    assert ack.acked_through == 3, "acked past a gap"

    # The missing fact arrives. The cursor jumps over everything now contiguous.
    ack = ingestor.ingest(agent.batch([f for f in facts if f.agent_seq == 4]))
    assert ack.acked_through == facts[-1].agent_seq


def test_the_cursor_survives_a_server_restart(ingestor, db, agent, tmp_path):
    agent.play([0, 0, -1000, -900])
    ack = ingestor.ingest(agent.drain())
    assert ack.acked_through > 0

    # A new Ingestor over the same database is a restarted server.
    restarted = Ingestor(db)
    assert restarted.cursor_for(agent.agent_id) == ack.acked_through


def test_an_unknown_agent_has_a_cursor_of_zero(ingestor):
    assert ingestor.cursor_for("never-heard-of-it") == 0


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------


def test_a_match_needs_two_confirmed_reads_to_start(ingestor, db, agent):
    agent.presence(state="playing", cash=0)
    ingestor.ingest(agent.drain())
    assert counts(db)["matches"] == 0, "one read started a match"

    agent.presence(state="playing", cash=0)
    ingestor.ingest(agent.drain())
    assert counts(db)["matches"] == 1


def test_a_flicker_out_of_playing_does_not_end_a_match(ingestor, db, agent):
    """The bug that fragmented real sessions into several files."""
    agent.play([0, 0, -5260, -5000])
    agent.presence(state="", cash=None)          # one flicker
    agent.presence(state="playing", cash=-4800)
    agent.presence(state="playing", cash=-4000)
    ingestor.ingest(agent.drain())

    assert counts(db)["matches"] == 1
    row = db.query_one("SELECT closed FROM matches")
    assert row["closed"] == 0, "a single flicker closed the match"


def test_five_misses_end_the_match_and_a_new_one_starts_after(ingestor, db, agent):
    agent.play([0, 0, -5260, -4000, 1200])
    agent.menu(times=5)
    agent.play([0, 0, -3000, 500])
    ingestor.ingest(agent.drain())

    rows = db.query("SELECT match_key, closed, ended_at FROM matches ORDER BY started_at")
    assert len(rows) == 2
    assert rows[0]["closed"] == 1 and rows[0]["ended_at"]
    assert rows[1]["closed"] == 0, "the second match is still live"


def test_silence_ends_a_match_rather_than_stretching_it(ingestor, db, agent):
    """An agent that dies mid-match leaves no run of menu reads — just a gap."""
    agent.play([0, 0, -5260, -4000])
    agent.tick(3600)  # an hour of nothing
    agent.play([0, 0, -2000, 800])
    ingestor.ingest(agent.drain())

    rows = db.query("SELECT closed FROM matches ORDER BY started_at")
    assert len(rows) == 2, "the outage was absorbed into one match"
    assert rows[0]["closed"] == 1


def test_lives_advance_on_spawns_after_the_insertion(ingestor, db, agent):
    agent.play([0, 0, -5260, -4000])
    agent.spawn()                 # a respawn, well after the start
    agent.play([-9000, -8000])
    agent.spawn()
    agent.play([-12000, -11000])
    ingestor.ingest(agent.drain())

    lives = db.query(
        "SELECT DISTINCT life FROM cash_samples ORDER BY life"
    )
    assert [r["life"] for r in lives] == [1, 2, 3]


def test_the_insertion_spawn_is_not_a_second_life(ingestor, db, agent):
    """The first spawn is spawning into the match, never a respawn."""
    agent.presence(state="playing", cash=0)
    agent.presence(state="playing", cash=0)
    agent.spawn(at=agent.clock)   # lands on the confirmed start, inside the grace
    agent.play([-5260, -4000])
    ingestor.ingest(agent.drain())

    assert [r["life"] for r in db.query("SELECT DISTINCT life FROM cash_samples")] == [1]


def test_faction_waits_for_a_real_team(ingestor, db, agent):
    agent.play([0, 0], faction="unknown")
    agent.play([-5260, -4000], faction="bravo")
    ingestor.ingest(agent.drain())

    assert db.query_one("SELECT faction FROM matches")["faction"] == "bravo"


def test_the_map_lands_on_the_match(ingestor, db, agent):
    agent.map("Kavkazi")
    agent.play([0, 0, -5260])
    ingestor.ingest(agent.drain())
    assert db.query_one("SELECT map FROM matches")["map"] == "Kavkazi"


def test_an_agent_reporting_it_stopped_closes_its_match(ingestor, db, agent):
    agent.play([0, 0, -5260, 1000])
    agent.status("stopped")
    ingestor.ingest(agent.drain())
    assert db.query_one("SELECT closed FROM matches")["closed"] == 1


# ---------------------------------------------------------------------------
# Facts stay facts
# ---------------------------------------------------------------------------


def test_the_raw_money_string_is_kept_beside_the_number_it_parsed_to(ingestor, db, agent):
    """A wrong parser must be fixable against the evidence, not in spite of it."""
    agent.play([0, 0, -5260])
    ingestor.ingest(agent.drain())

    rows = db.query("SELECT cash, raw FROM cash_samples ORDER BY elapsed_sec")
    assert rows[-1]["cash"] == -5260
    assert rows[-1]["raw"] == "-$5,260"
    assert all(r["raw"] for r in rows), "a reading lost its original string"


def test_a_presence_poll_is_stored_in_full_when_anything_but_money_changes(
    ingestor, db, agent
):
    """Ordinary polls are samples; the interesting ones are kept whole.

    Writing every poll to `source_events` as well doubled the database in order
    to record that nothing had changed. What must not be lost is any poll where
    something *did* change - a state transition, a faction assignment - and
    those are still stored verbatim.
    """
    import json

    agent.play([0, 0], faction="unknown")
    agent.play([-5260, -5000, -4000], faction="bravo")
    agent.presence(state="mainmenu")
    ingestor.ingest(agent.drain())

    bodies = [
        json.loads(r["payload"])
        for r in db.query(
            "SELECT payload FROM source_events WHERE kind = 'presence' ORDER BY id"
        )
    ]
    # Far fewer rows than polls, but every transition is there.
    assert len(bodies) < 6
    assert any(b.get("faction") == "unknown" for b in bodies)
    assert any(b.get("faction") == "bravo" for b in bodies)
    assert any(b.get("game_state") == "mainmenu" for b in bodies)
    for body in bodies:
        assert set(body) <= {"game_state", "faction", "profit_loss"}


def test_a_money_string_that_will_not_parse_is_always_kept_whole(ingestor, db, agent):
    agent.presence(state="playing", cash=0)
    agent.presence(state="playing", cash=0)
    agent._emit(
        "presence",
        "rich_presence",
        {"game_state": "playing", "faction": "bravo",
         "profit_loss": "some future format"},
    )
    ingestor.ingest(agent.drain())

    import json

    bodies = [
        json.loads(r["payload"])
        for r in db.query("SELECT payload FROM source_events WHERE kind = 'presence'")
    ]
    assert any(b.get("profit_loss") == "some future format" for b in bodies), (
        "an unparseable reading was dropped instead of kept for a better parser"
    )


def test_xp_is_stored_as_totals_with_a_reproducible_delta(ingestor, db, agent):
    agent.play([0, 0, -1000])
    agent.xp(Wardog=80, Recon=4)
    agent.play([-500])
    agent.xp(Wardog=83, Recon=4)
    ingestor.ingest(agent.drain())

    rows = db.query("SELECT role, total, delta FROM xp_events ORDER BY id")
    by_role = [(r["role"], r["total"], r["delta"]) for r in rows]
    # First reading has no predecessor, so no delta — not a delta of zero.
    assert ("Wardog", 80, None) in by_role
    assert ("Wardog", 83, 3) in by_role
    assert ("Recon", 4, 0) in by_role


def test_three_timestamps_are_kept_apart(ingestor, db, agent):
    """An agent that was offline delivers old observations now."""
    agent.play([0, 0, -5260])
    batch = agent.drain()
    later = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    ingestor.ingest(batch, received_at=later)

    row = db.query_one(
        "SELECT source_ts, observed_at, received_at, ingested_at FROM cash_samples LIMIT 1"
    )
    assert row["observed_at"].startswith("2026-09-19")
    assert row["received_at"] == later
    assert row["received_at"] != row["observed_at"]
    assert row["ingested_at"] >= row["received_at"] or row["ingested_at"] is not None


# ---------------------------------------------------------------------------
# Persist before publishing
# ---------------------------------------------------------------------------


def test_every_published_event_has_its_fact_on_disk(ingestor, db, agent):
    agent.map("Kavkazi")
    agent.play([0, 0, -5260, -4000, 1200])
    ingestor.ingest(agent.drain())

    published = db.query("SELECT seq, kind, match_key, payload FROM api_events ORDER BY seq")
    assert published, "nothing was published"
    assert [r["seq"] for r in published] == sorted(r["seq"] for r in published)

    import json

    samples = {
        (r["elapsed_sec"], r["cash"])
        for r in db.query("SELECT elapsed_sec, cash FROM cash_samples")
    }
    for row in published:
        if row["kind"] != "match.sample":
            continue
        payload = json.loads(row["payload"])
        assert (payload["elapsed_sec"], payload["cash"]) in samples


def test_a_failed_batch_leaves_nothing_behind(ingestor, db, agent, monkeypatch):
    """One transaction: a batch that raises part-way writes none of itself."""
    agent.play([0, 0, -5260, -4000])
    before = counts(db)

    original = Segmenter.feed
    calls = {"n": 0}

    def explode(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] > 2:
            raise RuntimeError("boom")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Segmenter, "feed", explode)
    with pytest.raises(RuntimeError, match="boom"):
        ingestor.ingest(agent.drain())

    assert counts(db) == before, "a failed batch left rows behind"
    assert ingestor.cursor_for(agent.agent_id) == 0
