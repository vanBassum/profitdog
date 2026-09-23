"""Is that PC running, and is it the build it should be?

Two questions the site could not answer. It knew which build each PC had last
reported, and nothing about whether the PC was still there -- because an agent
with an empty outbox made no requests at all, so "watching happily for six
hours" and "switched off six hours ago" produced identical rows.

The fix is a heartbeat: a batch with no facts in it, sent every minute whether
or not there is anything to report. These tests hold three things in place:

- the agent sends one, and does not send one per tick;
- an empty batch is an ordinary batch to the server -- it moves `last_seen_at`
  and the reported version, and writes no facts;
- the verdicts drawn from that ("online", "outdated") are computed once, on the
  server, and never guessed at when either side stayed silent.

The rule running through all of it: *not knowing is an answer*. An agent too
old to report a version is "version unknown", not "old"; a server that was
never told which build is current says nothing about currency rather than
calling a perfectly good agent out of date.
"""

from __future__ import annotations

import threading
import time
import urllib.error
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from profitdog_agent.outbox import Outbox
from profitdog_agent.uplink import Uplink
from profitdog_protocol import HEARTBEAT_INTERVAL_SEC, OFFLINE_AFTER_SEC, FactBatch
from profitdog_server import agents as agent_status

NOW = datetime(2026, 9, 21, 20, 0, 0, tzinfo=timezone.utc)


def row(*, version=None, seen_ago=None, label="GAMING-PC", agent_id="a1"):
    """One `agents` row as the database hands it over."""
    return {
        "agent_id": agent_id,
        "label": label,
        "agent_version": version,
        "last_seen_at": (
            None if seen_ago is None else (NOW - timedelta(seconds=seen_ago)).isoformat()
        ),
    }


# ---------------------------------------------------------------------------
# Is it running?
# ---------------------------------------------------------------------------


def test_a_pc_that_checked_in_a_moment_ago_is_online():
    status = agent_status.describe(row(seen_ago=5), current_version=None, now=NOW)
    assert status.online
    assert status.silent_for == pytest.approx(5, abs=1)


def test_a_missed_heartbeat_does_not_kill_a_pc():
    """Three heartbeats of grace, and this is why.

    A missed request is the ordinary cost of a laptop lid, a sleeping Wi-Fi
    radio or a server restart. A status that flickers to "offline" every time
    one is dropped is a status nobody looks at twice, which makes it worse than
    no status at all.
    """
    silent = agent_status.describe(
        row(seen_ago=HEARTBEAT_INTERVAL_SEC * 2), current_version=None, now=NOW
    )
    assert silent.online


def test_a_pc_that_has_gone_quiet_is_offline():
    status = agent_status.describe(
        row(seen_ago=OFFLINE_AFTER_SEC + 1), current_version=None, now=NOW
    )
    assert not status.online


def test_a_pc_that_has_never_reported_is_not_quietly_called_online():
    status = agent_status.describe(row(seen_ago=None), current_version=None, now=NOW)
    assert not status.online
    assert status.silent_for is None


def test_a_timestamp_nobody_can_read_is_never_heard_from_rather_than_a_crash():
    """The honest failure. A row whose stamp cannot be parsed is a row we have
    heard nothing usable from, which is what "never" already means -- and far
    better than a page that 500s over one bad column."""
    status = agent_status.describe(
        {"agent_id": "a1", "label": None, "agent_version": None,
         "last_seen_at": "not a date"},
        current_version=None,
        now=NOW,
    )
    assert status.silent_for is None
    assert not status.online


def test_how_long_ago_is_said_in_words_somebody_would_use():
    assert agent_status.since_text(5) == "just now"
    assert agent_status.since_text(60 * 7) == "7 minutes ago"
    assert agent_status.since_text(3600) == "an hour ago"
    assert agent_status.since_text(3600 * 5) == "5 hours ago"
    assert agent_status.since_text(86400) == "yesterday"
    assert agent_status.since_text(86400 * 4) == "4 days ago"
    assert agent_status.since_text(None) == "never"


# ---------------------------------------------------------------------------
# Is it the current build?
# ---------------------------------------------------------------------------


def test_a_matching_build_is_current():
    assert agent_status.version_state("1.4.0", "1.4.0") == "current"


def test_a_lower_build_has_an_update_waiting():
    assert agent_status.version_state("1.3.9", "1.4.0") == "outdated"
    assert agent_status.version_state("0.9.0", "1.0.0") == "outdated"


