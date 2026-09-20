"""Schema migrations.

## Why a list of scripts and not a schema file

A schema file tells you what the database looks like now. It does not tell you
how to get an existing one there, and this database is the only copy of history
that exists once the CSVs are gone. So the shape is expressed as an ordered
list of forward steps, each applied exactly once and recorded in
`schema_migrations`. Adding a column means appending a step, never editing one
that has shipped.

## The four kinds of row

The separation is the whole design, so it is worth stating before the tables.

- **Facts** — `cash_samples`, `source_events`, `xp_events`, and the `matches`
  they hang off. What an agent observed. Immutable once written. Every one
  carries who saw it, which run of that agent saw it, its place in that agent's
  sequence, and three timestamps that are routinely different.
- **Conclusions** — *none of them are here.* Lives, kits, ledger entries,
  break-even times and classifications are computed from facts on read by a
  versioned ruleset. Nothing derived is stored, so nothing derived can go
  stale, and a rule change re-reads history instead of migrating it.
- **Corrections** — `overrides`. What a person said when a derivation was
  wrong. Kept apart from both of the above, because a correction is neither an
  observation nor a conclusion and folding it into either loses the ability to
  recompute one without discarding the other.
- **The published log** — `api_events`. Committed deltas in commit order,
  written in the same transaction as the fact they announce. Its `seq` is the
  cursor a browser reconnects on.

## Why matches are here at all, when segmentation is a conclusion

A match is a conclusion — the server decides where one starts by watching
`game_state`, and the thresholds that decide it are versioned like any other
rule. But it is a conclusion every other fact needs to *point at*, and a
foreign key is worth more than the purity: without it, every read of one match
would scan the whole sample table by timestamp.

The compromise is that `matches` is rebuildable. It holds nothing but identity
and the observations that opened and closed it; drop the table, replay the
`source_events`, and the same matches come back. `segmentation.py` is the only
thing that writes it.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        -- ------------------------------------------------------------------
        -- Who is reporting
        -- ------------------------------------------------------------------

        CREATE TABLE agents (
            id            INTEGER PRIMARY KEY,
            agent_id      TEXT    NOT NULL UNIQUE,
            label         TEXT,
            first_seen_at TEXT    NOT NULL,
            last_seen_at  TEXT    NOT NULL,
            last_boot_id  TEXT,
            -- Highest agent_seq held with no gap behind it. This is the number
            -- returned to the agent, and the only thing that licenses it to
            -- forget anything.
            acked_through INTEGER NOT NULL DEFAULT 0
        );

        -- One row per run of an agent process. A gap in agent_seq with a boot
        -- change across it is a restart; a gap without one is lost data, and
        -- the difference is worth being able to see.
        CREATE TABLE agent_boots (
            id            INTEGER PRIMARY KEY,
            agent_ref     INTEGER NOT NULL REFERENCES agents (id) ON DELETE CASCADE,
            boot_id       TEXT    NOT NULL,
            first_seen_at TEXT    NOT NULL,
            last_seen_at  TEXT    NOT NULL,
            UNIQUE (agent_ref, boot_id)
        );

        -- The idempotency gate. Every envelope the server has ever accepted,
        -- by (agent, sequence). One envelope can fan out into several rows —
        -- an xp reading becomes one row per role — so uniqueness lives here
        -- rather than on the typed tables, where it could not express that.
        --
        -- WITHOUT ROWID: this is two integers and a short string per fact, and
        -- at 1.8M facts the saved rowid indirection is most of its size.
        CREATE TABLE ingested_envelopes (
            agent_ref INTEGER NOT NULL REFERENCES agents (id) ON DELETE CASCADE,
            agent_seq INTEGER NOT NULL,
            kind      TEXT    NOT NULL,
            PRIMARY KEY (agent_ref, agent_seq)
        ) WITHOUT ROWID;

        -- ------------------------------------------------------------------
        -- Facts
        -- ------------------------------------------------------------------

        -- One server session, cut out of the observation stream. See the note
        -- above on why a conclusion is allowed a table.
        CREATE TABLE matches (
            id          INTEGER PRIMARY KEY,
            -- Stable natural key, independent of any filename. Everything
            -- outward-facing names a match by this.
            match_key   TEXT    NOT NULL UNIQUE,
            agent_ref   INTEGER REFERENCES agents (id) ON DELETE SET NULL,
            started_at  TEXT    NOT NULL,   -- ISO-8601 UTC
            ended_at    TEXT,               -- null while the match is live
            map         TEXT,
            faction     TEXT,
            build       TEXT,
            source      TEXT    NOT NULL,   -- 'agent' | 'import:csv'
            observed_at TEXT    NOT NULL,
            received_at TEXT    NOT NULL,
            ingested_at TEXT    NOT NULL,
            closed      INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX matches_started_at ON matches (started_at);
        CREATE INDEX matches_map        ON matches (map);
        CREATE INDEX matches_build      ON matches (build);
        CREATE INDEX matches_open       ON matches (closed, started_at);
        CREATE INDEX matches_agent      ON matches (agent_ref, started_at);

        -- The profit/loss curve, exactly as the game reported it.
        CREATE TABLE cash_samples (
            id          INTEGER PRIMARY KEY,
            match_id    INTEGER NOT NULL REFERENCES matches (id) ON DELETE CASCADE,
            elapsed_sec REAL    NOT NULL,
            cash        INTEGER NOT NULL,
            life        INTEGER NOT NULL,
            -- The verbatim string Wardogs published, beside the number we read
            -- out of it. This is what makes a wrong parser fixable: re-read
            -- history rather than discover the evidence was thrown away. It
            -- lives here rather than in a second `source_events` row per poll
            -- because a duplicate row per reading doubled the database for one
            -- short string.
            raw         TEXT,
            agent_ref   INTEGER REFERENCES agents (id) ON DELETE SET NULL,
            boot_ref    INTEGER REFERENCES agent_boots (id) ON DELETE SET NULL,
            agent_seq   INTEGER,
            source_ts   TEXT,
            observed_at TEXT    NOT NULL,
            received_at TEXT    NOT NULL,
            ingested_at TEXT    NOT NULL,
            build       TEXT,
            -- Two readings of one match at the same offset are the same
            -- reading. Belt to the envelope gate's braces: it also protects
            -- against a re-import, where the facts arrive with no agent
            -- sequence at all.
            UNIQUE (match_id, elapsed_sec)
        );
        CREATE INDEX cash_samples_match ON cash_samples (match_id, elapsed_sec);

        -- Everything an agent saw that is not an ordinary reading of the
        -- curve: spawns, map loads, agent lifecycle, and any presence poll
        -- where something other than the money changed — a state transition, a
        -- faction assignment, a value that would not parse. Ordinary playing
        -- polls are `cash_samples` rows, which carry their own raw string, so
        -- nothing observed is missing from the database.
        CREATE TABLE source_events (
            id          INTEGER PRIMARY KEY,
            match_id    INTEGER REFERENCES matches (id) ON DELETE CASCADE,
            kind        TEXT    NOT NULL,   -- 'presence'|'spawn'|'map'|'agent_status'
            source      TEXT    NOT NULL,   -- 'rich_presence'|'breadcrumbs'|'agent'
            -- The original factual values, verbatim, as JSON.
            payload     TEXT    NOT NULL,
            life        INTEGER,
            agent_ref   INTEGER REFERENCES agents (id) ON DELETE SET NULL,
            boot_ref    INTEGER REFERENCES agent_boots (id) ON DELETE SET NULL,
            agent_seq   INTEGER,
            source_ts   TEXT,
            observed_at TEXT    NOT NULL,
            received_at TEXT    NOT NULL,
            ingested_at TEXT    NOT NULL,
            build       TEXT
        );
        CREATE INDEX source_events_match ON source_events (match_id, observed_at);
        CREATE INDEX source_events_kind  ON source_events (kind, observed_at);
        CREATE INDEX source_events_seq   ON source_events (agent_ref, agent_seq);

        -- Role XP. The fact is the absolute total the save file held; `delta`
        -- beside it is a convenience computed at ingest from the previous
        -- reading, and is reproducible from the totals alone. The domain layer
        -- reads the totals, never the deltas, so a wrong delta cannot reach a
        -- conclusion.
        CREATE TABLE xp_events (
            id          INTEGER PRIMARY KEY,
            match_id    INTEGER REFERENCES matches (id) ON DELETE SET NULL,
            role        TEXT    NOT NULL,
            -- The absolute value the save file held. Null only for history
            -- imported from the CSV-era `role_xp_log.csv`, which recorded
            -- gains and never totals — a real absence, not a zero, and the
            -- domain falls back to `delta` for exactly those rows.
            total       INTEGER,
            delta       INTEGER,
            agent_ref   INTEGER REFERENCES agents (id) ON DELETE SET NULL,
            boot_ref    INTEGER REFERENCES agent_boots (id) ON DELETE SET NULL,
            agent_seq   INTEGER,
            source_ts   TEXT,
            observed_at TEXT    NOT NULL,
            received_at TEXT    NOT NULL,
            ingested_at TEXT    NOT NULL,
            build       TEXT,
            CHECK (total IS NOT NULL OR delta IS NOT NULL)
        );
        CREATE INDEX xp_events_match ON xp_events (match_id);
        CREATE INDEX xp_events_time  ON xp_events (observed_at);
        CREATE INDEX xp_events_role  ON xp_events (role, observed_at);

        -- ------------------------------------------------------------------
        -- Corrections
        -- ------------------------------------------------------------------

        CREATE TABLE overrides (
            id          INTEGER PRIMARY KEY,
            scope       TEXT    NOT NULL,   -- 'life_kit_cost'
            match_id    INTEGER NOT NULL REFERENCES matches (id) ON DELETE CASCADE,
            life_number INTEGER NOT NULL,
            value       REAL,               -- null clears it
            note        TEXT,
            created_at  TEXT    NOT NULL,
            UNIQUE (scope, match_id, life_number)
        );

        -- ------------------------------------------------------------------
        -- The published log
        -- ------------------------------------------------------------------

        CREATE TABLE api_events (
            seq          INTEGER PRIMARY KEY AUTOINCREMENT,
            kind         TEXT NOT NULL,
            match_key    TEXT,
            payload      TEXT NOT NULL,
            committed_at TEXT NOT NULL
        );
        CREATE INDEX api_events_match ON api_events (match_key, seq);
        """,
    ),
    (
        2,
        """
        -- ------------------------------------------------------------------
        -- The derived cache
        -- ------------------------------------------------------------------
        --
        -- Added because a measurement said so, not because it seemed wise.
        -- `profitdog.server.bench` over a thousand hours of play (1,351 matches,
        -- 7,429 lives, 1.34M readings) put a full re-derivation of every match
        -- at 3.2 seconds, which is the cost of loading the history page. One
        -- match on its own derives in about 2ms, so the cost is entirely in
        -- the count.
        --
        -- Nothing here is a source of truth. Every row is reproducible from
        -- facts, every row records the exact inputs it was built from, and
        -- `DELETE FROM derived_matches` is a supported operation that costs
        -- one slow page load. `tests/test_cache.py` asserts that dropping it
        -- changes no answer anywhere.
        CREATE TABLE derived_matches (
            match_id    INTEGER PRIMARY KEY REFERENCES matches (id) ON DELETE CASCADE,
            -- Which rules produced this, and a fingerprint of their source. The
            -- version alone is not enough: editing `v1.py` without renaming it
            -- would otherwise leave every cached row silently stale.
            ruleset     TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            -- Identifies the facts and corrections this was built from. Any
            -- change to either produces a different value and a rebuild.
            input_hash  TEXT NOT NULL,
            payload     TEXT NOT NULL,
            built_at    TEXT NOT NULL
        );
        """,
    ),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def current_version(conn: sqlite3.Connection) -> int:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return row[0] or 0


def migrate(conn: sqlite3.Connection) -> int:
    """Bring a database up to the latest schema. Returns the version reached.

    Each step runs in its own transaction, so an interrupted migration leaves
    the database at the last version that fully applied rather than somewhere
    between two.
    """
    version = current_version(conn)
    for number, script in MIGRATIONS:
        if number <= version:
            continue
        with conn:
            conn.executescript(script)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (number, _now()),
            )
        version = number
    return version


LATEST_VERSION = MIGRATIONS[-1][0]
