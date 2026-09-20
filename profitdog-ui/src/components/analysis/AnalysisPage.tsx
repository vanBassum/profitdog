import * as React from "react"

import {
  api,
  type AnalysisResponse,
  type MatchesResponse,
  type MetricsResponse,
} from "@/lib/api"
import { apiQuery, type HistoryFilters } from "@/lib/history"
import { useQuery } from "@/lib/live"
import { analysisQuery, type AnalysisAxes } from "@/lib/metrics"
import { TERMS } from "@/lib/terms"

import { HistoryFilterBar } from "../history/HistoryFilters"
import { AnalysisChart } from "./AnalysisChart"
import { AnalysisControls } from "./AnalysisControls"
import { AnalysisReadout } from "./AnalysisReadout"
import { PlottedLivesTable } from "./PlottedLivesTable"

interface Props {
  filters: HistoryFilters
  onFiltersChange: (next: Partial<HistoryFilters>) => void
  axes: AnalysisAxes
  onAxesChange: (next: Partial<AnalysisAxes>) => void
  metrics: MetricsResponse | null
  revision: number
}

/**
 * One dot per life, on any two of the nine things a life can be measured by.
 *
 * ## Why this is its own page
 *
 * Session answers "how did this match go" and History answers "how have I been
 * doing". Neither can answer "is the expensive loadout worth it", or "do
 * longer lives earn more", because both aggregate away the thing those
 * questions are about: the individual life, which is the only unit where one
 * kit price meets one set of earnings and one clock.
 *
 * ## Where the answer comes from
 *
 * `/api/analysis?x=…&y=…` — the points, the correlation, the sentence beside
 * it, and an account of every life that could not be plotted. All of it under
 * the same filter predicate History uses, and under each match's own ruleset.
 *
 * The browser used to compute this: nine metric accessors, an exclusion
 * walker, Pearson's r and the wording. That was a second implementation of an
 * accounting the server also had, and the two would have drifted. It asks now.
 *
 * ## What is left out, and said out loud
 *
 * A life needs a value on both axes to be a dot. Which lives that excludes
 * depends entirely on the pair chosen — no kit price matters only when kit
 * cost is on an axis; never having recovered matters only for break-even time
 * — so the count and the reason come back with every response and are reported
 * beside the chart. On break-even the omission would be directional: the lives
 * that never paid for themselves are exactly the ones a break-even chart would
 * drop.
 */
export function AnalysisPage({
  filters,
  onFiltersChange,
  axes,
  onAxesChange,
  metrics,
  revision,
}: Props) {
  const [selectedId, setSelectedId] = React.useState<string | null>(null)
  const [hoverId, setHoverId] = React.useState<string | null>(null)

  const filterQuery = apiQuery(filters)
  const query = analysisQuery(filters, axes)

  const { data: matches } = useQuery<MatchesResponse>(
    `matches:${filterQuery}:${revision}`,
    (signal) => api.matches(filterQuery, signal),
    [filterQuery, revision]
  )
  const { data: result, error } = useQuery<AnalysisResponse>(
    `analysis:${query}:${revision}`,
    (signal) => api.analysis(query, signal),
    [query, revision]
  )

  // A life that is no longer on the chart cannot stay selected — the axes or
  // the filters moved out from under it, and a highlight pointing at nothing
  // is worse than no highlight.
  const stillPlotted = result?.points.some((p) => p.id === selectedId) ?? false
  const selected = stillPlotted ? selectedId : null

  if (error && !result) {
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
        No matches recorded yet. This compares your lives against each other —
        what they cost, what they earned, how long they lasted. Start the agent
        and play.
      </Empty>
    )
  }

  return (
    <>
      {matches && (
        <HistoryFilterBar
          className="mb-3"
          filters={filters}
          onChange={onFiltersChange}
          maps={matches.filters.maps}
          builds={matches.filters.builds}
          showUnknownBuild={
            matches.filters.has_unknown_build &&
            matches.filters.builds.length > 0
          }
          showGrouping={false}
          covered={matches.covered}
        />
      )}

      <AnalysisControls
        className="mb-3"
        axes={axes}
        onChange={onAxesChange}
        metrics={metrics}
      />

      <section className="overflow-hidden rounded-lg border">
        <div className="grid lg:grid-cols-[1fr_300px]">
          {!result || result.points.length === 0 ? (
            // No axes for no data: an empty grid reads as "zero", which is a
            // different claim from "nothing could be plotted".
            <div className="flex h-56 items-center justify-center px-4">
              <p className="max-w-sm text-center text-sm text-muted-foreground">
                {result
                  ? `No life in this period has both ${result.x.phrase} and ${result.y.phrase}. Widen the period, pick another pair, or set a kit price on the Session page.`
                  : "Loading…"}
              </p>
            </div>
          ) : (
            <AnalysisChart
              className="h-[clamp(280px,46vh,460px)] px-3 pt-3 pb-1"
              result={result}
              showTrend={axes.trend}
              selectedId={selected}
              onSelect={setSelectedId}
              hoverId={hoverId}
              onHover={setHoverId}
            />
          )}

          {result && (
            <AnalysisReadout
              className="border-t p-4 lg:border-t-0 lg:border-l"
              result={result}
            />
          )}
        </div>
      </section>

      {result && (
        <PlottedLivesTable
          className="mt-3"
          points={result.points}
          selectedId={selected}
          hoverId={hoverId}
          onSelect={setSelectedId}
          onHover={setHoverId}
        />
      )}

      <p className="mt-3 rounded-lg border border-dashed px-3 py-2 text-xs text-muted-foreground">
        Dots are individual lives, in dollars and seconds — never a percentage,
        because a $100 kit returning $1,000 beats a $5,000 kit on return and
        loses badly on money. {TERMS.unassignedIncome} is excluded: it belongs
        to no life, so it cannot be counted towards one.
      </p>
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
