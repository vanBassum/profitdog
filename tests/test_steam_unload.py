"""The AppID has to be given back, and giving it back means unloading the DLL.

Initialising Steamworks with the game's AppID registers this process with the
Steam client as a running copy of Wardogs. `SteamAPI_Shutdown()` ends the
session but leaves the library mapped, and ctypes never calls `FreeLibrary` on
its own — so the process kept an open connection to the Steam client, the game
would not stop properly, and Steam's Play button stayed "Stop".

These tests need no Steam: the leak was in loading and unloading the library,
which can be exercised directly.
"""

from __future__ import annotations

import ctypes
import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="steam_api64.dll is a Windows DLL"
)


def _mapped(name: str = "steam_api64.dll") -> bool:
    """Is the named module mapped into this process right now?"""
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    return bool(kernel32.GetModuleHandleW(name))


@pytest.fixture()
def dll_path():
    from profitdog_agent.adapters import richpresence

    try:
        return richpresence._find_dll()
    except FileNotFoundError:
        pytest.skip("steam_api64.dll not available on this machine")


def test_unload_takes_the_library_back_out(dll_path):
    from profitdog_agent.adapters import richpresence

    if _mapped():
        pytest.skip("already mapped by something else in this process")

    dll = ctypes.WinDLL(dll_path)
    assert _mapped(), "precondition: loading it should map it"

    richpresence._unload(dll)
    assert not _mapped(), (
        "steam_api64.dll stayed mapped after _unload - Steam will go on "
        "counting this process as a running copy of the game"
    )


def test_load_and_unload_stay_balanced_across_sessions(dll_path):
    """Each init() is its own LoadLibrary, so each shutdown must free one.

    An unbalanced pair would not show up on the first game of a sitting; it
    would show up on the second, when a reference from the first was still
    holding the module in place.
    """
    from profitdog_agent.adapters import richpresence

    if _mapped():
        pytest.skip("already mapped by something else in this process")

    for _ in range(3):
        dll = ctypes.WinDLL(dll_path)
        assert _mapped()
        richpresence._unload(dll)
        assert not _mapped(), "a previous session's reference outlived its shutdown"


def test_unload_ignores_anything_that_is_not_a_loaded_library():
    """A stray handle must never reach FreeLibrary.

    `FreeLibrary` on a handle that did not come from a load is an access
    violation — the process dies, and no `except` in Python sees it. So the
    guard is a type check before the call, and this pins it: passing an object
    that merely *looks* like a library must be ignored, not freed.

    It matters because `shutdown()` runs from a `finally`, from `__exit__` and
    from an `atexit` hook, which are exactly the paths reached while things are
    already going wrong.
    """
    from profitdog_agent.adapters import richpresence

    class NotADll:
        _handle = 0xDEAD

    richpresence._unload(NotADll())  # must not crash the interpreter
    richpresence._unload(None)
    richpresence._unload(object())


def test_shutdown_is_idempotent(dll_path):
    """It is called from several places and any of them may arrive first."""
    from profitdog_agent.adapters import richpresence

    rp = richpresence.RichPresence(dll_path=dll_path)
    rp.shutdown()
    rp.shutdown()
    assert not rp.active
