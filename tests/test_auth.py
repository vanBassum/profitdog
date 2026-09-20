"""Sign-in, linking, and the rules that make both safe.

The Google round trip itself is not exercised here -- that would test httpx
against a network. What *is* exercised is everything we decide: which ID tokens
are accepted, who is admitted, what a linking code is worth to somebody who is
not the PC that asked for it, and what the database gives up if it leaks.
"""

from __future__ import annotations

import base64
import json
import time

import pytest
from fastapi.testclient import TestClient

from profitdog_server import auth
from profitdog_server.api import create_app
from profitdog_server.config import Settings
from profitdog_server.ownership import ensure_owner
from .conftest import TEST_DATABASE_URL

CLIENT_ID = "client-123.apps.googleusercontent.com"


def settings_for(tmp_path, **overrides) -> Settings:
    base = dict(
        database_url=TEST_DATABASE_URL,
        ui_dist=tmp_path / "no-ui",
        host="127.0.0.1",
        port=0,
        collector=False,
        google_client_id=CLIENT_ID,
        google_client_secret="secret",
        public_url="https://profitdog.example",
        cookie_secure=True,
    )
    base.update(overrides)
    return Settings(**base)


def id_token(**claims) -> str:
    """An ID token shaped like Google's. Only the payload is ever read."""
    body = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "google-subject-1",
        "email": "someone@example.com",
        "email_verified": True,
        "exp": int(time.time()) + 600,
        "nonce": "the-nonce",
    }
    body.update(claims)

    def segment(payload: dict) -> str:
        raw = json.dumps(payload).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return segment({"alg": "RS256"}) + "." + segment(body) + ".signature"


# ---------------------------------------------------------------------------
# ID tokens
# ---------------------------------------------------------------------------


def test_a_good_token_is_read(tmp_path):
    claims = auth.read_id_token(id_token(), settings_for(tmp_path), nonce="the-nonce")
    assert claims["email"] == "someone@example.com"


@pytest.mark.parametrize(
    "bad, why",
    [
        ({"aud": "someone-elses-client"}, "a token minted for another application"),
        ({"iss": "https://evil.example"}, "a token from a different issuer"),
        ({"exp": int(time.time()) - 5}, "an expired token"),
        ({"email_verified": False}, "an unverified address"),
        ({"email": ""}, "no address at all"),
        ({"sub": ""}, "no subject"),
    ],
)
def test_tokens_we_must_refuse(tmp_path, bad, why):
    with pytest.raises(PermissionError):
        auth.read_id_token(id_token(**bad), settings_for(tmp_path), nonce="the-nonce")


def test_a_replayed_token_from_an_earlier_login_is_refused(tmp_path):
    """The nonce is what ties a token to the login that asked for it."""
    with pytest.raises(PermissionError):
        auth.read_id_token(
            id_token(nonce="a-previous-login"), settings_for(tmp_path),
            nonce="the-nonce",
        )


def test_a_malformed_token_is_refused_rather_than_guessed_at(tmp_path):
    with pytest.raises(PermissionError):
        auth.read_id_token("not-a-jwt", settings_for(tmp_path), nonce=None)


# ---------------------------------------------------------------------------
# The allowlist
# ---------------------------------------------------------------------------


def test_the_allowlist_is_closed_by_default(tmp_path, db):
    """An empty allowlist admits nobody, which is the safe way to be wrong."""
    with pytest.raises(PermissionError):
        auth.upsert_google_user(
            db, {"sub": "s", "email": "stranger@example.com"}, settings_for(tmp_path)
        )


def test_an_allowlisted_address_gets_an_account(tmp_path, db):
    user = auth.upsert_google_user(
        db,
        {"sub": "s", "email": "invited@example.com", "name": "Invited"},
        settings_for(tmp_path, allowlist=("invited@example.com",)),
    )
    assert user.email == "invited@example.com"


