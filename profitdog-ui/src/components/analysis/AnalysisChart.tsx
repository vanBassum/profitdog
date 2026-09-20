import * as React from "react"

import type { AnalysisPoint, AnalysisResponse, MetricDto } from "@/lib/api"
import { formatMatchDuration } from "@/lib/history"
import { formatMetric, formatMetricTick } from "@/lib/metrics"
import {
  formatDuration,
  formatMoneyExact,
  formatSignedExact,
} from "@/lib/stats"
import { TERMS } from "@/lib/terms"
import { cn } from "@/lib/utils"

import {
  gutterFor,
  linearScale,
  niceTicks,
  niceTimeTicks,
  useMeasuredSize,
} from "../viz/scale"

interface Props {
  /** One pairing, exactly as the server answered it. */
  result: AnalysisResponse
  showTrend: boolean
  selectedId: string | null
  onSelect: (id: string | null) => void
  hoverId: string | null
  onHover: (id: string | null) => void
  className?: string
}

const MARGIN = { top: 16, right: 22, bottom: 44, left: 56 }
const DOT_RADIUS = 4.5
const CALLOUT_WIDTH = 236

/**
 * One dot per life, on whichever two metrics are selected.
 *
 * ## Colour says profitable, position says everything else
 *
 * A dot is green when its life kept money and red when it did not, on every
 * pairing — including the ones where neither axis is profit. That is the point:
 * it is the one reading that stays constant as the axes change, so a cloud of
 * red in the top-right of "kit cost against earnings" is immediately legible as
 * expensive lives that earned well and still lost money, which neither axis
 * alone would have said.
 *
 * ## Both axes are anchored at zero
 *
 * Not for tidiness — it is what makes the `x = y` line exact. Both scales are
 * linear and both include zero, so the locus of `x == y` is a straight line
 * through the origin whatever the two ranges are; it simply is not drawn at 45
 * degrees when the ranges differ. Which side of it a dot falls on, the only
 * thing that line is for, is unaffected by that. A shared domain would put it
 * at 45 degrees and squeeze every dot into one corner, since earnings routinely
 * run to several times the dearest kit.
 *
 * The line is only drawn when the two axes are in the same unit — see
 * `isComparable`. Dollars against seconds have no diagonal.
 *
 * ## The trend line is drawn faintly on purpose
 *
 * A least-squares fit through eleven points is a description, not a prediction,
 * and a confident-looking line invites reading it as the latter. It is thin,
 * dashed and dim so it reads as "roughly, this way" — and it is clipped to the
 * range actually observed, because extending it past the cheapest and dearest
 * kit on record would be extrapolating from nothing.
 */
