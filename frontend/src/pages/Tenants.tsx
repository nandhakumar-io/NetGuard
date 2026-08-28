import { useEffect, useState } from "react";
import { Edit2, Plus, Trash2, ShieldAlert, CheckCircle2, XCircle } from "lucide-react";
import { api } from "../lib/api";

interface Tenant {
  id: string;
  name: string;
  slug: string;
  is_active: boolean;
  device_count: number;
  user_count: number;
}

export default function Tenants() {
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [editingTenant, setEditingTenant] = useState<Tenant | null>(null);
  const [formData, setFormData] = useState({ name: "", slug: "", is_active: true });
  const [saving, setSaving] = useState(false);

  const fetchTenants = async () => {
    try {
      setLoading(true);
      setError(null);
      const res = await api.get<Tenant[]>("/tenants");
      setTenants(res.data);
    } catch (err: any) {
      if (err.response?.status === 403) {
        setError("You do not have permission to view this page. Network Admin or MSP staff access required.");
      } else {
        setError("Failed to load tenants.");
      }
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchTenants();
  }, []);

  const handleOpenModal = (tenant?: Tenant) => {
    if (tenant) {
      setEditingTenant(tenant);
      setFormData({ name: tenant.name, slug: tenant.slug, is_active: tenant.is_active });
    } else {
      setEditingTenant(null);
      setFormData({ name: "", slug: "", is_active: true });
    }
    setIsModalOpen(true);
  };

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    try {
      if (editingTenant) {
        await api.patch(`/tenants/${editingTenant.id}`, {
          name: formData.name !== editingTenant.name ? formData.name : undefined,
          is_active: formData.is_active !== editingTenant.is_active ? formData.is_active : undefined,
        });
      } else {
        await api.post("/tenants", {
          name: formData.name,
          slug: formData.slug,
        });
      }
      await fetchTenants();
      setIsModalOpen(false);
    } catch (err: any) {
      alert(err.response?.data?.detail || "Failed to save tenant");
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (tenant: Tenant) => {
    if (!window.confirm(`Are you sure you want to permanently delete '${tenant.name}'?\n\nThis will only succeed if the tenant has no devices or users assigned.`)) {
      return;
    }
    try {
      await api.delete(`/tenants/${tenant.id}`);
      await fetchTenants();
    } catch (err: any) {
      alert(err.response?.data?.detail || "Failed to delete tenant");
    }
  };

  if (loading) {
    return (
      <div className="flex h-[50vh] items-center justify-center text-slate-500">
        <div className="animate-pulse flex flex-col items-center gap-2">
          <div className="h-4 w-24 bg-slate-200 dark:bg-slate-700 rounded"></div>
          <div className="text-sm">Loading tenants...</div>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-8 max-w-2xl mx-auto">
        <div className="bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-400 p-6 rounded-lg border border-red-200 dark:border-red-900/50 flex flex-col items-center text-center gap-4">
          <ShieldAlert className="w-12 h-12" />
          <div>
            <h2 className="text-lg font-semibold mb-1">Access Denied</h2>
            <p>{error}</p>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="p-6 max-w-7xl mx-auto">
      <div className="flex items-center justify-between mb-8">
        <div>
          <h1 className="text-2xl font-semibold text-slate-900 dark:text-white">Tenants</h1>
          <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
          Manage customer environments. Accessible to Network Admins and MSP staff.
          </p>
        </div>
        <button
          onClick={() => handleOpenModal()}
          className="inline-flex items-center gap-2 px-4 py-2 bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-medium rounded-md shadow-sm transition-colors"
        >
          <Plus className="w-4 h-4" />
          New Tenant
        </button>
      </div>

      <div className="bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-lg shadow-sm overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm text-left">
            <thead className="text-xs text-slate-500 uppercase bg-slate-50 dark:bg-slate-800 dark:text-slate-400 border-b border-slate-200 dark:border-slate-800">
              <tr>
                <th className="px-6 py-4 font-medium">Name</th>
                <th className="px-6 py-4 font-medium">Slug</th>
                <th className="px-6 py-4 font-medium">Status</th>
                <th className="px-6 py-4 font-medium text-right">Devices</th>
                <th className="px-6 py-4 font-medium text-right">Users</th>
                <th className="px-6 py-4 font-medium text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-200 dark:divide-slate-800">
              {tenants.map((t) => (
                <tr key={t.id} className="hover:bg-slate-50 dark:hover:bg-slate-800/50 transition-colors">
                  <td className="px-6 py-4 whitespace-nowrap font-medium text-slate-900 dark:text-slate-100">
                    {t.name}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap font-mono text-slate-500 dark:text-slate-400">
                    {t.slug}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap">
                    {t.is_active ? (
                      <span className="inline-flex items-center gap-1.5 px-2 py-1 rounded bg-green-50 dark:bg-green-500/10 text-green-700 dark:text-green-400 text-xs font-medium">
                        <CheckCircle2 className="w-3.5 h-3.5" />
                        Active
                      </span>
                    ) : (
                      <span className="inline-flex items-center gap-1.5 px-2 py-1 rounded bg-slate-100 dark:bg-slate-800 text-slate-700 dark:text-slate-400 text-xs font-medium border border-slate-200 dark:border-slate-700">
                        <XCircle className="w-3.5 h-3.5" />
                        Inactive
                      </span>
                    )}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-right text-slate-600 dark:text-slate-300">
                    {t.device_count}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-right text-slate-600 dark:text-slate-300">
                    {t.user_count}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-right">
                    <div className="flex items-center justify-end gap-3">
                      <button
                        onClick={() => handleOpenModal(t)}
                        title="Edit Tenant"
                        className="p-1.5 text-slate-400 hover:text-indigo-600 dark:hover:text-indigo-400 transition-colors rounded hover:bg-slate-100 dark:hover:bg-slate-800"
                      >
                        <Edit2 className="w-4 h-4" />
                      </button>
                      <button
                        onClick={() => handleDelete(t)}
                        title="Delete Tenant"
                        className="p-1.5 text-slate-400 hover:text-red-600 transition-colors rounded hover:bg-red-50 dark:hover:bg-red-900/30"
                      >
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
              {tenants.length === 0 && (
                <tr>
                  <td colSpan={6} className="px-6 py-12 text-center text-slate-500">
                    No tenants found.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {isModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-900/50 backdrop-blur-sm animate-in fade-in duration-200">
          <div className="bg-white dark:bg-slate-900 rounded-xl shadow-xl w-full max-w-md border border-slate-200 dark:border-slate-800 flex flex-col animate-in zoom-in-95 duration-200">
            <div className="p-6 border-b border-slate-100 dark:border-slate-800">
              <h2 className="text-xl font-semibold text-slate-900 dark:text-white">
                {editingTenant ? "Edit Tenant" : "New Tenant"}
              </h2>
            </div>
            
            <form onSubmit={handleSave} className="flex flex-col">
              <div className="p-6 flex flex-col gap-5">
                <div>
                  <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1.5">
                    Tenant Name
                  </label>
                  <input
                    type="text"
                    required
                    value={formData.name}
                    onChange={(e) => setFormData(p => ({...p, name: e.target.value}))}
                    className="w-full px-3 py-2 bg-white dark:bg-slate-950 border border-slate-300 dark:border-slate-800 rounded-md shadow-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 dark:text-white sm:text-sm"
                    placeholder="e.g. Acme Corp"
                  />
                </div>
                
                <div>
                  <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1.5">
                    Tenant Slug
                  </label>
                  <input
                    type="text"
                    required
                    disabled={!!editingTenant}
                    value={formData.slug}
                    onChange={(e) => setFormData(p => ({...p, slug: e.target.value}))}
                    className="w-full px-3 py-2 bg-white dark:bg-slate-950 border border-slate-300 dark:border-slate-800 rounded-md shadow-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 disabled:opacity-50 disabled:bg-slate-50 dark:disabled:bg-slate-900 dark:text-white sm:text-sm font-mono"
                    placeholder="e.g. acme"
                    pattern="^[a-z0-9][a-z0-9\-]{0,62}[a-z0-9]$|^[a-z0-9]$"
                  />
                  <p className="mt-1.5 text-xs text-slate-500">
                    Lowercase letters, numbers, and hyphens. Cannot be changed later.
                  </p>
                </div>

                {editingTenant && (
                  <label className="flex items-center gap-3 p-3 border border-slate-200 dark:border-slate-800 rounded-md cursor-pointer hover:bg-slate-50 dark:hover:bg-slate-800/50 transition-colors">
                    <input
                      type="checkbox"
                      checked={formData.is_active}
                      onChange={(e) => setFormData(p => ({...p, is_active: e.target.checked}))}
                      className="w-4 h-4 text-indigo-600 rounded border-slate-300 focus:ring-indigo-600"
                    />
                    <div className="flex flex-col">
                      <span className="text-sm font-medium text-slate-900 dark:text-white">Active</span>
                      <span className="text-xs text-slate-500">Deactivating a tenant will block logins for its users.</span>
                    </div>
                  </label>
                )}
              </div>

              <div className="p-6 border-t border-slate-100 dark:border-slate-800 bg-slate-50 dark:bg-slate-900 rounded-b-xl flex items-center justify-end gap-3">
                <button
                  type="button"
                  onClick={() => setIsModalOpen(false)}
                  className="px-4 py-2 text-sm font-medium text-slate-700 dark:text-slate-300 hover:bg-slate-200 dark:hover:bg-slate-800 rounded-md transition-colors"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={saving}
                  className="px-4 py-2 bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-medium rounded-md shadow-sm transition-colors disabled:opacity-50"
                >
                  {saving ? "Saving..." : "Save Tenant"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
