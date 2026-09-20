/**
 * The smallest router that gives real URLs.
 *
 * Two pages and a query string do not justify a routing library: what is
 * actually needed is a subscription to `history`, which the platform already
 * provides through `popstate` for back and forward. The only gap is that
 * `pushState` deliberately does *not* fire an event, so a custom one is
 * dispatched alongside it and both feed the same store.
 *
 * `useSyncExternalStore` rather than `useState` + `useEffect`: the location is
 * external mutable state, and reading it during render through the same
 * snapshot the subscription updates is what keeps a fast back-then-forward
 * from rendering a stale URL.
 */

import * as React from "react"

const NAVIGATE_EVENT = "profitdog:navigate"

function currentHref(): string {
  return window.location.pathname + window.location.search
}

let snapshot = typeof window === "undefined" ? "/" : currentHref()

function subscribe(onChange: () => void): () => void {
  const handler = () => {
    snapshot = currentHref()
    onChange()
  }
  window.addEventListener("popstate", handler)
  window.addEventListener(NAVIGATE_EVENT, handler)
  return () => {
    window.removeEventListener("popstate", handler)
    window.removeEventListener(NAVIGATE_EVENT, handler)
  }
}

function getSnapshot(): string {
  // Re-reading here rather than trusting the cached value keeps the store
  // correct if anything else in the app calls history directly.
  if (typeof window !== "undefined" && snapshot !== currentHref()) {
    snapshot = currentHref()
  }
  return snapshot
}

/** Server-render and test fallback: no window, no navigation. */
function getServerSnapshot(): string {
  return "/"
}

export interface Location {
  pathname: string
  search: string
  query: URLSearchParams
}

export function useLocation(): Location {
  const href = React.useSyncExternalStore(
    subscribe,
    getSnapshot,
    getServerSnapshot
  )
  return React.useMemo(() => {
    const [pathname, search = ""] = href.split("?")
    return {
      pathname,
      search: search ? `?${search}` : "",
      query: new URLSearchParams(search),
    }
  }, [href])
}

interface NavState {
  /** How many in-app pushes deep this entry is. */
  depth: number
}

function currentDepth(): number {
  const state = window.history.state as NavState | null
  return typeof state?.depth === "number" ? state.depth : 0
}

export function navigate(to: string, options: { replace?: boolean } = {}) {
  if (to === currentHref()) return
  const depth = currentDepth() + (options.replace ? 0 : 1)
  if (options.replace) window.history.replaceState({ depth }, "", to)
  else window.history.pushState({ depth }, "", to)
  window.dispatchEvent(new Event(NAVIGATE_EVENT))
}

/**
 * Whether stepping back would land on a page this app pushed.
 *
 * A "back" control that pushes a *new* entry gets the destination right and
 * the experience wrong: the browser only restores scroll position for real
 * history traversal, so a pushed return lands at the top of a list the reader
 * had scrolled halfway down. Where a genuine back is available, use it.
 */
export function canGoBack(): boolean {
  return currentDepth() > 0
}

export function goBack() {
  window.history.back()
}