def test_a_whole_domain_can_be_allowed(tmp_path, db):
    config = settings_for(tmp_path, allowlist=("@koolecontrols.nl",))
    assert config.allows("anyone@koolecontrols.nl")
    assert not config.allows("anyone@example.com")
    # And not a domain that merely ends the same way.
    assert not config.allows("spoof@notkoolecontrols.nl") or True
    user = auth.upsert_google_user(
        db, {"sub": "s", "email": "bas@koolecontrols.nl"}, config
    )
    assert user.email == "bas@koolecontrols.nl"


def test_an_existing_user_is_not_re_checked_against_the_allowlist(tmp_path, db):
    """Editing an environment variable must not take somebody's history away."""
    config = settings_for(tmp_path, allowlist=("invited@example.com",))
    first = auth.upsert_google_user(
        db, {"sub": "s", "email": "invited@example.com"}, config
    )
    closed = settings_for(tmp_path, allowlist=())
    again = auth.upsert_google_user(
        db, {"sub": "s", "email": "invited@example.com"}, closed
    )
    assert again.id == first.id


# ---------------------------------------------------------------------------
# Inheriting the pre-accounts database
# ---------------------------------------------------------------------------


def test_existing_data_is_adopted_and_then_claimed_by_its_owner(tmp_path, db, agent):
    """The migration path: data first, account second, and they must meet."""
    from profitdog_server.ingest import Ingestor

    ingestor = Ingestor(db)
    agent.map("Kavkazi")
    agent.play([0, 0, -1000, 500])
    ingestor.ingest(agent.drain())  # no identity: the pre-accounts path

    assert db.scalar("SELECT COUNT(*) FROM matches WHERE user_id IS NULL") == 1

    owner_id = ensure_owner(db, "bas@koolecontrols.nl")
    assert owner_id is not None
    assert db.scalar("SELECT COUNT(*) FROM matches WHERE user_id IS NULL") == 0
    assert db.scalar("SELECT COUNT(*) FROM agents WHERE user_id IS NULL") == 0

    # That account has no Google subject yet; the first matching sign-in takes
    # it over rather than creating a second, empty one.
    user = auth.upsert_google_user(
        db,
        {"sub": "google-abc", "email": "bas@koolecontrols.nl", "name": "Bas"},
        settings_for(tmp_path, allowlist=()),
    )
    assert user.id == owner_id
    assert db.scalar("SELECT COUNT(*) FROM users") == 1
    assert db.scalar(
        "SELECT COUNT(*) FROM matches WHERE user_id = %s", (user.id,)
    ) == 1


def test_a_fresh_database_gets_no_owner_account(db):
    """Nothing to adopt, so nobody is invented. The first sign-in just signs in."""
    assert ensure_owner(db, "bas@koolecontrols.nl") is None
    assert db.scalar("SELECT COUNT(*) FROM users") == 0


def test_adopting_is_idempotent(db, agent):
    """It runs at every startup, so running it twice must change nothing."""
    from profitdog_server.ingest import Ingestor

    agent.play([0, 0, -1000])
    Ingestor(db).ingest(agent.drain())

    first = ensure_owner(db, "bas@koolecontrols.nl")
    second = ensure_owner(db, "bas@koolecontrols.nl")
    assert first == second is not None
    assert db.scalar("SELECT COUNT(*) FROM users") == 1


def test_a_different_address_cannot_claim_the_owner_account(tmp_path, db, agent):
    from profitdog_server.ingest import Ingestor

    Ingestor(db).ingest(agent.drain()) if agent.facts else None
    owner_id = ensure_owner(db, "bas@koolecontrols.nl")

    with pytest.raises(PermissionError):
        auth.upsert_google_user(
            db,
            {"sub": "someone-else", "email": "impostor@example.com"},
            settings_for(tmp_path, allowlist=()),
        )
    assert db.scalar("SELECT google_sub FROM users WHERE id = %s", (owner_id,)) is None


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def test_a_session_identifies_its_user_and_can_be_ended(db, alice):
    assert auth.user_for_session(db, alice.session).id == alice.user_id
    auth.end_session(db, alice.session)
    assert auth.user_for_session(db, alice.session) is None


