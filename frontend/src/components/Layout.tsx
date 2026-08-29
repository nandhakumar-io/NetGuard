import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useAuth, isAdmin, hasPermission } from "../lib/auth";
import type { CurrentUser } from "../lib/auth";
import { useTheme } from "../lib/theme";
import NotificationBell from "./NotificationBell";
import CommandPalette from "./CommandPalette";

type NavItem = { to: string; label: string; end?: boolean; icon: JSX.Element };
type NavGroup = { label: string; icon: JSX.Element; items: NavItem[] };

// Sidebar pages hidden by default unless the viewer is a Network Admin
// (or, for terminal-recordings, Security) -- an admin can grant any of
// these to a specific user via Users > Custom Permissions without
// promoting them to a whole other role. Mirrors backend app.core.
// permissions.PAGE_PERMISSIONS; the backend remains the real enforcement
// point (some of these routes are also role-gated server-side, e.g.
// /users, /backups), this just keeps the nav from listing pages a given
// user can't actually do anything on.
const RESTRICTED_PAGE_PERMISSIONS: Record<string, string> = {
  "/users": "page:users",
  "/integrations": "page:integrations",
  "/backups": "page:backups",
  "/terminal-recordings": "page:terminal-recordings",
  "/audit-log": "page:audit-log",
  "/auditor-export": "page:auditor-export",
  "/rbac-audit": "page:rbac-audit",
};

// Auditors and Security already have a legitimate standing reason to see
// the audit/compliance pages without needing an individual grant.
const ROLE_DEFAULT_ACCESS: Record<string, string[]> = {
  "/audit-log": ["auditor", "security"],
  "/auditor-export": ["auditor", "security"],
  "/rbac-audit": ["auditor"],
};

// /tenant-board is cross-tenant NOC data -- MSP staff only (they need
// is_msp_staff, not just network_admin). /tenants (CRUD) is now open
// to network_admin too, since they're the operators who manage customer
// environments. Kept as a separate Set so the comment stays clear.
const MSP_STAFF_ONLY_PAGES = new Set(["/tenant-board"]);
// Pages that network_admin can see via isAdmin(), without a named permission.
const ADMIN_ONLY_PAGES = new Set(["/tenants"]);

function canSeePage(user: CurrentUser | null, to: string): boolean {
  if (MSP_STAFF_ONLY_PAGES.has(to)) return !!user?.is_msp_staff;
  if (ADMIN_ONLY_PAGES.has(to)) return isAdmin(user) || !!user?.is_msp_staff;
  // MSP staff can now also view/delete terminal session recordings
  // (any functional role -- see backend app.api.terminal_recordings.
  // _reviewer_only/_deleter_only), same as the ADMIN_ONLY_PAGES bypass
  // above, without needing a per-page permission grant.
  if (to === "/terminal-recordings" && user?.is_msp_staff) return true;
  const permKey = RESTRICTED_PAGE_PERMISSIONS[to];
  if (!permKey) return true; // not a restricted page -- unchanged, open to every authenticated role
  if (isAdmin(user)) return true;
  if (user && ROLE_DEFAULT_ACCESS[to]?.includes(user.role)) return true;
  if (hasPermission(user, permKey)) return true;
  // "Logs & Audit Export" is a convenience bundle covering both
  // export-adjacent pages, so an admin doesn't have to grant two
  // near-identical permissions for what's really one capability.
  if ((to === "/audit-log" || to === "/auditor-export") && hasPermission(user, "logs_export")) return true;
  return false;
}

function filterGroupsForUser(allGroups: NavGroup[], user: CurrentUser | null): NavGroup[] {
  return allGroups
    .map((group) => ({ ...group, items: group.items.filter((item) => canSeePage(user, item.to)) }))
    .filter((group) => group.items.length > 0);
}

// Slightly larger than the old fixed 16px so each glyph reads clearly in
// the collapsed 72px rail -- every item below also gets its own distinct
// icon (previously the rail rendered the *group's* icon once per item,
// so a 5-item group showed 5 identical icons stacked on top of each other).
const iconProps = { width: 18, height: 18, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2 } as const;

