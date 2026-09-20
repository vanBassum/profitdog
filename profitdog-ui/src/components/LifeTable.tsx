import * as React from "react"

import { KitPriceInfo } from "@/components/KitPriceInfo"
import type { LifeAnnotation } from "@/lib/api"
import { formatLifeElapsed } from "@/lib/stats"
import {
  formatDuration,
  formatMoneyExact,
  formatSignedExact,
} from "@/lib/stats"
import { HINTS, TERMS } from "@/lib/terms"
import { cn } from "@/lib/utils"

interface Props {
  annotations: LifeAnnotation[]
  /**
   * Record a corrected kit price, by life number.
   *
   * Corrections are stored on the server beside the facts, not in this
   * browser's `localStorage` — a price you corrected on the desktop used to be
   * invisible on the laptop, which for a figure the whole page depends on is a
   * bug rather than a quirk.
   */
  onOverride: (lifeNumber: number, value: number | null) => void
  /** Collapsed hands the vertical space back to the chart. */
  open: boolean
  onOpenChange: (open: boolean) => void
  className?: string
}

/**
 * The numeric reference behind the chart.
 *
 * It reads the same `LifeAnnotation` objects the chart draws from, so a kit
 * price or a break-even time shown here is the one plotted above rather than
 * a parallel calculation that could drift.
 *
 * Profit carries the weight, not return. A life that keeps $10,000 on a
 * $4,000 kit is worth more than one keeping $1,000 on a $100 kit, even though
 * the cheap kit wins on percentage — ranking by ratio would recommend the
 * smaller outcome. The percentage is still available on hover, where it can
 * inform without dominating.
 *
 * Per row the arithmetic is exactly the one the summary band states:
 *
 *     profit = earned − kit cost − other outflow
 *
 * Unassigned movement is deliberately absent from every row. It is money no
 * detected life can claim, so putting it in a life's line would be inventing
 * the attribution the ledger refused to make. It is accounted for once, in the
 * band above, which is why this table needs no reconciliation beneath it.
 */
