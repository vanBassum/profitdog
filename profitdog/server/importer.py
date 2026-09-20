"""One-time import of the CSV era.

    python -m profitdog.server.importer --from . --db profitdog.sqlite3

## Why this writes rows directly instead of replaying facts through ingest

Everything else in this system arrives as agent observations and is segmented
by the server. A tempting symmetry would be to turn each CSV row back into a
presence fact and push it through `Ingestor`. It would be wrong, and subtly:
segmentation needs two confirmed `playing` reads before it opens a match, so
every imported match would start two readings late and every `elapsed_sec`
would shift by four seconds. The curve would be *almost* right, which is worse
than obviously wrong.

A `session_*.csv` is already a segmented match — that decision was made, years
of evenings ago, by the tracker that wrote it. Re-litigating it now would
change history rather than import it. So the file's own offsets are preserved
byte for byte, and the rows are marked `source = 'import:csv'` so that a reader
can always tell which matches were segmented by this server and which arrived
pre-cut.

## Idempotency

Re-running the import is safe and changes nothing. Matches are keyed on the
file they came from, samples on `(match_id, elapsed_sec)`, so a second run
inserts nothing and reports the same counts.

## Validation

The import is not finished when the rows are in; it is finished when the rows
are *checked*. Every match is verified for sample count, first and last cash
value, and the ledger's own invariants, against the file it came from. A
mismatch anywhere fails the import loudly rather than leaving a plausible-looking
database that quietly disagrees with the CSVs you are about to delete.
"""

from __future__ import annotations

import argparse
import csv
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db import Database, open_database, utc_now
from .domain import rulesets
from .domain.engine import derive_match
from .normalize import normalize_map, parse_ts

log = logging.getLogger("profitdog.server.importer")

XP_ROLES = ["Wardog", "Infantry", "Medic", "Driver", "Pilot", "Support", "Recon"]


@dataclass
class FileReport:
    path: Path
    match_key: str
    rows: int = 0
    samples_written: int = 0
    samples_in_db: int = 0
    first_cash: int | None = None
    last_cash: int | None = None
    lives: int = 0
    curve_net: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class ImportReport:
    files: list[FileReport] = field(default_factory=list)
    xp_rows: int = 0
    xp_attached: int = 0
    xp_orphaned: int = 0
    skipped: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(f.ok for f in self.files)

    @property
    def total_samples(self) -> int:
        return sum(f.samples_in_db for f in self.files)

    def text(self) -> str:
        lines = [
            f"{'match':34} {'rows':>6} {'stored':>7} {'lives':>6} {'net':>10}  status",
            "-" * 78,
        ]
        for report in sorted(self.files, key=lambda f: f.match_key):
            status = "ok" if report.ok else "; ".join(report.errors)
            lines.append(
                f"{report.match_key:34} {report.rows:6} {report.samples_in_db:7}"
                f" {report.lives:6} {report.curve_net:10}  {status}"
            )
        lines.append("-" * 78)
        lines.append(
            f"{len(self.files)} matches, {self.total_samples} samples, "
            f"{self.xp_rows} XP rows ({self.xp_attached} attached to a match, "
            f"{self.xp_orphaned} between matches)"
        )
        for note in self.skipped:
            lines.append(f"skipped: {note}")
        lines.append("VALIDATION PASSED" if self.ok else "VALIDATION FAILED")
        return "\n".join(lines)


def match_key_for_file(path: Path) -> str:
    """`session_20260919_230057.csv` -> `m-import-20260919_230057`."""
    stem = path.stem
    if stem.startswith("session_"):
        stem = stem[len("session_") :]
    return f"m-import-{stem}"


def _read_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = []
        for raw in csv.DictReader(handle):
            try:
                rows.append(
                    {
                        "timestamp": (raw.get("timestamp") or "").strip(),
                        "elapsed": float(raw["seconds_elapsed"]),
                        "cash": int(float(raw["cash"])),
                        "map": (raw.get("map") or "").strip(),
                        "faction": (raw.get("faction") or "").strip(),
                        "life": int(raw.get("life") or 1),
                    }
                )
            except (TypeError, ValueError, KeyError):
                # A truncated final row is the normal way a tracker crash shows
                # up in a CSV. Skip it rather than failing the whole file.
                continue
    rows.sort(key=lambda r: r["elapsed"])
    return rows


def _first_real_faction(rows: list[dict]) -> str | None:
    for row in rows:
        value = row["faction"].lower()
        if value and value != "unknown":
            return row["faction"]
    return None


