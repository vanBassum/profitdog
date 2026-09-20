"""Run the agent: collect locally, ship to a server.

    python -m profitdog.agent
    python -m profitdog.agent --server https://profitdog.example:5174

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
import threading

from .collector import Collector
from .config import UPLINK_INTERVAL_SEC, AgentSettings
from .outbox import Outbox
from .uplink import Uplink


def main() -> int:
    settings = AgentSettings.from_env()
    parser = argparse.ArgumentParser(prog="profitdog.agent")
    parser.add_argument("--server", default=settings.server, help="Server base URL")
    parser.add_argument("--outbox", default=str(settings.outbox), help="Outbox path")
    parser.add_argument("--label", default=settings.label, help="Name for this machine")
    parser.add_argument("--interval", type=float, default=settings.interval)
    parser.add_argument("--no-collect", action="store_true", help="Only drain the outbox")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
    )
    log = logging.getLogger("profitdog.agent")

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

    uplink = Uplink(outbox, args.server, label=args.label)
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
