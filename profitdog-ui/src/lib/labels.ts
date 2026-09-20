/**
 * Placing chart labels so they do not sit on top of one another.
 *
 * ## Why not just stagger every other one
 *
 * The previous pass alternated rows whenever two anchors were closer than a
 * fixed number of pixels. That rule cannot see text, so it did both wrong
 * things at once: it moved a short label clear of a neighbour it was never
 * touching, and left a long one overlapping a neighbour just outside the
 * threshold. `kit −$3,260` is roughly five times the width of `L3`, and a rule
 * that measures only the gap between anchors treats them as the same size.
 *
 * So this works on bounding boxes. Each label declares where it wants to be
 * and a short list of places it would accept instead; the resolver walks them
 * in priority order and takes the first slot that is actually free.
 *
 * ## What gets dropped
 *
 * When nothing fits, low-priority labels are hidden rather than allowed to
 * overlap — but only ones marked droppable, and only after every alternative
 * has been tried. Hiding is not losing: the caller keeps the full text in the
 * tooltip, and marks that carry meaning on their own (the recovery dot, the
 * kit rule) are drawn whether their caption survives or not. A chart that
 * silently overlaps two numbers is unreadable in a way that a chart with one
 * number on hover is not.
 *
 * Nothing here changes type size, and nothing here changes the height of the
 * plot. Both are ways of paying for crowding with legibility.
 */

export interface LabelCandidate {
  id: string
  /** Anchor position in plot pixels. */
  x: number
  /** Preferred vertical position, before any offset is applied. */
  y: number
  text: string
  fontSize: number
  anchor: "start" | "middle" | "end"
  /**
   * Higher is placed first and therefore wins a contested slot. Use it to say
   * what must stay visible: a life's identity outranks its kit price, which
   * outranks a recovery caption.
   */
  priority: number
  /**
   * Vertical offsets to try, in order of preference. Alternating signs is
   * what produces the above/below stagger for neighbouring labels.
   */
  offsets: number[]
  /** Whether this label may be hidden when no offset fits. */
  droppable: boolean
}

export interface PlacedLabel {
  id: string
  x: number
  y: number
  anchor: "start" | "middle" | "end"
  text: string
  /** True when no free slot existed; the caller should show it on hover. */
  hidden: boolean
  /** The space this label occupies, so a caller can verify it is unshared. */
  box: Box
}

export interface Box {
  left: number
  right: number
  top: number
  bottom: number
}

/**
 * Width of a string at a given size, without measuring it in the DOM.
 *
 * A real measurement would mean a layout pass per label per render. The factor
 * is tuned to Inter's digits and lower case; it errs high, which is the safe
 * direction — over-estimating spaces labels slightly further apart than
 * necessary, under-estimating lets them touch.
 */
const AVERAGE_CHAR_WIDTH = 0.58

export function estimateTextWidth(text: string, fontSize: number): number {
  return text.length * fontSize * AVERAGE_CHAR_WIDTH
}

function boxFor(
  candidate: LabelCandidate,
  y: number,
  padX: number,
  padY: number
): Box {
  const width = estimateTextWidth(candidate.text, candidate.fontSize)
  const left =
    candidate.anchor === "start"
      ? candidate.x
      : candidate.anchor === "end"
        ? candidate.x - width
        : candidate.x - width / 2
  return {
    left: left - padX,
    right: left + width + padX,
    // SVG text sits on its baseline, so the glyphs occupy the space above it.
    top: y - candidate.fontSize - padY,
    bottom: y + padY,
  }
}

export function overlaps(a: Box, b: Box): boolean {
  return !(
    a.right <= b.left ||
    a.left >= b.right ||
    a.bottom <= b.top ||
    a.top >= b.bottom
  )
}

export interface PlacementOptions {
  /** Labels may not be placed outside this band. */
  minY: number
  maxY: number
  /** Horizontal and vertical breathing room between two labels. */
  padX?: number
  padY?: number
  /** Boxes already occupied by something that is not a label. */
  reserved?: Box[]
}

/**
 * Resolve a set of labels to non-overlapping positions.
 *
 * Returns one entry per candidate, in the order they were given. A hidden
 * entry still carries its text so the caller can route it to a tooltip.
 */
export function placeLabels(
  candidates: LabelCandidate[],
  options: PlacementOptions
): Map<string, PlacedLabel> {
  const { minY, maxY, padX = 3, padY = 2, reserved = [] } = options
  const placed: Box[] = [...reserved]
  const out = new Map<string, PlacedLabel>()

  // Highest priority first so the labels that must survive claim their space
  // before anything else can take it. Left-to-right within a priority keeps
  // the result stable and reading order intact.
  const ordered = [...candidates].sort(
    (a, b) => b.priority - a.priority || a.x - b.x
  )

  for (const candidate of ordered) {
    let chosen: { y: number; box: Box } | null = null

    for (const offset of candidate.offsets) {
      const y = Math.min(
        Math.max(candidate.y + offset, minY + candidate.fontSize),
        maxY
      )
      const box = boxFor(candidate, y, padX, padY)
      if (!placed.some((other) => overlaps(box, other))) {
        chosen = { y, box }
        break
      }
    }

    if (chosen === null && !candidate.droppable) {
      // It must be drawn, so take the preferred slot and accept the overlap.
      // Only the highest-priority marks are non-droppable, and two of those
      // colliding means the chart is narrower than its own data.
      const y = Math.min(
        Math.max(
          candidate.y + (candidate.offsets[0] ?? 0),
          minY + candidate.fontSize
        ),
        maxY
      )
      chosen = { y, box: boxFor(candidate, y, padX, padY) }
    }

    if (chosen === null) {
      out.set(candidate.id, {
        id: candidate.id,
        x: candidate.x,
        y: candidate.y,
        anchor: candidate.anchor,
        text: candidate.text,
        hidden: true,
        box: boxFor(candidate, candidate.y, padX, padY),
      })
      continue
    }

    placed.push(chosen.box)
    out.set(candidate.id, {
      id: candidate.id,
      x: candidate.x,
      y: chosen.y,
      anchor: candidate.anchor,
      text: candidate.text,
      hidden: false,
      box: chosen.box,
    })
  }

  return out
}

/**
 * Offsets that try the preferred side first, then the other, then further out.
 *
 * `above` picks the starting direction, so two neighbouring labels given
 * opposite preferences resolve to opposite sides on the first attempt instead
 * of both marching down the same column.
 */
export function alternatingOffsets(base: number, above: boolean): number[] {
  const sign = above ? -1 : 1
  return [
    sign * base,
    -sign * base,
    sign * (base + 13),
    -sign * (base + 13),
    sign * (base + 26),
    -sign * (base + 26),
  ]
}
