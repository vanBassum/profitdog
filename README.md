# profitdog

Tracks what your Wardogs loadouts actually cost you and what they earn back,
per life, and lets you ask whether the expensive kit is worth it.

Two programs:

| | runs on | owns |
| --- | --- | --- |
| **`profitdog.agent`** | the gaming PC | reading local Wardogs sources, and getting what it read to the server |
| **`profitdog.server`** | anywhere | SQLite, the domain rules, the REST API, the browser WebSocket, and the UI |

Both can run on the same machine. Nothing in the protocol assumes it.

## Architecture

```text
  GAMING PC                                SERVER (here, or anywhere)
  ─────────                                ──────────────────────────
  Rich Presence ─┐
  breadcrumbs   ─┼─► collector ─► outbox ──HTTP──► ingest ─► SQLite (facts)
  save file     ─┘               (SQLite,           │             │
                                  durable)          │        segmentation
                                                    │             │
                                              api_events     domain rules
                                              (published)     (versioned,
                                                    │        derived on read)
                                                    ▼             │
  browser  ◄────── WebSocket deltas ────────────────┘             │
           ◄────── REST snapshots ──────────────────────────────────┘
```

### The agent reports; it does not conclude

The agent reads three local sources and sends what they said. It does not
decide when a match started, which life a reading belongs to, what a kit cost,
or whether a role gained XP. The rule applied throughout: *if re-running it over
the same stored facts could produce a different answer, it does not belong in
the agent.*

Two consequences that look like omissions and are not:

- **XP is sent as totals, never gains.** The save file says `Wardog: 82`. That
  is the fact. "You gained 3" is a comparison against an earlier fact, and the
  server can make it whenever it likes — including differently, later, if the
  comparison turns out to have been wrong.
- **Spawns are sent; lives are not counted.** Whether a spawn is a new life or
  the insertion into the match depends on when the match started, which is a
  conclusion the agent is not entitled to.

This matters because a rule that turns out to be wrong is then fixed by
redeploying the *server* and re-reading history, rather than chasing a new agent
onto a gaming PC that may be mid-match.

### Delivery: an outbox, sequence numbers and acknowledgements

Every fact carries three identity fields:

- `agent_id` — which machine. Two gaming PCs can feed one server.
- `boot_id` — which run of the agent process. A gap in the sequence with a boot
  change across it is a restart; a gap without one is lost data.
- `agent_seq` — a per-agent counter that only goes up, *across* boots.

The agent appends facts to a small durable outbox and ships them in batches. The
server stores a batch and answers with the highest sequence it holds *with no
gap behind it*; the agent deletes up to there and not one number further. That
gives at-least-once delivery on the wire and exactly-once effect in the
database — the network is allowed to be unreliable, and the server's idempotency
gate (`ingested_envelopes`, keyed on `(agent, sequence)`) turns the resulting
duplicates into no-ops.

Play with no server running and nothing is lost; it queues and drains when one
appears. `tests/test_agent_server.py` breaks the connection in each of the four
ways it actually breaks — server not up, outage mid-match, lost acknowledgement,
server restored from a backup — and asserts that none of them loses or
duplicates a fact.

### Three timestamps, never collapsed

- `source_ts` — when the source says it happened. The breadcrumb log timestamps
  its own events; Rich Presence has no clock, so for presence reads this is the
  agent's wall clock at the poll.
- `observed_at` — when the agent read it.
- `received_at` — when the server took delivery.

An agent that was offline for an hour delivers facts whose `received_at` is an
hour after `observed_at`. Collapsing any two would make "when did this happen"
and "when did we learn it" the same question, and they are the two you need
separately the moment anything goes wrong.

### Facts, conclusions, corrections

Three different kinds of row, kept apart on purpose:

- **Facts** — `cash_samples`, `source_events`, `xp_events`. What was observed.
  Immutable. A reading keeps the verbatim string Wardogs published (`"-$5,260"`)
  beside the number parsed out of it, so a wrong parser is fixable against the
  evidence instead of having destroyed it.
- **Conclusions** — lives, kits, ledger entries, break-even times,
  classifications. *Not stored.* Computed on read by a versioned ruleset.
- **Corrections** — `overrides`. What a person said when a derivation was wrong.
  Applied on top of a fresh derivation every time, so a rule change does not have
  to know corrections exist and a correction does not have to be re-entered when
  the rules change.

A correction re-labels spending; it cannot invent any. Saying "that kit was
$4,000, not the $5,260 we read off the fall" claims that $1,260 of that fall was
something else — not that the life kept $1,260 more — so it moves dollars
between `kit cost` and `other outflow` and leaves profit exactly where the curve
put it.

### Versioned rules

Wardogs is in Early Access. When a patch changes the economy, every rule in `v1`
stops describing the game — but it does not stop describing *the matches played
under it*. So rules are versioned, old implementations are kept forever, and each
match is read under the rules it was played under: **by game build first**, and
by effective date when no build was recorded (which is every match so far, since
nothing local publishes one). No migration, no rewrite of history.

To add one: copy `v1.py` to `v2.py`, change what the patch changed, register it
with the builds it covers and the date it landed. Do not edit `v1.py`.

## Running it

```bash
pip install -r requirements.txt

# once, to bring the CSV era in
python -m profitdog.server.importer --from . --db profitdog.sqlite3

# the server (serves the API and the built UI on http://127.0.0.1:5174)
cd profitdog-ui && pnpm install && pnpm build && cd ..
python -m profitdog.server

# on the gaming PC
python -m profitdog.agent --server http://127.0.0.1:5174
```

