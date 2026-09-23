"""The contract between `profitdog_agent` and `profitdog_server`.

This is the only module both halves import. It is deliberately dependency-free
and contains no logic beyond shaping and validating envelopes, because the two
halves are expected to end up on different machines — and eventually on
different release cycles.

## What the agent is, and is not

The agent reads local Wardogs sources and reports *what it saw*. That is all.
It does not decide when a match started, what a kit cost, which life a reading
belongs to, or whether a role gained XP. Every one of those is a conclusion,
conclusions belong to a versioned ruleset, and rulesets live on the server — so
a rule that turns out to be wrong is fixed by redeploying the server and
re-reading history, not by chasing a new agent onto a gaming PC that may be
mid-match.

The practical test applied throughout: if re-running it over the same stored
facts could ever produce a different answer, it does not belong in the agent.
Diffing XP totals fails that test, so the agent sends totals. Counting lives
fails it, so the agent sends spawns. Segmenting matches fails it, so the agent
sends `game_state` and lets the server cut the stream up.

## Identity: agent, boot, sequence

Every envelope carries three identity fields, and they answer three different
questions:

- `agent_id` — which machine. Stable across reinstalls, stored beside the
  outbox. Two gaming PCs can feed one server without colliding.
- `boot_id` — which run of the agent process. New every start. This is what
  makes a restart visible: a gap in `agent_seq` with a boot change across it is
  a restart, while a gap without one is lost data.
- `agent_seq` — a per-agent counter that only ever goes up, *across* boots. It
  is the cursor the whole delivery protocol is built on: the server acks a
  sequence number, the agent drops everything at or below it, and a
  reconnecting agent asks where to resume.

`(agent_id, agent_seq)` identifies an envelope for all time. Ingestion is keyed
on it, which is what makes redelivery free: an agent that never got its ack
resends, and the server recognises and drops the duplicates without the agent
needing to know it happened.

## Three timestamps, never collapsed

- `source_ts` — when the source says the thing happened. Rich Presence carries
  no clock, so for presence reads this is the agent's wall clock at the poll;
  the breadcrumb log *does* timestamp its own events, so for spawns it is the
  game's. Nullable, because some sources genuinely do not know.
- `observed_at` — when the agent read it. Always present.
- `received_at` — when the server took delivery. Added server-side.

They are routinely far apart. An agent that was offline for an hour delivers
facts whose `received_at` is an hour after `observed_at`, and a spawn parsed
out of a breadcrumb file can have a `source_ts` well before the poll that
noticed it. Collapsing any two of them would make "when did this happen" and
"when did we learn it" the same question, and they are the two questions you
need separately the moment anything goes wrong.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

PROTOCOL_VERSION = 1

#: How often an agent with nothing to send says so anyway.
#:
#: An idle agent makes no requests at all: `flush_once` returns early on an
#: empty outbox, and reading the cursor changes nothing server-side. So a PC
#: that has been watching happily for six hours and one that was switched off
#: six hours ago look identical from the server -- both last spoke when the
#: last match ended. "Is my agent running?" was unanswerable, which is the one
#: question somebody asks when their matches stop appearing.
#:
#: The heartbeat is a batch with no facts in it. That is deliberate: it needs
#: no new endpoint, no new envelope and no new failure mode, and it carries the
#: label, version and boot id that ingest already knows what to do with. A
#: fresh agent therefore reports its build immediately rather than only once
#: somebody plays.
HEARTBEAT_INTERVAL_SEC = 60

#: How long the server waits before calling a PC offline.
#:
#: Three heartbeats, not one. A missed request is the normal cost of a laptop
#: lid, a sleeping Wi-Fi radio or a server restart, and a status that flickers
#: to "offline" on every one of them is a status nobody trusts. Both halves
#: read these two numbers from here, because a threshold shorter than the
#: interval it is measuring would mark every healthy agent dead.
OFFLINE_AFTER_SEC = HEARTBEAT_INTERVAL_SEC * 3

#: What an envelope can be. Each is a raw observation; none is a conclusion.
FactKind = Literal[
    # A single Rich Presence poll: game_state, profit_loss, faction as read.
    "presence",
    # A "Player Spawned" breadcrumb, with the game's own timestamp.
    "spawn",
    # The map the crash-reporter breadcrumbs say is loaded.
    "map",
    # One read of PlayerRoleProgress.sav: absolute totals per role.
    "xp",
    # The agent's own lifecycle, so gaps in the stream can be explained.
    "agent_status",
]

FACT_KINDS: tuple[str, ...] = ("presence", "spawn", "map", "xp", "agent_status")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True, slots=True)
class Fact:
    """One immutable observation, as it travels over the wire."""

    agent_id: str
    boot_id: str
    agent_seq: int
    kind: str
    source: str
    observed_at: str
    #: The original factual values, verbatim. Never normalised by the agent:
    #: whatever was read is what is sent, so a parsing mistake can be corrected
    #: later against the evidence instead of being baked in at the edge.
    body: dict[str, Any]
    source_ts: str | None = None
    #: Game build, when the agent can determine it. The primary input to
    #: ruleset selection, so it rides on every fact rather than being looked up
    #: from whatever the match happened to record first.
    build: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @staticmethod
    def from_json(raw: dict[str, Any]) -> "Fact":
        missing = [
            k
            for k in ("agent_id", "boot_id", "agent_seq", "kind", "source", "observed_at")
            if raw.get(k) in (None, "")
        ]
        if missing:
            raise ValueError(f"fact is missing {', '.join(missing)}")
        if raw["kind"] not in FACT_KINDS:
            raise ValueError(f"unknown fact kind {raw['kind']!r}")
        seq = raw["agent_seq"]
        if not isinstance(seq, int) or seq <= 0:
            raise ValueError(f"agent_seq must be a positive integer, got {seq!r}")
        body = raw.get("body")
        if not isinstance(body, dict):
            raise ValueError("fact body must be an object")
        return Fact(
            agent_id=str(raw["agent_id"]),
            boot_id=str(raw["boot_id"]),
            agent_seq=seq,
            kind=str(raw["kind"]),
            source=str(raw["source"]),
            observed_at=str(raw["observed_at"]),
            body=body,
            source_ts=raw.get("source_ts"),
            build=raw.get("build"),
        )


@dataclass(frozen=True, slots=True)
class FactBatch:
    """What the agent POSTs. Facts are in ascending `agent_seq` order."""

    agent_id: str
    boot_id: str
    facts: list[Fact] = field(default_factory=list)
    protocol: int = PROTOCOL_VERSION
    #: Free-form, for the operator's benefit only — never used for identity.
    label: str | None = None
    #: Which build of the agent sent this. Reporting only, like `label`: the
    #: server stores the latest one so "which version is that PC on?" can be
    #: answered from the account rather than by walking over to the machine.
    #: Absent from an older agent, which is why nothing may depend on it.
    version: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "agent_id": self.agent_id,
            "boot_id": self.boot_id,
            "label": self.label,
            "version": self.version,
            "facts": [f.to_json() for f in self.facts],
        }

    @staticmethod
    def from_json(raw: dict[str, Any]) -> "FactBatch":
        protocol = int(raw.get("protocol", 0))
        if protocol != PROTOCOL_VERSION:
            raise ValueError(
                f"unsupported protocol {protocol}; this server speaks {PROTOCOL_VERSION}"
            )
        facts = [Fact.from_json(f) for f in raw.get("facts", [])]
        for previous, current in zip(facts, facts[1:]):
            if current.agent_seq <= previous.agent_seq:
                raise ValueError(
                    "facts must be in ascending agent_seq order; "
                    f"{current.agent_seq} followed {previous.agent_seq}"
                )
        return FactBatch(
            agent_id=str(raw["agent_id"]),
            boot_id=str(raw["boot_id"]),
            facts=facts,
            protocol=protocol,
            label=raw.get("label"),
            version=raw.get("version"),
        )


@dataclass(frozen=True, slots=True)
class Ack:
    """What the server answers.

    `acked_through` is the highest sequence number the server holds *with no
    gaps behind it*. The agent may delete everything at or below it and nothing
    above, which is the whole of the delivery guarantee:

    - A batch that arrives twice acks the same number twice. Harmless.
    - A batch that is lost in flight is never acked, so the agent still has it.
    - A batch with a gap in front of it is stored but acks only up to the gap,
      so the agent keeps retrying the missing part rather than silently
      dropping it.

    `accepted` and `duplicates` are for operators reading logs; nothing in the
    protocol depends on them.
    """

    acked_through: int
    accepted: int
    duplicates: int
    server_seq: int
    protocol: int = PROTOCOL_VERSION

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_json(raw: dict[str, Any]) -> "Ack":
        return Ack(
            acked_through=int(raw["acked_through"]),
            accepted=int(raw.get("accepted", 0)),
            duplicates=int(raw.get("duplicates", 0)),
            server_seq=int(raw.get("server_seq", 0)),
            protocol=int(raw.get("protocol", PROTOCOL_VERSION)),
        )


def dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Body shapes
# ---------------------------------------------------------------------------
#
# Documented rather than enforced. The server stores every body verbatim and
# reads the keys it understands, so an agent that learns to report something
# new does not need the server updated in lockstep to keep working — the new
# key is simply stored until a server exists that reads it.
#
#   presence  {"game_state": "playing", "profit_loss": "+$150 Profit",
#              "faction": "bravo", "in_profit_or_loss": "1"}
#   spawn     {"at": "2026-09-19T23:05:12+00:00"}
#   map       {"map": "Kavkazi"}
#   xp        {"roles": {"Wardog": 82, "Infantry": 14, ...}}
#   agent_status {"status": "started"|"stopped"|"game_open"|"game_closed",
#                 "detail": "..."}

__all__ = [
    "PROTOCOL_VERSION",
    "HEARTBEAT_INTERVAL_SEC",
    "OFFLINE_AFTER_SEC",
    "FACT_KINDS",
    "Fact",
    "FactBatch",
    "Ack",
    "utc_now",
    "dumps",
]
