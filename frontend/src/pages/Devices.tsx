import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { Device } from "../lib/types";

const statusColor: Record<string, string> = {
  online: "bg-risklow",
  offline: "bg-slate-400",
  degraded: "bg-riskmed",
  unknown: "bg-slate-300",
};

export default function Devices() {
  const [devices, setDevices] = useState<Device[]>([]);
  const [form, setForm] = useState({ hostname: "", ip_address: "", vendor: "cisco", site: "" });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = () => {
    api.get<Device[]>("/devices").then((res) => setDevices(res.data)).catch(() => setError("Failed to load devices."));
  };

  useEffect(load, []);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      await api.post("/devices", form);
      setForm({ hostname: "", ip_address: "", vendor: "cisco", site: "" });
      load();
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to create device.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div>
      <h1 className="text-2xl font-bold text-navy">Device Inventory</h1>
      <p className="text-sm text-slate-500 mt-1">Centralized inventory of managed network devices.</p>

      <form onSubmit={submit} className="mt-6 bg-white border border-slate-200 rounded-xl p-5 grid grid-cols-1 md:grid-cols-5 gap-3">
        <input
          className="border border-slate-300 rounded-lg px-3 py-2 text-sm"
          placeholder="Hostname (e.g. RTR-01)"
          value={form.hostname}
          onChange={(e) => setForm({ ...form, hostname: e.target.value })}
          required
        />
        <input
          className="border border-slate-300 rounded-lg px-3 py-2 text-sm"
          placeholder="IP Address"
          value={form.ip_address}
          onChange={(e) => setForm({ ...form, ip_address: e.target.value })}
          required
        />
        <select
          className="border border-slate-300 rounded-lg px-3 py-2 text-sm"
          value={form.vendor}
          onChange={(e) => setForm({ ...form, vendor: e.target.value })}
        >
          <option value="cisco">Cisco</option>
          <option value="juniper">Juniper</option>
          <option value="arista">Arista</option>
          <option value="linux">Linux</option>
        </select>
        <input
          className="border border-slate-300 rounded-lg px-3 py-2 text-sm"
          placeholder="Site (optional)"
          value={form.site}
          onChange={(e) => setForm({ ...form, site: e.target.value })}
        />
        <button
          type="submit"
          disabled={loading}
          className="bg-brandblue text-white rounded-lg px-4 py-2 text-sm font-semibold hover:bg-navy transition-colors disabled:opacity-50"
        >
          {loading ? "Adding…" : "Add Device"}
        </button>
      </form>

      {error && <p className="text-riskcrit text-sm mt-2">{error}</p>}

      <div className="mt-6 bg-white border border-slate-200 rounded-xl overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-navy text-white">
            <tr>
              <th className="text-left px-4 py-3 font-semibold">Hostname</th>
              <th className="text-left px-4 py-3 font-semibold">IP Address</th>
              <th className="text-left px-4 py-3 font-semibold">Vendor</th>
              <th className="text-left px-4 py-3 font-semibold">Site</th>
              <th className="text-left px-4 py-3 font-semibold">Status</th>
            </tr>
          </thead>
          <tbody>
            {devices.length === 0 && (
              <tr>
                <td colSpan={5} className="text-center text-slate-400 py-8">
                  No devices yet. Add one above.
                </td>
              </tr>
            )}
            {devices.map((d, i) => (
              <tr key={d.id} className={i % 2 ? "bg-slate-50" : "bg-white"}>
                <td className="px-4 py-3 font-medium text-navy">{d.hostname}</td>
                <td className="px-4 py-3 text-slate-600">{d.ip_address}</td>
                <td className="px-4 py-3 text-slate-600 capitalize">{d.vendor}</td>
                <td className="px-4 py-3 text-slate-600">{d.site || "—"}</td>
                <td className="px-4 py-3">
                  <span className="inline-flex items-center gap-1.5">
                    <span className={`w-2 h-2 rounded-full ${statusColor[d.status]}`} />
                    <span className="capitalize text-slate-600">{d.status}</span>
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
