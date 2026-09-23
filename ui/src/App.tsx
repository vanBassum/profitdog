import * as React from "react"

import { AnalysisPage } from "@/components/analysis/AnalysisPage"
import { HistoryPage } from "@/components/history/HistoryPage"
import { SessionPage } from "@/components/SessionPage"
import { NavTab } from "@/components/Nav"
import { ThemeToggle } from "@/components/ThemeToggle"
import { api, type AgentsResponse, type MetricsResponse } from "@/lib/api"
import {
  historyHref,
  readFilters,
  readSessionParams,
  writeFilters,
  type HistoryFilters,
} from "@/lib/history"
import { useLive, useQuery } from "@/lib/live"
import { analysisHref, readAxes, type AnalysisAxes } from "@/lib/metrics"
import { navigate, useLocation } from "@/lib/router"
import { cn } from "@/lib/utils"

type Page = "session" | "history" | "analysis"

/**
 * The shell: which page, which filters, and one live connection for all of it.
 *
 * ## What this component no longer does
 *
 * It used to hold the application's data. Matches were parsed from CSV text in
 * the browser, kit-price corrections lived in `localStorage`, and sample data
 * stood in when nothing had been tracked yet. All three are gone:
 *
 * - Matches come from the server, already derived under the ruleset each one
 *   was played under.
 * - Corrections are a `PUT`, stored beside the facts rather than in one
 *   browser's storage — a correction you made on the desktop was invisible on
 *   the laptop, which for a figure the whole page depends on is a bug.
 * - Sample data is gone with the CSV parser that could fabricate it. An empty
 *   database now says it is empty, which is true and more useful than a
 *   plausible fiction.
 *
 * What remains here is the shell: which page is showing, which filters are
 * active, and one live connection shared by all of them.
 */
export function App() {
  const live = useLive()
  const location = useLocation()

  const page: Page = location.pathname.startsWith("/history")
    ? "history"
    : location.pathname.startsWith("/analysis")
      ? "analysis"
      : "session"

  const filters = React.useMemo(
    () => readFilters(location.query),
    [location.query]
  )
  const axes = React.useMemo(() => readAxes(location.query), [location.query])
  const sessionParams = React.useMemo(
    () => readSessionParams(location.query),
    [location.query]
  )

  // The metric catalogue is served rather than hard-coded, so the Analysis
  // controls describe whatever the server actually offers. It never changes
  // within a session, so it is fetched once.
  const { data: metrics } = useQuery<MetricsResponse>("metrics", (signal) =>
    api.metrics(signal)
  )

  // `/` is not a page of its own. Replace rather than push, so Back does not
  // bounce off a redirect the reader never saw.
  //
  // `/spend-vs-earn` was the fixed kit-cost-against-earnings chart before
  // Analysis generalised it. That pairing is still the default, so an old link
  // lands on the same picture, carrying its filters with it.
  React.useEffect(() => {
    if (location.pathname === "/" || location.pathname === "") {
      navigate("/session", { replace: true })
    } else if (location.pathname.startsWith("/spend-vs-earn")) {
      navigate(analysisHref(filters), { replace: true })
    }
  }, [location.pathname, filters])

  const updateFilters = (next: Partial<HistoryFilters>) => {
    // Filter changes replace rather than push: needing twelve Back presses to
    // escape a dropdown is not what Back is for. Opening a match still pushes,
    // because that is a move between pages.
    const merged = { ...filters, ...next }
    navigate(
      page === "analysis"
        ? analysisHref(merged, axes)
        : `/history${writeFilters(merged)}`,
      { replace: true }
    )
  }

  const updateAxes = (next: Partial<AnalysisAxes>) => {
    navigate(analysisHref(filters, { ...axes, ...next }), { replace: true })
  }

  return (
    // No background of its own: <body> already paints one, and leaving this
    // transparent is what lets a page wash the whole viewport behind itself
    // (the session page's faction tint).
    <div className="min-h-svh">
      <div className="mx-auto w-full max-w-[1600px] px-[clamp(24px,2vw,32px)] pt-5 pb-4">
        <header className="mb-4 flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-b pb-2.5">
          <div className="flex items-center gap-5">
            <h1 className="text-lg font-semibold">profitdog</h1>
            <nav className="flex items-center gap-1" aria-label="Main">
              <NavTab to="/session" active={page === "session"}>
                Session
              </NavTab>
              {/* Filters travel with every tab, so switching and coming back
                  does not quietly reset the view. */}
              <NavTab to={historyHref(filters)} active={page === "history"}>
                History
              </NavTab>
              <NavTab
                to={analysisHref(filters, axes)}
                active={page === "analysis"}
              >
                Analysis
              </NavTab>
            </nav>
          </div>

          <div className="flex items-center gap-3">
            <LivePill
              status={live.status}
              seq={live.seq}
              updatedAt={live.updatedAt}
            />
            {/* Nothing arrives until the agent is running on the gaming PC,
                so this says whether it is — and stays a plain anchor either
                way, because /download is server-rendered and outside the
                browser router. */}
            <AgentLink />
            <ThemeToggle />
          </div>
        </header>

        {page === "history" ? (
          <HistoryPage
            filters={filters}
            onFiltersChange={updateFilters}
            revision={live.revision}
          />
        ) : page === "analysis" ? (
          <AnalysisPage
            filters={filters}
            onFiltersChange={updateFilters}
            axes={axes}
            onAxesChange={updateAxes}
            metrics={metrics}
            revision={live.revision}
          />
        ) : (
          <SessionPage
            matchKey={sessionParams.matchKey}
            backHref={sessionParams.backHref}
            live={live}
          />
        )}
      </div>
    </div>
  )
}

