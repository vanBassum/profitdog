/**
 * The one vocabulary every page speaks.
 *
 * ## Why a module, and not just careful writing
 *
 * Session and History were written weeks apart against the same ledger, and
 * they drifted: the same dollar was "Gained" on one page and "Gross gained" on
 * the other, "Other spend" here and "Other outflow" nowhere, "Net" in four
 * places meaning three different totals. A reader moving between pages had to
 * re-learn the words, and worse, had no way to tell whether two differently
 * named figures were the same quantity. Strings drift precisely because
 * nothing fails when they do — so they live here, imported rather than
 * retyped.
 *
 * ## The model
 *
 *     Earned − Spent + Unassigned income = Profit
 *
 * - **Earned** — every positive movement attributed to a detected life.
 * - **Spent** — every classified outflow, exactly `Kit cost + Other outflow`.
 * - **Unassigned income** — positive movement no detected life can claim. See
 *   {@link unassignedTerm} for why the word changes with the sign.
 * - **Profit** — what is left.
 *
 * ## What is no longer here
 *
 * The arithmetic. This module used to own an `Accounting` type and the
 * functions that summed lives into sessions and sessions into periods. Those
 * were a second implementation of the server's ledger, and two implementations
 * of one accounting disagree eventually. Every total on screen now arrives
 * from `/api/*` already added up; this file is the naming, and nothing else.
 */

import type { Unassigned } from "./api"

/** User-facing names. Import these rather than writing the words inline. */
export const TERMS = {
  earned: "Earned",
  spent: "Spent",
  kitCost: "Kit cost",
  otherOutflow: "Other outflow",
  unassignedIncome: "Unassigned income",
  unassignedOutflow: "Unassigned outflow",
  unassignedMovement: "Unassigned movement",
  profit: "Profit",
  sessionProfit: "Session profit",
  periodProfit: "Period profit",
  cumulativeProfit: "Cumulative profit",
  breakEven: "Break-even",
  /** The unit, not a metric. Safe as a table title or a count. */
  lives: "Lives",
} as const

/** One-liners for the places that have room for a word of explanation. */
export const HINTS = {
  earned: "Every positive movement attributed to a detected life",
  spent:
    "Everything classified as leaving the account: kits plus other outflow",
  kitCost: "The detected loadout cost, or the price you corrected it to",
  otherOutflow:
    "Negative movement during a life that was not part of its kit — counted here rather than quietly subtracted from Earned",
  unassignedIncome: "Income outside a detected life",
  unassignedOutflow: "Outflow outside a detected life",
  unassignedMovement: "Movement outside a detected life, in both directions",
} as const

export const EQUATION = `${TERMS.earned} − ${TERMS.spent} + ${TERMS.unassignedIncome} = ${TERMS.profit}`

export const NO_UNASSIGNED: Unassigned = {
  positive: 0,
  negative: 0,
  total: 0,
  count: 0,
}

/**
 * What to call unassigned movement, given what it actually contains.
 *
 * The name follows the sign, and only the sign. An aggregate holding both
 * signs gets the neutral "Unassigned movement" and says so in its tooltip:
 * summing +$2,000 and −$400 into a single "$1,600 income" would hide the
 * outflow completely, which is the failure this vocabulary exists to prevent.
 *
 * What it never does is guess *why*. Not "payout", not "settlement", not "win
 * bonus" — the data that would prove any of those is exactly the data the
 * agent does not record.
 */
export function unassignedTerm(composition: Unassigned): {
  label: string
  hint: string
  tooltip: string
} {
  const { positive, negative, total, count } = composition
  const mixed = positive > 0 && negative < 0

  if (mixed) {
    return {
      label: TERMS.unassignedMovement,
      hint: HINTS.unassignedMovement,
      tooltip:
        `${count} movement${count === 1 ? "" : "s"} outside any detected life: ` +
        `+$${Math.round(positive).toLocaleString("en-US")} in and ` +
        `−$${Math.round(-negative).toLocaleString("en-US")} out, ` +
        `netting ${total >= 0 ? "+" : "−"}$${Math.round(Math.abs(total)).toLocaleString("en-US")}. ` +
        "Not attributed to a life, and not named — nothing in the data says what caused them.",
    }
  }

  if (negative < 0) {
    return {
      label: TERMS.unassignedOutflow,
      hint: HINTS.unassignedOutflow,
      tooltip:
        "Outflow observed in this period that no detected life can account for. " +
        "Not attributed to a life, and not named — nothing in the data says what caused it.",
    }
  }

  return {
    label: TERMS.unassignedIncome,
    hint: HINTS.unassignedIncome,
    tooltip:
      "Income observed in this period that no detected life can account for. " +
      "Not attributed to a life, and not named — nothing in the data says what caused it.",
  }
}

export function addUnassigned(a: Unassigned, b: Unassigned): Unassigned {
  return {
    positive: a.positive + b.positive,
    negative: a.negative + b.negative,
    total: a.total + b.total,
    count: a.count + b.count,
  }
}

/**
 * One level of the accounting, in the names this UI uses.
 *
 * A rename of what the server sent, and nothing more. The figures are not
 * re-added here — `accountingOf` reads `spent` and `profit` off the response
 * rather than recomputing them from the parts, because the server has already
 * checked that they reconcile and a second opinion could only disagree.
 */
export interface Accounting {
  earned: number
  kitCost: number
  otherOutflow: number
  spent: number
  unassigned: Unassigned
  profit: number
}

export function accountingOf(source: {
  earned: number
  kit_cost: number
  other_outflow: number
  spent: number
  unassigned: Unassigned
  profit: number
}): Accounting {
  return {
    earned: source.earned,
    kitCost: source.kit_cost,
    otherOutflow: source.other_outflow,
    spent: source.spent,
    unassigned: source.unassigned,
    profit: source.profit,
  }
}
