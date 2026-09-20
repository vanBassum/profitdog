"""Run the server: ingest, derive, serve.

    python -m profitdog_server
    python -m profitdog_server --database-url postgresql://... --port 5174

One process owns the database, the domain, the API and the built UI. The agent
is a separate program, on this machine or another one; this end does not care
which, and never reads a local game file.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
from pathlib import Path

import uvicorn

from .api import create_app
from .config import Settings


def _redacted(url: str) -> str:
    """A connection string safe to log: everything but the password.

    The URL reaches the log on every start, and a password in a log file is a
    password in every backup of that log file.
    """
    from urllib.parse import urlsplit, urlunsplit

    try:
        parts = urlsplit(url)
    except ValueError:  # pragma: no cover - defensive
        return "<unparseable>"
    if parts.password is None:
        return url
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    netloc = f"{parts.username}:***@{host}" if parts.username else host
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def main(argv: list[str] | None = None) -> int:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(prog="profitdog_server")
    parser.add_argument("--database-url", default=settings.database_url)
    parser.add_argument("--ui", default=str(settings.ui_dist))
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
    )

    # Replaced field by field rather than rebuilt, so that everything the
    # environment supplied and the command line says nothing about -- the
    # Google client, the allowlist, the cookie policy -- survives. Building a
    # fresh Settings here silently reset all of it to defaults, which for the
    # allowlist meant "admit nobody" and for cookie_secure meant the opposite
    # of what a local run wanted.
    resolved = dataclasses.replace(
        settings,
        database_url=args.database_url,
        ui_dist=Path(args.ui).resolve(),
        host=args.host,
        port=args.port,
        collector=False,
    )
    app = create_app(settings=resolved)
    log = logging.getLogger("profitdog_server")
    log.info("database %s", _redacted(resolved.database_url))
    log.info("ui       %s%s", resolved.ui_dist,
             "" if resolved.ui_dist.is_dir() else "  (not built — API only)")
    log.info("listening on http://%s:%d", resolved.host, resolved.port)
    uvicorn.run(app, host=resolved.host, port=resolved.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
