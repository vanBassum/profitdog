"""Who is asking, and what they may do.

## Two kinds of caller, deliberately unequal

A **person** signs in with Google and gets a session cookie. They may read
their own data and correct it. They may not upload: a browser is not an
observer of anything.

An **agent** presents a bearer credential it was given when its PC was linked.
It may upload facts, and ask where the server is in its own sequence. It may
not read a match, a history page or an analysis. This is not a scope string
that could be widened later by accident -- the agent's credential is looked up
by a different function from the session's, and nothing that answers a question
about data calls it.

Keeping them apart is what makes the agent credential safe to leave on a gaming
PC. The worst a stolen one can do is write rubbish into the account it belongs
to. It cannot read the history, and it cannot be traded for a session.

## Google tokens stop here

The agent never sees anything from Google. The authorization code, the token
exchange and the ID token all live on the server, between the browser and
Google. The agent's whole relationship with identity is a code it asked for and
a credential it was handed, neither of which means anything to Google.

## What is stored, and what is not

Session tokens and agent credentials are random, and only their SHA-256 is
written down. A leaked database yields no usable cookie and no usable
credential. The linking code's device secret is hashed for the same reason.

Comparisons use `secrets.compare_digest`, so a secret cannot be discovered a
byte at a time by timing the failures.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import (
    GOOGLE_AUTH_URL,
    GOOGLE_ISSUERS,
    GOOGLE_TOKEN_URL,
    LINK_CODE_TTL_SEC,
    SESSION_TTL_SEC,
    Settings,
)
from .db import Database, utc_now

log = logging.getLogger("profitdog_server.auth")

SESSION_COOKIE = "profitdog_session"
#: The OAuth handshake's own cookie: state, nonce and where to return to.
#: Separate from the session so that starting a login never disturbs an
#: existing one, and so an abandoned login expires by itself.
FLOW_COOKIE = "profitdog_flow"
FLOW_TTL_SEC = 600


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


def new_token(nbytes: int = 32) -> str:
    """A bearer value with no structure worth guessing at."""
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_link_code() -> str:
    """Short enough to read aloud, long enough not to be guessed.

    Eight characters from a 32-letter alphabet is 40 bits. Codes live ten
    minutes and are single use, so the only attack worth the name is online
    guessing against a live code, and 2**40 does not reward it. The alphabet
    omits I, O, 0 and 1, which are the characters people mistype.
    """
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(8))
    return raw[:4] + "-" + raw[4:]


def normalise_code(code: str) -> str:
    """Accept what a person types: any case, with or without the dash."""
    cleaned = "".join(ch for ch in (code or "").upper() if ch.isalnum())
    if len(cleaned) == 8:
        return cleaned[:4] + "-" + cleaned[4:]
    return cleaned


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _expired(value: str | None, *, now: datetime | None = None) -> bool:
    moment = _parse(value)
    if moment is None:
        return True
    return moment <= (now or datetime.now(timezone.utc))


def _in(seconds: float) -> str:
    moment = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return moment.isoformat(timespec="microseconds")


# ---------------------------------------------------------------------------
# Who
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class User:
    id: int
    email: str
    name: str | None
    picture: str | None

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "picture": self.picture,
        }


@dataclass(frozen=True)
class AgentIdentity:
    """A linked PC: which credential, which agent row, whose account."""

    credential_id: int
    agent_ref: int
    agent_id: str
    user_id: int


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def create_session(db: Database, user_id: int, *, user_agent: str | None = None) -> str:
    token = new_token()
    with db.write() as conn:
        conn.execute(
            "INSERT INTO sessions (user_id, token_hash, created_at, expires_at,"
            " last_seen_at, user_agent) VALUES (%s, %s, %s, %s, %s, %s)",
            (
                user_id,
                token_hash(token),
                utc_now(),
                _in(SESSION_TTL_SEC),
                utc_now(),
                (user_agent or "")[:200] or None,
            ),
        )
    return token


def user_for_session(db: Database, token: str | None) -> User | None:
    """The signed-in person, or None. An expired session is None, and is swept."""
    if not token:
        return None
    row = db.query_one(
        "SELECT s.id, s.expires_at, u.id AS user_id, u.email, u.name, u.picture"
        "  FROM sessions s JOIN users u ON u.id = s.user_id"
        " WHERE s.token_hash = %s",
        (token_hash(token),),
    )
    if row is None:
        return None
    if _expired(row["expires_at"]):
        end_session(db, token)
        return None
    return User(
        id=int(row["user_id"]),
        email=str(row["email"]),
        name=row["name"],
        picture=row["picture"],
    )


def end_session(db: Database, token: str | None) -> None:
    if not token:
        return
    with db.write() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = %s", (token_hash(token),))


def sweep_sessions(db: Database) -> int:
    with db.write() as conn:
        deleted = conn.execute(
            "DELETE FROM sessions WHERE expires_at <= %s", (utc_now(),)
        ).rowcount
    return int(deleted)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


def upsert_google_user(db: Database, claims: dict, settings: Settings) -> User:
    """Find or create the account behind a verified Google identity.

    Three cases, in order:

    1. The subject is known. That is the account, and the stored address is
       refreshed in case it changed.
    2. The subject is new, but the verified address matches a row that has no
       subject yet. That row is claimed. This is how the pre-accounts database
       reaches its owner, and it is the only time an address decides an
       account.
    3. Neither. A new account, if the allowlist admits the address.
    """
    sub = str(claims["sub"])
    email = str(claims.get("email") or "").strip().lower()
    name = claims.get("name")
    picture = claims.get("picture")

    row = db.query_one("SELECT * FROM users WHERE google_sub = %s", (sub,))
    if row is not None:
        with db.write() as conn:
            conn.execute(
                "UPDATE users SET email = %s, name = %s, picture = %s, last_login_at = %s"
                " WHERE id = %s",
                (email or row["email"], name, picture, utc_now(), int(row["id"])),
            )
        return User(int(row["id"]), email or str(row["email"]), name, picture)

    unclaimed = None
    if email:
        unclaimed = db.query_one(
            "SELECT * FROM users WHERE email = %s AND google_sub IS NULL", (email,)
        )
    if unclaimed is not None:
        with db.write() as conn:
            conn.execute(
                "UPDATE users SET google_sub = %s, name = %s, picture = %s,"
                " last_login_at = %s WHERE id = %s",
                (sub, name, picture, utc_now(), int(unclaimed["id"])),
            )
        log.info("account for %s claimed by its Google subject", email)
        return User(int(unclaimed["id"]), email, name, picture)

    if not settings.allows(email):
        raise PermissionError((email or "this account") + " is not on the allowlist")

    with db.write() as conn:
        cursor = conn.execute(
            "INSERT INTO users (google_sub, email, name, picture, created_at,"
            " last_login_at) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (sub, email, name, picture, utc_now(), utc_now()),
        )
        user_id = int(cursor.fetchone()[0])
    log.info("new account for %s", email)
    return User(user_id, email, name, picture)


# ---------------------------------------------------------------------------
# Google OpenID Connect
# ---------------------------------------------------------------------------


def authorization_url(settings: Settings, *, state: str, nonce: str) -> str:
    from urllib.parse import urlencode

    query = urlencode(
        {
            "client_id": settings.google_client_id,
            "redirect_uri": settings.redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
            # Ask which account every time rather than silently reusing a
            # cached consent: the allowlist decision depends on the address.
            "prompt": "select_account",
        }
    )
    return GOOGLE_AUTH_URL + "?" + query


def _decode_segment(segment: str) -> dict:
    padded = segment + "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))


def read_id_token(token: str, settings: Settings, *, nonce: str | None) -> dict:
    """The claims in an ID token that came straight from Google's token endpoint.

    The signature is deliberately not checked, and that is sound *only* because
    of where this token came from. OpenID Connect Core section 3.1.3.7 says it
    in as many words: in the authorization code flow, an ID token received
    directly from the token endpoint over a TLS connection whose certificate
    was validated may be trusted without verifying its signature. We made that
    request ourselves, to a pinned URL, with our own client secret.

    What still has to be checked is everything that is about *us* rather than
    about Google's integrity: that the token names this client, that it has not
    expired, and that it answers the nonce this particular login sent. Those
    are what stop a token minted for another application, or replayed from an
    earlier login, being accepted here.

    If this ever stops fetching the token itself -- an implicit flow, a token
    arriving from a client -- this function is wrong, and a real JWKS signature
    check has to replace it.
    """
    try:
        _, payload, _ = token.split(".")
        claims = _decode_segment(payload)
    except (ValueError, TypeError) as exc:
        raise PermissionError("malformed ID token") from exc

    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise PermissionError("ID token was not issued by Google")

    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if settings.google_client_id not in audiences:
        raise PermissionError("ID token is for a different client")

    expires = claims.get("exp")
    if not isinstance(expires, (int, float)) or expires <= time.time():
        raise PermissionError("ID token has expired")

    if nonce is not None:
        if not secrets.compare_digest(str(claims.get("nonce") or ""), nonce):
            raise PermissionError("ID token does not answer this login")

    if not claims.get("sub"):
        raise PermissionError("ID token carries no subject")
    if not claims.get("email"):
        raise PermissionError("Google returned no address")
    if not claims.get("email_verified"):
        # The owner account is claimed by address, so an unverified address
        # would let anyone who can type it inherit somebody else's history.
        raise PermissionError("this Google address is not verified")
    return claims


async def exchange_code(code: str, settings: Settings) -> dict:
    """Trade the authorization code for tokens. The only call Google gets."""
    import httpx

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.redirect_uri,
                "grant_type": "authorization_code",
            },
            headers={"accept": "application/json"},
        )
    if response.status_code != 200:
        log.warning("google token exchange failed: %s", response.text[:300])
        raise PermissionError("Google rejected the sign-in")
    body = response.json()
    if "id_token" not in body:
        raise PermissionError("Google returned no ID token")
    return body


# ---------------------------------------------------------------------------
# Agent credentials
# ---------------------------------------------------------------------------

CREDENTIAL_PREFIX = "pdog_"


def issue_credential(
    db: Database, *, user_id: int, agent_ref: int, label: str | None
) -> str:
    """Mint an upload-only credential. The plaintext exists only as this return."""
    token = CREDENTIAL_PREFIX + new_token(32)
    with db.write() as conn:
        conn.execute(
            "INSERT INTO agent_credentials (user_id, agent_ref, token_hash, label,"
            " created_at) VALUES (%s, %s, %s, %s, %s)",
            (user_id, agent_ref, token_hash(token), label, utc_now()),
        )
    return token


def identify_agent(db: Database, token: str | None) -> AgentIdentity | None:
    """The PC behind a bearer credential, or None if it is not one.

    Revocation is checked here rather than only at issue, so revoking takes
    effect on that agent's very next upload.
    """
    if not token or not token.startswith(CREDENTIAL_PREFIX):
        return None
    row = db.query_one(
        "SELECT c.id, c.agent_ref, c.user_id, c.revoked_at, a.agent_id"
        "  FROM agent_credentials c JOIN agents a ON a.id = c.agent_ref"
        " WHERE c.token_hash = %s",
        (token_hash(token),),
    )
    if row is None or row["revoked_at"]:
        return None
    return AgentIdentity(
        credential_id=int(row["id"]),
        agent_ref=int(row["agent_ref"]),
        agent_id=str(row["agent_id"]),
        user_id=int(row["user_id"]),
    )


def touch_credential(db: Database, credential_id: int) -> None:
    with db.write() as conn:
        conn.execute(
            "UPDATE agent_credentials SET last_used_at = %s WHERE id = %s",
            (utc_now(), credential_id),
        )


def revoke_credential(db: Database, *, user_id: int, credential_id: int) -> bool:
    """Revoke one of *this user's* credentials. False if it is not theirs."""
    with db.write() as conn:
        changed = conn.execute(
            "UPDATE agent_credentials SET revoked_at = %s"
            " WHERE id = %s AND user_id = %s AND revoked_at IS NULL",
            (utc_now(), credential_id, user_id),
        ).rowcount
    return bool(changed)


