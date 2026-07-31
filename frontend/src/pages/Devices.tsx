import { useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import { Device } from "../lib/types";
import { useAuth } from "../lib/auth";

const statusColor: Record<string, string> = {
  online: "bg-risklow",
  offline: "bg-slate-400",
  degraded: "bg-riskmed",
  unknown: "bg-slate-300",
};

const emptyForm = {
  hostname: "",
  ip_address: "",
  vendor: "cisco",
  site: "",
  ssh_username: "",
  ssh_credential_ref: "",
};

export default function Devices() {
  const { user } = useAuth();
  const canManage = user?.role === "network_admin";
  const [devices, setDevices] = useState<Device[]>([]);
  const [form, setForm] = useState(emptyForm);
  const [loading, setLoading] = useState(false);
  const [initialLoading, setInitialLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [vendorFilter, setVendorFilter] = useState<string>("all");
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);

  const load = () => {
    api
      .get<Device[]>("/devices")
      .then((res) => {
        setDevices(res.data);
        setError(null);
      })
      .catch(() => setError("Failed to load devices. Is the backend running?"))
      .finally(() => setInitialLoading(false));
  };

  useEffect(load, []);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      await api.post("/devices", form);
      setForm(emptyForm);
      setShowForm(false);
      load();
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to create device.");
    } finally {
      setLoading(false);
    }
  };

  const removeDevice = async (id: string, hostname: string) => {
    if (!window.confirm(`Remove ${hostname} from inventory? This cannot be undone.`)) return;
    setDeletingId(id);
    setError(null);
    try {
      await api.delete(`/devices/${id}`);
      setDevices((prev) => prev.filter((d) => d.id !== id));
    } catch (err: any) {
      setError(err?.response?.data?.detail || `Failed to remove ${hostname}.`);
    } finally {
      setDeletingId(null);
    }
  };

  const vendors = useMemo(() => Array.from(new Set(devices.map((d) => d.vendor))), [devices]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return devices.filter((d) => {
      if (vendorFilter !== "all" && d.vendor !== vendorFilter) return false;
      if (!q) return true;
      return (
        d.hostname.toLowerCase().includes(q) ||
        d.ip_address.toLowerCase().includes(q) ||
        (d.site || "").toLowerCase().includes(q)
      );
    });
  }, [devices, query, vendorFilter]);

  const counts = useMemo(() => {
    const c = { online: 0, offline: 0, degraded: 0, unknown: 0 };
    devices.forEach((d) => (c[d.status] = (c[d.status] ?? 0) + 1));
    return c;
  }, [devices]);

  return (
    <div>
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold text-navy">Device Inventory</h1>
          <p className="text-sm text-slate-500 mt-1">Centralized inventory of managed network devices.</p>
        </div>
        {canManage && (
          <button
            onClick={() => setShowForm((s) => !s)}
            className="bg-brandblue text-white rounded-lg px-4 py-2 text-sm font-semibold hover:bg-navy transition-colors"
          >
            {showForm ? "Cancel" : "+ Add Device"}
          </button>
        )}
      </div>

      <div className="flex flex-wrap gap-3 mt-5">
        <div className="bg-white border border-slate-200 rounded-lg px-4 py-2 text-xs text-slate-500">
          Total <span className="text-navy font-semibold ml-1">{devices.length}</span>
        </div>
        <div className="bg-white border border-slate-200 rounded-lg px-4 py-2 text-xs text-slate-500 flex items-center gap-1.5">
          <span className="w-2 h-2 rounded-full bg-risklow" /> Online
          <span className="text-navy font-semibold ml-1">{counts.online}</span>
        </div>
        <div className="bg-white border border-slate-200 rounded-lg px-4 py-2 text-xs text-slate-500 flex items-center gap-1.5">
          <span className="w-2 h-2 rounded-full bg-riskmed" /> Degraded
          <span className="text-navy font-semibold ml-1">{counts.degraded}</span>
        </div>
        <div className="bg-white border border-slate-200 rounded-lg px-4 py-2 text-xs text-slate-500 flex items-center gap-1.5">
          <span className="w-2 h-2 rounded-full bg-slate-400" /> Offline
          <span className="text-navy font-semibold ml-1">{counts.offline}</span>
        </div>
      </div>

      {canManage && showForm && (
        <form onSubmit={submit} className="mt-5 bg-white border border-slate-200 rounded-xl p-5">
          <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
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
          </div>

          <div className="grid grid-cols-1 md:grid-cols-4 gap-3 mt-3">
            <input
              className="border border-slate-300 rounded-lg px-3 py-2 text-sm"
              placeholder="SSH Username (e.g. admin)"
              value={form.ssh_username}
              onChange={(e) => setForm({ ...form, ssh_username: e.target.value })}
              required
            />
            <div className="md:col-span-2">
              <input
                className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm"
                placeholder="SSH Credential Ref (e.g. lab-switch-1)"
                value={form.ssh_credential_ref}
                onChange={(e) => setForm({ ...form, ssh_credential_ref: e.target.value })}
                required
              />
              <p className="text-[11px] text-slate-400 mt-1">
                Points to a secret, not the password itself. Resolved from env var{" "}
                <code className="bg-slate-100 px-1 rounded">
                  NETGUARD_CRED_{(form.ssh_credential_ref || "REF").replace(/[^A-Za-z0-9]/g, "_").toUpperCase()}
                </code>{" "}
                — never stored in the DB.
              </p>
            </div>
            <button
              type="submit"
              disabled={loading}
              className="bg-brandblue text-white rounded-lg px-4 py-2 text-sm font-semibold hover:bg-navy transition-colors disabled:opacity-50 h-fit self-start"
            >
              {loading ? "Adding…" : "Add Device"}
            </button>
          </div>
        </form>
      )}

      {!canManage && (
        <p className="mt-5 text-xs text-slate-400 italic bg-white border border-slate-200 rounded-xl p-4">
          Only a Network Administrator can add or remove devices. You have read-only access.
        </p>
      )}

      {error && <p className="text-riskcrit text-sm mt-3">{error}</p>}

      <div className="flex flex-wrap gap-2 items-center mt-5 mb-3">
        <input
          className="border border-slate-300 rounded-lg px-3 py-2 text-sm w-full max-w-xs"
          placeholder="Search hostname, IP, or site…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <select
          className="border border-slate-300 rounded-lg px-3 py-2 text-sm"
          value={vendorFilter}
          onChange={(e) => setVendorFilter(e.target.value)}
        >
          <option value="all">All vendors</option>
          {vendors.map((v) => (
            <option key={v} value={v} className="capitalize">
              {v}
            </option>
          ))}
        </select>
        <button
          onClick={load}
          className="text-xs text-brandblue font-medium hover:text-navy ml-auto"
          title="Refresh"
        >
          ↻ Refresh
        </button>
      </div>

      <div className="bg-white border border-slate-200 rounded-xl overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-navy text-white">
            <tr>
              <th className="text-left px-4 py-3 font-semibold">Hostname</th>
              <th className="text-left px-4 py-3 font-semibold">IP Address</th>
              <th className="text-left px-4 py-3 font-semibold">Vendor</th>
              <th className="text-left px-4 py-3 font-semibold">Site</th>
              <th className="text-left px-4 py-3 font-semibold">SSH Credential</th>
              <th className="text-left px-4 py-3 font-semibold">Status</th>
              {canManage && <th className="text-right px-4 py-3 font-semibold">Actions</th>}
            </tr>
          </thead>
          <tbody>
            {initialLoading && (
              <tr>
                <td colSpan={canManage ? 7 : 6} className="text-center text-slate-400 py-8">
                  Loading devices…
                </td>
              </tr>
            )}
            {!initialLoading && filtered.length === 0 && (
              <tr>
                <td colSpan={canManage ? 7 : 6} className="text-center text-slate-400 py-8">
                  {devices.length === 0 ? "No devices yet. Add one above." : "No devices match your search."}
                </td>
              </tr>
            )}
            {filtered.map((d, i) => (
              <tr key={d.id} className={i % 2 ? "bg-slate-50" : "bg-white"}>
                <td className="px-4 py-3 font-medium text-navy">{d.hostname}</td>
                <td className="px-4 py-3 text-slate-600 font-mono text-xs">{d.ip_address}</td>
                <td className="px-4 py-3 text-slate-600 capitalize">{d.vendor}</td>
                <td className="px-4 py-3 text-slate-600">{d.site || "—"}</td>
                <td className="px-4 py-3 text-slate-500 text-xs">
                  {d.ssh_username ? (
                    <span>
                      {d.ssh_username}
                      {d.ssh_credential_ref && (
                        <span className="text-slate-400"> · {d.ssh_credential_ref}</span>
                      )}
                    </span>
                  ) : (
                    <span className="text-riskcrit/80 italic">not configured</span>
                  )}
                </td>
                <td className="px-4 py-3">
                  <span className="inline-flex items-center gap-1.5">
                    <span className={`w-2 h-2 rounded-full ${statusColor[d.status]}`} />
                    <span className="capitalize text-slate-600">{d.status}</span>
                  </span>
                </td>
                {canManage && (
                  <td className="px-4 py-3 text-right">
                    <button
                      onClick={() => removeDevice(d.id, d.hostname)}
                      disabled={deletingId === d.id}
                      className="text-xs text-riskcrit hover:text-red-800 font-medium disabled:opacity-50"
                    >
                      {deletingId === d.id ? "Removing…" : "Remove"}
                    </button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}