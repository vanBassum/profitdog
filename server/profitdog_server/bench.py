"""A realistic synthetic load, and what it costs.

    python -m profitdog_server.bench --hours 1000

## What this is for

The architecture makes one bet: nothing derived is stored, and every
conclusion is recomputed from facts on read. That bet is cheap to make and
expensive to be wrong about, so it is measured rather than assumed. If a
thousand hours of history cannot be filtered, bucketed and correlated inside a
reasonable response time, the answer is a cache keyed on ruleset version and
input hash — and this is the thing that gets to decide, not taste.

## Why the data has to be realistic

A benchmark over flat curves would measure SQLite and nothing else. The cost
here is in the *shape*: `build_ledger` walks every reading, groups by life,
detects a kit purchase per life, classifies the gaps between lives, and
searches each life for its break-even crossing. So the generator produces what
the real files contain — a kit bought a poll or two into each life, sometimes
in instalments, earnings arriving in bursts, the occasional mid-life outgoing,
money that moves between lives and belongs to none of them, and matches that
end in the menu.

One thousand hours at two-second polling is 1.8 million readings across roughly
three thousand matches and twenty thousand lives — several years of play for
one person, which is the point: the numbers below are a ceiling, not a
forecast.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from profitdog_protocol import Fact, FactBatch
from .db import Database, open_database
from .domain.analysis import METRIC_KEYS, analyse
from .domain.cache import MatchCache, drop as drop_cache
from .domain.engine import MATCH_COLUMNS, derive_all, derive_match
from .domain.history import Filters, build_buckets, filter_matches, totals_of
from .downsample import downsample
from .ingest import Ingestor

POLL_SEC = 2.0
ROLES = ["Wardog", "Infantry", "Medic", "Driver", "Pilot", "Support", "Recon"]
MAPS = ["Kavkazi", "Europe", "Frontend", "Tarkhun", "Zavod"]
FACTIONS = ["alpha", "bravo", "charlie"]


@dataclass
class Generated:
    matches: int = 0
    lives: int = 0
    samples: int = 0
    facts: int = 0
    seconds: float = 0.0
    batches: list = field(default_factory=list)


class Synthesizer:
    """Produces the fact stream a very prolific agent would have produced."""

    def __init__(self, hours: float, seed: int = 20260919) -> None:
        self.target_seconds = hours * 3600
        self.rng = random.Random(seed)
        self.seq = 0
        self.clock = datetime(2024, 1, 1, 18, 0, tzinfo=timezone.utc)
        self.agent_id = "bench-agent"
        self.boot_id = "bench-boot"
        self.xp = {role: 0 for role in ROLES}

    def _fact(self, kind: str, source: str, body: dict, source_ts: str | None = None) -> Fact:
        self.seq += 1
        at = self.clock.isoformat()
        return Fact(
            agent_id=self.agent_id,
            boot_id=self.boot_id,
            agent_seq=self.seq,
            kind=kind,
            source=source,
            observed_at=at,
            body=body,
            source_ts=source_ts or at,
        )

    def _presence(self, state: str, cash: int | None, faction: str) -> Fact:
        body = {"game_state": state, "faction": faction, "in_profit_or_loss": "1"}
        if cash is not None:
            body["profit_loss"] = f"{'-' if cash < 0 else '+'}${abs(cash):,} Profit"
        return self._fact("presence", "rich_presence", body)

    def match(self) -> tuple[list[Fact], int, float]:
        """One plausible match. Returns its facts, life count and duration."""
        rng = self.rng
        facts: list[Fact] = []
        chosen_map = rng.choice(MAPS)
        faction = rng.choice(FACTIONS)
        lives = rng.randint(2, 9)
        facts.append(self._fact("map", "breadcrumbs", {"map": chosen_map}))

        cash = 0
        started = self.clock
        # Two confirmed reads open the match.
        for _ in range(2):
            facts.append(self._presence("playing", cash, "unknown"))
            self.clock += timedelta(seconds=POLL_SEC)

        for life in range(lives):
            if life > 0:
                # A respawn: the breadcrumb log times it, the kit follows.
                facts.append(
                    self._fact("spawn", "breadcrumbs", {"at": self.clock.isoformat()})
                )
            kit = rng.choice([390, 640, 1870, 2990, 3110, 3260, 3660, 5260])
            if rng.random() < 0.25:
                # Paid in two instalments, seconds apart — the case that made
                # `MAX_PART_GAP_SEC` necessary.
                first = int(kit * rng.uniform(0.5, 0.8))
                cash -= first
                facts.append(self._presence("playing", cash, faction))
                self.clock += timedelta(seconds=POLL_SEC)
                cash -= kit - first
            else:
                cash -= kit
            facts.append(self._presence("playing", cash, faction))
            self.clock += timedelta(seconds=POLL_SEC)

            # The life itself: mostly flat, with bursts of income and the
            # occasional outgoing that is provably not the kit.
            for _ in range(rng.randint(40, 320)):
                roll = rng.random()
                if roll < 0.22:
                    cash += rng.choice([50, 120, 150, 250, 400, 900, 1500])
                elif roll < 0.25:
                    cash -= rng.choice([35, 70, 175, 470, 6250])
                facts.append(self._presence("playing", cash, faction))
                self.clock += timedelta(seconds=POLL_SEC)

            if rng.random() < 0.3:
                # Money that moves between lives and belongs to neither.
                cash += rng.randint(100, 3000)

        # Back to the menu: five misses end it.
        for _ in range(5):
            facts.append(self._presence("mainmenu", None, faction))
            self.clock += timedelta(seconds=POLL_SEC)

        if rng.random() < 0.6:
            role = rng.choice(ROLES)
            self.xp[role] += rng.randint(1, 3)
            facts.append(self._fact("xp", "save_file", {"roles": dict(self.xp)}))

        # A gap before the next match, so match boundaries are real.
        self.clock += timedelta(minutes=rng.randint(2, 20))
        return facts, lives, (self.clock - started).total_seconds()

    def generate(self, batch_size: int = 5000) -> Generated:
        out = Generated()
        pending: list[Fact] = []
        played = 0.0
        while played < self.target_seconds:
            facts, lives, duration = self.match()
            played += duration
            out.matches += 1
            out.lives += lives
            out.samples += sum(
                1 for f in facts if f.kind == "presence" and "profit_loss" in f.body
            )
            pending.extend(facts)
            while len(pending) >= batch_size:
                out.batches.append(
                    FactBatch(self.agent_id, self.boot_id, pending[:batch_size])
                )
                pending = pending[batch_size:]
        if pending:
            out.batches.append(FactBatch(self.agent_id, self.boot_id, pending))
        out.facts = self.seq
        out.seconds = played
        return out


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


class Timer:
    def __init__(self) -> None:
        self.results: dict[str, list[float]] = {}

    def time(self, name: str, fn, repeat: int = 1):
        value = None
        for _ in range(repeat):
            start = time.perf_counter()
            value = fn()
            self.results.setdefault(name, []).append(
                (time.perf_counter() - start) * 1000
            )
        return value

    def report(self) -> list[tuple[str, float, float, int]]:
        return [
            (name, statistics.median(times), max(times), len(times))
            for name, times in self.results.items()
        ]


def run(db: Database, hours: float, seed: int = 20260919, repeat: int = 5) -> dict:
    synth = Synthesizer(hours, seed)
    print(f"generating ~{hours:g} hours of play…", flush=True)
    start = time.perf_counter()
    data = synth.generate()
    generate_ms = (time.perf_counter() - start) * 1000
    print(
        f"  {data.matches} matches, {data.lives} lives, {data.facts} facts "
        f"({data.seconds / 3600:.0f} hours of play) in {generate_ms / 1000:.1f}s",
        flush=True,
    )

    ingestor = Ingestor(db)
    print("ingesting…", flush=True)
    start = time.perf_counter()
    for index, batch in enumerate(data.batches):
        ingestor.ingest(batch)
        if index % 20 == 0:
            done = (index + 1) * 100 // max(len(data.batches), 1)
            print(f"\r  {done}%", end="", flush=True)
    ingest_s = time.perf_counter() - start
    print(f"\r  done in {ingest_s:.1f}s", flush=True)
    db.checkpoint()

    stored = {
        table: int(db.scalar(f"SELECT COUNT(*) FROM {table}"))
        for table in (
            "matches", "cash_samples", "source_events", "xp_events",
            "ingested_envelopes", "api_events",
        )
    }
    size_mb = sum(
        p.stat().st_size for p in db.path.parent.glob(db.path.name + "*")
    ) / (1024 * 1024)

    timer = Timer()
    rows = db.query(f"{MATCH_COLUMNS} ORDER BY started_at")
    cache = MatchCache(db)

    # The uncached cost, which is what the cache exists to answer.
    all_views = timer.time(
        "derive every match (no cache)", lambda: derive_all(db, rows), repeat=1
    )
    drop_cache(db)
    timer.time(
        "derive every match (cold cache)", lambda: cache.derive_all(rows), repeat=1
    )
    all_views = timer.time(
        "derive every match (warm cache)", lambda: cache.derive_all(rows), repeat=3
    )

    filters_all = Filters(range="all")
    filters_30 = Filters(range="30d")
    now = datetime.now(timezone.utc)

    timer.time(
        "history, all time",
        lambda: build_buckets(filter_matches(all_views, filters_all, now), "day"),
        repeat=repeat,
    )
    timer.time(
        "history, 30 days",
        lambda: build_buckets(filter_matches(all_views, filters_30, now), "match"),
        repeat=repeat,
    )
    timer.time("totals", lambda: totals_of(all_views), repeat=repeat)

    lives = [life for view in all_views for life in view.lives]
    timer.time(
        "analysis, kit vs earned",
        lambda: analyse(lives, "kitCost", "earned"),
        repeat=repeat,
    )
    timer.time(
        "analysis, every pairing",
        lambda: [analyse(lives, x, y) for x in METRIC_KEYS for y in METRIC_KEYS],
        repeat=1,
    )

    one = rows[len(rows) // 2]
    timer.time(
        "one match, full ledger",
        lambda: derive_match(db, one, with_ledger=True),
        repeat=repeat,
    )

    def curve():
        raw = [
            (r["elapsed_sec"], r["cash"], r["life"])
            for r in db.query(
                "SELECT elapsed_sec, cash, life FROM cash_samples WHERE match_id = %s"
                " ORDER BY elapsed_sec",
                (int(one["id"]),),
            )
        ]
        return downsample(raw, 600)

    timer.time("one match, chart curve", curve, repeat=repeat)

    return {
        "hours": data.seconds / 3600,
        "matches": data.matches,
        "lives": data.lives,
        "facts": data.facts,
        "ingest_seconds": ingest_s,
        "facts_per_second": data.facts / ingest_s if ingest_s else 0,
        "stored": stored,
        "size_mb": size_mb,
        "timings": timer.report(),
    }


def print_report(result: dict) -> None:
    print()
    print("=" * 72)
    print(f"  {result['hours']:.0f} hours of play")
    print("=" * 72)
    print(f"  matches            {result['matches']:>12,}")
    print(f"  lives              {result['lives']:>12,}")
    print(f"  facts ingested     {result['facts']:>12,}")
    for table, count in result["stored"].items():
        print(f"  {table:<18} {count:>12,}")
    print(f"  database on disk   {result['size_mb']:>11.1f} MB")
    print()
    print(
        f"  ingest             {result['ingest_seconds']:>11.1f} s "
        f"({result['facts_per_second']:,.0f} facts/s)"
    )
    print()
    print(f"  {'read path':<34} {'median':>10} {'worst':>10}   runs")
    print("  " + "-" * 62)
    for name, median, worst, runs in result["timings"]:
        print(f"  {name:<34} {median:>9.1f}ms {worst:>9.1f}ms {runs:>6}")
    print("=" * 72)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="profitdog_server.bench")
    parser.add_argument("--hours", type=float, default=1000.0)
    parser.add_argument("--db", default="")
    parser.add_argument("--seed", type=int, default=20260919)
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--json", default="", help="Also write the result here")
    args = parser.parse_args(argv)

    path = Path(args.db) if args.db else Path("bench.sqlite3")
    for existing in path.parent.glob(path.name + "*"):
        existing.unlink()

    db = open_database(path)
    try:
        result = run(db, args.hours, args.seed, args.repeat)
        print_report(result)
        if args.json:
            Path(args.json).write_text(json.dumps(result, indent=2), encoding="utf-8")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