def credentials_for(db: Database, user_id: int) -> list[dict]:
    rows = db.query(
        "SELECT c.id, c.label, c.created_at, c.last_used_at, c.revoked_at,"
        "       a.agent_id FROM agent_credentials c"
        "  JOIN agents a ON a.id = c.agent_ref"
        " WHERE c.user_id = %s ORDER BY c.created_at DESC",
        (user_id,),
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Linking a PC
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LinkRequest:
    code: str
    device_secret: str
    expires_in: int


class LinkPending(Exception):
    """Nobody has approved this code yet. The agent should keep waiting."""


class LinkFailed(Exception):
    """The code is unknown, expired, already used, or not this device's."""


def start_link(db: Database, *, agent_id: str, label: str | None) -> LinkRequest:
    """Open a linking request. Called by an agent that belongs to nobody yet."""
    device_secret = new_token()
    with db.write() as conn:
        for _ in range(5):
            code = new_link_code()
            taken = conn.execute(
                "SELECT 1 FROM link_codes WHERE code = %s", (code,)
            ).fetchone()
            if taken:  # pragma: no cover - 40 bits makes this unreachable
                continue
            conn.execute(
                "INSERT INTO link_codes (code, device_secret_hash, agent_id,"
                " label, created_at, expires_at) VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    code,
                    token_hash(device_secret),
                    agent_id,
                    label,
                    utc_now(),
                    _in(LINK_CODE_TTL_SEC),
                ),
            )
            break
        else:  # pragma: no cover
            raise RuntimeError("could not allocate a linking code")
    return LinkRequest(
        code=code, device_secret=device_secret, expires_in=int(LINK_CODE_TTL_SEC)
    )