const groups: NavGroup[] = [
  {
    label: "Overview",
    icon: <svg {...iconProps}><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" /></svg>,
    items: [
      {
        to: "/", label: "Dashboard", end: true,
        icon: <svg {...iconProps}><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" /></svg>,
      },
      {
        to: "/insights", label: "Insights",
        icon: <svg {...iconProps}><path d="M3 3v18h18" /><path d="M7 15l4-5 3 3 5-7" /></svg>,
      },
      {
        to: "/tenant-board", label: "Tenant Board",
        icon: <svg {...iconProps}><rect x="3" y="4" width="7" height="16" rx="1" /><rect x="14" y="4" width="7" height="16" rx="1" /><path d="M6.5 9h0M17.5 9h0" /></svg>,
      },
      {
        to: "/tenants", label: "Tenants",
        icon: <svg {...iconProps}><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2" /><circle cx="9" cy="7" r="4" /><path d="M23 21v-2a4 4 0 00-3-3.87" /><path d="M16 3.13a4 4 0 010 7.75" /></svg>,
      },
      {
        to: "/reports", label: "Reports",
        icon: <svg {...iconProps}><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z" /><path d="M14 2v6h6" /><path d="M8 13h8M8 17h4M8 9h2" /></svg>,
      },
    ],
  },
  {
    label: "Change Management",
    icon: <svg {...iconProps}><path d="M9 11l3 3L22 4" /><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11" /></svg>,
    items: [
      {
        to: "/change-requests", label: "Change Requests",
        icon: <svg {...iconProps}><path d="M9 11l3 3L22 4" /><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11" /></svg>,
      },
      {
        to: "/deployments", label: "Deployments",
        icon: <svg {...iconProps}><path d="M12 15V3" /><path d="M7 8l5-5 5 5" /><path d="M4 15v4a2 2 0 002 2h12a2 2 0 002-2v-4" /></svg>,
      },
      {
        to: "/templates", label: "Templates",
        icon: <svg {...iconProps}><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z" /><path d="M14 2v6h6" /><path d="M9 13h6M9 17h6" /></svg>,
      },
      {
        to: "/maintenance-windows", label: "Maintenance Windows",
        icon: <svg {...iconProps}><path d="M14.7 6.3a4 4 0 11-5.4 5.4L4 17v3h3l5.3-5.3a4 4 0 015.4-5.4z" /><path d="M17 4l3 3" /></svg>,
      },
      {
        to: "/firmware-upgrades", label: "Firmware Upgrades",
        icon: <svg {...iconProps}><rect x="6" y="6" width="12" height="12" rx="1" /><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3" /></svg>,
      },
    ],
  },
  {
    label: "Inventory",
    icon: <svg {...iconProps}><rect x="2" y="7" width="20" height="14" rx="2" /><path d="M16 21V5a2 2 0 00-2-2h-4a2 2 0 00-2 2v16" /></svg>,
    items: [
      {
        to: "/devices", label: "Devices",
        icon: <svg {...iconProps}><rect x="2" y="7" width="20" height="14" rx="2" /><path d="M16 21V5a2 2 0 00-2-2h-4a2 2 0 00-2 2v16" /></svg>,
      },
      {
        to: "/discovery", label: "Discovery",
        icon: <svg {...iconProps}><circle cx="11" cy="11" r="7" /><path d="M21 21l-4.3-4.3" /><path d="M11 8v3l2 2" /></svg>,
      },
      {
        to: "/groups", label: "Groups",
        icon: <svg {...iconProps}><path d="M12 2l9 5-9 5-9-5 9-5z" /><path d="M3 12l9 5 9-5" /><path d="M3 17l9 5 9-5" /></svg>,
      },
      {
        to: "/ipam", label: "IPAM",
        icon: <svg {...iconProps}><rect x="3" y="4" width="18" height="6" rx="1" /><rect x="3" y="14" width="18" height="6" rx="1" /><path d="M7 10v4M11 10v4M15 10v4" /></svg>,
      },
      {
        to: "/config-search", label: "Config Search",
        icon: <svg {...iconProps}><circle cx="11" cy="11" r="7" /><path d="M21 21l-4.3-4.3" /></svg>,
      },
    ],
  },
  {
    label: "Network Visibility",
    icon: <svg {...iconProps}><circle cx="12" cy="5" r="2" /><circle cx="5" cy="19" r="2" /><circle cx="19" cy="19" r="2" /><path d="M12 7v6M12 13l-5.5 4M12 13l5.5 4" /></svg>,
    items: [
      {
        to: "/topology", label: "Topology",
        icon: <svg {...iconProps}><circle cx="12" cy="5" r="2" /><circle cx="5" cy="19" r="2" /><circle cx="19" cy="19" r="2" /><path d="M12 7v6M12 13l-5.5 4M12 13l5.5 4" /></svg>,
      },
      {
        to: "/path-trace", label: "Path Trace",
        icon: <svg {...iconProps}><circle cx="5" cy="6" r="2" /><circle cx="19" cy="18" r="2" /><path d="M5 8v4a4 4 0 004 4h6" /></svg>,
      },
      {
        to: "/traffic-analysis", label: "Traffic Analysis",
        icon: <svg {...iconProps}><path d="M2 12h4l2-7 4 14 3-9 2 5h5" /></svg>,
      },
      {
        to: "/syslog", label: "Syslog",
        icon: <svg {...iconProps}><rect x="2" y="4" width="20" height="16" rx="2" /><path d="M6 9l3 3-3 3M12 15h6" /></svg>,
      },
      {
        to: "/drift", label: "Drift",
        icon: <svg {...iconProps}><circle cx="6" cy="6" r="3" /><circle cx="6" cy="18" r="3" /><path d="M6 9v6" /><path d="M18 6a9 9 0 01-9 9" /></svg>,
      },
    ],
  },
  {
    label: "Alerting",
    icon: <svg {...iconProps}><path d="M18 8a6 6 0 00-12 0c0 7-3 9-3 9h18s-3-2-3-9" /><path d="M13.73 21a2 2 0 01-3.46 0" /></svg>,
    items: [
      {
        to: "/alerts", label: "Alerts",
        icon: <svg {...iconProps}><path d="M18 8a6 6 0 00-12 0c0 7-3 9-3 9h18s-3-2-3-9" /><path d="M13.73 21a2 2 0 01-3.46 0" /></svg>,
      },
      {
        to: "/alert-runbooks", label: "Alert Runbooks",
        icon: <svg {...iconProps}><path d="M4 19.5A2.5 2.5 0 016.5 17H20" /><path d="M6.5 2H20v20H6.5A2.5 2.5 0 014 19.5v-15A2.5 2.5 0 016.5 2z" /></svg>,
      },
      {
        to: "/escalation-policies", label: "Escalation Policies",
        icon: <svg {...iconProps}><path d="M12 2v4M12 18v4M4.9 4.9l2.8 2.8M16.3 16.3l2.8 2.8M2 12h4M18 12h4M4.9 19.1l2.8-2.8M16.3 7.7l2.8-2.8" /><circle cx="12" cy="12" r="3" /></svg>,
      },
      {
        to: "/on-call-schedules", label: "On-Call Schedules",
        icon: <svg {...iconProps}><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 3" /><path d="M8 2l-2 2M18 2l2 2" /></svg>,
      },
      {
        to: "/noc", label: "NOC Mode (mobile)",
        icon: <svg {...iconProps}><rect x="6" y="2" width="12" height="20" rx="2" /><path d="M11 18h2" /></svg>,
      },
      {
        to: "/wallboard", label: "Wall Board (TV)",
        icon: <svg {...iconProps}><rect x="2" y="4" width="20" height="14" rx="2" /><path d="M8 21h8M12 18v3" /></svg>,
      },
      {
        to: "/incidents", label: "Incidents",
        icon: <svg {...iconProps}><path d="M12 9v4M12 17h.01" /><path d="M10.3 3.9L1.8 18a1.5 1.5 0 001.3 2.3h17.8a1.5 1.5 0 001.3-2.3L13.7 3.9a1.5 1.5 0 00-2.6 0z" /></svg>,
      },
    ],
  },
  {
    label: "Security & Access",
    icon: <svg {...iconProps}><path d="M12 2l8 4v6c0 5-3.4 8.4-8 10-4.6-1.6-8-5-8-10V6l8-4z" /></svg>,
    items: [
      {
        to: "/security", label: "Security",
        icon: <svg {...iconProps}><path d="M12 2l8 4v6c0 5-3.4 8.4-8 10-4.6-1.6-8-5-8-10V6l8-4z" /></svg>,
      },
      {
        to: "/jit-access", label: "JIT Access",
        icon: <svg {...iconProps}><circle cx="8" cy="8" r="5" /><path d="M11.5 11.5L21 21M16 16l3-3M19 19l2-2" /></svg>,
      },
      {
        to: "/audit-log", label: "Audit Log",
        icon: <svg {...iconProps}><path d="M4 4h16v16H4z" /><path d="M8 8h8M8 12h8M8 16h4" /></svg>,
      },
      {
        to: "/auditor-export", label: "Auditor Export",
        icon: <svg {...iconProps}><path d="M12 3v12" /><path d="M7 10l5 5 5-5" /><path d="M4 19h16" /></svg>,
      },
      {
        to: "/terminal-recordings", label: "Session Recordings",
        icon: <svg {...iconProps}><rect x="2" y="4" width="20" height="16" rx="2" /><path d="M6 9l4 3-4 3M12 15h6" /></svg>,
      },
      {
        to: "/rbac-audit", label: "RBAC Audit",
        icon: <svg {...iconProps}><circle cx="9" cy="8" r="3" /><path d="M2 20a7 7 0 0114 0" /><circle cx="18" cy="8" r="2.2" /><path d="M16.5 4.5a2.2 2.2 0 013 2 2.2 2.2 0 01-1.5 2.1" /><path d="M17 13.5a6.3 6.3 0 015 6.5h-3" /></svg>,
      },
      {
        to: "/integrations", label: "Integrations",
        icon: <svg {...iconProps}><path d="M9 3H5a2 2 0 00-2 2v4" /><path d="M15 3h4a2 2 0 012 2v4" /><path d="M9 21H5a2 2 0 01-2-2v-4" /><path d="M15 21h4a2 2 0 002-2v-4" /><rect x="9" y="9" width="6" height="6" rx="1" /></svg>,
      },
      {
        to: "/users", label: "Users",
        icon: <svg {...iconProps}><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2" /><circle cx="9" cy="7" r="4" /><path d="M23 21v-2a4 4 0 00-3-3.87M16 3.13a4 4 0 010 7.75" /></svg>,
      },
    ],
  },
  {
    label: "Lab",
    icon: <svg {...iconProps}><path d="M9 2v6L4 20a1 1 0 001 1h14a1 1 0 001-1L15 8V2" /><path d="M9 2h6" /></svg>,
    items: [
      {
        to: "/lab", label: "GNS3 Lab",
        icon: <svg {...iconProps}><path d="M9 2v6L4 20a1 1 0 001 1h14a1 1 0 001-1L15 8V2" /><path d="M9 2h6" /></svg>,
      },
    ],
  },
  {
    label: "System",
    icon: <svg {...iconProps}><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 11-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09a1.65 1.65 0 00-1-1.51 1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 11-2.83-2.83l.06-.06a1.65 1.65 0 00.33-1.82 1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09a1.65 1.65 0 001.51-1 1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 112.83-2.83l.06.06a1.65 1.65 0 001.82.33H9a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 112.83 2.83l-.06.06a1.65 1.65 0 00-.33 1.82V9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z" /></svg>,
    items: [
      {
        to: "/jobs", label: "Jobs",
        icon: <svg {...iconProps}><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 3" /></svg>,
      },
      {
        to: "/backups", label: "Backups",
        icon: <svg {...iconProps}><ellipse cx="12" cy="5" rx="9" ry="3" /><path d="M3 5v14a9 3 0 0018 0V5" /><path d="M3 12a9 3 0 0018 0" /></svg>,
      },
    ],
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
  iconOnly = false,
}: {
  group: NavGroup;
  defaultOpen: boolean;
  onNavigate?: () => void;
  iconOnly?: boolean;
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

  if (iconOnly) {
    // Collapsed rail: no group headers/labels at all, just a flat icon
    // dock of every item in the group, each with a native title tooltip.
    // Avoids any opacity/width fade choreography entirely.
    return (
      <div className="space-y-0.5">
        {group.items.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            title={item.label}
            onClick={onNavigate}
            className={({ isActive }) =>
              `flex items-center justify-center w-full h-9 rounded-lg transition-colors ${
                isActive
                  ? "bg-brandblue text-white dark:bg-noc-cyan/15 dark:text-noc-cyan"
                  : "text-slate-300 dark:text-noc-muted hover:bg-white/5 hover:text-white dark:hover:text-noc-text"
              }`
            }
          >
            {item.icon}
          </NavLink>
        ))}
      </div>
    );
  }

  return (
    <div>
      <button
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        title={group.label}
        className="w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-[11px] font-semibold uppercase tracking-wider text-slate-400 dark:text-noc-muted hover:bg-white/5 hover:text-slate-200 dark:hover:text-noc-text transition-colors"
      >
        <span className="text-slate-500 dark:text-noc-muted shrink-0">{group.icon}</span>
        <span className="flex-1 text-left whitespace-nowrap">{group.label}</span>
        <svg
          width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"
          className={`transition-transform duration-150 shrink-0 ${open ? "rotate-90" : ""}`}
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

  // Desktop (md+) sidebar collapse: a real React state toggle (button
  // click), not a CSS :hover trick. Previously the rail collapsed to 72px
  // and only expanded on :hover with label text faded in via opacity on a
  // separate timing from the width transition -- that combination is what
  // made text intermittently invisible/clipped and the rail feel
  // unreliable to expand. A click-driven toggle with text conditionally
  // rendered (not just faded) removes that whole class of bug and gives
  // predictable, persistent expand/collapse instead of a hover-only state.
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem("ng6-sidebar-collapsed") === "1");
  const [isHovered, setIsHovered] = useState(false);
  const effectivelyCollapsed = collapsed && !isHovered;

  useEffect(() => {
    localStorage.setItem("ng6-sidebar-collapsed", collapsed ? "1" : "0");
  }, [collapsed]);

  // Mobile/tablet (<md): the sidebar is a true off-canvas drawer. It is
  // fixed + translated fully off-screen when closed, so it never
  // occupies layout space and can never sit on top of the page. A
  // hamburger button in the header opens it; it then slides in above a
  // dimmed backdrop, and tapping the backdrop, a nav link, or the close
  // button dismisses it.
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

  const visibleGroups = filterGroupsForUser(groups, user);

  const sidebarContent = (onNavigate?: () => void, iconOnly?: boolean) => (
    <>
      <div className="px-4 py-6 border-b border-white/10 dark:border-noc-border flex items-center gap-3 overflow-hidden">
        <div className="w-8 h-8 shrink-0 flex items-center justify-center rounded-lg bg-white/10 text-white font-display font-bold text-sm">
          NG
        </div>
        {!iconOnly && (
          <div className="min-w-0">
            <p className="font-display text-xl font-bold tracking-widest uppercase whitespace-nowrap">NetGuard</p>
            <p className="text-[11px] text-slate-300 dark:text-noc-muted mt-0.5 noc-label uppercase tracking-wider whitespace-nowrap">Network Ops Console</p>
          </div>
        )}
        <button
          onClick={() => setMobileOpen(false)}
          aria-label="Close navigation"
          className="md:hidden ml-auto w-8 h-8 shrink-0 flex items-center justify-center rounded-lg text-white/70 hover:bg-white/10 hover:text-white transition-colors"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M18 6L6 18M6 6l12 12" /></svg>
        </button>
      </div>
      <nav className="flex-1 px-3 py-4 space-y-1 overflow-y-auto overflow-x-hidden">
        {visibleGroups.map((group) => (
          <NavGroupSection
            key={group.label}
            group={group}
            defaultOpen={isGroupActive(group, location.pathname)}
            onNavigate={onNavigate}
            iconOnly={iconOnly}
          />
        ))}
      </nav>
      <div className="px-4 py-4 border-t border-white/10 dark:border-noc-border overflow-hidden">
        {user && !iconOnly && (
          <div className="mb-3 min-w-0">
            <p className="text-sm font-medium truncate">{user.full_name}</p>
            <p className="text-[11px] text-slate-300 dark:text-noc-muted capitalize truncate">{user.role.replace(/_/g, " ")}</p>
          </div>
        )}
        <button
          onClick={async () => {
            await logout();
            navigate("/login");
          }}
          title="Sign out"
          className={`flex items-center gap-2.5 text-[11px] text-slate-400 dark:text-noc-muted hover:text-white dark:hover:text-noc-text transition-colors ${iconOnly ? "justify-center w-full" : ""}`}
        >
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="shrink-0">
            <path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4" /><path d="M16 17l5-5-5-5" /><path d="M21 12H9" />
          </svg>
          {!iconOnly && <span className="whitespace-nowrap">Sign out</span>}
        </button>
        {!iconOnly && <p className="text-[11px] text-slate-500 dark:text-noc-faint mt-2 noc-num whitespace-nowrap">v1.0 · Prototype</p>}
      </div>
    </>
  );

  return (
    <div className="min-h-screen bg-slate-50 dark:bg-slate-900 text-navy dark:text-slate-100 md:flex">
      {/* Desktop sidebar: a normal static column (no :hover dependency).
          Collapse/expand is a real click-driven toggle (state below,
          persisted in localStorage), swapping between a 264px full panel
          with labels and a 72px icon-only rail with tooltips -- so it's
          both always legible and actually flexible, unlike the previous
          hover-only rail where the width and label-opacity transitions
          could drift out of sync and leave text clipped/invisible. */}
      <aside
        onMouseEnter={() => setIsHovered(true)}
        onMouseLeave={() => setIsHovered(false)}
        className={`hidden md:flex bg-navy dark:bg-noc-panel dark:border-r dark:border-noc-border text-white flex-shrink-0 flex-col md:static overflow-hidden z-20 transition-[width] duration-200 ease-in-out ${
          effectivelyCollapsed ? "w-[72px]" : "w-64"
        }`}
      >
        {sidebarContent(undefined, effectivelyCollapsed)}
        <button
          onClick={() => setCollapsed((c) => !c)}
          aria-label={collapsed ? "Expand navigation" : "Collapse navigation"}
          title={collapsed ? "Expand navigation" : "Collapse navigation"}
          className="shrink-0 h-9 flex items-center justify-center gap-2 text-slate-400 dark:text-noc-muted hover:bg-white/5 hover:text-white dark:hover:text-noc-text border-t border-white/10 dark:border-noc-border transition-colors"
        >
          <svg
            width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"
            className={`transition-transform duration-200 ${collapsed ? "rotate-180" : ""}`}
          >
            <path d="M15 6l-6 6 6 6" />
          </svg>
          {!effectivelyCollapsed && <span className="text-[11px] font-semibold uppercase tracking-wider">{collapsed ? "Pin Expand" : "Collapse"}</span>}
        </button>
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