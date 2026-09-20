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
  GAMING PC                                SERVER (hosted)
  ─────────                                ───────────────
  Rich Presence ─┐
  breadcrumbs   ─┼─► collector ─► outbox ──HTTP──► ingest ─► PostgreSQL
  save file     ─┘               (SQLite,           │        (facts)
                                  durable)          │             │
                                                    │        segmentation
                                                    │             │
                                              api_events     domain rules
                                              (published)     (versioned,
                                                    │        derived on read)
                                                    ▼             │
  browser  ◄────── WebSocket deltas ────────────────┘             │
           ◄────── REST snapshots ──────────────────────────────────┘
```

### Three apps, and the contract between them

```text
  agent/      the Windows agent            → dist/profitdog.exe
  server/     the API, the UI, sign-in     → ghcr.io/vanbassum/profitdog-server
  ui/         the React app                → built into the server image
  protocol/   the wire contract both halves speak
```

`agent/` and `server/` do not import each other — the only edge between them is
`protocol/`, and `tests/test_no_csv_paths.py` keeps it that way. That is what
makes the agent shippable to a machine that has never heard of PostgreSQL, and
the server deployable without a Windows DLL in the image.

### Two databases, on purpose

**PostgreSQL** holds everything on the server: facts, accounts, sessions,
linking codes, the published log. It is a hosted multi-user service now, and
its data has to outlive the container, back up without stopping writers, and
not care that the process restarted.

**SQLite** is still the agent's outbox, and should stay that way. That is a
single-writer queue on one gaming PC with no server in sight — exactly the
shape SQLite is best at, and a PC that cannot reach the network must still be
able to record a match. Nothing under `agent/` imports a database driver.

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

## Accounts

One server, several people, and nobody sees anybody else's play.

Identity is Google's, over OpenID Connect, entirely server-side. The browser
gets an HTTP-only session cookie; the agent gets something else entirely, and
the two are never interchangeable:

| | a person | a linked PC |
| --- | --- | --- |
| proves it with | session cookie | `Authorization: Bearer pdog_…` |
| may | read and correct their own data | upload facts |
| may not | upload | read anything at all |

That asymmetry is the point. A gaming PC is the least defended machine in the
system, and the credential it holds is a bearer token. The worst a stolen one
can do is write rubbish into the account it belongs to — it cannot read a
history, and it cannot be traded for a session.

**Google tokens never reach the agent.** The authorization code, the token
exchange and the ID token live between the browser, the server and Google. The
agent only ever sees a code it asked for and a credential it was handed.

### Linking a PC

The EXE is generic: the same bytes for everyone, carrying no account.

1. It asks the server for a short-lived code and prints it.
2. It opens the server in the default browser.
3. You sign in with Google and approve the code.
4. It exchanges the code for an upload-only credential and stores it.

The code lasts ten minutes, works once, and is bound to a device secret that
only the asking PC holds — so reading a code over someone's shoulder is not
enough to collect the credential. On Windows the credential is sealed with
DPAPI against your login, so the file is useless on another account or another
machine.

`--relink` forgets the stored credential and starts again. A credential is
revoked by setting `revoked_at` on its `agent_credentials` row, which takes
effect on that agent's very next upload.

### Ownership

`agents`, `matches` and `api_events` carry a `user_id`. The facts themselves do
not: each hangs off a match or an agent that has one, and a second copy of the
answer is a second place for it to be wrong.

Every read is scoped at the query, not filtered afterwards — `all_matches()`
takes an account and has no unscoped form, so a route cannot forget. Another
account's match is a **404, not a 403**: a 403 would confirm that the key names
a real match.

### Configuration

| | |
| --- | --- |
| `PROFITDOG_PUBLIC_URL` | where a browser reaches this server; builds the OAuth redirect and the link URL |
| `PROFITDOG_GOOGLE_CLIENT_ID` | OAuth client id |
| `PROFITDOG_GOOGLE_CLIENT_SECRET` | OAuth client secret |
| `PROFITDOG_ALLOWLIST` | who may create an account: `a@b.com,@example.com`. **Empty admits nobody** |
| `PROFITDOG_OWNER_EMAIL` | the account that inherits everything written before accounts existed |
| `PROFITDOG_COOKIE_SECURE` | `0` only for a local http:// server; a Secure cookie is never sent over http |
| `PROFITDOG_AGENT_EXE` | the build served at `/download` |

Register `{PROFITDOG_PUBLIC_URL}/auth/callback` as the authorized redirect URI
in the Google Cloud console.

HTTPS is assumed. Cookies are `Secure` and `HttpOnly`, `SameSite=Lax` so the
Google callback works while cross-site POSTs still do not.

The allowlist admits *new* accounts only. Removing an address does not take an
existing account away — losing a history to an environment variable would be a
surprising way to be revoked, and revocation is a separate concern from
admission.

### Inheriting the single-user database

Rows written before any of this carry `user_id IS NULL`, which means "from
before the question was asked" and never "public". On startup the address in
`PROFITDOG_OWNER_EMAIL` gets a `users` row and adopts all of them. That account
has no Google subject until the first sign-in whose **verified** address matches
claims it — which is why `email_verified` is insisted upon, and the only time an
address rather than a subject decides an account.

It runs every startup and is idempotent, so the CSV importer can keep writing
ownerless history and it will be adopted next time the server starts.

## Running it

The whole thing, including its database:

```bash
cp .env.example .env          # then fill in the Google client
docker compose up
```

That serves http://127.0.0.1:5174. Without a Google client it still starts and
says so on `/login`, which is the useful failure — everything else can be
exercised.

By hand, for development:

```bash
docker compose up -d postgres         # just the database
pip install -r requirements-dev.txt

