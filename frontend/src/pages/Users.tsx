import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";

interface AdminUser {
  id: string;
  email: string;
  full_name: string;
  role: string;
  extra_roles: string[];
  extra_permissions: string[];
  is_active: boolean;
  is_approved: boolean;
  mfa_enabled: boolean;
  sso_provider: string | null;
  created_at: string | null;
  last_login_at: string | null;
  is_msp_staff: boolean;
  tenant_id: string | null;
  tenant_name: string | null;
}

interface TenantOption {
  id: string;
  name: string;
  slug: string;
}

interface PermissionCatalogEntry {
  key: string;
  label: string;
  description: string;
}

interface PermissionCatalog {
  capabilities: PermissionCatalogEntry[];
  pages: PermissionCatalogEntry[];
}

interface RoleCounts {
  total: number;
  network_admin: number;
  network_engineer: number;
  noc_engineer: number;
  security: number;
  auditor: number;
  disabled: number;
  pending_approval: number;
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
  const [tenancyUser, setTenancyUser] = useState<AdminUser | null>(null);
  const [actioningId, setActioningId] = useState<string | null>(null);
  const [resetPasswordResult, setResetPasswordResult] = useState<{ email: string; password: string } | null>(null);

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

  const resetPassword = async (u: AdminUser) => {
    if (
      !confirm(
        `Reset ${u.email}'s password? This generates a new temporary password, ends every active session for ` +
          `them, and shows the temporary password here once -- it isn't emailed or stored anywhere.`
      )
    )
      return;
    setActioningId(u.id);
    try {
      const res = await api.post(`/users/${u.id}/reset-password`);
      setResetPasswordResult({ email: u.email, password: res.data.temporary_password });
    } catch (err: any) {
      alert(err?.response?.data?.detail || "Failed to reset password");
    } finally {
      setActioningId(null);
    }
  };

  const approveUser = async (u: AdminUser) => {
    setActioningId(u.id);
    try {
      await api.post(`/users/${u.id}/approve`);
      await load();
    } catch (err: any) {
      alert(err?.response?.data?.detail || "Failed to approve user");
    } finally {
      setActioningId(null);
    }
  };

