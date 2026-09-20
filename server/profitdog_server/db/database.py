"""The database, which is PostgreSQL.

## Why this moved off SQLite

SQLite was right while profitdog was one person's tracker on one machine. It
stopped being right the moment the server grew accounts: a hosted service wants
its data somewhere that survives the container it runs in, takes a backup
without stopping writers, and does not care that the web process and a future
second one are different processes.

SQLite has *not* left the project. The agent's outbox is still SQLite, and
deliberately so — it is a single-writer queue on one gaming PC with no server in
sight, which is exactly the shape SQLite is best at. Nothing under
`profitdog_agent` imports this module.

## One writer, still

`write()` serialises on a lock, as it did under SQLite, and that is not
laziness about Postgres's concurrency. Two invariants depend on it:

- **Segmentation state is in memory.** `Segmenter` holds a per-agent state
  machine and decides match boundaries from it. Two batches for one agent
  interleaving would corrupt that, and no isolation level fixes a race in a
  Python dict.
- **`api_events.seq` is a cursor clients resume on.** Sequence values are
  handed out before commit, so two concurrent transactions can commit out of
  order and leave a client that saw the higher number permanently blind to the
  lower one. Serialising writers removes that window rather than narrowing it.

Reads do not take the lock and run concurrently on their own connections.

## Persist before publishing

`write_event` is the only way a delta reaches a client, and it writes the fact
and the `api_events` row in the *same* transaction. A client therefore cannot
be told about something that is not stored: either the transaction committed
and both exist, or it did not and neither does. That is what makes the sequence
number a safe cursor to reconnect on.

## Rows

`Row` is a small stand-in for `sqlite3.Row`: indexable by name *and* by
position, and accepted by `dict()`. Both styles are used throughout the domain
and the API, and a row type that did only one of them would have meant
rewriting call sites for no gain.

## Timestamps are still text

Stored as ISO-8601 UTC strings rather than `timestamptz`. They are written in
one place, they sort correctly as text because they are always normalised to
UTC at one precision, and every ruleset already parses them from text.
Converting would mean changing how conclusions are drawn in order to change how
bytes are stored, which is the wrong reason to touch a rule.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, TypeVar

import psycopg
from psycopg import sql

from .schema import LATEST_VERSION, current_version, migrate

log = logging.getLogger("profitdog_server.db")

T = TypeVar("T")

DEFAULT_URL = "postgresql://profitdog:profitdog@127.0.0.1:5432/profitdog"


def utc_now() -> str:
    """One spelling of 'now', so stored timestamps sort as strings."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def database_url() -> str:
    return os.environ.get("PROFITDOG_DATABASE_URL", DEFAULT_URL)


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


class Row:
    """A result row, readable by column name or by position.

    Modelled on `sqlite3.Row`, including the one part of it that is surprising:
    **iterating a row yields its values, not its column names.** Code here does
    `for (seq,) in rows` and expects the number, and a `Mapping`, which would
    have yielded the string `"agent_seq"` instead, silently produces nonsense
    rather than an error.

    `dict(row)` still works: `dict` looks for a `keys()` method first and uses
    the mapping protocol when it finds one, so `keys()` and `__getitem__`
    together are enough without inheriting from `Mapping` and taking its
    iteration semantics with it.
    """

    __slots__ = ("_columns", "_values", "_index")

    def __init__(self, columns: tuple[str, ...], values: tuple[Any, ...]) -> None:
        self._columns = columns
        self._values = values
        self._index: dict[str, int] | None = None

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        if self._index is None:
            self._index = {name: i for i, name in enumerate(self._columns)}
        try:
            return self._values[self._index[key]]
        except KeyError:
            raise KeyError(key) from None

    def __iter__(self):
        # Values, as sqlite3.Row does. See the class docstring.
        return iter(self._values)

    def __contains__(self, key) -> bool:
        return key in self._columns

    def __len__(self) -> int:
        return len(self._values)

    def keys(self) -> list[str]:
        return list(self._columns)

    def values(self) -> list[Any]:
        return list(self._values)

    def __repr__(self) -> str:
        pairs = ", ".join(f"{c}={v!r}" for c, v in zip(self._columns, self._values))
        return f"Row({pairs})"

    def items(self):
        return list(zip(self._columns, self._values))

    def get(self, key, default=None):
        try:
            return self[key]
        except (KeyError, IndexError):
            return default

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Row):
            return self._columns == other._columns and self._values == other._values
        if isinstance(other, Mapping):
            return dict(self) == dict(other)
        if isinstance(other, (tuple, list)):
            return list(self._values) == list(other)
        return NotImplemented

    def __hash__(self):  # pragma: no cover - rows are never keys
        return hash((self._columns, self._values))


def row_factory(cursor):
    description = cursor.description
    if description is None:  # pragma: no cover - statements returning nothing
        return lambda values: values
    columns = tuple(column.name for column in description)
    return lambda values: Row(columns, tuple(values))


# ---------------------------------------------------------------------------
# The database
# ---------------------------------------------------------------------------


