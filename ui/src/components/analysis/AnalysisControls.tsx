import type { MetricsResponse } from "@/lib/api"
import { activePreset, metricByKey, type AnalysisAxes } from "@/lib/metrics"
import { cn } from "@/lib/utils"

interface Props {
  axes: AnalysisAxes
  onChange: (next: Partial<AnalysisAxes>) => void
  /**
   * The server's catalogue of metrics and presets.
   *
   * Served rather than hard-coded, so these controls describe whatever the
   * server actually offers. A metric added there appears here with no change.
   */
  metrics: MetricsResponse | null
  className?: string
}

/**
 * Presets, the two axis pickers, swap, and the trend toggle.
 *
 * ## A preset is a shortcut, not a mode
 *
 * Every button here does exactly one thing: set the two dropdowns. Nothing is
 * locked afterwards, no filter changes underneath, and the reader can move
 * either axis to anything and stay there. The buttons highlight when the
 * current pair happens to match one of them, which is a statement about where
 * you are rather than a claim to own the view.
 *
 * That matters because the presets are opinionated — they are the five
 * questions worth asking first, not the five that are allowed. Making them
 * modes would turn the other seventy-two combinations into a feature nobody
 * finds.
 *
 * ## Swap is a button, not two dropdown changes
 *
 * Reversing the axes is the single most common thing to want after reading a
 * scatter — "is that the same picture the other way round?" — and doing it by
 * hand means two selections with a meaningless intermediate state in between,
 * where both axes briefly show the same metric.
 */
export function AnalysisControls({ axes, onChange, metrics, className }: Props) {
  const preset = activePreset(metrics, axes.x, axes.y)
  const options = metrics?.metrics ?? []
  const presets = metrics?.presets ?? []

  return (
    <div className={cn("flex flex-wrap items-center gap-x-3 gap-y-2", className)}>
      <div
        role="group"
        aria-label="Preset comparisons"
        className="flex flex-wrap items-center gap-1.5"
      >
        {presets.map((item) => (
          <button
            key={item.id}
            type="button"
            aria-pressed={preset?.id === item.id}
            title={`${metricByKey(metrics, item.x)?.label ?? item.x} on the x-axis, ${metricByKey(metrics, item.y)?.label ?? item.y} on the y-axis`}
            onClick={() => onChange({ x: item.x, y: item.y })}
            className={cn(
              "rounded-md border px-2.5 py-1.5 text-xs transition-colors",
              preset?.id === item.id
                ? "border-transparent bg-[var(--viz-positive)] font-medium text-white"
                : "text-muted-foreground hover:bg-accent hover:text-foreground"
            )}
          >
            {item.label}
          </button>
        ))}
      </div>

      <div className="ml-auto flex flex-wrap items-center gap-2 text-xs">
        <MetricSelect
          label="X axis"
          value={axes.x}
          options={options}
          onChange={(x) => onChange({ x })}
        />

        <button
          type="button"
          onClick={() => onChange({ x: axes.y, y: axes.x })}
          title="Swap the axes"
          aria-label="Swap the axes"
          className="rounded-md border p-1.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
        >
          <SwapGlyph />
        </button>

        <MetricSelect
          label="Y axis"
          value={axes.y}
          options={options}
          onChange={(y) => onChange({ y })}
        />

        <label className="flex cursor-pointer items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-muted-foreground hover:bg-accent hover:text-foreground">
          <input
            type="checkbox"
            checked={axes.trend}
            onChange={(event) => onChange({ trend: event.target.checked })}
            className="size-3.5 accent-[var(--viz-positive)]"
          />
          Trend line
        </label>
      </div>
    </div>
  )
}

function MetricSelect({
  label,
  value,
  options,
  onChange,
}: {
  label: string
  value: string
  options: MetricsResponse["metrics"]
  onChange: (value: string) => void
}) {
  const current = options.find((metric) => metric.key === value)
  return (
    <select
      aria-label={label}
      value={value}
      title={current?.hint}
      onChange={(event) => onChange(event.target.value)}
      className="rounded-md border bg-background px-2.5 py-1.5 text-xs hover:bg-accent"
    >
      {options.map((metric) => (
        <option key={metric.key} value={metric.key} title={metric.hint}>
          {metric.label}
        </option>
      ))}
    </select>
  )
}

function SwapGlyph() {
  return (
    <svg aria-hidden viewBox="0 0 14 14" className="size-3.5">
      <path
        d="M2 4.5h8M8 2l2.5 2.5L8 7M12 9.5H4m2.5-2.5L4 9.5 6.5 12"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}
