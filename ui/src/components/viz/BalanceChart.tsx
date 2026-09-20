import * as React from "react"

import type {
  AdjustmentMark,
  ChartAnnotations,
  LifeAnnotation,
  ServerSession,
  SpendMark,
} from "@/lib/api"
import { buildLabelCandidates, labelClass } from "@/lib/chart-labels"
import type { PlacedLabel } from "@/lib/labels"
import { placeLabels } from "@/lib/labels"
import {
  adjustmentLabel,
  formatElapsed,
  formatLifeElapsed,
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
  niceTimeTicks,
  paddedDomain,
  useMeasuredSize,
} from "./scale"

interface Props {
  session: ServerSession
  annotations: ChartAnnotations
  isLive?: boolean
  /**
   * The feed's last-poll timestamp, used as the clock for extending a live
   * line out to the present. Passed in rather than read from `Date.now()`
   * during render, which would be impure and could disagree between renders.
   */
  now?: Date | null
  className?: string
}

// Tight but not cramped: enough head-room for the L labels and a kit figure,
// and just enough below for the tick row plus its title.
const BASE_MARGIN = { top: 18, right: 72, bottom: 32, left: 64 }
/** Below this the inline annotation text is dropped before the chart is. */
const NARROW = 560

/**
 * Readings this far apart mean the tracker was not sampling in between, so
 * there is nothing to interpolate: anything drawn across the gap would be
 * invented. Comfortably above the tracker's 2s poll, below the cadence of its
 * older write-on-change mode.
 */
const MAX_SMOOTH_GAP_SEC = 6

/**
 * A movement must be at least this tall on screen to get its own mark, and
 * this much taller again to get a caption.
 *
 * The rule is deliberately in pixels rather than dollars. The requirement is
 * that nothing *visible* on the curve is unexplained, and what is visible
 * depends on the plot's height and the range it is showing, not on the size of
 * the number. A $5 outgoing that occupies a third of a pixel needs no label;
 * the same $5 on a flat, tightly-zoomed session would, and gets one.
 */
const MARK_MIN_PX = 3
const CAPTION_MIN_PX = 9

/** How close to the cursor an event has to be to appear in the tooltip. */
const HOVER_WINDOW_SEC = 15

/**
 * Monotone cubic tangents (Fritsch–Carlson).
 *
 * The limiter is the whole reason to use this rather than a plain spline: an
 * ordinary cubic overshoots around a sharp move, so a kit purchase would draw
 * a dip below the price actually paid and a payout a peak above the money
 * actually held. Monotone interpolation cannot leave the range of the
 * surrounding readings, so the curve is smooth but never fictional.
 */
function monotoneTangents(xs: number[], ys: number[]): number[] {
  const n = xs.length
  if (n < 2) return [0]

  const secant = new Array<number>(n - 1)
  for (let i = 0; i < n - 1; i++) {
    const dx = xs[i + 1] - xs[i]
    secant[i] = dx === 0 ? 0 : (ys[i + 1] - ys[i]) / dx
  }

  const m = new Array<number>(n)
  m[0] = secant[0]
  m[n - 1] = secant[n - 2]
  for (let i = 1; i < n - 1; i++) {
    m[i] = secant[i - 1] * secant[i] <= 0 ? 0 : (secant[i - 1] + secant[i]) / 2
  }

  for (let i = 0; i < n - 1; i++) {
    if (secant[i] === 0) {
      m[i] = 0
      m[i + 1] = 0
      continue
    }
    const a = m[i] / secant[i]
    const b = m[i + 1] / secant[i]
    const sum = a * a + b * b
    if (sum > 9) {
      const t = 3 / Math.sqrt(sum)
      m[i] = t * a * secant[i]
      m[i + 1] = t * b * secant[i]
    }
  }
  return m
}

/**
 * A smooth curve where the tracker really sampled, a held step where it did
 * not. At a 2s tick the money genuinely moved through those values; across a
 * longer gap it did not, and curving through would redraw a gradual climb
 * that never happened.
 */
