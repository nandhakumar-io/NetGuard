import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { Device, FirmwareUpgrade, FirmwareUpgradeStatus } from "../lib/types";
import { useAuth } from "../lib/auth";

const statusStyle: Record<FirmwareUpgradeStatus, string> = {
  pending: "bg-slate-100 text-slate-600",
  scheduled: "bg-blue-100 text-blue-700",
  downloading: "bg-indigo-100 text-indigo-700",
  installing: "bg-indigo-100 text-indigo-700",
  rebooting: "bg-amber-100 text-amber-700",
  verifying: "bg-amber-100 text-amber-700",
  completed: "bg-green-100 text-green-700",
  failed: "bg-red-100 text-red-700",
  rolled_back: "bg-orange-100 text-orange-700",
  cancelled: "bg-slate-100 text-slate-500",
};

const IN_FLIGHT: FirmwareUpgradeStatus[] = ["downloading", "installing", "rebooting", "verifying"];

const emptyForm = {
  device_ids: [] as string[],
  target_version: "",
  image_filename: "",
  reboot_wait_seconds: 90,
};

export default function FirmwareUpgradesPage() {
  const { user } = useAuth();
  const canManage = user?.role === "network_admin";

  const [jobs, setJobs] = useState<FirmwareUpgrade[]>([]);
  const [devices, setDevices] = useState<Device[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState(emptyForm);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const load = () => {
    api
      .get<FirmwareUpgrade[]>("/firmware-upgrades")
      .then((res) => {
        setJobs(res.data);
        setError(null);
      })
      .catch(() => setError("Failed to load firmware upgrade jobs."))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
    api.get<Device[]>("/devices").then((res) => setDevices(res.data)).catch(() => {});
  }, []);

  // Light polling while anything is in flight, so progress (downloading
  // -> installing -> rebooting -> verifying) updates without a manual
  // refresh -- mirrors how Deployments.tsx already polls.
  useEffect(() => {
    const hasInFlight = jobs.some((j) => IN_FLIGHT.includes(j.status) || j.status === "pending");
    if (!hasInFlight) return;
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [jobs]);

  const openNew = () => {
    setForm(emptyForm);
    setSaveError(null);
    setShowForm(true);
  };

  const toggleDevice = (id: string) => {
    setForm((f) => ({
      ...f,
      device_ids: f.device_ids.includes(id) ? f.device_ids.filter((d) => d !== id) : [...f.device_ids, id],
    }));
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (form.device_ids.length === 0) {
      setSaveError("Select at least one device.");
      return;
    }
    setSaving(true);
    setSaveError(null);
    try {
      if (form.device_ids.length === 1) {
        await api.post("/firmware-upgrades", {
          device_id: form.device_ids[0],
          target_version: form.target_version,
          image_filename: form.image_filename,
          reboot_wait_seconds: form.reboot_wait_seconds,
        });
      } else {
        await api.post("/firmware-upgrades/batch", {
          device_ids: form.device_ids,
          target_version: form.target_version,
          image_filename: form.image_filename,
          reboot_wait_seconds: form.reboot_wait_seconds,
        });
      }
      setShowForm(false);
      load();
    } catch (err: any) {
      setSaveError(err?.response?.data?.detail ?? "Failed to start firmware upgrade.");
    } finally {
      setSaving(false);
    }
  };

  const retry = async (id: string) => {
    try {
      await api.post(`/firmware-upgrades/${id}/retry`);
      load();
    } catch {
      setError("Failed to retry job.");
    }
  };

  const cancelJob = async (id: string) => {
    try {
      await api.post(`/firmware-upgrades/${id}/cancel`);
      load();
    } catch {
      setError("Failed to cancel job.");
    }
  };

  const deviceHostname = (id: string) => devices.find((d) => d.id === id)?.hostname ?? id;

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-slate-900">Firmware &amp; OS Upgrades</h1>
          <p className="text-sm text-slate-500 mt-1">
            Push a target image to one or many devices at once and track download → install → reboot → verify,
            with automatic rollback if a device doesn't come back healthy.
          </p>
        </div>
        {canManage && (
          <button onClick={openNew} className="px-4 py-2 bg-blue-600 text-white rounded-md text-sm font-medium hover:bg-blue-700">
            Start upgrade
          </button>
        )}
      </div>

      {error && <div className="p-3 bg-red-50 text-red-700 rounded-md text-sm">{error}</div>}

      <div className="bg-white rounded-lg border border-slate-200 overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-slate-500 text-xs uppercase">
            <tr>
              <th className="text-left px-4 py-3">Device</th>
              <th className="text-left px-4 py-3">From → To</th>
              <th className="text-left px-4 py-3">Image</th>
              <th className="text-left px-4 py-3">Status</th>
              <th className="text-left px-4 py-3">Detail</th>
              <th className="text-left px-4 py-3">Started</th>
              <th className="px-4 py-3" />
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {loading && (
              <tr>
                <td colSpan={7} className="px-4 py-6 text-center text-slate-400">
                  Loading…
                </td>
              </tr>
            )}
            {!loading && jobs.length === 0 && (
              <tr>
                <td colSpan={7} className="px-4 py-6 text-center text-slate-400">
                  No firmware upgrade jobs yet.
                </td>
              </tr>
            )}
            {jobs.map((j) => (
              <tr key={j.id}>
                <td className="px-4 py-3 font-medium text-slate-800">{deviceHostname(j.device_id)}</td>
                <td className="px-4 py-3 text-slate-600">
                  {j.from_version || "unknown"} → {j.target_version}
                </td>
                <td className="px-4 py-3 text-slate-500 font-mono text-xs">{j.image_filename}</td>
                <td className="px-4 py-3">
                  <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${statusStyle[j.status]}`}>
                    {j.status.replace("_", " ")}
                  </span>
                </td>
                <td className="px-4 py-3 text-slate-500 text-xs max-w-xs truncate" title={j.error_message ?? j.current_step_detail ?? ""}>
                  {j.error_message ?? j.current_step_detail ?? "—"}
                </td>
                <td className="px-4 py-3 text-slate-500">{j.started_at ? new Date(j.started_at).toLocaleString() : "—"}</td>
                <td className="px-4 py-3 text-right space-x-3">
                  {canManage && (j.status === "failed" || j.status === "rolled_back") && (
                    <button onClick={() => retry(j.id)} className="text-xs text-blue-600 hover:underline">
                      Retry
                    </button>
                  )}
                  {canManage && (j.status === "pending" || j.status === "scheduled") && (
                    <button onClick={() => cancelJob(j.id)} className="text-xs text-red-600 hover:underline">
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
          <form onSubmit={submit} className="bg-white rounded-lg p-6 w-full max-w-lg space-y-4">
            <h2 className="text-lg font-semibold text-slate-900">Start firmware/OS upgrade</h2>

            <div>
              <label className="block text-xs font-medium text-slate-500 mb-1">
                Devices ({form.device_ids.length} selected)
              </label>
              <div className="border border-slate-300 rounded-md max-h-40 overflow-y-auto divide-y divide-slate-100">
                {devices.map((d) => (
                  <label key={d.id} className="flex items-center gap-2 px-3 py-2 text-sm hover:bg-slate-50 cursor-pointer">
                    <input type="checkbox" checked={form.device_ids.includes(d.id)} onChange={() => toggleDevice(d.id)} />
                    <span className="font-medium text-slate-800">{d.hostname}</span>
                    <span className="text-slate-400 text-xs">{d.ip_address}</span>
                  </label>
                ))}
              </div>
              <p className="text-xs text-slate-400 mt-1">Selecting more than one starts a batch job (one row per device).</p>
            </div>

            <div>
              <label className="block text-xs font-medium text-slate-500 mb-1">Target version</label>
              <input
                required
                value={form.target_version}
                onChange={(e) => setForm({ ...form, target_version: e.target.value })}
                className="w-full border border-slate-300 rounded-md px-3 py-2 text-sm"
                placeholder="e.g. 17.9.4a"
              />
            </div>

            <div>
              <label className="block text-xs font-medium text-slate-500 mb-1">Image filename</label>
              <input
                required
                value={form.image_filename}
                onChange={(e) => setForm({ ...form, image_filename: e.target.value })}
                className="w-full border border-slate-300 rounded-md px-3 py-2 text-sm font-mono"
                placeholder="cat9k_iosxe.17.09.04a.SPA.bin"
              />
            </div>

            <div>
              <label className="block text-xs font-medium text-slate-500 mb-1">Expected reboot wait (seconds)</label>
              <input
                type="number"
                min={10}
                value={form.reboot_wait_seconds}
                onChange={(e) => setForm({ ...form, reboot_wait_seconds: Number(e.target.value) })}
                className="w-full border border-slate-300 rounded-md px-3 py-2 text-sm"
              />
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
                {saving ? "Starting…" : "Start upgrade"}
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}