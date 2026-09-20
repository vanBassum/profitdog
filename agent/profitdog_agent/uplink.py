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

## When the cursor cannot move

There is one state the cursor alone cannot get out of. The server is waiting
for sequence numbers this agent no longer holds -- acknowledged by a server
that has since been replaced, and deleted here on the strength of that
acknowledgement -- so its cursor stays where it is no matter what arrives.
Every batch then comes back "all duplicates, acked through 0", the head of the
queue never clears, and because a batch is always the head, nothing behind it
is ever offered either. Play all evening and none of it is uploaded.

So a batch the server accounts for in full -- every fact either stored or
already held -- is treated as delivered, cursor or no cursor. That is not a
guess: the server has just said what it did with each one, and the only thing
keeping them here was a promise about *earlier* facts that nobody can keep.
The server's own gap-closing fix makes this rare; this makes an agent that
meets a server without that fix recover anyway, which matters because the
agent is an EXE on somebody's gaming PC and the server is not.

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

A request that succeeds and moves nothing backs off the same way. It is not a
contradiction: the server answered, accepted the batch as duplicates it
already had, and left its cursor where it was, so sending the same five
hundred facts again immediately achieves exactly as much. Treating "200 OK"
as progress is what turns a stuck cursor into a permanent upload loop, which
is how this was found.
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
        #: Whether the "acknowledges only 0" warning has been said already.
        #: Against a server whose cursor is stuck it is true of every batch,
        #: and a warning on every poll is a warning nobody reads.
        self._warned_unacked = False

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
        if dropped:
            # A cursor that moves is a cursor that works: if it ever stops
            # again, that is news worth hearing about once more.
            self._warned_unacked = False

        if not dropped:
            # The cursor did not move. It cannot, if the server is waiting for
            # sequence numbers below this batch -- numbers acknowledged by a
            # server that has since been replaced and deleted here on the
            # strength of that acknowledgement. Nobody has them; nobody ever
            # will.
            #
            # The cursor is not the only proof of delivery. The server also
            # reports what it did with each fact, and `accepted + duplicates`
            # covering the whole batch means it either stored or already had
            # every one of them. Combined with the batch being contiguous --
            # it is the head of a queue that only ever loses an acknowledged
            # prefix, so it has no holes -- there is nothing in this range the
            # server is missing, and keeping it is what stops everything
            # behind it from ever being offered.
            seqs = [fact.agent_seq for fact in facts]
            accounted = ack.accepted + ack.duplicates == len(facts)
            contiguous = seqs[-1] - seqs[0] + 1 == len(seqs)
            if accounted and contiguous:
                dropped = self.outbox.ack(seqs[-1])
                say = log.debug if self._warned_unacked else log.warning
                self._warned_unacked = True
                say(
                    "server holds all %d facts through %d but acknowledges "
                    "only %d; forgetting them anyway so the queue can move",
                    len(facts),
                    seqs[-1],
                    ack.acked_through,
                )
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
                moved = self.drain()
                self.status.connected = True
                self.status.last_success_at = time.time()
                held = self.outbox.depth()
                if moved == 0 and held:
                    # The server is up and taking the batch, but its cursor is
                    # not moving, so nothing can be deleted and the next
                    # attempt would send the identical facts. Back off and
                    # re-read the cursor rather than spin.
                    self.status.last_error = (
                        "server is not acknowledging: %d facts held" % held
                    )
                    wait = self._backoff
                    self._backoff = min(self._backoff * 2, MAX_BACKOFF_SEC)
                    log.warning(
                        "delivered a batch but the server acknowledged nothing "
                        "(still at %d); %d facts held, retrying in %.0fs",
                        self.status.acked_through,
                        held,
                        wait,
                    )
                    try:
                        self.sync_cursor()
                    except Exception:  # noqa: BLE001 - it answered a moment ago
                        pass
                else:
                    self.status.last_error = None
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
