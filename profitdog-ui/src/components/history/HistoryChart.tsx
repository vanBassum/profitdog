import * as React from "react"

import type { HistoryBucket, HistoryResponse } from "@/lib/api"
import type { Grouping } from "@/lib/history"
import { formatMatchDuration } from "@/lib/history"
import {
  formatMoney,
  formatMoneyExact,
  formatSigned,
  formatSignedExact,
} from "@/lib/stats"
import { TERMS, unassignedTerm } from "@/lib/terms"
import { cn } from "@/lib/utils"

import {
  gutterFor,
  linearScale,
  niceTicks,
  useMeasuredSize,
} from "../viz/scale"

interface Props {
  buckets: HistoryBucket[]
  markers: HistoryResponse["markers"]
  grouping: Grouping
  selectedId: string | null
  onSelect: (key: string | null) => void
  /** Second activation: open the match, or drill into an aggregated bucket. */
  onOpen: (bucket: HistoryBucket) => void
  className?: string
}

const MARGIN = { top: 16, right: 64, bottom: 30, left: 56 }
/** Bands thinner than this stop being readable; the plot scrolls instead. */
const MIN_BAND = 26
/**
 * A pair wider than this reads as a block, not a measurement. Roughly double
 * the old single-bar cap, so each of the two keeps the weight one bar had.
 */
const MAX_BAR = 62
/** Hairline between the two bars of a pair. */
const BAR_GAP = 3
const CALLOUT_WIDTH = 224

/**
 * Two bars per match: what came in, and what went out.
 *
 * ## Why a pair, and not a stack
 *
 * This chart used to stack kit cost and other outflow below zero against
 * earnings above it, with a cumulative line on a second right-hand axis. Three
 * encodings, two axes, four colours — and the question anyone actually brings
 * to it is the simplest one there is: did I make more than I spent? Answering
 * that from a diverging stack means comparing a length above a line with the
 * sum of two segments below it, which the eye is bad at, while a second axis
 * in different units invites reading the line against the bars it crosses.
 *
 * Both bars now start at zero and grow upward, so the comparison is the one
 * thing the eye *is* good at: which rectangle is taller. Spent is the whole
 * outflow — kit cost plus other outflow — because that is the quantity being
 * compared against earnings. The split between the two is still a real
 * question, just a second one: it lives in the hover panel and the Matches
 * table, where a number can be read exactly rather than estimated.
 *
 * Profit sits above each pair as a signed figure. It is the difference between
 * the two bars, which the chart already shows; printing it as well means the
 * reader never has to estimate a subtraction from two lengths.
 *
 * Unassigned movement gets a small amber tick rather than a bar. It belongs to
 * no life and, as `ledger.ts` explains, to no explanation either — sizing it
 * like the other quantities would give it a weight in the comparison that the
 * data cannot support. It is marked so it cannot be missed, and named in the
 * hover panel.
 */
