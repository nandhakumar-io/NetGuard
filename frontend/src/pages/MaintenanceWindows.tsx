import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { Device, MaintenanceScope, MaintenanceWindow } from "../lib/types";
import { useAuth } from "../lib/auth";

const emptyForm = {
  name: "",
  reason: "",
  scope: "device" as MaintenanceScope,
  device_id: "",
  site: "",
  starts_at: "",
  ends_at: "",
};

const scopeStyle: Record<MaintenanceScope, string> = {
  device: "bg-blue-100 text-blue-700",
  site: "bg-purple-100 text-purple-700",
  fleet: "bg-amber-100 text-amber-700",
};

function toLocalInputValue(iso: string): string {
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export default function MaintenanceWindowsPage() {
  const { user } = useAuth();
  const canManage = user?.role === "network_admin" || user?.role === "network_engineer" || user?.role === "noc_engineer";

  const [windows, setWindows] = useState<MaintenanceWindow[]>([]);
  const [devices, setDevices] = useState<Device[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeOnly, setActiveOnly] = useState(false);

  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState(emptyForm);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const load = () => {
    setLoading(true);
    api
      .get<MaintenanceWindow[]>("/maintenance-windows", { params: activeOnly ? { active_only: true } : {} })
      .then((res) => {
        setWindows(res.data);
        setError(null);
      })
      .catch(() => setError("Failed to load maintenance windows."))
      .finally(() => setLoading(false));
  };

  useEffect(load, [activeOnly]);
  useEffect(() => {
    api.get<Device[]>("/devices").then((res) => setDevices(res.data)).catch(() => {});
  }, []);

  const openNew = () => {
    const now = new Date();
    const inHour = new Date(now.getTime() + 60 * 60 * 1000);
    setForm({ ...emptyForm, starts_at: toLocalInputValue(now.toISOString()), ends_at: toLocalInputValue(inHour.toISOString()) });
    setSaveError(null);
    setShowForm(true);
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setSaveError(null);
    try {
      await api.post("/maintenance-windows", {
        name: form.name,
        reason: form.reason || null,
        scope: form.scope,
        device_id: form.scope === "device" ? form.device_id || null : null,
        site: form.scope === "site" ? form.site || null : null,
        starts_at: new Date(form.starts_at).toISOString(),
        ends_at: new Date(form.ends_at).toISOString(),
      });
      setShowForm(false);
      load();
    } catch (err: any) {
      setSaveError(err?.response?.data?.detail ?? "Failed to create maintenance window.");
    } finally {
      setSaving(false);
    }
  };

  const cancelWindow = async (id: string) => {
    if (!confirm("Cancel this maintenance window? Alerts for its devices will resume paging immediately.")) return;
    try {
      await api.post(`/maintenance-windows/${id}/cancel`);
      load();
    } catch {
      setError("Failed to cancel window.");
    }
  };

  const deviceHostname = (id: string | null) => devices.find((d) => d.id === id)?.hostname ?? id ?? "—";

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-slate-900">Maintenance Windows</h1>
          <p className="text-sm text-slate-500 mt-1">
            Schedule planned work so alerts for the covered device, site, or fleet are suppressed instead of paging.
          </p>
        </div>
        {canManage && (
          <button onClick={openNew} className="px-4 py-2 bg-blue-600 text-white rounded-md text-sm font-medium hover:bg-blue-700">
            Schedule window
          </button>
        )}
      </div>

      <label className="flex items-center gap-2 text-sm text-slate-600">
        <input type="checkbox" checked={activeOnly} onChange={(e) => setActiveOnly(e.target.checked)} />
        Show only currently-active windows
      </label>

      {error && <div className="p-3 bg-red-50 text-red-700 rounded-md text-sm">{error}</div>}

      <div className="bg-white rounded-lg border border-slate-200 overflow-x-auto">
        <table className="w-full text-sm min-w-[640px]">
          <thead className="bg-slate-50 text-slate-500 text-xs uppercase">
            <tr>
              <th className="text-left px-4 py-3">Name</th>
              <th className="text-left px-4 py-3">Scope</th>
              <th className="text-left px-4 py-3">Target</th>
              <th className="text-left px-4 py-3">Starts</th>
              <th className="text-left px-4 py-3">Ends</th>
              <th className="text-left px-4 py-3">Status</th>
              <th className="text-left px-4 py-3">Created by</th>
              <th className="px-4 py-3" />
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {loading && (
              <tr>
                <td colSpan={8} className="px-4 py-6 text-center text-slate-400">
                  Loading…
                </td>
              </tr>
            )}
            {!loading && windows.length === 0 && (
              <tr>
                <td colSpan={8} className="px-4 py-6 text-center text-slate-400">
                  No maintenance windows scheduled.
                </td>
              </tr>
            )}
            {windows.map((w) => (
              <tr key={w.id}>
                <td className="px-4 py-3 font-medium text-slate-800">
                  {w.name}
                  {w.reason && <div className="text-xs text-slate-400">{w.reason}</div>}
                </td>
                <td className="px-4 py-3">
                  <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${scopeStyle[w.scope]}`}>{w.scope}</span>
                </td>
                <td className="px-4 py-3 text-slate-600">
                  {w.scope === "device" ? deviceHostname(w.device_id) : w.scope === "site" ? w.site : "entire fleet"}
                </td>
                <td className="px-4 py-3 text-slate-600">{new Date(w.starts_at).toLocaleString()}</td>
                <td className="px-4 py-3 text-slate-600">{new Date(w.ends_at).toLocaleString()}</td>
                <td className="px-4 py-3">
                  {w.cancelled ? (
                    <span className="px-2 py-0.5 rounded-full text-xs bg-slate-100 text-slate-500">cancelled</span>
                  ) : w.is_active ? (
                    <span className="px-2 py-0.5 rounded-full text-xs bg-green-100 text-green-700">active — suppressing</span>
                  ) : new Date(w.starts_at) > new Date() ? (
                    <span className="px-2 py-0.5 rounded-full text-xs bg-blue-100 text-blue-700">upcoming</span>
                  ) : (
                    <span className="px-2 py-0.5 rounded-full text-xs bg-slate-100 text-slate-500">ended</span>
                  )}
                </td>
                <td className="px-4 py-3 text-slate-500">{w.created_by}</td>
                <td className="px-4 py-3 text-right">
                  {canManage && !w.cancelled && (
                    <button onClick={() => cancelWindow(w.id)} className="text-xs text-red-600 hover:underline">
                      Cancel
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {showForm && (
        <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50">
          <form onSubmit={submit} className="bg-white rounded-lg p-6 w-full max-w-md space-y-4">
            <h2 className="text-lg font-semibold text-slate-900">Schedule maintenance window</h2>

            <div>
              <label className="block text-xs font-medium text-slate-500 mb-1">Name</label>
              <input
                required
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                className="w-full border border-slate-300 rounded-md px-3 py-2 text-sm"
                placeholder="e.g. Core switch reload — sw1"
              />
            </div>

            <div>
              <label className="block text-xs font-medium text-slate-500 mb-1">Reason (optional)</label>
              <input
                value={form.reason}
                onChange={(e) => setForm({ ...form, reason: e.target.value })}
                className="w-full border border-slate-300 rounded-md px-3 py-2 text-sm"
              />
            </div>

            <div>
              <label className="block text-xs font-medium text-slate-500 mb-1">Scope</label>
              <select
                value={form.scope}
                onChange={(e) => setForm({ ...form, scope: e.target.value as MaintenanceScope })}
                className="w-full border border-slate-300 rounded-md px-3 py-2 text-sm"
              >
                <option value="device">Single device</option>
                <option value="site">Entire site</option>
                <option value="fleet">Entire fleet (use sparingly)</option>
              </select>
            </div>

            {form.scope === "device" && (
              <div>
                <label className="block text-xs font-medium text-slate-500 mb-1">Device</label>
                <select
                  required
                  value={form.device_id}
                  onChange={(e) => setForm({ ...form, device_id: e.target.value })}
                  className="w-full border border-slate-300 rounded-md px-3 py-2 text-sm"
                >
                  <option value="">Select a device…</option>
                  {devices.map((d) => (
                    <option key={d.id} value={d.id}>
                      {d.hostname} ({d.ip_address})
                    </option>
                  ))}
                </select>
              </div>
            )}

            {form.scope === "site" && (
              <div>
                <label className="block text-xs font-medium text-slate-500 mb-1">Site</label>
                <input
                  required
                  value={form.site}
                  onChange={(e) => setForm({ ...form, site: e.target.value })}
                  className="w-full border border-slate-300 rounded-md px-3 py-2 text-sm"
                  placeholder="e.g. hq-dc1"
                />
              </div>
            )}

            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-xs font-medium text-slate-500 mb-1">Starts</label>
                <input
                  required
                  type="datetime-local"
                  value={form.starts_at}
                  onChange={(e) => setForm({ ...form, starts_at: e.target.value })}
                  className="w-full border border-slate-300 rounded-md px-3 py-2 text-sm"
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-slate-500 mb-1">Ends</label>
                <input
                  required
                  type="datetime-local"
                  value={form.ends_at}
                  onChange={(e) => setForm({ ...form, ends_at: e.target.value })}
                  className="w-full border border-slate-300 rounded-md px-3 py-2 text-sm"
                />
              </div>
            </div>

            {saveError && <div className="text-sm text-red-600">{saveError}</div>}

            <div className="flex justify-end gap-2 pt-2">
              <button type="button" onClick={() => setShowForm(false)} className="px-4 py-2 text-sm text-slate-600 hover:text-slate-900">
                Cancel
              </button>
              <button
                type="submit"
                disabled={saving}
                className="px-4 py-2 bg-blue-600 text-white rounded-md text-sm font-medium hover:bg-blue-700 disabled:opacity-50"
              >
                {saving ? "Scheduling…" : "Schedule"}
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}