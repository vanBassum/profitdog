"""
richpresence.py — Read Wardogs' own Steam Rich Presence for match state and
profit/loss, as an alternative to OCR-based tracking in tracker.py.

Wardogs publishes its own state via Steamworks Rich Presence — the exact
mechanism Steam uses to show "+$150 Profit" next to a friend's name in your
friends list. Reading it for yourself needs the raw Steamworks "flat API" in
steam_api64.dll directly via ctypes: the `steamworkspy` package on PyPI is a
convenient source for that DLL, but its own Python wrapper doesn't expose
rich presence at all, so this talks to the DLL's exports directly instead.

No login, no credentials, no packet inspection — this only reads local
Steam client state for the currently logged-in user, the same category of
access the Steam friends-list UI itself uses.

Initializing against the game's AppID is what makes Steam treat this process
as a running copy of Wardogs, so the session is deliberately short-lived: hold
it only while the game is actually running (`game_running()`), and always
release it. The context manager is the way to be sure.

Usage:
    import richpresence

    if richpresence.game_running():
        with richpresence.RichPresence() as rp:
            if rp.started:
                data = rp.read()   # {'game_state': 'playing', ...}
"""

import atexit
import ctypes
import logging
import os
import re
import sys
from ctypes import wintypes

log = logging.getLogger("profitdog_agent.richpresence")

APP_ID = 1867240  # Wardogs, from steamapps/appmanifest_1867240.acf

# ---------------------------------------------------------------------------
# Is the game actually running?
# ---------------------------------------------------------------------------
#
# This matters far more than it looks. Initializing Steamworks with the game's
# AppID registers *this* process with the Steam client as a running instance of
# Wardogs — that is what steam_appid.txt is for, and it is the only way to read
# the game's Rich Presence, since Steam only shares a friend's rich presence
# with someone running the same app.
#
# The cost is that Steam then counts two instances of Wardogs: the game, and
# us. Quit the game and Steam still sees ours, so the app stays "Running", the
# Play button stays "Stop", and Steam asks whether you really want to close
# with a game still running. The tracker was holding that registration for its
# entire lifetime — from the moment the app opened until it was closed —
# whether or not anyone was playing.
#
# So the session is now scoped to the game process: claimed when the client
# appears, released the moment it goes. When Wardogs is not running, neither is
# our claim on its AppID.

TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        # ULONG_PTR: a pointer-sized field, so spell it as a pointer rather
        # than c_ulong, which is 32-bit even in a 64-bit process and would
        # misalign every field after it.
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