  const rejectUser = async (u: AdminUser) => {
    if (!confirm(`Reject ${u.email}'s registration? This deletes the pending account and cannot be undone.`)) return;
    setActioningId(u.id);
    try {
      await api.post(`/users/${u.id}/reject`);
      await load();
    } catch (err: any) {
      alert(err?.response?.data?.detail || "Failed to reject user");
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

      {/* Pending approval queue -- self-registered accounts (POST
         /auth/register) can't sign in until approved here. Only rendered
         when there's actually something waiting, so it doesn't take up
         permanent space on a deployment where nobody self-registers. */}
      {!!counts?.pending_approval && (
        <div className="mb-6 bg-amber-50 dark:bg-amber-950/20 border border-amber-200 dark:border-amber-900/50 rounded-xl overflow-hidden">
          <div className="px-4 py-3 flex items-center gap-2 border-b border-amber-200 dark:border-amber-900/50">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-amber-600 shrink-0">
              <circle cx="12" cy="12" r="10" /><path d="M12 6v6l4 2" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            <span className="font-bold text-sm text-amber-800 dark:text-amber-300">
              {counts.pending_approval} account{counts.pending_approval === 1 ? "" : "s"} awaiting approval
            </span>
          </div>
          <div className="divide-y divide-amber-200/70 dark:divide-amber-900/40">
            {users.filter((u) => !u.is_approved).map((u) => (
              <div key={u.id} className="px-4 py-2.5 flex items-center justify-between gap-3">
                <div>
                  <div className="font-bold text-sm text-navy dark:text-white">{u.full_name}</div>
                  <div className="text-xs text-slate-500 dark:text-slate-400">
                    {u.email} · requested {roleLabel(u.role)}{u.tenant_name ? ` · ${u.tenant_name}` : ""}
                  </div>
                </div>
                <div className="flex items-center gap-3 shrink-0">
                  <button
                    disabled={actioningId === u.id}
                    onClick={() => approveUser(u)}
                    className="text-xs font-bold text-white bg-emerald-600 hover:bg-emerald-700 px-3 py-1.5 rounded-lg disabled:opacity-40"
                  >
                    Approve
                  </button>
                  <button
                    disabled={actioningId === u.id}
                    onClick={() => rejectUser(u)}
                    className="text-xs font-bold text-red-600 hover:underline disabled:opacity-40 disabled:no-underline"
                  >
                    Reject
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

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
        <div className="grid grid-cols-2 md:grid-cols-7 gap-3 mb-6">
          {[
            { label: "Total", value: counts.total, color: "text-navy dark:text-white" },
            { label: "Admin", value: counts.network_admin, color: "text-red-600" },
            { label: "Engineer", value: counts.network_engineer, color: "text-blue-600" },
            { label: "NOC", value: counts.noc_engineer, color: "text-purple-600" },
            { label: "Security", value: counts.security, color: "text-amber-600" },
            { label: "Pending", value: counts.pending_approval, color: "text-amber-500" },
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
              <th className="text-left py-2.5 px-4">Tenant</th>
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
              <tr><td colSpan={7} className="text-center py-8 text-slate-400">No users match.</td></tr>
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
                    {u.extra_permissions.length > 0 && (
                      <div className="flex flex-wrap gap-1 mt-1">
                        {u.extra_permissions.map((p) => (
                          <span
                            key={p}
                            title="Custom permission: individually-granted capability or page access"
                            className="px-1.5 py-0.5 rounded text-[10px] font-bold border border-dashed bg-sky-50 text-sky-700 border-sky-200"
                          >
                            +{p.replace(/^page:/, "").replace(/_/g, " ")}
                          </span>
                        ))}
                      </div>
                    )}
                  </td>
                  <td className="py-2.5 px-4">
                    {u.is_msp_staff ? (
                      <span className="px-2 py-0.5 rounded-full text-xs font-bold bg-violet-100 text-violet-700 dark:bg-violet-950/50 dark:text-violet-300">
                        MSP Staff
                      </span>
                    ) : (
                      <span className="text-xs text-slate-500 dark:text-slate-400">{u.tenant_name || "Unassigned"}</span>
                    )}
                  </td>
                  <td className="py-2.5 px-4">
                    {!u.is_approved ? (
                      <span className="px-2 py-0.5 rounded-full text-xs font-bold bg-amber-100 text-amber-700 dark:bg-amber-950/50 dark:text-amber-300">
                        Pending Approval
                      </span>
                    ) : (
                      <span className={`px-2 py-0.5 rounded-full text-xs font-bold ${u.is_active ? "bg-emerald-100 text-emerald-700" : "bg-slate-100 text-slate-500"}`}>
                        {u.is_active ? "Active" : "Disabled"}
                      </span>
                    )}
                  </td>
                  <td className="py-2.5 px-4 text-slate-500 dark:text-slate-400">{fmtDate(u.last_login_at)}</td>
                  <td className="py-2.5 px-4">
                    {u.mfa_enabled
                      ? <span className="text-xs font-bold text-emerald-600">Enabled</span>
                      : <span className="text-xs text-slate-400">Off</span>}
                  </td>
                  <td className="py-2.5 px-4 text-right">
                    {!u.is_approved ? (
                      <div className="flex justify-end gap-3">
                        <button
                          disabled={actioningId === u.id}
                          onClick={() => approveUser(u)}
                          className="text-xs font-bold text-white bg-emerald-600 hover:bg-emerald-700 px-3 py-1.5 rounded-lg disabled:opacity-40"
                        >
                          Approve
                        </button>
                        <button
                          disabled={actioningId === u.id}
                          onClick={() => rejectUser(u)}
                          className="text-xs font-bold text-red-600 hover:underline disabled:opacity-40 disabled:no-underline"
                        >
                          Reject
                        </button>
                      </div>
                    ) : (
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
                        disabled={actioningId === u.id}
                        onClick={() => setTenancyUser(u)}
                        title="Assign this user to a tenant, or make them MSP staff (cross-tenant access)"
                        className="text-xs font-bold text-slate-500 hover:underline disabled:opacity-40 disabled:no-underline"
                      >
                        Tenancy
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
                        disabled={actioningId === u.id || currentUser?.id === u.id || !!u.sso_provider}
                        onClick={() => resetPassword(u)}
                        title={u.sso_provider ? "This account signs in via SSO and has no local password" : "Generate a new temporary password and end every active session"}
                        className="text-xs font-bold text-amber-600 hover:underline disabled:opacity-40 disabled:no-underline"
                      >
                        Reset Password
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
                    )}
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
      {tenancyUser && (
        <TenancyModal user={tenancyUser} onClose={() => setTenancyUser(null)} onSaved={load} />
      )}
      {resetPasswordResult && (
        <ResetPasswordResultModal result={resetPasswordResult} onClose={() => setResetPasswordResult(null)} />
      )}
    </div>
  );
}

function ResetPasswordResultModal({
  result,
  onClose,
}: {
  result: { email: string; password: string };
  onClose: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(result.password);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      /* clipboard unavailable -- the text below is still selectable */
    }
  };

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4" onClick={onClose}>
      <div
        className="bg-white dark:bg-slate-800 rounded-xl w-full max-w-md p-6"
        onClick={(e) => e.stopPropagation()}
      >
        <p className="text-sm font-semibold text-navy dark:text-white mb-1">
          Password reset for {result.email}
        </p>
        <p className="text-xs text-slate-500 dark:text-slate-400 mb-4">
          Every active session for this account was ended. This temporary password is shown once -- it isn't
          emailed or stored anywhere, so copy it now and relay it to the user through a trusted channel.
        </p>
        <div className="flex items-center gap-2 bg-slate-50 dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded-lg px-3 py-2 mb-4">
          <span className="font-mono text-sm text-navy dark:text-white flex-1 break-all select-all">
            {result.password}
          </span>
          <button
            onClick={copy}
            className="text-xs font-semibold text-brandblue hover:underline shrink-0"
          >
            {copied ? "Copied!" : "Copy"}
          </button>
        </div>
        <button
          onClick={onClose}
          className="w-full bg-brandblue text-white rounded-lg px-4 py-2 text-sm font-semibold hover:bg-navy transition-colors"
        >
          Done
        </button>
      </div>
    </div>
  );
}

function PermissionsModal({ user, onClose, onSaved }: { user: AdminUser; onClose: () => void; onSaved: () => void }) {
  const [selected, setSelected] = useState<Set<string>>(new Set(user.extra_roles));
  const [selectedPerms, setSelectedPerms] = useState<Set<string>>(new Set(user.extra_permissions));
  const [catalog, setCatalog] = useState<PermissionCatalog | null>(null);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .get<PermissionCatalog>("/users/permissions/catalog")
      .then((res) => setCatalog(res.data))
      .catch(() => setCatalogError("Failed to load the permissions catalog."));
  }, []);

  const toggle = (role: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(role)) next.delete(role);
      else next.add(role);
      return next;
    });
  };

  const togglePerm = (key: string) => {
    setSelectedPerms((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const submit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      await api.patch(`/users/${user.id}/permissions`, {
        extra_roles: Array.from(selected),
        extra_permissions: Array.from(selectedPerms),
      });
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
      <div
        className="bg-white dark:bg-slate-800 rounded-xl w-full max-w-md p-6 max-h-[85vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-lg font-bold text-navy dark:text-white mb-1">Custom Permissions</h2>
        <p className="text-xs text-slate-500 dark:text-slate-400 mb-4">
          {user.full_name} is a <span className="font-bold">{roleLabel(user.role)}</span>. Grant access to
          specific other roles' features, or individual capabilities/pages, without changing their base role.
        </p>
        {error && <div className="mb-3 p-2.5 rounded-lg bg-red-50 text-red-700 text-xs">{error}</div>}

        <div className="text-[11px] font-bold uppercase tracking-wide text-slate-400 mb-2">Whole Role Access</div>
        <div className="space-y-2 mb-5">
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

        <div className="text-[11px] font-bold uppercase tracking-wide text-slate-400 mb-2">Capabilities</div>
        {catalogError && <div className="mb-3 p-2.5 rounded-lg bg-red-50 text-red-700 text-xs">{catalogError}</div>}
        {!catalog && !catalogError && <div className="text-xs text-slate-400 mb-4">Loading…</div>}
        {catalog && (
          <div className="space-y-2 mb-5">
            {catalog.capabilities.map((p) => (
              <label
                key={p.key}
                className="flex items-start gap-2.5 px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 text-sm cursor-pointer hover:bg-slate-50 dark:hover:bg-slate-900"
              >
                <input
                  type="checkbox"
                  checked={selectedPerms.has(p.key)}
                  onChange={() => togglePerm(p.key)}
                  className="mt-0.5 rounded border-slate-300 dark:border-slate-600"
                />
                <span>
                  <span className="block font-bold text-navy dark:text-white text-xs">{p.label}</span>
                  <span className="block text-[11px] text-slate-400">{p.description}</span>
                </span>
              </label>
            ))}
          </div>
        )}

        <div className="text-[11px] font-bold uppercase tracking-wide text-slate-400 mb-2">Page Access</div>
        {catalog && (
          <div className="space-y-2">
            {catalog.pages.map((p) => (
              <label
                key={p.key}
                className="flex items-start gap-2.5 px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 text-sm cursor-pointer hover:bg-slate-50 dark:hover:bg-slate-900"
              >
                <input
                  type="checkbox"
                  checked={selectedPerms.has(p.key)}
                  onChange={() => togglePerm(p.key)}
                  className="mt-0.5 rounded border-slate-300 dark:border-slate-600"
                />
                <span>
                  <span className="block font-bold text-navy dark:text-white text-xs">{p.label}</span>
                  <span className="block text-[11px] text-slate-400">{p.description}</span>
                </span>
              </label>
            ))}
          </div>
        )}

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

function TenancyModal({ user, onClose, onSaved }: { user: AdminUser; onClose: () => void; onSaved: () => void }) {
  const [tenants, setTenants] = useState<TenantOption[]>([]);
  const [tenantsError, setTenantsError] = useState<string | null>(null);
  const [isMspStaff, setIsMspStaff] = useState(user.is_msp_staff);
  const [tenantId, setTenantId] = useState<string>(user.tenant_id || "");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .get<{ tenants: TenantOption[] }>("/users/tenants")
      .then((res) => setTenants(res.data.tenants))
      .catch(() => setTenantsError("Failed to load tenants."));
  }, []);

  const submit = async () => {
    if (!isMspStaff && !tenantId) {
      setError("Select a tenant, or mark this user as MSP staff.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await api.patch(`/users/${user.id}/tenancy`, {
        is_msp_staff: isMspStaff,
        tenant_id: isMspStaff ? null : tenantId,
      });
      onSaved();
      onClose();
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to update tenancy");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4" onClick={onClose}>
      <div
        className="bg-white dark:bg-slate-800 rounded-xl w-full max-w-md p-6"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-lg font-bold text-navy dark:text-white mb-1">Tenancy</h2>
        <p className="text-xs text-slate-500 dark:text-slate-400 mb-4">
          Assign {user.full_name} to a single tenant, or make them MSP staff with cross-tenant access
          (the Tenant Board and any other cross-tenant views).
        </p>
        {error && <div className="mb-3 p-2.5 rounded-lg bg-red-50 text-red-700 text-xs">{error}</div>}
        {tenantsError && <div className="mb-3 p-2.5 rounded-lg bg-amber-50 text-amber-700 text-xs">{tenantsError}</div>}

        <label className="flex items-center gap-2.5 px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 text-sm cursor-pointer hover:bg-slate-50 dark:hover:bg-slate-900 mb-4">
          <input
            type="checkbox"
            checked={isMspStaff}
            onChange={(e) => setIsMspStaff(e.target.checked)}
            className="rounded border-slate-300 dark:border-slate-600"
          />
          <div>
            <div className="font-bold text-navy dark:text-white">MSP Staff</div>
            <div className="text-xs text-slate-500 dark:text-slate-400">Works across every tenant, not scoped to one</div>
          </div>
        </label>

        {!isMspStaff && (
          <div className="mb-2">
            <div className="text-[11px] font-bold uppercase tracking-wide text-slate-400 mb-2">Tenant</div>
            <select
              value={tenantId}
              onChange={(e) => setTenantId(e.target.value)}
              className="w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-700 dark:bg-slate-900 text-sm"
            >
              <option value="">Select a tenant…</option>
              {tenants.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </select>
          </div>
        )}

        <div className="flex justify-end gap-2 mt-5">
          <button onClick={onClose} className="px-4 py-2 rounded-lg text-sm font-bold text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-700">
            Cancel
          </button>
          <button
            onClick={submit}
            disabled={submitting}
            className="px-4 py-2 rounded-lg text-sm font-bold bg-brandblue text-white hover:bg-brandblue/90 disabled:opacity-50"
          >
            {submitting ? "Saving…" : "Save Tenancy"}
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