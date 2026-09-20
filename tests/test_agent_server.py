"""The agent and the server, talking to each other for real.

Every other test exercises one half. This one wires the actual `Uplink` to the
actual API and then breaks the connection in each of the ways a gaming PC and a
server on someone's network genuinely break:

- the server is not up yet when you start playing;
- it goes away mid-match and comes back;
- the batch lands but the acknowledgement does not;
- the server is restored from a backup and is now behind the agent.

In all four the requirement is the same and is the reason the system is split
this way at all: no fact is lost, nothing is stored twice, and the agent needs
no human intervention to recover.
"""

from __future__ import annotations

import json
import threading
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from profitdog_agent.outbox import Outbox
from profitdog_agent.uplink import MIN_BACKOFF_SEC, Uplink
from profitdog_server import auth
from profitdog_server.api import create_app
from profitdog_server.config import Settings
from .conftest import TEST_DATABASE_URL


class Wire:
    """Routes the uplink's HTTP calls into the test client, or drops them.

    The uplink is used exactly as it ships — same batching, same ack handling,
    same cursor re-read after a failure. Only the socket underneath is
    replaced, so what is under test is the real delivery logic.
    """

    def __init__(self, client: TestClient, credential: str) -> None:
        self.client = client
        # The credential the PC was given when it was linked. A real uplink
        # sends this on every call; the wire does the same so these tests go
        # through the same authorisation the live path does.
        self.headers = {"authorization": "Bearer " + credential}
        self.up = True
        self.swallow_acks = False
        self.posts = 0

    def install(self, uplink: Uplink) -> None:
        def post(path: str, payload: dict) -> dict:
            if not self.up:
                raise urllib.error.URLError("connection refused")
            self.posts += 1
            response = self.client.post(path, json=payload, headers=self.headers)
            if response.status_code != 200:
                raise urllib.error.HTTPError(path, response.status_code, "", {}, None)
            if self.swallow_acks:
                # The server stored it; the reply never arrived.
                raise urllib.error.URLError("connection reset while reading the reply")
            return response.json()

        def get(path: str) -> dict:
            if not self.up:
                raise urllib.error.URLError("connection refused")
            response = self.client.get(path, headers=self.headers)
            return response.json()

        uplink._post = post  # noqa: SLF001 - swapping the transport is the point
        uplink._get = get  # noqa: SLF001


def attempt(uplink: Uplink) -> bool:
    """One delivery attempt, as `Uplink.run` makes it: failures are expected.

    `drain` raises so the retry loop can back off; these tests drive it by
    hand, so they swallow the same exceptions `run` does.
    """
    try:
        uplink.drain()
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


@pytest.fixture()
def wired(tmp_path, db, accounts):
    settings = Settings(
        database_url=TEST_DATABASE_URL,
        ui_dist=tmp_path / "no-ui",
        host="127.0.0.1",
        port=0,
        collector=False,
    )
    app = create_app(db=db, settings=settings)
    with TestClient(app) as client:
        outbox = Outbox(tmp_path / "outbox.sqlite3", boot_id="boot-1")
        # This PC belongs to somebody: the server will not take facts from an
        # agent that was never linked.
        account = accounts(
            "player@example.com", agent_id=outbox.agent_id, label="gaming-pc"
        )
        # The same person, reading in a browser: the uploaded facts have to
        # come back out through the API they own.
        client.cookies.set(auth.SESSION_COOKIE, account.session)
        uplink = Uplink(outbox, "", label="gaming-pc")
        wire = Wire(client, account.credential)
        wire.install(uplink)
        yield outbox, uplink, wire, db, client
        outbox.close()


def a_match(outbox: Outbox, start: datetime, readings: list[int]) -> None:
    """The facts a real agent produces while playing one match."""
    clock = start
    outbox.append("map", "breadcrumbs", {"map": "Kavkazi"},
                  source_ts=clock.isoformat(), observed_at=clock.isoformat())
    for cash in readings:
        clock += timedelta(seconds=2)
        outbox.append(
            "presence", "rich_presence",
            {"game_state": "playing", "faction": "bravo",
             "profit_loss": f"{'-' if cash < 0 else '+'}${abs(cash):,}"},
            source_ts=clock.isoformat(), observed_at=clock.isoformat(),
        )
    for _ in range(5):
        clock += timedelta(seconds=2)
        outbox.append("presence", "rich_presence", {"game_state": "mainmenu"},
                      source_ts=clock.isoformat(), observed_at=clock.isoformat())