def _running_process_names():
    """Every running process's executable name, lowercased.

    A toolhelp snapshot rather than shelling out to `tasklist`: this is checked
    several times a second, and spawning a console process at that rate would
    cost more than everything else the tracker does put together — and would
    flash a console window on every check under a windowed build.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32)]
    kernel32.Process32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == _INVALID_HANDLE_VALUE:
        return None  # Distinct from "nothing matched" — see game_running().

    names = set()
    try:
        entry = _PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32)
        if kernel32.Process32First(snapshot, ctypes.byref(entry)):
            while True:
                names.add(entry.szExeFile.decode(errors="replace").lower())
                if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(snapshot)
    return names


# Matched by shape rather than pinned to one filename: Unreal names shipping
# binaries `<Target>-<Platform>-Shipping.exe`, and the target has been renamed
# across builds before. The launcher is excluded deliberately — it is the
# process Steam starts, but it is not the one that publishes Rich Presence, and
# it can outlive the client. Holding the AppID for a lingering launcher would
# reintroduce exactly the delay this is here to remove.
_LAUNCHER_EXE = "wardogslauncher-shipping.exe"


def game_running():
    """True when the Wardogs *client* is running.

    Returns True if the process list can't be read at all, which only happens
    when the snapshot call fails. Failing open is the safer default: it keeps
    the tracker working on a machine where the check is unavailable, at the
    cost of the old behaviour, rather than silently never tracking anything.
    """
    names = _running_process_names()
    if names is None:
        return True
    return any(
        name.startswith("wardogs")
        and name.endswith("-shipping.exe")
        and name != _LAUNCHER_EXE
        for name in names
    )

# steamworkspy ships the raw steam_api64.dll as a redistributable runtime
# component (same DLL every Steam game ships with its own build) — reused
# here rather than requiring a separate download.
_DLL_CANDIDATES = []
if getattr(sys, "frozen", False):
    # Bundled by profitdog.spec's `binaries` entry, extracted alongside the
    # frozen app — ctypes.WinDLL can't be auto-detected by PyInstaller's
    # static analysis the way a normal `import` can, so it has to be listed
    # there explicitly.
    _DLL_CANDIDATES.append(os.path.join(sys._MEIPASS, "steam_api64.dll"))
# An explicit override wins over every guess below, for a machine that keeps
# the DLL somewhere of its own.
_env_dll = os.environ.get("PROFITDOG_STEAM_DLL")
if _env_dll:
    _DLL_CANDIDATES.append(_env_dll)

_DLL_CANDIDATES.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "steam_api64.dll"))

# The agent's own directory (<repo>/agent/), three levels up from this module
# at <repo>/agent/profitdog_agent/adapters/richpresence.py. The DLL is checked
# in there, beside the spec that freezes it, rather than next to this file --
# so searching only "next to this file" finds nothing.
#
# The repository root is tried too, which is where it used to live: a checkout
# that predates the move, or a copy someone dropped at the top, still works.
_agent_root = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
_DLL_CANDIDATES.append(os.path.join(_agent_root, "steam_api64.dll"))
_DLL_CANDIDATES.append(
    os.path.join(os.path.abspath(os.path.join(_agent_root, "..")), "steam_api64.dll")
)

# Where the process was started from, which is the repository root in the
# documented `python -m profitdog_agent` invocation.
_DLL_CANDIDATES.append(os.path.join(os.getcwd(), "steam_api64.dll"))

try:
    import steamworkspy
    _DLL_CANDIDATES.append(os.path.join(os.path.dirname(steamworkspy.__file__), "steam_api64.dll"))
except ImportError:
    pass


def _find_dll():
    for path in _DLL_CANDIDATES:
        if os.path.exists(path):
            return path
    raise FileNotFoundError(
        "steam_api64.dll not found. Place it in the repository root, set "
        "PROFITDOG_STEAM_DLL to its path, or `pip install steamworkspy`. "
        "Looked in: " + ", ".join(_DLL_CANDIDATES)
    )


def _unload(dll) -> None:
    """Take steam_api64.dll back out of this process.

    `SteamAPI_Shutdown()` ends the Steamworks session, but it does not unmap
    the library, and ctypes never calls `FreeLibrary` of its own accord — not
    when the `WinDLL` is dropped, not when it is garbage collected. So the DLL
    stayed loaded with its connection to the Steam client open, and Steam went
    on counting this process as a running copy of Wardogs: the game would not
    stop properly, and the Play button stayed "Stop".

    Every `init()` does its own `WinDLL(...)`, which is a `LoadLibrary` and a
    reference on the module, so exactly one `FreeLibrary` per shutdown keeps
    the count balanced across repeated game sessions.

    Only a real ctypes library is accepted. `FreeLibrary` on a handle that did
    not come from a load is an access violation — a process-level crash that no
    `except` can catch — so the guard has to be *before* the call rather than
    around it.
    """
    if not isinstance(dll, ctypes.CDLL):
        return
    handle = getattr(dll, "_handle", None)
    if not handle:
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.FreeLibrary.argtypes = [ctypes.c_void_p]
        kernel32.FreeLibrary.restype = ctypes.c_int
        if not kernel32.FreeLibrary(ctypes.c_void_p(handle)):
            log.warning(
                "could not unload steam_api64.dll (error %d) - Steam may still "
                "count this process as Wardogs",
                ctypes.get_last_error(),
            )
    except Exception:
        log.warning("could not unload steam_api64.dll", exc_info=True)


def parse_profit(raw):
    """'-$5,440' -> -5440, '+$150' -> 150, None/unparseable -> None."""
    if not raw:
        return None
    m = re.match(r"([+-]?)\$?([\d,]+)", raw.strip())
    if not m:
        return None
    sign = -1 if m.group(1) == "-" else 1
    return sign * int(m.group(2).replace(",", ""))


# Wardogs' Rich Presence reports your TEAM (not squad — confirmed live: a
# squad change left this untouched, only switching teams changed it) as an
# internal codename. Verified against the game's three actual factions:
# Lonestar (blue), Valkyra (red), Manticore (green).
FACTION_NAMES = {
    "alpha": "Lonestar",
    "bravo": "Valkyra",
    "charlie": "Manticore",
}


def faction_name(code):
    """Human-readable team name for a `faction` Rich Presence value, or the
    raw code itself if unrecognized (e.g. 'unknown' during matchmaking)."""
    return FACTION_NAMES.get(code, code)


def _pick_versioned(dll, prefix, versions):
    """First exported `<prefix><nnn>` accessor, newest version first.

    Steamworks names its interface accessors with the interface version baked
    into the symbol, so a hardcoded name silently stops resolving the moment a
    game ships a newer SDK than the one this was written against.
    """
    for version in versions:
        name = f"{prefix}{version:03d}"
        if hasattr(dll, name):
            return getattr(dll, name)
    raise AttributeError(
        f"No {prefix}* accessor found in {dll._name}. The Steamworks SDK that "
        "built this DLL is newer or older than anything this supports."
    )


class RichPresence:
    def __init__(self, app_id=APP_ID, dll_path=None):
        self.app_id = app_id
        self.dll_path = dll_path or _find_dll()
        self.dll = None
        self.friends = None
        self.user = None
        self.steam_id = None
        self.init_error = None
        self.started = False
        self._init_call = None

    def init(self):
        """Claim a Steamworks session for the game's AppID.

        Returns True on success, False if Steam isn't running or init fails.

        Every successful call must be paired with `shutdown()`, because until
        it is, Steam counts this process as a running copy of Wardogs. Prefer
        the context manager, or let the `atexit` hook below catch the paths
        that get there by surprise.
        """
        dll = ctypes.WinDLL(self.dll_path)

        # Steamworks 1.59 turned SteamAPI_Init into a header-inline helper and
        # left SteamAPI_InitFlat as the exported entry point, so which one is
        # available depends on which SDK built the DLL. Wardogs ships 1.61,
        # where only InitFlat exists; the steamworkspy DLL is older and only
        # has Init. Bind whichever is actually exported.
        if hasattr(dll, "SteamAPI_Init"):
            dll.SteamAPI_Init.restype = ctypes.c_bool
            self._init_call = lambda: bool(dll.SteamAPI_Init())
        else:
            # ESteamAPIInitResult SteamAPI_InitFlat(SteamErrMsg *pOutErrMsg)
            # returns 0 (k_ESteamAPIInitResult_OK) on success and fills a
            # 1024-byte buffer with the reason on failure.
            dll.SteamAPI_InitFlat.restype = ctypes.c_int
            dll.SteamAPI_InitFlat.argtypes = [ctypes.c_char_p]

            def _init_flat():
                err = ctypes.create_string_buffer(1024)
                result = dll.SteamAPI_InitFlat(err)
                if result != 0:
                    self.init_error = err.value.decode(errors="replace")
                return result == 0

            self._init_call = _init_flat

        # The interface accessors are versioned in their symbol name and get
        # bumped by SDK releases (Wardogs' 1.61 carries SteamUser v023, while
        # older DLLs export v021). Resolve them by probing rather than pinning
        # a version that only happens to match one SDK.
        friends_accessor = _pick_versioned(dll, "SteamAPI_SteamFriends_v", range(30, 14, -1))
        user_accessor = _pick_versioned(dll, "SteamAPI_SteamUser_v", range(30, 17, -1))
        friends_accessor.restype = ctypes.c_void_p
        user_accessor.restype = ctypes.c_void_p
        dll.SteamAPI_ISteamUser_GetSteamID.restype = ctypes.c_uint64
        dll.SteamAPI_ISteamUser_GetSteamID.argtypes = [ctypes.c_void_p]
        dll.SteamAPI_ISteamFriends_GetFriendRichPresenceKeyCount.restype = ctypes.c_int32
        dll.SteamAPI_ISteamFriends_GetFriendRichPresenceKeyCount.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        dll.SteamAPI_ISteamFriends_GetFriendRichPresenceKeyByIndex.restype = ctypes.c_char_p
        dll.SteamAPI_ISteamFriends_GetFriendRichPresenceKeyByIndex.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_int32]
        dll.SteamAPI_ISteamFriends_GetFriendRichPresence.restype = ctypes.c_char_p
        dll.SteamAPI_ISteamFriends_GetFriendRichPresence.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_char_p]
        dll.SteamAPI_ISteamFriends_RequestFriendRichPresence.restype = None
        dll.SteamAPI_ISteamFriends_RequestFriendRichPresence.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        dll.SteamAPI_RunCallbacks.restype = None
        dll.SteamAPI_Shutdown.restype = None

        # steam_appid.txt is how an unlaunched process tells SteamAPI_Init
        # which app it is. Steam reads it during that call and never again, so
        # it exists for exactly the length of the call and is then put back the
        # way it was found. Leaving it lying around is not harmless: any Steam
        # game later started with this directory as its working directory would
        # read it and claim to be Wardogs.
        with _appid_file(self.app_id):
            if not self._init_call():
                return False

        self.dll = dll
        self.friends = friends_accessor()
        self.user = user_accessor()
        self.steam_id = dll.SteamAPI_ISteamUser_GetSteamID(self.user)
        _live_sessions.add(self)
        return True

    def read(self):
        """Poll current rich presence. Returns {} while in a menu (Wardogs
        only sets keys once you're actually in a match)."""
        dll = self.dll
        dll.SteamAPI_RunCallbacks()
        dll.SteamAPI_ISteamFriends_RequestFriendRichPresence(self.friends, self.steam_id)
        dll.SteamAPI_RunCallbacks()

        count = dll.SteamAPI_ISteamFriends_GetFriendRichPresenceKeyCount(self.friends, self.steam_id)
        data = {}
        for i in range(count):
            key = dll.SteamAPI_ISteamFriends_GetFriendRichPresenceKeyByIndex(self.friends, self.steam_id, i)
            if key is None:
                continue
            val = dll.SteamAPI_ISteamFriends_GetFriendRichPresence(self.friends, self.steam_id, key)
            data[key.decode()] = val.decode() if val else None
        return data

    def shutdown(self):
        """Release the AppID back to Steam. Safe to call twice, or never.

        Idempotent on purpose: it is called from the tracker's `finally`, from
        `__exit__`, and from the `atexit` hook, and any of those can be the one
        that gets there first.
        """
        dll, self.dll = self.dll, None
        _live_sessions.discard(self)
        if dll is None:
            return
        self.friends = None
        self.user = None
        self.steam_id = None
        dll.SteamAPI_Shutdown()
        # Ending the session is not enough on its own: while the library is
        # still mapped, Steam keeps seeing a process registered against the
        # game's AppID.
        _unload(dll)

    @property
    def active(self):
        """Whether a Steamworks session is currently held."""
        return self.dll is not None

    def __enter__(self):
        self.started = self.init()
        return self

    def __exit__(self, *_exc):
        self.shutdown()
        return False


# Last line of defence. A tracker thread killed at interpreter exit, an
# unhandled exception, a GUI torn down without joining its worker — none of
# them run a `finally`, and every one of them used to leave Steam believing
# Wardogs was still running until it noticed the process was gone.
_live_sessions = set()


@atexit.register
def _release_live_sessions():
    for session in list(_live_sessions):
        try:
            session.shutdown()
        except Exception:
            pass


class _appid_file:
    """steam_appid.txt for the duration of SteamAPI_Init, then put back.

    Restores whatever was there before rather than simply deleting, so a
    directory that legitimately contains one — a game folder, say — is left
    exactly as it was found.
    """

    def __init__(self, app_id):
        self.app_id = app_id
        self.path = os.path.join(os.getcwd(), "steam_appid.txt")
        self.previous = None

    def __enter__(self):
        try:
            with open(self.path, "rb") as f:
                self.previous = f.read()
        except OSError:
            self.previous = None
        with open(self.path, "w") as f:
            f.write(str(self.app_id))
        return self

    def __exit__(self, *_exc):
        try:
            if self.previous is None:
                os.remove(self.path)
            else:
                with open(self.path, "wb") as f:
                    f.write(self.previous)
        except OSError:
            pass
        return False
