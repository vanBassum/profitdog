import * as React from "react"

import { LifeTable } from "@/components/LifeTable"
import { SummaryBand } from "@/components/SummaryBand"
import { BalanceChart } from "@/components/viz/BalanceChart"
import { NavLink } from "@/components/Nav"
import {
  isUnreachable,
  api,
  toAnnotations,
  toSession,
  type CurveResponse,
  type MatchDetail,
  type MatchesResponse,
} from "@/lib/api"
import { useQuery, type LiveFeed } from "@/lib/live"
import { canGoBack, goBack, navigate } from "@/lib/router"
import { sessionHref } from "@/lib/history"
import { cn } from "@/lib/utils"

interface Props {
  /** From the URL, so a link or a reload lands on the same match. */
  matchKey: string | null
  /** Where "Back to history" goes, with its filters intact. */
  backHref: string | null
  live: LiveFeed
}

const TABLE_OPEN_KEY = "profitdog:lives-table-open"

/** Wardogs' team codenames, to the chart palette's faction slot. */
const FACTION_TOKENS: Record<string, string> = {
  alpha: "--series-lonestar",
  bravo: "--series-valkyra",
  charlie: "--series-manticore",
}
/** Enough to draw a match at full fidelity; the server decimates past it. */
const CURVE_POINTS = 2000

function readTableOpen(): boolean {
  try {
    // Open by default: the table is the reason the chart's numbers can be
    // checked, so it has to be closed deliberately rather than found.
    return localStorage.getItem(TABLE_OPEN_KEY) !== "closed"
  } catch {
    return true
  }
}

/**
 * One server session: its money curve, and the lives read out of it.
 *
 * One session at a time, deliberately. The x-axis is time since joining *that*
 * server, so stitching two together would quietly answer a different question
 * — that is what History is for.
 *
 * ## Live without refetching
 *
 * A match in progress gains a reading every two seconds. Refetching the whole
 * curve each time would be wasteful and would flicker, so the socket's
 * `match.sample` deltas are appended to the curve already on screen. Only
 * structural news — the match ending, a correction landing — refetches
 * anything, and then it refetches a snapshot rather than trying to patch one.
 */