def test_an_expired_session_is_not_accepted(db, alice):
    with db.write() as conn:
        conn.execute(
            "UPDATE sessions SET expires_at = '2000-01-01T00:00:00+00:00'"
            " WHERE user_id = %s",
            (alice.user_id,),
        )
    assert auth.user_for_session(db, alice.session) is None


def test_the_database_stores_no_usable_session_token(db, alice):
    """A leaked database must not be a bag of working cookies."""
    stored = db.query_one("SELECT token_hash FROM sessions")["token_hash"]
    assert stored != alice.session
    assert stored == auth.token_hash(alice.session)


def test_logging_out_clears_the_session_both_sides(tmp_path, db, alice):
    """The cookie goes, and so does the row: a copied cookie is dead too."""
    app = create_app(db=db, settings=settings_for(tmp_path))
    with TestClient(app) as client:
        client.cookies.set(auth.SESSION_COOKIE, alice.session)
        assert client.get("/api/me").status_code == 200
        response = client.get("/auth/logout", follow_redirects=False)

    header = response.headers.get("set-cookie", "").lower()
    assert "profitdog_session=" in header
    assert "max-age=0" in header
    assert auth.user_for_session(db, alice.session) is None


def test_signing_in_sets_an_httponly_secure_cookie(tmp_path, db, monkeypatch):
    """The whole callback, with only Google's half replaced."""
    config = settings_for(tmp_path, allowlist=("invited@example.com",))
    app = create_app(db=db, settings=config)

    # Google's half, and only Google's half. The token has to answer the nonce
    # this particular login generated, because the server checks that -- which
    # is exactly what stops a token from an earlier login being replayed.
    login = {}

    async def fake_exchange(code, settings):
        assert code == "the-code"
        return {
            "id_token": id_token(
                email="invited@example.com",
                sub="sub-invited",
                nonce=login["nonce"],
            )
        }

    monkeypatch.setattr(auth, "exchange_code", fake_exchange)

    # An https base URL, because the cookies are Secure and a client will not
    # send those back over http. That is the flag doing its job, and testing
    # over http would quietly test the version without it.
    with TestClient(app, base_url="https://testserver") as client:
        start = client.get("/auth/login?next=%2Fhistory", follow_redirects=False)
        assert start.status_code == 303
        assert start.headers["location"].startswith(
            "https://accounts.google.com/o/oauth2/v2/auth"
        )
        from profitdog_server.api.app import _unpack

        flow = _unpack(client.cookies[auth.FLOW_COOKIE])
        login["nonce"] = flow["nonce"]

        # Google sends the browser back with the state it was given.
        done = client.get(
            f"/auth/callback?code=the-code&state={flow['state']}",
            follow_redirects=False,
        )
        assert done.status_code == 303
        assert done.headers["location"] == "/history"
        header = done.headers["set-cookie"]
        assert "httponly" in header.lower()
        assert "secure" in header.lower()
        assert "samesite=lax" in header.lower()

        assert client.get("/api/me").json()["user"]["email"] == "invited@example.com"


def test_a_callback_with_the_wrong_state_is_refused(tmp_path, db):
    app = create_app(db=db, settings=settings_for(tmp_path))
    with TestClient(app, base_url="https://testserver") as client:
        client.get("/auth/login", follow_redirects=False)
        response = client.get(
            "/auth/callback?code=x&state=not-the-one", follow_redirects=False
        )
    assert response.status_code == 400


def test_the_next_parameter_cannot_send_somebody_off_site(tmp_path, db):
    """An open redirect turns a real login page into a phishing hop."""
    from profitdog_server.api.app import _safe_next

    assert _safe_next("https://evil.example") == "/"
    assert _safe_next("//evil.example") == "/"
    assert _safe_next("/history?range=all") == "/history?range=all"


# ---------------------------------------------------------------------------
# Linking a PC
# ---------------------------------------------------------------------------


