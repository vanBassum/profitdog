"""profitdog — one service that collects, stores, derives and serves.

See `ARCHITECTURE.md` for the shape of the whole thing. The short version:

    adapters  ->  facts (SQLite, immutable)  ->  domain (derived on read)  ->  API

Facts are the source of truth and are never rewritten. Everything a reader sees
about lives, kits, break-even or classifications is a *conclusion* computed from
those facts by a versioned ruleset, and can be thrown away and recomputed.
Manual corrections live in a third place again, so that a correction can never
be mistaken for either an observation or a derivation.
"""

__version__ = "2.0.0"
