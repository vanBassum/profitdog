"""Reading the local Wardogs sources, and reporting what was read.

## What this does and does not decide

It polls three local sources and appends what they said to the outbox. It does
not decide when a match started, which life a reading belongs to, what a kit
cost, or whether a role gained XP. Those are conclusions; conclusions are
versioned; versioned things live on the server. The rule applied throughout: if
re-running it over the same facts could produce a different answer, it is not
allowed in here.

Two consequences worth naming, because both look like omissions:

- **XP is reported as totals, never gains.** The save file holds
  `Wardog: 82`. That is the fact. "You gained 3" is a comparison against an
  earlier fact, and the server can make it from the two readings whenever it
  likes — including differently, later, if the comparison turns out to have
  been wrong.
- **Spawns are reported, lives are not counted.** The breadcrumb log says a
  player spawned at a time. Whether that is a new life, or the insertion into
  the match itself, depends on when the match started — which is a conclusion
  this process is not entitled to.

## Change-only reporting, and why it is still complete

Map and XP are read by polling files that mostly say the same thing. Sending
them unchanged every two seconds would multiply the fact stream for no
information, so they are emitted when the value moves. That is deduplication of
a transport, not derivation: the value sent is the value read, verbatim.

The watermark that makes it work is written in the same transaction as the fact
(see `outbox.append`), so the two cannot disagree — and it survives restarts,
or every restart would re-report the whole breadcrumb log as new spawns.

## The Steam AppID discipline, and where it now lives

Reading Wardogs' Rich Presence means registering with Steam *as* Wardogs, and
Steam counts that registration as a running instance of the game. An agent that
stayed initialised all day would leave Steam convinced the game was still
running long after you quit it — the whole reason this loop watches for the
process on a much finer cadence than it polls. The session is claimed when the
game appears and dropped the moment it goes.

Dropping it means ending a process, not calling a function. `SteamSession` runs
the Steamworks half in a child, because `SteamAPI_Shutdown` plus unloading the
DLL demonstrably was not enough — Steam kept showing the game as running until
the whole agent was closed. `adapters/steamsession.py` has the full account.
From up here nothing changed: claim on `init()`, read every poll, `shutdown()`
the moment the game goes.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from profitdog_protocol import utc_now
from .adapters import mapinfo, richpresence, rolexp
from .adapters.steamsession import SessionLost, SteamSession
from .config import (
    GAME_WATCH_INTERVAL_SEC,
    IDLE_INTERVAL_SEC,
    POLL_INTERVAL_SEC,
)
from .outbox import Outbox

log = logging.getLogger("profitdog_agent.collector")

WATERMARK_SPAWN = "watermark:spawn_at"
WATERMARK_MAP = "watermark:map"
WATERMARK_XP = "watermark:xp"

#: Even when nothing changed, re-report map and XP this often. Change-only
#: reporting means a lost fact looks exactly like "no change", and a heartbeat
#: is what bounds how long that can stay true.
HEARTBEAT_SEC = 300.0


def _wait(stop: threading.Event, seconds: float) -> bool:
    """Sleep. True if asked to stop while waiting."""
    return stop.wait(seconds)


def _wait_while_running(
    stop: threading.Event, seconds: float, tick: float = GAME_WATCH_INTERVAL_SEC
) -> bool:
    """Sleep between polls, but give up the moment Wardogs exits.

    Returns True if the caller should stop entirely, False to carry on — which
    includes the game-exited case, because the loop's own next pass is what
    releases the AppID. Sleeping the whole poll interval in one go would mean
    up to `interval` seconds of Steam still believing the game was running
    after it quit.
    """
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if _wait(stop, min(tick, remaining)):
            return True
        if not richpresence.game_running():
            return False


class Collector:
    """Polls the local sources and appends facts to an outbox."""

    def __init__(self, outbox: Outbox, *, interval: float = POLL_INTERVAL_SEC) -> None:
        self.outbox = outbox
        self.interval = interval
        self._rp: SteamSession | None = None
        self._last_heartbeat = 0.0

    # -- the sources -----------------------------------------------------

    def _emit_presence(self, data: dict) -> None:
        """One Rich Presence poll, verbatim.

        Every key the game published is stored, not just the four this project
        currently reads. They cost nothing to keep and a future question about
        `steam_display` or `in_profit_or_loss` can then be asked of history
        rather than only of matches played after someone thought to record it.
        """
        self.outbox.append(
            "presence",
            "rich_presence",
            dict(data),
            source_ts=utc_now(),
            build=self._build(),
        )

    def _emit_spawns(self) -> int:
        """Any breadcrumb spawn newer than the watermark.

        The breadcrumb log timestamps its own events, so these carry a real
        `source_ts` from the game rather than the agent's polling clock — the
        one source here that does.
        """
        watermark = self.outbox.get_meta(WATERMARK_SPAWN)
        seen = datetime.fromisoformat(watermark) if watermark else None
        emitted = 0
        for at in sorted(mapinfo.spawn_events()):
            if seen is not None and at <= seen:
                continue
            self.outbox.append(
                "spawn",
                "breadcrumbs",
                {"at": at.isoformat()},
                source_ts=at.isoformat(),
                build=self._build(),
                watermarks={WATERMARK_SPAWN: at.isoformat()},
            )
            emitted += 1
        return emitted

    def _emit_map(self, *, force: bool = False) -> None:
        current = mapinfo.current_map()
        if not current:
            return
        if not force and self.outbox.get_meta(WATERMARK_MAP) == current:
            return
        self.outbox.append(
            "map",
            "breadcrumbs",
            {"map": current},
            build=self._build(),
            watermarks={WATERMARK_MAP: current},
        )

    def _emit_xp(self, *, force: bool = False) -> None:
        """Absolute role totals, as the save file holds them.

        A bad or empty read is skipped rather than reported as zeros: Wardogs
        rewrites this file in place, so a read landing mid-write returns
        nothing, and reporting that as "every role dropped to zero" would put a
        fiction into the permanent record.
        """
        totals = rolexp.read_role_xp()
        if not totals:
            return
        fingerprint = ",".join(f"{k}={v}" for k, v in sorted(totals.items()))
        if not force and self.outbox.get_meta(WATERMARK_XP) == fingerprint:
            return
        self.outbox.append(
            "xp",
            "save_file",
            {"roles": totals},
            build=self._build(),
            watermarks={WATERMARK_XP: fingerprint},
        )

    def _build(self) -> str | None:
        """The game build, when anything local can say what it is.

        Nothing can, today: Rich Presence does not publish it, the breadcrumbs
        do not carry it, and the save file holds only XP. It is threaded
        through every fact anyway, because the moment a source does expose it
        the server's ruleset selection starts working on new facts without
        either half needing a schema change — and because a null here is a
        recorded "we did not know", which is a different and more useful thing
        from a column that was never there.
        """
        return None

    def _status(self, status: str, detail: str | None = None) -> None:
        self.outbox.append(
            "agent_status",
            "agent",
            {"status": status, **({"detail": detail} if detail else {})},
        )

    # -- the loop --------------------------------------------------------

    def run(self, stop: threading.Event) -> None:
        """Poll until asked to stop. Releases the Steam AppID on the way out."""
        log.info(
            "watching for Wardogs (AppID %s), polling every %.1fs",
            richpresence.APP_ID,
            self.interval,
        )
        self._status("started")
        game_was_running = False

        try:
            while not stop.is_set():
                if not richpresence.game_running():
                    if self._rp is not None:
                        log.info("Wardogs closed - released its Steam AppID")
                        self._release()
                    if game_was_running:
                        self._status("game_closed")
                        game_was_running = False
                    # The save file is read directly and needs no Steam session,
                    # and Wardogs can finish writing it seconds after the process
                    # is gone — so XP is still watched while the game is not.
                    self._emit_xp()
                    if _wait(stop, IDLE_INTERVAL_SEC):
                        break
                    continue

                if self._rp is None:
                    session = SteamSession()
                    if not session.init():
                        detail = session.init_error or "is Steam running?"
                        log.warning("could not initialise Rich Presence: %s", detail)
                        self._status("steam_unavailable", detail)
                        if _wait(stop, IDLE_INTERVAL_SEC):
                            break
                        continue
                    self._rp = session
                    game_was_running = True
                    self._status("game_open")
                    log.info("Wardogs is running - reading its Rich Presence")

                try:
                    presence = self._rp.read()
                except SessionLost as exc:
                    # The helper died or stopped answering. Nothing is lost:
                    # the game is still running, and the next pass claims a
                    # fresh session. Releasing this one first is what keeps the
                    # dead child from being the thing Steam is still counting.
                    log.warning("lost the Steam session (%s); claiming a new one", exc)
                    self._release()
                    self._status("steam_unavailable", str(exc))
                    if _wait(stop, IDLE_INTERVAL_SEC):
                        break
                    continue

                self._emit_presence(presence)
                self._emit_spawns()

                heartbeat = time.monotonic() - self._last_heartbeat > HEARTBEAT_SEC
                self._emit_map(force=heartbeat)
                self._emit_xp(force=heartbeat)
                if heartbeat:
                    self._last_heartbeat = time.monotonic()

                if _wait_while_running(stop, self.interval):
                    break
        finally:
            self._status("stopped")
            self._release()

    def _release(self) -> None:
        """Hand the AppID back. The single most important line in this file.

        It ends the helper process, and Steam believes that — which is more
        than could be said for the calls it used to make.
        """
        if self._rp is None:
            return
        self._rp.shutdown()
        self._rp = None