export function SessionPage({ matchKey, backHref, live }: Props) {
  const [tableOpen, setTableOpen] = React.useState<boolean>(readTableOpen)

  React.useEffect(() => {
    try {
      localStorage.setItem(TABLE_OPEN_KEY, tableOpen ? "open" : "closed")
    } catch {
      // A lost preference is not worth an error state.
    }
  }, [tableOpen])

  // Which match: the URL's, or the newest there is.
  const { data: list } = useQuery<MatchesResponse>(
    `matches:session:${live.revision}`,
    (signal) => api.matches("?range=all", signal),
    [live.revision]
  )
  const resolved =
    matchKey ?? (list?.matches.length ? list.matches[list.matches.length - 1].match_key : null)

  // Until a match is resolved there is nothing to ask for. Passing `null`
  // skips the request outright; fetching with an unresolved key asked the
  // server for the match literally named "null" and got a 404 back.
  const { data: detail, error } = useQuery<MatchDetail>(
    `match:${resolved}:${live.revision}`,
    resolved ? (signal) => api.match(resolved, signal) : null,
    [resolved, live.revision]
  )
  const { data: curve } = useQuery<CurveResponse>(
    `curve:${resolved}:${live.revision}`,
    resolved ? (signal) => api.curve(resolved, CURVE_POINTS, signal) : null,
    [resolved, live.revision]
  )

  const isLive = !!resolved && live.liveMatchKey === resolved

  /** The fetched curve, plus anything the socket has delivered since. */
  const merged = React.useMemo(() => {
    if (!curve) return null
    const extra = (resolved ? live.liveSamples.get(resolved) : null) ?? []
    if (extra.length === 0) return curve
    const last = curve.points.length
      ? curve.points[curve.points.length - 1].t
      : -Infinity
    const appended = extra.filter((point) => point.t > last)
    return appended.length
      ? { ...curve, points: [...curve.points, ...appended] }
      : curve
  }, [curve, resolved, live.liveSamples])

  const session = React.useMemo(
    () => (detail && merged ? toSession(detail, merged) : null),
    [detail, merged]
  )
  const annotations = React.useMemo(
    () =>
      detail && merged
        ? toAnnotations(detail, merged)
        : { lives: [], adjustments: [] },
    [detail, merged]
  )

  const setOverride = async (life: number, value: number | null) => {
    if (!resolved) return
    await api.setOverride(resolved, life, value)
    // The server publishes `override.set`, which bumps the revision and
    // refetches this page — so the correction is applied by the same path that
    // would apply it if another tab had made it.
  }

  if (isUnreachable(error) && !detail) {
    return (
      <Empty>
        Could not reach the server. It retries on its own — nothing is lost
        while it is away, because the agent keeps its facts until they land.
      </Empty>
    )
  }

  if (error && !detail) {
    return <Empty>That match is not in the database.</Empty>
  }

  if (!session || session.points.length === 0 || !detail) {
    return (
      <Empty>
        Nothing recorded yet. profitdog only sees your matches once the agent is
        running on the PC you play on —{" "}
        {/* Plain anchor: /download is server-rendered and outside the
            browser router, so intercepting the click would 404 in-page. */}
        <a
          href="/download"
          className="font-medium text-foreground underline underline-offset-4"
        >
          download it here
        </a>
        , run it, and join a server. The curve appears here.
      </Empty>
    )
  }

  // Wardogs names the team only once it has assigned one, so a match that has
  // just started has no faction yet. It arrives as `match.meta`, which refetches
  // `detail`, so the tint and the badge appear mid-match without a reload.
  const factionToken = detail.faction ? FACTION_TOKENS[detail.faction] : undefined
  const factionColor = `var(${factionToken ?? "--series-unknown"})`
  const factionName = detail.faction_display ?? detail.faction

  return (
    <>
      {factionToken && (
        <div
          aria-hidden
          className="viz-root faction-tint"
          style={{ "--faction": factionColor } as React.CSSProperties}
        />
      )}

      <div className="mb-3 flex flex-wrap items-center gap-3">
        {backHref && (
          <NavLink
            to={backHref}
            onClick={(event) => {
              // Same destination either way; a real history step also brings
              // the reader back to where they were scrolled to.
              if (!event.defaultPrevented && canGoBack()) {
                event.preventDefault()
                goBack()
              }
            }}
            className="flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs text-muted-foreground hover:bg-accent hover:text-foreground"
          >
            <svg aria-hidden viewBox="0 0 12 12" className="size-3 rotate-180">
              <path
                d="M4 2.5 L8 6 L4 9.5"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.6"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
            Back to history
          </NavLink>
        )}

        {list && list.matches.length > 1 && (
          <select
            value={resolved ?? ""}
            onChange={(event) =>
              navigate(sessionHref(event.target.value, backHref ?? undefined), {
                replace: true,
              })
            }
            aria-label="Which server session to show"
            className="max-w-[46vw] truncate rounded-md border bg-background px-2 py-1.5 text-xs"
          >
            {[...list.matches].reverse().map((match) => (
              <option key={match.match_key} value={match.match_key}>
                {match.map_display ?? "Unknown map"} ·{" "}
                {new Date(match.started_at).toLocaleString(undefined, {
                  month: "short",
                  day: "numeric",
                  hour: "2-digit",
                  minute: "2-digit",
                })}
                {match.live ? " · live" : ""}
              </option>
            ))}
          </select>
        )}

        <span
          className="viz-root flex items-center gap-2 rounded-md border px-2.5 py-1 text-xs"
          style={
            factionName
              ? {
                  borderColor: `color-mix(in oklab, ${factionColor} 55%, transparent)`,
                  backgroundColor: `color-mix(in oklab, ${factionColor} 12%, transparent)`,
                }
              : undefined
          }
          title={
            factionName
              ? "The team Wardogs put you on for this match"
              : "Wardogs has not assigned a team yet"
          }
        >
          <span
            aria-hidden
            className="size-2.5 rounded-full"
            style={{ backgroundColor: factionColor, opacity: factionName ? 1 : 0.4 }}
          />
          {factionName ? (
            <span>
              <span className="text-muted-foreground">Playing </span>
              <span className="font-semibold">{factionName}</span>
            </span>
          ) : (
            <span className="text-muted-foreground">Team not assigned yet</span>
          )}
        </span>

        {/* Which rules read this match. Normally invisible noise, but the
            moment a second ruleset exists it is the first thing you want to
            know when two matches disagree. */}
        <span className="ml-auto text-[11px] text-muted-foreground">
          read under ruleset {detail.ruleset}
          {merged?.downsampled &&
            ` · curve reduced to ${merged.returned} of ${merged.stored} readings`}
        </span>
      </div>

      {/* Opaque, so the faction tint frames the chart rather than washing
          through it — the marks' rings are drawn in the card's own colour. */}
      <section className="overflow-hidden rounded-lg border bg-card">
        <SummaryBand match={detail} peak={session.peak} trough={session.trough} />
        {/* Closing the table hands its height to the chart. The upper bound
            rises with it rather than the clamp being removed: a curve
            stretched over an entire tall monitor reads as noise, because the
            eye stops being able to compare two distant points at a glance. */}
        <BalanceChart
          className={cn(
            "px-4 pt-2 pb-1 transition-[height] duration-200",
            tableOpen
              ? "h-[clamp(260px,44vh,412px)]"
              : "h-[clamp(320px,72vh,660px)]"
          )}
          session={session}
          annotations={annotations}
          isLive={isLive}
          now={live.updatedAt}
        />
      </section>

      <LifeTable
        className="mt-3 bg-card"
        annotations={annotations.lives}
        open={tableOpen}
        onOpenChange={setTableOpen}
        onOverride={setOverride}
      />
    </>
  )
}

function Empty({ children }: { children: React.ReactNode }) {
  return (
    <div className="mt-3 flex h-56 items-center justify-center rounded-lg border border-dashed">
      <p className="max-w-sm text-center text-sm text-muted-foreground">
        {children}
      </p>
    </div>
  )
}
