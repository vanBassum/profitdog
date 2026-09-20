import { AccountingSummary } from "@/components/AccountingSummary"
import type { MatchDetail } from "@/lib/api"
import { formatSigned } from "@/lib/stats"
import { accountingOf, TERMS } from "@/lib/terms"

interface Props {
  match: MatchDetail
  peak: number
  trough: number
  className?: string
}

/**
 * The session's accounting, as the shared equation.
 *
 * Every figure arrives already added up, under the ruleset this match was
 * played under, with the server's own invariants satisfied — including that
 * the parts reconcile with the curve's end-to-end movement. This component
 * decides which figures go into the equation and what counts sit beside them,
 * and computes none of them.
 *
 * "Unprofitable" rather than "lost": the count is about whether a life earned
 * back what its kit cost, which has nothing to do with whether the match was
 * won. Nothing in the data establishes match outcome at all.
 */
export function SummaryBand({ match, peak, trough, className }: Props) {
  const lives = match.lives_detail
  const unprofitable = lives.filter((l) => l.has_kit && l.profit < 0).length

  return (
    <AccountingSummary
      className={className}
      accounting={accountingOf(match)}
      profitLabel={TERMS.sessionProfit}
      counts={
        <p className="text-sm tracking-wide whitespace-nowrap uppercase tabular-nums">
          <span className="font-semibold">{lives.length}</span>
          <span className="text-muted-foreground"> lives · </span>
          <span
            className="font-semibold"
            style={{ color: unprofitable > 0 ? "var(--viz-loss)" : undefined }}
            title="Lives that did not earn back what their kit cost. Says nothing about whether the match was won."
          >
            {unprofitable} unprofitable
          </span>
        </p>
      }
      trailing={
        <p className="text-xs whitespace-nowrap text-muted-foreground tabular-nums">
          peak {formatSigned(peak)} · low {formatSigned(trough)}
        </p>
      }
    />
  )
}
