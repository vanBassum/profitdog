"""Giving the pre-multi-user database an owner.

Everything in this repository was written when there was one person and no
question of whose data it was. Rows from that era carry `user_id IS NULL`,
which is not "public" and must never be read as such — it is "from before the
question was asked".

So they are adopted. The address in `PROFITDOG_OWNER_EMAIL` gets a `users` row
with no `google_sub`, and every ownerless agent, match and published event is
assigned to it. The first Google sign-in whose *verified* address matches that
one claims the row and fills the subject in; from then on the account is
identified by the subject like any other.

Claiming by address is the only bootstrap available — the database predates
anyone having signed in, so there is no subject to seed it with — and it is why
`email_verified` is insisted upon at login. An unverified address would let
anyone who could type it inherit a stranger's history.

This runs on every startup rather than once inside a migration, because rows
can arrive ownerless afterwards too: the CSV importer writes history that no
agent ever uploaded. It is idempotent, and does nothing at all once there is
nothing left to adopt.
"""

from __future__ import annotations

import logging

from .db import Database, utc_now

log = logging.getLogger("profitdog_server.ownership")


def unowned_counts(db: Database) -> dict[str, int]:
    """How much of the database predates ownership."""
    return {
        table: int(
            db.scalar(f"SELECT COUNT(*) FROM {table} WHERE user_id IS NULL") or 0
        )
        for table in ("agents", "matches", "api_events")
    }


def ensure_owner(db: Database, email: str) -> int | None:
    """Adopt every ownerless row. Returns the owning user id, or None.

    None means there was nothing to adopt and no owner row already existed —
    a fresh database, where the first person to sign in simply gets their own
    account and this has no work to do.
    """
    if not email:
        return None
    address = email.strip().lower()
    if not address:
        return None

    with db.write() as conn:
        pending = {
            table: int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE user_id IS NULL"
                ).fetchone()[0]
            )
            for table in ("agents", "matches", "api_events")
        }
        existing = conn.execute(
            "SELECT id FROM users WHERE email = %s", (address,)
        ).fetchone()
        if not any(pending.values()) and existing is None:
            return None

        if existing is None:
            cursor = conn.execute(
                "INSERT INTO users (email, created_at) VALUES (%s, %s)"
                " RETURNING id",
                (address, utc_now()),
            )
            user_id = int(cursor.fetchone()[0])
            log.info("created owner account for %s", address)
        else:
            user_id = int(existing["id"])

        for table in ("agents", "matches", "api_events"):
            if pending[table]:
                conn.execute(
                    f"UPDATE {table} SET user_id = %s WHERE user_id IS NULL",
                    (user_id,),
                )
        if any(pending.values()):
            log.info(
                "adopted %d agents, %d matches, %d events for %s",
                pending["agents"], pending["matches"], pending["api_events"], address,
            )
        return user_id


__all__ = ["ensure_owner", "unowned_counts"]
