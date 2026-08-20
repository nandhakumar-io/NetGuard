import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";

interface AdminUser {
  id: string;
  email: string;
  full_name: string;
  role: string;
  extra_roles: string[];
  is_active: boolean;
  mfa_enabled: boolean;
  sso_provider: string | null;
  created_at: string | null;
  last_login_at: string | null;
}

interface RoleCounts {
  total: number;
  network_admin: number;
  network_engineer: number;
  noc_engineer: number;
  security: number;
  auditor: number;
  disabled: number;
}

const ROLE_OPTIONS = ["network_admin", "network_engineer", "noc_engineer", "security", "auditor"];

const ROLE_BADGE: Record<string, string> = {
  network_admin: "bg-red-100 text-red-700",
  network_engineer: "bg-blue-100 text-blue-700",
  noc_engineer: "bg-purple-100 text-purple-700",
  security: "bg-amber-100 text-amber-700",
  auditor: "bg-slate-100 text-slate-600",
};

function fmtDate(iso: string | null): string {
  if (!iso) return "Never";
  return new Date(iso).toLocaleString(undefined, {
    month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit",
  });
}

function roleLabel(role: string): string {
  return role.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export default function Users() {
  const { user: currentUser } = useAuth();
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [counts, setCounts] = useState<RoleCounts | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | string>("all");
  const [search, setSearch] = useState("");
  const [showNewUser, setShowNewUser] = useState(false);
  const [permissionsUser, setPermissionsUser] = useState<AdminUser | null>(null);
  const [actioningId, setActioningId] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.get("/users");
      setUsers(res.data.users);
      setCounts(res.data.counts);
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to load users");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const toggleActive = async (u: AdminUser) => {
    setActioningId(u.id);
    try {
      await api.patch(`/users/${u.id}/status`, { is_active: !u.is_active });
      await load();
    } catch (err: any) {
      alert(err?.response?.data?.detail || "Failed to update status");
    } finally {
      setActioningId(null);
    }
  };

  const revokeSessions = async (u: AdminUser) => {
    if (!confirm(`Force sign-out ${u.email} from every device? Any active session will be ended immediately.`)) return;
    setActioningId(u.id);
    try {
      const res = await api.post(`/users/${u.id}/revoke-sessions`);
      const count = res.data.revoked_count;
      alert(count > 0 ? `Signed out ${count} active session${count === 1 ? "" : "s"} for ${u.email}.` : `${u.email} has no active sessions.`);
    } catch (err: any) {
      alert(err?.response?.data?.detail || "Failed to revoke sessions");
    } finally {
      setActioningId(null);
    }
  };

  const removeUser = async (u: AdminUser) => {
    if (!confirm(`Permanently delete ${u.email}? This cannot be undone.`)) return;
    setActioningId(u.id);
    try {
      await api.delete(`/users/${u.id}`);
      await load();
    } catch (err: any) {
      alert(err?.response?.data?.detail || "Failed to delete user");
    } finally {
      setActioningId(null);
    }
  };

  const filtered = users.filter((u) => {
    if (filter !== "all" && u.role !== filter) return false;
    if (search && !u.email.toLowerCase().includes(search.toLowerCase()) && !u.full_name.toLowerCase().includes(search.toLowerCase())) {
      return false;
    }
    return true;
  });

  return (
    <div className="p-6 max-w-7xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <div className="w-11 h-11 rounded-xl bg-brandblue flex items-center justify-center text-white">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2" /><circle cx="9" cy="7" r="4" />
              <path d="M23 21v-2a4 4 0 00-3-3.87M16 3.13a4 4 0 010 7.75" />
            </svg>
          </div>
          <div>
            <h1 className="text-2xl font-bold text-navy dark:text-white">User Management</h1>
            <p className="text-sm text-slate-500 dark:text-slate-400">Manage accounts, roles and group-level access</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={load} className="p-2 rounded-lg border border-slate-200 dark:border-slate-700 hover:bg-slate-50 dark:hover:bg-slate-800">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M23 4v6h-6M1 20v-6h6" /><path d="M3.51 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.49 15" />
            </svg>
          </button>
          <button
            onClick={() => setShowNewUser(true)}
            className="flex items-center gap-1.5 bg-brandblue text-white font-bold px-4 py-2 rounded-lg hover:opacity-90"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M12 5v14M5 12h14" /></svg>
            New User
          </button>
        </div>
      </div>

      {error && <div className="mb-4 p-3 rounded-lg bg-red-50 text-red-700 text-sm">{error}</div>}

      {/* Role descriptions */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3 mb-6">
        <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-4 flex gap-3">
          <span className="w-2 h-2 mt-1.5 rounded-full bg-red-500 shrink-0" />
          <div>
            <div className="font-bold text-navy dark:text-white">Network Admin</div>
            <div className="text-xs text-slate-500 dark:text-slate-400">Full access to all devices, config, and settings</div>
          </div>
        </div>
        <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-4 flex gap-3">
          <span className="w-2 h-2 mt-1.5 rounded-full bg-blue-500 shrink-0" />
          <div>
            <div className="font-bold text-navy dark:text-white">Engineer / NOC</div>
            <div className="text-xs text-slate-500 dark:text-slate-400">Can manage devices in their assigned groups</div>
          </div>
        </div>
        <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-4 flex gap-3">
          <span className="w-2 h-2 mt-1.5 rounded-full bg-slate-400 shrink-0" />
          <div>
            <div className="font-bold text-navy dark:text-white">Auditor</div>
            <div className="text-xs text-slate-500 dark:text-slate-400">Read-only access — cannot execute any actions</div>
          </div>
        </div>
      </div>

      {/* Stat cards */}
      {counts && (
        <div className="grid grid-cols-2 md:grid-cols-6 gap-3 mb-6">
          {[
            { label: "Total", value: counts.total, color: "text-navy dark:text-white" },
            { label: "Admin", value: counts.network_admin, color: "text-red-600" },
            { label: "Engineer", value: counts.network_engineer, color: "text-blue-600" },
            { label: "NOC", value: counts.noc_engineer, color: "text-purple-600" },
            { label: "Security", value: counts.security, color: "text-amber-600" },
            { label: "Disabled", value: counts.disabled, color: "text-slate-400" },
          ].map((c) => (
            <div key={c.label} className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-4">
              <div className={`text-2xl font-bold ${c.color}`}>{c.value}</div>
              <div className="text-[11px] uppercase tracking-wide text-slate-400 font-bold mt-1">{c.label}</div>
            </div>
          ))}
        </div>
      )}

      {/* Filters */}
      <div className="flex items-center gap-2 mb-3">
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search users..."
          className="flex-1 max-w-xs px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 text-sm"
        />
        <button onClick={() => setFilter("all")} className={`px-3 py-1.5 rounded-full text-xs font-bold ${filter === "all" ? "bg-brandblue text-white" : "bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-300"}`}>All</button>
        {ROLE_OPTIONS.map((r) => (
          <button
            key={r}
            onClick={() => setFilter(r)}
            className={`px-3 py-1.5 rounded-full text-xs font-bold capitalize ${filter === r ? "bg-brandblue text-white" : "bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-300"}`}
          >
            {roleLabel(r)}
          </button>
        ))}
      </div>

      {/* Table */}
      <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 dark:bg-slate-900/40 text-[11px] uppercase tracking-wide text-slate-400 font-bold">
            <tr>
              <th className="text-left py-2.5 px-4">User</th>
              <th className="text-left py-2.5 px-4">Role</th>
              <th className="text-left py-2.5 px-4">Status</th>
              <th className="text-left py-2.5 px-4">Last Login</th>
              <th className="text-left py-2.5 px-4">MFA</th>
              <th className="text-right py-2.5 px-4">Actions</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={6} className="text-center py-8 text-slate-400">Loading…</td></tr>
            ) : filtered.length === 0 ? (
              <tr><td colSpan={6} className="text-center py-8 text-slate-400">No users match.</td></tr>
            ) : (
              filtered.map((u) => (
                <tr key={u.id} className="border-t border-slate-100 dark:border-slate-700/50">
                  <td className="py-2.5 px-4">
                    <div className="flex items-center gap-2.5">
                      <div className="w-8 h-8 rounded-full bg-slate-200 dark:bg-slate-700 flex items-center justify-center font-bold text-xs text-slate-600 dark:text-slate-300">
                        {u.full_name.charAt(0).toUpperCase()}
                      </div>
                      <div>
                        <div className="font-bold text-navy dark:text-white flex items-center gap-1.5">
                          {u.full_name}
                          {currentUser?.id === u.id && <span className="text-[10px] font-bold text-slate-400">you</span>}
                          {u.sso_provider && <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 dark:bg-slate-700 text-slate-500">SSO</span>}
                        </div>
                        <div className="text-xs text-slate-400">{u.email}</div>
                      </div>
                    </div>
                  </td>
                  <td className="py-2.5 px-4">
                    <span className={`px-2 py-0.5 rounded-full text-xs font-bold ${ROLE_BADGE[u.role] || "bg-slate-100 text-slate-600"}`}>
                      {roleLabel(u.role)}
                    </span>
                    {u.extra_roles.length > 0 && (
                      <div className="flex flex-wrap gap-1 mt-1">
                        {u.extra_roles.map((r) => (
                          <span
                            key={r}
                            title="Custom permission: granted this role's access in addition to their base role"
                            className={`px-1.5 py-0.5 rounded text-[10px] font-bold border border-dashed ${ROLE_BADGE[r] || "bg-slate-100 text-slate-600"}`}
                          >
                            +{roleLabel(r)}
                          </span>
                        ))}
                      </div>
                    )}
                  </td>
                  <td className="py-2.5 px-4">
                    <span className={`px-2 py-0.5 rounded-full text-xs font-bold ${u.is_active ? "bg-emerald-100 text-emerald-700" : "bg-slate-100 text-slate-500"}`}>
                      {u.is_active ? "Active" : "Disabled"}
                    </span>
                  </td>
                  <td className="py-2.5 px-4 text-slate-500 dark:text-slate-400">{fmtDate(u.last_login_at)}</td>
                  <td className="py-2.5 px-4">
                    {u.mfa_enabled
                      ? <span className="text-xs font-bold text-emerald-600">Enabled</span>
                      : <span className="text-xs text-slate-400">Off</span>}
                  </td>
                  <td className="py-2.5 px-4 text-right">
                    <div className="flex justify-end gap-3">
                      <button
                        disabled={actioningId === u.id}
                        onClick={() => setPermissionsUser(u)}
                        title="Grant this user access to specific other roles' features, on top of their base role"
                        className="text-xs font-bold text-slate-500 hover:underline disabled:opacity-40 disabled:no-underline"
                      >
                        Permissions
                      </button>
                      <button
                        disabled={actioningId === u.id || currentUser?.id === u.id}
                        onClick={() => toggleActive(u)}
                        className="text-xs font-bold text-brandblue hover:underline disabled:opacity-40 disabled:no-underline"
                      >
                        {u.is_active ? "Disable" : "Enable"}
                      </button>
                      <button
                        disabled={actioningId === u.id || currentUser?.id === u.id}
                        onClick={() => revokeSessions(u)}
                        title="End every active session for this user immediately"
                        className="text-xs font-bold text-amber-600 hover:underline disabled:opacity-40 disabled:no-underline"
                      >
                        Force Sign-Out
                      </button>
                      <button
                        disabled={actioningId === u.id || currentUser?.id === u.id || u.is_active}
                        onClick={() => removeUser(u)}
                        title={u.is_active ? "Disable the account before deleting it" : undefined}
                        className="text-xs font-bold text-red-600 hover:underline disabled:opacity-40 disabled:no-underline"
                      >
                        Delete
                      </button>
                    </div>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {showNewUser && <NewUserModal onClose={() => setShowNewUser(false)} onCreated={load} />}
      {permissionsUser && (
        <PermissionsModal user={permissionsUser} onClose={() => setPermissionsUser(null)} onSaved={load} />
      )}
    </div>
  );
}

function PermissionsModal({ user, onClose, onSaved }: { user: AdminUser; onClose: () => void; onSaved: () => void }) {
  const [selected, setSelected] = useState<Set<string>>(new Set(user.extra_roles));
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const toggle = (role: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(role)) next.delete(role);
      else next.add(role);
      return next;
    });
  };

  const submit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      await api.patch(`/users/${user.id}/permissions`, { extra_roles: Array.from(selected) });
      onSaved();
      onClose();
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to update permissions");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4" onClick={onClose}>
      <div className="bg-white dark:bg-slate-800 rounded-xl w-full max-w-md p-6" onClick={(e) => e.stopPropagation()}>
        <h2 className="text-lg font-bold text-navy dark:text-white mb-1">Custom Permissions</h2>
        <p className="text-xs text-slate-500 dark:text-slate-400 mb-4">
          {user.full_name} is a <span className="font-bold">{roleLabel(user.role)}</span>. Grant access to
          specific other roles' features below, without changing their base role.
        </p>
        {error && <div className="mb-3 p-2.5 rounded-lg bg-red-50 text-red-700 text-xs">{error}</div>}
        <div className="space-y-2">
          {ROLE_OPTIONS.filter((r) => r !== user.role).map((r) => (
            <label
              key={r}
              className="flex items-center gap-2.5 px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 text-sm cursor-pointer hover:bg-slate-50 dark:hover:bg-slate-900"
            >
              <input
                type="checkbox"
                checked={selected.has(r)}
                onChange={() => toggle(r)}
                className="rounded border-slate-300 dark:border-slate-600"
              />
              <span className={`px-2 py-0.5 rounded-full text-xs font-bold ${ROLE_BADGE[r] || "bg-slate-100 text-slate-600"}`}>
                {roleLabel(r)}
              </span>
              <span className="text-xs text-slate-400">access</span>
            </label>
          ))}
        </div>
        <div className="flex justify-end gap-2 mt-5">
          <button onClick={onClose} className="px-4 py-2 rounded-lg text-sm font-bold text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-700">
            Cancel
          </button>
          <button
            onClick={submit}
            disabled={submitting}
            className="px-4 py-2 rounded-lg text-sm font-bold bg-brandblue text-white hover:opacity-90 disabled:opacity-50"
          >
            {submitting ? "Saving…" : "Save Permissions"}
          </button>
        </div>
      </div>
    </div>
  );
}

function NewUserModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [email, setEmail] = useState("");
  const [fullName, setFullName] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("network_engineer");
  const [extraRoles, setExtraRoles] = useState<Set<string>>(new Set());
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const toggleExtra = (r: string) => {
    setExtraRoles((prev) => {
      const next = new Set(prev);
      if (next.has(r)) next.delete(r);
      else next.add(r);
      return next;
    });
  };

  const submit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      await api.post("/users", {
        email,
        full_name: fullName,
        password,
        role,
        extra_roles: Array.from(extraRoles),
      });
      onCreated();
      onClose();
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to create user");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
      <div className="bg-white dark:bg-slate-800 rounded-xl w-full max-w-md p-6">
        <h2 className="text-lg font-bold text-navy dark:text-white mb-4">New User</h2>
        {error && <div className="mb-3 p-2.5 rounded-lg bg-red-50 text-red-700 text-xs">{error}</div>}
        <div className="space-y-3">
          <div>
            <label className="text-xs font-bold text-slate-500 block mb-1">Full Name</label>
            <input value={fullName} onChange={(e) => setFullName(e.target.value)} className="w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 text-sm" />
          </div>
          <div>
            <label className="text-xs font-bold text-slate-500 block mb-1">Email</label>
            <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} className="w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 text-sm" />
          </div>
          <div>
            <label className="text-xs font-bold text-slate-500 block mb-1">Temporary Password</label>
            <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} className="w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 text-sm" />
          </div>
          <div>
            <label className="text-xs font-bold text-slate-500 block mb-1">Role</label>
            <select value={role} onChange={(e) => setRole(e.target.value)} className="w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 text-sm capitalize">
              {ROLE_OPTIONS.map((r) => <option key={r} value={r}>{roleLabel(r)}</option>)}
            </select>
          </div>
          <div>
            <label className="text-xs font-bold text-slate-500 block mb-1">
              Custom Permissions <span className="font-normal text-slate-400">(optional, beyond their role)</span>
            </label>
            <div className="space-y-1.5">
              {ROLE_OPTIONS.filter((r) => r !== role).map((r) => (
                <label key={r} className="flex items-center gap-2 text-xs text-slate-600 dark:text-slate-300 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={extraRoles.has(r)}
                    onChange={() => toggleExtra(r)}
                    className="rounded border-slate-300 dark:border-slate-600"
                  />
                  {roleLabel(r)} access
                </label>
              ))}
            </div>
          </div>
        </div>
        <div className="flex justify-end gap-2 mt-5">
          <button onClick={onClose} className="px-4 py-2 rounded-lg text-sm font-bold text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-700">Cancel</button>
          <button
            onClick={submit}
            disabled={submitting || !email || !fullName || !password}
            className="px-4 py-2 rounded-lg text-sm font-bold bg-brandblue text-white hover:opacity-90 disabled:opacity-50"
          >
            {submitting ? "Creating…" : "Create User"}
          </button>
        </div>
      </div>
    </div>
  );
}