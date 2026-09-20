import type { AnalysisResponse } from "@/lib/api"
import { cn } from "@/lib/utils"

interface Props {
  /** One pairing, exactly as the server answered it. */
  result: AnalysisResponse
  className?: string
}

/**
 * The reading, beside the chart rather than under it.
 *
 * ## Why the number never travels alone
 *
 * An r on its own is an invitation to supply the meaning, and the meaning a
 * reader supplies is causation. So the figure, the sentence, the count it was
 * computed from, the lives it could not use and the caution all sit in one
 * column, in that order — the same order someone would have to ask for them in
 * to avoid being misled. Splitting them up, or putting the caveat somewhere it
 * can be scrolled past, is how a careful number becomes a careless claim.
 *
 * ## What the caution has to say, and why it stays put
 *
 * Two things, and neither is optional. That a correlation is not a cause is
 * the general point. That this chart cannot see what role you played or what
 * you were doing is the specific one, and it is the more useful of the two:
 * every dot here is a life reduced to money and a clock, and the obvious
 * explanation for any pattern in it — that expensive kits get bought for the
 * matches you were already going to play differently — is invisible to the
 * data. The tracker records no role, no vehicle and no objective. Nothing on
 * this page can control for what it never saw.
 */
export function AnalysisReadout({ result, className }: Props) {
  const { summary, excluded, x, y } = result
  const plotted = result.points.length

  return (
    <div className={cn("viz-root flex flex-col gap-3", className)}>
      <div>
        <div className="text-[10px] font-medium tracking-wide text-muted-foreground uppercase">
          {y.label} vs {x.label}
        </div>
        <p className="mt-1 flex flex-wrap items-baseline gap-x-2">
          {summary.value && (
            <span
              className="text-2xl font-semibold tabular-nums"
              title="Pearson's r: −1 is a perfect opposite, 0 is no straight-line relationship, +1 is a perfect match"
            >
              {summary.value}
            </span>
          )}
          <span className="text-sm">{summary.reading}</span>
        </p>
        {summary.caveat && (
          <p className="mt-1 text-[11px] text-muted-foreground">
            {summary.caveat}
          </p>
        )}
      </div>

      <dl className="grid grid-cols-[1fr_auto] gap-x-3 gap-y-1 border-t pt-3 text-xs tabular-nums">
        <dt className="text-muted-foreground">Lives plotted</dt>
        <dd className="text-right font-medium">{plotted}</dd>
        <dt className="text-muted-foreground">Left out</dt>
        <dd
          className="text-right font-medium"
          style={excluded.count > 0 ? { color: "var(--viz-spend)" } : undefined}
        >
          {excluded.count}
        </dd>
      </dl>

      {/* Never silently dropped: a chart missing lives has to say which, and
          why, in the same breath as the number it computed without them. */}
      <p className="text-[11px] text-muted-foreground">
        {excluded.summary ??
          `Every life the filters selected is on the chart — ${plotted} of ${excluded.total}.`}
      </p>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t pt-3 text-[11px] text-muted-foreground">
        <Swatch color="var(--viz-gain)">Kept money</Swatch>
        <Swatch color="var(--viz-loss)">Lost money</Swatch>
      </div>

      <p className="rounded-md border border-dashed px-2.5 py-2 text-[11px] leading-relaxed text-muted-foreground">
        Two things moving together is not one causing the other. This chart also
        cannot see what role you played or what you were doing — the tracker
        records neither — so anything it shows may just as easily be about the
        matches you chose as about the kit you bought.
      </p>
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
        className="size-2 rounded-full"
        style={{ background: color }}
      />
      {children}
    </span>
  )
}