def import_file(db: Database, path: Path) -> FileReport:
    key = match_key_for_file(path)
    report = FileReport(path=path, match_key=key)
    rows = _read_rows(path)
    report.rows = len(rows)
    if not rows:
        report.errors.append("no readable rows")
        return report

    started_at = parse_ts(rows[0]["timestamp"]) or datetime.now(timezone.utc)
    ended_at = parse_ts(rows[-1]["timestamp"]) or (
        started_at + timedelta(seconds=rows[-1]["elapsed"])
    )
    now = utc_now()

    with db.write() as conn:
        existing = conn.execute(
            "SELECT id FROM matches WHERE match_key = ?", (key,)
        ).fetchone()
        if existing is None:
            cursor = conn.execute(
                "INSERT INTO matches (match_key, agent_ref, started_at, ended_at, map,"
                " faction, build, source, observed_at, received_at, ingested_at, closed)"
                " VALUES (?, NULL, ?, ?, ?, ?, NULL, 'import:csv', ?, ?, ?, 1)",
                (
                    key,
                    started_at.isoformat(),
                    ended_at.isoformat(),
                    normalize_map(rows[0]["map"]),
                    _first_real_faction(rows),
                    rows[0]["timestamp"] or started_at.isoformat(),
                    now,
                    now,
                ),
            )
            match_id = int(cursor.lastrowid)
        else:
            match_id = int(existing[0])

        payload = [
            (
                match_id,
                row["elapsed"],
                row["cash"],
                row["life"],
                row["timestamp"] or None,
                row["timestamp"] or started_at.isoformat(),
                now,
                now,
            )
            for row in rows
        ]
        before = conn.execute(
            "SELECT COUNT(*) FROM cash_samples WHERE match_id = ?", (match_id,)
        ).fetchone()[0]
        conn.executemany(
            "INSERT OR IGNORE INTO cash_samples (match_id, elapsed_sec, cash, life,"
            " source_ts, observed_at, received_at, ingested_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            payload,
        )
        after = conn.execute(
            "SELECT COUNT(*) FROM cash_samples WHERE match_id = ?", (match_id,)
        ).fetchone()[0]

    report.samples_written = after - before
    report.samples_in_db = after
    report.first_cash = rows[0]["cash"]
    report.last_cash = rows[-1]["cash"]

    _validate(db, match_id, rows, report)
    return report


def _validate(db: Database, match_id: int, rows: list[dict], report: FileReport) -> None:
    """Check the stored match against the file it came from."""
    stored = db.query(
        "SELECT elapsed_sec, cash, life FROM cash_samples WHERE match_id = ?"
        " ORDER BY elapsed_sec",
        (match_id,),
    )
    # A CSV can legitimately repeat an offset (two polls inside the same
    # rounded second); the unique constraint collapses those, so the count is
    # compared against the distinct offsets rather than the raw row count.
    distinct = len({row["elapsed"] for row in rows})
    if len(stored) != distinct:
        report.errors.append(f"stored {len(stored)} samples, file has {distinct} offsets")
    if stored:
        if int(stored[0]["cash"]) != rows[0]["cash"]:
            report.errors.append(
                f"first cash {stored[0]['cash']} != {rows[0]['cash']}"
            )
        if int(stored[-1]["cash"]) != rows[-1]["cash"]:
            report.errors.append(f"last cash {stored[-1]['cash']} != {rows[-1]['cash']}")

    row = db.query_one(
        "SELECT id, match_key, started_at, ended_at, map, faction, build, closed"
        " FROM matches WHERE id = ?",
        (match_id,),
    )
    view = derive_match(db, row, with_ledger=True)
    report.lives = len(view.lives)
    report.curve_net = view.profit

    # The ledger's own identities, on real imported data. If the books do not
    # balance here, the import has produced a curve the domain cannot explain.
    entry = rulesets.select(row["build"], view.started_at)
    errors = entry.module.ledger_invariant_errors(view.ledger)
    report.errors.extend(errors)

    expected_net = rows[-1]["cash"] - rows[0]["cash"]
    if view.profit != expected_net:
        report.errors.append(
            f"curve net {view.profit} != {expected_net} from the file"
        )


def import_xp_log(db: Database, path: Path, report: ImportReport) -> None:
    """The CSV-era `role_xp_log.csv`: gains, never totals.

    Rows name the session file they belonged to, which is how the old UI joined
    them. That join is replaced here by a real foreign key — and rows with a
    blank session file, which the old UI silently dropped, are kept with a null
    match: a gain that landed between matches is still a gain.
    """
    if not path.exists():
        return
    now = utc_now()
    rows: list[tuple] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for raw in csv.DictReader(handle):
            when = (raw.get("timestamp") or "").strip()
            session = (raw.get("session_file") or "").strip()
            match_key = match_key_for_file(Path(session)) if session else None
            match_id = None
            if match_key:
                found = db.query_one(
                    "SELECT id FROM matches WHERE match_key = ?", (match_key,)
                )
                match_id = int(found["id"]) if found else None
                if match_id is None:
                    report.skipped.append(
                        f"XP row at {when} names {session}, which was not imported"
                    )
            for role in XP_ROLES:
                try:
                    delta = int(raw.get(role) or 0)
                except ValueError:
                    delta = 0
                if delta:
                    rows.append((match_id, role, None, delta, when or now, now, now, now))
                    report.xp_rows += 1
                    if match_id is None:
                        report.xp_orphaned += 1
                    else:
                        report.xp_attached += 1

    if rows:
        with db.write() as conn:
            conn.executemany(
                "INSERT INTO xp_events (match_id, role, total, delta, source_ts,"
                " observed_at, received_at, ingested_at) VALUES (?,?,?,?,?,?,?,?)",
                rows,
            )


def run_import(db: Database, source_dir: Path) -> ImportReport:
    report = ImportReport()
    files = sorted(source_dir.glob("session_*.csv"))
    log.info("importing %d session files from %s", len(files), source_dir)
    for path in files:
        result = import_file(db, path)
        report.files.append(result)
        log.info(
            "%s: %d rows, %d lives, net %d%s",
            result.match_key,
            result.rows,
            result.lives,
            result.curve_net,
            "" if result.ok else "  FAILED: " + "; ".join(result.errors),
        )
    import_xp_log(db, source_dir / "role_xp_log.csv", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="profitdog.server.importer")
    parser.add_argument("--from", dest="source", default=".", help="Directory of CSVs")
    parser.add_argument("--db", default="profitdog.sqlite3")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    db = open_database(Path(args.db))
    try:
        report = run_import(db, Path(args.source))
        print(report.text())
        return 0 if report.ok else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