class Database:
    """Connections, transactions, and the published log.

    Two kinds of connection, for the two kinds of work: readers, one per
    thread and autocommit, which never block anyone; and one writer for the
    whole process, held under a lock, for the reasons in the module docstring.
    """

    def __init__(self, url: str | None = None, *, schema: str | None = None) -> None:
        self.url = url or database_url()
        #: An optional Postgres schema to confine everything to. The tests give
        #: every test its own and drop it afterwards, which is as isolated as a
        #: database each and far cheaper to create.
        self.schema = schema
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._write_conn: psycopg.Connection | None = None

        with self.write() as conn:
            if self.schema:
                conn.execute(
                    sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                        sql.Identifier(self.schema)
                    )
                )
                self._set_search_path(conn)
            migrate(conn)
        self.schema_version = self._version()

    # -- connections -----------------------------------------------------

    def _set_search_path(self, conn: psycopg.Connection) -> None:
        if self.schema:
            conn.execute(
                sql.SQL("SET search_path TO {}, public").format(
                    sql.Identifier(self.schema)
                )
            )

    def _connect(self, *, autocommit: bool) -> psycopg.Connection:
        conn = psycopg.connect(self.url, autocommit=autocommit, row_factory=row_factory)
        self._set_search_path(conn)
        return conn

    def _reader(self) -> psycopg.Connection:
        """A read-only connection for this thread.

        Per thread rather than shared, because a connection is one conversation
        and two threads talking over it interleave their results. Autocommit,
        so a reader never holds a transaction open while somebody reads a chart.
        """
        conn = getattr(self._local, "conn", None)
        if conn is None or conn.closed:
            conn = self._connect(autocommit=True)
            self._local.conn = conn
        return conn

    # -- reading ---------------------------------------------------------

    def query(self, statement: str, params: Sequence[Any] = ()) -> list[Row]:
        with self._reader().cursor() as cursor:
            cursor.execute(statement, tuple(params))
            return cursor.fetchall()

    def query_one(self, statement: str, params: Sequence[Any] = ()) -> Row | None:
        with self._reader().cursor() as cursor:
            cursor.execute(statement, tuple(params))
            return cursor.fetchone()

    def scalar(self, statement: str, params: Sequence[Any] = ()) -> Any:
        row = self.query_one(statement, params)
        return None if row is None else row[0]

    # -- writing ---------------------------------------------------------

    @contextmanager
    def write(self) -> Iterator[psycopg.Connection]:
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
                    if self._write_conn is None or self._write_conn.closed:
                        self._write_conn = self._connect(autocommit=False)
                    conn = self._write_conn
                    try:
                        yield conn
                    except BaseException:
                        conn.rollback()
                        raise
                    else:
                        conn.commit()
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
        user_id: int | None = None,
        conn: psycopg.Connection | None = None,
    ) -> int:
        """Append to the published log. Returns the sequence number.

        Call this *inside* the same `write()` block as the fact it announces.
        Doing it afterwards would open exactly the window this design exists to
        close: a client told about a sample that a crash then took away.
        """
        statement = (
            "INSERT INTO api_events (kind, match_key, payload, committed_at, user_id)"
            " VALUES (%s, %s, %s, %s, %s) RETURNING seq"
        )
        args = (
            kind,
            match_key,
            json.dumps(payload, separators=(",", ":")),
            utc_now(),
            user_id,
        )
        if conn is not None:
            return int(conn.execute(statement, args).fetchone()[0])
        with self.write() as owned:
            return int(owned.execute(statement, args).fetchone()[0])

    def transact(self, fn: Callable[[psycopg.Connection], T]) -> T:
        with self.write() as conn:
            return fn(conn)

    # -- the published log -----------------------------------------------

    def latest_seq(self) -> int:
        return int(self.scalar("SELECT COALESCE(MAX(seq), 0) FROM api_events") or 0)

    def oldest_seq(self) -> int:
        return int(self.scalar("SELECT COALESCE(MIN(seq), 0) FROM api_events") or 0)

    def trim_events(self, keep: int) -> int:
        """Drop all but the newest `keep` published events."""
        with self.write() as conn:
            deleted = conn.execute(
                "DELETE FROM api_events WHERE seq <= ("
                " SELECT COALESCE(MAX(seq), 0) - %s FROM api_events)",
                (keep,),
            ).rowcount
        return int(deleted)

    # -- lifecycle -------------------------------------------------------

    def _version(self) -> int:
        with self.write() as conn:
            return current_version(conn)

    def drop_schema(self) -> None:
        """Remove this database's schema entirely. For tests."""
        if not self.schema:
            raise RuntimeError("refusing to drop the default schema")
        with self.write() as conn:
            conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(self.schema)
                )
            )

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None and not conn.closed:
            conn.close()
        self._local.conn = None
        if self._write_conn is not None and not self._write_conn.closed:
            self._write_conn.close()
        self._write_conn = None


def open_database(url: str | None = None, *, schema: str | None = None) -> Database:
    db = Database(url, schema=schema)
    if db.schema_version != LATEST_VERSION:  # pragma: no cover - defensive
        raise RuntimeError(
            f"database at {db.url} is at schema {db.schema_version}, "
            f"expected {LATEST_VERSION}"
        )
    return db


__all__ = [
    "Database",
    "Row",
    "database_url",
    "open_database",
    "utc_now",
    "current_version",
]
