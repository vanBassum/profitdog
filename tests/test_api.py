"""The HTTP and WebSocket surface.

The WebSocket tests are the ones that matter most. A live client that silently
misses a delta shows a stale chart and has no way to notice, so the contract —
replay a small gap, or say `resync` and mean it — is exercised for every case:
in sync, slightly behind, far behind, ahead of the server, and reconnecting
around a restart.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from profitdog_server import auth
from profitdog_server.api import create_app
from profitdog_server.config import Settings
from profitdog_server.downsample import downsample
from .conftest import TEST_DATABASE_URL


@pytest.fixture()
def client(tmp_path, db, accounts):
    """A signed-in browser, with one linked PC behind it.

    Every route below answers for an account now, so the client carries a
    session cookie by default and `agent` is that account's own simulator.
    Tests about *not* being signed in clear the cookie deliberately.
    """
    settings = Settings(
        database_url=TEST_DATABASE_URL,
        ui_dist=tmp_path / "no-ui",
        host="127.0.0.1",
        port=0,
        collector=False,
    )
    app = create_app(db=db, settings=settings)
    account = accounts("owner@example.com", agent_id="agent-test")
    with TestClient(app) as test_client:
        test_client.app_state = app.state
        test_client.cookies.set(auth.SESSION_COOKIE, account.session)
        test_client.account = account
        yield test_client


@pytest.fixture()
def agent(client):
    """The simulator for the client's own linked PC.

    Overrides the plain `agent` fixture from conftest: facts have to arrive
    under the agent id the client's credential was issued for, or the server
    refuses them -- which is the point of the credential.
    """
    return client.account.sim


def post_facts(client, agent) -> dict:
    response = client.post(
        "/api/agent/facts",
        json=agent.drain().to_json(),
        headers=client.account.bearer,
    )
    assert response.status_code == 200, response.text
    return response.json()


def a_played_match(agent, cash=(0, 0, -5260, -4000, -1000, 2000, 3954)):
    agent.map("Kavkazi")
    agent.play(list(cash))
    agent.status("stopped")


# ---------------------------------------------------------------------------
# Agent endpoints
# ---------------------------------------------------------------------------


def test_facts_are_accepted_and_acknowledged(client, agent):
    a_played_match(agent)
    ack = post_facts(client, agent)
    assert ack["accepted"] > 0
    assert ack["duplicates"] == 0
    assert ack["acked_through"] > 0

    # No agent_id parameter any more: the credential says which agent this is,
    # so there is nothing to ask for and nothing to get wrong.
    cursor = client.get("/api/agent/cursor", headers=client.account.bearer).json()
    assert cursor["agent_id"] == agent.agent_id
    assert cursor["acked_through"] == ack["acked_through"]


def test_a_malformed_batch_is_refused_rather_than_half_stored(client):
    response = client.post(
        "/api/agent/facts",
        json={"protocol": 99, "agent_id": "x", "boot_id": "y", "facts": []},
        headers=client.account.bearer,
    )
    assert response.status_code == 400
    assert "protocol" in response.json()["detail"]


def test_a_replayed_batch_changes_nothing(client, agent, db):
    a_played_match(agent)
    batch = agent.batch().to_json()
    client.post("/api/agent/facts", json=batch, headers=client.account.bearer)
    before = db.scalar("SELECT COUNT(*) FROM cash_samples")
    second = client.post(
        "/api/agent/facts", json=batch, headers=client.account.bearer
    ).json()
    assert second["duplicates"] == len(batch["facts"])
    assert db.scalar("SELECT COUNT(*) FROM cash_samples") == before


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


def test_matches_and_totals(client, agent):
    a_played_match(agent)
    post_facts(client, agent)

    payload = client.get("/api/matches", params={"range": "all"}).json()
    assert len(payload["matches"]) == 1
    match = payload["matches"][0]
    assert match["map"] == "Kavkazi"
    assert match["map_display"] == "Bakurani"
    assert match["ruleset"] == "v1"
    assert match["live"] is False
    assert payload["totals"]["matches"] == 1
    assert payload["filters"]["maps"] == [{"value": "Kavkazi", "label": "Bakurani"}]


def test_match_detail_carries_the_ledger_not_just_the_summary(client, agent):
    a_played_match(agent)
    post_facts(client, agent)
    key = client.get("/api/matches", params={"range": "all"}).json()["matches"][0][
        "match_key"
    ]

    detail = client.get(f"/api/matches/{key}").json()
    assert detail["lives_detail"], "no lives"
    assert "kit_marks" in detail and detail["kit_marks"]
    assert detail["curve"]["net"] == detail["profit"]
    life = detail["lives_detail"][0]
    assert life["profit"] == life["earned"] - life["kit_cost"] - life["other_outflow"]


def test_an_unknown_match_is_a_404(client):
    assert client.get("/api/matches/nope").status_code == 404


def test_the_curve_is_bounded_but_keeps_its_extremes(client, agent):
    # A long match, with a deep single-reading trough in the middle of it.
    cash = [0, 0] + [i * 10 for i in range(400)]
    cash[200] = -9999
    agent.play(cash)
    agent.status("stopped")
    post_facts(client, agent)
    key = client.get("/api/matches", params={"range": "all"}).json()["matches"][0][
        "match_key"
    ]

    full = client.get(f"/api/matches/{key}/curve", params={"max_points": 2000}).json()
    small = client.get(f"/api/matches/{key}/curve", params={"max_points": 50}).json()

    assert small["returned"] <= 50
    assert small["downsampled"] is True
    assert full["downsampled"] is False
    # The trough is the whole point: a kit purchase is one sharp fall, and a
    # decimation that loses it shows a life that never bought anything.
    assert min(p["c"] for p in small["points"]) == -9999
    assert small["points"][0] == full["points"][0]
    assert small["points"][-1] == full["points"][-1]


def test_downsampling_never_reaches_the_derivation(client, agent, db):
    cash = [0, 0] + [i * 10 for i in range(400)]
    cash[200] = -9999
    agent.play(cash)
    agent.status("stopped")
    post_facts(client, agent)
    key = client.get("/api/matches", params={"range": "all"}).json()["matches"][0][
        "match_key"
    ]

    # The ledger is built from every stored sample, whatever a chart asked for.
    detail = client.get(f"/api/matches/{key}").json()
    stored = db.scalar("SELECT COUNT(*) FROM cash_samples")
    assert client.get(f"/api/matches/{key}/curve").json()["stored"] == stored
    assert detail["curve"]["net"] == detail["profit"]


def test_analysis_answers_for_the_filtered_lives(client, agent):
    a_played_match(agent)
    post_facts(client, agent)

    payload = client.get(
        "/api/analysis", params={"x": "kitCost", "y": "earned", "range": "all"}
    ).json()
    assert payload["x"]["label"] == "Kit cost"
    assert payload["comparable"] is True
    assert payload["correlation"]["n"] == len(payload["points"])
    assert "summary" in payload and payload["summary"]["reading"]

    # A map filter that matches nothing empties it, and says so rather than
    # returning an empty chart with a confident number beside it.
    empty = client.get(
        "/api/analysis", params={"x": "kitCost", "y": "earned", "map": "Nowhere",
                                 "range": "all"}
    ).json()
    assert empty["points"] == []
    assert empty["correlation"]["r"] is None


def test_an_unknown_metric_is_refused(client):
    assert client.get("/api/analysis", params={"x": "roiPct", "y": "earned"}).status_code == 400


def test_the_metric_catalogue_is_served_so_the_ui_need_not_copy_it(client):
    payload = client.get("/api/metrics").json()
    assert len(payload["metrics"]) == 9
    assert len(payload["presets"]) == 5
    assert {m["key"] for m in payload["metrics"]} >= {"kitCost", "breakEven", "spent"}
    for metric in payload["metrics"]:
        assert metric["unit"] in ("money", "duration", "rate")


def test_history_buckets_and_totals(client, agent):
    a_played_match(agent)
    post_facts(client, agent)
    payload = client.get("/api/history", params={"range": "all", "group": "match"}).json()
    assert len(payload["buckets"]) == 1
    assert payload["buckets"][0]["cumulative_profit"] == payload["totals"]["profit"]


def test_overrides_are_recorded_and_applied_without_moving_profit(client, agent):
    a_played_match(agent)
    post_facts(client, agent)
    key = client.get("/api/matches", params={"range": "all"}).json()["matches"][0][
        "match_key"
    ]
    before = client.get(f"/api/matches/{key}").json()["lives_detail"][0]

    client.put(f"/api/matches/{key}/overrides/1", json={"value": 1})
    after = client.get(f"/api/matches/{key}").json()["lives_detail"][0]

    assert after["kit_cost"] == 1
    assert after["cost_confidence"] == "manual"
    assert after["profit"] == before["profit"], "a correction moved the profit"
    assert after["spent"] == before["spent"]

    # Clearing it returns to the derived value: the correction was never
    # merged into the facts.
    client.put(f"/api/matches/{key}/overrides/1", json={"value": None})
    cleared = client.get(f"/api/matches/{key}").json()["lives_detail"][0]
    assert cleared["kit_cost"] == before["kit_cost"]
    assert cleared["cost_confidence"] == before["cost_confidence"]


def test_xp_totals_and_per_match_gains(client, agent):
    agent.xp(Wardog=80, Recon=4)
    agent.play([0, 0, -5260, 1000])
    agent.xp(Wardog=83, Recon=5)
    agent.status("stopped")
    post_facts(client, agent)

    payload = client.get("/api/xp").json()
    assert payload["totals"]["Wardog"] == 83
    gains = list(payload["by_match"].values())
    assert gains and gains[0]["Wardog"] == 3


# ---------------------------------------------------------------------------
# Live deltas
# ---------------------------------------------------------------------------


def read_frames(socket, count):
    return [json.loads(socket.receive_text()) for _ in range(count)]


def test_a_fresh_client_gets_hello_then_live_deltas(client, agent):
    with client.websocket_connect("/api/live") as socket:
        hello = json.loads(socket.receive_text())
        assert hello["type"] == "hello"
        assert hello["resync"] is False

        a_played_match(agent)
        post_facts(client, agent)

        kinds = []
        for _ in range(6):
            frame = json.loads(socket.receive_text())
            if frame["type"] == "event":
                kinds.append(frame["kind"])
        assert "match.started" in kinds
        assert "match.sample" in kinds


def test_reconnecting_replays_exactly_the_gap(client, agent, db):
    a_played_match(agent)
    post_facts(client, agent)
    midpoint = db.latest_seq()

    # More happens while the client is away.
    agent.play([0, 0, -3000, 500])
    agent.status("stopped")
    post_facts(client, agent)
    final = db.latest_seq()

    with client.websocket_connect(f"/api/live?since={midpoint}") as socket:
        hello = json.loads(socket.receive_text())
        assert hello["resync"] is False
        assert hello["replaying"] == final - midpoint
        assert hello["seq"] == final

        seqs = [json.loads(socket.receive_text())["seq"] for _ in range(hello["replaying"])]
        # Exactly the gap, in order, with nothing repeated and nothing skipped.
        assert seqs == list(range(midpoint + 1, final + 1))


def test_a_client_that_is_already_current_replays_nothing(client, agent, db):
    a_played_match(agent)
    post_facts(client, agent)
    with client.websocket_connect(f"/api/live?since={db.latest_seq()}") as socket:
        hello = json.loads(socket.receive_text())
        assert hello["replaying"] == 0
        assert hello["resync"] is False


def test_a_client_too_far_behind_is_told_to_start_again(client, agent, db, monkeypatch):
    import profitdog_server.api.app as api

    monkeypatch.setattr(api, "MAX_REPLAY_EVENTS", 3)
    a_played_match(agent)
    post_facts(client, agent)
    assert db.latest_seq() > 3

    with client.websocket_connect("/api/live?since=0") as socket:
        hello = json.loads(socket.receive_text())
        assert hello["resync"] is True, "replayed an unbounded backlog"
        assert hello["replaying"] == 0
        # The client is still told where the server is, so its refetch lands
        # on a known cursor rather than a guess.
        assert hello["seq"] == db.latest_seq()


def test_a_client_ahead_of_the_server_is_told_to_start_again(client, agent, db):
    """A restored backup, or a different server behind the same address."""
    a_played_match(agent)
    post_facts(client, agent)
    with client.websocket_connect(f"/api/live?since={db.latest_seq() + 500}") as socket:
        hello = json.loads(socket.receive_text())
        assert hello["resync"] is True


def test_a_client_older_than_the_retained_log_is_told_to_start_again(client, agent, db):
    a_played_match(agent)
    post_facts(client, agent)
    db.trim_events(keep=2)

    with client.websocket_connect("/api/live?since=1") as socket:
        hello = json.loads(socket.receive_text())
        assert hello["resync"] is True


def test_published_events_never_run_ahead_of_the_facts(client, agent, db):
    """Every sample event has its row; the log cannot lead the database."""
    a_played_match(agent)
    post_facts(client, agent)

    stored = {
        (round(float(r["elapsed_sec"]), 1), int(r["cash"]))
        for r in db.query("SELECT elapsed_sec, cash FROM cash_samples")
    }
    for row in db.query("SELECT payload FROM api_events WHERE kind = 'match.sample'"):
        payload = json.loads(row["payload"])
        assert (round(payload["elapsed_sec"], 1), payload["cash"]) in stored


# ---------------------------------------------------------------------------
# Downsampling, directly
# ---------------------------------------------------------------------------


def test_downsample_keeps_short_curves_whole():
    rows = [(i * 2.0, i, 1) for i in range(10)]
    assert len(downsample(rows, 100)) == 10


def test_downsample_is_bounded_and_keeps_both_ends():
    rows = [(i * 2.0, (i * 37) % 500 - 250, 1) for i in range(5000)]
    out = downsample(rows, 200)
    assert len(out) <= 200
    assert out[0]["t"] == 0.0
    assert out[-1]["t"] == rows[-1][0]
    assert min(p["c"] for p in out) == min(r[1] for r in rows)
    assert max(p["c"] for p in out) == max(r[1] for r in rows)


def test_downsample_output_is_still_in_time_order():
    rows = [(i * 2.0, (i * 91) % 700, 1) for i in range(3000)]
    out = downsample(rows, 120)
    assert [p["t"] for p in out] == sorted(p["t"] for p in out)
