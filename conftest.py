"""Make the three apps importable without installing anything.

The repository is a monorepo of three deliverables that do not depend on one
another -- `agent/`, `server/`, `ui/` -- plus `protocol/`, the wire contract
the two Python halves both speak. Each Python source root goes on `sys.path`
here, so `pytest` from the repository root imports them the same way their
deployed forms do: `import profitdog_agent`, never `import agent.profitdog_agent`.

This is deliberately a path shim and not a packaging step. Installing three
distributions to run the tests would mean an editable install per app and a
reinstall whenever a module moves; a monorepo whose tests need a build step is
a monorepo people stop running the tests in.

The deployed forms need no shim: the server image copies `server/` and
`protocol/` onto its path, and the frozen agent is built with `agent/` and
`protocol/` in the spec's `pathex`.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

for source_root in ("protocol", "server", "agent"):
    path = str(ROOT / source_root)
    if path not in sys.path:
        sys.path.insert(0, path)
