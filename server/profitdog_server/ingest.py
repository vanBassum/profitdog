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

## Telling clients that derived numbers moved

A sample is not only a point on a curve: it can change the kit price, the
totals, the profit. Those are *derived*, so nothing in the fact stream
announces them, and a client that refetches only on structural news (a match
opening, a life starting, a match ending) would keep showing a stale kit for as
long as the player's balance happened to sit still.

So after the rows are written, every match this batch touched is re-derived and
compared against the last values published for it. If any watched field moved,
`match.derived.updated` goes out with the fields that changed. Clients refetch
that match and nothing else.

`duration_sec` is deliberately not watched: it grows with every single sample
of an open match, so including it would fire the event on every poll and turn
"tell me when something changed" back into polling.

## Whose batch is it

A batch arrives with an agent id in it, but that field is a claim, not a
credential. When the caller authenticated -- which on the HTTP path it always
has -- the `identity` argument carries the agent row and the account its
credential was issued for, and *that* is what the rows are written against. The
claimed agent id is checked against it and a mismatch is refused, so one
person's agent cannot file facts under another person's name by editing a JSON
field.

`identity=None` is for the paths with no HTTP request behind them: the CSV
importer and the benchmark, which are run by hand against a local database.

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

from profitdog_protocol import Ack, Fact, FactBatch
from .auth import AgentIdentity
from .config import API_EVENT_RETENTION
from .db import Database, utc_now
from .domain.engine import MATCH_COLUMNS, Reading, derive_match
from .segmentation import Pending, Segmenter

log = logging.getLogger("profitdog_server.ingest")

#: The derived fields worth waking a client for. `duration_sec` is absent on
#: purpose — see the module docstring.
WATCHED_DERIVED = (
    "kit_cost",
    "earned",
    "other_outflow",
    "spent",
    "profit",
    "lives",
    "unassigned",
)

#: Kinds that already make a client refetch the match. When one of these is
#: going out in the same batch, a derived update alongside it would only buy a
#: second refetch of what the first one is about to fetch anyway.
REFETCHING_KINDS = frozenset(
    {"match.started", "match.ended", "match.meta", "match.life", "xp.gained"}
)

SAMPLE_SQL = (
    "INSERT INTO cash_samples"
    " (match_id, elapsed_sec, cash, life, raw, agent_ref, boot_ref, agent_seq,"
    "  source_ts, observed_at, received_at, ingested_at, build)"
    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
    # Two readings of one match at one offset are the same reading. Named
    # rather than blanket `ON CONFLICT DO NOTHING`, so a collision on any
    # *other* constraint is still an error worth hearing about.
    " ON CONFLICT (match_id, elapsed_sec) DO NOTHING"
)
EVENT_SQL = (
    "INSERT INTO source_events"
    " (match_id, kind, source, payload, life, agent_ref, boot_ref, agent_seq,"
    "  source_ts, observed_at, received_at, ingested_at, build)"
    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
)
XP_SQL = (
    "INSERT INTO xp_events"
    " (match_id, role, total, delta, agent_ref, boot_ref, agent_seq,"
    "  source_ts, observed_at, received_at, ingested_at, build)"
    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
)


