"""The agent's durable queue and its delivery guarantee.

What is being protected here is the one property the whole split depends on:
the agent never forgets a fact it has not been told the server holds. Every
test below is a way of losing something — a crash, a dropped connection, a lost
acknowledgement, a restored backup — with an assertion that nothing went with
it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest

from profitdog_agent.outbox import Outbox
from profitdog_protocol import Fact, FactBatch


@pytest.fixture()
def outbox(tmp_path):
    with Outbox(tmp_path / "outbox.sqlite3", boot_id="boot-1") as box:
        yield box


def test_sequence_is_monotonic_and_starts_at_one(outbox):
    facts = [outbox.append("presence", "rich_presence", {"i": i}) for i in range(5)]
    assert [f.agent_seq for f in facts] == [1, 2, 3, 4, 5]
    assert outbox.depth() == 5


def test_identity_survives_a_restart_but_the_boot_does_not(tmp_path):
    path = tmp_path / "outbox.sqlite3"
    with Outbox(path, boot_id="boot-1") as first:
        agent_id = first.agent_id
        first.append("presence", "rich_presence", {"n": 1})

    with Outbox(path, boot_id="boot-2") as second:
        # The machine is the same machine.
        assert second.agent_id == agent_id
        # The run is a different run, and the sequence carries on regardless —
        # it is the agent's history, not this process's.
        assert second.boot_id == "boot-2"
        fact = second.append("presence", "rich_presence", {"n": 2})
        assert fact.agent_seq == 2
        assert fact.boot_id == "boot-2"


def test_ack_drops_what_was_acknowledged_and_nothing_above_it(outbox):
    for i in range(10):
        outbox.append("presence", "rich_presence", {"i": i})

    assert outbox.ack(4) == 4
    remaining = [f.agent_seq for f in outbox.pending()]
    assert remaining == [5, 6, 7, 8, 9, 10]
    assert outbox.acked_through == 4

    # An ack for something already forgotten is a no-op, not an error: a
    # retried request can legitimately be acknowledged twice.
    assert outbox.ack(4) == 0
    assert [f.agent_seq for f in outbox.pending()] == [5, 6, 7, 8, 9, 10]


def test_an_ack_that_goes_backwards_never_resurrects_or_forgets(outbox):
    for i in range(6):
        outbox.append("presence", "rich_presence", {"i": i})
    outbox.ack(5)
    # A server restored from a backup reports a lower cursor. The agent must
    # not treat that as permission to forget less than it already has, and must
    # not pretend the deleted facts are back — they are gone because they were
    # acknowledged, which is exactly the contract.
    assert outbox.ack(2) == 0
    assert outbox.acked_through == 5
    assert [f.agent_seq for f in outbox.pending()] == [6]


def test_watermarks_commit_with_the_fact_they_suppress(outbox):
    outbox.append(
        "map",
        "breadcrumbs",
        {"map": "Kavkazi"},
        watermarks={"watermark:map": "Kavkazi"},
    )
    assert outbox.get_meta("watermark:map") == "Kavkazi"
    assert outbox.depth() == 1


def test_facts_round_trip_through_the_wire_format(outbox):
    outbox.append(
        "xp",
        "save_file",
        {"roles": {"Wardog": 82}},
        source_ts="2026-09-19T23:00:00+00:00",
        build="41234",
    )
    [fact] = outbox.pending()
    batch = FactBatch(agent_id=outbox.agent_id, boot_id=outbox.boot_id, facts=[fact])
    restored = FactBatch.from_json(json.loads(json.dumps(batch.to_json())))

    assert restored.facts[0] == fact
    assert restored.facts[0].build == "41234"
    assert restored.facts[0].source_ts == "2026-09-19T23:00:00+00:00"


def test_a_batch_out_of_order_is_rejected(outbox):
    a = outbox.append("presence", "rich_presence", {"i": 0})
    b = outbox.append("presence", "rich_presence", {"i": 1})
    payload = FactBatch(
        agent_id=outbox.agent_id, boot_id=outbox.boot_id, facts=[b, a]
    ).to_json()
    with pytest.raises(ValueError, match="ascending"):
        FactBatch.from_json(payload)


def test_a_fact_missing_identity_is_rejected():
    with pytest.raises(ValueError, match="agent_id"):
        Fact.from_json({"boot_id": "b", "agent_seq": 1, "kind": "presence",
                        "source": "s", "observed_at": "t", "body": {}})
    with pytest.raises(ValueError, match="positive integer"):
        Fact.from_json({"agent_id": "a", "boot_id": "b", "agent_seq": 0,
                        "kind": "presence", "source": "s", "observed_at": "t", "body": {}})
    with pytest.raises(ValueError, match="unknown fact kind"):
        Fact.from_json({"agent_id": "a", "boot_id": "b", "agent_seq": 1,
                        "kind": "guesses", "source": "s", "observed_at": "t", "body": {}})


def test_a_sequence_number_is_never_reused_after_a_crash(tmp_path):
    """The property the idempotency gate is keyed on.

    A child process appends facts and is killed without a chance to clean up.
    Whatever it managed to commit must still be there, and the next sequence
    number must be above every one it used — because the server dedupes on that
    number, and a reused one would silently overwrite a different fact.
    """
    path = tmp_path / "outbox.sqlite3"
    script = textwrap.dedent(
        f"""
        import os, sys
        for _root in {[str(__import__("pathlib").Path(__file__).resolve().parents[1] / p) for p in ("agent", "protocol")]!r}:
            sys.path.insert(0, _root)
        from profitdog_agent.outbox import Outbox
        box = Outbox({str(path)!r}, boot_id="crashy")
        for i in range(200):
            fact = box.append("presence", "rich_presence", {{"i": i}})
            if fact.agent_seq == 120:
                sys.stdout.write("120\\n")
                sys.stdout.flush()
                os._exit(9)   # no finally, no close, no flush
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 9, result.stderr

    with Outbox(path, boot_id="after-crash") as box:
        pending = box.pending(limit=1000)
        assert len(pending) >= 120, "committed facts were lost"
        assert [f.agent_seq for f in pending] == list(range(1, len(pending) + 1))
        # The critical assertion: the next number is above everything used.
        assert box.next_seq > pending[-1].agent_seq
        fresh = box.append("presence", "rich_presence", {"after": True})
        assert fresh.agent_seq > pending[-1].agent_seq


