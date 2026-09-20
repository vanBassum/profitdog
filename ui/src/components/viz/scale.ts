import * as React from "react"

export interface LinearScale {
  (value: number): number
  domain: [number, number]
  range: [number, number]
}

export function linearScale(
  domain: [number, number],
  range: [number, number]
): LinearScale {
  const [d0, d1] = domain
  const [r0, r1] = range
  const span = d1 - d0
  const scale = ((value: number) =>
    span === 0
      ? (r0 + r1) / 2
      : r0 + ((value - d0) / span) * (r1 - r0)) as LinearScale
  scale.domain = domain
  scale.range = range
  return scale
}

/** Round a domain outward to human-readable tick boundaries. */
export function niceTicks(min: number, max: number, target = 5): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return []
  if (min === max) return [min]

  const rawStep = (max - min) / Math.max(target, 1)
  const magnitude = Math.pow(10, Math.floor(Math.log10(Math.abs(rawStep) || 1)))
  const normalized = rawStep / magnitude
  // 2.5 earns its place on the ladder: without it a raw step of ~2.2 snaps all
  // the way to 5, which on a typical kit-cost range leaves just two x labels.
  const step =
    (normalized <= 1
      ? 1
      : normalized <= 2
        ? 2
        : normalized <= 2.5
          ? 2.5
          : normalized <= 5
            ? 5
            : 10) * magnitude

  const start = Math.floor(min / step) * step
  const end = Math.ceil(max / step) * step

  const ticks: number[] = []
  // Guard against a pathological step producing an unbounded loop.
  for (let v = start, i = 0; v <= end + step * 0.5 && i < 200; v += step, i++) {
    ticks.push(Math.abs(v) < step * 1e-9 ? 0 : v)
  }
  return ticks
}

/** Domain padded by 8% so marks never sit on the frame. */
export function paddedDomain(
  values: number[],
  includeZero = false
): [number, number] {
  if (values.length === 0) return [0, 1]
  let min = Math.min(...values)
  let max = Math.max(...values)
  if (includeZero) {
    min = Math.min(min, 0)
    max = Math.max(max, 0)
  }
  if (min === max) {
    const bump = Math.abs(min) * 0.1 || 1
    return [min - bump, max + bump]
  }
  const pad = (max - min) * 0.08
  return [min - pad, max + pad]
}

/**
 * Size of an element, tracked across resizes, for responsive SVG charts.
 *
 * Height is measured as well as width so the chart can be sized by CSS — a
 * `clamp()` on the container — rather than by a constant baked into the
 * component. That keeps the card inside a viewport-relative budget without
 * the component having to know anything about viewports.
 */
export function useMeasuredSize<T extends HTMLElement>(
  fallbackWidth = 640,
  fallbackHeight = 280
) {
  const ref = React.useRef<T | null>(null)
  const [size, setSize] = React.useState({
    width: fallbackWidth,
    height: fallbackHeight,
  })

  React.useEffect(() => {
    const node = ref.current
    if (!node) return

    const measure = (width: number, height: number) => {
      if (width > 0 && height > 0) {
        setSize((current) =>
          current.width === width && current.height === height
            ? current
            : { width, height }
        )
      }
    }

    const observer = new ResizeObserver((entries) => {
      const box = entries[0]?.contentRect
      if (box) measure(box.width, box.height)
    })
    observer.observe(node)

    const box = node.getBoundingClientRect()
    measure(box.width || fallbackWidth, box.height || fallbackHeight)

    return () => observer.disconnect()
  }, [fallbackWidth, fallbackHeight])

  return [ref, size] as const
}

/**
 * Ticks for a duration axis, snapped to intervals a person actually thinks in.
 *
 * `niceTicks` works in powers of ten, which is right for money and wrong for
 * time — it produces boundaries like 20000s, rendering as "5h 33m". Clock
 * units are not decimal, so the ladder has to be spelled out.
 */
const TIME_STEPS = [
  30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400,
]

export function niceTimeTicks(maxSeconds: number, target = 6): number[] {
  if (!Number.isFinite(maxSeconds) || maxSeconds <= 0) return [0]

  const ideal = maxSeconds / Math.max(target, 1)
  const step =
    TIME_STEPS.find((candidate) => candidate >= ideal) ??
    TIME_STEPS[TIME_STEPS.length - 1]

  const ticks: number[] = []
  for (let t = 0; t <= maxSeconds && ticks.length < 200; t += step) {
    ticks.push(t)
  }
  return ticks
}

/**
 * Width of the widest label, estimated from character count.
 *
 * A fixed y-axis gutter silently clips: at phone width "-$5,000" overflows a
 * 46px gutter and the SVG viewport crops the minus, so a loss renders as a
 * gain. Sizing the gutter from the labels that will actually be drawn makes
 * that impossible rather than merely unlikely.
 *
 * 0.58em per character is a deliberate over-estimate for tabular digits, which
 * are narrower — erring toward a slightly wide gutter, never a clipped one.
 */
export function gutterFor(labels: string[], fontSize = 11): number {
  const widest = labels.reduce((max, l) => Math.max(max, l.length), 0)
  return Math.min(Math.max(widest * fontSize * 0.58 + 20, 40), 78)
}
