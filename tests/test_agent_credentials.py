"""The credential on the gaming PC: how it is stored, and how it is got.

Two things are worth pinning here. The credential must not sit on disk in the
clear on Windows, because a bearer token is a copy-and-go secret and a gaming
PC is the least defended machine in the system. And the linking handshake must
end with a credential without the agent ever having seen anything from Google.
"""

from __future__ import annotations

import sys

import pytest
from fastapi.testclient import TestClient

from profitdog_agent.credentials import CredentialStore
from profitdog_agent.linking import LinkingError, ensure_credential, link
from profitdog_server import auth
from profitdog_server.api import create_app
from profitdog_server.config import Settings
from .conftest import TEST_DATABASE_URL


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def test_a_credential_round_trips(tmp_path):
    store = CredentialStore(tmp_path / "credential.json")
    assert store.load() is None

    store.save(token="pdog_abc123", server="https://profitdog.example", agent_id="a-1")
    loaded = store.load(server="https://profitdog.example")
    assert loaded.token == "pdog_abc123"
    assert loaded.agent_id == "a-1"


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_on_windows_the_token_is_not_on_disk_in_the_clear(tmp_path):
    """The whole reason DPAPI is here."""
    path = tmp_path / "credential.json"
    store = CredentialStore(path)
    store.save(token="pdog_secret", server="https://profitdog.example", agent_id="a-1")

    raw = path.read_text(encoding="utf-8")
    assert "pdog_secret" not in raw
    assert store.load(server="https://profitdog.example").protected is True


def test_a_trailing_slash_on_the_server_does_not_orphan_the_credential(tmp_path):
    store = CredentialStore(tmp_path / "credential.json")
    store.save(token="pdog_abc", server="https://profitdog.example/", agent_id="a-1")
    assert store.load(server="https://profitdog.example") is not None


def test_a_credential_for_another_server_is_not_offered(tmp_path):
    """Sending it would hand a secret to a host that was never meant to have it."""
    store = CredentialStore(tmp_path / "credential.json")
    store.save(token="pdog_abc", server="https://profitdog.example", agent_id="a-1")
    assert store.load(server="https://somewhere-else.example") is None


def test_clearing_forgets_it(tmp_path):
    store = CredentialStore(tmp_path / "credential.json")
    store.save(token="pdog_abc", server="https://x.example", agent_id="a-1")
    store.clear()
    assert store.load(server="https://x.example") is None
    store.clear()  # and clearing twice is not an error


def test_an_interrupted_write_cannot_destroy_a_good_credential(tmp_path):
    """Saved via a temporary file and renamed, so there is no half-written state."""
    path = tmp_path / "credential.json"
    store = CredentialStore(path)
    store.save(token="pdog_first", server="https://x.example", agent_id="a-1")
    store.save(token="pdog_second", server="https://x.example", agent_id="a-1")
    assert store.load(server="https://x.example").token == "pdog_second"
    assert not (tmp_path / "credential.json.new").exists()


# ---------------------------------------------------------------------------
# The handshake
# ---------------------------------------------------------------------------


@pytest.fixture()
def server(tmp_path, db):
    settings = Settings(
        database_url=TEST_DATABASE_URL,
        ui_dist=tmp_path / "no-ui",
        host="127.0.0.1",
        port=0,
        collector=False,
        public_url="http://testserver",
        cookie_secure=False,
    )
    app = create_app(db=db, settings=settings)
    with TestClient(app) as client:
        yield client


class ViaTestClient:
    """Routes the agent's HTTP calls into the test app, as `Wire` does."""

    def __init__(self, client):
        self.client = client

    def install(self, module):
        def post(base_url, path, payload, timeout=15.0):
            response = self.client.post(path, json=payload)
            try:
                return response.status_code, response.json()
            except ValueError:
                return response.status_code, {}

        module._post = post


def test_linking_ends_with_a_stored_credential(tmp_path, db, server, alice, monkeypatch):
    """The whole flow, from an agent that belongs to nobody to one that uploads."""
    import profitdog_agent.linking as linking

    ViaTestClient(server).install(linking)
    store = CredentialStore(tmp_path / "credential.json")

    approved = {"done": False}

    def approve_between_polls(_seconds):
        """Stand in for a person: sign in and approve, once."""
        if approved["done"]:
            return
        code = db.query_one(
            "SELECT code FROM link_codes WHERE approved_at IS NULL"
            " ORDER BY id DESC LIMIT 1"
        )["code"]
        assert auth.approve_link(db, code=str(code), user_id=alice.user_id)
        approved["done"] = True

    credential = link(
        "http://testserver",
        agent_id="agent-brand-new",
        label="Gaming PC",
        store=store,
        open_browser=False,
        sleep=approve_between_polls,
    )

    assert credential.token.startswith("pdog_")
    # Stored, and usable on the next run without linking again.
    assert store.load(server="http://testserver").token == credential.token

    identity = auth.identify_agent(db, credential.token)
    assert identity.user_id == alice.user_id
    assert identity.agent_id == "agent-brand-new"


def test_an_already_linked_pc_does_not_link_again(tmp_path, server):
    import profitdog_agent.linking as linking

    ViaTestClient(server).install(linking)
    store = CredentialStore(tmp_path / "credential.json")
    store.save(token="pdog_existing", server="http://testserver", agent_id="a-1")

    credential = ensure_credential(
        "http://testserver",
        agent_id="a-1",
        label="PC",
        store=store,
        open_browser=False,
    )
    assert credential.token == "pdog_existing"


def test_linking_gives_up_rather_than_waiting_for_ever(tmp_path, server, monkeypatch):
    """Nobody approves. The agent must stop, not poll the server for ever."""
    import profitdog_agent.linking as linking

    ViaTestClient(server).install(linking)
    store = CredentialStore(tmp_path / "credential.json")

    clock = {"t": 0.0}
    monkeypatch.setattr(linking.time, "monotonic", lambda: clock["t"])

    def hurry(seconds):
        clock["t"] += 120.0  # two minutes per poll: the deadline arrives

    with pytest.raises(LinkingError):
        link(
            "http://testserver",
            agent_id="agent-ignored",
            label="PC",
            store=store,
            open_browser=False,
            sleep=hurry,
        )
    assert store.load(server="http://testserver") is None

# ---------------------------------------------------------------------------
# Where the agent looks
# ---------------------------------------------------------------------------


def test_the_default_server_is_the_hosted_one_not_localhost():
    """A downloaded EXE must not aim at the machine it is running on.

    This defaulted to http://127.0.0.1:5174 for as long as profitdog was one
    person's tracker, and it survived the move to a hosted service. The result
    was an agent that asked the *local* machine for a linking code, printed a
    localhost URL, and -- on a developer's box, where something was listening --
    got a real code from a different profitdog that knew nothing about the
    account they were signed into.
    """
    from profitdog_agent.config import DEFAULT_SERVER, AgentSettings

    assert DEFAULT_SERVER.startswith("https://")
    assert "127.0.0.1" not in DEFAULT_SERVER
    assert "localhost" not in DEFAULT_SERVER
    assert AgentSettings.from_env().server == DEFAULT_SERVER


def test_the_default_is_overridable(monkeypatch):
    """Development points it back at a local server, and must be able to."""
    from profitdog_agent.config import AgentSettings

    monkeypatch.setenv("PROFITDOG_SERVER", "http://127.0.0.1:5174")
    assert AgentSettings.from_env().server == "http://127.0.0.1:5174"
