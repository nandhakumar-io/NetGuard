import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../lib/auth";
import { useTheme } from "../lib/theme";
import NotificationBell from "./NotificationBell";
import CommandPalette from "./CommandPalette";

type NavItem = { to: string; label: string; end?: boolean };
type NavGroup = { label: string; icon: JSX.Element; items: NavItem[] };

const iconProps = { width: 16, height: 16, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2 } as const;

const groups: NavGroup[] = [
  {
    label: "Overview",
    icon: <svg {...iconProps}><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" /></svg>,
    items: [{ to: "/", label: "Dashboard", end: true }, { to: "/insights", label: "Insights" }],
  },
  {
    label: "Change Management",
    icon: <svg {...iconProps}><path d="M9 11l3 3L22 4" /><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11" /></svg>,
    items: [
      { to: "/change-requests", label: "Change Requests" },
      { to: "/deployments", label: "Deployments" },
      { to: "/templates", label: "Templates" },
      { to: "/maintenance-windows", label: "Maintenance Windows" },
      { to: "/firmware-upgrades", label: "Firmware Upgrades" },
    ],
  },
  {
    label: "Inventory",
    icon: <svg {...iconProps}><rect x="2" y="7" width="20" height="14" rx="2" /><path d="M16 21V5a2 2 0 00-2-2h-4a2 2 0 00-2 2v16" /></svg>,
    items: [
      { to: "/devices", label: "Devices" },
      { to: "/groups", label: "Groups" },
      { to: "/config-search", label: "Config Search" },
    ],
  },
  {
    label: "Network Visibility",
    icon: <svg {...iconProps}><circle cx="12" cy="5" r="2" /><circle cx="5" cy="19" r="2" /><circle cx="19" cy="19" r="2" /><path d="M12 7v6M12 13l-5.5 4M12 13l5.5 4" /></svg>,
    items: [
      { to: "/topology", label: "Topology" },
      { to: "/path-trace", label: "Path Trace" },
      { to: "/traffic-analysis", label: "Traffic Analysis" },
      { to: "/syslog", label: "Syslog" },
      { to: "/drift", label: "Drift" },
    ],
  },
  {
    label: "Alerting",
    icon: <svg {...iconProps}><path d="M18 8a6 6 0 00-12 0c0 7-3 9-3 9h18s-3-2-3-9" /><path d="M13.73 21a2 2 0 01-3.46 0" /></svg>,
    items: [
      { to: "/alerts", label: "Alerts" },
      { to: "/incidents", label: "Incidents" },
    ],
  },
  {
    label: "Security & Access",
    icon: <svg {...iconProps}><path d="M12 2l8 4v6c0 5-3.4 8.4-8 10-4.6-1.6-8-5-8-10V6l8-4z" /></svg>,
    items: [
      { to: "/security", label: "Security" },
      { to: "/jit-access", label: "JIT Access" },
      { to: "/audit-log", label: "Audit Log" },
      { to: "/rbac-audit", label: "RBAC Audit" },
      { to: "/integrations", label: "Integrations" },
    ],
  },
  {
    label: "Lab",
    icon: <svg {...iconProps}><path d="M9 2v6L4 20a1 1 0 001 1h14a1 1 0 001-1L15 8V2" /><path d="M9 2h6" /></svg>,
    items: [{ to: "/lab", label: "GNS3 Lab" }],
  },
  {
    label: "System",
    icon: <svg {...iconProps}><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 11-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09a1.65 1.65 0 00-1-1.51 1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 11-2.83-2.83l.06-.06a1.65 1.65 0 00.33-1.82 1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09a1.65 1.65 0 001.51-1 1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 112.83-2.83l.06.06a1.65 1.65 0 001.82.33H9a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 112.83 2.83l-.06.06a1.65 1.65 0 00-.33 1.82V9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z" /></svg>,
    items: [{ to: "/jobs", label: "Jobs" }],
  },
];

function ThemeToggle() {
  const { theme, toggleTheme } = useTheme();
  const isDark = theme === "dark";
  return (
    <button
      onClick={toggleTheme}
      title={isDark ? "Switch to light mode" : "Switch to dark mode"}
      aria-label="Toggle color theme"
      className="w-9 h-9 rounded-lg flex items-center justify-center text-slate-500 hover:bg-slate-100 hover:text-navy dark:text-slate-400 dark:hover:bg-slate-700 dark:hover:text-white transition-colors"
    >
      {isDark ? (
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <circle cx="12" cy="12" r="4" />
          <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" />
        </svg>
      ) : (
        <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
          <path d="M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z" />
        </svg>
      )}
    </button>
  );
}

function isGroupActive(group: NavGroup, pathname: string): boolean {
  return group.items.some((item) =>
    item.end ? pathname === item.to : pathname === item.to || pathname.startsWith(item.to + "/")
  );
}

