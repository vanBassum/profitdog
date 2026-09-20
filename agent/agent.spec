# -*- mode: python ; coding: utf-8 -*-
#
# Builds `dist/profitdog.exe` — the agent, frozen for the gaming PC.
#
# It used to build the Tkinter desktop app, which read `session_*.csv` off disk
# and drew them. That app is gone with the CSVs; what is left that genuinely
# wants to be a double-clickable exe is the agent, because it is the only piece
# that has to run on the machine Wardogs is installed on. The server is a
# long-running service and the UI is a web page it serves, and neither is
# improved by being frozen.
#
# Console, not windowed: the agent's whole output is "how many facts are
# waiting and is the server reachable", which is worth being able to see. The
# old app was windowed because it *was* the window.

import os


def _steam_dll():
    """Where steam_api64.dll is on the machine doing the build.

    This used to be one hardcoded absolute path into a particular developer's
    A: drive, which meant the build only ever worked on that one machine. The
    order here matches what agent/adapters/richpresence.py searches at runtime,
    so a checkout that runs also builds.
    """
    candidates = []
    override = os.environ.get('PROFITDOG_STEAM_DLL')
    if override:
        candidates.append(override)
    # Checked into the repository root, next to this spec.
    candidates.append(os.path.join(SPECPATH, 'steam_api64.dll'))
    try:
        import steamworkspy
        candidates.append(
            os.path.join(os.path.dirname(steamworkspy.__file__), 'steam_api64.dll')
        )
    except ImportError:
        pass
    for path in candidates:
        if os.path.exists(path):
            return path
    # Better to stop here than to produce an exe that dies on the gaming PC
    # with a missing-DLL error, far from whoever could fix the build.
    raise SystemExit(
        'steam_api64.dll not found for the frozen build. Place it in the '
        'repository root, set PROFITDOG_STEAM_DLL, or `pip install '
        'steamworkspy`. Looked in: ' + ', '.join(candidates)
    )


a = Analysis(
    # Not agent/__main__.py directly: PyInstaller runs the entry script as a
    # bare `__main__`, where that module's relative imports have no parent
    # package and fail at startup. entry.py imports it absolutely.
    ['entry.py'],
    # The agent's own source, plus the wire contract it shares with the
    # server. Nothing from server/ is on this path, which is what makes the
    # exclusion of the domain below a belt to this brace.
    pathex=[SPECPATH, os.path.join(SPECPATH, '..', 'protocol')],
    binaries=[
        # ctypes.WinDLL loads this dynamically (agent/adapters/richpresence.py)
        # — PyInstaller's static analysis cannot see that the way it sees a
        # normal `import`, so it has to be listed explicitly or the frozen exe
        # will not have it.
        (_steam_dll(), '.'),
    ],
    datas=[],
    hiddenimports=['profitdog_agent.collector', 'profitdog_agent.uplink'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # The agent is stdlib plus msgpack. Everything here is something a machine
    # with a data-science install lying around would otherwise get pulled in,
    # turning a ~15 MB exe into 150+ MB of libraries it never calls.
    excludes=[
        'torch', 'torchvision', 'torchaudio', 'scipy', 'sympy', 'IPython',
        'jupyter', 'notebook', 'jupyter_client', 'jupyter_core', 'ipykernel',
        'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'tensorflow', 'sklearn',
        'numba', 'llvmlite', 'transformers', 'nltk', 'spacy', 'tornado',
        'zmq', 'pytest', 'matplotlib', 'pandas', 'numpy',
        # The server half has no business in an agent build: it would ship the
        # domain rules to a machine that must never apply them.
        'fastapi', 'uvicorn', 'starlette', 'pydantic', 'psycopg', 'profitdog_server',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='profitdog',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
