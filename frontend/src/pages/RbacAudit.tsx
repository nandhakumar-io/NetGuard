import { useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";

interface RbacUser {
  id: string;
  email: string;
  full_name: string;
  role: string;
  is_active: boolean;
  mfa_enabled: boolean;
  created_at: string | null;
  total_audit_events: number;
  last_action: string | null;
  last_action_result: string | null;
  last_action_at: string | null;
}

interface RbacUsersResponse {
  users: RbacUser[];
  total_users: number;
  never_active_count: number;
  stale_accounts_90d: string[];
}

interface MatrixEntry {
  resource: string;
  endpoint: string;
  roles: string[];
}

interface MatrixResponse {
  roles: string[];
  matrix: MatrixEntry[];
}

const ROLE_BADGE: Record<string, string> = {
  network_admin: "bg-red-100 text-red-700",
  network_engineer: "bg-blue-100 text-blue-700",
  noc_engineer: "bg-purple-100 text-purple-700",
  security: "bg-amber-100 text-amber-700",
  auditor: "bg-slate-100 text-slate-600",
};

export default function RbacAudit() {
  const [users, setUsers] = useState<RbacUsersResponse | null>(null);
  const [matrix, setMatrix] = useState<MatrixResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [roleFilter, setRoleFilter] = useState("all");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    Promise.all([api.get<RbacUsersResponse>("/rbac/users"), api.get<MatrixResponse>("/rbac/matrix")])
      .then(([u, m]) => {
        setUsers(u.data);
        setMatrix(m.data);
      })
      .catch((err) => setError(err?.response?.data?.detail || "You don't have access to this view."))
      .finally(() => setLoading(false));
  }, []);

  const filtered = useMemo(() => {
    if (!users) return [];
    if (roleFilter === "all") return users.users;
    return users.users.filter((u) => u.role === roleFilter);
  }, [users, roleFilter]);

  if (error) {
    return (
      <div>
        <h1 className="text-2xl font-bold text-navy dark:text-white">RBAC Audit</h1>
        <p className="text-sm text-red-600 mt-3">{error}</p>
      </div>
    );
  }

  return (
    <div>
      <h1 className="text-2xl font-bold text-navy dark:text-white">RBAC Audit</h1>
      <p className="text-sm text-slate-500 mt-1">
        Who has access to what, cross-referenced with actual activity from the audit trail.
      </p>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mt-5 mb-6">
        <div className="bg-white dark:bg-noc-panel border border-slate-200 dark:border-noc-border rounded-xl p-4">
          <p className="text-xs text-slate-400 uppercase tracking-wide font-semibold">Total users</p>
          <p className="text-2xl font-bold text-navy dark:text-white mt-1">{users?.total_users ?? "—"}</p>
        </div>
        <div className="bg-white dark:bg-noc-panel border border-slate-200 dark:border-noc-border rounded-xl p-4">
          <p className="text-xs text-slate-400 uppercase tracking-wide font-semibold">Never active</p>
          <p className="text-2xl font-bold text-amber-600 mt-1">{users?.never_active_count ?? "—"}</p>
        </div>
        <div className="bg-white dark:bg-noc-panel border border-slate-200 dark:border-noc-border rounded-xl p-4 col-span-2">
          <p className="text-xs text-slate-400 uppercase tracking-wide font-semibold">Stale accounts (90d+ inactive)</p>
          <p className="text-sm text-navy dark:text-white mt-1">
            {users && users.stale_accounts_90d.length > 0 ? users.stale_accounts_90d.join(", ") : "None"}
          </p>
        </div>
      </div>

      <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-400 mb-3">Permission Matrix</h2>
      <div className="bg-white dark:bg-noc-panel border border-slate-200 dark:border-noc-border rounded-xl overflow-hidden mb-8">
        <table className="w-full text-sm">
          <thead className="bg-navy text-white">
            <tr>
              <th className="text-left px-4 py-3 font-semibold">Resource</th>
              <th className="text-left px-4 py-3 font-semibold">Endpoint(s)</th>
              <th className="text-left px-4 py-3 font-semibold">Allowed roles</th>
            </tr>
          </thead>
          <tbody>
            {(matrix?.matrix ?? []).map((m, i) => (
              <tr key={m.resource} className={i % 2 ? "bg-slate-50 dark:bg-white/5" : ""}>
                <td className="px-4 py-3 text-navy dark:text-white">{m.resource}</td>
                <td className="px-4 py-3 text-slate-500 font-mono text-xs">{m.endpoint}</td>
                <td className="px-4 py-3">
                  <div className="flex flex-wrap gap-1">
                    {m.roles.map((r) => (
                      <span key={r} className={`px-2 py-0.5 rounded-full text-xs font-medium ${ROLE_BADGE[r] || "bg-slate-100 text-slate-600"}`}>
                        {r.replace(/_/g, " ")}
                      </span>
                    ))}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="flex items-center justify-between mb-3">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-400">Users</h2>
        <select
          className="border border-slate-300 dark:border-noc-border dark:bg-noc-panel rounded-lg px-3 py-1.5 text-sm"
          value={roleFilter}
          onChange={(e) => setRoleFilter(e.target.value)}
        >
          <option value="all">All roles</option>
          {(matrix?.roles ?? []).map((r) => (
            <option key={r} value={r}>
              {r.replace(/_/g, " ")}
            </option>
          ))}
        </select>
      </div>

      <div className="bg-white dark:bg-noc-panel border border-slate-200 dark:border-noc-border rounded-xl overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-navy text-white">
            <tr>
              <th className="text-left px-4 py-3 font-semibold">User</th>
              <th className="text-left px-4 py-3 font-semibold">Role</th>
              <th className="text-left px-4 py-3 font-semibold">MFA</th>
              <th className="text-left px-4 py-3 font-semibold">Last action</th>
              <th className="text-left px-4 py-3 font-semibold">Last activity</th>
              <th className="text-left px-4 py-3 font-semibold">Total events</th>
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr>
                <td colSpan={6} className="text-center text-slate-400 py-8">Loading users…</td>
              </tr>
            )}
            {!loading && filtered.length === 0 && (
              <tr>
                <td colSpan={6} className="text-center text-slate-400 py-8">No users match this filter.</td>
              </tr>
            )}
            {filtered.map((u, i) => (
              <tr key={u.id} className={i % 2 ? "bg-slate-50 dark:bg-white/5" : ""}>
                <td className="px-4 py-3">
                  <p className="font-medium text-navy dark:text-white">{u.full_name}</p>
                  <p className="text-xs text-slate-500">{u.email}</p>
                </td>
                <td className="px-4 py-3">
                  <span className={`px-2 py-1 rounded-full text-xs font-medium ${ROLE_BADGE[u.role] || "bg-slate-100 text-slate-600"}`}>
                    {u.role.replace(/_/g, " ")}
                  </span>
                </td>
                <td className="px-4 py-3">
                  <span className={`px-2 py-1 rounded-full text-xs font-medium ${u.mfa_enabled ? "bg-green-100 text-green-700" : "bg-slate-100 text-slate-500"}`}>
                    {u.mfa_enabled ? "Enabled" : "Off"}
                  </span>
                </td>
                <td className="px-4 py-3 text-slate-600">{u.last_action || "—"}</td>
                <td className="px-4 py-3 text-slate-500 whitespace-nowrap">
                  {u.last_action_at ? new Date(u.last_action_at).toLocaleString() : "Never"}
                </td>
                <td className="px-4 py-3 text-slate-600">{u.total_audit_events}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}