export function HistoryChart({
  buckets,
  markers,
  grouping,
  selectedId,
  onSelect,
  onOpen,
  className,
}: Props) {
  const [ref, { width, height }] = useMeasuredSize<HTMLDivElement>()
  const [hoverKey, setHoverKey] = React.useState<string | null>(null)

  const plotHeight = Math.max(height - MARGIN.top - MARGIN.bottom, 40)

  // One axis, in dollars, from zero. Both quantities are magnitudes now, so
  // nothing reaches below the baseline and the old signed domain would only
  // waste half the plot.
  const spentOf = (b: HistoryBucket) => b.kit_cost + b.other_outflow
  const barValues = buckets.flatMap((b) => [b.earned, spentOf(b)])
  const axisDomain: [number, number] = [0, Math.max(...barValues, 0)]
  const ticks = niceTicks(axisDomain[0], axisDomain[1], 4).filter(
    (t) => t >= axisDomain[0]
  )
  // Let the axis round up to its own top tick so the tallest bar does not end
  // flush against the frame with nothing to read it against.
  const yDomain: [number, number] = [
    0,
    Math.max(ticks[ticks.length - 1] ?? 1, 1),
  ]

  const margin = {
    ...MARGIN,
    left: gutterFor(ticks.map(formatMoney)),
    // No second axis to make room for — just enough that the last profit
    // label is not clipped.
    right: 24,
  }

  const available = Math.max(width - margin.left - margin.right, 10)
  // Bands share the full width rather than stacking up from the left. Capping
  // the *band* would leave three matches huddled in the corner of an empty
  // plot; capping only the bar keeps the spacing honest and the bars readable.
  const band = Math.max(available / Math.max(buckets.length, 1), MIN_BAND)
  // A long history is scrolled rather than squeezed: past a certain width a
  // bar is a hairline, and a hairline cannot be clicked, hovered or read.
  const plotWidth = Math.max(band * buckets.length, available)
  const svgWidth = plotWidth + margin.left + margin.right

  const y = linearScale(yDomain, [plotHeight, 0])
  const zeroY = y(0)
  // Two bars share the space one used to have, with a hairline between them so
  // the pair reads as a pair rather than as two neighbouring matches.
  const pairWidth = Math.min(band * 0.62, MAX_BAR)
  const barWidth = Math.max((pairWidth - BAR_GAP) / 2, 3)

  const centerOf = (index: number) => band * index + band / 2

  const activeKey = hoverKey ?? selectedId
  const active = buckets.find((b) => b.key === activeKey) ?? null
  const activeIndex = active ? buckets.indexOf(active) : -1

  const move = (delta: number) => {
    if (buckets.length === 0) return
    const from =
      activeIndex === -1 ? (delta > 0 ? -1 : buckets.length) : activeIndex
    const next = Math.min(Math.max(from + delta, 0), buckets.length - 1)
    onSelect(buckets[next].key)
  }

  return (
    <div className={cn("viz-root relative", className)} ref={ref}>
      <div className="h-full overflow-x-auto">
        <svg
          width={svgWidth}
          height={height}
          role="group"
          aria-label={`Earned against spent per ${grouping} across ${buckets.length} ${grouping === "match" ? "matches" : grouping + "s"}`}
          onKeyDown={(event) => {
            if (event.key === "ArrowRight") {
              event.preventDefault()
              move(1)
            } else if (event.key === "ArrowLeft") {
              event.preventDefault()
              move(-1)
            } else if (event.key === "Escape") {
              onSelect(null)
            }
          }}
        >
          <g transform={`translate(${margin.left},${margin.top})`}>
            {ticks.map((tick) =>
              tick === 0 ? null : (
                <line
                  key={`grid-${tick}`}
                  x1={0}
                  x2={plotWidth}
                  y1={y(tick)}
                  y2={y(tick)}
                  stroke="var(--viz-grid)"
                  strokeWidth={1}
                />
              )
            )}

            {/* Selection sits behind the data: it marks a column, it is not a
                value in it. */}
            {selectedId !== null &&
              buckets.map((bucket, index) =>
                bucket.key === selectedId ? (
                  <rect
                    key={`sel-${bucket.key}`}
                    x={band * index}
                    y={-MARGIN.top + 2}
                    width={band}
                    height={plotHeight + MARGIN.top - 2}
                    fill="var(--viz-positive)"
                    opacity={0.1}
                  />
                ) : null
              )}

            {markers.map((marker: HistoryResponse["markers"][number]) => (
              <g key={`marker-${marker.build}-${marker.bucket_index}`}>
                <line
                  x1={band * marker.bucket_index}
                  x2={band * marker.bucket_index}
                  y1={-6}
                  y2={plotHeight}
                  stroke="var(--viz-ink-muted)"
                  strokeWidth={1}
                  strokeDasharray="3 4"
                  opacity={0.5}
                />
                <text
                  x={band * marker.bucket_index + 5}
                  y={-4}
                  className="fill-[var(--viz-ink-muted)] text-[9px] tracking-wide"
                >
                  {marker.label}
                </text>
              </g>
            ))}

            {buckets.map((bucket, index) => {
              const center = centerOf(index)
              const spent = spentOf(bucket)
              const earnedTop = y(bucket.earned)
              const spentTop = y(spent)
              const left = center - pairWidth / 2

              return (
                <g
                  key={bucket.key}
                  tabIndex={0}
                  role="button"
                  aria-label={ariaLabelFor(bucket, grouping)}
                  aria-pressed={bucket.key === selectedId}
                  className="cursor-pointer outline-none"
                  onMouseEnter={() => setHoverKey(bucket.key)}
                  onMouseLeave={() => setHoverKey(null)}
                  onFocus={() => setHoverKey(bucket.key)}
                  onBlur={() => setHoverKey(null)}
                  onClick={() =>
                    bucket.key === selectedId
                      ? onOpen(bucket)
                      : onSelect(bucket.key)
                  }
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault()
                      if (bucket.key === selectedId) onOpen(bucket)
                      else onSelect(bucket.key)
                    }
                  }}
                >
                  {/* A full-height target: the bars themselves are too thin to
                      be a comfortable hit area, especially for a keyboard
                      focus ring or a trackpad. */}
                  <rect
                    x={band * index}
                    y={0}
                    width={band}
                    height={plotHeight}
                    fill="transparent"
                  />

                  {/* Earned left, Spent right, in the order the equation on
                      the band above reads them. */}
                  <rect
                    x={left}
                    y={earnedTop}
                    width={barWidth}
                    height={Math.max(
                      zeroY - earnedTop,
                      bucket.earned > 0 ? 1 : 0
                    )}
                    fill="var(--viz-positive)"
                    opacity={0.9}
                  />
                  <rect
                    x={left + barWidth + BAR_GAP}
                    y={spentTop}
                    width={barWidth}
                    height={Math.max(zeroY - spentTop, spent > 0 ? 1 : 0)}
                    fill="var(--viz-spend)"
                    opacity={0.9}
                  />

                  {/* Unassigned movement: marked, never sized. A bar would
                      give it a weight in the comparison that nothing in the
                      data supports — see this file's header. */}
                  {bucket.unassigned.total !== 0 && (
                    <rect
                      x={left}
                      y={Math.min(earnedTop, spentTop) - 9}
                      width={pairWidth}
                      height={2.5}
                      rx={1.25}
                      fill="var(--viz-adjust)"
                    />
                  )}

                  {/* The difference between the two bars, printed rather than
                      left to be estimated. */}
                  <text
                    x={center}
                    y={
                      Math.min(earnedTop, spentTop) -
                      (bucket.unassigned.total !== 0 ? 15 : 6)
                    }
                    textAnchor="middle"
                    className="text-[10px] font-semibold tabular-nums"
                    fill={
                      bucket.profit >= 0 ? "var(--viz-gain)" : "var(--viz-loss)"
                    }
                  >
                    {formatSigned(bucket.profit)}
                  </text>

                  {bucket.key === activeKey && (
                    <rect
                      x={band * index}
                      y={0}
                      width={band}
                      height={plotHeight}
                      fill="none"
                      stroke="var(--viz-ink-muted)"
                      strokeWidth={1}
                      opacity={0.35}
                    />
                  )}
                </g>
              )
            })}

            {/* The baseline both bars stand on. */}
            <line
              x1={0}
              x2={plotWidth}
              y1={zeroY}
              y2={zeroY}
              stroke="var(--viz-ink-muted)"
              strokeWidth={1}
            />

            {ticks.map((tick) => (
              <text
                key={`lt-${tick}`}
                x={-8}
                y={y(tick)}
                textAnchor="end"
                dominantBaseline="middle"
                className="fill-[var(--viz-ink-muted)] text-[10px] tabular-nums"
              >
                {formatMoney(tick)}
              </text>
            ))}

            {buckets.map((bucket, index) => {
              // Only label what will not collide: at a dense width every other
              // tick, and never one narrower than its own text.
              const step = Math.max(Math.ceil(52 / band), 1)
              if (index % step !== 0) return null
              return (
                <text
                  key={`xl-${bucket.key}`}
                  x={centerOf(index)}
                  y={plotHeight + 14}
                  textAnchor="middle"
                  className="fill-[var(--viz-ink-muted)] text-[10px]"
                >
                  {bucket.label}
                </text>
              )
            })}
          </g>
        </svg>
      </div>

      {/* Pinned to whichever top corner the active bar is not in. Following
          the bar instead put the panel on top of the data it describes, which
          is the one thing a tooltip must never do. */}
      {active && (
        <Callout
          bucket={active}
          grouping={grouping}
          style={{
            left:
              margin.left + centerOf(activeIndex) > width / 2
                ? 8
                : Math.max(width - CALLOUT_WIDTH - 8, 8),
            top: 4,
          }}
        />
      )}
    </div>
  )
}

