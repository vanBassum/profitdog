"""Delivery: outbox to server, with acknowledgements and offline replay.

## The whole protocol in four sentences

The agent sends a batch of facts in ascending sequence order. The server stores
them and answers with the highest sequence it holds with no gap behind it. The
agent deletes everything at or below that number. Anything else — a dropped
connection, a server restart, a laptop lid closing mid-match — resolves itself
on the next attempt, because nothing was deleted that was not acknowledged.

That gives at-least-once delivery on the wire and exactly-once effect in the
database, which is the pairing worth having: the network is allowed to be
unreliable, and the server's idempotency gate turns the resulting duplicates
into no-ops.

## Why it re-reads the server's cursor on failure

One case does not resolve itself by retrying blindly: the server stored a batch
and the *acknowledgement* was lost. The agent still holds facts the server
already has, and will keep resending them forever — harmlessly, but forever,
and behind them a queue that never drains.

So after a failure, and on startup, the agent asks the server where it is
(`GET /api/agent/cursor`) and acks its own outbox to that point. Restoring the
server from a backup is the same case in reverse: the cursor comes back
*lower* than the agent's, and because the agent only ever deletes up to an
acknowledged number, everything the backup lost is still in the outbox waiting
to be sent again.

## Back-off

Failures back off exponentially to a ceiling, because the common failure is "no
server yet" and hammering a closed port every two seconds for an evening is
neither useful nor quiet. Success resets it immediately: a flap should cost one
slow retry, not a slow evening.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from profitdog_protocol import Ack, FactBatch
from .outbox import Outbox

log = logging.getLogger("profitdog_agent.uplink")

#: Facts per request. Large enough that an hour of backlog drains in a few
#: round trips, small enough that one failed request is cheap to repeat.
BATCH_SIZE = 500

MIN_BACKOFF_SEC = 1.0
MAX_BACKOFF_SEC = 60.0


@dataclass
class UplinkStatus:
    connected: bool = False
    last_error: str | None = None
    last_success_at: float | None = None
    acked_through: int = 0
    pending: int = 0


class Uplink:
    """Ships the outbox to a server, and keeps trying until it lands."""

    def __init__(
        self,
        outbox: Outbox,
        base_url: str,
        *,
        timeout: float = 15.0,
        label: str | None = None,
        credential: str | None = None,
    ) -> None:
        self.outbox = outbox
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.label = label
        #: What this PC uploads with. Sent on every call; the server decides
        #: whose account the facts land in from this and never from the agent
        #: id in the batch, so a wrong one is refused rather than misfiled.
        self.credential = credential
        self.status = UplinkStatus(acked_through=outbox.acked_through)
        self._backoff = MIN_BACKOFF_SEC

    # -- transport -------------------------------------------------------

    def _headers(self, extra: dict | None = None) -> dict:
        headers = dict(extra or {})
        if self.credential:
            headers["authorization"] = "Bearer " + self.credential
        return headers

    def _post(self, path: str, payload: dict) -> dict:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers=self._headers({"content-type": "application/json"}),
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def _get(self, path: str) -> dict:
        request = urllib.request.Request(
            f"{self.base_url}{path}", headers=self._headers(), method="GET"
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    # -- operations ------------------------------------------------------

    def sync_cursor(self) -> int:
        """Ask the server where it is, and ack the outbox to match.

        Covers the two cases a blind retry cannot: a lost acknowledgement (the
        cursor is ahead of ours, so we can forget more) and a restored backup
        (the cursor is behind, so we must resend — which happens by itself,
        because those facts were never deleted).
        """
        # No agent id in the query any more: the credential says which agent
        # this is, so there is nothing here that could name somebody else.
        payload = self._get("/api/agent/cursor")
        cursor = int(payload.get("acked_through", 0))
        if cursor > self.outbox.acked_through:
            dropped = self.outbox.ack(cursor)
            if dropped:
                log.info(
                    "server was ahead of us: acked through %d, dropped %d facts",
                    cursor,
                    dropped,
                )
        elif cursor < self.outbox.acked_through:
            log.warning(
                "server cursor %d is behind ours %d — it has lost facts, and we "
                "will resend everything we still hold",
                cursor,
                self.outbox.acked_through,
            )
        self.status.acked_through = cursor
        return cursor

    def flush_once(self) -> int:
        """Send one batch. Returns how many facts were acknowledged."""
        facts = self.outbox.pending(BATCH_SIZE)
        if not facts:
            self.status.pending = 0
            return 0

        batch = FactBatch(
            agent_id=self.outbox.agent_id,
            boot_id=self.outbox.boot_id,
            facts=facts,
            label=self.label,
        )
        ack = Ack.from_json(self._post("/api/agent/facts", batch.to_json()))
        dropped = self.outbox.ack(ack.acked_through)
        self.status.acked_through = ack.acked_through
        self.status.pending = self.outbox.depth()
        log.debug(
            "sent %d facts, %d accepted, %d duplicates, acked through %d",
            len(facts),
            ack.accepted,
            ack.duplicates,
            ack.acked_through,
        )
        return dropped

    def drain(self) -> int:
        """Send until the outbox is empty or a batch fails. Returns facts sent."""
        total = 0
        while True:
            sent = self.flush_once()
            total += sent
            if sent == 0 or self.outbox.depth() == 0:
                return total

    # -- the loop --------------------------------------------------------

    def run(self, stop: threading.Event, interval: float = 2.0) -> None:
        """Ship facts until asked to stop. Never raises."""
        try:
            self.sync_cursor()
            self.status.connected = True
        except Exception as exc:  # noqa: BLE001 - the server may simply not be up
            self.status.last_error = str(exc)
            log.info("no server yet (%s); facts will queue until there is one", exc)

        while not stop.is_set():
            try:
                self.drain()
                self.status.connected = True
                self.status.last_error = None
                self.status.last_success_at = time.time()
                self._backoff = MIN_BACKOFF_SEC
                wait = interval
            except (urllib.error.URLError, OSError, ValueError) as exc:
                self.status.connected = False
                self.status.last_error = str(exc)
                self.status.pending = self.outbox.depth()
                wait = self._backoff
                self._backoff = min(self._backoff * 2, MAX_BACKOFF_SEC)
                log.warning(
                    "delivery failed (%s); %d facts held, retrying in %.0fs",
                    exc,
                    self.status.pending,
                    wait,
                )
                # A failure may mean a lost ack rather than a lost batch, so the
                # next attempt starts by asking where the server actually is.
                try:
                    self.sync_cursor()
                except Exception:  # noqa: BLE001 - it is still down; keep waiting
                    pass
            if stop.wait(wait):
                break
