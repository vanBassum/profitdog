"""Agent-side settings.

Deliberately separate from the server's. The agent is meant to be deployable on
its own — a gaming PC that has never heard of the server package — so it may
not import from it. Anything both halves need is in `profitdog_protocol`, which
depends on neither.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: Poll cadence for Rich Presence. The tracker has always used 2s and every
#: stored curve has that shape; changing it changes what a flat stretch means.
POLL_INTERVAL_SEC = 2.0

#: How often to re-check that Wardogs is still running while it is. This is the
#: latency between quitting the game and Steam being told its AppID is free, so
#: it is deliberately much finer than the poll interval.
GAME_WATCH_INTERVAL_SEC = 0.25

#: How often to look for the game while it is not running.
IDLE_INTERVAL_SEC = 1.0

#: How often the uplink tries to drain the outbox when it is healthy.
UPLINK_INTERVAL_SEC = 2.0

#: Where an agent looks when nobody has told it otherwise.
#:
#: The hosted instance, not localhost. This EXE is downloaded from a server and
#: run by someone who has no reason to know a URL, and a default of
#: 127.0.0.1:5174 sent them to whatever happened to be listening on their own
#: machine -- which, for anyone running the stack locally, is a *different*
#: profitdog that knows nothing about their account.
#:
#: Overridden by `--server` or `PROFITDOG_SERVER`, which is what development
#: uses.
DEFAULT_SERVER = "https://profitdog.vanbassum.com"


@dataclass(frozen=True)
class AgentSettings:
    server: str
    outbox: Path
    label: str | None
    interval: float
    #: Where the upload credential lives. Beside the outbox, because both are
    #: this PC's own state and neither means anything on another machine.
    credentials: Path = Path.home() / ".profitdog-agent" / "credential.json"

    @staticmethod
    def from_env() -> "AgentSettings":
        home = Path(
            os.environ.get("PROFITDOG_AGENT_HOME", Path.home() / ".profitdog-agent")
        )
        return AgentSettings(
            server=os.environ.get("PROFITDOG_SERVER", DEFAULT_SERVER),
            outbox=Path(os.environ.get("PROFITDOG_OUTBOX", home / "outbox.sqlite3")),
            label=os.environ.get("PROFITDOG_AGENT_LABEL") or os.environ.get("COMPUTERNAME"),
            interval=float(os.environ.get("PROFITDOG_POLL", POLL_INTERVAL_SEC)),
            credentials=Path(
                os.environ.get("PROFITDOG_CREDENTIALS", home / "credential.json")
            ),
        )