`python -m PyInstaller profitdog.spec` builds `dist/profitdog.exe` — the agent,
frozen, for a machine with no Python on it.

## The API

Snapshots over REST, committed deltas over WebSocket, and never the other way
round. A socket that pushed derived summaries would be a second implementation of
the domain racing the first one over the network.

| | |
| --- | --- |
| `POST /api/agent/facts` | take delivery of a batch; returns the acknowledgement |
| `GET /api/agent/cursor` | where the server is for one agent, so it can resume |
| `GET /api/agents` | which agents have reported, and when |
| `GET /api/health` | schema version, rulesets, sequence, counts, cache stats |
| `GET /api/matches` | match summaries, period totals, what is available to filter by |
| `GET /api/matches/{key}` | one match: lives, ledger, adjustments, kit marks |
| `GET /api/matches/{key}/curve` | the money curve, decimated to `max_points` |
| `GET /api/matches/{key}/overrides` | corrections on one match |
| `PUT /api/matches/{key}/overrides/{life}` | set or clear a kit price |
| `GET /api/history` | buckets, cumulative line, build markers, totals |
| `GET /api/analysis` | one metric pairing: points, correlation, reading, exclusions |
| `GET /api/metrics` | the metric catalogue and presets |
| `GET /api/xp` | role totals and per-match gains |
| `GET /api/stream/cursor` | the current sequence number |
| `POST /api/maintenance/rebuild-cache` | throw the derived cache away |
| `WS /api/live?since=N` | replay the gap after `N`, then live deltas |

Filters (`range`, `map`, `build`, `q`, `group`) mean the same thing on every
endpoint because one predicate implements them.

### Reconnecting

Every delta has a `seq` assigned in the same transaction as the fact it
announces, so a client that remembers the last one it saw knows exactly where it
is. On reconnect with `?since=N` it gets one of three answers: nothing to
replay; a short replay then live deltas; or `resync`, because the gap is wider
than `MAX_REPLAY_EVENTS` or older than the retained log. The third is a promise
about bounded work, not an error — and it is also what happens if the server is
restored from a backup, where the client is *ahead* and replay cannot help.

## Charts

The curve is decimated server-side, bucketed by time with **each bucket's
minimum and maximum kept**. Every kit purchase is a sharp fall across one or two
readings; stride sampling would drop exactly those and draw a life that never
bought anything. Derivation never sees a decimated curve.

## Performance

`python -m profitdog.server.bench --hours 1000` generates a realistic synthetic
load — kits bought in instalments, earnings in bursts, mid-life outgoings, money
between lives, matches ending in the menu — and measures it. At 1,000 hours
(1,351 matches, 7,429 lives, 1.36M facts, 288 MB):

| | |
| --- | --- |
| ingest | 66,000 facts/s |
| derive every match, no cache | 2,951 ms |
| derive every match, warm cache | **235 ms** |
| one match, full ledger | 2.3 ms |
| one match, chart curve | 1.2 ms |

Derive-on-read was the design and is still what happens for a single match. The
benchmark is what decided it was not enough for the history and analysis pages,
so `domain/cache.py` sits in front of the many-match reads — keyed on the ruleset
version, a hash of that ruleset's own source, and a revision of the facts and
corrections that went in. Nothing is only in the cache; `DELETE FROM
derived_matches` costs one slow page load, and `tests/test_cache.py` asserts
every answer is identical with it dropped, cold and warm.

## Tests

```bash
python -m pytest              # 109 tests
cd profitdog-ui && pnpm build
```

The one worth knowing about is `tests/test_domain_parity.py`. Every per-life
figure it asserts — kit costs, earnings, break-even times, correlations — was
read out of the TypeScript implementation before that was deleted. A port checked
against its own output tests nothing; this one is checked against its
predecessor's, to the dollar, on the same two recorded sessions.

## Known limitations

- Kit price is **derived**, not measured. Wardogs publishes no loadout cost —
  not through Rich Presence, not in the crash-reporter breadcrumbs, not in the
  role-XP save. It is read off the fall in the curve, labelled as derived
  everywhere it appears, and correctable by hand.
- A kit bought *after* a life turned a profit, without the curve ever dipping,
  cannot be seen. Such a life reads as having no kit price rather than a guessed
  one.
- No role, vehicle, weapon or objective data exists in any local source, so
  nothing here can control for what you were actually doing. The Analysis page
  says so beside every correlation.
- Game build is modelled and threaded through every fact but is always null:
  nothing local publishes it yet. Ruleset selection falls back to date until
  something does.
- Reading Rich Presence means registering with Steam *as* Wardogs, so the agent
  claims the AppID only while the game is running and drops it the moment the
  process goes — otherwise Steam goes on believing you are still playing.

## Credits

profitdog started from [Adazan21/wardogs-profit-tracker](https://github.com/Adazan21/wardogs-profit-tracker)
by addison — a Tkinter app that read Rich Presence and drew the result from CSV.
That project's commits are the first five in this history, kept rather than
squashed so the attribution is in the log and not only in this paragraph. Almost
nothing of the original code survives the split into agent and server, but the
idea that Rich Presence is a usable source at all came from there.

The upstream repository carries no licence, so it grants no redistribution
rights; this fork exists on the assumption that is an oversight rather than a
decision. If the original author would rather it did not, say so and it comes
down.