function curvePath(
  points: ServerSession["points"][number][],
  x: (v: number) => number,
  y: (v: number) => number
): string {
  if (points.length === 0) return ""

  const runs: ServerSession["points"][number][][] = []
  let run: ServerSession["points"][number][] = [points[0]]
  for (let i = 1; i < points.length; i++) {
    if (points[i].elapsedSec - points[i - 1].elapsedSec > MAX_SMOOTH_GAP_SEC) {
      runs.push(run)
      run = [points[i]]
    } else {
      run.push(points[i])
    }
  }
  runs.push(run)

  const parts: string[] = []
  let previous: ServerSession["points"][number] | null = null

  for (const segment of runs) {
    const head = segment[0]
    if (previous === null) {
      parts.push(`M ${x(head.elapsedSec)} ${y(head.value)}`)
    } else {
      parts.push(`L ${x(head.elapsedSec)} ${y(previous.value)}`)
      parts.push(`L ${x(head.elapsedSec)} ${y(head.value)}`)
    }

    if (segment.length > 1) {
      const xs = segment.map((p) => x(p.elapsedSec))
      const ys = segment.map((p) => y(p.value))
      const m = monotoneTangents(xs, ys)
      for (let i = 0; i < segment.length - 1; i++) {
        const dx = (xs[i + 1] - xs[i]) / 3
        parts.push(
          `C ${xs[i] + dx} ${ys[i] + m[i] * dx} ${xs[i + 1] - dx} ${ys[i + 1] - m[i + 1] * dx} ${xs[i + 1]} ${ys[i + 1]}`
        )
      }
    }

    previous = segment[segment.length - 1]
  }

  return parts.join(" ")
}

