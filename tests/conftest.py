"""Shared fixtures and a small fact-stream builder.

`AgentSim` exists because almost every server-side test needs a plausible
stream of observations, and hand-writing envelopes obscures what each test is
actually about. It produces exactly what a real agent produces — presence polls
every two seconds, spawns with the game's own timestamps, XP as absolute
totals — so a test that passes against it is testing the real ingest path.
"""

from __future__ import annotations

import itertools
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest

from dataclasses import dataclass

from profitdog_protocol import Fact, FactBatch
from profitdog_server import auth
from profitdog_server.db import open_database, utc_now
from profitdog_server.ingest import Ingestor

FIXTURES = Path(__file__).resolve().parents[1] / "ui" / "src" / "lib" / "__fixtures__"


#: Where the tests find PostgreSQL. There is no fallback to an in-process
#: database, on purpose: a test suite that quietly runs against something
#: other than the engine production uses will pass on SQL the real one
#: rejects, which is exactly the class of bug this project just spent a port
#: discovering.
TEST_DATABASE_URL = os.environ.get(
    "PROFITDOG_TEST_DATABASE_URL",
    os.environ.get(
        "PROFITDOG_DATABASE_URL",
        "postgresql://profitdog:profitdog@127.0.0.1:55432/profitdog_test",
    ),
)


def pytest_configure(config):
    """Fail early and clearly when there is no database to test against."""
    try:
        with psycopg.connect(TEST_DATABASE_URL, connect_timeout=5) as conn:
            conn.execute("SELECT 1")
    except psycopg.Error as exc:
        raise pytest.UsageError(
            "cannot reach PostgreSQL at %s. "
            "Start one with `docker compose up -d postgres`, or point "
            "PROFITDOG_TEST_DATABASE_URL somewhere else. The driver said: %s"
            % (TEST_DATABASE_URL.rsplit("@", 1)[-1], str(exc).strip())
        ) from exc


@pytest.fixture()
def db():
    """A database of one's own, as a PostgreSQL schema.

    A schema rather than a whole database: `CREATE DATABASE` cannot run inside
    a transaction, takes a template lock that serialises the suite, and costs
    far more than the isolation is worth. A schema gives every test its own
    namespace of tables, created and dropped in milliseconds, and two tests
    cannot see each other's rows any more than two databases could.
    """
    name = "test_" + uuid.uuid4().hex[:16]
    database = open_database(TEST_DATABASE_URL, schema=name)
    try:
        yield database
    finally:
        try:
            database.drop_schema()
        finally:
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


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


@dataclass
class Account:
    """One signed-in person, with one linked PC.

    Carries both credentials a test might need: the session cookie a browser
    would hold, and the bearer token the agent on that PC would upload with.
    Having both on one object is what makes a cross-account test easy to write
    honestly -- there is no way to accidentally read with the wrong one,
    because you have to name which you are using.
    """

    user_id: int
    email: str
    session: str
    agent_id: str
    credential: str
    sim: "AgentSim"

    @property
    def cookies(self) -> dict:
        return {auth.SESSION_COOKIE: self.session}

    @property
    def bearer(self) -> dict:
        return {"authorization": "Bearer " + self.credential}


@pytest.fixture()
def accounts(db):
    """Make an account with a linked agent, through the real linking flow.

    Deliberately not an INSERT: going through start/approve/redeem means these
    fixtures break if the handshake breaks, and every test that uses them is
    standing on the same path a real PC takes.
    """
    made: list[Account] = []

    def make(email: str, *, agent_id: str | None = None, label: str | None = None):
        index = len(made) + 1
        agent = agent_id or ("agent-%d" % index)
        with db.write() as conn:
            cursor = conn.execute(
                "INSERT INTO users (google_sub, email, created_at)"
                " VALUES (%s, %s, %s) RETURNING id",
                ("sub-%s" % email, email, utc_now()),
            )
            user_id = int(cursor.fetchone()[0])

        request = auth.start_link(db, agent_id=agent, label=label or email)
        assert auth.approve_link(db, code=request.code, user_id=user_id)
        credential = auth.redeem_link(
            db, code=request.code, device_secret=request.device_secret
        )

        account = Account(
            user_id=user_id,
            email=email,
            session=auth.create_session(db, user_id),
            agent_id=agent,
            credential=credential,
            sim=AgentSim(agent_id=agent, boot_id="boot-%d" % index),
        )
        made.append(account)
        return account

    return make


@pytest.fixture()
def alice(accounts):
    return accounts("alice@example.com", agent_id="agent-alice")


@pytest.fixture()
def bob(accounts):
    return accounts("bob@example.com", agent_id="agent-bob")