cd ui && pnpm install && pnpm build && cd ..
export PROFITDOG_DATABASE_URL=postgresql://profitdog:profitdog@127.0.0.1:55432/profitdog
export PROFITDOG_PUBLIC_URL=http://127.0.0.1:5174
export PROFITDOG_GOOGLE_CLIENT_ID=... PROFITDOG_GOOGLE_CLIENT_SECRET=...
export PROFITDOG_ALLOWLIST=you@example.com
export PROFITDOG_COOKIE_SECURE=0
PYTHONPATH=server:protocol python -m profitdog_server

# on the gaming PC: shows a code, opens a browser, links itself
PYTHONPATH=agent:protocol python -m profitdog_agent --server http://127.0.0.1:5174
```

`PROFITDOG_COOKIE_SECURE=0` matters locally and only locally: a `Secure` cookie
is never sent back over `http://`, so sign-in appears to succeed and then
silently does not. Leave it on behind TLS.

The `PYTHONPATH` is what a root `conftest.py` does for the tests and what the
Dockerfile and the PyInstaller spec do for the deployed forms — three source
roots, no install step.

`cd agent && python -m PyInstaller agent.spec` builds `agent/dist/profitdog.exe`
— the agent, frozen, for a machine with no Python on it. CI does this on a
Windows runner for every tag and attaches it to the release; see
`.github/workflows/release.yml`.

## The API

Snapshots over REST, committed deltas over WebSocket, and never the other way
round. A socket that pushed derived summaries would be a second implementation of
the domain racing the first one over the network.

| | |
| --- | --- |
| `POST /api/agent/facts` | *(agent)* take delivery of a batch; returns the acknowledgement |
| `GET /api/agent/cursor` | *(agent)* where the server is for this agent, so it can resume |
| `POST /api/agent/link/start` | *(open)* ask for a linking code |
| `POST /api/agent/link/poll` | *(open)* wait for approval, then collect the credential once |
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
| `GET /api/me` | who the browser is signed in as |
| `GET /login`, `/auth/login`, `/auth/callback`, `/auth/logout` | the Google round trip |
| `GET /link`, `POST /link/approve` | approve a PC that is showing a code |
| `GET /download`, `/download/profitdog.exe` | the agent build |

Everything not marked *(agent)* or *(open)* needs a session, and answers for
that account only. An agent credential is refused on all of them.

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

`PYTHONPATH=server:protocol python -m profitdog_server.bench --hours 1000` generates a realistic synthetic
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
docker compose up -d postgres     # the suite needs a real one
python -m pytest                  # 179 tests
cd ui && pnpm typecheck && pnpm build
```

**The tests run against PostgreSQL, and there is no fallback.** Every test gets
its own schema (`CREATE SCHEMA test_...`), created and dropped in milliseconds,
so they are isolated without a database each. `PROFITDOG_TEST_DATABASE_URL`
points them somewhere else; with nothing to connect to, the run fails at
collection with the command to start one rather than quietly testing something
easier.

That is not pedantry. The port off SQLite was caught by exactly this: a query
selecting a bare column beside `MAX()` with no `GROUP BY`, which SQLite answers
and PostgreSQL rejects, and a placeholder list built by joining the *characters*
of a string — a bug that was invisible while the placeholder was one character
long.

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
