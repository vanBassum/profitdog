import * as React from "react"

import { AccountingSummary } from "@/components/AccountingSummary"
import {
  api,
  type HistoryResponse,
  type MatchesResponse,
} from "@/lib/api"
import {
  apiQuery,
  historyHref,
  sessionHref,
  type HistoryFilters,
} from "@/lib/history"
import { useQuery } from "@/lib/live"
import { navigate } from "@/lib/router"
import { accountingOf, TERMS } from "@/lib/terms"

import { HistoryFilterBar } from "./HistoryFilters"
import { ChartLegend, HistoryChart } from "./HistoryChart"
import { MatchesTable } from "./MatchesTable"

interface Props {
  filters: HistoryFilters
  onFiltersChange: (next: Partial<HistoryFilters>) => void
  /** Bumped by the live feed when something a snapshot depends on changed. */
  revision: number
}

/**
 * How the last weeks added up.
 *
 * ## Two requests, no derivation
 *
 * `/api/matches` for the rows and what is available to filter by;
 * `/api/history` for the buckets, the cumulative line, the build markers and
 * the period totals. Both are answered by the same filter predicate the
 * Analysis page uses, on the server, so the two pages cannot disagree about
 * which matches a date range means.
 *
 * This page used to roll all of that up in the browser from CSVs it had just
 * parsed. What it does now is ask, and draw.
 */
export function HistoryPage({ filters, onFiltersChange, revision }: Props) {
  const query = apiQuery(filters)

  const { data: matches, error } = useQuery<MatchesResponse>(
    `matches:${query}:${revision}`,
    (signal) => api.matches(query, signal),
    [query, revision]
  )
  const { data: history } = useQuery<HistoryResponse>(
    `history:${query}:${revision}`,
    (signal) => api.history(query, signal),
    [query, revision]
  )

  const openMatch = (key: string) => {
    // The whole history URL travels with the link, so Back lands on the view
    // that was left rather than a rebuilt default.
    navigate(sessionHref(key, historyHref({ ...filters, selected: key })))
  }

  /**
   * The filtered matches as a CSV file, at full precision.
   *
   * A deliberate export, not a data path: it is built from the typed rows the
   * server already sent, in the words on screen, so a spreadsheet does not have
   * to work out which column is which. Nothing reads it back.
   */
  const handleExport = () => {
    if (!matches) return
    const columns = [
      "match_key", "started_at", "ended_at", "map", "build", "ruleset", "lives",
      "kit_cost", "earned", "other_outflow", "spent",
      "unassigned_income", "unassigned_outflow", "unassigned_movement",
      "profit", "duration_seconds",
    ]
    const cell = (value: unknown) => {
      const text = value == null ? "" : String(value)
      return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
    }
    const lines = [columns.join(",")]
    for (const row of matches.matches) {
      lines.push(
        [
          row.match_key, row.started_at, row.ended_at ?? "", row.map ?? "",
          row.build ?? "", row.ruleset, row.lives, row.kit_cost, row.earned,
          row.other_outflow, row.spent, row.unassigned.positive,
          row.unassigned.negative, row.unassigned.total, row.profit,
          row.duration_sec,
        ]
          .map(cell)
          .join(",")
      )
    }
    const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" })
    const url = URL.createObjectURL(blob)
    const link = document.createElement("a")
    link.href = url
    link.download = `profitdog-matches-${new Date().toISOString().slice(0, 10)}.csv`
    link.click()
    URL.revokeObjectURL(url)
  }

  const openBucket = (bucket: { match_keys: string[] }) => {
    if (bucket.match_keys.length === 1) {
      openMatch(bucket.match_keys[0])
      return
    }
    // An aggregated bucket holds several matches, and picking one for the
    // reader would be arbitrary. Narrow the view instead and let the table
    // show what is inside it.
    onFiltersChange({ grouping: "match", selected: null })
  }

  if (error && !matches) {
    return (
      <Empty>
        Could not reach the server. It retries on its own, and nothing is lost
        while it is away.
      </Empty>
    )
  }

  if (matches && matches.available === 0) {
    return (
      <Empty>
        No matches recorded yet. Start the agent on your gaming PC and play —
        history fills in on its own.
      </Empty>
    )
  }

  if (!matches || !history) {
    return <Empty>Loading…</Empty>
  }

  return (
    <>
      <HistoryFilterBar
        className="mb-3"
        filters={filters}
        onChange={onFiltersChange}
        maps={matches.filters.maps}
        builds={matches.filters.builds}
        showUnknownBuild={
          matches.filters.has_unknown_build && matches.filters.builds.length > 0
        }
        covered={matches.covered}
      />

      <section className="overflow-hidden rounded-lg border">
        <AccountingSummary
          accounting={accountingOf(history.totals)}
          profitLabel={TERMS.periodProfit}
          counts={
            <p className="text-sm tracking-wide whitespace-nowrap uppercase tabular-nums">
              <span className="font-semibold">{history.totals.matches}</span>
              <span className="text-muted-foreground"> matches · </span>
              <span className="font-semibold">{history.totals.lives}</span>
              <span className="text-muted-foreground"> lives</span>
            </p>
          }
        />

        {history.buckets.length === 0 ? (
          // No axes for no data: an empty grid reads as "zero", which is a
          // different claim from "nothing matched".
          <div className="flex h-56 items-center justify-center px-4">
            <p className="max-w-xs text-center text-sm text-muted-foreground">
              No matches in this range. Widen the period or clear a filter.
            </p>
          </div>
        ) : (
          <>
            <HistoryChart
              className="h-[clamp(220px,32vh,300px)] px-3 pt-3"
              buckets={history.buckets}
              markers={history.markers}
              grouping={filters.grouping}
              selectedId={filters.selected}
              onSelect={(selected) => onFiltersChange({ selected })}
              onOpen={openBucket}
            />
            <ChartLegend className="px-4 pt-1 pb-3" />
          </>
        )}
      </section>

      <MatchesTable
        className="mt-3"
        rows={matches.matches}
        bestId={matches.best}
        selectedId={filters.selected}
        search={filters.search}
        onSearch={(search) => onFiltersChange({ search })}
        onSelect={(selected) => onFiltersChange({ selected })}
        onOpen={openMatch}
        onExport={handleExport}
      />
    </>
  )
}

function Empty({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-[60vh] items-center justify-center rounded-lg border border-dashed">
      <p className="max-w-sm text-center text-sm text-muted-foreground">
        {children}
      </p>
    </div>
  )
}
