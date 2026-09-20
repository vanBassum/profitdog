/**
 * Which two metrics the Analysis page is showing.
 *
 * ## What is left of this module
 *
 * It used to define the nine metrics, read them off lives the browser had
 * derived, decide which lives could be plotted, group the exclusions and word
 * the sentence. All of that is `profitdog/server/domain/analysis.py` now.
 * `/api/metrics` serves the catalogue and `/api/analysis` serves one pairing's
 * points, correlation, reading and exclusions.
 *
 * What is left is axis *selection* — which is a fact about the page, belongs
 * in the URL, and would be strange to ask a server about.
 *
 * The keys below are the server's keys. They are not a second definition of
 * the metrics; they are the vocabulary for asking about them, and
 * `isMetricKey` is checked against the catalogue the server actually served
 * rather than against this list, so a metric added on the server needs no
 * change here.
 */

import type { MetricDto, MetricsResponse } from "./api"
import type { HistoryFilters } from "./history"
import { apiQuery, writeFilters } from "./history"
import {
  formatDuration,
  formatElapsed,
  formatMoney,
  formatMoneyExact,
} from "./stats"

export interface AnalysisAxes {
  x: string
  y: string
  /** Whether the least-squares line is drawn. */
  trend: boolean
}

/** The first preset, so the page opens on the question that started all this. */
export const DEFAULT_AXES: AnalysisAxes = {
  x: "kitCost",
  y: "earned",
  trend: true,
}

export function readAxes(query: URLSearchParams): AnalysisAxes {
  return {
    x: query.get("x") || DEFAULT_AXES.x,
    y: query.get("y") || DEFAULT_AXES.y,
    // Only ever switched off explicitly, so an old link without the parameter
    // opens the way the page normally looks.
    trend: query.get("trend") !== "off",
  }
}

/**
 * The whole Analysis URL: History's filters, plus which axes.
 *
 * The filters go through `writeFilters` rather than being rewritten here, so a
 * range or a map means the same thing in this address as in History's — which
 * is what lets the nav carry a reader between pages without resetting the view.
 */
export function analysisHref(
  filters: HistoryFilters,
  axes: AnalysisAxes = DEFAULT_AXES
): string {
  const query = new URLSearchParams(writeFilters(filters).replace(/^\?/, ""))
  if (axes.x !== DEFAULT_AXES.x) query.set("x", axes.x)
  if (axes.y !== DEFAULT_AXES.y) query.set("y", axes.y)
  if (!axes.trend) query.set("trend", "off")
  const text = query.toString()
  return `/analysis${text ? `?${text}` : ""}`
}

/** The API request for one pairing under the current filters. */
export function analysisQuery(
  filters: HistoryFilters,
  axes: AnalysisAxes
): string {
  return apiQuery(filters, { x: axes.x, y: axes.y })
}

/** Whether the server's catalogue knows this key. */
export function isMetricKey(
  catalogue: MetricsResponse | null,
  key: string | null
): boolean {
  if (!catalogue || !key) return false
  return catalogue.metrics.some((metric) => metric.key === key)
}

export function metricByKey(
  catalogue: MetricsResponse | null,
  key: string
): MetricDto | null {
  return catalogue?.metrics.find((metric) => metric.key === key) ?? null
}

/** The preset matching the current axes, or null for any other combination. */
export function activePreset(
  catalogue: MetricsResponse | null,
  x: string,
  y: string
) {
  return catalogue?.presets.find((p) => p.x === x && p.y === y) ?? null
}

/**
 * Whether an `x = y` line means anything for this pair.
 *
 * The server says so in every analysis response; this is only for the moment
 * before one has arrived, and agrees with it by construction — same rule, two
 * different metrics in the same unit.
 */
export function isComparable(x: MetricDto | null, y: MetricDto | null): boolean {
  return !!x && !!y && x.unit === y.unit && x.key !== y.key
}

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

/**
 * Full precision, for a tooltip, a table cell or the readout.
 *
 * Driven by the unit the server declared, so a metric added there formats
 * correctly here without this file knowing it exists.
 */
export function formatMetric(metric: MetricDto, value: number): string {
  switch (metric.unit) {
    case "money":
      return formatMoneyExact(value)
    case "rate":
      return `${formatMoneyExact(value)}/min`
    default:
      return formatDuration(value)
  }
}

/**
 * Abbreviated, for an axis tick.
 *
 * Durations get the coarser "45m" / "1h 20m" rather than the table's
 * "45m 00s": an axis label is read at a glance and the seconds are noise at
 * that size, while a table cell is read to be checked.
 */
export function formatMetricTick(metric: MetricDto, value: number): string {
  switch (metric.unit) {
    case "money":
      return formatMoney(value)
    case "rate":
      return `${formatMoney(value)}/min`
    default:
      return formatElapsed(value)
  }
}