function NavGroupSection({
  group,
  defaultOpen,
  onNavigate,
  fadeClass = "",
}: {
  group: NavGroup;
  defaultOpen: boolean;
  onNavigate?: () => void;
  fadeClass?: string;
}) {
  // Each group manages its own open/closed state -- click the header to
  // expand, click again to collapse, independent of the other groups.
  // Re-syncs to defaultOpen when the active route moves into/out of this
  // group (e.g. navigating via the command palette), so the group
  // containing the current page stays expanded automatically without
  // fighting a manual toggle mid-session.
  const [open, setOpen] = useState(defaultOpen);

  useEffect(() => {
    if (defaultOpen) setOpen(true);
  }, [defaultOpen]);

  return (
    <div>
      <button
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        title={group.label}
        className="w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-[11px] font-semibold uppercase tracking-wider text-slate-400 dark:text-noc-muted hover:bg-white/5 hover:text-slate-200 dark:hover:text-noc-text transition-colors"
      >
        <span className="text-slate-500 dark:text-noc-muted shrink-0">{group.icon}</span>
        <span className={`flex-1 text-left ${fadeClass || "whitespace-nowrap"}`}>{group.label}</span>
        <svg
          width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"
          className={`transition-transform duration-150 shrink-0 ${open ? "rotate-90" : ""} ${fadeClass}`}
        >
          <path d="M9 6l6 6-6 6" />
        </svg>
      </button>
      <div
        className={`grid overflow-hidden transition-[grid-template-rows] duration-200 ease-out ${
          open ? "grid-rows-[1fr]" : "grid-rows-[0fr]"
        }`}
      >
        <div className="min-h-0 overflow-hidden">
          <div className={`pl-4 pt-1 pb-1 space-y-0.5 ${fadeClass}`}>
            {group.items.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                title={item.label}
                onClick={onNavigate}
                className={({ isActive }) =>
                  `block px-3 py-1.5 rounded-lg text-sm font-medium transition-colors whitespace-nowrap ${
                    isActive
                      ? "bg-brandblue text-white dark:bg-noc-cyan/15 dark:text-noc-cyan dark:border-l-2 dark:border-noc-cyan"
                      : "text-slate-300 dark:text-noc-muted hover:bg-white/5 hover:text-white dark:hover:text-noc-text"
                  }`
                }
              >
                {item.label}
              </NavLink>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

export default function Layout() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  // Desktop (md+): the sidebar is a normal static column, exactly as
  // before -- always visible at w-64, taking up real space in the flex
  // row so the page content sits beside it. This is untouched.
  //
  // Mobile/tablet (<md): the sidebar is a true off-canvas drawer. It is
  // fixed + translated fully off-screen when closed, so it never
  // occupies layout space and can never sit on top of the page. A
  // hamburger button in the header opens it; it then slides in above a
  // dimmed backdrop, and tapping the backdrop, a nav link, or the close
  // button dismisses it. This replaces the previous hover/pin icon-rail,
  // which -- because it depended on `:hover` (unavailable on touch) and
  // kept the sidebar `fixed` with the page padded to match -- could get
  // out of sync and leave the expanded rail sitting on top of the page.
  const [mobileOpen, setMobileOpen] = useState(false);

  // Close the drawer automatically on navigation.
  useEffect(() => {
    setMobileOpen(false);
  }, [location.pathname]);

  // Prevent background scroll while the mobile drawer is open.
  useEffect(() => {
    if (mobileOpen) {
      const prev = document.body.style.overflow;
      document.body.style.overflow = "hidden";
      return () => {
        document.body.style.overflow = prev;
      };
    }
  }, [mobileOpen]);

  // Fade-only class applied to any label/text that should disappear while
  // the rail is collapsed and fade back in on hover. Icons/badges never
  // carry this class, so the collapsed rail still reads as a usable icon
  // dock rather than a sliver of clipped text.
  const fade =
    "opacity-0 group-hover/side:opacity-100 transition-opacity duration-150 group-hover/side:delay-100 whitespace-nowrap";

  const sidebarContent = (onNavigate?: () => void, collapsible?: boolean) => (
    <>
      <div className="px-4 py-6 border-b border-white/10 dark:border-noc-border flex items-center gap-3 overflow-hidden">
        <div className="w-8 h-8 shrink-0 flex items-center justify-center rounded-lg bg-white/10 text-white font-display font-bold text-sm">
          NG
        </div>
        <div className={`min-w-0 ${collapsible ? fade : ""}`}>
          <p className="font-display text-xl font-bold tracking-widest uppercase whitespace-nowrap">NetGuard</p>
          <p className="text-[11px] text-accent dark:text-noc-cyan mt-0.5 noc-label uppercase tracking-wider whitespace-nowrap">Network Ops Console</p>
        </div>
        <button
          onClick={() => setMobileOpen(false)}
          aria-label="Close navigation"
          className="md:hidden ml-auto w-8 h-8 shrink-0 flex items-center justify-center rounded-lg text-white/70 hover:bg-white/10 hover:text-white transition-colors"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M18 6L6 18M6 6l12 12" /></svg>
        </button>
      </div>
      <nav className="flex-1 px-3 py-4 space-y-1 overflow-y-auto overflow-x-hidden">
        {groups.map((group) => (
          <NavGroupSection
            key={group.label}
            group={group}
            defaultOpen={isGroupActive(group, location.pathname)}
            onNavigate={onNavigate}
            fadeClass={collapsible ? fade : ""}
          />
        ))}
      </nav>
      <div className="px-4 py-4 border-t border-white/10 dark:border-noc-border overflow-hidden">
        {user && (
          <div className={`mb-3 min-w-0 ${collapsible ? fade : ""}`}>
            <p className="text-sm font-medium truncate">{user.full_name}</p>
            <p className="text-[11px] text-accent dark:text-noc-cyan capitalize truncate">{user.role.replace(/_/g, " ")}</p>
          </div>
        )}
        <button
          onClick={async () => {
            await logout();
            navigate("/login");
          }}
          title="Sign out"
          className="flex items-center gap-2.5 text-[11px] text-slate-400 dark:text-noc-muted hover:text-white dark:hover:text-noc-text transition-colors"
        >
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="shrink-0">
            <path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4" /><path d="M16 17l5-5-5-5" /><path d="M21 12H9" />
          </svg>
          <span className={collapsible ? fade : "whitespace-nowrap"}>Sign out</span>
        </button>
        <p className={`text-[11px] text-slate-500 dark:text-noc-faint mt-2 noc-num ${collapsible ? fade : "whitespace-nowrap"}`}>v1.0 · Prototype</p>
      </div>
    </>
  );

  return (
    <div className="min-h-screen bg-slate-50 dark:bg-slate-900 text-navy dark:text-slate-100 md:flex">
      {/* Desktop sidebar: collapses to a 72px icon rail and expands to a
          full 264px panel on hover. Pure CSS (group-hover) so there's no
          layout-thrash from React state, and it lives as a normal flex
          item -- not fixed/absolute -- so main content reflows smoothly
          alongside it instead of being covered. */}
      <aside
        className="hidden md:flex group/side bg-navy dark:bg-noc-panel dark:border-r dark:border-noc-border text-white flex-shrink-0 flex-col md:static w-[72px] hover:w-64 transition-[width] duration-300 ease-in-out overflow-hidden z-20"
      >
        {sidebarContent(undefined, true)}
      </aside>

      {/* Mobile backdrop */}
      <div
        onClick={() => setMobileOpen(false)}
        aria-hidden="true"
        className={`md:hidden fixed inset-0 z-40 bg-black/50 transition-opacity duration-200 ${
          mobileOpen ? "opacity-100 pointer-events-auto" : "opacity-0 pointer-events-none"
        }`}
      />

      {/* Mobile off-canvas drawer */}
      <aside
        className={`md:hidden bg-navy dark:bg-noc-panel dark:border-r dark:border-noc-border text-white flex flex-col fixed inset-y-0 left-0 z-50 w-72 max-w-[85vw] shadow-xl transition-transform duration-200 ease-out ${
          mobileOpen ? "translate-x-0" : "-translate-x-full"
        }`}
        role="dialog"
        aria-modal="true"
        aria-label="Navigation menu"
      >
        {sidebarContent(() => setMobileOpen(false), false)}
      </aside>

      <main className="flex-1 flex flex-col overflow-y-auto dark:bg-noc-bg min-w-0">
        <header className="h-14 shrink-0 border-b border-slate-200 dark:border-noc-border bg-white dark:bg-noc-panel flex items-center justify-between gap-2 px-3 sm:px-6 sticky top-0 z-30">
          <button
            onClick={() => setMobileOpen(true)}
            aria-label="Open navigation"
            className="md:hidden w-9 h-9 shrink-0 flex items-center justify-center rounded-lg text-slate-500 hover:bg-slate-100 hover:text-navy dark:text-slate-400 dark:hover:bg-slate-700 dark:hover:text-white transition-colors"
          >
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M4 6h16M4 12h16M4 18h16" /></svg>
          </button>
          <button
            onClick={() => window.dispatchEvent(new Event("open-command-palette"))}
            className="flex items-center gap-2 text-xs text-slate-400 dark:text-noc-muted border border-slate-200 dark:border-noc-border rounded-lg px-3 py-1.5 hover:bg-slate-50 dark:hover:bg-noc-panel2 transition-colors w-full md:max-w-xs"
          >
            <span>🔎</span>
            <span className="flex-1 text-left truncate">
              <span className="hidden sm:inline">Search devices, alerts, configs…</span>
              <span className="sm:hidden">Search…</span>
            </span>
            <span className="hidden sm:inline font-mono text-[10px] bg-slate-100 dark:bg-noc-panel2 dark:border dark:border-noc-border px-1.5 py-0.5 rounded">⌘K</span>
          </button>
          <div className="flex items-center gap-1 sm:gap-2 shrink-0">
            <ThemeToggle />
            <NotificationBell />
          </div>
        </header>
        <div className="flex-1 p-4 sm:p-6 lg:p-8 min-w-0">
          <Outlet />
        </div>
      </main>
      <CommandPalette />
    </div>
  );
}