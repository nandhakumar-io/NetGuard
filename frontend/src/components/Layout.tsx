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

function NavGroupSection({ group, defaultOpen }: { group: NavGroup; defaultOpen: boolean }) {
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
        className="w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-[11px] font-semibold uppercase tracking-wider text-slate-400 dark:text-noc-muted hover:bg-white/5 hover:text-slate-200 dark:hover:text-noc-text transition-colors"
      >
        <span className="text-slate-500 dark:text-noc-muted">{group.icon}</span>
        <span className="flex-1 text-left">{group.label}</span>
        <svg
          width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"
          className={`transition-transform duration-150 ${open ? "rotate-90" : ""}`}
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
          <div className="pl-4 pt-1 pb-1 space-y-0.5">
            {group.items.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  `block px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${
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
  // Sidebar is a fixed 64-wide column on desktop (md and up) but an
  // off-canvas drawer on phones/small tablets -- at w-64 it would otherwise
  // eat most or all of a phone's viewport width with no way to reach the
  // page content underneath it.
  const [mobileNavOpen, setMobileNavOpen] = useState(false);

  // Close the drawer automatically on navigation, so tapping a nav link
  // doesn't leave the drawer covering the page you just navigated to.
  useEffect(() => {
    setMobileNavOpen(false);
  }, [location.pathname]);

  return (
    <div className="min-h-screen flex bg-slate-50 dark:bg-slate-900 text-navy dark:text-slate-100">
      {mobileNavOpen && (
        <div
          className="fixed inset-0 z-30 bg-black/50 md:hidden"
          onClick={() => setMobileNavOpen(false)}
          aria-hidden="true"
        />
      )}
      <aside
        className={`w-64 bg-navy dark:bg-noc-panel dark:border-r dark:border-noc-border text-white flex-shrink-0 flex flex-col fixed inset-y-0 left-0 z-40 transition-transform duration-200 ease-out md:static md:translate-x-0 ${
          mobileNavOpen ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        <div className="px-5 py-6 border-b border-white/10 dark:border-noc-border flex items-center justify-between">
          <div>
            <p className="font-display text-xl font-bold tracking-widest uppercase">NetGuard</p>
            <p className="text-[11px] text-accent dark:text-noc-cyan mt-0.5 noc-label uppercase tracking-wider">Network Ops Console</p>
          </div>
          <button
            onClick={() => setMobileNavOpen(false)}
            aria-label="Close navigation"
            className="md:hidden w-8 h-8 flex items-center justify-center rounded-lg text-slate-300 hover:bg-white/10 hover:text-white transition-colors"
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M18 6L6 18M6 6l12 12" />
            </svg>
          </button>
        </div>
        <nav className="flex-1 px-3 py-4 space-y-1 overflow-y-auto">
          {groups.map((group) => (
            <NavGroupSection
              key={group.label}
              group={group}
              defaultOpen={isGroupActive(group, location.pathname)}
            />
          ))}
        </nav>
        <div className="px-5 py-4 border-t border-white/10 dark:border-noc-border">
          {user && (
            <div className="mb-3">
              <p className="text-sm font-medium truncate">{user.full_name}</p>
              <p className="text-[11px] text-accent dark:text-noc-cyan capitalize">{user.role.replace(/_/g, " ")}</p>
            </div>
          )}
          <button
            onClick={async () => {
              await logout();
              navigate("/login");
            }}
            className="text-[11px] text-slate-400 dark:text-noc-muted hover:text-white dark:hover:text-noc-text transition-colors"
          >
            Sign out
          </button>
          <p className="text-[11px] text-slate-500 dark:text-noc-faint mt-2 noc-num">v1.0 · Prototype</p>
        </div>
      </aside>
      <main className="flex-1 flex flex-col overflow-y-auto dark:bg-noc-bg min-w-0">
        <header className="h-14 shrink-0 border-b border-slate-200 dark:border-noc-border bg-white dark:bg-noc-panel flex items-center justify-between gap-2 px-3 sm:px-6">
          <button
            onClick={() => setMobileNavOpen(true)}
            aria-label="Open navigation"
            className="md:hidden w-9 h-9 shrink-0 flex items-center justify-center rounded-lg text-slate-500 dark:text-noc-muted hover:bg-slate-100 dark:hover:bg-noc-panel2 transition-colors"
          >
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M3 6h18M3 12h18M3 18h18" />
            </svg>
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