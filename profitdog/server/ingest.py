"""Taking delivery of facts.

## One batch, one transaction

Everything a batch does — the envelopes it records, the rows it writes, the
matches it opens or closes, the events it publishes, the cursor it advances —
commits together or not at all. There is no state in which the server has
half a batch, and no state in which a client has been told about a sample that
is not on disk.

That last point is the whole reason `api_events` rows are written here rather
than after the commit. Publishing after committing would leave a window where a
crash loses the event; publishing before committing would leave one where a
crash loses the *fact* and the client keeps the event. Writing both in one
transaction removes the window rather than narrowing it, and it is what makes
the sequence number a cursor a client can safely reconnect on.

## Idempotency

`ingested_envelopes` holds `(agent, sequence)` for every envelope ever
accepted. An envelope already in there is dropped without being looked at. That
is what turns at-least-once delivery on the wire into exactly-once effect in
the database — the agent may resend anything, at any time, for any reason, and
the worst case is a wasted round trip.

`cash_samples` carries a second, natural guard on `(match_id, elapsed_sec)`,
which catches the case the first one cannot: a re-import, where the facts have
no agent sequence at all because they came out of a CSV.

## The acknowledgement

The agent is told the highest sequence number held *with no gap behind it*. It
deletes up to there and not one number further. A batch that arrives with a
gap in front of it is stored — the facts are good, there is no reason to throw
them away — but acks only up to the gap, so the agent keeps retrying the
missing part instead of dropping it. When the gap is filled by a later batch,
the cursor jumps past both.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..protocol import Ack, Fact, FactBatch
from .config import API_EVENT_RETENTION
from .db import Database, utc_now
from .segmentation import Pending, Segmenter

log = logging.getLogger("profitdog.server.ingest")

SAMPLE_SQL = (
    "INSERT OR IGNORE INTO cash_samples"
    " (match_id, elapsed_sec, cash, life, raw, agent_ref, boot_ref, agent_seq,"
    "  source_ts, observed_at, received_at, ingested_at, build)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
)
EVENT_SQL = (
    "INSERT INTO source_events"
    " (match_id, kind, source, payload, life, agent_ref, boot_ref, agent_seq,"
    "  source_ts, observed_at, received_at, ingested_at, build)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
)
XP_SQL = (
    "INSERT INTO xp_events"
    " (match_id, role, total, delta, agent_ref, boot_ref, agent_seq,"
    "  source_ts, observed_at, received_at, ingested_at, build)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
)


class Ingestor:
    """Accepts fact batches from agents. The only writer of facts."""

    def __init__(self, db: Database, segmenter: Segmenter | None = None) -> None:
        self.db = db
        self.segmenter = segmenter or Segmenter(db)
        self.segmenter.restore()
        #: Called after a batch commits, with the api_event rows it published.
        #: Set by the API layer to fan out to WebSocket clients — after the
        #: commit, never during it.
        self.on_published = None
        #: The published log is a replay buffer, not history. Everything in it
        #: is derivable from the facts, so it is trimmed to a bounded window
        #: and a client that falls further behind refetches a snapshot. Left
        #: untrimmed it grew to a quarter of the database.
        self.retention = API_EVENT_RETENTION
        self._since_trim = 0

    # -- agent bookkeeping ----------------------------------------------

    def _agent_ref(self, conn, agent_id: str, label: str | None, now: str) -> int:
        row = conn.execute(
            "SELECT id FROM agents WHERE agent_id = ?", (agent_id,)
        ).fetchone()
        if row is not None:
            conn.execute(
                "UPDATE agents SET last_seen_at = ?, label = COALESCE(?, label)"
                " WHERE id = ?",
                (now, label, row[0]),
            )
            return int(row[0])
        cursor = conn.execute(
            "INSERT INTO agents (agent_id, label, first_seen_at, last_seen_at,"
            " acked_through) VALUES (?, ?, ?, ?, 0)",
            (agent_id, label, now, now),
        )
        log.info("first contact from agent %s (%s)", agent_id, label or "unlabelled")
        return int(cursor.lastrowid)

    def _boot_ref(self, conn, agent_ref: int, boot_id: str, now: str) -> int:
        row = conn.execute(
            "SELECT id FROM agent_boots WHERE agent_ref = ? AND boot_id = ?",
            (agent_ref, boot_id),
        ).fetchone()
        if row is not None:
            conn.execute(
                "UPDATE agent_boots SET last_seen_at = ? WHERE id = ?", (now, row[0])
            )
            return int(row[0])
        cursor = conn.execute(
            "INSERT INTO agent_boots (agent_ref, boot_id, first_seen_at, last_seen_at)"
            " VALUES (?, ?, ?, ?)",
            (agent_ref, boot_id, now, now),
        )
        conn.execute(
            "UPDATE agents SET last_boot_id = ? WHERE id = ?", (boot_id, agent_ref)
        )
        return int(cursor.lastrowid)

    # -- the cursor ------------------------------------------------------

    def _advance_cursor(self, conn, agent_ref: int, current: int) -> int:
        """Walk forward over contiguous sequence numbers we hold.

        Bounded: a very long contiguous run is acknowledged a chunk at a time,
        over successive batches, rather than in one unbounded scan. The agent
        keeps a little longer than strictly necessary, which costs it nothing.
        """
        rows = conn.execute(
            "SELECT agent_seq FROM ingested_envelopes"
            " WHERE agent_ref = ? AND agent_seq > ? ORDER BY agent_seq LIMIT 50000",
            (agent_ref, current),
        ).fetchall()
        acked = current
        for (seq,) in rows:
            if seq != acked + 1:
                break
            acked = seq
        return acked

    def cursor_for(self, agent_id: str) -> int:
        value = self.db.scalar(
            "SELECT acked_through FROM agents WHERE agent_id = ?", (agent_id,)
        )
        return int(value or 0)

    # -- ingestion -------------------------------------------------------

    def ingest(self, batch: FactBatch, *, received_at: str | None = None) -> Ack:
        received_at = received_at or utc_now()
        ingested_at = utc_now()
        accepted = 0
        duplicates = 0
        published: list[tuple[int, str, str | None, dict]] = []

        with self.db.write() as conn:
            agent_ref = self._agent_ref(conn, batch.agent_id, batch.label, received_at)
            boot_ref = self._boot_ref(conn, agent_ref, batch.boot_id, received_at)
            pending = Pending()

            for fact in batch.facts:
                inserted = conn.execute(
                    "INSERT OR IGNORE INTO ingested_envelopes"
                    " (agent_ref, agent_seq, kind) VALUES (?, ?, ?)",
                    (agent_ref, fact.agent_seq, fact.kind),
                ).rowcount
                if not inserted:
                    duplicates += 1
                    continue
                accepted += 1
                self.segmenter.feed(
                    conn,
                    fact,
                    agent_ref=agent_ref,
                    boot_ref=boot_ref,
                    received_at=received_at,
                    ingested_at=ingested_at,
                    pending=pending,
                )

            self._flush(conn, pending)
            for kind, match_key, payload in pending.api:
                seq = self.db.write_event(kind, payload, match_key=match_key, conn=conn)
                published.append((seq, kind, match_key, payload))

            current = int(
                conn.execute(
                    "SELECT acked_through FROM agents WHERE id = ?", (agent_ref,)
                ).fetchone()[0]
            )
            acked = self._advance_cursor(conn, agent_ref, current)
            if acked != current:
                conn.execute(
                    "UPDATE agents SET acked_through = ? WHERE id = ?", (acked, agent_ref)
                )

        # Only now — after the commit — does anyone hear about it.
        if published and self.on_published is not None:
            self.on_published(published)

        self._since_trim += len(published)
        if self._since_trim >= self.retention // 4:
            self._since_trim = 0
            self.db.trim_events(self.retention)

        return Ack(
            acked_through=acked,
            accepted=accepted,
            duplicates=duplicates,
            server_seq=published[-1][0] if published else self.db.latest_seq(),
        )

    def _flush(self, conn, pending: Pending) -> None:
        if pending.samples:
            conn.executemany(SAMPLE_SQL, pending.samples)
        if pending.events:
            conn.executemany(EVENT_SQL, pending.events)
        if pending.xp:
            conn.executemany(XP_SQL, pending.xp)

    # -- corrections -----------------------------------------------------

    def set_override(
        self, match_key: str, life_number: int, value: float | None, note: str | None = None
    ) -> dict[str, Any]:
        """Record a manual kit price. Never touches a fact or a derivation."""
        with self.db.write() as conn:
            row = conn.execute(
                "SELECT id FROM matches WHERE match_key = ?", (match_key,)
            ).fetchone()
            if row is None:
                raise KeyError(match_key)
            match_id = int(row[0])
            if value is None:
                conn.execute(
                    "DELETE FROM overrides WHERE scope = 'life_kit_cost'"
                    " AND match_id = ? AND life_number = ?",
                    (match_id, life_number),
                )
            else:
                conn.execute(
                    "INSERT INTO overrides (scope, match_id, life_number, value, note,"
                    " created_at) VALUES ('life_kit_cost', ?, ?, ?, ?, ?)"
                    " ON CONFLICT(scope, match_id, life_number) DO UPDATE SET"
                    " value = excluded.value, note = excluded.note,"
                    " created_at = excluded.created_at",
                    (match_id, life_number, float(value), note, utc_now()),
                )
            payload = {"life": life_number, "value": value}
            seq = self.db.write_event(
                "override.set", payload, match_key=match_key, conn=conn
            )
        if self.on_published is not None:
            self.on_published([(seq, "override.set", match_key, payload)])
        return {"seq": seq, **payload}


def decode_event(row) -> dict[str, Any]:
    return {
        "seq": int(row["seq"]),
        "kind": row["kind"],
        "match_key": row["match_key"],
        "payload": json.loads(row["payload"]),
        "committed_at": row["committed_at"],
    }
