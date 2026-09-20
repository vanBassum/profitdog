"""Where things live, and the handful of numbers worth naming once."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Poll cadence for the Rich Presence collector, in seconds. The tracker has
#: always used 2s and every stored curve has that shape; changing it changes
#: what a "flat stretch" in the data means.
POLL_INTERVAL_SEC = 2.0

#: How often to re-check that Wardogs is still running while it is. This is the
#: latency between quitting the game and Steam being told its AppID is free, so
#: it is deliberately much finer than the poll interval.
GAME_WATCH_INTERVAL_SEC = 0.25

#: How often to look for the game while it is not running.
IDLE_INTERVAL_SEC = 1.0

#: Consecutive `playing` reads before a match is considered started, and
#: consecutive non-`playing` reads before it is considered ended. Real data
#: showed `game_state` can flicker away from `playing` for a poll or two during
#: genuinely continuous play; with zero tolerance that silently fragmented
#: single matches into several.
CONFIRM_READS = 2
MISS_THRESHOLD = 5

#: The insertion spawn is not a second life, so spawns within this of the
#: match's confirmed start are the insertion itself.
LIFE_SPAWN_GRACE_SEC = 3.0

#: Events kept in the published log for reconnecting clients to replay. A
#: client further behind than this is told to refetch a snapshot instead —
#: replaying an unbounded backlog is slower than starting again.
API_EVENT_RETENTION = 20_000

#: Most events a single reconnect will replay before the server gives up and
#: asks for a resync. Well under retention, so the decision is about the cost
#: to *this* client rather than about what happens to be on disk.
MAX_REPLAY_EVENTS = 2_000

#: Hard ceiling on points returned for a chart, whatever the client asks for.
#: The curve is decimated to fit; derivation never sees a decimated curve.
MAX_CHART_POINTS = 2_000
DEFAULT_CHART_POINTS = 600


#: How long a browser session lasts before it has to sign in again.
SESSION_TTL_SEC = 14 * 24 * 3600

#: How long a linking code is worth anything. Short: it is read off one screen
#: and typed into another, and a code still live an hour later is a code
#: someone else can approve.
LINK_CODE_TTL_SEC = 600

#: What the agent is told to wait between polls while a code is unapproved.
LINK_POLL_INTERVAL_SEC = 2.0

#: Google's endpoints, named once.
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")


@dataclass(frozen=True)
class Settings:
    #: A libpq connection string. The database is PostgreSQL and is not
    #: optional: there is no embedded fallback, because a fallback that only
    #: appears when the real one is misconfigured is a way to run for weeks
    #: writing somewhere nobody is backing up.
    database_url: str
    ui_dist: Path
    host: str
    port: int
    collector: bool
    #: Where this server is reachable from a browser. Used to build the OAuth
    #: redirect and the URL the agent sends someone to, so it has to be the
    #: public address rather than the bind address.
    public_url: str = "http://127.0.0.1:5174"
    google_client_id: str = ""
    google_client_secret: str = ""
    #: Addresses allowed to create an account. Empty means nobody new: a
    #: closed door is the safe default for a server on the public internet,
    #: and opening it is one environment variable.
    allowlist: tuple[str, ...] = ()
    #: The account that inherits everything written before there were accounts.
    owner_email: str = ""
    #: `Secure` on the session cookie. On by default because this is intended
    #: to be served over HTTPS; the only reason to turn it off is a local
    #: http:// development server, where a Secure cookie is never sent back.
    cookie_secure: bool = True
    #: The agent build handed out at /download.
    agent_exe: Path = REPO_ROOT / "agent" / "dist" / "profitdog.exe"

    @property
    def auth_configured(self) -> bool:
        """Whether Google sign-in can actually be attempted."""
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_url.rstrip('/')}/auth/callback"

    def allows(self, email: str) -> bool:
        """Whether this address may create an account.

        Existing users are not checked against this: taking someone's account
        away by editing an environment variable would be a surprising way to
        lose a history, and revocation is a separate concern from admission.
        """
        address = (email or "").strip().lower()
        if not address:
            return False
        for entry in self.allowlist:
            rule = entry.strip().lower()
            if not rule:
                continue
            # A bare domain, written as `@example.com`, admits everyone in it.
            if rule.startswith("@"):
                if address.endswith(rule):
                    return True
            elif address == rule:
                return True
        return False

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            database_url=os.environ.get(
                "PROFITDOG_DATABASE_URL",
                "postgresql://profitdog:profitdog@127.0.0.1:5432/profitdog",
            ),
            ui_dist=Path(
                os.environ.get("PROFITDOG_UI", REPO_ROOT / "ui" / "dist")
            ).resolve(),
            host=os.environ.get("PROFITDOG_HOST", "127.0.0.1"),
            port=int(os.environ.get("PROFITDOG_PORT", "5174")),
            collector=os.environ.get("PROFITDOG_COLLECTOR", "1") != "0",
            public_url=os.environ.get(
                "PROFITDOG_PUBLIC_URL", "http://127.0.0.1:5174"
            ).rstrip("/"),
            google_client_id=os.environ.get("PROFITDOG_GOOGLE_CLIENT_ID", ""),
            google_client_secret=os.environ.get("PROFITDOG_GOOGLE_CLIENT_SECRET", ""),
            allowlist=tuple(
                part.strip()
                for part in os.environ.get("PROFITDOG_ALLOWLIST", "").split(",")
                if part.strip()
            ),
            owner_email=os.environ.get("PROFITDOG_OWNER_EMAIL", "").strip().lower(),
            cookie_secure=os.environ.get("PROFITDOG_COOKIE_SECURE", "1") != "0",
            agent_exe=Path(
                os.environ.get(
                    "PROFITDOG_AGENT_EXE", REPO_ROOT / "agent" / "dist" / "profitdog.exe"
                )
            ),
        )