class Ingestor:
    """Accepts fact batches from agents. The only writer of facts."""

    def __init__(self, db: Database, segmenter: Segmenter | None = None) -> None:
        self.db = db
        self.segmenter = segmenter or Segmenter(db)
        self.segmenter.restore()
        #: The last derived values published per match id, so a change can be
        #: recognised as a change. Memory only: after a restart the first batch
        #: for a match republishes its numbers, which costs one refetch and is
        #: the safe direction to be wrong in.
        self._last_derived: dict[int, dict[str, Any]] = {}
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

    def _agent_ref(self, conn, agent_id: str, label: str | None, now: str,
                   user_id: int | None = None) -> int:
        row = conn.execute(
            "SELECT id FROM agents WHERE agent_id = %s", (agent_id,)
        ).fetchone()
        if row is not None:
            conn.execute(
                "UPDATE agents SET last_seen_at = %s, label = COALESCE(%s, label)"
                " WHERE id = %s",
                (now, label, row[0]),
            )
            return int(row[0])
        cursor = conn.execute(
            "INSERT INTO agents (agent_id, label, first_seen_at, last_seen_at,"
            " acked_through, user_id) VALUES (%s, %s, %s, %s, 0, %s) RETURNING id",
            (agent_id, label, now, now, user_id),
        )
        new_id = int(cursor.fetchone()[0])
        log.info("first contact from agent %s (%s)", agent_id, label or "unlabelled")
        return new_id

    def _boot_ref(self, conn, agent_ref: int, boot_id: str, now: str) -> int:
        row = conn.execute(
            "SELECT id FROM agent_boots WHERE agent_ref = %s AND boot_id = %s",
            (agent_ref, boot_id),
        ).fetchone()
        if row is not None:
            conn.execute(
                "UPDATE agent_boots SET last_seen_at = %s WHERE id = %s", (now, row[0])
            )
            return int(row[0])
        cursor = conn.execute(
            "INSERT INTO agent_boots (agent_ref, boot_id, first_seen_at, last_seen_at)"
            " VALUES (%s, %s, %s, %s) RETURNING id",
            (agent_ref, boot_id, now, now),
        )
        boot_ref = int(cursor.fetchone()[0])
        conn.execute(
            "UPDATE agents SET last_boot_id = %s WHERE id = %s", (boot_id, agent_ref)
        )
        return boot_ref

    # -- the cursor ------------------------------------------------------

    def _advance_cursor(self, conn, agent_ref: int, current: int) -> int:
        """Walk forward over contiguous sequence numbers we hold.

        Bounded: a very long contiguous run is acknowledged a chunk at a time,
        over successive batches, rather than in one unbounded scan. The agent
        keeps a little longer than strictly necessary, which costs it nothing.
        """
        rows = conn.execute(
            "SELECT agent_seq FROM ingested_envelopes"
            " WHERE agent_ref = %s AND agent_seq > %s ORDER BY agent_seq LIMIT 50000",
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
            "SELECT acked_through FROM agents WHERE agent_id = %s", (agent_id,)
        )
        return int(value or 0)

    # -- ingestion -------------------------------------------------------

    def ingest(
        self,
        batch: FactBatch,
        *,
        identity: AgentIdentity | None = None,
        received_at: str | None = None,
    ) -> Ack:
        received_at = received_at or utc_now()
        ingested_at = utc_now()
        accepted = 0
        duplicates = 0
        published: list[tuple[int, str, str | None, dict, int | None]] = []

        if identity is not None and batch.agent_id != identity.agent_id:
            # The credential says which agent this is. The batch saying
            # otherwise is either a bug or an attempt to file facts under
            # someone else, and neither is worth guessing at.
            raise PermissionError(
                "this credential belongs to agent " + identity.agent_id
            )

        with self.db.write() as conn:
            if identity is not None:
                agent_ref = identity.agent_ref
                user_id: int | None = identity.user_id
                conn.execute(
                    "UPDATE agents SET last_seen_at = %s, label = COALESCE(%s, label)"
                    " WHERE id = %s",
                    (received_at, batch.label, agent_ref),
                )
            else:
                agent_ref = self._agent_ref(
                    conn, batch.agent_id, batch.label, received_at
                )
                owner = conn.execute(
                    "SELECT user_id FROM agents WHERE id = %s", (agent_ref,)
                ).fetchone()
                user_id = None if owner is None or owner[0] is None else int(owner[0])
            boot_ref = self._boot_ref(conn, agent_ref, batch.boot_id, received_at)
            pending = Pending()
            pending.user_id = user_id

            for fact in batch.facts:
                inserted = conn.execute(
                    "INSERT INTO ingested_envelopes"
                    " (agent_ref, agent_seq, kind) VALUES (%s, %s, %s)"
                    " ON CONFLICT (agent_ref, agent_seq) DO NOTHING",
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
            self._note_derived_changes(conn, pending)
            for kind, match_key, payload in pending.api:
                seq = self.db.write_event(
                    kind, payload, match_key=match_key, user_id=user_id, conn=conn
                )
                published.append((seq, kind, match_key, payload, user_id))

            current = int(
                conn.execute(
                    "SELECT acked_through FROM agents WHERE id = %s", (agent_ref,)
                ).fetchone()[0]
            )
            acked = self._advance_cursor(conn, agent_ref, current)
            if acked != current:
                conn.execute(
                    "UPDATE agents SET acked_through = %s WHERE id = %s", (acked, agent_ref)
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

    # -- derived changes -------------------------------------------------

    def _note_derived_changes(self, conn, pending: Pending) -> None:
        """Append `match.derived.updated` for matches whose numbers moved.

        Called inside the batch's transaction and *after* `_flush`, so the
        samples just written are visible. Readings and overrides are read on
        the write connection and handed to `derive_match` directly: the reader
        connection is a separate one and cannot see this transaction's rows
        until it commits.
        """
        touched = {row[0] for row in pending.samples if row[0] is not None}
        if not touched:
            return

        already = {
            key for kind, key, _ in pending.api if kind in REFETCHING_KINDS
        }
        closing = {key for kind, key, _ in pending.api if kind == "match.ended"}

        for match_id in sorted(touched):
            row = conn.execute(
                MATCH_COLUMNS + " WHERE id = %s", (match_id,)
            ).fetchone()
            if row is None:
                continue

            match_key = str(row["match_key"])
            current = self._derived_snapshot(conn, row)
            previous = self._last_derived.get(match_id)
            if match_key in closing:
                # Closed: it derives nothing further, so stop holding its
                # numbers rather than growing this dict for the process's life.
                self._last_derived.pop(match_id, None)
            else:
                self._last_derived[match_id] = current
            # A match that is already being refetched this batch needs no
            # second nudge; record the baseline and move on.
            if match_key in already:
                continue
            if previous is None:
                changed = dict(current)
            else:
                changed = {
                    field: value
                    for field, value in current.items()
                    if previous.get(field) != value
                }
            if changed:
                pending.api.append(
                    (
                        "match.derived.updated",
                        match_key,
                        {"match_key": match_key, "changed": changed},
                    )
                )

    def _derived_snapshot(self, conn, row) -> dict[str, Any]:
        """The watched derived fields for one match, read through `conn`."""
        match_id = int(row["id"])
        readings = [
            Reading(
                elapsed_sec=float(r["elapsed_sec"]),
                cash=int(r["cash"]),
                life=int(r["life"]),
                timestamp=r["observed_at"],
            )
            for r in conn.execute(
                "SELECT elapsed_sec, cash, life, observed_at FROM cash_samples"
                " WHERE match_id = %s ORDER BY elapsed_sec",
                (match_id,),
            )
        ]
        overrides = {
            int(r["life_number"]): float(r["value"])
            for r in conn.execute(
                "SELECT life_number, value FROM overrides"
                " WHERE scope = 'life_kit_cost' AND match_id = %s AND value IS NOT NULL",
                (match_id,),
            )
        }
        view = derive_match(self.db, row, readings=readings, overrides=overrides)
        summary = view.summary_json()
        return {field: summary[field] for field in WATCHED_DERIVED}

    def _flush(self, conn, pending: Pending) -> None:
        # `executemany` lives on the cursor rather than the connection, which
        # is where the driver keeps the prepared statement it reuses across the
        # batch -- the whole reason the rows were buffered.
        with conn.cursor() as cursor:
            if pending.samples:
                cursor.executemany(SAMPLE_SQL, pending.samples)
            if pending.events:
                cursor.executemany(EVENT_SQL, pending.events)
            if pending.xp:
                cursor.executemany(XP_SQL, pending.xp)

    # -- corrections -----------------------------------------------------

    def set_override(
        self,
        match_key: str,
        life_number: int,
        value: float | None,
        note: str | None = None,
        *,
        user_id: int | None = None,
    ) -> dict[str, Any]:
        """Record a manual kit price. Never touches a fact or a derivation.

        `user_id` scopes the lookup rather than being checked afterwards: a
        match belonging to someone else is not found, so this is the same
        `KeyError` -- and therefore the same 404 -- as a match that does not
        exist. Telling the difference would say whether a stranger's match key
        is real.
        """
        with self.db.write() as conn:
            if user_id is None:
                row = conn.execute(
                    "SELECT id, user_id FROM matches WHERE match_key = %s",
                    (match_key,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT id, user_id FROM matches WHERE match_key = %s AND user_id = %s",
                    (match_key, user_id),
                ).fetchone()
            if row is None:
                raise KeyError(match_key)
            match_id = int(row[0])
            owner = None if row[1] is None else int(row[1])
            if value is None:
                conn.execute(
                    "DELETE FROM overrides WHERE scope = 'life_kit_cost'"
                    " AND match_id = %s AND life_number = %s",
                    (match_id, life_number),
                )
            else:
                conn.execute(
                    "INSERT INTO overrides (scope, match_id, life_number, value, note,"
                    " created_at) VALUES ('life_kit_cost', %s, %s, %s, %s, %s)"
                    " ON CONFLICT(scope, match_id, life_number) DO UPDATE SET"
                    " value = excluded.value, note = excluded.note,"
                    " created_at = excluded.created_at",
                    (match_id, life_number, float(value), note, utc_now()),
                )
            payload = {"life": life_number, "value": value}
            seq = self.db.write_event(
                "override.set", payload, match_key=match_key, user_id=owner, conn=conn
            )
        if self.on_published is not None:
            self.on_published([(seq, "override.set", match_key, payload, owner)])
        return {"seq": seq, **payload}


def decode_event(row) -> dict[str, Any]:
    return {
        "seq": int(row["seq"]),
        "kind": row["kind"],
        "match_key": row["match_key"],
        "payload": json.loads(row["payload"]),
        "committed_at": row["committed_at"],
    }
