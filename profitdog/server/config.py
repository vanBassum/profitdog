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


@dataclass(frozen=True)
class Settings:
    database: Path
    ui_dist: Path
    host: str
    port: int
    collector: bool

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            database=Path(
                os.environ.get("PROFITDOG_DB", REPO_ROOT / "profitdog.sqlite3")
            ).resolve(),
            ui_dist=Path(
                os.environ.get("PROFITDOG_UI", REPO_ROOT / "profitdog-ui" / "dist")
            ).resolve(),
            host=os.environ.get("PROFITDOG_HOST", "127.0.0.1"),
            port=int(os.environ.get("PROFITDOG_PORT", "5174")),
            collector=os.environ.get("PROFITDOG_COLLECTOR", "1") != "0",
        )
