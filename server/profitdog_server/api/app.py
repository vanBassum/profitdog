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
import base64
import contextlib
import dataclasses
import json
import logging
import secrets
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from contextlib import asynccontextmanager

from fastapi import (
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles

from .. import auth
from ..config import (
    DEFAULT_CHART_POINTS,
    LINK_POLL_INTERVAL_SEC,
    SESSION_TTL_SEC,
    MAX_CHART_POINTS,
    MAX_REPLAY_EVENTS,
    Settings,
)
from ..ownership import ensure_owner
from . import pages
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
from profitdog_protocol import Ack, FactBatch

log = logging.getLogger("profitdog_server.api")


# ---------------------------------------------------------------------------
# Live fan-out
# ---------------------------------------------------------------------------


def _pack(payload: dict) -> str:
    """A dict small enough to live in a cookie, encoded so it survives one."""
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unpack(value: str | None) -> dict:
    """The reverse, and never an exception: a bad cookie is simply no cookie."""
    if not value:
        return {}
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, TypeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _safe_next(target: str | None) -> str:
    """Where to go after signing in, if it is somewhere on this server.

    An open redirect is the classic way a login page gets turned into a
    phishing hop: `/login?next=https://example.invalid` sends someone who
    started at the real address somewhere else, with the trust the real
    address earned. Only a path is ever honoured, and a protocol-relative
    `//host` is a URL wearing a path's clothes, so it is refused too.
    """
    value = (target or "/").strip()
    if not value.startswith("/") or value.startswith("//"):
        return "/"
    return value


class Hub:
    """Fans committed deltas out to connected browsers.

    Every subscriber has its own bounded queue. A client too slow to keep up
    has its queue overflow, and is sent `resync` rather than being allowed to
    stall the publisher — one wedged browser tab must never be able to hold up
    the ingestion of a live match.
    """

    def __init__(self, capacity: int = 512) -> None:
        self.capacity = capacity
        #: Queues by account. A delta is only ever put on the queues of the
        #: account that owns it, so a socket cannot be sent another person's
        #: match even momentarily -- there is no filtering step after the fact
        #: that could be forgotten.
        self._subscribers: dict[int, set[asyncio.Queue]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self, user_id: int) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.capacity)
        self._subscribers.setdefault(user_id, set()).add(queue)
        return queue

    def unsubscribe(self, user_id: int, queue: asyncio.Queue) -> None:
        queues = self._subscribers.get(user_id)
        if queues is None:
            return
        queues.discard(queue)
        if not queues:
            self._subscribers.pop(user_id, None)

    def publish(
        self, events: list[tuple[int, str, str | None, dict, int | None]]
    ) -> None:
        """Called from the ingest thread, after the transaction committed."""
        if self._loop is None or not self._subscribers:
            return
        self._loop.call_soon_threadsafe(self._fanout, events)

    def _fanout(
        self, events: list[tuple[int, str, str | None, dict, int | None]]
    ) -> None:
        by_user: dict[int, list[tuple]] = {}
        for seq, kind, match_key, payload, user_id in events:
            # An event with no owner belongs to nobody and is shown to nobody.
            # That case is pre-accounts data being replayed, not a broadcast.
            if user_id is None:
                continue
            by_user.setdefault(int(user_id), []).append((seq, kind, match_key, payload))

        for user_id, batch in by_user.items():
            for queue in list(self._subscribers.get(user_id, ())):
                self._deliver(queue, batch)

    def _deliver(self, queue: asyncio.Queue, events: list[tuple]) -> None:
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
    database = db or open_database(settings.database_url)
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

    # -- who is asking ---------------------------------------------------

    # Everything written before there were accounts is adopted by the address
    # in PROFITDOG_OWNER_EMAIL, and that account is claimed by its first
    # Google sign-in. Idempotent, so it is simply done at every startup.
    ensure_owner(database, settings.owner_email)

    def session_token(request: Request) -> str | None:
        return request.cookies.get(auth.SESSION_COOKIE)

    def viewer(request: Request) -> auth.User | None:
        """The signed-in person, or None. Never raises."""
        return auth.user_for_session(database, session_token(request))

    def require_user(request: Request) -> auth.User:
        """The signed-in person, or 401.

        Every route that answers a question about data goes through this, and
        takes the returned id into its queries. A route that forgets returns
        nothing at all rather than everything, because the queries below have
        no unscoped form.
        """
        user = viewer(request)
        if user is None:
            raise HTTPException(status_code=401, detail="sign in required")
        return user

    def require_agent(request: Request) -> auth.AgentIdentity:
        """The linked PC behind a bearer credential, or 401.

        Deliberately a different function from `require_user`: a browser
        session can never satisfy this, and an agent credential can never
        satisfy that one.
        """
        header = request.headers.get("authorization") or ""
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(status_code=401, detail="agent credential required")
        identity = auth.identify_agent(database, token.strip())
        if identity is None:
            raise HTTPException(status_code=401, detail="unknown or revoked credential")
        return identity

    def set_session_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            auth.SESSION_COOKIE,
            token,
            max_age=SESSION_TTL_SEC,
            httponly=True,
            secure=settings.cookie_secure,
            # Lax rather than Strict: the Google callback is a cross-site
            # redirect back into this origin, and Strict would withhold the
            # cookie on exactly that navigation. Lax still withholds it from
            # cross-site POSTs, which is what matters for the override route.
            samesite="lax",
            path="/",
        )

    # -- helpers ---------------------------------------------------------

    def now() -> datetime:
        return datetime.now(timezone.utc)

    def all_matches(user_id: int):
        """Every match belonging to one account, derived under its rulesets.

        The account is a required argument and the `WHERE` is not optional:
        there is no way to call this and get everyone's matches, which is the
        only way to be sure no route accidentally does.

        Through the cache: a full re-derivation of a thousand hours of history
        measured 3.2s, which is not a page load. Every entry records the
        ruleset and inputs it was built from, so a stale one cannot be used —
        see `domain/cache.py`. The cache is keyed by match, and matches belong
        to one account, so it needs no notion of who is reading.
        """
        return cache.derive_all(
            database.query(
                f"{MATCH_COLUMNS} WHERE user_id = %s ORDER BY started_at",
                (user_id,),
            )
        )

    def selected(request: Request, user_id: int):
        """The matches the query string selects, within one account."""
        filters = Filters.from_query(request.query_params)
        matches = all_matches(user_id)
        return filters, matches, filter_matches(matches, filters, now())

    def lives_of(matches) -> list:
        return [life for match in matches for life in match.lives]

    def match_or_404(key: str, user_id: int):
        """One match of this account's, or 404.

        Another account's match is 404, not 403. A 403 would confirm that the
        key names a real match, which is a fact about someone else's history
        and not ours to hand out.
        """
        row = database.query_one(
            f"{MATCH_COLUMNS} WHERE match_key = %s AND user_id = %s", (key, user_id)
        )
        if row is None:
            raise HTTPException(status_code=404, detail=f"no match {key!r}")
        return row

    # -- agent -----------------------------------------------------------

    @app.post("/api/agent/facts")
    async def post_facts(request: Request) -> JSONResponse:
        """Take delivery of a batch of facts. The only way data gets in."""
        identity = require_agent(request)
        try:
            batch = FactBatch.from_json(await request.json())
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # Ingestion is synchronous and holds the write lock, so it runs off the
        # event loop: a long batch from a reconnecting agent must not stall the
        # sockets feeding live browsers.
        try:
            ack: Ack = await asyncio.to_thread(
                ingestor.ingest, batch, identity=identity
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        await asyncio.to_thread(auth.touch_credential, database, identity.credential_id)
        return JSONResponse(ack.to_json())

    @app.get("/api/agent/cursor")
    async def agent_cursor(request: Request) -> dict:
        """Where the server is for this agent, so it can resume or resend.

        The agent is not asked which agent it is; its credential says so. The
        old query parameter would have let anyone read any agent's cursor.
        """
        identity = require_agent(request)
        return {
            "agent_id": identity.agent_id,
            "acked_through": ingestor.cursor_for(identity.agent_id),
            "server_seq": database.latest_seq(),
        }

    @app.get("/api/agents")
    async def agents(request: Request) -> dict:
        user = require_user(request)
        rows = database.query(
            "SELECT agent_id, label, first_seen_at, last_seen_at, last_boot_id,"
            " acked_through FROM agents WHERE user_id = %s ORDER BY last_seen_at DESC",
            (user.id,),
        )
        return {"agents": [dict(r) for r in rows]}

    # -- snapshots -------------------------------------------------------

    @app.get("/api/health")
    async def health(request: Request) -> dict:
        """Counts for the signed-in account, not for the server.

        The totals used to be global. On a shared server that is a readout of
        how much everyone else is playing, so they are scoped like everything
        else; the genuinely server-wide fields that remain say nothing about
        anybody.
        """
        user = require_user(request)
        return {
            "ok": True,
            "schema": database.schema_version,
            "rulesets": rulesets.versions(),
            "seq": database.latest_seq(),
            "oldest_seq": database.oldest_seq(),
            "matches": database.scalar(
                "SELECT COUNT(*) FROM matches WHERE user_id = %s", (user.id,)
            ),
            "samples": database.scalar(
                "SELECT COUNT(*) FROM cash_samples s JOIN matches m ON m.id = s.match_id"
                " WHERE m.user_id = %s",
                (user.id,),
            ),
            "cache": {**cache_stats(database), "hits": cache.hits, "misses": cache.misses},
        }

    @app.post("/api/maintenance/rebuild-cache")
    async def rebuild_cache(request: Request) -> dict:
        """Throw the derived cache away. Nothing is lost; the next read rebuilds it.

        Behind a session not because the cache is private -- it holds nothing
        that is not rederivable -- but because dropping it is real work, and an
        anonymous caller should not be able to ask for it on a loop.
        """
        require_user(request)
        dropped = await asyncio.to_thread(drop_cache, database)
        return {"dropped": dropped}

    @app.get("/api/matches")
    async def matches(request: Request) -> dict:
        user = require_user(request)
        filters, every, rows = selected(request, user.id)
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
    async def match_detail(key: str, request: Request) -> dict:
        user = require_user(request)
        return derive_match(
            database, match_or_404(key, user.id), with_ledger=True
        ).detail_json()

    @app.get("/api/matches/{key}/curve")
    async def match_curve(
        key: str,
        request: Request,
        max_points: int = Query(DEFAULT_CHART_POINTS, ge=2, le=MAX_CHART_POINTS),
    ) -> dict:
        user = require_user(request)
        row = match_or_404(key, user.id)
        rows = database.query(
            "SELECT elapsed_sec, cash, life FROM cash_samples WHERE match_id = %s"
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
        user = require_user(request)
        filters, _, rows = selected(request, user.id)
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
        user = require_user(request)
        if x not in METRICS or y not in METRICS:
            raise HTTPException(status_code=400, detail="unknown metric")
        _, _, rows = selected(request, user.id)
        result = analyse(lives_of(rows), x, y)
        result["matches"] = len(rows)
        return result

    @app.get("/api/metrics")
    async def metrics(request: Request) -> dict:
        """The metric catalogue, so the UI does not keep a second copy of it."""
        require_user(request)
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

        Scoped through `agent_ref` rather than through the match: an XP reading
        can have no match (the save file is read while the game is closed), so
        joining to `matches` would have quietly dropped exactly the rows that
        land between matches. The agent is what always has an owner.
        """
        user = require_user(request)
        owned = (
            " x.agent_ref IN (SELECT id FROM agents WHERE user_id = %s)"
        )
        totals: dict[str, int] = {}
        for role in XP_ROLES:
            value = database.scalar(
                "SELECT x.total FROM xp_events x WHERE x.role = %s"
                " AND x.total IS NOT NULL AND" + owned +
                " ORDER BY x.observed_at DESC, x.id DESC LIMIT 1",
                (role, user.id),
            )
            if value is not None:
                totals[role] = int(value)

        gains: dict[str, dict[str, int]] = {}
        unattributed: dict[str, int] = {}
        previous: dict[str, int] = {}
        for row in database.query(
            "SELECT x.role, x.total, x.delta, m.match_key FROM xp_events x"
            " LEFT JOIN matches m ON m.id = x.match_id"
            " WHERE" + owned +
            " ORDER BY x.observed_at, x.id",
            (user.id,),
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
        user = require_user(request)
        body = await request.json() if await request.body() else {}
        value = body.get("value")
        try:
            result = await asyncio.to_thread(
                ingestor.set_override, key, life, value, body.get("note"),
                user_id=user.id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no match {key!r}") from exc
        return result

    @app.get("/api/matches/{key}/overrides")
    async def get_overrides(key: str, request: Request) -> dict:
        user = require_user(request)
        row = match_or_404(key, user.id)
        rows = database.query(
            "SELECT life_number, value, note, created_at FROM overrides"
            " WHERE scope = 'life_kit_cost' AND match_id = %s",
            (int(row["id"]),),
        )
        return {"match_key": key, "overrides": [dict(r) for r in rows]}

    # -- live ------------------------------------------------------------

    @app.get("/api/stream/cursor")
    async def stream_cursor(request: Request) -> dict:
        require_user(request)
        return {"seq": database.latest_seq(), "oldest": database.oldest_seq()}

    @app.websocket("/api/live")
    async def live(socket: WebSocket, since: int | None = None) -> None:
        # Authenticated before the handshake is accepted, from the same cookie
        # the REST routes use. A socket is a read of live data and is gated
        # exactly as a read would be; accepting first and closing after would
        # hand an unauthenticated client a moment of connection it can use to
        # learn the server is there and someone is playing.
        user = auth.user_for_session(database, socket.cookies.get(auth.SESSION_COOKIE))
        if user is None:
            await socket.close(code=4401, reason="sign in required")
            return

        await socket.accept()
        queue = hub.subscribe(user.id)
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
                # Scoped like every other read. The sequence itself is
                # server-wide, so a client's cursor may skip numbers that
                # belonged to someone else; that is fine, because the cursor
                # is only ever compared against the server's own latest.
                replay = [
                    decode_event(row)
                    for row in database.query(
                        "SELECT seq, kind, match_key, payload, committed_at"
                        " FROM api_events WHERE seq > %s AND user_id = %s"
                        " ORDER BY seq LIMIT %s",
                        (since, user.id, MAX_REPLAY_EVENTS),
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
            hub.unsubscribe(user.id, queue)

    # -- signing in ------------------------------------------------------

    @app.get("/api/me")
    async def me(request: Request) -> dict:
        """Who the browser is. The UI's first call, and its sign-in check."""
        user = viewer(request)
        if user is None:
            raise HTTPException(status_code=401, detail="sign in required")
        return {"user": user.as_json()}

    @app.get("/login")
    async def login_view(request: Request, next: str = "/") -> Response:
        if viewer(request) is not None:
            return RedirectResponse(_safe_next(next), status_code=303)
        return HTMLResponse(
            pages.login_page(
                next_url=_safe_next(next), configured=settings.auth_configured
            )
        )

    @app.get("/auth/login")
    async def auth_login(request: Request, next: str = "/") -> Response:
        """Send the browser to Google, remembering why.

        `state` is what makes the callback verifiable as the continuation of
        *this* login rather than a URL somebody was handed; `nonce` does the
        same for the ID token. Both live in a short cookie of their own, so an
        abandoned login leaves nothing behind and never disturbs a session that
        already exists.
        """
        if not settings.auth_configured:
            return HTMLResponse(pages.login_page(configured=False), status_code=503)
        state = auth.new_token(24)
        nonce = auth.new_token(24)
        response = RedirectResponse(
            auth.authorization_url(settings, state=state, nonce=nonce),
            status_code=303,
        )
        response.set_cookie(
            auth.FLOW_COOKIE,
            # Base64 rather than raw JSON: a cookie value cannot safely carry
            # quotes or commas, and a JSON object is mostly quotes. Left raw,
            # it comes back quoted and backslash-escaped by some clients and
            # fails to parse on exactly the request that completes a login.
            _pack({"state": state, "nonce": nonce, "next": _safe_next(next)}),
            max_age=auth.FLOW_TTL_SEC,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="lax",
            path="/",
        )
        return response

    @app.get("/auth/callback")
    async def auth_callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ) -> Response:
        flow = _unpack(request.cookies.get(auth.FLOW_COOKIE))

        def refuse(message: str, status: int = 400) -> Response:
            response = HTMLResponse(
                pages.login_page(error=message, configured=settings.auth_configured),
                status_code=status,
            )
            response.delete_cookie(auth.FLOW_COOKIE, path="/")
            return response

        if error:
            return refuse("Google reported: " + str(error)[:120])
        if not code or not state:
            return refuse("That sign-in did not complete. Try again.")
        expected = str(flow.get("state") or "")
        if not expected or not secrets.compare_digest(expected, state):
            # Either the cookie is gone (a stale tab, a different browser) or
            # this callback was not started here at all.
            return refuse("That sign-in link is stale. Try again.")

        try:
            tokens = await auth.exchange_code(code, settings)
            claims = auth.read_id_token(
                tokens["id_token"], settings, nonce=str(flow.get("nonce") or "")
            )
            user = await asyncio.to_thread(
                auth.upsert_google_user, database, claims, settings
            )
        except PermissionError as exc:
            return refuse(str(exc), status=403)

        token = await asyncio.to_thread(
            auth.create_session,
            database,
            user.id,
            user_agent=request.headers.get("user-agent"),
        )
        response = RedirectResponse(_safe_next(flow.get("next")), status_code=303)
        set_session_cookie(response, token)
        response.delete_cookie(auth.FLOW_COOKIE, path="/")
        return response

    @app.get("/auth/logout")
    @app.post("/auth/logout")
    async def auth_logout(request: Request) -> Response:
        await asyncio.to_thread(auth.end_session, database, session_token(request))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(
            auth.SESSION_COOKIE,
            path="/",
            httponly=True,
            secure=settings.cookie_secure,
            samesite="lax",
        )
        return response

    # -- linking a PC ----------------------------------------------------

    @app.post("/api/agent/link/start")
    async def link_start(request: Request) -> dict:
        """An unlinked agent asks for a code. The only unauthenticated route.

        It has to be: the whole point is that the agent has no credential yet.
        What it gets back is worth nothing on its own -- a code that does
        nothing until a signed-in person approves it, and a device secret that
        proves which PC is entitled to collect the result.
        """
        body = await request.json() if await request.body() else {}
        agent_id = str(body.get("agent_id") or "").strip()
        if not agent_id:
            raise HTTPException(status_code=400, detail="agent_id is required")
        raw_label = body.get("label")
        label = str(raw_label).strip() or None if raw_label else None
        req = await asyncio.to_thread(
            auth.start_link, database, agent_id=agent_id, label=label
        )
        base = settings.public_url.rstrip("/")
        return {
            "code": req.code,
            "device_secret": req.device_secret,
            "verification_url": base + "/link",
            "verification_url_complete": base + "/link?code=" + req.code,
            "expires_in": req.expires_in,
            "interval": LINK_POLL_INTERVAL_SEC,
        }

    @app.post("/api/agent/link/poll")
    async def link_poll(request: Request) -> JSONResponse:
        """The agent waiting to be approved. Hands over the credential once."""
        body = await request.json() if await request.body() else {}
        code = str(body.get("code") or "")
        secret = str(body.get("device_secret") or "")
        try:
            token = await asyncio.to_thread(
                auth.redeem_link, database, code=code, device_secret=secret
            )
        except auth.LinkPending:
            return JSONResponse({"status": "pending"}, status_code=202)
        except auth.LinkFailed as exc:
            return JSONResponse(
                {"status": "failed", "detail": str(exc)}, status_code=400
            )
        return JSONResponse({"status": "linked", "credential": token})

    @app.get("/link")
    async def link_view(request: Request, code: str | None = None) -> Response:
        if viewer(request) is None:
            target = "/link?code=" + code if code else "/link"
            return RedirectResponse(
                "/login?next=" + urllib.parse.quote(target, safe=""), status_code=303
            )
        if not code:
            return HTMLResponse(pages.link_prompt_page())
        pending = await asyncio.to_thread(auth.pending_link, database, code)
        if pending is None:
            return HTMLResponse(
                pages.link_page(
                    code=code,
                    agent_label=None,
                    error="That code is unknown or has expired.",
                ),
                status_code=404,
            )
        return HTMLResponse(
            pages.link_page(code=str(pending["code"]), agent_label=pending["label"])
        )

    @app.post("/link/approve")
    async def link_approve(request: Request) -> Response:
        # The body is parsed by hand rather than with `Form(...)`, which would
        # pull in python-multipart for a single urlencoded field. The approval
        # page posts one value and nothing else.
        user = require_user(request)
        raw = (await request.body()).decode("utf-8", "replace")
        fields = urllib.parse.parse_qs(raw)
        code = (fields.get("code") or [""])[0]
        ok = await asyncio.to_thread(
            auth.approve_link, database, code=code, user_id=user.id
        )
        if not ok:
            return HTMLResponse(
                pages.link_page(
                    code=code,
                    agent_label=None,
                    error="That code is unknown, expired, or already approved.",
                ),
                status_code=400,
            )
        return HTMLResponse(pages.link_page(code=code, agent_label=None, done=True))

    # -- the agent build -------------------------------------------------

    @app.get("/download")
    async def download_view(request: Request) -> Response:
        if viewer(request) is None:
            return RedirectResponse("/login?next=%2Fdownload", status_code=303)
        return HTMLResponse(
            pages.download_page(
                available=Path(settings.agent_exe).is_file(),
                releases_url=settings.releases_url,
            )
        )

    @app.get("/download/profitdog.exe")
    async def download_agent(request: Request) -> Response:
        """The generic agent. The same bytes for everyone.

        Behind the session because there is no reason to serve an executable to
        the anonymous internet, not because the build is a secret: it carries
        no credential and belongs to no account until someone approves it.
        """
        require_user(request)
        exe = Path(settings.agent_exe)
        if not exe.is_file():
            raise HTTPException(status_code=404, detail="no agent build on this server")
        return FileResponse(
            exe,
            media_type="application/vnd.microsoft.portable-executable",
            filename="profitdog.exe",
        )

    # -- the UI ----------------------------------------------------------

    dist = Path(settings.ui_dist)
    if dist.is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}")
        async def spa(path: str, request: Request) -> Any:
            """Serve the built UI, with client-side routes falling back to it.

            `/history` and `/analysis` are addresses the browser router owns,
            not files. Anything that is not a real file and not an API route is
            answered with `index.html` so a reload on one of them works.

            Signed out, that fallback becomes a redirect to `/login`. The
            bundle is still served to anyone who asks for it by name — it is
            public JavaScript and holds nothing — but landing on a profitdog
            address without a session takes you to the door rather than to an
            app that can only show you errors.
            """
            # An /api path that reached here matched no route -- a typo, or a
            # GET at a POST-only endpoint. It must not be answered with the
            # app shell: a client asking for JSON would get HTML, parse it,
            # fail somewhere unrelated, and report "could not reach the
            # server" about a server that answered immediately.
            if path.startswith("api/"):
                raise HTTPException(status_code=404, detail=f"no route /{path}")

            candidate = (dist / path).resolve()
            if path and candidate.is_file() and candidate.is_relative_to(dist.resolve()):
                return FileResponse(candidate)
            index = dist / "index.html"
            if not index.is_file():
                raise HTTPException(status_code=404, detail="UI not built")
            if viewer(request) is None:
                where = "/" + path if path else "/"
                if request.url.query:
                    where = where + "?" + request.url.query
                return RedirectResponse(
                    "/login?next=" + urllib.parse.quote(where, safe=""),
                    status_code=303,
                )
            return FileResponse(index)

    return app