START = datetime(2026, 9, 19, 20, 0, tzinfo=timezone.utc)


def test_a_clean_run_delivers_everything_and_empties_the_outbox(wired):
    outbox, uplink, _wire, db, client = wired
    a_match(outbox, START, [0, 0, -5260, -4000, -1000, 2000, 3954])

    assert attempt(uplink)

    assert outbox.depth() == 0, "facts were kept after being acknowledged"
    assert db.scalar("SELECT COUNT(*) FROM matches") == 1
    # Seven readings, six samples: the first `playing` poll is spent confirming
    # that a match has started and is not yet part of one. The CSV-era tracker
    # behaved identically — its first logged row was always the confirming
    # poll — so imported history and live history have the same shape.
    assert db.scalar("SELECT COUNT(*) FROM cash_samples") == 6

    # And it derived end to end, under a ruleset, with lives in it.
    payload = json.loads(client.get("/api/matches?range=all").text)
    match = payload["matches"][0]
    assert match["profit"] == 3954
    assert match["map_display"] == "Bakurani"
    assert match["ruleset"] == "v1"
    assert match["lives"] >= 1


def test_playing_with_no_server_loses_nothing(wired):
    """The usual case: the agent starts before the server does."""
    outbox, uplink, wire, db, _client = wired
    wire.up = False

    a_match(outbox, START, [0, 0, -5260, 1200])
    assert not attempt(uplink), "a dead server reported success"

    assert outbox.depth() > 0, "facts were dropped while offline"
    assert db.scalar("SELECT COUNT(*) FROM cash_samples") == 0

    # The server arrives.
    wire.up = True
    assert attempt(uplink)

    assert outbox.depth() == 0
    assert db.scalar("SELECT COUNT(*) FROM cash_samples") == 3
    assert db.scalar("SELECT COUNT(*) FROM matches") == 1


def test_an_outage_mid_match_is_stitched_back_together(wired):
    outbox, uplink, wire, db, _client = wired
    clock = START
    outbox.append("map", "breadcrumbs", {"map": "Kavkazi"},
                  source_ts=clock.isoformat(), observed_at=clock.isoformat())
    for cash in (0, 0, -5260):
        clock += timedelta(seconds=2)
        outbox.append("presence", "rich_presence",
                      {"game_state": "playing", "faction": "bravo",
                       "profit_loss": f"-${abs(cash):,}" if cash < 0 else f"+${cash}"},
                      source_ts=clock.isoformat(), observed_at=clock.isoformat())
    assert attempt(uplink)
    assert db.scalar("SELECT COUNT(*) FROM cash_samples") == 2

    # The network goes while play continues.
    wire.up = False
    for cash in (-4000, -2000, 500, 3000):
        clock += timedelta(seconds=2)
        outbox.append("presence", "rich_presence",
                      {"game_state": "playing", "faction": "bravo",
                       "profit_loss": f"-${abs(cash):,}" if cash < 0 else f"+${cash}"},
                      source_ts=clock.isoformat(), observed_at=clock.isoformat())
    attempt(uplink)
    wire.up = True
    assert attempt(uplink)

    # One match, every reading, in order — not two matches with a hole.
    assert db.scalar("SELECT COUNT(*) FROM matches") == 1
    rows = db.query("SELECT cash FROM cash_samples ORDER BY elapsed_sec")
    assert [r["cash"] for r in rows] == [0, -5260, -4000, -2000, 500, 3000]


def test_a_lost_acknowledgement_does_not_duplicate_anything(wired):
    """The server stored the batch; the reply never came back."""
    outbox, uplink, wire, db, _client = wired
    a_match(outbox, START, [0, 0, -5260, -4000, 1200])

    wire.swallow_acks = True
    assert not attempt(uplink)  # the server has it; the agent does not know
    stored = db.scalar("SELECT COUNT(*) FROM cash_samples")
    assert stored == 4
    assert outbox.depth() > 0, "the agent forgot facts it was never told about"

    # It resends. The idempotency gate turns that into a no-op.
    wire.swallow_acks = False
    assert attempt(uplink)

    assert db.scalar("SELECT COUNT(*) FROM cash_samples") == stored
    assert db.scalar("SELECT COUNT(*) FROM matches") == 1
    assert outbox.depth() == 0


