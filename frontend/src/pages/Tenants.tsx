import { useState, useEffect } from "react";
import { useAuth } from "../lib/auth";
import { api } from "../lib/api";

interface Tenant {
  id: string;
  name: string;
  slug: string;
  is_active: boolean;
  device_count: number;
  user_count: number;
}

interface TenantCreate {
  name: string;
  slug: string;
}

interface TenantUpdate {
  name?: string;
  is_active?: boolean;
}

function slugify(name: string): string {
  return name
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9\s-]/g, "")
    .replace(/\s+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 64);
}

export default function Tenants() {
  const { user } = useAuth();
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // New tenant form
  const [showCreate, setShowCreate] = useState(false);
  const [createName, setCreateName] = useState("");
  const [createSlug, setCreateSlug] = useState("");
  const [createLoading, setCreateLoading] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  // Edit modal
  const [editTenant, setEditTenant] = useState<Tenant | null>(null);
  const [editName, setEditName] = useState("");
  const [editLoading, setEditLoading] = useState(false);

  // Delete confirm
  const [deletingId, setDeletingId] = useState<string | null>(null);

  async function fetchTenants() {
    try {
      setLoading(true);
      setError(null);
      const res = await api.get("/tenants");
      setTenants(res.data);
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      setError(err?.response?.data?.detail ?? "Failed to load tenants");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    fetchTenants();
  }, []);

  // Auto-fill slug from name
  useEffect(() => {
    setCreateSlug(slugify(createName));
  }, [createName]);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    setCreateLoading(true);
    setCreateError(null);
    try {
      await api.post("/tenants", { name: createName.trim(), slug: createSlug } as TenantCreate);
      setCreateName("");
      setCreateSlug("");
      setShowCreate(false);
      fetchTenants();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      setCreateError(err?.response?.data?.detail ?? "Failed to create tenant");
    } finally {
      setCreateLoading(false);
    }
  }

  async function handleUpdate(id: string, payload: TenantUpdate) {
    setEditLoading(true);
    try {
      await api.patch(`/tenants/${id}`, payload);
      setEditTenant(null);
      fetchTenants();
    } finally {
      setEditLoading(false);
    }
  }

  async function handleDelete(id: string) {
    try {
      await api.delete(`/tenants/${id}`);
      setDeletingId(null);
      fetchTenants();
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } };
      alert(err?.response?.data?.detail ?? "Failed to delete tenant");
    }
  }

  if (!user?.is_msp_staff) {
    return (
      <div className="flex flex-col items-center justify-center py-24 gap-4">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" className="text-slate-300 dark:text-slate-600">
          <path d="M12 2l8 4v6c0 5-3.4 8.4-8 10-4.6-1.6-8-5-8-10V6l8-4z" />
        </svg>
        <p className="text-slate-500 dark:text-slate-400">MSP staff access required</p>
      </div>
    );
  }

  return (
    <div className="max-w-5xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-display font-bold tracking-tight">Tenant Management</h1>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-0.5">
            Create and manage customer tenants for MSP operations
          </p>
        </div>
        <button
          onClick={() => setShowCreate(true)}
          className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-brandblue text-white text-sm font-medium hover:bg-brandblue/90 transition-colors"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
            <path d="M12 5v14M5 12h14" />
          </svg>
          New Tenant
        </button>
      </div>

      {/* Create form */}
      {showCreate && (
        <div className="bg-white dark:bg-noc-panel rounded-xl border border-slate-200 dark:border-noc-border p-5 shadow-sm">
          <h2 className="text-base font-semibold mb-4">Create Tenant</h2>
          <form onSubmit={handleCreate} className="space-y-4">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div>
                <label className="block text-xs font-medium text-slate-600 dark:text-slate-400 mb-1.5">
                  Tenant Name
                </label>
                <input
                  type="text"
                  value={createName}
                  onChange={(e) => setCreateName(e.target.value)}
                  placeholder="Acme Corp"
                  required
                  className="w-full px-3 py-2 text-sm rounded-lg border border-slate-200 dark:border-noc-border bg-slate-50 dark:bg-noc-bg focus:outline-none focus:ring-2 focus:ring-brandblue dark:focus:ring-noc-cyan"
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-slate-600 dark:text-slate-400 mb-1.5">
                  Slug <span className="text-slate-400 font-normal">(auto-generated)</span>
                </label>
                <input
                  type="text"
                  value={createSlug}
                  onChange={(e) => setCreateSlug(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ""))}
                  placeholder="acme-corp"
                  required
                  pattern="^[a-z0-9][a-z0-9\-]{0,62}[a-z0-9]$|^[a-z0-9]$"
                  className="w-full px-3 py-2 text-sm rounded-lg border border-slate-200 dark:border-noc-border bg-slate-50 dark:bg-noc-bg focus:outline-none focus:ring-2 focus:ring-brandblue dark:focus:ring-noc-cyan font-mono"
                />
              </div>
            </div>
            {createError && (
              <p className="text-sm text-red-500 bg-red-50 dark:bg-red-900/20 px-3 py-2 rounded-lg">{createError}</p>
            )}
            <div className="flex items-center gap-3 justify-end">
              <button
                type="button"
                onClick={() => { setShowCreate(false); setCreateName(""); setCreateError(null); }}
                className="px-4 py-2 text-sm rounded-lg text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-700 transition-colors"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={createLoading}
                className="px-4 py-2 text-sm rounded-lg bg-brandblue text-white font-medium hover:bg-brandblue/90 disabled:opacity-60 transition-colors"
              >
                {createLoading ? "Creating…" : "Create Tenant"}
              </button>
            </div>
          </form>
        </div>
      )}

      {/* Tenants table */}
      <div className="bg-white dark:bg-noc-panel rounded-xl border border-slate-200 dark:border-noc-border shadow-sm overflow-hidden">
        {loading ? (
          <div className="flex items-center justify-center py-16">
            <div className="w-6 h-6 border-2 border-slate-200 dark:border-slate-700 border-t-brandblue rounded-full animate-spin" />
          </div>
        ) : error ? (
          <div className="py-12 text-center text-red-500 text-sm">{error}</div>
        ) : tenants.length === 0 ? (
          <div className="py-16 text-center text-slate-400 dark:text-noc-muted text-sm">
            No tenants yet. Click "New Tenant" to get started.
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-100 dark:border-noc-border text-xs font-semibold uppercase tracking-wider text-slate-400 dark:text-noc-muted">
                <th className="text-left px-5 py-3">Name</th>
                <th className="text-left px-5 py-3 hidden sm:table-cell">Slug</th>
                <th className="text-right px-5 py-3 hidden md:table-cell">Devices</th>
                <th className="text-right px-5 py-3 hidden md:table-cell">Users</th>
                <th className="text-left px-5 py-3">Status</th>
                <th className="px-5 py-3" />
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 dark:divide-noc-border">
              {tenants.map((t) => (
                <tr key={t.id} className="hover:bg-slate-50 dark:hover:bg-noc-panel2 transition-colors group">
                  <td className="px-5 py-3.5 font-medium">{t.name}</td>
                  <td className="px-5 py-3.5 hidden sm:table-cell font-mono text-xs text-slate-500 dark:text-noc-muted">
                    {t.slug}
                  </td>
                  <td className="px-5 py-3.5 hidden md:table-cell text-right text-slate-600 dark:text-noc-text">
                    {t.device_count}
                  </td>
                  <td className="px-5 py-3.5 hidden md:table-cell text-right text-slate-600 dark:text-noc-text">
                    {t.user_count}
                  </td>
                  <td className="px-5 py-3.5">
                    <span className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium ${
                      t.is_active
                        ? "bg-emerald-50 text-emerald-700 dark:bg-emerald-900/25 dark:text-emerald-400"
                        : "bg-slate-100 text-slate-500 dark:bg-slate-700 dark:text-slate-400"
                    }`}>
                      <span className={`w-1.5 h-1.5 rounded-full ${t.is_active ? "bg-emerald-500" : "bg-slate-400"}`} />
                      {t.is_active ? "Active" : "Inactive"}
                    </span>
                  </td>
                  <td className="px-5 py-3.5">
                    <div className="flex items-center justify-end gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                      <button
                        onClick={() => { setEditTenant(t); setEditName(t.name); }}
                        title="Rename"
                        className="w-8 h-8 flex items-center justify-center rounded-lg text-slate-400 hover:bg-slate-100 dark:hover:bg-noc-panel2 hover:text-navy dark:hover:text-noc-text transition-colors"
                      >
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                          <path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7" />
                          <path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z" />
                        </svg>
                      </button>
                      <button
                        onClick={() => handleUpdate(t.id, { is_active: !t.is_active })}
                        title={t.is_active ? "Deactivate" : "Reactivate"}
                        className="w-8 h-8 flex items-center justify-center rounded-lg text-slate-400 hover:bg-slate-100 dark:hover:bg-noc-panel2 hover:text-amber-600 dark:hover:text-amber-400 transition-colors"
                      >
                        {t.is_active ? (
                          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                            <path d="M10 17l-5-5 5-5M20 17l-5-5 5-5" />
                          </svg>
                        ) : (
                          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                            <path d="M5 12h14M12 5l7 7-7 7" />
                          </svg>
                        )}
                      </button>
                      <button
                        onClick={() => setDeletingId(t.id)}
                        title="Delete"
                        className="w-8 h-8 flex items-center justify-center rounded-lg text-slate-400 hover:bg-red-50 dark:hover:bg-red-900/20 hover:text-red-600 dark:hover:text-red-400 transition-colors"
                      >
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                          <path d="M3 6h18M19 6l-1 14H6L5 6M10 11v6M14 11v6M9 6V4h6v2" />
                        </svg>
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Edit modal */}
      {editTenant && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
          <div className="absolute inset-0 bg-black/50" onClick={() => setEditTenant(null)} />
          <div className="relative w-full max-w-sm bg-white dark:bg-noc-panel rounded-xl shadow-2xl p-6">
            <h2 className="text-base font-semibold mb-4">Rename Tenant</h2>
            <input
              type="text"
              value={editName}
              onChange={(e) => setEditName(e.target.value)}
              className="w-full px-3 py-2 text-sm rounded-lg border border-slate-200 dark:border-noc-border bg-slate-50 dark:bg-noc-bg focus:outline-none focus:ring-2 focus:ring-brandblue dark:focus:ring-noc-cyan mb-4"
              autoFocus
            />
            <div className="flex gap-3 justify-end">
              <button
                onClick={() => setEditTenant(null)}
                className="px-4 py-2 text-sm rounded-lg text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-700 transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={() => handleUpdate(editTenant.id, { name: editName.trim() })}
                disabled={editLoading || !editName.trim()}
                className="px-4 py-2 text-sm rounded-lg bg-brandblue text-white font-medium hover:bg-brandblue/90 disabled:opacity-60 transition-colors"
              >
                {editLoading ? "Saving…" : "Save"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Delete confirm */}
      {deletingId && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
          <div className="absolute inset-0 bg-black/50" onClick={() => setDeletingId(null)} />
          <div className="relative w-full max-w-sm bg-white dark:bg-noc-panel rounded-xl shadow-2xl p-6">
            <h2 className="text-base font-semibold mb-2">Delete Tenant?</h2>
            <p className="text-sm text-slate-500 dark:text-slate-400 mb-5">
              This is permanent and will fail if the tenant still has devices or users. Consider deactivating instead.
            </p>
            <div className="flex gap-3 justify-end">
              <button
                onClick={() => setDeletingId(null)}
                className="px-4 py-2 text-sm rounded-lg text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-700 transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={() => handleDelete(deletingId)}
                className="px-4 py-2 text-sm rounded-lg bg-red-600 text-white font-medium hover:bg-red-700 transition-colors"
              >
                Delete
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