/**
 * Whether the gaming PC is actually sending, in the corner of every page.
 *
 * `LivePill` next door answers a different question — whether *this browser*
 * is attached to the server — and the two were routinely confused. A live
 * socket and an empty session is precisely the state this is for: the page is
 * fine, and the PC is not sending.
 *
 * It degrades to the plain "Get the agent" link it replaced whenever there is
 * nothing to add: no linked PC yet, a request that failed, an answer not back.
 * The link is the same either way, which is the point — the thing that says a
 * PC is offline is the thing that opens the page explaining what to do.
 */
function AgentLink() {
  // Polled rather than pushed. Liveness changes with no committed data behind
  // it — a PC switched off publishes nothing — so there is no event on the
  // socket to hang this off, and once a minute is as often as a minute-by-
  // minute heartbeat can say anything new.
  const [tick, setTick] = React.useState(0)
  React.useEffect(() => {
    const timer = window.setInterval(() => setTick((n) => n + 1), 60_000)
    return () => window.clearInterval(timer)
  }, [])

  const { data } = useQuery<AgentsResponse>(
    `agents-${tick}`,
    (signal) => api.agents(signal),
    [tick]
  )

  const agents = data?.agents ?? []
  const online = agents.filter((agent) => agent.online)
  const outdated = agents.find((agent) => agent.version_state === "outdated")

  let label = "Get the agent"
  let dot: "on" | "off" | null = null
  let title = "Download the agent that sends matches here"

  if (agents.length > 0) {
    if (online.length > 0) {
      dot = "on"
      label = online.length === 1 ? "Agent running" : `${online.length} agents running`
      title = online
        .map((a) => `${a.label ?? a.agent_id}${a.version ? ` — ${a.version}` : ""}`)
        .join("\n")
    } else {
      dot = "off"
      label = "Agent offline"
      // Every linked PC is quiet. Naming the most recent one is more use than
      // a count, because it is the machine the reader is about to go and look
      // at.
      const recent = agents[0]
      title = `Nothing heard from ${recent.label ?? recent.agent_id} — it may not be running`
    }
    if (outdated && outdated.current_version) {
      label += ` · ${outdated.current_version} available`
    }
  }

  return (
    <a
      href="/download"
      className={cn(
        // `viz-root` for the same reason LivePill carries it: the dot's colour
        // is a chart token, and the tokens only exist inside that scope.
        "viz-root flex items-center gap-2 rounded-md border px-2.5 py-1 text-xs transition-colors hover:bg-accent hover:text-foreground",
        dot === "off" || outdated ? "text-foreground" : "text-muted-foreground"
      )}
      title={title}
    >
      {dot && (
        <span
          aria-hidden
          className={cn("size-2 rounded-full", dot === "off" && "opacity-40")}
          style={{
            background:
              dot === "on" ? "var(--viz-positive)" : "var(--viz-ink-muted)",
          }}
        />
      )}
      {label}
    </a>
  )
}

function LivePill({
  status,
  seq,
  updatedAt,
}: {
  status: "connecting" | "live" | "offline"
  seq: number | null
  updatedAt: Date | null
}) {
  const label =
    status === "live"
      ? "Live"
      : status === "connecting"
        ? "Connecting"
        : "Not connected"

  return (
    <span
      className="viz-root flex items-center gap-2 text-xs text-muted-foreground"
      title={
        status === "live"
          ? `Streaming committed updates${seq === null ? "" : ` (at #${seq})`}` +
            (updatedAt ? `, last at ${updatedAt.toLocaleTimeString()}` : "")
          : status === "offline"
            ? "No connection to the server — retrying"
            : "Connecting to the server"
      }
    >
      <span
        aria-hidden
        className={cn(
          "size-2 rounded-full",
          status === "live" && "animate-pulse",
          status === "offline" && "opacity-40"
        )}
        style={{
          background:
            status === "live" ? "var(--viz-positive)" : "var(--viz-ink-muted)",
        }}
      />
      {label}
    </span>
  )
}

export default App
