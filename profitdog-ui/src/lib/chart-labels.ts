/**
 * Which captions the balance chart asks for, and how badly it wants each one.
 *
 * Kept out of the component so the placement can be tested against real
 * sessions rather than eyeballed in a browser. Overlapping labels are a
 * rendering bug that a type-checker cannot see and a screenshot only catches
 * at one window width, so the check belongs somewhere it can be run at many.
 *
 * Priorities, highest first:
 *
 *   100  L1, L2 …             which life this is. Never dropped: a mark with
 *                             no identity is an unreadable mark.
 *    80  kit −$3,260          what it cost. The figure the view is about.
 *    70  unassigned income    money belonging to no life; must stay visible
 *                             while there is any room at all, because it is
 *                             the difference between the lives and the
 *                             headline.
 *    65  other outflow −$6,250  an outgoing that is not the kit.
 *    30  unclassified         the movement's classification word. Pure gloss;
 *                             the tooltip says it in full.
 *
 * ## What is deliberately not here
 *
 * There are no "break even" or "not recovered" captions. Both were words for
 * something the geometry already says: the recovery guide runs from the
 * purchase to the moment the life climbed back to where it started, and it
 * either ends in a dot or it does not. Writing it out as well put a caption
 * on every life — six on a normal session — to restate a line the reader is
 * already looking at, and those captions were the ones most often colliding
 * with the figures that could not be restated anywhere. The exact crossing
 * time is still one glance away, in the Lives table's Break-even column,
 * which is the place a precise time is actually useful.
 */

import type { AdjustmentMark, LifeAnnotation } from "./api"
import type { LabelCandidate } from "./labels"
import { alternatingOffsets } from "./labels"
import { adjustmentLabel, formatMoneyExact, formatSignedExact } from "./stats"
import { unassignedTerm } from "./terms"

export interface CandidateInput {
  lives: LifeAnnotation[]
  adjustments: AdjustmentMark[]
  /** Seconds to plot pixels. */
  x: (seconds: number) => number
  /** Dollars to plot pixels. */
  y: (dollars: number) => number
  /** A movement shorter than this on screen is not worth a caption. */
  captionMinPx: number
}

export function buildLabelCandidates({
  lives,
  adjustments,
  x,
  y,
  captionMinPx,
}: CandidateInput): LabelCandidate[] {
  const candidates: LabelCandidate[] = []

  for (const a of lives) {
    // `continue`, emphatically not `return`: a life with no readable kit price
    // has no purchase to caption, but every other life still does. This was a
    // `forEach` callback once, where `return` meant "skip this one" — as a
    // plain loop the same word abandons the whole function and hands the chart
    // an undefined list.
    if (!a.hasKit) continue
    const dropX = x(a.purchaseAtSec)
    const baselineY = y(a.baseline)
    const troughY = y(a.trough)

    candidates.push({
      id: `life-${a.lifeNumber}`,
      x: dropX - 15,
      y: baselineY,
      text: a.label,
      fontSize: 11,
      anchor: "end",
      priority: 100,
      // Small offsets only: it must stay readable as *this* purchase's label.
      offsets: [-7, -20, 6, -33],
      droppable: false,
    })

    candidates.push({
      id: `kit-${a.lifeNumber}`,
      x: dropX - 15,
      y: troughY,
      text: `kit −${formatMoneyExact(a.invested)}`,
      fontSize: 10,
      anchor: "end",
      priority: 80,
      offsets: alternatingOffsets(15, false),
      droppable: true,
    })

    for (const spend of a.spends) {
      if (Math.abs(y(spend.toValue) - y(spend.fromValue)) < captionMinPx) {
        continue
      }
      candidates.push({
        id: `spend-label-${spend.id}`,
        x: x(spend.atSec) + 7,
        y: y(spend.toValue),
        text: `other outflow −${formatMoneyExact(spend.amount)}`,
        fontSize: 10,
        anchor: "start",
        priority: 65,
        offsets: alternatingOffsets(14, false),
        droppable: true,
      })
    }
  }

  for (const mark of adjustments) {
    const atX = x(mark.adjustment.to_sec)
    const atY = y(mark.toValue)
    candidates.push({
      id: `adj-${mark.id}`,
      x: atX + 9,
      y: atY,
      text: `${unassignedTerm(
        { positive: Math.max(mark.adjustment.amount, 0), negative: Math.min(mark.adjustment.amount, 0), total: mark.adjustment.amount, count: 1 }
      ).label.toLowerCase()} ${formatSignedExact(mark.adjustment.amount)}`,
      fontSize: 10,
      anchor: "start",
      priority: 70,
      offsets: alternatingOffsets(10, true),
      droppable: true,
    })
    candidates.push({
      id: `adjkind-${mark.id}`,
      x: atX + 9,
      y: atY,
      text: adjustmentLabel(mark.adjustment.kind),
      fontSize: 10,
      anchor: "start",
      priority: 30,
      offsets: alternatingOffsets(23, true),
      droppable: true,
    })
  }

  return candidates
}

/** Which palette token a caption belongs to, from its id. */
export function labelClass(id: string): string {
  if (id.startsWith("kit-")) return "fill-[var(--viz-loss)]"
  if (id.startsWith("spend-label-")) return "fill-[var(--viz-spend)]"
  if (id.startsWith("adj")) return "fill-[var(--viz-adjust)]"
  return "fill-[var(--viz-ink)]"
}