export function LifeTable({
  annotations,
  onOverride,
  open,
  onOpenChange,
  className,
}: Props) {
  // "Best" means the most money taken home, never the largest multiple.
  const bestLife = annotations.reduce<LifeAnnotation | null>(
    (best, a) => (best === null || a.life.profit > best.life.profit ? a : best),
    null
  )
  const highlightBest =
    bestLife !== null && bestLife.life.profit > 0 && annotations.length > 1

  return (
    <section className={cn("overflow-hidden rounded-lg border", className)}>
      <div
        className={cn(
          "flex items-center justify-between gap-3 px-4 py-2",
          open && "border-b"
        )}
      >
        <button
          type="button"
          onClick={() => onOpenChange(!open)}
          aria-expanded={open}
          aria-controls="lives-table"
          className="-mx-1.5 flex items-center gap-1.5 rounded px-1.5 py-0.5 text-sm font-semibold hover:bg-accent"
        >
          <svg
            aria-hidden
            viewBox="0 0 12 12"
            className={cn(
              "size-3 transition-transform",
              open ? "rotate-90" : "rotate-0"
            )}
          >
            <path
              d="M4 2.5 L8 6 L4 9.5"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
          Lives
          <span className="font-normal text-muted-foreground">
            ({annotations.length})
          </span>
        </button>

        {/* Nothing but the kit-price note. The totals that used to sit here
            when collapsed restated the summary band a few hundred pixels
            above, in a second vocabulary — two spellings of one truth is how
            the two pages drifted apart in the first place. */}
        {open && <KitPriceInfo />}
      </div>

      {/* Open, the table grows to its content and the page scrolls: an inner
          scrollbar would hide rows behind a second, easily missed gesture.
          Closed, it is unmounted rather than hidden, so the chart above can
          claim the height instead of scrolling past an empty box. */}
      {open && (
        <div className="overflow-x-auto" id="lives-table">
          <table className="w-full text-sm">
            <caption className="sr-only">
              Every life, with what its kit cost, what it earned, and the profit
              it kept
            </caption>
            <thead>
              <tr className="border-b bg-card text-left text-xs text-muted-foreground">
                <th scope="col" className="px-4 py-2.5 font-medium">
                  Life
                </th>
                <th
                  scope="col"
                  className="px-4 py-2.5 text-right font-medium"
                  title={HINTS.kitCost}
                >
                  {TERMS.kitCost}
                </th>
                <th
                  scope="col"
                  className="px-4 py-2.5 text-right font-medium"
                  title={HINTS.earned}
                >
                  {TERMS.earned}
                </th>
                <th
                  scope="col"
                  className="px-4 py-2.5 text-right font-medium"
                  title={HINTS.otherOutflow}
                >
                  {TERMS.otherOutflow}
                </th>
                <th scope="col" className="px-4 py-2.5 text-right font-medium">
                  {TERMS.profit}
                </th>
                <th scope="col" className="px-4 py-2.5 text-right font-medium">
                  {TERMS.breakEven}
                </th>
                <th scope="col" className="px-4 py-2.5 text-right font-medium">
                  Length
                </th>
              </tr>
            </thead>
            <tbody className="viz-root">
              {annotations.map((a) => {
                const isBest =
                  highlightBest && a.lifeNumber === bestLife.lifeNumber
                return (
                  <tr key={a.lifeNumber} className="border-b last:border-b-0">
                    <td className="px-4 py-2 tabular-nums">
                      <span className="flex items-center gap-2">
                        {a.lifeNumber}
                        {isBest && (
                          <span
                            className="rounded px-1.5 py-0.5 text-[10px] font-medium"
                            style={{
                              color: "var(--viz-gain)",
                              background:
                                "color-mix(in srgb, var(--viz-gain) 14%, transparent)",
                            }}
                            title="Largest profit — the most money kept, not the biggest percentage"
                          >
                            best
                          </span>
                        )}
                      </span>
                    </td>

                    {/* Kit is supporting detail: what the life risked. */}
                    <td className="px-4 py-2 text-right text-muted-foreground">
                      <KitCell
                        annotation={a}
                        onOverride={onOverride}
                      />
                    </td>

                    {/* Every payout this life took, with nothing netted off
                      it. Whatever it also spent is the next column. */}
                    <td className="px-4 py-2 text-right tabular-nums">
                      {formatMoneyExact(a.life.earned)}
                    </td>

                    {/* An em dash, not a zero: most lives have no outflow
                      beyond their kit, and a column of $0 would make the ones
                      that did harder to spot rather than easier. */}
                    <td
                      className="px-4 py-2 text-right tabular-nums"
                      style={{
                        color:
                          a.life.other_outflow > 0
                            ? "var(--viz-spend)"
                            : undefined,
                      }}
                      title={
                        a.life.other_outflow > 0
                          ? `${a.spends.length} outflow${a.spends.length === 1 ? "" : "s"} after the kit was paid for, too late to be part of it`
                          : undefined
                      }
                    >
                      {a.life.other_outflow > 0 ? (
                        `−${formatMoneyExact(a.life.other_outflow)}`
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </td>

                    {/* Profit is the headline: heavier weight, and the only
                    coloured money figure in the row. */}
                    <td
                      className="px-4 py-2 text-right font-semibold tabular-nums"
                      style={{
                        color:
                          a.life.profit >= 0
                            ? "var(--viz-gain)"
                            : "var(--viz-loss)",
                      }}
                      title={
                        a.hasKit
                          ? `${formatMoneyExact(a.life.earned)} earned against a ${formatMoneyExact(a.invested)} kit and ${formatMoneyExact(a.life.other_outflow)} of other outflow`
                          : "No kit price could be read for this life"
                      }
                    >
                      {formatSignedExact(a.life.profit)}
                    </td>

                    <td className="px-4 py-2 text-right text-muted-foreground tabular-nums">
                      {!a.hasKit ? (
                        <span>—</span>
                      ) : a.breakEvenInLifeSec === null ? (
                        <span style={{ color: "var(--viz-loss)" }}>
                          Not recovered
                        </span>
                      ) : (
                        formatLifeElapsed(a.breakEvenInLifeSec)
                      )}
                    </td>

                    <td className="px-4 py-2 text-right text-muted-foreground tabular-nums">
                      {formatDuration(a.life.duration_sec)}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

/**
 * Kit price, click to correct.
 *
 * Wardogs never reports loadout cost, so this is read off the drop in the
 * curve. Typing a real number here re-prices the life everywhere, chart
 * included.
 */
function KitCell({
  annotation: a,
  onOverride,
}: {
  annotation: LifeAnnotation
  onOverride: (lifeNumber: number, value: number | null) => void
}) {
  const [draft, setDraft] = React.useState("")
  const [editing, setEditing] = React.useState(false)

  const commit = () => {
    setEditing(false)
    const trimmed = draft.trim()
    if (trimmed === "") {
      onOverride(a.lifeNumber, null)
      return
    }
    const parsed = Number(trimmed.replace(/[$,\s]/g, ""))
    onOverride(a.lifeNumber, Number.isFinite(parsed) && parsed > 0 ? parsed : null)
  }

  if (editing) {
    return (
      <input
        autoFocus
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") commit()
          if (e.key === "Escape") setEditing(false)
        }}
        aria-label={`Kit price for life ${a.lifeNumber}`}
        className="w-24 rounded border bg-background px-2 py-1 text-right text-sm tabular-nums"
        placeholder="e.g. 4200"
      />
    )
  }

  return (
    <button
      type="button"
      onClick={() => {
        setDraft(
          a.life.cost_confidence === "manual" ? String(a.life.kit_cost) : ""
        )
        setEditing(true)
      }}
      title={
        a.life.cost_confidence === "manual"
          ? "Entered by you. Clear it to go back to the derived figure."
          : a.hasKit
            ? "Derived from the drop into this life. Click to enter the real price."
            : "No drop was visible for this life, so the kit price could not be read. Click to enter it."
      }
      className="inline-flex items-center gap-1.5 rounded px-1 py-0.5 tabular-nums hover:bg-accent"
    >
      {a.hasKit ? (
        formatMoneyExact(a.invested)
      ) : (
        <span className="text-muted-foreground">set price</span>
      )}
      <span
        aria-hidden
        className={
          a.life.cost_confidence === "manual"
            ? "size-1.5 rounded-full bg-foreground/70"
            : "size-1.5 rounded-full border border-muted-foreground/60"
        }
      />
      <span className="sr-only">
        {a.life.cost_confidence === "manual" ? "entered by you" : "derived"}
      </span>
    </button>
  )
}
