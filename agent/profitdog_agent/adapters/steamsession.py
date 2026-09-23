"""The Steamworks session, held in a child process — because nothing else works.

## What was wrong

Initialising Steamworks against Wardogs' AppID registers this process with the
Steam client as a running copy of the game. That is the price of reading the
game's own Rich Presence, and it has to be paid back when the game quits, or
Steam goes on showing Wardogs as running: the Play button stays "Stop", and
quitting Steam asks whether you really mean to, with a game still going.

The agent already did everything the documentation offers. It called
`SteamAPI_Shutdown()`. It then called `FreeLibrary` on `steam_api64.dll`, which
ctypes never does by itself — and `tests/test_steam_unload.py` proves that the
module really does leave the process. The log said "released its Steam AppID",
truthfully, and Steam carried on showing the game as running anyway. Only
closing the agent entirely fixed it.

The reason is one level down. `steam_api64.dll` is a shim; on init it loads
`steamclient64.dll`, which is the part that opens the IPC connection to the
Steam client and starts threads on it. Shutting down the API does not close
that, and unloading the shim does not unload what the shim loaded. Steam's view
of "is this process playing" follows that connection, and the connection lives
as long as the process does.

There is no supported call that takes it back. Valve's own guidance is that
`SteamAPI_Shutdown` is a thing you do on your way out, not a thing you recover
from — re-initialising afterwards is undefined. So the only reliable release is
the one the operating system performs: process exit.

## What happens instead

The session lives in a child process which does nothing else. The parent starts
it when Wardogs appears, asks it for a reading every poll, and ends it the
moment the game goes. The AppID is released because the process holding it is
gone, which is the one mechanism that cannot half-work.

The child is this same program with a hidden flag, so there is no second
executable to ship, sign or keep in step — the frozen EXE re-runs itself, and a
source checkout re-runs `python -m profitdog_agent`.

## The wire between them

One JSON object per line on the child's stdout; bare commands on its stdin.
`read` asks for one poll, EOF on stdin means stop. It is deliberately smaller
than a real protocol: the two ends ship together and always have.

Its stderr is left alone, so the child's logging appears in the agent's console
next to everything else. Only stdout carries the protocol, which is why nothing
in the child may print.

The child exits on EOF, and EOF arrives when the parent's end of the pipe
closes — including when the parent dies without a chance to say anything. An
agent killed from Task Manager still frees the AppID.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading

log = logging.getLogger("profitdog_agent.steam")

#: The flag that turns this same program into the helper. Hidden from
#: `--help`: it is a private arrangement between the agent and itself.
HELPER_FLAG = "--steam-helper"

#: How long the child gets to claim a Steam session before we give up on it.
#: Generous, because this is Steam opening a connection, not us doing work.
READY_TIMEOUT_SEC = 30.0

#: How long one Rich Presence poll may take. Well beyond any healthy read; it
#: is here so a wedged child is noticed rather than hanging the collector.
READ_TIMEOUT_SEC = 15.0

#: How long a child gets to exit politely before it is killed.
STOP_TIMEOUT_SEC = 5.0


class SessionLost(RuntimeError):
    """The helper is gone or unreachable. Drop the session and claim a new one."""


def helper_command() -> list[str]:
    """How to start another copy of this program as the helper."""
    if getattr(sys, "frozen", False):
        return [sys.executable, HELPER_FLAG]
    return [sys.executable, "-m", "profitdog_agent", HELPER_FLAG]


def helper_env() -> dict[str, str]:
    """The child's environment.

    From source, `-m profitdog_agent` has to find the same package this process
    is running, which is not a given: the documented invocation runs from the
    repository with `agent/` and `protocol/` put on the path by the caller, and
    none of that survives into a fresh interpreter. Handing it this process's
    own `sys.path` makes the child import exactly what the parent imported.

    A frozen build carries its own imports and is left alone — PYTHONPATH there
    can only point a bundled interpreter at somebody else's modules.
    """
    env = dict(os.environ)
    if not getattr(sys, "frozen", False):
        env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    return env


class SteamSession:
    """A Steamworks session for Wardogs' AppID, owned by a child process.

    The three methods the collector already called — `init`, `read`,
    `shutdown` — mean the same things they always did. What changed is that
    `shutdown` now ends a process, which is the only thing Steam believes.
    """

    def __init__(self, *, command: list[str] | None = None) -> None:
        self._command = command if command is not None else helper_command()
        self._proc: subprocess.Popen | None = None
        self._lines: queue.Queue = queue.Queue()
        self.init_error: str | None = None

    @property
    def active(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # -- lifecycle -------------------------------------------------------

    def init(self) -> bool:
        """Start the helper and wait for it to claim a session.

        Returns True once the child holds one, False if Steam is not running or
        the child could not be started — the same contract the in-process
        version had, so the caller's retry loop is unchanged.
        """
        self.init_error = None
        try:
            proc = subprocess.Popen(
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                # stderr is inherited on purpose: the child's warnings belong
                # in the agent's console, and only stdout is the protocol.
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=helper_env(),
            )
        except OSError as exc:
            self.init_error = f"could not start the Steam helper: {exc}"
            return False

        self._proc = proc
        self._lines = queue.Queue()
        threading.Thread(
            target=_pump,
            args=(proc, self._lines),
            name="steam-helper-reader",
            daemon=True,
        ).start()

        try:
            hello = self._recv(READY_TIMEOUT_SEC)
        except SessionLost as exc:
            self.init_error = str(exc)
            self.shutdown()
            return False
        if not hello.get("ok"):
            self.init_error = str(hello.get("error") or "Steam refused the session")
            self.shutdown()
            return False
        log.debug("Steam helper running as pid %d", proc.pid)
        return True

    def read(self) -> dict:
        """One Rich Presence poll, as the child saw it.

        Raises `SessionLost` if the child has gone or stopped answering. That
        is not fatal to anything: the game is still running, and the caller can
        claim a fresh session on its next pass.
        """
        self._send("read")
        reply = self._recv(READ_TIMEOUT_SEC)
        data = reply.get("data")
        if not isinstance(data, dict):
            raise SessionLost(str(reply.get("error") or "the helper sent no reading"))
        return data

    def shutdown(self) -> None:
        """End the child, and with it Steam's belief that the game is running.

        Idempotent, and called from a `finally`, so it must not raise however
        confused things already are. Closing stdin is the polite request; the
        kill is what makes it a guarantee, because a helper that outlives this
        call is exactly the bug the whole module exists to fix.
        """
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=STOP_TIMEOUT_SEC)
            return
        except subprocess.TimeoutExpired:
            log.warning(
                "the Steam helper did not exit within %.0fs — killing it",
                STOP_TIMEOUT_SEC,
            )
        try:
            proc.kill()
            proc.wait(timeout=STOP_TIMEOUT_SEC)
        except (OSError, subprocess.TimeoutExpired):
            log.error(
                "the Steam helper (pid %s) will not die; Steam may keep showing "
                "Wardogs as running until it does",
                proc.pid,
            )

    # -- the wire --------------------------------------------------------

    def _send(self, command: str) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None or proc.stdin is None:
            raise SessionLost("the Steam helper is not running")
        try:
            proc.stdin.write(command + "\n")
            proc.stdin.flush()
        except OSError as exc:
            raise SessionLost(f"could not reach the Steam helper: {exc}") from exc

    def _recv(self, timeout: float) -> dict:
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            raise SessionLost(
                f"the Steam helper did not answer within {timeout:.0f}s"
            ) from None
        if line is None:
            raise SessionLost("the Steam helper exited")
        try:
            reply = json.loads(line)
        except ValueError:
            raise SessionLost(f"the Steam helper said {line!r}") from None
        if not isinstance(reply, dict):
            raise SessionLost(f"the Steam helper said {line!r}")
        return reply


def _pump(proc: subprocess.Popen, lines: queue.Queue) -> None:
    """Move the child's stdout into a queue, so reads can have a timeout.

    `readline()` on a pipe blocks with no way to bound it, and a collector
    blocked forever on a wedged child would never notice the game had closed —
    which is the failure this file exists to prevent, arriving by another door.
    The sentinel on the way out turns "the child died" into an ordinary reply
    the waiting side can act on.
    """
    stream = proc.stdout
    try:
        if stream is not None:
            for line in stream:
                line = line.strip()
                if line:
                    lines.put(line)
    except (OSError, ValueError):
        pass
    finally:
        lines.put(None)
        try:
            if stream is not None:
                stream.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# The child
# ---------------------------------------------------------------------------


def _say(payload: dict) -> None:
    """One JSON object, one line, flushed. The whole of the child's stdout."""
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def helper_main() -> int:
    """Run as the helper: hold one session, answer reads, exit on EOF.

    Nothing here may write to stdout except `_say`, and nothing may `print`.
    Logging goes to stderr, which is inherited from the agent and so appears in
    its console.

    The `finally` is the ordinary way out, not the exceptional one: the parent
    closes stdin when Wardogs quits, this loop ends, and the process exits —
    which is what actually hands the AppID back.
    """
    from . import richpresence

    try:
        rp = richpresence.RichPresence()
    except Exception as exc:  # noqa: BLE001 - a missing DLL is the likely one
        _say({"ok": False, "error": str(exc)})
        return 1

    try:
        started = rp.init()
    except Exception as exc:  # noqa: BLE001 - ctypes against a foreign DLL
        _say({"ok": False, "error": str(exc)})
        return 1
    if not started:
        _say({"ok": False, "error": rp.init_error or "is Steam running?"})
        return 1

    _say({"ok": True})
    try:
        for line in sys.stdin:
            command = line.strip()
            if not command or command == "stop":
                break
            if command != "read":
                _say({"error": f"unknown command {command!r}"})
                continue
            try:
                _say({"data": rp.read()})
            except Exception as exc:  # noqa: BLE001 - report and go, never hang
                _say({"error": str(exc)})
                break
    except (OSError, ValueError, KeyboardInterrupt):
        pass
    finally:
        try:
            rp.shutdown()
        except Exception:  # noqa: BLE001 - exiting anyway, and that is the point
            log.warning("the Steam session did not shut down cleanly", exc_info=True)
    return 0


__all__ = [
    "HELPER_FLAG",
    "SessionLost",
    "SteamSession",
    "helper_command",
    "helper_main",
]