def pending_link(db: Database, code: str) -> dict | None:
    """A code awaiting approval, for the page that shows it to a person."""
    row = db.query_one(
        "SELECT code, agent_id, label, expires_at, approved_at, consumed_at"
        " FROM link_codes WHERE code = %s",
        (normalise_code(code),),
    )
    if row is None or row["consumed_at"] or _expired(row["expires_at"]):
        return None
    return dict(row)


def approve_link(db: Database, *, code: str, user_id: int) -> bool:
    """A signed-in person vouches for the PC holding this code."""
    with db.write() as conn:
        row = conn.execute(
            "SELECT id, expires_at, approved_at, consumed_at FROM link_codes"
            " WHERE code = %s",
            (normalise_code(code),),
        ).fetchone()
        if row is None or row["consumed_at"] or _expired(row["expires_at"]):
            return False
        if row["approved_at"]:
            return False
        conn.execute(
            "UPDATE link_codes SET user_id = %s, approved_at = %s WHERE id = %s",
            (user_id, utc_now(), int(row["id"])),
        )
    return True


def redeem_link(db: Database, *, code: str, device_secret: str) -> str:
    """Trade an approved code for the agent's credential. Single use.

    The whole exchange is one transaction, and the row is marked consumed
    inside it, so two agents racing on one code cannot both come away with a
    credential.
    """
    with db.write() as conn:
        row = conn.execute(
            "SELECT * FROM link_codes WHERE code = %s", (normalise_code(code),)
        ).fetchone()
        if row is None:
            raise LinkFailed("unknown linking code")
        if row["consumed_at"]:
            raise LinkFailed("this linking code has already been used")
        if _expired(row["expires_at"]):
            raise LinkFailed("this linking code has expired")
        if not secrets.compare_digest(
            str(row["device_secret_hash"]), token_hash(device_secret or "")
        ):
            # Someone who read the code off a screen, without the secret the
            # asking PC kept to itself.
            raise LinkFailed("this linking code belongs to another device")
        if not row["approved_at"] or row["user_id"] is None:
            raise LinkPending()

        user_id = int(row["user_id"])
        agent_id = str(row["agent_id"])
        label = row["label"]

        existing = conn.execute(
            "SELECT id, user_id FROM agents WHERE agent_id = %s", (agent_id,)
        ).fetchone()
        if existing is None:
            cursor = conn.execute(
                "INSERT INTO agents (agent_id, label, first_seen_at, last_seen_at,"
                " user_id) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (agent_id, label, utc_now(), utc_now(), user_id),
            )
            agent_ref = int(cursor.fetchone()[0])
        else:
            agent_ref = int(existing["id"])
            owner = existing["user_id"]
            if owner is not None and int(owner) != user_id:
                # This agent id already reports into someone else's account.
                # Re-pointing it would hand them a live feed of another
                # person's play, so the link is refused instead.
                raise LinkFailed("this agent is already linked to another account")
            conn.execute(
                "UPDATE agents SET user_id = %s, label = COALESCE(%s, label)"
                " WHERE id = %s",
                (user_id, label, agent_ref),
            )

        conn.execute(
            "UPDATE link_codes SET consumed_at = %s WHERE id = %s",
            (utc_now(), int(row["id"])),
        )
        token = CREDENTIAL_PREFIX + new_token(32)
        conn.execute(
            "INSERT INTO agent_credentials (user_id, agent_ref, token_hash, label,"
            " created_at) VALUES (%s, %s, %s, %s, %s)",
            (user_id, agent_ref, token_hash(token), label, utc_now()),
        )
    return token


def sweep_link_codes(db: Database) -> int:
    with db.write() as conn:
        deleted = conn.execute(
            "DELETE FROM link_codes WHERE expires_at <= %s", (utc_now(),)
        ).rowcount
    return int(deleted)
