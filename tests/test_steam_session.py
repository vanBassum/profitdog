"""The Steam session lives in a child process, and ending it ends the process.

`test_steam_unload.py` pins the older half of this: `SteamAPI_Shutdown()` plus
an explicit `FreeLibrary` really does take `steam_api64.dll` back out of the
process. It was not enough. Steam went on showing Wardogs as running until the
whole agent was closed, because the shim loads `steamclient64.dll` and neither
shutting the API down nor unloading the shim closes that library's connection
to the Steam client.

So the guarantee moved to the one mechanism that cannot half-work: the session
is held by a child process, and `shutdown()` ends it. What is worth testing is
exactly that — that the child really dies, on the ordinary path and on the
awkward ones — which needs no Steam and no Windows, because the part under test
is process lifetime and a line protocol.

The fake helper is a plain Python script, so these run anywhere.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time

import pytest

from profitdog_agent.adapters.steamsession import (
    HELPER_FLAG,
    SessionLost,
    SteamSession,
    helper_command,
    helper_env,
)


def _fake_helper(tmp_path, body: str) -> list[str]:
    """A stand-in child speaking the same one-JSON-object-per-line protocol."""
    script = tmp_path / "fake_helper.py"
    script.write_text(
        textwrap.dedent(
            """
            import json, sys, time

            def say(payload):
                sys.stdout.write(json.dumps(payload) + "\\n")
                sys.stdout.flush()

            """
        )
        + textwrap.dedent(body),
        encoding="utf-8",
    )
    return [sys.executable, str(script)]


GOOD_HELPER = """
    say({"ok": True})
    for line in sys.stdin:
        command = line.strip()
        if command != "read":
            break
        say({"data": {"game_state": "playing", "profit_loss": "+$150"}})
