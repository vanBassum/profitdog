"""`match.derived.updated`: the event that keeps derived numbers from going stale.

A sample is folded into the kit price, the totals and the profit, but nothing in
the fact stream says so. A client that refetched only on structural news kept
showing a stale kit for as long as the player's balance sat still. These tests
pin the event that closes that gap — and, just as importantly, pin the cases
where it must *not* fire, because an event on every poll is only polling wearing
a different hat.
"""

from __future__ import annotations

import pytest


def published(ack_events):
    """(kind, match_key, payload) as the socket would see them."""
    return [(kind, key, payload) for _seq, kind, key, payload, _user in ack_events]


@pytest.fixture()
def captured(ingestor):
    """Everything the ingestor publishes, in order."""
    seen: list[tuple] = []
    ingestor.on_published = seen.extend
    return seen


def kinds(seen):
    return [row[1] for row in seen]


def derived(seen):
    return [row for row in seen if row[1] == "match.derived.updated"]


def test_a_moving_balance_publishes_a_derived_update(ingestor, agent, captured):
    """Cash falling is a kit being bought — the client has to be told.

    The purchase arrives in its own batch, after the match is already open.
    That is the case the bug was about: once `match.started` has been and gone,
    nothing structural follows, so without this event the kit price would sit
    on screen unchanged for as long as the balance did.
    """
    agent.play([0, 0])
    ingestor.ingest(agent.drain())
    captured.clear()

    agent.play([-790, -790])
    ingestor.ingest(agent.drain())

    assert derived(captured), "a kit purchase published no derived update"
    _seq, _kind, key, payload, _user = derived(captured)[-1]
    assert payload["match_key"] == key
    assert "kit_cost" in payload["changed"]
    assert payload["changed"]["kit_cost"] > 0


def test_a_flat_balance_publishes_nothing_further(ingestor, agent, captured):
    """The whole point: no change, no event. Otherwise this is polling."""
    agent.play([0, 0, -790, -790])
    ingestor.ingest(agent.drain())
    before = len(derived(captured))

    # Twenty more polls at exactly the same balance.
    agent.play([-790] * 20)
    ingestor.ingest(agent.drain())

    assert len(derived(captured)) == before, (
        "a balance that never moved still published derived updates"
    )


def test_duration_alone_never_publishes(ingestor, agent, captured):
    """`duration_sec` grows with every sample and is deliberately not watched."""
    agent.play([-500] * 3)
    ingestor.ingest(agent.drain())
    before = len(derived(captured))

    agent.play([-500] * 15)
    ingestor.ingest(agent.drain())

    assert len(derived(captured)) == before


def test_earning_publishes_the_totals_that_moved(ingestor, agent, captured):
    """Not just the kit: earned and profit go stale the same way."""
    agent.play([0, -1000, -1000])
    ingestor.ingest(agent.drain())
    captured.clear()

    agent.play([-1000, 2500, 2500])
    ingestor.ingest(agent.drain())

    updates = derived(captured)
    assert updates, "money coming in published no derived update"
    moved = updates[-1][3]["changed"]
    assert "earned" in moved and "profit" in moved


def test_no_duplicate_nudge_when_something_structural_already_fired(
    ingestor, agent, captured
):
    """`match.started` already makes the client refetch; one refetch is enough."""
    agent.play([0, 0, -400])
    ingestor.ingest(agent.drain())

    for _seq, kind, key, _payload, _user in captured:
        if kind == "match.started":
            started = key
            break
    else:
        pytest.fail("no match.started was published")

    same_batch = [
        row for row in derived(captured) if row[2] == started
    ]
    assert not same_batch, (
        "published a derived update for a match that was already being refetched"
    )


def test_the_payload_carries_only_publishable_fields(ingestor, agent, captured):
    """Bookkeeping must not leak into what goes over the wire."""
    agent.play([0, -300, -300, 900])
    ingestor.ingest(agent.drain())

    for _seq, _kind, _key, payload, _user in derived(captured):
        assert set(payload) == {"match_key", "changed"}
        assert all(not f.startswith("_") for f in payload["changed"])
        assert "duration_sec" not in payload["changed"]