def test_the_cursor_lets_a_restarted_agent_stop_resending(wired, tmp_path):
    """A lost ack that the agent only learns about on its next start."""
    outbox, uplink, wire, db, _client = wired
    a_match(outbox, START, [0, 0, -5260, 900])
    wire.swallow_acks = True
    attempt(uplink)
    assert outbox.depth() > 0

    outbox.close()

    # New process, same outbox, same agent id.
    restarted = Outbox(tmp_path / "outbox.sqlite3", boot_id="boot-2")
    second = Uplink(restarted, "", label="gaming-pc")
    wire.swallow_acks = False
    wire.install(second)

    # Asking where the server is resolves it without sending anything.
    posts_before = wire.posts
    cursor = second.sync_cursor()
    assert cursor > 0
    assert restarted.depth() == 0, "the outbox was not cleared by the cursor"
    assert wire.posts == posts_before, "it resent facts the server already had"
    restarted.close()


def test_a_restored_backup_gets_everything_the_agent_still_holds(wired, tmp_path):
    """The server's cursor goes backwards. The agent must not follow it down."""
    outbox, uplink, wire, db, _client = wired
    a_match(outbox, START, [0, 0, -5260, 800])
    assert attempt(uplink)
    assert outbox.depth() == 0
    delivered = db.scalar("SELECT COUNT(*) FROM cash_samples")

    # More play, held back.
    wire.up = False
    a_match(outbox, START + timedelta(hours=1), [0, 0, -3000, 2500])
    attempt(uplink)
    held = outbox.depth()
    assert held > 0

    # The server is restored from a backup taken before any of this: its cursor
    # is now behind the agent's.
    with db.write() as conn:
        conn.execute("UPDATE agents SET acked_through = 0")
    wire.up = True

    before = outbox.acked_through
    uplink.sync_cursor()
    # A lower cursor is not permission to forget less, and not a reason to
    # resurrect what was already acknowledged.
    assert outbox.acked_through == before
    assert outbox.depth() == held

    assert attempt(uplink)
    assert outbox.depth() == 0
    # The second match arrived; the first was not duplicated by the rewind.
    assert db.scalar("SELECT COUNT(*) FROM matches") == 2
    assert db.scalar("SELECT COUNT(*) FROM cash_samples") == delivered + 3


def test_a_replaced_database_does_not_wedge_the_agent_forever(wired):
    """The server is rebuilt. The agent has been running the whole time.

    This is the shape of a migration: the facts are gone from the server and
    so is every envelope it recorded, while the agent's sequence numbers carry
    on from where they were -- the early ones were acknowledged long ago and
    deleted. Nothing can ever fill that gap.

    Before the fix the cursor stayed at 0 and the agent posted the same batch
    every couple of seconds, was told it was all duplicates, and never
    drained. The requirement is only that it recovers on its own.
    """
    outbox, uplink, wire, db, _client = wired
    a_match(outbox, START, [0, 0, -5260, 900])
    assert attempt(uplink)
    assert outbox.depth() == 0

    # The new database: this agent's account and credential survive (it is
    # still linked), but the ledger of what it has already sent does not --
    # which is what the cursor is walked over.
    with db.write() as conn:
        conn.execute("DELETE FROM ingested_envelopes")
        conn.execute("UPDATE agents SET acked_through = 0")
    before = int(db.scalar("SELECT COUNT(*) FROM matches"))

    a_match(outbox, START + timedelta(hours=1), [0, 0, -3000, 2500])
    held = outbox.depth()
    assert held > 0

    posts_before = wire.posts
    assert attempt(uplink)

    assert outbox.depth() == 0, "the outbox never drained"
    assert wire.posts - posts_before <= 2, "it resent the same batch"
    # The play that happened after the rebuild landed, rather than being
    # stored and then acknowledged into a cursor that never moved.
    assert db.scalar("SELECT COUNT(*) FROM matches") == before + 1


