/**
 * Filter state, in the URL.
 *
 * ## What is left of this module
 *
 * It used to hold the history pipeline: match summaries rolled up from a
 * ledger the browser had just built, filter predicates, buckets, cumulative
 * totals, build markers, CSV export. All of that is `profitdog/server/domain/
 * history.py` now, and arrives over `/api/history` already grouped.
 *
 * What is left is the part that genuinely belongs to a browser: which filters
 * are active, spelled as a query string so a link or a reload restores the
 * view. The server reads exactly the same parameter names, so the same string
 * that addresses a page also addresses the data behind it.
 */

/** A range key the server understands. */
export type TimeRange = "7d" | "30d" | "all"
export type Grouping = "match" | "day" | "week"

export interface HistoryFilters {
  range: TimeRange
  /** A map name, or "all". */
  map: string
  /** A build id, "unknown", or "all". */
  build: string
  grouping: Grouping
  search: string
  /** Id of the selected match, kept in the URL so a link restores it. */
  selected: string | null
}

export const DEFAULT_FILTERS: HistoryFilters = {
  range: "30d",
  map: "all",
  build: "all",
  grouping: "match",
  search: "",
  selected: null,
}

export function readFilters(query: URLSearchParams): HistoryFilters {
  const range = query.get("range")
  const grouping = query.get("group")
  return {
    range: range === "7d" || range === "30d" || range === "all" ? range : "30d",
    map: query.get("map") || "all",
    build: query.get("build") || "all",
    grouping:
      grouping === "day" || grouping === "week" || grouping === "match"
        ? grouping
        : "match",
    search: query.get("q") || "",
    selected: query.get("sel"),
  }
}

/**
 * Filters as a query string, with defaults omitted.
 *
 * Leaving defaults out keeps `/history` itself a valid, shareable address for
 * the default view instead of a redirect to a noisier equivalent.
 */
export function writeFilters(filters: HistoryFilters): string {
  const query = new URLSearchParams()
  if (filters.range !== DEFAULT_FILTERS.range) query.set("range", filters.range)
  if (filters.map !== "all") query.set("map", filters.map)
  if (filters.build !== "all") query.set("build", filters.build)
  if (filters.grouping !== DEFAULT_FILTERS.grouping) {
    query.set("group", filters.grouping)
  }
  if (filters.search.trim()) query.set("q", filters.search.trim())
  if (filters.selected) query.set("sel", filters.selected)
  const text = query.toString()
  return text ? `?${text}` : ""
}

/**
 * The same filters, addressed to the API.
 *
 * `sel` is dropped: which row is highlighted is a fact about the page, not
 * about the query, and sending it would make two views of identical data look
 * like different requests.
 */
export function apiQuery(filters: HistoryFilters, extra: Record<string, string> = {}): string {
  const query = new URLSearchParams()
  query.set("range", filters.range)
  if (filters.map !== "all") query.set("map", filters.map)
  if (filters.build !== "all") query.set("build", filters.build)
  if (filters.grouping !== "match") query.set("group", filters.grouping)
  if (filters.search.trim()) query.set("q", filters.search.trim())
  for (const [key, value] of Object.entries(extra)) query.set(key, value)
  return `?${query.toString()}`
}

export function historyHref(filters: HistoryFilters): string {
  return `/history${writeFilters(filters)}`
}

// ---------------------------------------------------------------------------
// Navigation between pages
// ---------------------------------------------------------------------------

/**
 * A link to one match on the Session page, carrying the way back.
 *
 * The return address is the *whole* history URL, filters and selection
 * included, rather than a bare `/history`. Drilling into a match and pressing
 * Back should land on the list you left, not on a default view you then have
 * to rebuild — and encoding it in the link means it survives a reload and a
 * shared URL, which component state would not.
 */
export function sessionHref(matchKey: string, backTo?: string): string {
  const query = new URLSearchParams({ match: matchKey })
  if (backTo) query.set("back", backTo)
  return `/session?${query.toString()}`
}

export interface SessionParams {
  matchKey: string | null
  /** Where a "Back to history" control should go, if we arrived from there. */
  backHref: string | null
}

export function readSessionParams(query: URLSearchParams): SessionParams {
  const back = query.get("back")
  return {
    matchKey: query.get("match"),
    // Only ever an in-app path: a `back` of `https://…` in a shared link would
    // turn this control into an open redirect.
    backHref:
      back && back.startsWith("/") && !back.startsWith("//") ? back : null,
  }
}

/** "28m", "1h 04m" — match durations, read at a glance in a dense row. */
export function formatMatchDuration(seconds: number): string {
  const total = Math.max(Math.round(seconds / 60), 0)
  const hours = Math.floor(total / 60)
  const minutes = total % 60
  if (hours === 0) return `${minutes}m`
  return `${hours}h ${minutes.toString().padStart(2, "0")}m`
}
