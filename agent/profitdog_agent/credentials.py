"""Where the agent keeps the credential it was given.

The credential is the only secret on a gaming PC, and it is a bearer token: a
copy of the file is as good as the original. So on Windows it is sealed with
DPAPI before it touches the disk.

`CryptProtectData` with `CRYPTPROTECT_LOCAL_MACHINE` unset encrypts against the
*user's* login secret, not the machine's. The practical difference is the one
that matters here: the file is unreadable to another account on the same PC,
and unreadable on any other PC even with the whole profile copied across. No
key is stored anywhere for us to lose, and nothing has to be typed at startup,
which is what makes it suitable for something that runs when the machine boots.

Off Windows there is no DPAPI, so the token is written in the clear with the
file mode narrowed to the owner, and `protected` says so rather than implying a
protection that is not there. That path exists to keep the tests and any
non-Windows development honest, not as a recommendation.

The store also remembers which server issued the credential. A token from one
server means nothing to another, and silently sending it to the wrong one would
be a leak rather than an error.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("profitdog_agent.credentials")

#: Version the on-disk shape so a future change can recognise an old file
#: instead of guessing at it.
FORMAT = 1


class CredentialError(RuntimeError):
    """The stored credential could not be read or written."""


# ---------------------------------------------------------------------------
# DPAPI
# ---------------------------------------------------------------------------


def _dpapi_available() -> bool:
    return sys.platform == "win32"


def _dpapi(seal: bool, payload: bytes) -> bytes:
    """One call into CryptProtectData / CryptUnprotectData."""
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    source = BLOB(len(payload), ctypes.cast(
        ctypes.create_string_buffer(payload, len(payload)),
        ctypes.POINTER(ctypes.c_char),
    ))
    result = BLOB()

    if seal:
        fn = crypt32.CryptProtectData
        args = (ctypes.byref(source), "profitdog agent credential",
                None, None, None, 0, ctypes.byref(result))
    else:
        fn = crypt32.CryptUnprotectData
        args = (ctypes.byref(source), None, None, None, None, 0,
                ctypes.byref(result))

    if not fn(*args):
        raise CredentialError(
            "Windows refused to "
            + ("protect" if seal else "read")
            + " the credential (error %d)" % ctypes.get_last_error()
        )
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        # The API allocates the output buffer; not freeing it leaks for the
        # life of the process, which for an agent is the life of the machine.
        kernel32.LocalFree(result.pbData)


def seal(token: str) -> tuple[str, bool]:
    """Returns the bytes to store (base64) and whether they are protected."""
    import base64

    if not _dpapi_available():
        return base64.b64encode(token.encode("utf-8")).decode("ascii"), False
    sealed = _dpapi(True, token.encode("utf-8"))
    return base64.b64encode(sealed).decode("ascii"), True


def unseal(blob: str, protected: bool) -> str:
    import base64

    raw = base64.b64decode(blob.encode("ascii"))
    if not protected:
        return raw.decode("utf-8")
    if not _dpapi_available():
        raise CredentialError(
            "this credential was protected on Windows and cannot be read here"
        )
    return _dpapi(False, raw).decode("utf-8")


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Credential:
    token: str
    server: str
    agent_id: str
    protected: bool


class CredentialStore:
    """The credential file, read and written as a whole."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self, *, server: str | None = None) -> Credential | None:
        """The stored credential, or None if there is none to use.

        A file for a different server is treated as absent: the token would be
        rejected there anyway, and sending it would hand a secret to a host
        that was never meant to have it.
        """
        if not self.path.is_file():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CredentialError("credential file is unreadable: %s" % exc) from exc

        if int(data.get("format") or 0) != FORMAT:
            log.warning("ignoring credential file in an unknown format")
            return None
        stored_server = str(data.get("server") or "")
        if server is not None and stored_server != server.rstrip("/"):
            log.info(
                "stored credential is for %s, not %s - linking again",
                stored_server or "an unknown server", server,
            )
            return None
        token = unseal(str(data["token"]), bool(data.get("protected")))
        return Credential(
            token=token,
            server=stored_server,
            agent_id=str(data.get("agent_id") or ""),
            protected=bool(data.get("protected")),
        )

    def save(self, *, token: str, server: str, agent_id: str) -> Credential:
        blob, protected = seal(token)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "format": FORMAT,
            "server": server.rstrip("/"),
            "agent_id": agent_id,
            "protected": protected,
            "token": blob,
        }
        # Written to a neighbouring file and moved into place, so an
        # interrupted write cannot leave a half-file where a credential was.
        temporary = self.path.with_suffix(self.path.suffix + ".new")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self._narrow(temporary)
        os.replace(temporary, self.path)
        if not protected:
            log.warning(
                "credential stored unprotected at %s (no DPAPI on this platform)",
                self.path,
            )
        return Credential(
            token=token, server=server.rstrip("/"), agent_id=agent_id,
            protected=protected,
        )

    def clear(self) -> None:
        """Forget the credential. The next run links again."""
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def _narrow(path: Path) -> None:
        """Owner-only, where the platform expresses that in a mode."""
        if sys.platform == "win32":
            # Windows permissions are ACLs, not modes, and the file already
            # inherits the profile directory's. DPAPI is what actually
            # protects it; chmod here would be theatre.
            return
        try:
            os.chmod(path, 0o600)
        except OSError:  # pragma: no cover - best effort
            log.debug("could not narrow permissions on %s", path)


__all__ = ["Credential", "CredentialError", "CredentialStore", "seal", "unseal"]
