import * as React from "react"

import type { AnalysisPoint } from "@/lib/api"
import { formatMatchDuration } from "@/lib/history"
import {
  formatDuration,
  formatMoneyExact,
  formatSignedExact,
} from "@/lib/stats"
import { HINTS, TERMS } from "@/lib/terms"
import { cn } from "@/lib/utils"

interface Props {
  points: AnalysisPoint[]
  selectedId: string | null
  hoverId: string | null
  onSelect: (id: string | null) => void
  onHover: (id: string | null) => void
  className?: string
}

/**
 * The dots, as rows.
 *
 * ## Why the chart needs a table under it at all
 *
 * A scatter is the only view in this app where a figure cannot be read off the
 * screen exactly — a dot is a position, not a number, and "roughly four and a
 * half thousand" is not a figure anyone can check against the Session page.
 * The table is what makes the chart auditable: same lives, same order, same
 * ledger values, at full precision.
 *
 * ## The columns are the tooltip's columns
 *
 * Deliberately identical, and in the same order. Hovering a dot and reading a
 * row are two ways of asking the same question, and a reader who learns one
 * layout should not have to learn a second one four hundred pixels lower.
 *
 * Hover and selection are shared with the chart in both directions, so pointing
 * at a row lights its dot. That is the cheapest way to answer "which one is
 * that outlier" without a search box or a legend.
 */
export function PlottedLivesTable({
  points,
  selectedId,
  hoverId,
  onSelect,
  onHover,
  className,
}: Props) {
  // Newest first, matching every other list in the app: the life you want is
  // almost always from the match you just played.
  const ordered = React.useMemo(
    () =>
      [...points].sort((a, b) => {
        const at = (p: AnalysisPoint) =>
          new Date(p.started_at ?? p.match_started_at).getTime()
        return at(b) - at(a) || b.life_number - a.life_number
      }),
    [points]
  )

  if (ordered.length === 0) return null

  return (
    <section className={cn("overflow-hidden rounded-lg border", className)}>
      <div className="flex items-baseline justify-between gap-3 border-b px-4 py-2.5">
        <h2 className="text-[10px] font-medium tracking-wide text-muted-foreground uppercase">
          {TERMS.lives} on the chart
        </h2>
        <p className="text-[11px] text-muted-foreground tabular-nums">
          {ordered.length} plotted
        </p>
      </div>

      {/* Capped and scrollable: a long period can plot a hundred lives, and a
          hundred rows would push the chart they belong to off the screen. */}
      <div className="max-h-[38vh] overflow-auto">
        <table className="w-full text-sm">
          <caption className="sr-only">
            Every life on the chart, with its map, what its kit cost, what it
            earned, what else it spent, the profit it kept and how long it
            lasted
          </caption>
          <thead className="sticky top-0 z-10 bg-card">
            <tr className="border-b text-left text-xs text-muted-foreground">
              <th scope="col" className="px-4 py-2 font-medium">
                Life
              </th>
              <th scope="col" className="px-4 py-2 font-medium">
                Map
              </th>
              <th
                scope="col"
                className="px-4 py-2 text-right font-medium"
                title={HINTS.kitCost}
              >
                {TERMS.kitCost}
              </th>
              <th
                scope="col"
                className="px-4 py-2 text-right font-medium"
                title={HINTS.earned}
              >
                {TERMS.earned}
              </th>
              <th
                scope="col"
                className="px-4 py-2 text-right font-medium"
                title={HINTS.otherOutflow}
              >
                {TERMS.otherOutflow}
              </th>
              <th scope="col" className="px-4 py-2 text-right font-medium">
                {TERMS.profit}
              </th>
              <th scope="col" className="px-4 py-2 text-right font-medium">
                Length
              </th>
            </tr>
          </thead>
          <tbody className="viz-root">
            {ordered.map((life) => {
              const active = life.id === (hoverId ?? selectedId)
              return (
                <tr
                  key={life.id}
                  onMouseEnter={() => onHover(life.id)}
                  onMouseLeave={() => onHover(null)}
                  onClick={() => onSelect(life.id === selectedId ? null : life.id)}
                  aria-selected={life.id === selectedId}
                  className={cn(
                    "cursor-pointer border-b last:border-b-0",
                    active && "bg-accent"
                  )}
                >
                  <td className="px-4 py-2 tabular-nums">
                    <span className="flex items-baseline gap-2">
                      {life.life_number}
                      <span className="text-[11px] text-muted-foreground">
                        {new Date(
                          life.started_at ?? life.match_started_at
                        ).toLocaleString(undefined, {
                          month: "short",
                          day: "numeric",
                          hour: "2-digit",
                          minute: "2-digit",
                        })}
                      </span>
                    </span>
                  </td>

                  <td className="px-4 py-2 text-muted-foreground">
                    {life.map_display ?? "—"}
                  </td>

                  {/* An em dash, never $0: a life whose kit price could not be
                      read did not get a free kit. */}
                  <td className="px-4 py-2 text-right text-muted-foreground tabular-nums">
                    {life.has_kit ? (
                      formatMoneyExact(life.kit_cost)
                    ) : (
                      <span title="No fall was visible in the curve for this life, so no kit price could be read">
                        —
                      </span>
                    )}
                  </td>

                  <td className="px-4 py-2 text-right tabular-nums">
                    {formatMoneyExact(life.earned)}
                  </td>

                  <td
                    className="px-4 py-2 text-right tabular-nums"
                    style={{
                      color:
                        life.other_outflow > 0 ? "var(--viz-spend)" : undefined,
                    }}
                  >
                    {life.other_outflow > 0 ? (
                      `−${formatMoneyExact(life.other_outflow)}`
                    ) : (
                      <span className="text-muted-foreground">—</span>
                    )}
                  </td>

                  <td
                    className="px-4 py-2 text-right font-semibold tabular-nums"
                    style={{
                      color:
                        life.profit >= 0
                          ? "var(--viz-gain)"
                          : "var(--viz-loss)",
                    }}
                  >
                    {formatSignedExact(life.profit)}
                  </td>

                  <td className="px-4 py-2 text-right text-muted-foreground tabular-nums">
                    {life.duration_sec >= 3600
                      ? formatMatchDuration(life.duration_sec)
                      : formatDuration(life.duration_sec)}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </section>
  )
}