export function BalanceChart({
  session,
  annotations,
  isLive = false,
  now = null,
  className,
}: Props) {
  const [ref, { width, height }] = useMeasuredSize<HTMLDivElement>()
  const [hoverIndex, setHoverIndex] = React.useState<number | null>(null)

  const narrow = width < NARROW
  const lives = annotations.lives
  const adjustments = annotations.adjustments

  // While a session is live the line runs out to *now*, so a quiet spell reads
  // as flat money rather than a frozen chart.
  const recorded = session.points
  const lastRecorded = recorded[recorded.length - 1]
  const nowSec =
    isLive && now && session.startedAt
      ? (now.getTime() - session.startedAt.getTime()) / 1000
      : null
  const points: ServerSession["points"][number][] =
    nowSec !== null && lastRecorded && nowSec > lastRecorded.elapsedSec + 1
      ? [...recorded, { ...lastRecorded, elapsedSec: nowSec }]
      : recorded

  const xDomain: [number, number] = [
    0,
    Math.max(points[points.length - 1]?.elapsedSec ?? 1, 1),
  ]
  const yDomain = paddedDomain(
    points.map((p) => p.value),
    true
  )

  const plotHeight = Math.max(height - BASE_MARGIN.top - BASE_MARGIN.bottom, 40)
  const yTicks = niceTicks(yDomain[0], yDomain[1], 5).filter(
    (t) => t >= yDomain[0] && t <= yDomain[1]
  )

  const MARGIN = {
    ...BASE_MARGIN,
    left: gutterFor(yTicks.map(formatMoney)),
    right: narrow ? 16 : BASE_MARGIN.right,
  }
  const plotWidth = Math.max(width - MARGIN.left - MARGIN.right, 10)

  const x = linearScale(xDomain, [0, plotWidth])
  const y = linearScale(yDomain, [plotHeight, 0])
  const xTicks = niceTimeTicks(xDomain[1], narrow ? 4 : 6)

  // Named so the placement memo can depend on them by identity. The scales
  // themselves are rebuilt every render and would defeat memoisation.
  const [yMin, yMax] = yDomain
  const xMax = xDomain[1]

  const path = curvePath(points, x, y)
  const last = points[points.length - 1]
  const zeroY = y(0)
  const showsUnderwater = yDomain[0] < 0

  /**
   * Every caption on the plot, resolved against every other one.
   *
   * Building the candidates in one list is what makes this work: the previous
   * version resolved life labels against life labels and kit prices against
   * kit prices, in separate passes, so a kit price and a recovery caption
   * could still land on each other. Priorities encode what must survive —
   * a life's identity first, then what its kit cost, then whether it paid for
   * itself, then the explanatory captions.
   */
  const placed = React.useMemo(() => {
    if (narrow) return new Map<string, PlacedLabel>()

    const candidates = buildLabelCandidates({
      lives,
      adjustments,
      x,
      y,
      captionMinPx: CAPTION_MIN_PX,
    })

    // The endpoint figure is drawn unconditionally, so nothing may be placed
    // on top of it.
    const reserved = last
      ? [
          {
            left: x(last.elapsedSec),
            right: plotWidth + MARGIN.right,
            top: y(last.value) - 14,
            bottom: y(last.value) + 8,
          },
        ]
      : []

    return placeLabels(candidates, {
      minY: 0,
      maxY: plotHeight - 2,
      reserved,
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lives, adjustments, plotWidth, plotHeight, xMax, yMin, yMax, narrow])

  const hovered = hoverIndex === null ? null : (points[hoverIndex] ?? null)
  const hoveredLife = hovered
    ? (lives.find(
        (a) =>
          hovered.elapsedSec >= a.lifeStartSec &&
          hovered.elapsedSec <= a.lifeEndSec
      ) ?? null)
    : null
  const hoveredSpends = hovered
    ? lives
        .flatMap((a) => a.spends)
        .filter(
          (s) => Math.abs(s.atSec - hovered.elapsedSec) <= HOVER_WINDOW_SEC
        )
    : []
  const hoveredAdjustments = hovered
    ? adjustments.filter(
        (m) =>
          hovered.elapsedSec >= m.adjustment.from_sec - HOVER_WINDOW_SEC &&
          hovered.elapsedSec <= m.adjustment.to_sec + HOVER_WINDOW_SEC
      )
    : []

  const handleMove = (event: React.MouseEvent<SVGRectElement>) => {
    const bounds = event.currentTarget.getBoundingClientRect()
    const localX = event.clientX - bounds.left
    const target =
      xDomain[0] + (localX / Math.max(plotWidth, 1)) * (xDomain[1] - xDomain[0])

    let best = 0
    let bestGap = Infinity
    for (let i = 0; i < points.length; i++) {
      const gap = Math.abs(points[i].elapsedSec - target)
      if (gap < bestGap) {
        bestGap = gap
        best = i
      }
    }
    setHoverIndex(best)
  }

  return (
    <div className={cn("viz-root relative", className)} ref={ref}>
      <svg
        width={width}
        height={height}
        role="img"
        aria-label={`Balance on this server over time, ending at ${formatSigned(session.final)}, with ${lives.length} kit purchases and ${adjustments.length} unassigned movements marked`}
      >
        <g transform={`translate(${MARGIN.left},${MARGIN.top})`}>
          {showsUnderwater && (
            <rect
              x={0}
              y={zeroY}
              width={plotWidth}
              height={Math.max(plotHeight - zeroY, 0)}
              fill="var(--viz-underwater)"
            />
          )}

          {yTicks.map((tick) =>
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

          {/* The zero rule carries the judgement, so it sits a shade above the
              rest of the grid rather than blending into it. */}
          <line
            x1={0}
            x2={plotWidth}
            y1={zeroY}
            y2={zeroY}
            stroke="var(--viz-ink-muted)"
            strokeWidth={1}
          />

          {yTicks.map((tick) => (
            <text
              key={`ylab-${tick}`}
              x={-10}
              y={y(tick)}
              textAnchor="end"
              dominantBaseline="middle"
              className="fill-[var(--viz-ink-muted)] text-[11px] tabular-nums"
            >
              {formatMoney(tick)}
            </text>
          ))}

          {xTicks.map((tick) => (
            <text
              key={`xlab-${tick}`}
              x={x(tick)}
              y={plotHeight + 14}
              textAnchor="middle"
              className="fill-[var(--viz-ink-muted)] text-[11px] tabular-nums"
            >
              {formatElapsed(tick)}
            </text>
          ))}

          <text
            x={plotWidth / 2}
            y={plotHeight + 28}
            textAnchor="middle"
            className="fill-[var(--viz-ink-muted)] text-[11px]"
          >
            Session time
          </text>

          {/* Map boundaries sit behind everything: context, not content. */}
          {adjustments
            .filter((m) => m.isMapBoundary)
            .map((m) => (
              <g key={`boundary-${m.id}`}>
                <line
                  x1={x(m.adjustment.to_sec)}
                  x2={x(m.adjustment.to_sec)}
                  y1={0}
                  y2={plotHeight}
                  stroke="var(--viz-ink-muted)"
                  strokeWidth={1}
                  strokeDasharray="2 5"
                  opacity={0.45}
                />
                {!narrow && (
                  <text
                    x={x(m.adjustment.to_sec) + 5}
                    y={10}
                    className="fill-[var(--viz-ink-muted)] text-[9px] tracking-wide uppercase"
                  >
                    {m.adjustment.next_map}
                  </text>
                )}
              </g>
            ))}

          <path
            d={path}
            fill="none"
            stroke="var(--viz-positive)"
            strokeWidth={2}
            strokeLinejoin="round"
            strokeLinecap="round"
          />

          {/* Geometry first, captions second. A mark carries meaning on its
              own — a recovery dot says "this life paid for itself" whether or
              not there was room to write it out — so marks are never dropped
              for want of space to label them. */}
          {lives.map((a) => (
            <LifeGeometry
              key={`geo-${a.lifeNumber}`}
              annotation={a}
              x={x}
              y={y}
            />
          ))}

          {adjustments.map((m) => (
            <AdjustmentGeometry key={`adjgeo-${m.id}`} mark={m} x={x} y={y} />
          ))}

          {!narrow &&
            Array.from(placed.values())
              .filter((label) => !label.hidden)
              .map((label) => (
                <text
                  key={label.id}
                  x={label.x}
                  y={label.y}
                  textAnchor={label.anchor}
                  className={cn(
                    "tabular-nums",
                    labelClass(label.id),
                    label.id.startsWith("life-")
                      ? "text-[11px] font-semibold"
                      : "text-[10px]"
                  )}
                >
                  {label.text}
                </text>
              ))}

          {last && !narrow && (
            <>
              {isLive && (
                <circle
                  cx={x(last.elapsedSec)}
                  cy={y(last.value)}
                  r={4}
                  fill="var(--viz-positive)"
                  opacity={0.35}
                >
                  <animate
                    attributeName="r"
                    values="4;10;4"
                    dur="2s"
                    repeatCount="indefinite"
                  />
                  <animate
                    attributeName="opacity"
                    values="0.35;0;0.35"
                    dur="2s"
                    repeatCount="indefinite"
                  />
                </circle>
              )}
              <circle
                cx={x(last.elapsedSec)}
                cy={y(last.value)}
                r={4}
                fill="var(--viz-positive)"
                stroke="var(--viz-surface)"
                strokeWidth={2}
              />
              <text
                x={x(last.elapsedSec) + 10}
                y={y(last.value)}
                dominantBaseline="middle"
                className="fill-[var(--viz-ink)] text-[12px] font-semibold tabular-nums"
              >
                {formatSigned(session.final)}
              </text>
            </>
          )}

          {hovered && (
            <>
              <line
                x1={x(hovered.elapsedSec)}
                x2={x(hovered.elapsedSec)}
                y1={0}
                y2={plotHeight}
                stroke="var(--viz-ink-muted)"
                strokeWidth={1}
              />
              <circle
                cx={x(hovered.elapsedSec)}
                cy={y(hovered.value)}
                r={4.5}
                fill="var(--viz-positive)"
                stroke="var(--viz-surface)"
                strokeWidth={2}
              />
            </>
          )}

          <rect
            x={0}
            y={0}
            width={plotWidth}
            height={plotHeight}
            fill="transparent"
            onMouseMove={handleMove}
            onMouseLeave={() => setHoverIndex(null)}
          />
        </g>
      </svg>

      {hovered && (
        <div
          className="pointer-events-none absolute z-10 max-w-[280px] rounded-md border bg-popover px-2.5 py-2 text-xs shadow-md"
          style={{
            left: Math.min(
              Math.max(MARGIN.left + x(hovered.elapsedSec) + 12, 8),
              Math.max(width - 292, 8)
            ),
            top: MARGIN.top,
          }}
        >
          <div className="font-medium tabular-nums">
            {formatSignedExact(hovered.value)}
          </div>
          <div className="mt-0.5 text-muted-foreground">
            {formatElapsed(hovered.elapsedSec)} in · life {hovered.life}
          </div>

          {/* Everything a label may have had to give up for space lives here,
              so crowding costs convenience and never information. */}
          {hoveredLife && hoveredLife.hasKit && (
            <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 border-t pt-1.5 tabular-nums">
              <dt className="text-muted-foreground">{TERMS.kitCost}</dt>
              <dd className="text-right">
                {formatMoneyExact(hoveredLife.invested)}
              </dd>
              <dt className="text-muted-foreground">{TERMS.earned}</dt>
              <dd className="text-right">
                {formatMoneyExact(hoveredLife.life.earned)}
              </dd>
              {hoveredLife.life.other_outflow > 0 && (
                <>
                  <dt className="text-muted-foreground">
                    {TERMS.otherOutflow}
                  </dt>
                  <dd
                    className="text-right"
                    style={{ color: "var(--viz-spend)" }}
                  >
                    −{formatMoneyExact(hoveredLife.life.other_outflow)}
                  </dd>
                </>
              )}
              <dt className="text-muted-foreground">{TERMS.profit}</dt>
              <dd
                className="text-right font-medium"
                style={{
                  color:
                    hoveredLife.life.profit >= 0
                      ? "var(--viz-gain)"
                      : "var(--viz-loss)",
                }}
              >
                {formatSignedExact(hoveredLife.life.profit)}
              </dd>
              <dt className="text-muted-foreground">{TERMS.breakEven}</dt>
              <dd className="text-right">
                {hoveredLife.breakEvenInLifeSec === null
                  ? "Not recovered"
                  : formatLifeElapsed(hoveredLife.breakEvenInLifeSec)}
              </dd>
            </dl>
          )}

          {hoveredSpends.map((spend) => (
            <div
              key={spend.id}
              className="mt-2 border-t pt-1.5"
              style={{ color: "var(--viz-spend)" }}
            >
              <div className="font-medium tabular-nums">
                {TERMS.otherOutflow} −{formatMoneyExact(spend.amount)}
              </div>
              <div className="text-muted-foreground">
                {formatLifeElapsed(spend.afterKitSec)} after the kit — too
                late to be part of it, so it is counted separately
              </div>
            </div>
          ))}

          {hoveredAdjustments.map((mark) => (
            <div
              key={mark.id}
              className="mt-2 border-t pt-1.5"
              style={{ color: "var(--viz-adjust)" }}
            >
              <div className="font-medium tabular-nums">
                {unassignedTerm({ positive: Math.max(mark.adjustment.amount, 0), negative: Math.min(mark.adjustment.amount, 0), total: mark.adjustment.amount, count: 1 }).label}{" "}
                {formatSignedExact(mark.adjustment.amount)} ·{" "}
                {adjustmentLabel(mark.adjustment.kind)}
              </div>
              <div className="text-muted-foreground">
                {mark.adjustment.source}
              </div>
              {mark.adjustment.rp_state && (
                <div className="text-muted-foreground">
                  game_state: {mark.adjustment.rp_state}
                </div>
              )}
              <div className="text-muted-foreground">
                Not counted towards any life.
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

/**
 * One life's marks: the kit purchase, any later spending, and the climb back.
 *
 * The dashed guide is a local segment from the purchase to the moment this
 * life earned its own kit back, deliberately never spanning the full plot: a
 * full-width rule would invite reading a later life's payout — or unassigned
 * movement that belongs to nobody — as this life breaking even.
 *
 * This geometry now carries the recovery story on its own. The captions that
 * used to spell it out are gone; the guide's length and whether it ends in a
 * dot say the same thing without competing for space with the kit figures,
 * and the exact crossing time is in the Lives table's Break-even column.
 */
function LifeGeometry({
  annotation: a,
  x,
  y,
}: {
  annotation: LifeAnnotation
  x: (v: number) => number
  y: (v: number) => number
}) {
  if (!a.hasKit) return null

  const dropX = x(a.purchaseAtSec)
  const baselineY = y(a.baseline)
  const troughY = y(a.trough)
  const guideEndX = x(a.guideToSec)
  const recovered = a.breakEvenAtSec !== null
  const markerHalf = 13

  return (
    <g>
      <line
        x1={dropX}
        x2={dropX}
        y1={baselineY}
        y2={troughY}
        stroke="var(--viz-kit)"
        strokeWidth={1.5}
      />
      <line
        x1={dropX - markerHalf}
        x2={dropX + markerHalf}
        y1={troughY}
        y2={troughY}
        stroke="var(--viz-kit)"
        strokeWidth={2}
        strokeLinecap="round"
      />

      {a.spends.map((spend) => (
        <SpendGeometry key={spend.id} spend={spend} x={x} y={y} />
      ))}

      <line
        x1={dropX}
        x2={guideEndX}
        y1={baselineY}
        y2={baselineY}
        stroke="var(--viz-ink-muted)"
        strokeWidth={1}
        strokeDasharray="3 3"
        opacity={0.55}
      />

      {/* Drawn regardless of whether its caption found room. */}
      {recovered && (
        <circle
          cx={guideEndX}
          cy={baselineY}
          r={3}
          fill="var(--viz-ink-muted)"
          stroke="var(--viz-surface)"
          strokeWidth={1.5}
        />
      )}
    </g>
  )
}

/** A non-kit outgoing: same language as a kit, dashed to say "not the kit". */
function SpendGeometry({
  spend,
  x,
  y,
}: {
  spend: SpendMark
  x: (v: number) => number
  y: (v: number) => number
}) {
  const fromY = y(spend.fromValue)
  const toY = y(spend.toValue)
  if (Math.abs(toY - fromY) < MARK_MIN_PX) return null

  const atX = x(spend.atSec)
  return (
    <g>
      <line
        x1={atX}
        x2={atX}
        y1={fromY}
        y2={toY}
        stroke="var(--viz-spend)"
        strokeWidth={1.5}
        strokeDasharray="3 2"
      />
      <line
        x1={atX - 6}
        x2={atX + 6}
        y1={toY}
        y2={toY}
        stroke="var(--viz-spend)"
        strokeWidth={1.75}
        strokeLinecap="round"
      />
    </g>
  )
}

/**
 * Money that belongs to no life.
 *
 * Drawn as a span rather than a point, because that is what it is: the
 * evidence only says the balance was one number here and another number
 * there. A diamond marks where it ended, distinct in shape as well as colour
 * from the rules that mark spending, so the two never read as the same kind
 * of event in a grey-scale print or to a colour-blind eye.
 */
function AdjustmentGeometry({
  mark,
  x,
  y,
}: {
  mark: AdjustmentMark
  x: (v: number) => number
  y: (v: number) => number
}) {
  const fromX = x(mark.adjustment.from_sec)
  const toX = x(mark.adjustment.to_sec)
  const fromY = y(mark.fromValue)
  const toY = y(mark.toValue)
  const r = 4

  return (
    <g>
      <path
        d={`M ${fromX} ${fromY} L ${toX} ${fromY} L ${toX} ${toY}`}
        fill="none"
        stroke="var(--viz-adjust)"
        strokeWidth={1.75}
        strokeDasharray="4 3"
      />
      <path
        d={`M ${toX} ${toY - r} L ${toX + r} ${toY} L ${toX} ${toY + r} L ${toX - r} ${toY} Z`}
        fill="var(--viz-adjust)"
        stroke="var(--viz-surface)"
        strokeWidth={1.5}
      />
    </g>
  )
}
