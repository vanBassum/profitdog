"""The database: migrations, integrity, one writer, and surviving a kill -9.

Crash recovery is tested by actually killing a process mid-write rather than by
simulating one. A rolled-back transaction in the same process exercises the
driver; `os._exit` mid-transaction exercises what happens when the machine
takes the server away with a transaction open, which is the case that actually
occurs.

On PostgreSQL that case is, if anything, more interesting than it was on
SQLite: the data now outlives the process entirely, so what is being asserted
is that the *server* dying leaves the *database* with a whole batch or none of
it -- and that the connection's death is what rolls it back, with nothing to
replay and nobody to run a recovery step.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import pytest

from profitdog_server.db import LATEST_VERSION, open_database
from profitdog_server.db.schema import MIGRATIONS, current_version
from .conftest import TEST_DATABASE_URL

REPO = Path(__file__).resolve().parents[1]


def test_migrations_are_numbered_once_and_in_order():
    numbers = [n for n, _ in MIGRATIONS]
    assert numbers == sorted(numbers)
    assert len(set(numbers)) == len(numbers)
    assert numbers[0] == 1


def test_opening_twice_is_idempotent():
    """Migrating an already-migrated database must do nothing at all."""
    name = "test_" + uuid.uuid4().hex[:16]
    first = open_database(TEST_DATABASE_URL, schema=name)
    assert first.schema_version == LATEST_VERSION
    first.close()

    second = open_database(TEST_DATABASE_URL, schema=name)
    try:
        assert second.schema_version == LATEST_VERSION
        applied = second.query(
            "SELECT version FROM schema_migrations ORDER BY version"
        )
        assert [r["version"] for r in applied] == [n for n, _ in MIGRATIONS]
    finally:
        second.drop_schema()
        second.close()


def test_the_database_is_the_one_we_think_it_is(db):
    """No pragmas to check any more, so check what they used to guarantee.

    Under SQLite this asserted `journal_mode=wal` and `foreign_keys=1`, both of
    which were per-connection settings that could silently be off. PostgreSQL
    has no equivalent switch -- durability and referential integrity are not
    optional -- so what is worth pinning instead is that the connection really
    is PostgreSQL, and that the schema landed where this Database was told to
    put it rather than leaking into `public`.
    """
    assert "PostgreSQL" in db.scalar("SELECT version()")
    assert db.scalar("SELECT current_schema()") == db.schema
    tables = db.query(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
        (db.schema,),
    )
    assert "matches" in {r["table_name"] for r in tables}


def test_a_reader_sees_committed_writes_and_not_uncommitted_ones(db):
    with db.write() as conn:
        conn.execute(
            "INSERT INTO agents (agent_id, first_seen_at, last_seen_at)"
            " VALUES ('a', 't', 't')"
        )
        # Still inside the transaction: a separate read connection must not see
        # it yet, which is what makes a half-built batch invisible to the API.
        assert db.scalar("SELECT COUNT(*) FROM agents") == 0
    assert db.scalar("SELECT COUNT(*) FROM agents") == 1


def test_a_failed_write_rolls_back_entirely(db):
    with pytest.raises(RuntimeError):
        with db.write() as conn:
            conn.execute(
                "INSERT INTO agents (agent_id, first_seen_at, last_seen_at)"
                " VALUES ('a', 't', 't')"
            )
            raise RuntimeError("boom")
    assert db.scalar("SELECT COUNT(*) FROM agents") == 0


def test_nested_writes_commit_once(db):
    """A helper needing a transaction can be called from inside one."""
    with db.write() as outer:
        outer.execute(
            "INSERT INTO agents (agent_id, first_seen_at, last_seen_at)"
            " VALUES ('a', 't', 't')"
        )
        with db.write() as inner:
            inner.execute(
                "INSERT INTO agents (agent_id, first_seen_at, last_seen_at)"
                " VALUES ('b', 't', 't')"
            )
        # The inner block must not have committed the outer one's work.
        assert db.scalar("SELECT COUNT(*) FROM agents") == 0
    assert db.scalar("SELECT COUNT(*) FROM agents") == 2


def test_event_sequence_numbers_are_dense_and_increasing(db):
    with db.write() as conn:
        seqs = [db.write_event("t", {"i": i}, conn=conn) for i in range(5)]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == 5
    assert db.latest_seq() == seqs[-1]


def test_trimming_events_leaves_the_newest(db):
    with db.write() as conn:
        for i in range(20):
            db.write_event("t", {"i": i}, conn=conn)
    dropped = db.trim_events(keep=5)
    assert dropped == 15
    assert db.scalar("SELECT COUNT(*) FROM api_events") == 5
    assert db.latest_seq() == 20
    assert db.oldest_seq() == 16


def test_foreign_keys_actually_bite(db):
    """A sample must point at a match that exists."""
    import psycopg

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with db.write() as conn:
            conn.execute(
                "INSERT INTO cash_samples (match_id, elapsed_sec, cash, life,"
                " observed_at, received_at, ingested_at)"
                " VALUES (9999, 0, 0, 1, 't', 't', 't')"
            )


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------


#: Run in a child process so it can be killed for real. Placeholders are
#: substituted rather than `.format`-ed: the script is full of braces.
CRASH_SCRIPT = textwrap.dedent(
    """
    import os, sys
    from datetime import datetime, timedelta, timezone
    for _root in ("__REPO__/server", "__REPO__/protocol"):
        sys.path.insert(0, _root)
    from profitdog_server.db import open_database
    from profitdog_protocol import Fact, FactBatch
    from profitdog_server.ingest import Ingestor

    db = open_database("__URL__", schema="__SCHEMA__")
    ing = Ingestor(db)
    base = datetime(2026, 9, 19, 20, 0, tzinfo=timezone.utc)

    def batch(start, n, boot="b1"):
        facts = []
        for i in range(n):
            seq = start + i
            at = (base + timedelta(seconds=2 * seq)).isoformat()
            facts.append(Fact(
                agent_id="crash-agent", boot_id=boot, agent_seq=seq,
                kind="presence", source="rich_presence",
                observed_at=at, source_ts=at,
                body={"game_state": "playing", "faction": "bravo",
                      "profit_loss": "+$" + str(seq * 10)},
            ))
        return FactBatch(agent_id="crash-agent", boot_id=boot, facts=facts)

    ing.ingest(batch(1, 60))
    print(str(db.latest_seq()) + "|" + str(ing.cursor_for("crash-agent")), flush=True)

    # Die in the middle of the next batch, with the transaction still open.
    import profitdog_server.segmentation as seg
    original = seg.Segmenter.feed
    calls = [0]
    def feed(self, *a, **k):
        calls[0] += 1
        if calls[0] > 20:
            os._exit(9)
        return original(self, *a, **k)
    seg.Segmenter.feed = feed
    ing.ingest(batch(61, 60))
    """
)


def test_a_kill_mid_batch_leaves_a_consistent_database():
    """Kill the server mid-batch; the database must hold all of it or none.

    The child writes one whole batch, reports where it got to, then dies with
    `os._exit` in the middle of the next one -- no `finally`, no rollback, no
    close. PostgreSQL notices the connection has gone and discards the open
    transaction, which is the behaviour being asserted: nothing half-written
    survives, and no published event refers to a fact that does not exist.
    """
    name = "test_" + uuid.uuid4().hex[:16]
    script = (
        CRASH_SCRIPT.replace("__REPO__", str(REPO).replace("\\", "/"))
        .replace("__URL__", TEST_DATABASE_URL)
        .replace("__SCHEMA__", name)
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=180
    )
    assert result.returncode == 9, result.stderr
    before_seq, before_cursor = result.stdout.strip().split("|")

    db = open_database(TEST_DATABASE_URL, schema=name)
    try:
        # WAL replayed, schema intact, and the committed batch is all there.
        assert db.schema_version == LATEST_VERSION
        assert db.latest_seq() == int(before_seq)
        assert db.scalar(
            "SELECT acked_through FROM agents WHERE agent_id = 'crash-agent'"
        ) == int(before_cursor)

        # The killed batch left nothing: no envelopes past the cursor, no
        # samples from it, no events announcing samples that do not exist.
        assert (
            db.scalar(
                "SELECT COUNT(*) FROM ingested_envelopes WHERE agent_seq > %s",
                (int(before_cursor),),
            )
            == 0
        )
        stored = {
            (round(float(r["elapsed_sec"]), 1), int(r["cash"]))
            for r in db.query("SELECT elapsed_sec, cash FROM cash_samples")
        }
        for row in db.query(
            "SELECT payload FROM api_events WHERE kind = 'match.sample'"
        ):
            payload = json.loads(row["payload"])
            assert (round(payload["elapsed_sec"], 1), payload["cash"]) in stored, (
                "an event survived the crash without its fact"
            )
    finally:
        db.drop_schema()
        db.close()


def test_an_agent_resends_what_the_crash_took(tmp_path):
    """The other half of recovery: the agent still holds the lost batch.

    The server acknowledged sequence 60. The agent has deleted up to 60 and
    kept everything after, so the facts the crash destroyed are still on the
    gaming PC waiting to be sent again — which is the entire reason the ack is
    the highest *contiguous* sequence rather than the highest seen.
    """
    from profitdog_agent.outbox import Outbox

    with Outbox(tmp_path / "outbox.sqlite3", boot_id="b1") as box:
        for i in range(120):
            box.append("presence", "rich_presence", {"i": i})
        box.ack(60)
        remaining = [f.agent_seq for f in box.pending(limit=200)]

    assert remaining == list(range(61, 121))
