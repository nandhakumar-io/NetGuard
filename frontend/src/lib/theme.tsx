import React, { createContext, useContext, useEffect, useState } from "react";

type Theme = "dark" | "light";

interface ThemeContextType {
  theme: Theme;
  toggleTheme: () => void;
}

const ThemeContext = createContext<ThemeContextType | undefined>(undefined);

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [theme, setTheme] = useState<Theme>("light");

  useEffect(() => {
    const savedTheme = localStorage.getItem("theme") as Theme | null;
    if (savedTheme) {
      setTheme(savedTheme);
    } else {
      setTheme("light");
    }
  }, []);

  useEffect(() => {
    if (theme === "dark") {
      document.documentElement.classList.add("dark");
    } else {
      document.documentElement.classList.remove("dark");
    }
    localStorage.setItem("theme", theme);
  }, [theme]);

  const toggleTheme = () => {
    setTheme((prev) => (prev === "dark" ? "light" : "dark"));
  };

  return (
    <ThemeContext.Provider value={{ theme, toggleTheme }}>
      {children}
    </ThemeContext.Provider>
  );
}

export function useTheme() {
  const context = useContext(ThemeContext);
  if (context === undefined) {
    throw new Error("useTheme must be used within a ThemeProvider");
  }
  return context;
}

/** Design-token reference for light/dark pairings, extracted from the
 * pairings already used consistently across the older, well-audited
 * pages (Devices, ChangeRequests, AlertCenter, CommandPalette). Newer
 * pages (JitAccess, FirmwareUpgrades, Drift) grew their own dark:
 * classes ad hoc -- some inconsistent, some (FirmwareUpgrades, Drift)
 * missing dark: entirely -- so this is the canonical map every page
 * should read from instead of re-deriving a shade by eye. Plain
 * strings (not Tailwind @apply) so a page can compose them inline,
 * e.g. `className={SURFACE.card}` or
 * `className={`${SURFACE.card} p-4`}`.
 *
 * Not exhaustive -- one-off accents (status pill colors, severity
 * colors) stay local to their component -- this covers only the
 * structural surfaces (panel backgrounds, borders, text hierarchy,
 * inputs, hover/divider states) that recur on nearly every page.
 */
export const SURFACE = {
  // Card / panel container
  card: "bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700",
  // Page background behind cards (rarely needed -- most pages rely on Layout's own bg)
  page: "bg-slate-50 dark:bg-slate-950",
  // Sunken well inside a card (stat tiles, code blocks, nested panels)
  well: "bg-slate-50 dark:bg-slate-900",

  // Borders
  border: "border-slate-200 dark:border-slate-700",
  borderSubtle: "border-slate-100 dark:border-slate-700/50",

  // Text hierarchy
  heading: "text-navy dark:text-white",
  body: "text-slate-700 dark:text-slate-200",
  muted: "text-slate-500 dark:text-slate-400",
  faint: "text-slate-400 dark:text-slate-500",

  // Form controls
  input: "border border-slate-200 dark:border-slate-600 bg-white dark:bg-slate-900 text-slate-700 dark:text-slate-200",

  // Interactive states
  hoverRow: "hover:bg-slate-50 dark:hover:bg-slate-700/50",
  divider: "border-slate-100 dark:border-slate-700/50",
} as const;