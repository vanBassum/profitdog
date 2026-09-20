"""The database handle: WAL, one writer, many readers.

## One writer, and why it is a rule rather than a habit

SQLite in WAL mode lets readers run while a writer is working, but it still
permits exactly one writer at a time — a second one gets `SQLITE_BUSY` and the
caller gets to invent a retry policy. Rather than scatter that decision, every
write in this service goes through a single connection guarded by one lock, so
`SQLITE_BUSY` is unreachable by construction and write ordering is total. In
the running service that lock is held by one asyncio task draining one queue
(see `writer.py`); off-line tools — the importer, the benchmark, the tests —
take it directly, and never run at the same time as the service.

## Persist before publishing

`write_event` is the only way a delta reaches a client, and it writes the fact
and the `api_events` row in the *same* transaction. A client therefore cannot
be told about something that is not on disk: either the transaction committed
and both exist, or it did not and neither does. That is what makes the sequence
number a safe cursor to reconnect on — there is no window in which the log is
ahead of the data it describes.

## Crash recovery

Nothing special is required, which is the point of the choice: WAL's own
journal replays on the next open. What this module adds is the guarantee that
there are no half-written *logical* states to recover to — a match and its
first sample land together, a sample and its published event land together.
`tests/test_storage.py` kills a process mid-write and asserts exactly that.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from .schema import LATEST_VERSION, current_version, migrate

T = TypeVar("T")


def utc_now() -> str:
    """One spelling of 'now', so stored timestamps sort as strings."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _configure(conn: sqlite3.Connection, *, writable: bool) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # NORMAL is the documented-safe pairing with WAL: a power loss can lose the
    # tail of the last transaction but cannot corrupt the database. The thing
    # being written here is a game's profit curve sampled every two seconds, so
    # trading a possible lost sample for not fsyncing 1.8M times is the right
    # way round.
    conn.execute("PRAGMA synchronous=NORMAL")
    if writable:
        # Wait rather than fail if some other process holds the write lock —
        # this service never does, but a stray sqlite3 shell might.
        conn.execute("PRAGMA busy_timeout=5000")


class Database:
    """A migrated SQLite database with one write connection and N readers."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self._write_conn = sqlite3.connect(
            self.path, isolation_level=None, check_same_thread=False
        )
        _configure(self._write_conn, writable=True)
        with self._write_lock:
            self.schema_version = migrate(self._write_conn)
        self._local = threading.local()

    # -- reading ---------------------------------------------------------

    def _reader(self) -> sqlite3.Connection:
        """One read connection per thread, reused.

        Readers are never shared across threads: a connection is not safe to
        use concurrently, and the alternative — a lock around reads — would
        give up the concurrency WAL exists to provide.
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, check_same_thread=False)
            _configure(conn, writable=False)
            self._local.conn = conn
        return conn

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return self._reader().execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self._reader().execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = self._reader().execute(sql, params).fetchone()
        return None if row is None else row[0]

    # -- writing ---------------------------------------------------------

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """The write connection, inside a transaction, held exclusively.

        Everything that changes the database goes through here. Re-entrant, so
        a helper that needs a transaction can be called from inside one without
        deadlocking or — worse — silently committing half of its caller's work.
        """
        with self._write_lock:
            depth = getattr(self._local, "depth", 0)
            self._local.depth = depth + 1
            try:
                if depth == 0:
                    self._write_conn.execute("BEGIN IMMEDIATE")
                    try:
                        yield self._write_conn
                    except BaseException:
                        self._write_conn.execute("ROLLBACK")
                        raise
                    else:
                        self._write_conn.execute("COMMIT")
                else:
                    yield self._write_conn
            finally:
                self._local.depth = depth

    def write_event(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        match_key: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        """Append to the published log. Returns the sequence number.

        Call this *inside* the same `write()` block as the fact it announces.
        Doing it afterwards would open exactly the window this design exists to
        close: a client told about a sample that a crash then took away.
        """
        target = conn if conn is not None else self._write_conn
        cursor = target.execute(
            "INSERT INTO api_events (kind, match_key, payload, committed_at)"
            " VALUES (?, ?, ?, ?)",
            (kind, match_key, json.dumps(payload, separators=(",", ":")), utc_now()),
        )
        return int(cursor.lastrowid)

    def transact(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        with self.write() as conn:
            return fn(conn)

    # -- housekeeping ----------------------------------------------------

    def latest_seq(self) -> int:
        return int(self.scalar("SELECT COALESCE(MAX(seq), 0) FROM api_events") or 0)

    def oldest_seq(self) -> int:
        return int(self.scalar("SELECT COALESCE(MIN(seq), 0) FROM api_events") or 0)

    def trim_events(self, keep: int) -> int:
        """Drop published events older than the retention window.

        Returns how many went. A client further back than the oldest surviving
        event is told to refetch a snapshot; keeping the log forever would make
        that case unreachable at the cost of a table that only grows.
        """
        with self.write() as conn:
            cutoff = conn.execute(
                "SELECT seq FROM api_events ORDER BY seq DESC LIMIT 1 OFFSET ?",
                (keep,),
            ).fetchone()
            if cutoff is None:
                return 0
            cursor = conn.execute("DELETE FROM api_events WHERE seq <= ?", (cutoff[0],))
            return cursor.rowcount

    def checkpoint(self) -> None:
        with self._write_lock:
            self._write_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def close(self) -> None:
        with self._write_lock:
            self._write_conn.close()
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def open_database(path: Path | str) -> Database:
    db = Database(path)
    if db.schema_version != LATEST_VERSION:  # pragma: no cover - defensive
        raise RuntimeError(
            f"database at {db.path} is at schema {db.schema_version}, "
            f"expected {LATEST_VERSION}"
        )
    return db


__all__ = ["Database", "open_database", "utc_now", "current_version"]
