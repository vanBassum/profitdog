import * as React from "react"

import {
  formatMoney,
  formatMoneyExact,
  formatSigned,
  formatSignedExact,
} from "@/lib/stats"
import type { Accounting } from "@/lib/terms"
import { HINTS, TERMS, unassignedTerm } from "@/lib/terms"
import { cn } from "@/lib/utils"

interface Props {
  accounting: Accounting
  /** "Session profit" or "Period profit" — the only term that differs. */
  profitLabel: string
  /** Counts, in the page's own unit. Rendered after the equation. */
  counts?: React.ReactNode
  /** Pushed to the far right: peak/low, or nothing. */
  trailing?: React.ReactNode
  className?: string
}

/**
 * The accounting as one readable equation, shared by both pages.
 *
 * ## Why an equation rather than a row of metrics
 *
 * The band this replaces showed five figures side by side — net, invested,
 * gained, other spend, adjustments — with nothing to say how they related.
 * They did relate, exactly, but the reader had to work it out, and the two
 * pages had drifted into showing the same quantities under different names
 * anyway. Written as
 *
 *     Earned − Spent + Unassigned income = Session profit
 *
 * the relationship is the layout. Nothing can be quietly dropped, because a
 * missing term would leave the sentence ungrammatical; and nothing can be
 * double-counted, because every dollar appears on exactly one side of one
 * operator.
 *
 * ## What carries weight
 *
 * Profit is the answer to the question anyone opened the page with, so it is
 * the largest figure and the only one in the gain colour. Spent is red-orange
 * and Unassigned amber, matching the marks on the charts below, so a colour
 * seen on the plot can be traced back to a term here. Earned stays neutral:
 * it is the largest number on the band and colouring it too would leave the
 * eye nowhere to land.
 *
 * The breakdown under Spent — `Kits $16.5k · Other $6,675` — is secondary
 * text rather than two more terms. Both readings stay available without
 * making the reader parse a five-term equation to find out whether they are
 * up.
 *
 * ## One component, two pages
 *
 * Both pages render this, differing only in `profitLabel` and what they count.
 * That is the point: this is the piece that was duplicated and drifted, so
 * duplicating it again — even "just for History" — would put the drift back.
 */
export function AccountingSummary({
  accounting,
  profitLabel,
  counts,
  trailing,
  className,
}: Props) {
  const { earned, spent, kitCost, otherOutflow, unassigned, profit } =
    accounting
  const unassignedName = unassignedTerm(unassigned)

  return (
    <div
      className={cn(
        "viz-root flex flex-wrap items-start gap-x-4 gap-y-3 border-b px-4 py-3",
        className
      )}
    >
      <Term label={TERMS.earned} hint={HINTS.earned}>
        <Figure value={earned} title={formatMoneyExact(earned)} />
      </Term>

      {/* Each operator is glued to the term it introduces, so a narrow window
          breaks the line *before* a sign rather than after one. A row ending
          in a lonely "=" reads as a sentence that was cut off; a row starting
          with "= Period profit" still reads as the answer. */}
      <Group>
        <Operator>−</Operator>
        <Term
          label={TERMS.spent}
          hint={HINTS.spent}
          /* Both halves, abbreviated the same way the term above them is. A
             reader checking the split against the table gets the exact figures
             from the tooltip rather than from a wider band. */
          note={`Kits ${formatMoney(kitCost)} · Other ${formatMoney(otherOutflow)}`}
          noteTitle={`${TERMS.kitCost} ${formatMoneyExact(kitCost)} · ${TERMS.otherOutflow} ${formatMoneyExact(otherOutflow)}`}
        >
          <Figure
            value={spent}
            color="var(--viz-spend)"
            title={formatMoneyExact(spent)}
          />
        </Term>
      </Group>

      <Group>
        <Operator>+</Operator>
        <Term
          label={unassignedName.label}
          hint={unassignedName.tooltip}
          note={unassignedName.hint}
          noteTitle={unassignedName.tooltip}
        >
          <span
            className="text-base font-semibold tabular-nums"
            style={{ color: "var(--viz-adjust)" }}
            title={formatSignedExact(unassigned.total)}
          >
            {unassigned.total === 0 ? "$0" : formatSigned(unassigned.total)}
          </span>
        </Term>
      </Group>

      {/* The answer. Larger and coloured, because everything to its left
          exists to explain it. */}
      <Group>
        <Operator>=</Operator>
        <Term label={profitLabel}>
          <span
            className="text-2xl leading-7 font-semibold tabular-nums"
            style={{
              color: profit >= 0 ? "var(--viz-gain)" : "var(--viz-loss)",
            }}
            title={formatSignedExact(profit)}
          >
            {formatSigned(profit)}
          </span>
        </Term>
      </Group>

      {/* Secondary, and pushed to the far end: counts and extremes are things
          you look up, not things you read the band for. `ml-auto` on the first
          of them keeps them together on the right at full width and lets them
          wrap as a pair when the equation needs the whole row. */}
      {counts && <div className="mt-3 ml-auto">{counts}</div>}
      {trailing && (
        <div className={cn("mt-3", counts ? "self-center" : "ml-auto")}>
          {trailing}
        </div>
      )}
    </div>
  )
}

/** An operator and its term, kept on the same line as each other. */
function Group({ children }: { children: React.ReactNode }) {
  return <div className="flex items-start gap-x-4">{children}</div>
}

/**
 * One term: its name, its figure, and optionally a line of detail beneath.
 *
 * The name is uppercase micro-type so the figures stay the thing being read,
 * and the detail line sits under the figure rather than beside it — beside
 * would make it compete with the next term for the same horizontal read.
 */
function Term({
  label,
  hint,
  note,
  noteTitle,
  children,
}: {
  label: string
  hint?: string
  note?: string
  noteTitle?: string
  children: React.ReactNode
}) {
  return (
    <div className="leading-tight whitespace-nowrap">
      <div
        className="text-[10px] font-medium tracking-wide text-muted-foreground uppercase"
        title={hint}
      >
        {label}
      </div>
      {children}
      {note && (
        <div
          className="mt-0.5 text-[10px] text-muted-foreground tabular-nums"
          title={noteTitle}
        >
          {note}
        </div>
      )}
    </div>
  )
}

/**
 * The `−`, `+` and `=` between terms.
 *
 * Hidden from assistive technology: a screen reader is already getting the
 * labelled figures in order, and "minus" read between them adds nothing that
 * the names Spent and Profit do not already carry. The blank first line keeps
 * the glyph on the same baseline as the figures either side of it.
 */
function Operator({ children }: { children: React.ReactNode }) {
  return (
    <div
      aria-hidden
      className="leading-tight text-muted-foreground select-none"
    >
      <div className="text-[10px]">&nbsp;</div>
      <span className="text-base">{children}</span>
    </div>
  )
}

function Figure({
  value,
  color,
  title,
}: {
  value: number
  color?: string
  title?: string
}) {
  return (
    <span
      className="text-base font-semibold tabular-nums"
      style={color ? { color } : undefined}
      title={title}
    >
      {formatMoney(value)}
    </span>
  )
}
