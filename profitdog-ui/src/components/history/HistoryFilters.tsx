import type { Grouping, HistoryFilters, TimeRange } from "@/lib/history"
import { cn } from "@/lib/utils"

interface Props {
  filters: HistoryFilters
  onChange: (next: Partial<HistoryFilters>) => void
  /** Raw map value plus the community name to show for it. */
  maps: { value: string; label: string }[]
  builds: string[]
  showUnknownBuild: boolean
  /**
   * Whether to offer the grouping control.
   *
   * Off for Analysis, where the unit is already the life — the smallest thing
   * there is — so there is nothing left to roll up. A control that changes
   * nothing on the page it sits above is worse than a missing one: it invites
   * the reader to wonder what they are not seeing.
   */
  showGrouping?: boolean
  /** The span the filtered matches actually cover, as the server reported it. */
  covered: { from: string; to: string } | null
  className?: string
}

const RANGES: { value: TimeRange; label: string }[] = [
  { value: "7d", label: "7 days" },
  { value: "30d", label: "30 days" },
  { value: "all", label: "All time" },
]

const GROUPINGS: { value: Grouping; label: string }[] = [
  { value: "match", label: "By match" },
  { value: "day", label: "By day" },
  { value: "week", label: "By week" },
]

/**
 * One row, above the analytics card.
 *
 * The date range on the right is a *caption*, not a control: it reports the
 * span the filtered matches actually cover, which is not the same as the span
 * the time filter asks for. "30 days" with three matches in it covers three
 * days, and saying so is the difference between an empty-looking chart being
 * confusing and being obvious.
 */
export function HistoryFilterBar({
  filters,
  onChange,
  maps,
  builds,
  showUnknownBuild,
  showGrouping = true,
  covered,
  className,
}: Props) {
  return (
    <div
      className={cn(
        "flex flex-wrap items-center gap-x-3 gap-y-2 text-xs",
        className
      )}
    >
      <div
        role="group"
        aria-label="Time range"
        className="flex overflow-hidden rounded-md border"
      >
        {RANGES.map((range) => (
          <button
            key={range.value}
            type="button"
            aria-pressed={filters.range === range.value}
            onClick={() => onChange({ range: range.value })}
            className={cn(
              "px-3 py-1.5 transition-colors",
              filters.range === range.value
                ? "bg-[var(--viz-positive)] font-medium text-white"
                : "text-muted-foreground hover:bg-accent hover:text-foreground"
            )}
          >
            {range.label}
          </button>
        ))}
      </div>

      <Select
        label="Map"
        value={filters.map}
        onChange={(map) => onChange({ map })}
      >
        <option value="all">All maps</option>
        {maps.map((map) => (
          <option key={map.value} value={map.value}>
            {map.label}
          </option>
        ))}
      </Select>

      <Select
        label="Build"
        value={filters.build}
        onChange={(build) => onChange({ build })}
      >
        <option value="all">All builds</option>
        {builds.map((build) => (
          <option key={build} value={build}>
            CL {build}
          </option>
        ))}
        {/* Only offered when there is something to select: an "Unknown" option
            that always matches nothing is a dead end. */}
        {showUnknownBuild && <option value="unknown">Unknown</option>}
      </Select>

      {showGrouping && (
        <Select
          label="Grouping"
          value={filters.grouping}
          onChange={(grouping) => onChange({ grouping: grouping as Grouping })}
        >
          {GROUPINGS.map((group) => (
            <option key={group.value} value={group.value}>
              {group.label}
            </option>
          ))}
        </Select>
      )}

      <p className="ml-auto flex items-center gap-2 rounded-md border px-2.5 py-1.5 text-muted-foreground tabular-nums">
        <CalendarGlyph />
        {covered ? formatCovered(covered) : "No matches in range"}
      </p>
    </div>
  )
}

function formatCovered(range: { from: string; to: string }): string {
  const from = new Date(range.from)
  const to = new Date(range.to)
  const sameYear = from.getFullYear() === to.getFullYear()
  const day = (date: Date, withYear: boolean) =>
    date.toLocaleDateString(undefined, {
      day: "numeric",
      month: "short",
      year: withYear ? "numeric" : undefined,
    })
  if (from.toDateString() === to.toDateString()) return day(from, true)
  return `${day(from, !sameYear)} – ${day(to, true)}`
}

function Select({
  label,
  value,
  onChange,
  children,
}: {
  label: string
  value: string
  onChange: (value: string) => void
  children: React.ReactNode
}) {
  return (
    <select
      aria-label={label}
      value={value}
      onChange={(event) => onChange(event.target.value)}
      className="rounded-md border bg-background px-2.5 py-1.5 text-xs hover:bg-accent"
    >
      {children}
    </select>
  )
}

function CalendarGlyph() {
  return (
    <svg aria-hidden viewBox="0 0 14 14" className="size-3.5 opacity-70">
      <rect
        x="1.5"
        y="2.5"
        width="11"
        height="10"
        rx="1.5"
        fill="none"
        stroke="currentColor"
      />
      <path d="M1.5 5.5h11M4.5 1.5v2M9.5 1.5v2" stroke="currentColor" />
    </svg>
  )
}
