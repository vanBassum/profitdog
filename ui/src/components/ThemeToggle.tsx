import { Moon, Sun } from "lucide-react"

import { Button } from "@/components/ui/button"
import { useTheme } from "@/components/theme-provider"

/**
 * The light/dark switch, top right.
 *
 * The keyboard already had `d`; a shortcut nobody is told about is not a
 * control, so this is the same flip with a surface. It shows the theme you
 * would get by pressing it rather than the one you are in — the icon is the
 * destination, which is how a switch reads.
 */
export function ThemeToggle() {
  const { resolvedTheme, toggleTheme } = useTheme()
  const next = resolvedTheme === "dark" ? "light" : "dark"

  return (
    <Button
      variant="ghost"
      size="icon"
      onClick={toggleTheme}
      aria-label={`Switch to ${next} mode`}
      title={`Switch to ${next} mode (d)`}
    >
      {resolvedTheme === "dark" ? <Sun /> : <Moon />}
    </Button>
  )
}
