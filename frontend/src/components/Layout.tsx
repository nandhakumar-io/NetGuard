import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { useAuth } from "../lib/auth";
import { useTheme } from "../lib/theme";
import NotificationBell from "./NotificationBell";

const links = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/change-requests", label: "Change Requests" },
  { to: "/deployments", label: "Deployments" },
  { to: "/devices", label: "Devices" },
  { to: "/config-search", label: "Config Search" },
  { to: "/templates", label: "Templates" },
  { to: "/topology", label: "Topology" },
  { to: "/drift", label: "Drift" },
  { to: "/alerts", label: "Alerts" },
  { to: "/lab", label: "GNS3 Lab" },
  { to: "/audit-log", label: "Audit Log" },
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
      <aside className="w-60 bg-navy dark:bg-slate-950 text-white flex-shrink-0 flex flex-col">
        <div className="px-5 py-6 border-b border-white/10">
          <p className="text-lg font-bold tracking-tight">NetGuard</p>
          <p className="text-[11px] text-accent mt-0.5">Network Change Management</p>
        </div>
        <nav className="flex-1 px-3 py-4 space-y-1">
          {links.map((l) => (
            <NavLink
              key={l.to}
              to={l.to}
              end={l.end}
              className={({ isActive }) =>
                `block px-3 py-2 rounded-lg text-sm font-medium transition-colors ${
                  isActive ? "bg-brandblue text-white" : "text-slate-300 dark:text-slate-400 hover:bg-white/5 hover:text-white"
                }`
              }
            >
              {l.label}
            </NavLink>
          ))}
        </nav>
        <div className="px-5 py-4 border-t border-white/10">
          {user && (
            <div className="mb-3">
              <p className="text-sm font-medium truncate">{user.full_name}</p>
              <p className="text-[11px] text-accent capitalize">{user.role.replace(/_/g, " ")}</p>
            </div>
          )}
          <button
            onClick={async () => {
              await logout();
              navigate("/login");
            }}
            className="text-[11px] text-slate-400 dark:text-slate-500 hover:text-white transition-colors"
          >
            Sign out
          </button>
          <p className="text-[11px] text-slate-500 dark:text-slate-400 mt-2">v1.0 · Prototype</p>
        </div>
      </aside>
      <main className="flex-1 flex flex-col overflow-y-auto">
        <header className="h-14 shrink-0 border-b border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 flex items-center justify-end gap-2 px-6">
          <ThemeToggle />
          <NotificationBell />
        </header>
        <div className="flex-1 p-8">
          <Outlet />
        </div>
      </main>
    </div>
  );
}