def test_a_server_that_was_not_told_says_nothing_about_currency():
    """The default on a checkout, and on any image built without the stamp.

    Silence is the only correct output here. The alternative is telling
    somebody their perfectly good agent is out of date because the server has
    no idea what date it is.
    """
    assert agent_status.version_state("1.4.0", None) == "unknown"
    assert agent_status.version_state("1.4.0", "") == "unknown"


def test_an_agent_too_old_to_report_a_version_is_unknown_not_outdated():
    assert agent_status.version_state(None, "1.4.0") == "unknown"


def test_a_checkout_is_a_dev_build_and_not_nagged():
    """`0.0.0+dev` is a real answer. Offering somebody a download link for the
    tree they are editing is noise, and calling it outdated is wrong: it is
    not behind the release, it is beside it."""
    assert agent_status.version_state("0.0.0+dev", "1.4.0") == "dev"


def test_an_agent_ahead_of_the_server_is_never_told_to_downgrade():
    """A PC on a newer build than the server knows about means the *server* is
    behind. There is nothing useful to tell the player about that, and "update
    to 1.3.0" would be advice to go backwards."""
    assert agent_status.version_state("1.5.0", "1.4.0") == "current"


def test_two_versions_that_cannot_be_ordered_are_still_not_the_same_build():
    """No invented ordering. They differ, which is all that can be said."""
    assert agent_status.version_state("nightly", "1.4.0") == "outdated"


def test_a_suffix_does_not_stop_a_release_matching_its_number():
    assert agent_status.version_state("1.4.0-rc1", "1.4.0") == "current"


# ---------------------------------------------------------------------------
# The agent's end: saying so when there is nothing to say
# ---------------------------------------------------------------------------


class FakeServer:
    """Counts what the uplink sends, and answers like the real endpoint."""

    def __init__(self) -> None:
        self.batches: list[dict] = []
        self.up = True

    def post(self, path: str, payload: dict) -> dict:
        if not self.up:
            raise urllib.error.URLError("connection refused")
        self.batches.append(payload)
        return {"acked_through": 0, "accepted": 0, "duplicates": 0, "server_seq": 0}

    def get(self, path: str) -> dict:
        if not self.up:
            raise urllib.error.URLError("connection refused")
        return {"agent_id": "a", "acked_through": 0, "server_seq": 0}

    def install(self, uplink: Uplink) -> None:
        uplink._post = self.post  # noqa: SLF001 - swapping the transport is the point
        uplink._get = self.get  # noqa: SLF001


@pytest.fixture()
def idle(tmp_path):
    """An uplink with an empty outbox and nowhere to get facts from."""
    outbox = Outbox(tmp_path / "outbox.sqlite3")
    uplink = Uplink(outbox, "http://server.invalid", label="GAMING-PC", version="1.4.0")
    server = FakeServer()
    server.install(uplink)
    try:
        yield uplink, server
    finally:
        outbox.close()


def test_an_idle_agent_still_tells_the_server_it_is_there(idle):
    uplink, server = idle
    assert uplink.drain() == 0
    assert server.batches == [], "draining an empty outbox should send nothing"

    uplink.heartbeat()
    assert len(server.batches) == 1
    sent = FactBatch.from_json(server.batches[0])
    assert sent.facts == []
    # The whole reason it is a batch and not a new endpoint: it carries the
    # things ingest already knows what to do with.
    assert sent.label == "GAMING-PC"
    assert sent.version == "1.4.0"
    assert sent.agent_id == uplink.outbox.agent_id
    assert sent.boot_id == uplink.outbox.boot_id


def test_the_first_heartbeat_goes_out_at_startup(idle):
    """So a PC that is switched on and never played still reports its build.

    Waiting a minute would mean a fresh install shows nothing on the page
    somebody is refreshing while they wait for it.
    """
    uplink, server = idle
    assert uplink.heartbeat_due()

    uplink.heartbeat()
    assert len(server.batches) == 1
    assert not uplink.heartbeat_due(), "one a minute, not one a tick"


