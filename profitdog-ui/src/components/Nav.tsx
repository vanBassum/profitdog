/**
 * Navigation controls for the application toolbar.
 *
 * Split from `lib/router.ts` so that module exports only functions and hooks:
 * a file mixing components with plain exports loses fast refresh, and the
 * router is imported by almost everything.
 */

import * as React from "react"

import { navigate } from "@/lib/router"
import { cn } from "@/lib/utils"

/**
 * A link that navigates in-page but behaves like a link otherwise.
 *
 * Modified clicks are left alone on purpose — middle-click and ctrl-click open
 * a new tab, and intercepting them would break an expectation the underline
 * and the cursor have already made.
 */
export function NavLink({
  to,
  active,
  className,
  children,
  onClick,
  ...rest
}: {
  to: string
  active?: boolean
  className?: string
  children: React.ReactNode
} & Omit<React.AnchorHTMLAttributes<HTMLAnchorElement>, "href">) {
  return (
    <a
      href={to}
      aria-current={active ? "page" : undefined}
      onClick={(event) => {
        // The caller gets first refusal, so a link can keep its href for
        // middle-click while handling a plain click its own way.
        onClick?.(event)
        if (
          event.defaultPrevented ||
          event.button !== 0 ||
          event.metaKey ||
          event.ctrlKey ||
          event.shiftKey ||
          event.altKey
        ) {
          return
        }
        event.preventDefault()
        navigate(to)
      }}
      className={className}
      {...rest}
    >
      {children}
    </a>
  )
}

/** Tab-style navigation item for the application toolbar. */
export function NavTab({
  to,
  active,
  children,
}: {
  to: string
  active: boolean
  children: React.ReactNode
}) {
  return (
    <NavLink
      to={to}
      active={active}
      className={cn(
        "relative rounded-md px-2.5 py-1 text-sm transition-colors",
        active
          ? "font-medium text-foreground"
          : "text-muted-foreground hover:text-foreground"
      )}
    >
      {children}
      {/* The underline is the state, so it is drawn rather than implied by
          colour alone — colour is not available to every reader. */}
      <span
        aria-hidden
        className={cn(
          "absolute inset-x-1.5 -bottom-[7px] h-[2px] rounded-full",
          active ? "bg-[var(--viz-positive)]" : "bg-transparent"
        )}
      />
    </NavLink>
  )
}