"""


def test_a_session_reads_through_the_child(tmp_path):
    session = SteamSession(command=_fake_helper(tmp_path, GOOD_HELPER))
    assert session.init(), session.init_error
    try:
        assert session.read() == {"game_state": "playing", "profit_loss": "+$150"}
        assert session.read()["game_state"] == "playing"
    finally:
        session.shutdown()


def test_shutdown_ends_the_process(tmp_path):
    """The whole point. Steam frees the AppID when the process holding it exits.

    Not "the session was closed" or "the DLL was unloaded" — both of those were
    true when this was broken. The process is gone, or nothing has been fixed.
    """
    session = SteamSession(command=_fake_helper(tmp_path, GOOD_HELPER))
    assert session.init(), session.init_error
    proc = session._proc
    assert proc is not None and proc.poll() is None

    session.shutdown()

    assert proc.poll() is not None, (
        "the helper outlived shutdown() — Steam will keep showing Wardogs as "
        "running for as long as it does"
    )
    assert not session.active


def test_shutdown_kills_a_child_that_will_not_leave(tmp_path):
    """Closing stdin is the request; the kill is the guarantee.

    A helper wedged inside Steamworks will not notice EOF. Waiting politely
    forever would leave exactly the process this design exists to remove, so
    after a bounded wait it is killed.
    """
    stubborn = """
        import signal
        try:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        except (AttributeError, ValueError):
            pass
        say({"ok": True})
        while True:
            time.sleep(3600)
    """
    session = SteamSession(command=_fake_helper(tmp_path, stubborn))
    assert session.init(), session.init_error
    proc = session._proc
    assert proc is not None

    started = time.monotonic()
    session.shutdown()
    elapsed = time.monotonic() - started

    assert proc.poll() is not None, "a stubborn helper survived shutdown()"
    # It waits once politely and then kills; two full timeouts would mean the
    # kill path never ran.
    assert elapsed < 20.0, f"shutdown took {elapsed:.1f}s"


def test_shutdown_is_idempotent_and_safe_before_init(tmp_path):
    """It is called from a `finally`, so it meets every half-built state."""
    session = SteamSession(command=_fake_helper(tmp_path, GOOD_HELPER))
    session.shutdown()  # never started
    assert session.init(), session.init_error
    session.shutdown()
    session.shutdown()
    assert not session.active


def test_init_reports_why_steam_refused(tmp_path):
    """A child that cannot claim a session says so, and leaves nothing behind."""
    refuses = """
        say({"ok": False, "error": "Steam is not running"})
    """
    session = SteamSession(command=_fake_helper(tmp_path, refuses))
    assert not session.init()
    assert "Steam is not running" in (session.init_error or "")
    assert not session.active


def test_init_survives_a_child_that_cannot_start():
    """A missing interpreter is a failed init, not a crashed agent."""
    session = SteamSession(command=["profitdog-no-such-program-anywhere"])
    assert not session.init()
    assert session.init_error


def test_a_child_that_dies_mid_session_raises_session_lost(tmp_path):
    """The caller can claim a new session; it must not hang waiting for a corpse."""
    quitter = """
        say({"ok": True})
        sys.stdin.readline()
        sys.exit(0)
    """
    session = SteamSession(command=_fake_helper(tmp_path, quitter))
    assert session.init(), session.init_error
    try:
        with pytest.raises(SessionLost):
            # The first read is consumed and the child exits without answering;
            # whichever read notices, it has to raise rather than block.
            session.read()
            session.read()
    finally:
        session.shutdown()


def test_a_silent_child_does_not_hang_the_collector(tmp_path, monkeypatch):
    """A helper wedged inside Steam must time out, not stop the poll loop.

    The collector's own game-closed check lives in the same loop as its reads.
    A read that could block forever would mean a game that quit an hour ago and
    an AppID nobody ever hands back.
    """
    import profitdog_agent.adapters.steamsession as steamsession

    monkeypatch.setattr(steamsession, "READ_TIMEOUT_SEC", 0.5)
    silent = """
        say({"ok": True})
        while True:
            time.sleep(3600)
    """
    session = SteamSession(command=_fake_helper(tmp_path, silent))
    assert session.init(), session.init_error
    try:
        with pytest.raises(SessionLost):
            session.read()
    finally:
        session.shutdown()


def test_the_helper_command_is_this_same_program():
    """No second executable to ship, sign or keep in step."""
    command = helper_command()
    assert command[0] == sys.executable
    assert command[-1] == HELPER_FLAG
    if not getattr(sys, "frozen", False):
        assert command[1:3] == ["-m", "profitdog_agent"]


def test_the_child_is_handed_this_process_s_import_path():
    """From source, a fresh interpreter finds none of the monorepo's roots.

    `conftest.py` puts `agent/` and `protocol/` on this process's `sys.path`,
    and nothing about that survives into a subprocess — which is how the child
    was failing with "No module named profitdog_agent" before `helper_env`
    existed. The frozen build carries its own imports and is left alone.
    """
    env = helper_env()
    if getattr(sys, "frozen", False):
        assert "PYTHONPATH" not in env or env["PYTHONPATH"] == os.environ.get(
            "PYTHONPATH", env.get("PYTHONPATH")
        )
        return
    roots = env["PYTHONPATH"].split(os.pathsep)
    assert any(root.endswith("agent") for root in roots), roots


def test_the_helper_flag_never_reaches_the_ordinary_agent():
    """`--steam-helper` must be intercepted before argparse, the outbox or linking.

    Run for real, in a subprocess, because "before" is a statement about import
    and startup order that an in-process call cannot make. With no Steam on the
    machine the child refuses the session and exits — which is the proof it got
    into `helper_main` rather than into the agent, whose first move would have
    been to reach for the outbox and a server.
    """
    result = subprocess.run(
        helper_command(),
        env=helper_env(),
        input="",
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert "Traceback" not in result.stderr, result.stderr
    # One JSON line on stdout and nothing else: no argparse usage message, no
    # linking code, no facts-pending banner.
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, f"the helper said nothing (stderr: {result.stderr})"
    import json

    first = json.loads(lines[0])
    assert set(first) <= {"ok", "error"}


# ---------------------------------------------------------------------------
# The collector's half of the bargain
# ---------------------------------------------------------------------------


class _FakeSession:
    """A session that records what was done to it, and when."""

    made: list["_FakeSession"] = []

    def __init__(self, **_kwargs):
        self.init_error = None
        self.reads = 0
        self.shutdowns = 0
        _FakeSession.made.append(self)

    def init(self):
        return True

    def read(self):
        self.reads += 1
        return {"game_state": "playing"}

    def shutdown(self):
        self.shutdowns += 1


def test_the_collector_ends_the_session_the_moment_the_game_goes(
    tmp_path, monkeypatch
):
    """The bug as it was reported: the game closed, and Steam did not agree.

    A poll while Wardogs runs, then one after it quits. The session must be
    shut down on that second pass — not at some later heartbeat, and not when
    the agent itself is eventually closed, which was the only thing that worked
    before.
    """
    import threading as _threading

    from profitdog_agent import collector as collector_module
    from profitdog_agent.outbox import Outbox

    _FakeSession.made = []
    monkeypatch.setattr(collector_module, "SteamSession", _FakeSession)
    # The local sources read real game files; this test is about the session.
    monkeypatch.setattr(collector_module.mapinfo, "spawn_events", lambda: [])
    monkeypatch.setattr(collector_module.mapinfo, "current_map", lambda: None)
    monkeypatch.setattr(collector_module.rolexp, "read_role_xp", lambda: {})

    running = iter([True, False])
    stop = _threading.Event()

    def game_running():
        try:
            return next(running)
        except StopIteration:
            stop.set()  # second pass done; let the loop finish
            return False

    monkeypatch.setattr(collector_module.richpresence, "game_running", game_running)

    outbox = Outbox(tmp_path / "outbox.sqlite3")
    try:
        collector_module.Collector(outbox, interval=0.01).run(stop)
    finally:
        outbox.close()

    assert len(_FakeSession.made) == 1, "expected exactly one session to be claimed"
    session = _FakeSession.made[0]
    assert session.reads == 1
    assert session.shutdowns >= 1, (
        "the collector never released the session when Wardogs closed"
    )
