"""Agent-side settings.

Deliberately separate from the server's. The agent is meant to be deployable on
its own — a gaming PC that has never heard of the server package — so it may
not import from it. Anything both halves need is in `profitdog.protocol`, which
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


@dataclass(frozen=True)
class AgentSettings:
    server: str
    outbox: Path
    label: str | None
    interval: float

    @staticmethod
    def from_env() -> "AgentSettings":
        home = Path(
            os.environ.get("PROFITDOG_AGENT_HOME", Path.home() / ".profitdog-agent")
        )
        return AgentSettings(
            server=os.environ.get("PROFITDOG_SERVER", "http://127.0.0.1:5174"),
            outbox=Path(os.environ.get("PROFITDOG_OUTBOX", home / "outbox.sqlite3")),
            label=os.environ.get("PROFITDOG_AGENT_LABEL") or os.environ.get("COMPUTERNAME"),
            interval=float(os.environ.get("PROFITDOG_POLL", POLL_INTERVAL_SEC)),
        )
