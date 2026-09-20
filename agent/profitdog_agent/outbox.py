"""The agent's durable outbox, and the identity that goes with it.

## Why the agent needs a disk at all

The server owns the database. The agent owns a queue — and the difference
matters, because the queue holds nothing that anyone would ever query. It is a
strictly ordered list of facts that have not been acknowledged yet, and it
empties itself as fast as the network lets it. Nothing reads it but the uplink,
nothing derives anything from it, and deleting it loses only whatever had not
reached the server yet.

It is SQLite because a queue that survives a crash has to be written
transactionally, and hand-rolling that over a flat file is how you end up with
a half-written last record. It is not a second system of record.

## The sequence number is allocated *durably*

`append` writes the fact and advances `next_seq` in one transaction. That is
the point of the whole module: a sequence number is never handed out unless the
fact carrying it is already on disk. Get that wrong and a crash between "took
the number" and "wrote the row" reuses a number for a different fact, which
turns the server's idempotency gate — keyed on exactly that number — into a
silent data-loss mechanism.

Sequence numbers continue across boots. They are the agent's whole history, not
this process's, so `next_seq` outlives the `boot_id` beside it.

## Two threads, one connection

The collector appends and the uplink drains, concurrently, for the whole life
of the process. A SQLite connection is not safe to use from two threads at
once, so every method here takes one re-entrant lock — including the reads,
which is the part that is easy to forget and the part that fails in production
rather than in a test: `pending()` runs on the uplink thread while `append()`
runs on the collector's.

Serialising them costs nothing. The contended operations are a handful of small
statements a couple of times a second, and the alternative — a connection per
thread — would give the two threads separate views of a queue whose whole job
is to be one queue.

## Watermarks

Some sources are read by polling a file that keeps saying the same thing. The
agent emits those on change, so it has to remember what it last saw — and
remember it across restarts, or every restart would re-report every spawn in
the breadcrumb log as though it had just happened. Those watermarks live here,
in the same transaction as the fact they suppress, so the two can never
disagree: a fact is emitted if and only if the watermark moved.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from profitdog_protocol import Fact

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    agent_seq  INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,
    envelope   TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Outbox:
    """A durable, ordered, acknowledge-to-delete queue of facts."""

    def __init__(self, path: Path | str, *, boot_id: str | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Shared between the collector and uplink threads, guarded by `_lock`.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        # FULL, not NORMAL: this is the only copy of a fact until the server
        # acknowledges it. The server can afford to lose the tail of a crash
        # because the agent still has it; the agent has no one to fall back on.
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(SCHEMA)

        self.agent_id = self._ensure("agent_id", lambda: str(uuid.uuid4()))
        self.boot_id = boot_id or str(uuid.uuid4())
        self._ensure("next_seq", lambda: "1")
        self._ensure("acked_through", lambda: "0")
        self.set_meta("last_boot_id", self.boot_id)
        self.set_meta("last_started_at", utc_now())

    # -- meta ------------------------------------------------------------

    def _ensure(self, key: str, make: Any) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
            if row is not None:
                return str(row["value"])
            value = str(make())
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)", (key, value)
            )
            return value

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row is not None else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    @contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # -- producing -------------------------------------------------------

    def append(
        self,
        kind: str,
        source: str,
        body: dict[str, Any],
        *,
        source_ts: str | None = None,
        build: str | None = None,
        observed_at: str | None = None,
        watermarks: dict[str, str] | None = None,
    ) -> Fact:
        """Record one observation. Returns the fact as it will be sent.

        `watermarks` are written in the same transaction, so "I emitted this"
        and "I have now seen up to here" commit together or not at all.
        """
        with self._txn() as conn:
            seq = int(
                conn.execute("SELECT value FROM meta WHERE key = 'next_seq'")
                .fetchone()["value"]
            )
            fact = Fact(
                agent_id=self.agent_id,
                boot_id=self.boot_id,
                agent_seq=seq,
                kind=kind,
                source=source,
                observed_at=observed_at or utc_now(),
                body=body,
                source_ts=source_ts,
                build=build,
            )
            conn.execute(
                "INSERT INTO outbox (agent_seq, kind, envelope, created_at)"
                " VALUES (?, ?, ?, ?)",
                (seq, kind, json.dumps(fact.to_json(), separators=(",", ":")), utc_now()),
            )
            conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'next_seq'", (str(seq + 1),)
            )
            for key, value in (watermarks or {}).items():
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
        return fact

    # -- consuming -------------------------------------------------------

    def pending(self, limit: int = 500) -> list[Fact]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT envelope FROM outbox ORDER BY agent_seq LIMIT ?", (limit,)
            ).fetchall()
        return [Fact.from_json(json.loads(r["envelope"])) for r in rows]

    def ack(self, through: int) -> int:
        """Forget everything at or below `through`. Returns rows dropped.

        Never acts on anything above it. A server that acknowledges a sequence
        number is promising it has that fact and every fact before it; it is
        promising nothing at all about the ones after.
        """
        if through <= 0:
            return 0
        with self._txn() as conn:
            dropped = conn.execute(
                "DELETE FROM outbox WHERE agent_seq <= ?", (through,)
            ).rowcount
            current = int(
                conn.execute("SELECT value FROM meta WHERE key = 'acked_through'")
                .fetchone()["value"]
            )
            if through > current:
                conn.execute(
                    "UPDATE meta SET value = ? WHERE key = 'acked_through'",
                    (str(through),),
                )
        return dropped

    @property
    def acked_through(self) -> int:
        return int(self.get_meta("acked_through", "0") or 0)

    @property
    def next_seq(self) -> int:
        return int(self.get_meta("next_seq", "1") or 1)

    def depth(self) -> int:
        with self._lock:
            return int(
                self._conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
            )

    def oldest_pending_age_sec(self) -> float | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT created_at FROM outbox ORDER BY agent_seq LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        created = datetime.fromisoformat(str(row["created_at"]))
        return (datetime.now(timezone.utc) - created).total_seconds()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "Outbox":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