def test_sending_facts_postpones_the_heartbeat(idle):
    """A busy agent has already said everything a heartbeat would.

    `last_seen_at` moves on every batch, so a heartbeat behind one is a
    request that buys nothing. This is why the clock is reset by the send and
    not by the socket.
    """
    uplink, server = idle
    uplink.outbox.append(
        "agent_status", "agent", {"status": "started"}, observed_at=NOW.isoformat()
    )

    uplink.drain()

    assert len(server.batches) == 1
    assert FactBatch.from_json(server.batches[0]).facts, "that was the real batch"
    assert not uplink.heartbeat_due()


def test_a_heartbeat_is_owed_again_once_the_interval_has_passed(idle, monkeypatch):
    uplink, _ = idle
    uplink.heartbeat()
    assert not uplink.heartbeat_due()

    later = uplink._told_server + HEARTBEAT_INTERVAL_SEC  # noqa: SLF001
    monkeypatch.setattr("time.monotonic", lambda: later)
    assert uplink.heartbeat_due()


def test_a_heartbeat_that_cannot_be_delivered_raises_like_any_other_batch(idle):
    """So the run loop backs off over it instead of treating silence as health."""
    uplink, server = idle
    server.up = False
    with pytest.raises(urllib.error.URLError):
        uplink.heartbeat()


