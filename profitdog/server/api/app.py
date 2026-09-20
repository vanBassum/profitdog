"""The HTTP and WebSocket surface, and the UI it serves.

## The division of labour, restated at the boundary

REST answers questions about state that already exists: a list of matches, one
match's ledger, a period's buckets, an analysis pairing. Every one of those is
a snapshot, computable from the database alone, cacheable, and safe to ask for
again.

WebSockets carry *committed deltas only* — "this match gained a sample", "this
match ended" — and never anything a client could have computed itself. A socket
that pushed derived summaries would be a second implementation of the domain
racing the first one over the network.

## Sequence numbers, and what a reconnecting client does

Every delta has a `seq` from the `api_events` table, assigned in the same
transaction as the fact it announces. A client remembers the last one it saw.
On reconnect it sends `?since=N` and one of three things happens:

- **N is current.** Nothing to replay; the stream continues.
- **N is a little behind.** The gap is replayed, oldest first, then live
  deltas follow. The client never needed to refetch anything.
- **N is too far behind, or older than the retained log.** The server says
  `resync` and the client refetches a snapshot over REST. This is a promise
  about bounded work, not an error: replaying forty thousand events is slower
  than asking for the answer.

The client is told the current sequence in the `hello` frame either way, so it
always knows where it now is.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config import (
    DEFAULT_CHART_POINTS,
    MAX_CHART_POINTS,
    MAX_REPLAY_EVENTS,
    Settings,
)
from ..db import Database, open_database
from ..domain import rulesets
from ..domain.analysis import METRIC_KEYS, METRICS, PRESETS, analyse
from ..domain.cache import MatchCache
from ..domain.cache import drop as drop_cache
from ..domain.cache import stats as cache_stats
from ..domain.engine import MATCH_COLUMNS, derive_match
from ..domain.history import (
    Filters,
    best_match_key,
    build_buckets,
    build_markers,
    covered_range,
    filter_matches,
    totals_of,
)
from ..downsample import downsample
from ..importer import XP_ROLES
from ..ingest import Ingestor, decode_event
from ..normalize import display_map_name
from ...protocol import Ack, FactBatch

log = logging.getLogger("profitdog.server.api")


# ---------------------------------------------------------------------------
# Live fan-out
# ---------------------------------------------------------------------------


class Hub:
    """Fans committed deltas out to connected browsers.

    Every subscriber has its own bounded queue. A client too slow to keep up
    has its queue overflow, and is sent `resync` rather than being allowed to
    stall the publisher — one wedged browser tab must never be able to hold up
    the ingestion of a live match.
    """

    def __init__(self, capacity: int = 512) -> None:
        self.capacity = capacity
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.capacity)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, events: list[tuple[int, str, str | None, dict]]) -> None:
        """Called from the ingest thread, after the transaction committed."""
        if self._loop is None or not self._subscribers:
            return
        self._loop.call_soon_threadsafe(self._fanout, events)

    def _fanout(self, events: list[tuple[int, str, str | None, dict]]) -> None:
        for queue in list(self._subscribers):
            for seq, kind, match_key, payload in events:
                try:
                    queue.put_nowait(
                        {"type": "event", "seq": seq, "kind": kind,
                         "match_key": match_key, "payload": payload}
                    )
                except asyncio.QueueFull:
                    # Drain and tell it to start again: a partial stream is
                    # worse than an honest "you are behind".
                    with contextlib.suppress(asyncio.QueueEmpty):
                        while True:
                            queue.get_nowait()
                    with contextlib.suppress(asyncio.QueueFull):
                        queue.put_nowait({"type": "resync", "reason": "too slow"})
                    break


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


def create_app(db: Database | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    database = db or open_database(settings.database)
    hub = Hub()
    cache = MatchCache(database)
    ingestor = Ingestor(database)
    ingestor.on_published = hub.publish

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # The hub publishes from the ingest thread via `call_soon_threadsafe`,
        # so it needs the loop that will actually be running.
        hub.bind(asyncio.get_running_loop())
        yield

    app = FastAPI(title="profitdog", version="2.0.0", lifespan=lifespan)
    app.state.db = database
    app.state.hub = hub
    app.state.ingestor = ingestor
    app.state.cache = cache
    app.state.settings = settings

    # -- helpers ---------------------------------------------------------

    def now() -> datetime:
        return datetime.now(timezone.utc)

    def all_matches():
        # Through the cache: a full re-derivation of a thousand hours of
        # history measured 3.2s, which is not a page load. Every entry records
        # the ruleset and inputs it was built from, so a stale one cannot be
        # used — see `domain/cache.py`.
        return cache.derive_all(
            database.query(f"{MATCH_COLUMNS} ORDER BY started_at")
        )

    def selected(request: Request):
        """The matches the query string selects, derived under their rulesets."""
        filters = Filters.from_query(request.query_params)
        matches = all_matches()
        return filters, matches, filter_matches(matches, filters, now())

    def lives_of(matches) -> list:
        return [life for match in matches for life in match.lives]

    def match_or_404(key: str):
        row = database.query_one(f"{MATCH_COLUMNS} WHERE match_key = ?", (key,))
        if row is None:
            raise HTTPException(status_code=404, detail=f"no match {key!r}")
        return row

    # -- agent -----------------------------------------------------------

    @app.post("/api/agent/facts")
    async def post_facts(request: Request) -> JSONResponse:
        """Take delivery of a batch of facts. The only way data gets in."""
        try:
            batch = FactBatch.from_json(await request.json())
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # Ingestion is synchronous and holds the write lock, so it runs off the
        # event loop: a long batch from a reconnecting agent must not stall the
        # sockets feeding live browsers.
        ack: Ack = await asyncio.to_thread(ingestor.ingest, batch)
        return JSONResponse(ack.to_json())

    @app.get("/api/agent/cursor")
    async def agent_cursor(agent_id: str) -> dict:
        """Where the server is for this agent, so it can resume or resend."""
        return {
            "agent_id": agent_id,
            "acked_through": ingestor.cursor_for(agent_id),
            "server_seq": database.latest_seq(),
        }

    @app.get("/api/agents")
    async def agents() -> dict:
        rows = database.query(
            "SELECT agent_id, label, first_seen_at, last_seen_at, last_boot_id,"
            " acked_through FROM agents ORDER BY last_seen_at DESC"
        )
        return {"agents": [dict(r) for r in rows]}

    # -- snapshots -------------------------------------------------------

    @app.get("/api/health")
    async def health() -> dict:
        return {
            "ok": True,
            "schema": database.schema_version,
            "rulesets": rulesets.versions(),
            "seq": database.latest_seq(),
            "oldest_seq": database.oldest_seq(),
            "matches": database.scalar("SELECT COUNT(*) FROM matches"),
            "samples": database.scalar("SELECT COUNT(*) FROM cash_samples"),
            "cache": {**cache_stats(database), "hits": cache.hits, "misses": cache.misses},
        }

    @app.post("/api/maintenance/rebuild-cache")
    async def rebuild_cache() -> dict:
        """Throw the derived cache away. Nothing is lost; the next read rebuilds it."""
        dropped = await asyncio.to_thread(drop_cache, database)
        return {"dropped": dropped}

    @app.get("/api/matches")
    async def matches(request: Request) -> dict:
        filters, every, rows = selected(request)
        maps = sorted({m.map for m in every if m.map})
        builds = sorted({m.build for m in every if m.build}, reverse=True)
        return {
            "matches": [m.summary_json() for m in rows],
            "totals": totals_of(rows),
            "covered": covered_range(rows),
            "best": best_match_key(rows),
            "filters": {
                "maps": [{"value": m, "label": display_map_name(m)} for m in maps],
                "builds": builds,
                "has_unknown_build": any(not m.build for m in every),
                "applied": dataclasses.asdict(filters),
            },
            "available": len(every),
        }

    @app.get("/api/matches/{key}")
    async def match_detail(key: str) -> dict:
        return derive_match(database, match_or_404(key), with_ledger=True).detail_json()

    @app.get("/api/matches/{key}/curve")
    async def match_curve(
        key: str,
        max_points: int = Query(DEFAULT_CHART_POINTS, ge=2, le=MAX_CHART_POINTS),
    ) -> dict:
        row = match_or_404(key)
        rows = database.query(
            "SELECT elapsed_sec, cash, life FROM cash_samples WHERE match_id = ?"
            " ORDER BY elapsed_sec",
            (int(row["id"]),),
        )
        raw = [(r["elapsed_sec"], r["cash"], r["life"]) for r in rows]
        points = downsample(raw, max_points)
        return {
            "match_key": key,
            "points": points,
            "stored": len(raw),
            "returned": len(points),
            # Said out loud, so a reader of the chart knows whether they are
            # looking at every reading or a faithful reduction of them.
            "downsampled": len(points) < len(raw),
        }

    @app.get("/api/history")
    async def history(request: Request) -> dict:
        filters, _, rows = selected(request)
        buckets = build_buckets(rows, filters.grouping)
        return {
            "buckets": buckets,
            "markers": build_markers(buckets),
            "totals": totals_of(rows),
            "covered": covered_range(rows),
            "best": best_match_key(rows),
            "grouping": filters.grouping,
        }

    @app.get("/api/analysis")
    async def analysis(
        request: Request,
        x: str = Query("kitCost"),
        y: str = Query("earned"),
    ) -> dict:
        if x not in METRICS or y not in METRICS:
            raise HTTPException(status_code=400, detail="unknown metric")
        _, _, rows = selected(request)
        result = analyse(lives_of(rows), x, y)
        result["matches"] = len(rows)
        return result

    @app.get("/api/metrics")
    async def metrics() -> dict:
        """The metric catalogue, so the UI does not keep a second copy of it."""
        return {
            "metrics": [METRICS[k].as_json() for k in METRIC_KEYS],
            "presets": PRESETS,
        }

    @app.get("/api/xp")
    async def xp(request: Request) -> dict:
        """Role totals, and what each match contributed.

        Gains are read off the totals rather than the stored `delta` column
        wherever totals exist — the delta is a convenience, the totals are the
        fact. Imported CSV-era rows have only a delta, and are used as-is.
        """
        totals: dict[str, int] = {}
        for role in XP_ROLES:
            value = database.scalar(
                "SELECT total FROM xp_events WHERE role = ? AND total IS NOT NULL"
                " ORDER BY observed_at DESC, id DESC LIMIT 1",
                (role,),
            )
            if value is not None:
                totals[role] = int(value)

        gains: dict[str, dict[str, int]] = {}
        unattributed: dict[str, int] = {}
        previous: dict[str, int] = {}
        for row in database.query(
            "SELECT x.role, x.total, x.delta, m.match_key FROM xp_events x"
            " LEFT JOIN matches m ON m.id = x.match_id"
            " ORDER BY x.observed_at, x.id"
        ):
            role = str(row["role"])
            if row["total"] is not None:
                current = int(row["total"])
                gain = current - previous[role] if role in previous else 0
                previous[role] = current
            else:
                gain = int(row["delta"] or 0)
            if gain <= 0:
                continue
            key = row["match_key"]
            bucket = gains.setdefault(key, {}) if key else unattributed
            if key:
                bucket[role] = bucket.get(role, 0) + gain
            else:
                unattributed[role] = unattributed.get(role, 0) + gain

        return {"totals": totals, "by_match": gains, "unattributed": unattributed}

    # -- corrections -----------------------------------------------------

    @app.put("/api/matches/{key}/overrides/{life}")
    async def set_override(key: str, life: int, request: Request) -> dict:
        body = await request.json() if await request.body() else {}
        value = body.get("value")
        try:
            result = await asyncio.to_thread(
                ingestor.set_override, key, life, value, body.get("note")
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no match {key!r}") from exc
        return result

    @app.get("/api/matches/{key}/overrides")
    async def get_overrides(key: str) -> dict:
        row = match_or_404(key)
        rows = database.query(
            "SELECT life_number, value, note, created_at FROM overrides"
            " WHERE scope = 'life_kit_cost' AND match_id = ?",
            (int(row["id"]),),
        )
        return {"match_key": key, "overrides": [dict(r) for r in rows]}

    # -- live ------------------------------------------------------------

    @app.get("/api/stream/cursor")
    async def stream_cursor() -> dict:
        return {"seq": database.latest_seq(), "oldest": database.oldest_seq()}

    @app.websocket("/api/live")
    async def live(socket: WebSocket, since: int | None = None) -> None:
        await socket.accept()
        queue = hub.subscribe()
        try:
            current = database.latest_seq()
            oldest = database.oldest_seq()

            replay: list[dict] = []
            resync = False
            if since is None:
                resync = False
            elif since > current:
                # A client ahead of us has been talking to a different database
                # — a restore, or a different server. Start it again.
                resync = True
            elif current - since > MAX_REPLAY_EVENTS:
                resync = True
            elif oldest and since < oldest - 1:
                resync = True
            else:
                replay = [
                    decode_event(row)
                    for row in database.query(
                        "SELECT seq, kind, match_key, payload, committed_at"
                        " FROM api_events WHERE seq > ? ORDER BY seq LIMIT ?",
                        (since, MAX_REPLAY_EVENTS),
                    )
                ]

            await socket.send_text(
                json.dumps(
                    {
                        "type": "hello",
                        "seq": current,
                        "oldest": oldest,
                        "replaying": len(replay),
                        # Said explicitly rather than implied by an empty
                        # replay: "nothing happened" and "start again" are
                        # different instructions.
                        "resync": resync,
                    }
                )
            )
            for event in replay:
                await socket.send_text(json.dumps({"type": "event", **event}))

            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=25.0)
                except asyncio.TimeoutError:
                    # Keeps intermediaries from closing an idle socket, and
                    # gives the client a liveness signal between matches.
                    await socket.send_text(json.dumps({"type": "ping"}))
                    continue
                await socket.send_text(json.dumps(message))
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001 - one socket must not take the server
            log.warning("live socket failed: %s", exc)
        finally:
            hub.unsubscribe(queue)

    # -- the UI ----------------------------------------------------------

    dist = Path(settings.ui_dist)
    if dist.is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}")
        async def spa(path: str) -> Any:
            """Serve the built UI, with client-side routes falling back to it.

            `/history` and `/analysis` are addresses the browser router owns,
            not files. Anything that is not a real file and not an API route is
            answered with `index.html` so a reload on one of them works.
            """
            candidate = (dist / path).resolve()
            if path and candidate.is_file() and candidate.is_relative_to(dist.resolve()):
                return FileResponse(candidate)
            index = dist / "index.html"
            if not index.is_file():
                raise HTTPException(status_code=404, detail="UI not built")
            return FileResponse(index)

    return app
