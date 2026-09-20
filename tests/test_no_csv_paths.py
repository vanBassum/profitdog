"""No runtime path may read or write a session CSV.

## Why this is a test and not a note in a README

The CSV era ended by deleting four programs and rewriting a fifth. What makes a
migration like that come undone is not the big pieces — those are obvious — but
a helper somewhere that still globs `session_*.csv` "just as a fallback", left
in because it was harmless at the time. A fallback that nothing exercises is a
path nobody maintains, and it is the one that will be running the day the
database is missing and someone wonders why the numbers look old.

So the absence is asserted. The one place CSVs are still allowed to appear is
the importer, which exists to read them exactly once, and the test fixtures it
is tested against.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: The importer reads CSVs by definition. Its tests do too. The UI's exporter
#: *writes* a download from typed API rows and never reads one back.
ALLOWED = {
    "profitdog/server/importer.py",
    # The schema explains, in a comment, why one column is nullable: the
    # CSV-era XP log recorded gains and never totals. Naming the file it is
    # describing is documentation, not a code path.
    "profitdog/server/db/schema.py",
    "tests/test_no_csv_paths.py",
    "tests/test_domain_parity.py",
    "tests/test_cache.py",
    "tests/test_rulesets.py",
}

PY_ROOTS = ["profitdog", "tools"]
TS_ROOT = REPO / "profitdog-ui" / "src"


def source_files(root: Path, suffixes: tuple[str, ...]) -> list[Path]:
    return [
        p
        for p in root.rglob("*")
        if p.suffix in suffixes and "node_modules" not in p.parts and "dist" not in p.parts
    ]


def relative(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def test_no_runtime_python_reads_session_csvs():
    offenders = []
    for root in PY_ROOTS:
        for path in source_files(REPO / root, (".py",)):
            if relative(path) in ALLOWED:
                continue
            text = path.read_text(encoding="utf-8")
            if re.search(r"session_\*?\.csv|session_\*|role_xp_log", text):
                offenders.append(relative(path))
    assert offenders == [], f"CSV discovery survives in: {offenders}"


def test_no_runtime_python_imports_the_csv_modules():
    """`tracker`, `plot_graph` and `app` are gone; nothing may import them."""
    gone = {"tracker", "plot_graph", "app"}
    offenders = []
    for root in PY_ROOTS:
        for path in source_files(REPO / root, (".py",)):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = {a.name.split(".")[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom):
                    # Relative imports name modules inside this package — the
                    # server's own `api/app.py` is not the deleted `app.py`.
                    if node.level:
                        continue
                    names = {(node.module or "").split(".")[0]}
                else:
                    continue
                if names & gone:
                    offenders.append(f"{relative(path)} imports {names & gone}")
    assert offenders == [], offenders


def test_the_deleted_programs_are_actually_deleted():
    for name in ("app.py", "tracker.py", "plot_graph.py"):
        assert not (REPO / name).exists(), f"{name} is still here"
    assert not (REPO / "profitdog-ui" / "server.mjs").exists(), (
        "server.mjs still exists — it served CSVs and spawned the tracker"
    )


def test_the_browser_no_longer_parses_csv_or_rebuilds_the_domain():
    """The UI consumes typed responses; it does not re-derive anything.

    Each of these modules was a piece of the ledger running in the browser. The
    accounting now has one implementation, on the server, and this is what
    keeps it that way — a second copy would not fail, it would simply drift.
    """
    gone = [
        "sessions.ts",   # CSV parsing
        "ledger.ts",     # the decomposition
        "kit.ts",        # kit detection
        "annotations.ts",  # break-even
        "correlation.ts",  # Pearson's r
        "bankroll.ts",
        "sample-data.ts",  # invented matches
    ]
    for name in gone:
        assert not (TS_ROOT / "lib" / name).exists(), f"src/lib/{name} is still here"


def test_no_typescript_parses_a_session_csv():
    offenders = []
    for path in source_files(TS_ROOT, (".ts", ".tsx")):
        text = path.read_text(encoding="utf-8")
        if re.search(r"parseSessionCsv|session_\*|/api/sessions", text):
            offenders.append(relative(path))
    assert offenders == [], f"CSV parsing survives in the browser: {offenders}"


def test_the_ui_talks_to_the_new_api_only():
    """Every endpoint the browser calls is one the server actually serves."""
    from profitdog.server.api import create_app
    from profitdog.server.config import Settings
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    app = create_app(
        settings=Settings(
            database=tmp / "t.sqlite3",
            ui_dist=tmp / "none",
            host="127.0.0.1",
            port=0,
            collector=False,
        )
    )
    served = {getattr(route, "path", "") for route in app.routes}

    api_source = (TS_ROOT / "lib" / "api.ts").read_text(encoding="utf-8")
    called = set(re.findall(r'"(/api/[a-z/-]+)', api_source))
    assert called, "the api client calls nothing"

    for path in called:
        # Parameterised routes are served with placeholders, so a prefix match
        # is the honest comparison.
        assert any(
            route == path or route.startswith(path + "/") for route in served
        ), f"{path} is called by the UI but not served"
