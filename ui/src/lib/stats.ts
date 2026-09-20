/** Shared number formatting for money, percentages and durations. */

export function formatMoney(value: number): string {
  const sign = value < 0 ? "-" : ""
  const abs = Math.abs(Math.round(value))
  if (abs >= 10000)
    return `${sign}$${(abs / 1000).toFixed(abs >= 100000 ? 0 : 1)}k`
  return `${sign}$${abs.toLocaleString("en-US")}`
}

/**
 * Full digits, never abbreviated.
 *
 * `formatMoney` rounds to thousands above $10k, which is right for a summary
 * band read at a glance and wrong everywhere a figure has to reconcile: a
 * table column that shows "$15.7k" cannot be checked against one showing
 * "$6,375". Ledger figures use this.
 */
export function formatMoneyExact(value: number): string {
  const sign = value < 0 ? "-" : ""
  return `${sign}$${Math.abs(Math.round(value)).toLocaleString("en-US")}`
}

export function formatSignedExact(value: number): string {
  return `${value > 0 ? "+" : ""}${formatMoneyExact(value)}`
}

export function formatSigned(value: number): string {
  return `${value > 0 ? "+" : ""}${formatMoney(value)}`
}

export function formatPct(value: number): string {
  return `${value > 0 ? "+" : ""}${Math.round(value)}%`
}

export function formatDuration(seconds: number): string {
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)
  return `${m}m ${s.toString().padStart(2, "0")}s`
}

/**
 * "0m", "45m", "1h 20m" — an axis reads better than raw seconds.
 *
 * Moved here from `bankroll.ts` when that module went: it was the only thing
 * in it that was about presentation rather than about the curve.
 */
export function formatElapsed(seconds: number): string {
  const total = Math.max(Math.round(seconds / 60), 0)
  const hours = Math.floor(total / 60)
  const minutes = total % 60
  if (hours === 0) return `${minutes}m`
  if (minutes === 0) return `${hours}h`
  return `${hours}h ${minutes}m`
}

/** "5m 42s" — elapsed within a life, not a position on the server clock. */
export function formatLifeElapsed(seconds: number): string {
  const total = Math.max(Math.round(seconds), 0)
  const minutes = Math.floor(total / 60)
  const rest = total % 60
  return `${minutes}m ${rest.toString().padStart(2, "0")}s`
}

/** Short, neutral wording for an adjustment's classification. */
export function adjustmentLabel(kind: string): string {
  switch (kind) {
    case "terminal_adjustment":
      return "after play stopped"
    case "map_transition":
      return "map change"
    default:
      return "unclassified"
  }
}
