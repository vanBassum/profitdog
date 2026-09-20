"""Claiming a PC for an account, from the PC's side.

The EXE is generic: the same bytes are handed to everyone, and nothing in it
says whose machine it is on. This is how it finds out.

    1. It asks the server for a linking code.
    2. It prints the code and opens the server in the default browser.
    3. Somebody signs in with Google there and approves the code.
    4. It polls until the server hands back a credential, and stores it.

Nothing from Google reaches this file. The browser talks to Google, the server
talks to Google, and this process only ever sees a code it asked for and a
credential it was given -- neither of which Google has heard of. That is the
point of doing it this way rather than opening an OAuth flow in the agent: a
gaming PC never holds a Google token, so a compromised one cannot be traded for
anything beyond uploading matches to the account that approved it.

The wait is deliberately patient and deliberately bounded. Someone may take a
minute to find their password; nobody takes ten, and a code that outlived its
usefulness should stop working rather than linger.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
import webbrowser

from .credentials import Credential, CredentialStore

log = logging.getLogger("profitdog_agent.linking")

#: Ceiling on the whole wait, whatever the server suggests. The server's codes
#: expire on their own; this is so a headless run cannot poll forever if one
#: somehow does not.
MAX_WAIT_SEC = 900.0


class LinkingError(RuntimeError):
    """Linking could not be completed."""


def _post(base_url: str, path: str, payload: dict, timeout: float = 15.0):
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {"detail": body[:200]}


def _announce(code: str, url: str) -> None:
    """Put the code where somebody looking at this PC will see it."""
    line = "=" * 52
    print("\n" + line)
    print("  Link this PC to your profitdog account")
    print(line)
    print("\n  Your code:   " + code)
    print("  Approve at:  " + url)
    print("\n  Waiting for approval...\n")


def link(
    base_url: str,
    *,
    agent_id: str,
    label: str | None,
    store: CredentialStore,
    open_browser: bool = True,
    sleep=time.sleep,
) -> Credential:
    """Run the handshake through to a stored credential.

    Blocks until somebody approves, the code expires, or the server refuses.
    """
    status, body = _post(
        base_url, "/api/agent/link/start", {"agent_id": agent_id, "label": label}
    )
    if status != 200:
        raise LinkingError(
            "the server would not start linking: %s"
            % (body.get("detail") or status)
        )

    code = str(body["code"])
    secret = str(body["device_secret"])
    approve_url = str(body.get("verification_url_complete") or body["verification_url"])
    interval = float(body.get("interval") or 2.0)
    deadline = time.monotonic() + min(float(body.get("expires_in") or 600), MAX_WAIT_SEC)

    _announce(code, approve_url)
    if open_browser:
        try:
            webbrowser.open(approve_url)
        except Exception:  # pragma: no cover - a headless box has no browser
            log.debug("could not open a browser; the URL is printed above")

    while time.monotonic() < deadline:
        status, body = _post(
            base_url,
            "/api/agent/link/poll",
            {"code": code, "device_secret": secret},
        )
        if status == 200 and body.get("status") == "linked":
            credential = store.save(
                token=str(body["credential"]), server=base_url, agent_id=agent_id
            )
            print("  This PC is linked. Tracking starts now.\n")
            return credential
        if status == 202 or body.get("status") == "pending":
            sleep(interval)
            continue
        raise LinkingError(str(body.get("detail") or "linking was refused"))

    raise LinkingError(
        "nobody approved this PC in time. Start the agent again for a new code."
    )


def ensure_credential(
    base_url: str,
    *,
    agent_id: str,
    label: str | None,
    store: CredentialStore,
    open_browser: bool = True,
) -> Credential:
    """The credential for this server, linking first if there is none.

    The stored one is used as-is without being checked against the server. A
    revoked credential is discovered on the first upload, which is where the
    uplink's retry loop already handles being turned away -- asking in advance
    would be one more thing to be wrong about while the server is unreachable.
    """
    existing = store.load(server=base_url)
    if existing is not None:
        return existing
    return link(
        base_url,
        agent_id=agent_id,
        label=label,
        store=store,
        open_browser=open_browser,
    )


__all__ = ["LinkingError", "ensure_credential", "link"]
