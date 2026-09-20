"""Frozen entry point for `dist/profitdog.exe`.

PyInstaller runs its entry script as the top-level `__main__`, with no package
around it — so `profitdog/agent/__main__.py` cannot be frozen directly: its
`from .collector import ...` has no parent package to be relative to, and the
exe dies on startup with "attempted relative import with no known parent
package".

This shim imports the package the ordinary absolute way and calls into it, so
the frozen build and the documented `python -m profitdog_agent` run the very
same code.
"""

from __future__ import annotations

import multiprocessing
import sys

from profitdog_agent.__main__ import main

if __name__ == "__main__":
    # The agent itself is threads, not processes, but PyInstaller's docs call
    # for this in any frozen Windows entry point: without it, anything that
    # does spawn a child re-runs this script instead of the child's target.
    multiprocessing.freeze_support()
    sys.exit(main())