function ariaLabelFor(bucket: HistoryBucket, grouping: Grouping): string {
  const what =
    grouping === "match"
      ? (bucket.map ?? "Unknown map")
      : `${bucket.matches} matches`
  return (
    `${bucket.label}, ${what}, ${bucket.lives} lives, ` +
    `earned ${formatMoneyExact(bucket.earned)}, ` +
    `spent ${formatMoneyExact(bucket.kit_cost + bucket.other_outflow)}, ` +
    `profit ${formatSignedExact(bucket.profit)}`
  )
}

/**
 * The bucket's figures, unrounded.
 *
 * The axes and the table abbreviate because they have to; this is where the
 * exact number lives, so nothing on the page is only ever available rounded.
 */
function Callout({
  bucket,
  grouping,
  style,
}: {
  bucket: HistoryBucket
  grouping: Grouping
  style: React.CSSProperties
}) {
  // A bucket holding exactly one match can name its clock time; an aggregate
  // cannot, and says how many it holds instead.
  const single = bucket.matches === 1

  return (
    <div
      className="pointer-events-none absolute z-10 rounded-md border bg-popover px-2.5 py-2 text-xs shadow-md"
      style={{ width: CALLOUT_WIDTH, ...style }}
    >
      <div className="flex items-baseline justify-between gap-2">
        <span className="font-medium">
          {single
            ? new Date(bucket.started_at).toLocaleString(undefined, {
                month: "short",
                day: "numeric",
                hour: "2-digit",
                minute: "2-digit",
              })
            : bucket.label}
        </span>
        <span className="text-muted-foreground">
          {grouping === "match"
            ? (bucket.map ?? "—")
            : `${bucket.matches} matches`}
        </span>
      </div>

      <dl className="mt-1.5 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 border-t pt-1.5 tabular-nums">
        <Row label="Lives">{bucket.lives}</Row>
        <Row label={TERMS.earned}>{formatMoneyExact(bucket.earned)}</Row>
        {/* Spent is the bar; the two rows under it are the split the chart
            deliberately does not draw, which is the whole reason this panel
            still carries it. */}
        <Row label={TERMS.spent} color="var(--viz-spend)">
          {formatMoneyExact(bucket.kit_cost + bucket.other_outflow)}
        </Row>
        <Row label={`· ${TERMS.kitCost}`} color="var(--viz-kit)">
          {formatMoneyExact(bucket.kit_cost)}
        </Row>
        <Row label={`· ${TERMS.otherOutflow}`} color="var(--viz-spend)">
          {bucket.other_outflow === 0 ? "—" : formatMoneyExact(bucket.other_outflow)}
        </Row>
        <Row
          label={unassignedTerm(bucket.unassigned).label}
          color="var(--viz-adjust)"
        >
          {formatSignedExact(bucket.unassigned.total)}
        </Row>
        <Row
          label={TERMS.profit}
          color={bucket.profit >= 0 ? "var(--viz-gain)" : "var(--viz-loss)"}
        >
          {formatSignedExact(bucket.profit)}
        </Row>
        <Row label="Build">{bucket.build ? `CL ${bucket.build}` : "—"}</Row>
        {single && (
          <Row label="Duration">
            {formatMatchDuration(bucket.duration_sec)}
          </Row>
        )}
      </dl>
    </div>
  )
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

/** Small, and immediately under the plot, where it is read once and ignored. */
export function ChartLegend({ className }: { className?: string }) {
  return (
    <div
      className={cn(
        "viz-root flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-muted-foreground",
        className
      )}
    >
      {/* Two swatches, because there are two bars. The kit/other split is a
          different question and lives in the hover panel and the table. */}
      <Swatch color="var(--viz-positive)">{TERMS.earned}</Swatch>
      <Swatch color="var(--viz-spend)">
        {TERMS.spent}
        <span className="opacity-70"> (kits + other outflow)</span>
      </Swatch>
      <span className="flex items-center gap-1.5">
        <svg width="14" height="8" aria-hidden>
          <rect
            x="0"
            y="3"
            width="14"
            height="2.5"
            rx="1.25"
            fill="var(--viz-adjust)"
          />
        </svg>
        {TERMS.unassignedIncome}
      </span>
    </div>
  )
}

function Swatch({
  color,
  children,
}: {
  color: string
  children: React.ReactNode
}) {
  return (
    <span className="flex items-center gap-1.5">
      <span
        aria-hidden
        className="size-2.5 rounded-[3px]"
        style={{ background: color }}
      />
      {children}
    </span>
  )
}
