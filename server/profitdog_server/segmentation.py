"""Cutting a continuous observation stream into matches.

## Why this is the server's job

The agent reports `game_state` and a running profit figure, every two seconds,
forever. Nothing in that stream says "a match started here" — that is a
judgement, made by watching the state settle, and judgements are versioned. Put
it in the agent and the thresholds ship to a gaming PC; put it here and a
mistake is fixed by replaying facts that never moved.

The thresholds themselves live in the ruleset (`rulesets/v1.py`), so a patch
that changes how `game_state` behaves gets a new ruleset rather than an edit to
this file.

## State is a cache, not a record

The segmenter keeps per-agent counters in memory — how many `playing` reads in
a row, which match is open, which life we are on. All of it is reconstructible
from the database, and `restore()` does exactly that on startup. That is what
makes a server restart mid-match a non-event: the open match is still open, its
life count is still the number of lives it has, and the only thing lost is a
streak counter that is about to be recomputed anyway.

## The two tolerances, and the data behind them

A match starts after `CONFIRM_READS` consecutive `playing` reads and ends after
`MISS_THRESHOLD` consecutive non-`playing` ones. The end tolerance is not
decoration: `game_state` was observed flickering away from `playing` for a poll
or two during genuinely continuous play, and with zero tolerance that silently
fragmented single matches into several.

A third case the CSV-era tracker could not handle at all: the agent dies
mid-match. There is no run of non-`playing` reads, just silence, and then
observations resume hours later. A gap wider than `STALE_MATCH_GAP_SEC` closes
the old match rather than stretching it across the outage — the alternative is
a single "match" with a two-hour flat stretch in the middle of it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from profitdog_protocol import Fact
from .domain import rulesets
from .normalize import is_real_faction, normalize_map, parse_profit, parse_ts


@dataclass
class AgentState:
    """Everything the segmenter needs to know about one agent, in memory."""

    agent_ref: int
    playing_streak: int = 0
    miss_streak: int = 0
    match_id: int | None = None
    match_key: str | None = None
    match_started_at: datetime | None = None
    last_observed_at: datetime | None = None
    life_number: int = 1
    seen_spawns: set[str] = field(default_factory=set)
    map: str | None = None
    faction: str | None = None
    build: str | None = None
    xp_totals: dict[str, int] = field(default_factory=dict)
    #: Last presence body with the money removed. A poll is only worth storing
    #: in full when something other than the money moved.
    presence_shape: str | None = None


@dataclass
class Pending:
    """Rows buffered for one batch, flushed with `executemany`.

    Buffering is what makes bulk paths — the importer, the benchmark — fast
    enough to be worth having, without the live path needing a second code
    route: a batch of one flushes one row.
    """

    samples: list[tuple] = field(default_factory=list)
    events: list[tuple] = field(default_factory=list)
    xp: list[tuple] = field(default_factory=list)
    api: list[tuple[str, str | None, dict]] = field(default_factory=list)
    #: Whose batch this is. Set by the ingestor from the authenticated
    #: credential and stamped onto any match opened while processing it, so a
    #: match is owned from the moment it exists rather than by a later sweep.
    user_id: int | None = None


def match_key_for(agent_id: str, started_at: datetime) -> str:
    """A stable name for a match, owned by nothing but its own start.

    Not a filename, not a row id, not an agent sequence: a match has to keep
    its identity across an export, a re-import and a server rebuild, and the
    only thing that survives all three is when it started and who saw it.
    """
    stamp = started_at.astimezone().strftime("%Y%m%dT%H%M%S")
    return f"m-{agent_id[:8]}-{stamp}"


class Segmenter:
    """Turns facts into rows, deciding match and life boundaries as it goes."""

    def __init__(self, db, ruleset: rulesets.RulesetEntry | None = None) -> None:
        self.db = db
        self.rules = ruleset or rulesets.by_version(rulesets.DEFAULT_VERSION)
        self.state: dict[int, AgentState] = {}

    # -- lifecycle -------------------------------------------------------

    def restore(self) -> int:
        """Rebuild in-memory state from whatever is on disk. Returns matches open."""
        self.state.clear()
        rows = self.db.query(
            "SELECT m.id, m.match_key, m.started_at, m.map, m.faction, m.build,"
            "       m.agent_ref,"
            "       (SELECT MAX(life) FROM cash_samples WHERE match_id = m.id) AS lives,"
            "       (SELECT MAX(observed_at) FROM cash_samples WHERE match_id = m.id) AS last_at"
            "  FROM matches m WHERE m.closed = 0 AND m.agent_ref IS NOT NULL"
        )
        for row in rows:
            state = AgentState(agent_ref=int(row["agent_ref"]))
            state.match_id = int(row["id"])
            state.match_key = str(row["match_key"])
            state.match_started_at = parse_ts(row["started_at"])
            state.life_number = int(row["lives"] or 1)
            state.map = row["map"]
            state.faction = row["faction"]
            state.build = row["build"]
            state.last_observed_at = parse_ts(row["last_at"]) or state.match_started_at
            self.state[state.agent_ref] = state
        return len(rows)

    def state_for(self, agent_ref: int) -> AgentState:
        state = self.state.get(agent_ref)
        if state is None:
            state = AgentState(agent_ref=agent_ref)
            self.state[agent_ref] = state
        return state

    # -- feeding ---------------------------------------------------------

    def feed(
        self,
        conn,
        fact: Fact,
        *,
        agent_ref: int,
        boot_ref: int,
        received_at: str,
        ingested_at: str,
        pending: Pending,
    ) -> None:
        state = self.state_for(agent_ref)
        if fact.build:
            state.build = fact.build

        handler = {
            "presence": self._presence,
            "spawn": self._spawn,
            "map": self._map,
            "xp": self._xp,
            "agent_status": self._agent_status,
        }.get(fact.kind)
        if handler is None:  # pragma: no cover - protocol validates kinds
            return
        handler(
            conn,
            fact,
            state,
            agent_ref=agent_ref,
            boot_ref=boot_ref,
            received_at=received_at,
            ingested_at=ingested_at,
            pending=pending,
        )

    # -- handlers --------------------------------------------------------

    def _presence(self, conn, fact, state, *, agent_ref, boot_ref, received_at,
                  ingested_at, pending) -> None:
        rules = self.rules.module
        at = parse_ts(fact.source_ts) or parse_ts(fact.observed_at)
        if at is None:
            return
        game_state = (fact.body.get("game_state") or "").strip().lower()

        # An outage looks like silence, not like a menu, so it needs its own
        # check — otherwise a match is stretched across it.
        if (
            state.match_id is not None
            and state.last_observed_at is not None
            and (at - state.last_observed_at)
            > timedelta(seconds=rules.STALE_MATCH_GAP_SEC)
        ):
            self._close_match(
                conn,
                state,
                ended_at=state.last_observed_at,
                reason="observations stopped",
                pending=pending,
            )

        if state.match_id is None:
            state.playing_streak = state.playing_streak + 1 if game_state == "playing" else 0
            if state.playing_streak >= rules.CONFIRM_READS:
                self._open_match(
                    conn, fact, state, at=at, agent_ref=agent_ref,
                    received_at=received_at, ingested_at=ingested_at, pending=pending,
                )

        if state.match_id is None:
            state.last_observed_at = at
            return

        if game_state == "playing":
            state.miss_streak = 0
            # Faction: Wardogs publishes `unknown` during matchmaking and only
            # names the team once it has assigned you one, so latching the
            # first non-empty value would pin the placeholder to the match.
            if not is_real_faction(state.faction):
                candidate = fact.body.get("faction")
                if is_real_faction(candidate):
                    state.faction = str(candidate)
                    conn.execute(
                        "UPDATE matches SET faction = %s WHERE id = %s",
                        (state.faction, state.match_id),
                    )
                    pending.api.append(
                        ("match.meta", state.match_key,
                         {"faction": state.faction, "map": state.map})
                    )

            raw = fact.body.get("profit_loss")
            cash = parse_profit(raw)
            if cash is not None and state.match_started_at is not None:
                elapsed = round((at - state.match_started_at).total_seconds(), 1)
                pending.samples.append(
                    (
                        state.match_id, elapsed, cash, state.life_number,
                        None if raw is None else str(raw),
                        agent_ref, boot_ref, fact.agent_seq,
                        fact.source_ts, fact.observed_at, received_at, ingested_at,
                        fact.build,
                    )
                )
                pending.api.append(
                    ("match.sample", state.match_key,
                     {"elapsed_sec": elapsed, "cash": cash, "life": state.life_number})
                )
        else:
            state.miss_streak += 1
            if state.miss_streak >= rules.MISS_THRESHOLD:
                self._close_match(
                    conn, state, ended_at=at,
                    reason=f"game_state={game_state or 'absent'}", pending=pending,
                )

        state.last_observed_at = at

        # A presence poll is kept in full whenever anything but the money
        # changed — a state transition, a faction assignment, a value that
        # would not parse, a key we do not read yet. An ordinary playing poll
        # is already stored, verbatim string and all, as a `cash_samples` row;
        # writing it twice doubled the database to record a repetition.
        shape = json.dumps(
            {k: v for k, v in sorted(fact.body.items()) if k != "profit_loss"}
        )
        unparsed = game_state == "playing" and parse_profit(
            fact.body.get("profit_loss")
        ) is None
        if shape != state.presence_shape or unparsed:
            state.presence_shape = shape
            pending.events.append(
                (
                    state.match_id, "presence", fact.source, json.dumps(fact.body),
                    state.life_number, agent_ref, boot_ref, fact.agent_seq,
                    fact.source_ts, fact.observed_at, received_at, ingested_at,
                    fact.build,
                )
            )

    def _spawn(self, conn, fact, state, *, agent_ref, boot_ref, received_at,
               ingested_at, pending) -> None:
        """A spawn breadcrumb. Becomes a new life only if it can be one.

        The very first spawn of a match is the insertion into the match itself,
        which is what started the tracking in the first place — never a second
        life. So only spawns strictly after the confirmed start, by more than
        the grace period, advance the counter.
        """
        rules = self.rules.module
        at = parse_ts(fact.body.get("at")) or parse_ts(fact.source_ts)
        key = at.isoformat() if at else f"seq:{fact.agent_seq}"
        is_new_life = False
        if (
            at is not None
            and state.match_id is not None
            and state.match_started_at is not None
            and key not in state.seen_spawns
            and at > state.match_started_at + timedelta(seconds=rules.LIFE_SPAWN_GRACE_SEC)
        ):
            state.seen_spawns.add(key)
            state.life_number += 1
            is_new_life = True
            pending.api.append(
                ("match.life", state.match_key, {"life": state.life_number})
            )

        pending.events.append(
            (
                state.match_id, "spawn", fact.source,
                json.dumps({**fact.body, "new_life": is_new_life}),
                state.life_number, agent_ref, boot_ref, fact.agent_seq,
                fact.source_ts, fact.observed_at, received_at, ingested_at, fact.build,
            )
        )

    def _map(self, conn, fact, state, *, agent_ref, boot_ref, received_at,
             ingested_at, pending) -> None:
        name = normalize_map(fact.body.get("map"))
        if name and state.map != name:
            state.map = name
            if state.match_id is not None:
                conn.execute(
                    "UPDATE matches SET map = %s WHERE id = %s", (name, state.match_id)
                )
                pending.api.append(
                    ("match.meta", state.match_key,
                     {"map": name, "faction": state.faction})
                )
        pending.events.append(
            (
                state.match_id, "map", fact.source, json.dumps(fact.body),
                state.life_number, agent_ref, boot_ref, fact.agent_seq,
                fact.source_ts, fact.observed_at, received_at, ingested_at, fact.build,
            )
        )

    def _xp(self, conn, fact, state, *, agent_ref, boot_ref, received_at,
            ingested_at, pending) -> None:
        """Absolute role totals. The delta beside them is a convenience.

        The fact is `Wardog: 82`. The gain is a comparison against the previous
        reading, stored next to it because it is cheap and useful, and
        reproducible from the totals alone — which is the only reason it is
        allowed to sit in a facts table at all. The domain layer reads totals.
        """
        roles = fact.body.get("roles") or {}
        if not isinstance(roles, dict):
            return
        gains: dict[str, int] = {}
        for role, total in sorted(roles.items()):
            try:
                value = int(total)
            except (TypeError, ValueError):
                continue
            previous = state.xp_totals.get(role)
            delta = None if previous is None else value - previous
            state.xp_totals[role] = value
            pending.xp.append(
                (
                    state.match_id, str(role), value, delta,
                    agent_ref, boot_ref, fact.agent_seq,
                    fact.source_ts, fact.observed_at, received_at, ingested_at,
                    fact.build,
                )
            )
            if delta:
                gains[str(role)] = delta
        if gains:
            pending.api.append(("xp.gained", state.match_key, {"roles": gains}))

    def _agent_status(self, conn, fact, state, *, agent_ref, boot_ref, received_at,
                      ingested_at, pending) -> None:
        status = (fact.body.get("status") or "").strip()
        # An agent that says it is stopping, or that the game closed, ends any
        # match it had open. Waiting for a stale-gap timeout would leave the
        # match "live" on screen for two minutes after you quit.
        if status in ("stopped", "game_closed") and state.match_id is not None:
            self._close_match(
                conn, state,
                ended_at=parse_ts(fact.observed_at) or state.last_observed_at,
                reason=f"agent reported {status}", pending=pending,
            )
        pending.events.append(
            (
                None, "agent_status", fact.source, json.dumps(fact.body),
                None, agent_ref, boot_ref, fact.agent_seq,
                fact.source_ts, fact.observed_at, received_at, ingested_at, fact.build,
            )
        )

    # -- match lifecycle -------------------------------------------------

    def _open_match(self, conn, fact, state, *, at, agent_ref, received_at,
                    ingested_at, pending) -> None:
        agent_id = fact.agent_id
        key = match_key_for(agent_id, at)
        # Two matches confirmed in the same second on the same agent is not a
        # thing that happens, but a re-ingest of the same facts after the match
        # row was deleted is — so collisions resolve rather than raise.
        suffix = 0
        base = key
        while self.db.query_one("SELECT 1 FROM matches WHERE match_key = %s", (key,)):
            suffix += 1
            key = f"{base}-{suffix}"

        cursor = conn.execute(
            "INSERT INTO matches (match_key, agent_ref, started_at, map, faction,"
            " build, source, observed_at, received_at, ingested_at, closed, user_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, 'agent', %s, %s, %s, 0, %s)"
            " RETURNING id",
            (
                key, agent_ref, at.isoformat(), state.map, None, state.build,
                fact.observed_at, received_at, ingested_at, pending.user_id,
            ),
        )
        state.match_id = int(cursor.fetchone()[0])
        state.match_key = key
        state.match_started_at = at
        state.life_number = 1
        state.faction = None
        state.seen_spawns.clear()
        state.miss_streak = 0
        state.playing_streak = 0
        state.presence_shape = None
        pending.api.append(
            ("match.started", key,
             {"started_at": at.isoformat(), "map": state.map, "build": state.build})
        )

    def _close_match(self, conn, state, *, ended_at, reason, pending) -> None:
        if state.match_id is None:
            return
        key = state.match_key
        conn.execute(
            "UPDATE matches SET closed = 1, ended_at = %s WHERE id = %s",
            (ended_at.isoformat() if ended_at else None, state.match_id),
        )
        pending.api.append(
            ("match.ended", key,
             {"ended_at": ended_at.isoformat() if ended_at else None, "reason": reason})
        )
        state.match_id = None
        state.match_key = None
        state.match_started_at = None
        state.life_number = 1
        state.playing_streak = 0
        state.miss_streak = 0
        state.seen_spawns.clear()
        state.faction = None
