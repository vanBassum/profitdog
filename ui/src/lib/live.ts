/**
 * The live connection: committed deltas, a cursor, and reconnection.
 *
 * ## What comes down this socket, and what does not
 *
 * Deltas only — "this match gained a reading", "this match ended", "a kit
 * price was corrected". Never a summary, never a ledger, never anything the
 * page could have asked for over REST. A socket that pushed derived figures
 * would be a second implementation of the domain racing the first one over the
 * network, which is the thing this whole rearchitecture was to stop.
 *
 * So a delta does one of two things here. A reading for the match on screen is
 * appended to its curve, which is what makes a live match draw smoothly rather
 * than flickering through refetches. Anything structural — a match starting or
 * ending, a correction, XP — bumps a revision counter, and the pages refetch
 * the snapshots that depend on it.
 *
 * ## Reconnecting
 *
 * Every delta carries a sequence number assigned by the server in the same
 * transaction as the fact it announces, so a client that remembers the last
 * one it saw knows exactly where it is. On reconnect it asks for everything
 * after that number and gets one of three answers:
 *
 * - nothing to replay, carry on;
 * - a short replay, then live deltas — the gap is closed and nothing was
 *   refetched;
 * - `resync`, because the gap is too wide or older than the server's retained
 *   log. Then, and only then, the page throws away what it has and refetches.
 *
 * The third case is a promise about bounded work rather than an error, and it
 * is also what happens if the server is restored from a backup: the client is
 * ahead of it, which cannot be reconciled by replay, and starting again is the
 * only honest answer.
 *
 * ## Back-off
 *
 * A closed socket retries with exponential back-off to a ceiling. The usual
 * reason is that the server is not running yet, and hammering a closed port
 * every second for an evening helps nobody. A successful connection resets it,
 * so a flap costs one slow retry rather than a slow evening.
 */

import * as React from "react"

import { liveUrl, type CurvePoint } from "./api"

export type LiveStatus = "connecting" | "live" | "offline"

interface HelloFrame {
  type: "hello"
  seq: number
  oldest: number
  replaying: number
  resync: boolean
}

interface EventFrame {
  type: "event"
  seq: number
  kind: string
  match_key: string | null
  payload: Record<string, unknown>
}

type Frame = HelloFrame | EventFrame | { type: "ping" } | { type: "resync"; reason: string }

const MIN_BACKOFF_MS = 1000
const MAX_BACKOFF_MS = 30000

/** Deltas that change something a snapshot would have to be refetched for. */
const STRUCTURAL = new Set([
  "match.started",
  "match.ended",
  "match.meta",
  "match.life",
  "override.set",
  "xp.gained",
  // Derived numbers — the kit price, the totals — move without any structural
  // news: a sample the server folds into them is not itself an announcement.
  // Without this the screen kept a settled kit cost stale for as long as the
  // balance sat still, because nothing else was bumping the revision.
  "match.derived.updated",
])

export interface LiveFeed {
  status: LiveStatus
  /** Last sequence number seen. Null before the first hello. */
  seq: number | null
  /** Bumped whenever a snapshot somewhere needs refetching. */
  revision: number
  /** The match currently being played, if one is. */
  liveMatchKey: string | null
  /** Readings that have arrived since this page loaded, by match. */
  liveSamples: Map<string, CurvePoint[]>
  /** When the last frame landed. */
  updatedAt: Date | null
}

