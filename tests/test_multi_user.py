"""Two accounts on one server, and the wall between them.

This file exists to be the thing that fails if the wall ever comes down. Every
test is the same shape: Alice plays, Bob asks, and Bob must not learn anything.

The two directions are tested separately because they fail separately:

- **Reading.** Every route that answers a question has to answer it for the
  account that asked. A route that forgot its `WHERE` returns everyone's data
  and nothing else complains.
- **Uploading.** A credential decides whose account a batch lands in. A server
  that believed the agent id in the payload would let anyone write into anyone
  else's history by editing one JSON field.

Bob's requests are all well-formed and authenticated. That is deliberate: the
interesting failure is not an attacker, it is a second ordinary user who exists
at all.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from profitdog_server import auth
from profitdog_server.api import create_app
from profitdog_server.config import Settings
from .conftest import TEST_DATABASE_URL


@pytest.fixture()
def app(tmp_path, db):
    settings = Settings(
        database_url=TEST_DATABASE_URL,
        ui_dist=tmp_path / "no-ui",
        host="127.0.0.1",
        port=0,
        collector=False,
    )
    return create_app(db=db, settings=settings)


@pytest.fixture()
def http(app):
    with TestClient(app) as client:
        yield client


def as_user(client: TestClient, account):
    client.cookies.clear()
    client.cookies.set(auth.SESSION_COOKIE, account.session)
    return client


def play(client: TestClient, account, cash=(0, 0, -5260, -1000, 3954)):
    """One finished match, uploaded by that account's linked PC."""
    account.sim.map("Kavkazi")
    account.sim.play(list(cash))
    account.sim.status("stopped")
    response = client.post(
        "/api/agent/facts",
        json=account.sim.drain().to_json(),
        headers=account.bearer,
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def test_each_account_sees_only_its_own_matches(http, alice, bob):
    play(http, alice)
    play(http, bob, cash=(0, 0, -390, 120))

    mine = as_user(http, alice).get("/api/matches?range=all").json()
    theirs = as_user(http, bob).get("/api/matches?range=all").json()

    assert len(mine["matches"]) == 1
    assert len(theirs["matches"]) == 1
    my_keys = {m["match_key"] for m in mine["matches"]}
    their_keys = {m["match_key"] for m in theirs["matches"]}
    assert my_keys.isdisjoint(their_keys)


def test_totals_do_not_include_the_other_account(http, alice, bob):
    """The summary band is a number, and a wrong one is not obviously wrong."""
    play(http, alice, cash=(0, 0, -1000, -1000))
    play(http, bob, cash=(0, 0, -7000, -7000))

    mine = as_user(http, alice).get("/api/matches?range=all").json()
    assert mine["totals"]["matches"] == 1
    assert mine["available"] == 1
    # Alice spent 1000. If Bob's 7000 leaked in, this is the assertion that
    # notices, and no page would have looked broken.
    assert mine["totals"]["spent"] == 1000


def test_another_accounts_match_is_a_404_not_a_403(http, alice, bob):
    """404, so the reply does not confirm that the key names a real match."""
    play(http, alice)
    key = as_user(http, alice).get("/api/matches?range=all").json()["matches"][0][
        "match_key"
    ]

    as_user(http, bob)
    for path in (
        f"/api/matches/{key}",
        f"/api/matches/{key}/curve",
        f"/api/matches/{key}/overrides",
    ):
        response = http.get(path)
        assert response.status_code == 404, (path, response.status_code)

    # And the same shape as a key that was never real at all.
    invented = http.get("/api/matches/m-nobody-20260101T000000")
    assert invented.status_code == 404
    assert invented.json()["detail"][:9] == http.get(
        f"/api/matches/{key}"
    ).json()["detail"][:9]


def test_history_analysis_and_xp_are_scoped(http, alice, bob):
    play(http, alice)
    play(http, bob)

    theirs = as_user(http, bob)
    history = theirs.get("/api/history?range=all").json()
    assert history["totals"]["matches"] == 1

    analysis = theirs.get("/api/analysis?range=all").json()
    assert analysis["matches"] == 1

    # XP is scoped through the agent rather than the match, because a reading
    # taken between matches has no match to scope it by.
    alice.sim.xp(Medic=5000)
    http.post(
        "/api/agent/facts",
        json=alice.sim.drain().to_json(),
        headers=alice.bearer,
    )
    assert as_user(http, bob).get("/api/xp").json()["totals"] == {}
    assert as_user(http, alice).get("/api/xp").json()["totals"]["Medic"] == 5000


def test_health_counts_are_per_account(http, alice, bob):
    play(http, alice)

    assert as_user(http, bob).get("/api/health").json()["matches"] == 0
    assert as_user(http, alice).get("/api/health").json()["matches"] == 1


def test_agents_are_listed_per_account(http, alice, bob):
    theirs = as_user(http, bob).get("/api/agents").json()["agents"]
    assert [a["agent_id"] for a in theirs] == [bob.agent_id]


def test_nothing_is_readable_without_a_session(http, alice):
    play(http, alice)
    http.cookies.clear()
    for path in (
        "/api/me",
        "/api/matches",
        "/api/history",
        "/api/analysis",
        "/api/metrics",
        "/api/xp",
        "/api/health",
        "/api/agents",
        "/api/stream/cursor",
    ):
        assert http.get(path).status_code == 401, path


def test_an_agent_credential_cannot_read_anything(http, alice):
    """The credential on a gaming PC is upload-only, and that is structural.

    If this ever passes something other than 401, a stolen credential has
    become a way to read somebody's whole history rather than only to write
    rubbish into it.
    """
    play(http, alice)
    http.cookies.clear()
    for path in ("/api/matches", "/api/history", "/api/xp", "/api/health"):
        response = http.get(path, headers=alice.bearer)
        assert response.status_code == 401, (path, response.status_code)


# ---------------------------------------------------------------------------
# Uploading
# ---------------------------------------------------------------------------


def test_a_credential_cannot_upload_as_another_agent(http, alice, bob):
    """The batch claims Bob's agent id; the credential is Alice's."""
    bob.sim.map("Kavkazi")
    bob.sim.play([0, 0, -1000])
    forged = bob.sim.drain().to_json()

    response = http.post("/api/agent/facts", json=forged, headers=alice.bearer)
    assert response.status_code == 403
    # The refusal names the credential's own agent, never the one the batch
    # claimed: saying "that is not agent-bob" would confirm to a stranger that
    # agent-bob exists.
    assert alice.agent_id in response.json()["detail"]
    assert bob.agent_id not in response.json()["detail"]


def test_facts_land_in_the_account_the_credential_belongs_to(http, alice, bob, db):
    play(http, alice)

    owner = db.query_one(
        "SELECT user_id FROM matches ORDER BY id DESC LIMIT 1"
    )["user_id"]
    assert owner == alice.user_id
    assert owner != bob.user_id


def test_uploading_needs_a_credential_at_all(http, alice):
    alice.sim.play([0, 0, -500])
    batch = alice.sim.drain().to_json()

    assert http.post("/api/agent/facts", json=batch).status_code == 401
    assert http.post(
        "/api/agent/facts", json=batch, headers={"authorization": "Bearer pdog_made_up"}
    ).status_code == 401
    # A session is not a credential: a browser may not upload.
    as_user(http, alice)
    assert http.post("/api/agent/facts", json=batch).status_code == 401


def test_a_revoked_credential_stops_working_immediately(http, alice, db):
    play(http, alice)
    credential_id = db.query_one(
        "SELECT id FROM agent_credentials WHERE user_id = %s", (alice.user_id,)
    )["id"]
    assert auth.revoke_credential(
        db, user_id=alice.user_id, credential_id=int(credential_id)
    )

    alice.sim.play([0, 0, -100])
    response = http.post(
        "/api/agent/facts",
        json=alice.sim.drain().to_json(),
        headers=alice.bearer,
    )
    assert response.status_code == 401


def test_one_account_cannot_revoke_anothers_credential(db, alice, bob):
    theirs = db.query_one(
        "SELECT id FROM agent_credentials WHERE user_id = %s", (bob.user_id,)
    )["id"]
    assert not auth.revoke_credential(
        db, user_id=alice.user_id, credential_id=int(theirs)
    )
    # And it still works, because refusing must not half-succeed.
    assert auth.identify_agent(db, bob.credential) is not None


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------


def test_an_override_cannot_be_written_onto_another_accounts_match(http, alice, bob):
    """A correction is a write a browser *can* make, so it needs its own test."""
    play(http, alice)
    key = as_user(http, alice).get("/api/matches?range=all").json()["matches"][0][
        "match_key"
    ]

    as_user(http, bob)
    response = http.put(f"/api/matches/{key}/overrides/1", json={"value": 1})
    assert response.status_code == 404

    # Alice's match is untouched: the refusal wrote nothing.
    overrides = as_user(http, alice).get(f"/api/matches/{key}/overrides").json()
    assert overrides["overrides"] == []


# ---------------------------------------------------------------------------
# Live deltas
# ---------------------------------------------------------------------------


def test_the_live_socket_refuses_an_unauthenticated_client(http):
    from starlette.websockets import WebSocketDisconnect

    http.cookies.clear()
    with pytest.raises(WebSocketDisconnect) as caught:
        with http.websocket_connect("/api/live") as socket:
            socket.receive_text()
    assert caught.value.code == 4401


def test_a_socket_never_replays_another_accounts_events(http, alice, bob):
    """Replay is a read of the published log, and is scoped like any other."""
    play(http, alice)
    play(http, bob)

    as_user(http, bob)
    with http.websocket_connect("/api/live?since=0") as socket:
        hello = socket.receive_json()
        assert hello["type"] == "hello"
        seen = [socket.receive_json() for _ in range(hello["replaying"])]

    keys = {event.get("match_key") for event in seen} - {None}
    mine = as_user(http, alice).get("/api/matches?range=all").json()
    assert keys.isdisjoint({m["match_key"] for m in mine["matches"]})


def test_a_live_delta_only_reaches_its_own_account(http, alice, bob):
    """The fan-out is per account, so a delta is never put on the wrong queue."""
    as_user(http, bob)
    with http.websocket_connect("/api/live") as socket:
        assert socket.receive_json()["type"] == "hello"

        play(http, alice)

        # Bob's socket should have nothing to say about Alice playing. The
        # server sends a ping every 25s and nothing else, so anything that
        # arrives here is a leak.
        bob.sim.map("Europe")
        bob.sim.play([0, 0, -200])
        http.post(
            "/api/agent/facts",
            json=bob.sim.drain().to_json(),
            headers=bob.bearer,
        )

        mine = as_user(http, alice).get("/api/matches?range=all").json()
        alice_keys = {m["match_key"] for m in mine["matches"]}

        as_user(http, bob)
        for _ in range(12):
            message = socket.receive_json()
            if message.get("type") != "event":
                continue
            assert message.get("match_key") not in alice_keys
            if message.get("kind") == "match.started":
                break
