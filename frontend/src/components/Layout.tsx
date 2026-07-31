import { NavLink, Outlet } from "react-router-dom";

const links = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/change-requests", label: "Change Requests" },
  { to: "/devices", label: "Devices" },
  { to: "/audit-log", label: "Audit Log" },
];

export default function Layout() {
  return (
    <div className="min-h-screen flex bg-slate-50">
      <aside className="w-60 bg-navy text-white flex-shrink-0 flex flex-col">
        <div className="px-5 py-6 border-b border-white/10">
          <p className="text-lg font-bold tracking-tight">NetGuard AI</p>
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
                  isActive ? "bg-brandblue text-white" : "text-slate-300 hover:bg-white/5 hover:text-white"
                }`
              }
            >
              {l.label}
            </NavLink>
          ))}
        </nav>
        <div className="px-5 py-4 border-t border-white/10 text-[11px] text-slate-400">
          v1.0 · Prototype
        </div>
      </aside>
      <main className="flex-1 p-8 overflow-y-auto">
        <Outlet />
      </main>
    </div>
  );
}