def test_the_loop_sends_one_without_being_asked(idle):
    """The wiring, not the pieces.

    `run` is where an empty drain, the heartbeat clock and the back-off meet,
    and every one of those was previously satisfied by doing nothing at all.
    """
    uplink, server = idle
    stop = threading.Event()
    worker = threading.Thread(target=uplink.run, args=(stop, 0.01), daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not server.batches and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        stop.set()
        worker.join(timeout=5)

    assert server.batches, "an idle agent left the server with nothing to go on"
    assert FactBatch.from_json(server.batches[0]).facts == []
    # One a minute. A loop ticking every 10ms must not produce a request every
    # time round, which is the failure that would have gone unnoticed until it
    # was a hundred requests a second in production.
    assert len(server.batches) == 1
    assert uplink.status.connected


def test_the_loop_does_not_call_an_empty_drain_a_connection(idle):
    """A drain with nothing to send makes no request, so it proves nothing.

    Reporting "connected" on the strength of it is how a server that had been
    down all evening still looked healthy on the gaming PC.
    """
    uplink, server = idle
    server.up = False
    stop = threading.Event()
    worker = threading.Thread(target=uplink.run, args=(stop, 0.01), daemon=True)
    worker.start()
    time.sleep(0.2)
    stop.set()
    worker.join(timeout=5)

    assert not uplink.status.connected
    assert uplink.status.last_error


# ---------------------------------------------------------------------------
# The server's end: an empty batch is an ordinary batch
# ---------------------------------------------------------------------------


def test_a_heartbeat_moves_last_seen_without_writing_a_fact(ingestor, agent, db):
    agent.play([100])
    ingestor.ingest(agent.drain(version="1.4.0"))
    before = db.query(
        "SELECT last_seen_at FROM agents WHERE agent_id = %s", (agent.agent_id,)
    )[0]["last_seen_at"]
    facts_before = db.query("SELECT count(*) AS n FROM ingested_envelopes")[0]["n"]

    ingestor.ingest(
        FactBatch(
            agent_id=agent.agent_id, boot_id=agent.boot_id, facts=[], version="1.4.0"
        )
    )

    after = db.query(
        "SELECT last_seen_at FROM agents WHERE agent_id = %s", (agent.agent_id,)
    )[0]["last_seen_at"]
    assert after > before
    assert db.query("SELECT count(*) AS n FROM ingested_envelopes")[0]["n"] == facts_before


def test_a_heartbeat_reports_an_upgrade_before_the_next_match(ingestor, agent, db):
    """Which is the point of sending version on it. Somebody who updates the
    agent and then goes to look should not have to play a match first."""
    agent.play([100])
    ingestor.ingest(agent.drain(version="1.3.0"))

    ingestor.ingest(
        FactBatch(
            agent_id=agent.agent_id, boot_id=agent.boot_id, facts=[], version="1.4.0"
        )
    )

    assert db.query(
        "SELECT agent_version FROM agents WHERE agent_id = %s", (agent.agent_id,)
    )[0]["agent_version"] == "1.4.0"


# ---------------------------------------------------------------------------
# What the page says
# ---------------------------------------------------------------------------


def page(rows, *, current_version=None, releases="https://example.invalid/releases"):
    from profitdog_server.api import pages

    return pages.download_page(
        available=True,
        releases_url=releases,
        agents=agent_status.describe_all(
            rows, current_version=current_version, now=NOW
        ),
    )


def test_the_page_says_which_pcs_are_running():
    html = page([
        row(agent_id="a1", label="GAMING-PC", version="1.4.0", seen_ago=10),
        row(agent_id="a2", label="LAPTOP", version="1.4.0", seen_ago=86400),
    ])
    assert "running" in html
    assert "last seen yesterday" in html


def test_the_page_offers_the_update_to_the_pc_that_needs_it():
    html = page(
        [row(version="1.3.0", seen_ago=10)],
        current_version="1.4.0",
    )
    assert "Update to 1.4.0" in html
    assert "https://example.invalid/releases" in html


def test_the_page_does_not_nag_a_pc_that_is_current():
    html = page([row(version="1.4.0", seen_ago=10)], current_version="1.4.0")
    assert "Update to" not in html


def test_the_page_does_not_nag_when_the_server_does_not_know_the_current_build():
    html = page([row(version="1.3.0", seen_ago=10)], current_version=None)
    assert "Update to" not in html
    assert "1.3.0" in html


def test_the_page_still_shows_a_version_it_cannot_judge():
    """The version is useful on its own -- it is what somebody reads out when
    asking for help. Only the verdict depends on the server knowing more."""
    html = page([row(version="0.0.0+dev", seen_ago=10)], current_version="1.4.0")
    assert "0.0.0+dev" in html
    assert "Update to" not in html


def test_an_update_is_still_named_when_there_is_nowhere_to_link_to():
    """A server with no releases URL still knows a newer build exists. Saying
    so without a link beats saying nothing."""
    html = page([row(version="1.3.0", seen_ago=10)], current_version="1.4.0",
                releases="")
    assert "Update to 1.4.0" in html


def test_the_page_is_unchanged_for_someone_with_no_pcs_yet():
    assert "Your PCs" not in page([])


# ---------------------------------------------------------------------------
# What the header asks for
# ---------------------------------------------------------------------------


@contextmanager
def signed_in(tmp_path, db, accounts, *, current_version=""):
    """A browser with a session, a linked PC, and a server of a known vintage."""
    from fastapi.testclient import TestClient

    from profitdog_server import auth
    from profitdog_server.api import create_app
    from profitdog_server.config import Settings

    from .conftest import TEST_DATABASE_URL

    settings = Settings(
        database_url=TEST_DATABASE_URL,
        ui_dist=tmp_path / "no-ui",
        host="127.0.0.1",
        port=0,
        collector=False,
        agent_version=current_version,
    )
    account = accounts("owner@example.com", agent_id="agent-test")
    with TestClient(create_app(db=db, settings=settings)) as client:
        client.cookies.set(auth.SESSION_COOKIE, account.session)
        yield client, account


def reported(client, account, version):
    """One batch from that PC, claiming a build."""
    account.sim.play([100])
    response = client.post(
        "/api/agent/facts",
        json=account.sim.drain(label="GAMING-PC", version=version).to_json(),
        headers=account.bearer,
    )
    assert response.status_code == 200, response.text


def test_the_header_is_told_whether_the_pc_is_running(tmp_path, db, accounts):
    """The same verdict the page draws, over JSON.

    Computed on the server rather than in the browser on purpose:
    `last_seen_at` was written by this machine's clock, and a laptop an hour
    out would otherwise see every agent as either dead or immortal.
    """
    with signed_in(tmp_path, db, accounts, current_version="1.4.0") as (http, account):
        reported(http, account, "1.3.0")
        body = http.get("/api/agents").json()

    listed = body["agents"][0]
    assert listed["online"] is True
    assert listed["version"] == "1.3.0"
    assert listed["version_state"] == "outdated"
    assert body["current_version"] == "1.4.0"
    # The raw row survives alongside the verdict, for anyone looking into an
    # agent that is behaving oddly rather than just checking on it.
    assert listed["acked_through"] > 0


def test_a_server_that_knows_of_no_release_offers_no_verdict(tmp_path, db, accounts):
    """A checkout, or an image built without the stamp. It still reports the
    version; it just does not tell anybody they are behind."""
    with signed_in(tmp_path, db, accounts) as (http, account):
        reported(http, account, "1.3.0")
        body = http.get("/api/agents").json()

    assert body["current_version"] is None
    assert body["agents"][0]["version"] == "1.3.0"
    assert body["agents"][0]["version_state"] == "unknown"
