"""Shared fixtures and a small fact-stream builder.

`AgentSim` exists because almost every server-side test needs a plausible
stream of observations, and hand-writing envelopes obscures what each test is
actually about. It produces exactly what a real agent produces — presence polls
every two seconds, spawns with the game's own timestamps, XP as absolute
totals — so a test that passes against it is testing the real ingest path.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from profitdog.protocol import Fact, FactBatch
from profitdog.server.db import open_database
from profitdog.server.ingest import Ingestor

FIXTURES = Path(__file__).resolve().parents[1] / "profitdog-ui" / "src" / "lib" / "__fixtures__"


@pytest.fixture()
def db(tmp_path):
    database = open_database(tmp_path / "profitdog.sqlite3")
    yield database
    database.close()


@pytest.fixture()
def ingestor(db):
    return Ingestor(db)


class AgentSim:
    """Builds the fact stream a real agent would produce."""

    def __init__(
        self,
        agent_id: str = "agent-test",
        boot_id: str = "boot-1",
        start: datetime | None = None,
        build: str | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.boot_id = boot_id
        self.build = build
        self.clock = start or datetime(2026, 9, 19, 20, 0, 0, tzinfo=timezone.utc)
        self._seq = itertools.count(1)
        self.facts: list[Fact] = []

    # -- time ------------------------------------------------------------

    def tick(self, seconds: float = 2.0) -> datetime:
        self.clock += timedelta(seconds=seconds)
        return self.clock

    # -- facts -----------------------------------------------------------

    def _emit(self, kind: str, source: str, body: dict, source_ts: str | None = None) -> Fact:
        fact = Fact(
            agent_id=self.agent_id,
            boot_id=self.boot_id,
            agent_seq=next(self._seq),
            kind=kind,
            source=source,
            observed_at=self.clock.isoformat(),
            body=body,
            source_ts=source_ts if source_ts is not None else self.clock.isoformat(),
            build=self.build,
        )
        self.facts.append(fact)
        return fact

    def presence(self, state: str = "playing", cash: int | None = None,
                 faction: str = "unknown", advance: float = 2.0) -> Fact:
        self.tick(advance)
        body: dict = {"game_state": state, "faction": faction}
        if cash is not None:
            body["profit_loss"] = f"{'-' if cash < 0 else '+'}${abs(cash):,}"
        return self._emit("presence", "rich_presence", body)

    def menu(self, times: int = 5, advance: float = 2.0) -> None:
        for _ in range(times):
            self.presence(state="mainmenu", advance=advance)

    def spawn(self, at: datetime | None = None) -> Fact:
        moment = at or self.clock
        return self._emit(
            "spawn", "breadcrumbs", {"at": moment.isoformat()},
            source_ts=moment.isoformat(),
        )

    def map(self, name: str) -> Fact:
        return self._emit("map", "breadcrumbs", {"map": name})

    def xp(self, **roles: int) -> Fact:
        return self._emit("xp", "save_file", {"roles": roles})

    def status(self, status: str) -> Fact:
        return self._emit("agent_status", "agent", {"status": status})

    def play(self, cashflow: list[int], faction: str = "bravo") -> None:
        """A run of `playing` polls carrying a given cash curve."""
        for cash in cashflow:
            self.presence(state="playing", cash=cash, faction=faction)

    # -- delivery --------------------------------------------------------

    def batch(self, facts: list[Fact] | None = None, label: str | None = None) -> FactBatch:
        return FactBatch(
            agent_id=self.agent_id,
            boot_id=self.boot_id,
            facts=list(self.facts if facts is None else facts),
            label=label,
        )

    def drain(self) -> FactBatch:
        batch = self.batch()
        self.facts = []
        return batch


@pytest.fixture()
def agent():
    return AgentSim()