def test_the_outbox_survives_the_two_threads_that_actually_use_it(tmp_path):
    """The collector appends while the uplink drains, for hours.

    This is not hypothetical: `profitdog/agent/__main__.py` runs exactly these
    two threads against one Outbox. Before the lock went in, the very first
    `pending()` from the uplink thread raised `SQLite objects created in a
    thread can only be used in that same thread` — a crash that no single
    threaded test could have produced, and that a real agent would have hit on
    its first delivery.
    """
    import threading

    errors: list[BaseException] = []
    drained: list[int] = []

    with Outbox(tmp_path / "outbox.sqlite3", boot_id="threads") as box:
        stop = threading.Event()

        def collect() -> None:
            try:
                for i in range(400):
                    box.append("presence", "rich_presence", {"i": i})
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)
            finally:
                stop.set()

        def deliver() -> None:
            try:
                while not stop.is_set() or box.depth():
                    facts = box.pending(50)
                    if facts:
                        box.ack(facts[-1].agent_seq)
                        drained.append(len(facts))
                    else:
                        stop.wait(0.001)
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        threads = [
            threading.Thread(target=collect, name="collector"),
            threading.Thread(target=deliver, name="uplink"),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert errors == [], errors
        assert sum(drained) == 400, "facts were lost between the two threads"
        assert box.depth() == 0
        assert box.next_seq == 401, "the sequence skipped or repeated a number"
