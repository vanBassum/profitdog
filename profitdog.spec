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

a = Analysis(
    ['profitdog/agent/__main__.py'],
    pathex=['.'],
    binaries=[
        # ctypes.WinDLL loads this dynamically (agent/adapters/richpresence.py)
        # — PyInstaller's static analysis cannot see that the way it sees a
        # normal `import`, so it has to be listed explicitly or the frozen exe
        # will not have it.
        (r'A:\python\lib\site-packages\steamworkspy\steam_api64.dll', '.'),
    ],
    datas=[],
    hiddenimports=['profitdog.agent.collector', 'profitdog.agent.uplink'],
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
        'fastapi', 'uvicorn', 'starlette', 'pydantic', 'profitdog.server',
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