export function AnalysisChart({
  result,
  showTrend,
  selectedId,
  onSelect,
  hoverId,
  onHover,
  className,
}: Props) {
  const points = result.points
  const xMetric = result.x
  const yMetric = result.y
  const correlation = result.correlation
  const [ref, { width, height }] = useMeasuredSize<HTMLDivElement>()

  const plotHeight = Math.max(height - MARGIN.top - MARGIN.bottom, 40)

  const xTicks = ticksFor(
    xMetric,
    points.map((p) => p.x)
  )
  const yTicks = ticksFor(
    yMetric,
    points.map((p) => p.y)
  )
  const [xLo, xHi] = boundsOf(
    xTicks,
    points.map((p) => p.x)
  )
  const [yLo, yHi] = boundsOf(
    yTicks,
    points.map((p) => p.y)
  )

  const margin = {
    ...MARGIN,
    // Room for the tick labels *and* the rotated axis title beside them.
    left: gutterFor(yTicks.map((t) => formatMetricTick(yMetric, t))) + 14,
  }
  const plotWidth = Math.max(width - margin.left - margin.right, 10)

  const x = linearScale([xLo, xHi], [0, plotWidth])
  const y = linearScale([yLo, yHi], [plotHeight, 0])

  // Where `x == y` enters and leaves the plot: the overlap of the two domains,
  // which is empty when they do not overlap at all.
  // The server decides whether an `x = y` line means anything for this pair;
  // the chart only decides where it would go.
  const diagonal = result.comparable ? overlap([xLo, xHi], [yLo, yHi]) : null

  const active = points.find((p) => p.id === (hoverId ?? selectedId)) ?? null

  const trendPath = React.useMemo(() => {
    if (!showTrend || !correlation.trend || points.length < 2) return null
    const xs = points.map((p) => p.x)
    const lo = Math.min(...xs)
    const hi = Math.max(...xs)
    if (lo === hi) return null
    const { slope, intercept } = correlation.trend
    const at = (value: number) => `${x(value)} ${y(slope * value + intercept)}`
    return `M ${at(lo)} L ${at(hi)}`
  }, [showTrend, correlation.trend, points, x, y])

  return (
    <div className={cn("viz-root relative", className)} ref={ref}>
      <svg
        width="100%"
        height={height}
        role="group"
        aria-label={`${yMetric.label} against ${xMetric.label} for ${points.length} lives`}
        onMouseLeave={() => onHover(null)}
      >
        <g transform={`translate(${margin.left},${margin.top})`}>
          {yTicks.map((tick) => (
            <g key={`gy-${tick}`}>
              <line
                x1={0}
                x2={plotWidth}
                y1={y(tick)}
                y2={y(tick)}
                stroke="var(--viz-grid)"
                strokeWidth={1}
              />
              <text
                x={-8}
                y={y(tick)}
                textAnchor="end"
                dominantBaseline="middle"
                className="fill-[var(--viz-ink-muted)] text-[10px] tabular-nums"
              >
                {formatMetricTick(yMetric, tick)}
              </text>
            </g>
          ))}

          {xTicks.map((tick, index) => (
            <text
              key={`gx-${tick}`}
              x={x(tick)}
              y={plotHeight + 14}
              /* The last label would hang off the right edge if it were
                 centred on its own tick, so it ends there instead. */
              textAnchor={index === xTicks.length - 1 ? "end" : "middle"}
              className="fill-[var(--viz-ink-muted)] text-[10px] tabular-nums"
            >
              {formatMetricTick(xMetric, tick)}
            </text>
          ))}

          {/* Zero, drawn solid where an axis crosses it. Profit and profit/min
              run negative, and "which dots are below nothing" is not a fact a
              reader should have to work out from the tick labels. */}
          {yLo < 0 && yHi > 0 && (
            <line
              x1={0}
              x2={plotWidth}
              y1={y(0)}
              y2={y(0)}
              stroke="var(--viz-ink-muted)"
              strokeWidth={1}
              opacity={0.55}
            />
          )}
          {xLo < 0 && xHi > 0 && (
            <line
              x1={x(0)}
              x2={x(0)}
              y1={0}
              y2={plotHeight}
              stroke="var(--viz-ink-muted)"
              strokeWidth={1}
              opacity={0.55}
            />
          )}

          {/* x = y. Drawn under the dots so it never hides one. */}
          {diagonal && (
            <>
              <line
                x1={x(diagonal[0])}
                y1={y(diagonal[0])}
                x2={x(diagonal[1])}
                y2={y(diagonal[1])}
                stroke="var(--viz-ink-muted)"
                strokeWidth={1.25}
                strokeDasharray="5 4"
                opacity={0.8}
              />
              {/* Sits clear of the line rather than on it: when the diagonal
                  runs all the way to the right edge there is no room to the
                  right of it, so the label backs up over the plot instead. */}
              <text
                x={
                  x(diagonal[1]) > plotWidth - 92
                    ? x(diagonal[1]) - 5
                    : x(diagonal[1]) + 6
                }
                y={y(diagonal[1]) - 8}
                textAnchor={x(diagonal[1]) > plotWidth - 92 ? "end" : "start"}
                className="fill-[var(--viz-ink-muted)] text-[10px]"
              >
                {`${yMetric.label.toLowerCase()} = ${xMetric.label.toLowerCase()}`}
              </text>
            </>
          )}

          {trendPath && (
            <path
              d={trendPath}
              fill="none"
              stroke="var(--viz-ink-muted)"
              strokeWidth={1.5}
              strokeDasharray="2 4"
              opacity={0.55}
            />
          )}

          {points.map((point) => {
            const profitable = point.profit >= 0
            const isActive = point.id === active?.id
            const isSelected = point.id === selectedId
            return (
              <circle
                key={point.id}
                cx={x(point.x)}
                cy={y(point.y)}
                r={isActive ? DOT_RADIUS + 2 : DOT_RADIUS}
                fill={profitable ? "var(--viz-gain)" : "var(--viz-loss)"}
                fillOpacity={isActive ? 1 : 0.82}
                stroke={isSelected ? "var(--viz-ink)" : "var(--viz-surface)"}
                strokeWidth={isSelected ? 1.75 : 1}
                tabIndex={0}
                role="img"
                aria-label={ariaLabelFor(point, xMetric, yMetric)}
                className="cursor-pointer outline-none"
                onMouseEnter={() => onHover(point.id)}
                onMouseLeave={() => onHover(null)}
                onFocus={() => onHover(point.id)}
                onBlur={() => onHover(null)}
                onClick={() => onSelect(isSelected ? null : point.id)}
              />
            )
          })}

          <text
            x={plotWidth / 2}
            y={plotHeight + 34}
            textAnchor="middle"
            className="fill-[var(--viz-ink-muted)] text-[11px]"
          >
            {xMetric.label}
          </text>
          <text
            transform={`translate(${-margin.left + 12},${plotHeight / 2}) rotate(-90)`}
            textAnchor="middle"
            className="fill-[var(--viz-ink-muted)] text-[11px]"
          >
            {yMetric.label}
          </text>
        </g>
      </svg>

      {active && (
        <LifeCallout
          point={active}
          x={xMetric}
          y={yMetric}
          style={{
            left:
              margin.left + x(active.x) > width / 2
                ? 8
                : Math.max(width - CALLOUT_WIDTH - 8, 8),
            top: 4,
          }}
        />
      )}
    </div>
  )
}

