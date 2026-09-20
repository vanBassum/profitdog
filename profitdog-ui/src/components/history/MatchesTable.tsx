import * as React from "react"

import { Button } from "@/components/ui/button"
import type { MatchSummary } from "@/lib/api"
import { formatMatchDuration } from "@/lib/history"
import {
  formatMoney,
  formatMoneyExact,
  formatSigned,
  formatSignedExact,
} from "@/lib/stats"
import { HINTS, TERMS, unassignedTerm } from "@/lib/terms"
import { cn } from "@/lib/utils"

interface Props {
  rows: MatchSummary[]
  bestId: string | null
  selectedId: string | null
  search: string
  onSearch: (value: string) => void
  onSelect: (id: string | null) => void
  onOpen: (id: string) => void
  onExport: () => void
  className?: string
}

/**
 * Roughly six rows clear a 1080p viewport below the chart, so that is the
 * page size. Paging is only rendered when there is a second page — a pager
 * under a four-row table is furniture.
 */
const PAGE_SIZE = 6

export function MatchesTable({
  rows,
  bestId,
  selectedId,
  search,
  onSearch,
  onSelect,
  onOpen,
  onExport,
  className,
}: Props) {
  const [page, setPage] = React.useState(0)
  const [searching, setSearching] = React.useState(false)

  // Newest first: the match you want is almost always the one you just played.
  const ordered = React.useMemo(
    () =>
      [...rows].sort((a, b) => new Date(b.started_at).getTime() - new Date(a.started_at).getTime()),
    [rows]
  )

  // A new result set starts at its first page. Adjusting during render rather
  // than in an effect means the reader never sees one frame of page 3 of a
  // one-page table.
  const [seenRows, setSeenRows] = React.useState(rows)
  if (seenRows !== rows) {
    setSeenRows(rows)
    setPage(0)
  }

  // One heading for the column, taken from every row on screen and off it:
  // a heading that changed as you paged would be describing the page rather
  // than the column.
  const unassignedHeading = unassignedTerm(
    rows.reduce(
      (acc, r) => ({
        positive: acc.positive + r.unassigned.positive,
        negative: acc.negative + r.unassigned.negative,
        total: acc.total + r.unassigned.total,
        count: acc.count + r.unassigned.count,
      }),
      { positive: 0, negative: 0, total: 0, count: 0 }
    )
  )

  const pages = Math.max(Math.ceil(ordered.length / PAGE_SIZE), 1)
  const current = Math.min(page, pages - 1)
  const visible = ordered.slice(current * PAGE_SIZE, (current + 1) * PAGE_SIZE)

  return (
    <section className={cn("overflow-hidden rounded-lg border", className)}>
      <div className="flex items-center justify-between gap-3 border-b px-4 py-2">
        <h2 className="text-sm font-semibold">Matches</h2>

        <div className="flex items-center gap-2">
          {searching || search ? (
            <input
              autoFocus
              value={search}
              onChange={(event) => onSearch(event.target.value)}
              onBlur={() => setSearching(false)}
              placeholder="Map, build, date…"
              aria-label="Search matches"
              className="w-44 rounded-md border bg-background px-2 py-1 text-xs"
            />
          ) : (
            <Button
              variant="outline"
              size="sm"
              onClick={() => setSearching(true)}
              aria-label="Search matches"
            >
              <SearchGlyph />
            </Button>
          )}
          <Button variant="outline" size="sm" onClick={onExport}>
            <DownloadGlyph />
            Export CSV
          </Button>
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <caption className="sr-only">
            Every tracked match in the selected period, with what it earned,
            what it spent and the profit it kept
          </caption>
          <thead>
            <tr className="border-b bg-card text-left text-xs text-muted-foreground">
              <Th>Date</Th>
              <Th>Map</Th>
              <Th>Build</Th>
              <Th align="right">Lives</Th>
              <Th align="right" hint={HINTS.kitCost}>
                {TERMS.kitCost}
              </Th>
              <Th align="right" hint={HINTS.earned}>
                {TERMS.earned}
              </Th>
              <Th align="right" hint={HINTS.otherOutflow}>
                {TERMS.otherOutflow}
              </Th>
              {/* Named for what the period as a whole contains, so the column
                  heading cannot claim "income" over a column holding an
                  outflow. */}
              <Th align="right" hint={unassignedHeading.tooltip}>
                {unassignedHeading.label}
              </Th>
              <Th align="right">{TERMS.profit}</Th>
              <Th align="right">Duration</Th>
              <Th align="right">
                <span className="sr-only">Open</span>
              </Th>
            </tr>
          </thead>

          <tbody className="viz-root">
            {visible.map((row) => {
              const selected = row.match_key === selectedId
              return (
                <tr
                  key={row.match_key}
                  tabIndex={0}
                  aria-selected={selected}
                  onClick={() => (selected ? onOpen(row.match_key) : onSelect(row.match_key))}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      event.preventDefault()
                      onOpen(row.match_key)
                    }
                  }}
                  className={cn(
                    "cursor-pointer border-b outline-none last:border-b-0 hover:bg-accent/50 focus-visible:bg-accent/50",
                    selected &&
                      "bg-[color-mix(in_srgb,var(--viz-positive)_12%,transparent)]"
                  )}
                >
                  <td className="px-4 py-2 whitespace-nowrap tabular-nums">
                    {new Date(row.started_at).toLocaleDateString(undefined, {
                      day: "numeric",
                      month: "short",
                      year: "numeric",
                    })}
                  </td>
                  <td className="px-4 py-2 whitespace-nowrap">
                    {row.map ?? <Unknown />}
                  </td>
                  <td className="px-4 py-2 whitespace-nowrap tabular-nums">
                    {row.build ?? <Unknown />}
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums">
                    {row.lives}
                  </td>

                  <Money value={-row.kit_cost} color="var(--viz-kit)" />
                  <Money value={row.earned} />
                  <Money
                    value={row.other_outflow === 0 ? null : -row.other_outflow}
                    color="var(--viz-spend)"
                  />
                  <Money
                    value={row.unassigned.total}
                    color={
                      row.unassigned.total !== 0 ? "var(--viz-adjust)" : undefined
                    }
                    signed
                    zeroAsDash={false}
                  />

                  <td
                    className="px-4 py-2 text-right font-semibold tabular-nums"
                    style={{
                      color:
                        row.profit >= 0 ? "var(--viz-gain)" : "var(--viz-loss)",
                    }}
                    title={formatSignedExact(row.profit)}
                  >
                    <span className="flex items-center justify-end gap-2">
                      <span aria-hidden>{formatSigned(row.profit)}</span>
                      <span className="sr-only">
                        {formatSignedExact(row.profit)}
                      </span>
                      {row.match_key === bestId && <BestBadge />}
                    </span>
                  </td>

                  <td className="px-4 py-2 text-right text-muted-foreground tabular-nums">
                    {formatMatchDuration(row.duration_sec)}
                  </td>
                  <td className="px-2 py-2 text-right">
                    <button
                      type="button"
                      aria-label={`Open ${row.map ?? "match"} on ${new Date(row.started_at).toLocaleDateString()} in Session`}
                      onClick={(event) => {
                        event.stopPropagation()
                        onOpen(row.match_key)
                      }}
                      className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
                    >
                      <ChevronGlyph />
                    </button>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      <div className="flex items-center justify-between gap-3 border-t px-4 py-2 text-xs text-muted-foreground">
        <p className="tabular-nums">
          {ordered.length} {ordered.length === 1 ? "match" : "matches"} ·{" "}
          {ordered.reduce((sum, r) => sum + r.lives, 0)} lives
        </p>

        {pages > 1 && (
          <div className="flex items-center gap-2 tabular-nums">
            <span>
              Rows {current * PAGE_SIZE + 1}–
              {Math.min((current + 1) * PAGE_SIZE, ordered.length)} of{" "}
              {ordered.length}
            </span>
            <Button
              variant="outline"
              size="icon-sm"
              aria-label="Previous page"
              disabled={current === 0}
              onClick={() => setPage(current - 1)}
            >
              <ChevronGlyph flip />
            </Button>
            <Button
              variant="outline"
              size="icon-sm"
              aria-label="Next page"
              disabled={current >= pages - 1}
              onClick={() => setPage(current + 1)}
            >
              <ChevronGlyph />
            </Button>
          </div>
        )}
      </div>
    </section>
  )
}

function Th({
  children,
  align = "left",
  hint,
}: {
  children: React.ReactNode
  align?: "left" | "right"
  hint?: string
}) {
  return (
    <th
      scope="col"
      title={hint}
      className={cn(
        "px-4 py-2.5 font-medium whitespace-nowrap",
        align === "right" && "text-right"
      )}
    >
      {children}
    </th>
  )
}

/**
 * An abbreviated figure with its exact value attached.
 *
 * `$16.5k` is what a dense row can carry; `$16,470` is what someone checking
 * the arithmetic needs. Both are present — the exact one in the `title` and in
 * screen-reader text — so abbreviating costs no information.
 */
function Money({
  value,
  color,
  signed = false,
  zeroAsDash = true,
}: {
  value: number | null
  color?: string
  signed?: boolean
  zeroAsDash?: boolean
}) {
  if (value === null || (zeroAsDash && value === 0)) {
    return (
      <td className="px-4 py-2 text-right tabular-nums">
        <Unknown />
      </td>
    )
  }
  const exact = signed ? formatSignedExact(value) : formatMoneyExact(value)
  const short = signed ? formatSigned(value) : formatMoney(value)
  return (
    <td
      className="px-4 py-2 text-right tabular-nums"
      style={color ? { color } : undefined}
      title={exact}
    >
      <span aria-hidden>{short}</span>
      <span className="sr-only">{exact}</span>
    </td>
  )
}

function Unknown() {
  return <span className="text-muted-foreground">—</span>
}

function BestBadge() {
  return (
    <span
      className="rounded px-1.5 py-0.5 text-[10px] font-medium"
      style={{
        color: "var(--viz-gain)",
        background: "color-mix(in srgb, var(--viz-gain) 14%, transparent)",
      }}
      title="Largest profit in this period — the most money kept, not the biggest percentage"
    >
      best
    </span>
  )
}

function ChevronGlyph({ flip = false }: { flip?: boolean }) {
  return (
    <svg
      aria-hidden
      viewBox="0 0 12 12"
      className={cn("size-3", flip && "rotate-180")}
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
  )
}

function SearchGlyph() {
  return (
    <svg aria-hidden viewBox="0 0 14 14" className="size-3.5">
      <circle
        cx="6"
        cy="6"
        r="4"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
      />
      <path
        d="M9 9 L12.5 12.5"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
    </svg>
  )
}

function DownloadGlyph() {
  return (
    <svg aria-hidden viewBox="0 0 14 14" className="size-3.5">
      <path
        d="M7 1.5v7M4 6l3 3 3-3M2 11.5h10"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}