def test_an_old_server_that_cannot_move_its_cursor_still_unblocks_the_queue(wired):
    """The agent's own way out, for a server that has not been fixed yet.

    The released EXE meets a server whose cursor is stuck below what the agent
    still holds. Every batch comes back acknowledged through 0, and since a
    batch is the head of the queue, the evening's play behind it is never
    offered at all -- which is what "I ran it and there is no profit on the
    page" looks like from the outside.

    What the server reports about the batch is the proof the cursor is not
    giving: it accounted for every fact in it. So they are forgotten and the
    queue moves.
    """
    outbox, uplink, _wire, _db, _client = wired
    a_match(outbox, START, [0, 0, -5260, 900])
    already_sent = max(fact.agent_seq for fact in outbox.pending(1000))
    a_match(outbox, START + timedelta(hours=1), [0, 0, -3000, 2500])
    sent: list[list[int]] = []

    def post(path: str, payload: dict) -> dict:
        seqs = [fact["agent_seq"] for fact in payload["facts"]]
        sent.append(seqs)
        # An old server: it already holds the first match, it takes the rest,
        # and its cursor is wedged behind sequence numbers nobody has.
        duplicates = len([seq for seq in seqs if seq <= already_sent])
        return {
            "acked_through": 0,
            "accepted": len(seqs) - duplicates,
            "duplicates": duplicates,
            "server_seq": 0,
        }

    uplink._post = post  # noqa: SLF001 - swapping the transport is the point

    assert uplink.flush_once() > 0, "nothing was forgotten, so nothing can move"
    assert outbox.depth() == 0, "the queue is still blocked behind its own head"
    assert len(sent) == 1


def test_facts_the_server_did_not_account_for_are_kept(wired):
    """The line the recovery must not cross.

    A server that says it took fewer facts than it was sent has lost some of
    them, and a cursor at 0 is then telling the truth. Forgetting the batch
    here would lose exactly the facts it failed to store.
    """
    outbox, uplink, _wire, _db, _client = wired
    a_match(outbox, START, [0, 0, -5260, 900])
    held = outbox.depth()

    def post(path: str, payload: dict) -> dict:
        seqs = payload["facts"]
        return {
            "acked_through": 0,
            "accepted": len(seqs) - 1,  # one went missing
            "duplicates": 0,
            "server_seq": 0,
        }

    uplink._post = post  # noqa: SLF001 - swapping the transport is the point

    assert uplink.flush_once() == 0
    assert outbox.depth() == held, "it forgot a fact the server never took"


def test_a_server_that_acknowledges_nothing_is_not_treated_as_progress(wired):
    """The loop's half of the same bug.

    A round trip that moves nothing means the next one would send the
    identical facts. Calling that a success reset the back-off and sent them
    two seconds later, forever.
    """
    outbox, uplink, _wire, _db, _client = wired
    a_match(outbox, START, [0, 0, -5260, 900])
    stop = threading.Event()
    posts = []

    def post(path: str, payload: dict) -> dict:
        posts.append(path)
        stop.set()
        # Stored some, lost some, acknowledged nothing: no progress is
        # possible and none may be claimed.
        return {
            "acked_through": 0,
            "accepted": len(payload["facts"]) - 1,
            "duplicates": 0,
            "server_seq": 0,
        }

    uplink._post = post  # noqa: SLF001 - swapping the transport is the point
    uplink._get = lambda path: {"acked_through": 0}  # noqa: SLF001

    uplink.run(stop, interval=0.01)

    assert len(posts) == 1, "it kept sending against a cursor that never moved"
    assert uplink._backoff > MIN_BACKOFF_SEC, "no back-off after a useless round trip"  # noqa: SLF001
    assert "not acknowledging" in (uplink.status.last_error or "")
    assert outbox.depth() > 0, "it deleted facts the server never acknowledged"


def test_the_agent_sends_no_conclusions(wired):
    """Whatever crosses the wire is an observation, never a derived figure."""
    outbox, _uplink, _wire, _db, _client = wired
    a_match(outbox, START, [0, 0, -5260, 1200])
    outbox.append("xp", "save_file", {"roles": {"Wardog": 82}})
    outbox.append("spawn", "breadcrumbs", {"at": START.isoformat()})

    forbidden = {
        "kit_cost", "kitCost", "life", "lives", "profit", "net", "break_even",
        "breakEven", "earned", "match", "match_key", "delta", "gain",
    }
    for fact in outbox.pending(limit=1000):
        assert not (forbidden & set(fact.body)), (
            f"the agent sent a conclusion in a {fact.kind} fact: {fact.body}"
        )
