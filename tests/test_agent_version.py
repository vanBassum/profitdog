"""Which build is that PC running?

The agent is an EXE somebody downloaded once and has not thought about since,
and until now there was no way to answer that question from anywhere but the
machine itself. Three places now answer it, and these tests pin two of them
(the third, the filename, belongs to the release workflow):

- the agent says it in its first log line and to `--version`
- it sends it with every batch, so the server can say it for each PC

The rule the server side has to keep is that a missing version means "it did
not say", never "it is old". An agent predating this sends no version at all,
and overwriting a known version with a NULL would turn a useful answer into a
worse one.
"""

from __future__ import annotations

from profitdog_protocol import FactBatch


def test_the_agent_knows_its_own_version():
    from profitdog_agent.version import agent_version

    assert agent_version()


def test_an_unreleased_checkout_does_not_claim_a_version():
    """`0.0.0+dev` is the answer, not a placeholder waiting to be tidied up.

    The number exists to identify bytes. A checkout that was never built by the
    release workflow has no bytes to identify, and saying so is more useful
    than inheriting whatever the last tag happened to be.
    """
    from profitdog_agent import version

    assert version.__version__ == "0.0.0+dev"


def test_an_override_wins_for_testing_a_release_path(monkeypatch):
    from profitdog_agent.version import agent_version

    monkeypatch.setenv("PROFITDOG_VERSION", "9.9.9")
    assert agent_version() == "9.9.9"


def test_the_agent_introduces_itself_with_version_and_commit(monkeypatch):
    from profitdog_agent.version import agent_build

    monkeypatch.setenv("PROFITDOG_VERSION", "9.9.9")
    monkeypatch.setenv("PROFITDOG_COMMIT", "d2107d1a0b1c2d3e4f")
    assert agent_build() == "9.9.9 (d2107d1a0b1c)"


def test_the_version_survives_the_wire():
    batch = FactBatch(agent_id="a", boot_id="b", facts=[], version="1.2.3")
    assert FactBatch.from_json(batch.to_json()).version == "1.2.3"


def test_a_batch_without_a_version_is_still_a_batch():
    """An agent older than this field must keep working, unchanged."""
    raw = {"protocol": 1, "agent_id": "a", "boot_id": "b", "facts": []}
    assert FactBatch.from_json(raw).version is None


def test_the_server_records_what_each_pc_is_running(ingestor, agent, db):
    agent.play([100, 200])
    ingestor.ingest(agent.drain(label="GAMING-PC", version="0.3.1"))

    row = db.query(
        "SELECT label, agent_version FROM agents WHERE agent_id = %s",
        (agent.agent_id,),
    )[0]
    assert row["label"] == "GAMING-PC"
    assert row["agent_version"] == "0.3.1"


def test_an_upgrade_is_picked_up_on_the_next_batch(ingestor, agent, db):
    agent.play([100])
    ingestor.ingest(agent.drain(version="0.3.0"))
    agent.play([200])
    ingestor.ingest(agent.drain(version="0.4.0"))

    assert db.query(
        "SELECT agent_version FROM agents WHERE agent_id = %s", (agent.agent_id,)
    )[0]["agent_version"] == "0.4.0"


def test_an_agent_that_says_nothing_does_not_erase_what_it_said_before(
    ingestor, agent, db
):
    """The COALESCE that matters.

    Two agents share a PC's identity across an upgrade and a downgrade -- or a
    batch simply predates the field. Blanking the column on the silent one
    would make "we do not know" the permanent answer for a machine that had
    already told us.
    """
    agent.play([100])
    ingestor.ingest(agent.drain(version="0.3.1"))
    agent.play([200])
    ingestor.ingest(agent.drain())  # no version at all

    assert db.query(
        "SELECT agent_version FROM agents WHERE agent_id = %s", (agent.agent_id,)
    )[0]["agent_version"] == "0.3.1"


def test_the_download_page_says_what_each_pc_is_running():
    from profitdog_server import agents as agent_status
    from profitdog_server.api import pages

    # The page is handed judged rows rather than raw ones: "online" and
    # "out of date" are decided once, in `profitdog_server.agents`, so the
    # page and `/api/agents` cannot come to different conclusions about the
    # same PC. See `test_agent_status.py` for those judgements themselves.
    html = pages.download_page(
        available=True,
        releases_url="https://example.invalid/releases",
        agents=agent_status.describe_all(
            [
                {"agent_id": "a1", "label": "GAMING-PC", "agent_version": "0.3.1",
                 "last_seen_at": None},
                {"agent_id": "a2", "label": "LAPTOP", "agent_version": None,
                 "last_seen_at": None},
            ],
            current_version=None,
        ),
    )
    assert "GAMING-PC" in html
    assert "0.3.1" in html
    # The one that never said must not be shown a number it did not report.
    assert "version unknown" in html


def test_the_download_page_is_unchanged_for_someone_with_no_pcs_yet():
    from profitdog_server.api import pages

    html = pages.download_page(available=True, releases_url="", agents=[])
    assert "Your PCs" not in html