export function useLive(): LiveFeed {
  const [status, setStatus] = React.useState<LiveStatus>("connecting")
  const [seq, setSeq] = React.useState<number | null>(null)
  const [revision, setRevision] = React.useState(0)
  const [liveMatchKey, setLiveMatchKey] = React.useState<string | null>(null)
  const [liveSamples, setLiveSamples] = React.useState<Map<string, CurvePoint[]>>(
    () => new Map()
  )
  const [updatedAt, setUpdatedAt] = React.useState<Date | null>(null)

  // The cursor lives in a ref as well as state: the reconnect handler reads it
  // outside React's render cycle, where the state value would be the one
  // captured when the effect ran rather than the latest.
  const cursor = React.useRef<number | null>(null)

  React.useEffect(() => {
    let socket: WebSocket | null = null
    let timer: ReturnType<typeof setTimeout> | undefined
    let backoff = MIN_BACKOFF_MS
    let closed = false

    const connect = () => {
      if (closed) return
      socket = new WebSocket(liveUrl(cursor.current))

      socket.onopen = () => {
        backoff = MIN_BACKOFF_MS
        setStatus("live")
      }

      socket.onmessage = (message) => {
        let frame: Frame
        try {
          frame = JSON.parse(message.data as string) as Frame
        } catch {
          return
        }
        setUpdatedAt(new Date())

        if (frame.type === "ping") return

        if (frame.type === "hello") {
          cursor.current = frame.seq
          setSeq(frame.seq)
          if (frame.resync) {
            // Start again from a snapshot: too far behind to replay, or ahead
            // of a server that has been restored.
            setLiveSamples(new Map())
            setRevision((n) => n + 1)
          }
          return
        }

        if (frame.type === "resync") {
          setLiveSamples(new Map())
          setRevision((n) => n + 1)
          return
        }

        cursor.current = frame.seq
        setSeq(frame.seq)

        if (frame.kind === "match.sample" && frame.match_key) {
          const key = frame.match_key
          const point: CurvePoint = {
            t: Number(frame.payload.elapsed_sec),
            c: Number(frame.payload.cash),
            l: Number(frame.payload.life ?? 1),
          }
          setLiveSamples((current) => {
            const next = new Map(current)
            const existing = next.get(key) ?? []
            // A replayed gap can repeat the reading already held; the curve
            // must stay strictly increasing in time or the line doubles back.
            if (existing.length && existing[existing.length - 1].t >= point.t) {
              return current
            }
            next.set(key, [...existing, point])
            return next
          })
          setLiveMatchKey(key)
          return
        }

        if (frame.kind === "match.started" && frame.match_key) {
          setLiveMatchKey(frame.match_key)
        }
        if (frame.kind === "match.ended") {
          // Compared inside the updater rather than against a captured value:
          // the handler outlives several renders, and reading the current key
          // from outside would clear a match that had already been replaced.
          setLiveMatchKey((current) =>
            current === frame.match_key ? null : current
          )
        }
        if (STRUCTURAL.has(frame.kind)) {
          setRevision((n) => n + 1)
        }
      }

      const retry = () => {
        if (closed) return
        setStatus("offline")
        timer = setTimeout(connect, backoff)
        backoff = Math.min(backoff * 2, MAX_BACKOFF_MS)
      }

      socket.onclose = (event) => {
        // 4401 is this server saying "sign in", chosen from the private range
        // because a WebSocket cannot carry an HTTP status. Retrying would
        // reconnect forever against a door that is not going to open.
        if (event.code === 4401) {
          closed = true
          const here = window.location.pathname + window.location.search
          window.location.href = `/login?next=${encodeURIComponent(here)}`
          return
        }
        retry()
      }
      socket.onerror = () => socket?.close()
    }

    connect()
    return () => {
      closed = true
      if (timer) clearTimeout(timer)
      socket?.close()
    }
  }, [])

  return { status, seq, revision, liveMatchKey, liveSamples, updatedAt }
}

/**
 * Run a request, again whenever its key or the live revision changes.
 *
 * Deliberately small. A data-fetching library would bring caching, retries and
 * a mental model, and what this application needs is "ask the server again
 * when something it told us about has changed" — which is one effect and an
 * abort signal.
 */
export function useQuery<T>(
  key: string,
  // `null` means "there is nothing to ask for yet" — an unresolved match key,
  // say. Skipping is the caller's intent made explicit, so the alternative
  // (asserting a value that is still null and fetching `/api/matches/null`)
  // cannot happen by accident.
  run: ((signal: AbortSignal) => Promise<T>) | null,
  deps: unknown[] = []
): { data: T | null; error: Error | null; loading: boolean } {
  // One piece of state carrying the key it was answered for, so `loading` is
  // *derived* — "the answer on screen is not for the question being asked" —
  // rather than a second flag an effect has to remember to set and clear.
  const [state, setState] = React.useState<{
    key: string | null
    data: T | null
    error: Error | null
  }>({ key: null, data: null, error: null })

  React.useEffect(() => {
    if (!run) return
    const controller = new AbortController()
    let cancelled = false
    run(controller.signal)
      .then((value) => {
        if (!cancelled) setState({ key, data: value, error: null })
      })
      .catch((cause: Error) => {
        if (cancelled || controller.signal.aborted) return
        // The previous answer is kept on screen rather than replaced with an
        // error state: a dropped request during a server restart should not
        // blank a page that was correct a second ago.
        setState((current) => ({ ...current, key, error: cause }))
      })
    return () => {
      cancelled = true
      controller.abort()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, ...deps])

  // A skipped query is not loading and has no error: it is simply not asking.
  if (!run) return { data: null, error: null, loading: false }
  return { data: state.data, error: state.error, loading: state.key !== key }
}
