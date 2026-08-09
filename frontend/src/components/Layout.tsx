import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { useAuth } from "../lib/auth";
import { useTheme } from "../lib/theme";
import NotificationBell from "./NotificationBell";
import CommandPalette from "./CommandPalette";

const links = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/change-requests", label: "Change Requests" },
  { to: "/deployments", label: "Deployments" },
  { to: "/devices", label: "Devices" },
  { to: "/groups", label: "Groups" },
  { to: "/config-search", label: "Config Search" },
  { to: "/templates", label: "Templates" },
  { to: "/topology", label: "Topology" },
  { to: "/path-trace", label: "Path Trace" },
  { to: "/syslog", label: "Syslog" },
  { to: "/traffic-analysis", label: "Traffic Analysis" },
  { to: "/drift", label: "Drift" },
  { to: "/alerts", label: "Alerts" },
  { to: "/maintenance-windows", label: "Maintenance Windows" },
  { to: "/firmware-upgrades", label: "Firmware Upgrades" },
  { to: "/incidents", label: "Incidents" },
  { to: "/insights", label: "Insights" },
  { to: "/lab", label: "GNS3 Lab" },
  { to: "/audit-log", label: "Audit Log" },
  { to: "/rbac-audit", label: "RBAC Audit" },
  { to: "/jit-access", label: "JIT Access" },
  { to: "/security", label: "Security" },
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

export default function Layout() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();

  return (
    <div className="min-h-screen flex bg-slate-50 dark:bg-slate-900 text-navy dark:text-slate-100">
      <aside className="w-60 bg-navy dark:bg-noc-panel dark:border-r dark:border-noc-border text-white flex-shrink-0 flex flex-col">
        <div className="px-5 py-6 border-b border-white/10 dark:border-noc-border">
          <p className="font-display text-xl font-bold tracking-widest uppercase">NetGuard</p>
          <p className="text-[11px] text-accent dark:text-noc-cyan mt-0.5 noc-label uppercase tracking-wider">Network Ops Console</p>
        </div>
        <nav className="flex-1 px-3 py-4 space-y-1 overflow-y-auto">
          {links.map((l) => (
            <NavLink
              key={l.to}
              to={l.to}
              end={l.end}
              className={({ isActive }) =>
                `block px-3 py-2 rounded-lg text-sm font-medium transition-colors ${
                  isActive
                    ? "bg-brandblue text-white dark:bg-noc-cyan/15 dark:text-noc-cyan dark:border-l-2 dark:border-noc-cyan"
                    : "text-slate-300 dark:text-noc-muted hover:bg-white/5 hover:text-white dark:hover:text-noc-text"
                }`
              }
            >
              {l.label}
            </NavLink>
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
      <main className="flex-1 flex flex-col overflow-y-auto dark:bg-noc-bg">
        <header className="h-14 shrink-0 border-b border-slate-200 dark:border-noc-border bg-white dark:bg-noc-panel flex items-center justify-between gap-2 px-6">
          <button
            onClick={() => window.dispatchEvent(new Event("open-command-palette"))}
            className="flex items-center gap-2 text-xs text-slate-400 dark:text-noc-muted border border-slate-200 dark:border-noc-border rounded-lg px-3 py-1.5 hover:bg-slate-50 dark:hover:bg-noc-panel2 transition-colors max-w-xs w-full"
          >
            <span>🔎</span>
            <span className="flex-1 text-left">Search devices, alerts, configs…</span>
            <span className="font-mono text-[10px] bg-slate-100 dark:bg-noc-panel2 dark:border dark:border-noc-border px-1.5 py-0.5 rounded">⌘K</span>
          </button>
          <div className="flex items-center gap-2">
            <ThemeToggle />
            <NotificationBell />
          </div>
        </header>
        <div className="flex-1 p-8">
          <Outlet />
        </div>
      </main>
      <CommandPalette />
    </div>
  );
}