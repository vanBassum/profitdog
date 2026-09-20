import * as React from "react"
import { Info } from "lucide-react"

/**
 * The kit-price explanation, on demand.
 *
 * It used to sit as a permanent paragraph under the table, which cost real
 * vertical space on every visit to explain something most visits already
 * understand. As a popover the information stays one click away without
 * competing with the chart for the page.
 */
export function KitPriceInfo() {
  // Hover shows it; a click pins it open. Without the pin a reader who moves
  // the pointer towards the text loses the text on the way, and the panel
  // could never be read on a device with no hover at all.
  const [hovered, setHovered] = React.useState(false)
  const [pinned, setPinned] = React.useState(false)
  const open = hovered || pinned

  return (
    <div
      className="relative"
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      <button
        type="button"
        onClick={() => setPinned((v) => !v)}
        onFocus={() => setHovered(true)}
        onBlur={() => setHovered(false)}
        aria-expanded={open}
        className="flex items-center gap-1.5 text-xs text-muted-foreground transition-colors hover:text-foreground"
      >
        <Info aria-hidden className="size-3.5" />
        How kit prices are estimated
      </button>

      {open && (
        <>
          {/* A backdrop rather than a document listener: dismissal stays in
              React's own event flow instead of a global side effect. Only a
              pinned panel takes one — an unpinned one closes on its own when
              the pointer leaves. */}
          {pinned && (
            <div
              className="fixed inset-0 z-20"
              onClick={() => setPinned(false)}
              aria-hidden
            />
          )}
          <div
            role="dialog"
            aria-label="How kit prices are estimated"
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                setPinned(false)
                setHovered(false)
              }
            }}
            className="absolute right-0 z-30 mt-2 w-80 rounded-lg border bg-popover p-3 text-xs leading-relaxed shadow-lg"
          >
            <p className="font-medium text-foreground">
              Kit prices are inferred, not reported.
            </p>
            <ul className="mt-2 space-y-1.5 text-muted-foreground">
              <li>Wardogs never publishes what a loadout cost.</li>
              <li>
                The price is read from the downward step at the start of a life,
                including a kit paid for in parts.
              </li>
              <li>
                Break-even is measured against the balance immediately before
                that purchase.
              </li>
              <li>
                A later life earning well never settles an earlier life&apos;s
                kit.
              </li>
              <li>
                Correcting a price moves the difference into Other outflow. It
                never changes Spent or Profit — the curve already recorded how
                much left the account.
              </li>
              <li className="text-foreground">
                Click any kit price to enter the real number instead.
              </li>
            </ul>
          </div>
        </>
      )}
    </div>
  )
}