/**
 * Ticks for one axis, anchored at zero.
 *
 * Durations get their own ladder: `niceTicks` works in powers of ten, which is
 * right for money and produces boundaries like 20000s for time — rendering as
 * "5h 33m", which is not a number anyone thinks in.
 */
function ticksFor(metric: MetricDto, values: number[]): number[] {
  const max = Math.max(...values, 0)
  if (metric.unit === "duration") return niceTimeTicks(max, 5)
  const min = Math.min(...values, 0)
  if (min === max) return [0, max || 1]
  return niceTicks(min, max, 5)
}

/** The domain the ticks and the data between them need, never collapsed. */
function boundsOf(ticks: number[], values: number[]): [number, number] {
  const lo = Math.min(ticks[0] ?? 0, ...values, 0)
  const hi = Math.max(ticks[ticks.length - 1] ?? 0, ...values, 0)
  // A single value at zero would otherwise give a zero-width domain, which
  // `linearScale` can only answer by putting everything in the middle.
  return hi === lo ? [lo, lo + 1] : [lo, hi]
}

/** The span both axes cover, or null when they do not overlap. */
function overlap(
  a: [number, number],
  b: [number, number]
): [number, number] | null {
  const lo = Math.max(a[0], b[0])
  const hi = Math.min(a[1], b[1])
  return hi > lo ? [lo, hi] : null
}

function ariaLabelFor(point: AnalysisPoint, x: MetricDto, y: MetricDto): string {
  return (
    `${point.map_display ?? "Unknown map"} life ${point.life_number}: ` +
    `${x.label} ${formatMetric(x, point.x)}, ` +
    `${y.label} ${formatMetric(y, point.y)}, ` +
    `profit ${formatSignedExact(point.profit)}`
  )
}

/** Everything about one life, at full precision. */
function LifeCallout({
  point,
  x,
  y,
  style,
}: {
  point: AnalysisPoint
  x: MetricDto
  y: MetricDto
  style: React.CSSProperties
}) {
  const life = point
  const when = new Date(life.started_at ?? life.match_started_at)

  return (
    <div
      className="pointer-events-none absolute z-10 rounded-md border bg-popover px-2.5 py-2 text-xs shadow-md"
      style={{ width: CALLOUT_WIDTH, ...style }}
    >
      <div className="flex items-baseline justify-between gap-2">
        <span className="font-medium tabular-nums">
          {when.toLocaleString(undefined, {
            month: "short",
            day: "numeric",
            hour: "2-digit",
            minute: "2-digit",
          })}
        </span>
        <span className="text-muted-foreground">{life.map_display ?? "—"}</span>
      </div>

      <dl className="mt-1.5 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 border-t pt-1.5 tabular-nums">
        <Row label="Life">{life.life_number}</Row>
        <Row label={TERMS.kitCost} color="var(--viz-kit)">
          {/* Never $0: a life whose kit price could not be read did not get a
              free kit, and the two must not look alike. */}
          {life.has_kit ? (
            formatMoneyExact(life.kit_cost)
          ) : (
            <span className="text-muted-foreground">—</span>
          )}
        </Row>
        <Row label={TERMS.earned}>{formatMoneyExact(life.earned)}</Row>
        <Row label={TERMS.otherOutflow} color="var(--viz-spend)">
          {life.other_outflow === 0
            ? "—"
            : `−${formatMoneyExact(life.other_outflow)}`}
        </Row>
        <Row
          label={TERMS.profit}
          color={life.profit >= 0 ? "var(--viz-gain)" : "var(--viz-loss)"}
        >
          {formatSignedExact(life.profit)}
        </Row>
        <Row label="Length">
          {life.duration_sec >= 3600
            ? formatMatchDuration(life.duration_sec)
            : formatDuration(life.duration_sec)}
        </Row>
        {/* The two plotted values, when they are not already in the list above
            — otherwise the dot's own position is the one thing the tooltip
            does not state. */}
        {extraRows(point, x, y).map((row) => (
          <Row key={row.label} label={row.label}>
            {row.value}
          </Row>
        ))}
      </dl>
    </div>
  )
}

/** Plotted values the fixed rows above do not already show. */
function extraRows(
  point: AnalysisPoint,
  x: MetricDto,
  y: MetricDto
): { label: string; value: string }[] {
  const shown = new Set(["kitCost", "earned", "otherOutflow", "profit", "lifeLength"])
  const rows: { label: string; value: string }[] = []
  if (!shown.has(x.key)) {
    rows.push({ label: x.label, value: formatMetric(x, point.x) })
  }
  if (!shown.has(y.key) && y.key !== x.key) {
    rows.push({ label: y.label, value: formatMetric(y, point.y) })
  }
  return rows
}

function Row({
  label,
  color,
  children,
}: {
  label: string
  color?: string
  children: React.ReactNode
}) {
  return (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="text-right" style={color ? { color } : undefined}>
        {children}
      </dd>
    </>
  )
}
