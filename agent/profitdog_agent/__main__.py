"""Run the agent: collect locally, ship to a server.

    python -m profitdog_agent
    python -m profitdog_agent --server https://profitdog.example:5174

Two threads and nothing else. The collector polls and appends to the outbox;
the uplink drains the outbox to the server. They share only the outbox, which
is transactional, so neither can see the other half-done — and either can stop
without the other noticing. That is deliberate: playing with no server reachable
must cost nothing but disk, and the facts must all be there when one appears.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from pathlib import Path

from .adapters.steamsession import HELPER_FLAG, helper_main
from .collector import Collector
from .config import UPLINK_INTERVAL_SEC, AgentSettings
from .credentials import CredentialError, CredentialStore
from .linking import LinkingError, ensure_credential
from .outbox import Outbox
from .uplink import Uplink
from .version import agent_build, agent_version


def main() -> int:
    # Before anything else, including argparse: this same program is what the
    # collector re-runs to hold the Steam session, and that child must not
    # parse flags, touch the outbox or try to link a PC. See
    # `adapters/steamsession.py` for why the session lives in a process of its
    # own at all.
    if HELPER_FLAG in sys.argv[1:]:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
            stream=sys.stderr,
        )
        return helper_main()

    settings = AgentSettings.from_env()
    parser = argparse.ArgumentParser(prog="profitdog_agent")
    parser.add_argument("--server", default=settings.server, help="Server base URL")
    parser.add_argument("--outbox", default=str(settings.outbox), help="Outbox path")
    parser.add_argument("--label", default=settings.label, help="Name for this machine")
    parser.add_argument("--interval", type=float, default=settings.interval)
    parser.add_argument("--no-collect", action="store_true", help="Only drain the outbox")
    parser.add_argument(
        "--credentials", default=str(settings.credentials),
        help="Where this PC's upload credential is kept",
    )
    parser.add_argument(
        "--relink", action="store_true",
        help="Forget the stored credential and link this PC again",
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="Print the linking URL instead of opening a browser",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument(
        "--version", action="version", version=f"profitdog agent {agent_build()}"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
    )
    log = logging.getLogger("profitdog_agent")

    # First line out, before anything that can go wrong: whoever is reading a
    # log to work out why their PC is behaving oddly needs to know which build
    # produced it, and a build that failed to start still managed to say so.
    log.info("profitdog agent %s", agent_build())

    outbox = Outbox(args.outbox)
    log.info("agent %s (boot %s)", outbox.agent_id, outbox.boot_id)
    log.info("outbox %s — %d facts pending", outbox.path, outbox.depth())
    log.info("server %s", args.server)

    stop = threading.Event()

    def shutdown(*_args: object) -> None:
        log.info("stopping…")
        stop.set()

    signal.signal(signal.SIGINT, shutdown)
    try:
        signal.signal(signal.SIGTERM, shutdown)
    except (AttributeError, ValueError):  # pragma: no cover - platform dependent
        pass

    # Linking happens before any thread starts. There is nothing useful for
    # the collector to do on a PC that has no account to send to, and doing it
    # here means the code and the browser appear while somebody is still
    # looking at the window they started.
    store = CredentialStore(Path(args.credentials))
    if args.relink:
        store.clear()
    try:
        credential = ensure_credential(
            args.server,
            agent_id=outbox.agent_id,
            label=args.label,
            store=store,
            open_browser=not args.no_browser,
        )
    except (LinkingError, CredentialError) as exc:
        log.error("%s", exc)
        return 1
    except OSError as exc:
        log.error("could not reach %s to link this PC: %s", args.server, exc)
        return 1
    log.info(
        "credential %s (%s)",
        "protected by Windows" if credential.protected else "stored unprotected",
        store.path,
    )

    uplink = Uplink(
        outbox,
        args.server,
        label=args.label,
        credential=credential.token,
        version=agent_version(),
    )
    threads = [
        threading.Thread(
            target=uplink.run, args=(stop, UPLINK_INTERVAL_SEC), name="uplink", daemon=True
        )
    ]
    if not args.no_collect:
        collector = Collector(outbox, interval=args.interval)
        threads.append(
            threading.Thread(target=collector.run, args=(stop,), name="collector", daemon=True)
        )

    for thread in threads:
        thread.start()
    try:
        while any(t.is_alive() for t in threads) and not stop.is_set():
            stop.wait(0.5)
    finally:
        stop.set()
        # Bounded: a hung worker must never stop the process exiting, and the
        # collector's own `finally` is what hands the Steam AppID back.
        for thread in threads:
            thread.join(timeout=5.0)
        # One last attempt, so a clean shutdown does not leave facts waiting
        # for the next launch when the server is right there.
        try:
            uplink.drain()
        except Exception as exc:  # noqa: BLE001 - shutting down either way
            log.info("could not flush on exit (%s); %d facts held", exc, outbox.depth())
        outbox.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