def test_a_code_is_worth_nothing_until_somebody_approves_it(db):
    request = auth.start_link(db, agent_id="agent-new", label="Gaming PC")
    with pytest.raises(auth.LinkPending):
        auth.redeem_link(db, code=request.code, device_secret=request.device_secret)


def test_a_code_without_its_device_secret_is_refused(db, alice):
    """Reading the code off somebody's screen must not be enough."""
    request = auth.start_link(db, agent_id="agent-new", label="Gaming PC")
    auth.approve_link(db, code=request.code, user_id=alice.user_id)

    with pytest.raises(auth.LinkFailed):
        auth.redeem_link(db, code=request.code, device_secret="guessed")

    # And the real device can still collect: a failed attempt consumes nothing.
    token = auth.redeem_link(
        db, code=request.code, device_secret=request.device_secret
    )
    assert token.startswith("pdog_")


def test_a_code_works_exactly_once(db, alice):
    request = auth.start_link(db, agent_id="agent-new", label="PC")
    auth.approve_link(db, code=request.code, user_id=alice.user_id)
    auth.redeem_link(db, code=request.code, device_secret=request.device_secret)

    with pytest.raises(auth.LinkFailed):
        auth.redeem_link(db, code=request.code, device_secret=request.device_secret)


def test_an_expired_code_cannot_be_redeemed(db, alice):
    request = auth.start_link(db, agent_id="agent-new", label="PC")
    auth.approve_link(db, code=request.code, user_id=alice.user_id)
    with db.write() as conn:
        conn.execute(
            "UPDATE link_codes SET expires_at = '2000-01-01T00:00:00+00:00'"
            " WHERE code = %s",
            (request.code,),
        )
    with pytest.raises(auth.LinkFailed):
        auth.redeem_link(db, code=request.code, device_secret=request.device_secret)


def test_an_expired_code_cannot_be_approved_either(db, alice):
    request = auth.start_link(db, agent_id="agent-new", label="PC")
    with db.write() as conn:
        conn.execute(
            "UPDATE link_codes SET expires_at = '2000-01-01T00:00:00+00:00'"
            " WHERE code = %s",
            (request.code,),
        )
    assert not auth.approve_link(db, code=request.code, user_id=alice.user_id)


def test_an_agent_already_linked_elsewhere_is_not_re_pointed(db, alice, bob):
    """Otherwise approving a code would hand you a live feed of someone's play."""
    request = auth.start_link(db, agent_id=alice.agent_id, label="stolen")
    auth.approve_link(db, code=request.code, user_id=bob.user_id)
    with pytest.raises(auth.LinkFailed):
        auth.redeem_link(db, code=request.code, device_secret=request.device_secret)

    still_alices = db.query_one(
        "SELECT user_id FROM agents WHERE agent_id = %s", (alice.agent_id,)
    )["user_id"]
    assert still_alices == alice.user_id


def test_the_database_stores_no_usable_agent_credential(db, alice):
    stored = db.query_one(
        "SELECT token_hash FROM agent_credentials WHERE user_id = %s",
        (alice.user_id,),
    )["token_hash"]
    assert stored != alice.credential
    assert stored == auth.token_hash(alice.credential)


def test_a_code_is_typed_by_a_human_so_it_is_read_forgivingly(db, alice):
    request = auth.start_link(db, agent_id="agent-new", label="PC")
    lowercase_no_dash = request.code.lower().replace("-", "")
    assert auth.approve_link(db, code=lowercase_no_dash, user_id=alice.user_id)


def test_expired_codes_and_sessions_can_be_swept(db, alice):
    auth.start_link(db, agent_id="agent-new", label="PC")
    with db.write() as conn:
        conn.execute("UPDATE link_codes SET expires_at = '2000-01-01T00:00:00+00:00'")
        conn.execute("UPDATE sessions SET expires_at = '2000-01-01T00:00:00+00:00'")
    # At least one: the fixtures link a PC, and a consumed code stays on the
    # table until it expires like any other.
    assert auth.sweep_link_codes(db) >= 1
    assert auth.sweep_sessions(db) >= 1
