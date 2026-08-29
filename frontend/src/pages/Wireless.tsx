/**
 * Wireless / AP Monitoring page
 *
 * Two sources of APs live here side by side:
 *  - "polled" APs, discovered by SNMP-walking a Cisco AireOS WLC (or
 *    compatible controller) via app.services.wireless_service. Read-only
 *    from this page -- edit them on the WLC itself.
 *  - "manual" APs, added directly through this page's CRUD form. Use
 *    this for standalone gear with no controller: TP-Link, Ruckus,
 *    Ubiquiti, MikroTik, Aruba IAP, or anything else. These support
 *    full add/edit/delete plus an on-demand reachability check (ping).
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";

interface WirelessController {
  device_id: string;
  hostname: string | null;
  ip_address: string | null;
}

type ApVendor = "cisco" | "aruba" | "ruckus" | "tplink" | "ubiquiti" | "mikrotik" | "other";

interface WirelessAP {
  id: string;
  controller_device_id: string | null;
  ap_index: string | null;
  ap_name: string | null;
  ap_model: string | null;
  ap_ip_address: string | null;
  vendor: ApVendor;
  mac_address: string | null;
  management_ip: string | null;
  site: string | null;
  notes: string | null;
  source: "polled" | "manual";
  oper_status: number | null;
  oper_status_label: "associated" | "disassociating" | "downloading" | "managed" | "unknown";
  client_count: number | null;
  band_2g_clients: number | null;
  band_5g_clients: number | null;
  created_at: string;
  polled_at: string;
}

interface WirelessSSID {
  id: string;
  controller_device_id: string;
  ssid_index: string;
  ssid_name: string;
  admin_status: number | null;
  enabled: boolean;
  mobile_station_count: number | null;
  polled_at: string;
}

interface WirelessSummary {
  controller_device_id: string;
  controller_hostname: string | null;
  total_aps: number;
  aps_up: number;
  aps_down: number;
  total_clients: number;
  band_2g_clients: number;
  band_5g_clients: number;
  ssid_count: number;
  polled_at: string | null;
}

const AP_STATUS: Record<string, { label: string; color: string; dot: string; bg: string }> = {
  associated:     { label: "Up",          color: "text-risklow",  dot: "bg-risklow",  bg: "bg-green-50 dark:bg-green-900/20" },
  managed:        { label: "Managed",     color: "text-brandblue", dot: "bg-brandblue", bg: "bg-blue-50 dark:bg-blue-900/20" },
  disassociating: { label: "Leaving",     color: "text-riskmed",  dot: "bg-riskmed",  bg: "bg-amber-50 dark:bg-amber-900/20" },
  downloading:    { label: "Booting",     color: "text-riskmed",  dot: "bg-amber-400", bg: "bg-amber-50 dark:bg-amber-900/20" },
  unknown:        { label: "Unknown",     color: "text-slate-400", dot: "bg-slate-300", bg: "bg-slate-50 dark:bg-slate-900/20" },
};

// oper_status is set to 0/1 by the manual "check" endpoint (see
// check_ap_reachability) -- this overrides the label for that case since
// "0" isn't one of the AireOS oper_status codes above.
function statusCfg(ap: WirelessAP) {
  if (ap.source === "manual" && ap.oper_status === 0) {
    return { label: "Down", color: "text-riskcrit", dot: "bg-riskcrit", bg: "bg-red-50 dark:bg-red-900/20" };
  }
  if (ap.source === "manual" && ap.oper_status === 1) {
    return AP_STATUS.associated;
  }
  return AP_STATUS[ap.oper_status_label] || AP_STATUS.unknown;
}

const VENDOR_LABELS: Record<ApVendor, string> = {
  cisco: "Cisco",
  aruba: "Aruba",
  ruckus: "Ruckus",
  tplink: "TP-Link",
  ubiquiti: "Ubiquiti",
  mikrotik: "MikroTik",
  other: "Other",
};

const VENDOR_ORDER: ApVendor[] = ["cisco", "aruba", "ruckus", "tplink", "ubiquiti", "mikrotik", "other"];

const emptyApForm = {
  ap_name: "",
  vendor: "tplink" as ApVendor,
  ap_model: "",
  ap_ip_address: "",
  management_ip: "",
  mac_address: "",
  site: "",
  notes: "",
};

function timeAgo(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime();
  const secs = Math.floor(diff / 1000);
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  return `${Math.floor(mins / 60)}h ago`;
}

export default function WirelessPage() {
  const [controllers, setControllers] = useState<WirelessController[]>([]);
  const [selectedId, setSelectedId] = useState<string>("");
  const [summary, setSummary] = useState<WirelessSummary | null>(null);
  const [allAps, setAllAps] = useState<WirelessAP[]>([]);
  const [ssids, setSsids] = useState<WirelessSSID[]>([]);
  const [loading, setLoading] = useState(false);
  const [polling, setPolling] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Manual AP CRUD state
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState(emptyApForm);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [checkingId, setCheckingId] = useState<string | null>(null);
  const [vendorFilter, setVendorFilter] = useState<"all" | ApVendor>("all");

  // Fetch available controllers on mount.
  useEffect(() => {
    api.get<WirelessController[]>("/wireless/controllers")
      .then((res) => {
        setControllers(res.data);
        if (res.data.length > 0) setSelectedId(res.data[0].device_id);
      })
      .catch(() => {});
  }, []);

  // All APs (manual + polled, across every controller) -- the manual
  // "Access Points" list and CRUD operate on this regardless of which
  // controller is selected in the polling dropdown above.
  const fetchAllAps = useCallback(() => {
    setLoading(true);
    setError(null);
    api.get<WirelessAP[]>("/wireless/aps")
      .then((res) => setAllAps(res.data))
      .catch(() => setError("Failed to load access points."))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    fetchAllAps();
  }, [fetchAllAps]);

  // Controller-scoped summary + SSIDs (polled data only).
  const fetchControllerData = useCallback((id: string) => {
    if (!id) return;
    Promise.all([
      api.get<WirelessSummary>(`/wireless/summary/${id}`),
      api.get<WirelessSSID[]>(`/wireless/ssids?controller_id=${id}`),
    ])
      .then(([sumRes, ssidRes]) => {
        setSummary(sumRes.data);
        setSsids(ssidRes.data);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (selectedId) fetchControllerData(selectedId);
  }, [selectedId, fetchControllerData]);

  const handlePoll = async () => {
    if (!selectedId || polling) return;
    setPolling(true);
    try {
      await api.post(`/wireless/poll/${selectedId}`);
      setTimeout(() => {
        fetchControllerData(selectedId);
        fetchAllAps();
        setPolling(false);
      }, 3500);
    } catch {
      setPolling(false);
    }
  };

  // --- Manual AP CRUD ----------------------------------------------------

  const startAdd = () => {
    setEditingId(null);
    setForm(emptyApForm);
    setFormError(null);
    setShowForm(true);
  };

  const startEdit = (ap: WirelessAP) => {
    setEditingId(ap.id);
    setForm({
      ap_name: ap.ap_name || "",
      vendor: ap.vendor,
      ap_model: ap.ap_model || "",
      ap_ip_address: ap.ap_ip_address || "",
      management_ip: ap.management_ip || "",
      mac_address: ap.mac_address || "",
      site: ap.site || "",
      notes: ap.notes || "",
    });
    setFormError(null);
    setShowForm(true);
  };

  const cancelForm = () => {
    setShowForm(false);
    setEditingId(null);
    setForm(emptyApForm);
    setFormError(null);
  };

  const submitForm = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!form.ap_name.trim()) {
      setFormError("Name is required.");
      return;
    }
    setSaving(true);
    setFormError(null);
    const payload = {
      ap_name: form.ap_name.trim(),
      vendor: form.vendor,
      ap_model: form.ap_model || null,
      ap_ip_address: form.ap_ip_address || null,
      management_ip: form.management_ip || null,
      mac_address: form.mac_address || null,
      site: form.site || null,
      notes: form.notes || null,
    };
    try {
      if (editingId) {
        const res = await api.patch<WirelessAP>(`/wireless/aps/${editingId}`, payload);
        setAllAps((prev) => prev.map((a) => (a.id === editingId ? res.data : a)));
      } else {
        const res = await api.post<WirelessAP>("/wireless/aps", payload);
        setAllAps((prev) => [...prev, res.data]);
      }
      cancelForm();
    } catch (err: any) {
      setFormError(err?.response?.data?.detail || `Failed to ${editingId ? "update" : "add"} access point.`);
    } finally {
      setSaving(false);
    }
  };

  const removeAp = async (ap: WirelessAP) => {
    if (!window.confirm(`Remove ${ap.ap_name || "this AP"}? This cannot be undone.`)) return;
    try {
      await api.delete(`/wireless/aps/${ap.id}`);
      setAllAps((prev) => prev.filter((a) => a.id !== ap.id));
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to remove access point.");
    }
  };

  const checkAp = async (ap: WirelessAP) => {
    setCheckingId(ap.id);
    try {
      const res = await api.post<WirelessAP>(`/wireless/aps/${ap.id}/check`);
      setAllAps((prev) => prev.map((a) => (a.id === ap.id ? res.data : a)));
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Reachability check failed.");
    } finally {
      setCheckingId(null);
    }
  };

  const visibleAps = useMemo(
    () => (vendorFilter === "all" ? allAps : allAps.filter((a) => a.vendor === vendorFilter)),
    [allAps, vendorFilter]
  );
  const vendorCounts = useMemo(() => {
    const counts: Partial<Record<ApVendor, number>> = {};
    allAps.forEach((a) => { counts[a.vendor] = (counts[a.vendor] || 0) + 1; });
    return counts;
  }, [allAps]);

  const noData = !loading && allAps.length === 0;

  return (
    <div>
      {/* Header */}
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold text-navy dark:text-white flex items-center gap-2">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} className="w-6 h-6 text-brandblue">
              <path d="M5 12.55a11 11 0 0114.08 0" />
              <path d="M1.42 9a16 16 0 0121.16 0" />
              <path d="M8.53 16.11a6 6 0 016.95 0" />
              <circle cx="12" cy="20" r="1" fill="currentColor" />
            </svg>
            Wireless / APs
          </h1>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
            SNMP-polled WLC APs plus manually-managed access points (TP-Link, Ruckus, Ubiquiti, MikroTik, Aruba, and more)
          </p>
        </div>

        <div className="flex items-center gap-3">
          {controllers.length > 0 && (
            <select
              value={selectedId}
              onChange={(e) => setSelectedId(e.target.value)}
              className="text-sm border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-1.5 bg-slate-50 dark:bg-slate-900 focus:ring-2 focus:ring-brandblue/30 focus:border-brandblue outline-none transition"
            >
              {controllers.map((c) => (
                <option key={c.device_id} value={c.device_id}>
                  {c.hostname || c.ip_address || c.device_id.slice(0, 8)}
                </option>
              ))}
            </select>
          )}
          {selectedId && (
            <button
              onClick={handlePoll}
              disabled={polling || loading}
              className="flex items-center gap-1.5 text-sm font-semibold text-white bg-slate-500 hover:bg-slate-600 px-3 py-1.5 rounded-lg shadow-sm disabled:opacity-50 transition-colors"
              title="Trigger an SNMP poll against the selected WLC"
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} className={`w-4 h-4 ${polling ? "animate-spin" : ""}`}>
                <path d="M3 12a9 9 0 109-9 9.75 9.75 0 00-6.74 2.74L3 8" />
                <path d="M3 3v5h5" />
              </svg>
              {polling ? "Polling…" : "Poll WLC"}
            </button>
          )}
          <button
            onClick={startAdd}
            className="flex items-center gap-1.5 text-sm font-semibold text-white bg-brandblue hover:bg-navy px-3 py-1.5 rounded-lg shadow-sm transition-colors"
          >
            + Add AP
          </button>
        </div>
      </div>

      {error && (
        <div className="mt-4 p-3 text-sm text-riskcrit bg-red-50 dark:bg-red-900/20 rounded-lg border border-red-200 dark:border-red-800 flex items-center justify-between">
          {error}
          <button onClick={() => setError(null)} className="font-bold text-riskcrit/70 hover:text-riskcrit">✕</button>
        </div>
      )}

      {/* Add / Edit AP form */}
      {showForm && (
        <form
          onSubmit={submitForm}
          className="mt-4 bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-5 shadow-sm"
        >
          <h3 className="text-sm font-bold text-navy dark:text-white mb-3">
            {editingId ? "Edit access point" : "Add access point"}
          </h3>
          {formError && (
            <p className="mb-3 text-xs font-semibold text-riskcrit bg-red-50 dark:bg-red-950/40 border border-red-200 dark:border-red-800 px-3 py-2 rounded-lg">
              {formError}
            </p>
          )}
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
            <input
              className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brandblue"
              placeholder="Name (e.g. AP-Lobby-01)"
              value={form.ap_name}
              onChange={(e) => setForm({ ...form, ap_name: e.target.value })}
              required
            />
            <select
              className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brandblue"
              value={form.vendor}
              onChange={(e) => setForm({ ...form, vendor: e.target.value as ApVendor })}
            >
              {VENDOR_ORDER.map((v) => (
                <option key={v} value={v}>{VENDOR_LABELS[v]}</option>
              ))}
            </select>
            <input
              className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brandblue"
              placeholder="Model (e.g. EAP670, R750)"
              value={form.ap_model}
              onChange={(e) => setForm({ ...form, ap_model: e.target.value })}
            />
            <input
              className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm font-mono focus:ring-2 focus:ring-brandblue"
              placeholder="Management IP"
              value={form.management_ip}
              onChange={(e) => setForm({ ...form, management_ip: e.target.value })}
            />
            <input
              className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm font-mono focus:ring-2 focus:ring-brandblue"
              placeholder="AP IP (optional, if different)"
              value={form.ap_ip_address}
              onChange={(e) => setForm({ ...form, ap_ip_address: e.target.value })}
            />
            <input
              className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm font-mono focus:ring-2 focus:ring-brandblue"
              placeholder="MAC address"
              value={form.mac_address}
              onChange={(e) => setForm({ ...form, mac_address: e.target.value })}
            />
            <input
              className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brandblue"
              placeholder="Site (optional)"
              value={form.site}
              onChange={(e) => setForm({ ...form, site: e.target.value })}
            />
            <input
              className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brandblue sm:col-span-2 lg:col-span-4"
              placeholder="Notes (optional)"
              value={form.notes}
              onChange={(e) => setForm({ ...form, notes: e.target.value })}
            />
          </div>
          <div className="flex items-center gap-2 mt-4">
            <button
              type="submit"
              disabled={saving}
              className="text-sm font-semibold text-white bg-brandblue hover:bg-navy px-4 py-2 rounded-lg shadow-sm disabled:opacity-50"
            >
              {saving ? "Saving…" : editingId ? "Save changes" : "Add access point"}
            </button>
            <button
              type="button"
              onClick={cancelForm}
              className="text-sm font-semibold text-slate-500 dark:text-slate-400 hover:text-navy dark:hover:text-white px-4 py-2 rounded-lg"
            >
              Cancel
            </button>
          </div>
        </form>
      )}

      {/* No controllers hint (informational only -- manual APs still work) */}
      {controllers.length === 0 && !loading && allAps.length === 0 && (
        <div className="mt-10 bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-12 text-center shadow-sm">
          <div className="text-5xl mb-4">📡</div>
          <h3 className="text-lg font-semibold text-navy dark:text-white">No access points yet</h3>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-2 max-w-md mx-auto">
            Click <b>+ Add AP</b> to manually add a standalone access point (TP-Link, Ruckus, Ubiquiti,
            MikroTik, etc.), or add a Cisco AireOS WLC as a device with SNMP enabled to auto-discover APs.
          </p>
        </div>
      )}

      {/* Summary bar (polled controller only) */}
      {summary && (
        <div className="mt-6 grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-6 gap-3">
          {[
            { label: "WLC APs",      value: summary.total_aps,    color: "text-navy dark:text-white" },
            { label: "APs Up",       value: summary.aps_up,       color: "text-risklow" },
            { label: "APs Down",     value: summary.aps_down,     color: summary.aps_down > 0 ? "text-riskcrit" : "text-slate-400" },
            { label: "Total Clients",value: summary.total_clients, color: "text-brandblue" },
            { label: "SSIDs",        value: summary.ssid_count,   color: "text-navy dark:text-white" },
            { label: "Last Poll",    value: summary.polled_at ? timeAgo(summary.polled_at) : "—", color: "text-slate-500 dark:text-slate-400" },
          ].map((s) => (
            <div key={s.label} className="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 px-4 py-3 shadow-sm">
              <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-400 dark:text-slate-500">{s.label}</p>
              <p className={`text-xl font-bold mt-0.5 ${s.color}`}>{s.value}</p>
            </div>
          ))}
        </div>
      )}

      {/* Vendor filter */}
      {allAps.length > 0 && (
        <div className="mt-6 flex flex-wrap gap-2 items-center">
          <button
            onClick={() => setVendorFilter("all")}
            className={`text-xs font-semibold px-3 py-1.5 rounded-full border transition-colors ${
              vendorFilter === "all"
                ? "bg-navy text-white border-navy"
                : "bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-300 border-slate-300 dark:border-slate-600"
            }`}
          >
            All ({allAps.length})
          </button>
          {VENDOR_ORDER.filter((v) => vendorCounts[v]).map((v) => (
            <button
              key={v}
              onClick={() => setVendorFilter(v)}
              className={`text-xs font-semibold px-3 py-1.5 rounded-full border transition-colors ${
                vendorFilter === v
                  ? "bg-navy text-white border-navy"
                  : "bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-300 border-slate-300 dark:border-slate-600"
              }`}
            >
              {VENDOR_LABELS[v]} ({vendorCounts[v]})
            </button>
          ))}
        </div>
      )}

      {/* Main content: AP grid + SSID table */}
      {(visibleAps.length > 0 || ssids.length > 0) && (
        <div className="mt-6 grid grid-cols-1 xl:grid-cols-3 gap-6">
          {/* AP grid — 2/3 width */}
          <div className="xl:col-span-2">
            <h2 className="text-xs font-semibold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-3">
              Access Points ({visibleAps.length})
            </h2>
            {loading ? (
              <div className="text-sm text-slate-400 py-8 text-center">Loading…</div>
            ) : (
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                {visibleAps.map((ap) => {
                  const cfg = statusCfg(ap);
                  return (
                    <div
                      key={ap.id}
                      className={`rounded-xl border border-slate-200 dark:border-slate-700 p-4 shadow-sm transition-colors ${cfg.bg}`}
                    >
                      <div className="flex items-start justify-between gap-2">
                        <div className="min-w-0">
                          <div className="flex items-center gap-1.5 flex-wrap">
                            <p className="text-sm font-semibold text-navy dark:text-white truncate">
                              {ap.ap_name || `AP #${ap.ap_index}`}
                            </p>
                            <span className="text-[9px] font-bold uppercase tracking-wide text-slate-500 dark:text-slate-400 bg-slate-100 dark:bg-slate-700 px-1.5 py-0.5 rounded-full">
                              {VENDOR_LABELS[ap.vendor] || ap.vendor}
                            </span>
                            {ap.source === "manual" && (
                              <span className="text-[9px] font-bold uppercase tracking-wide text-brandblue bg-blue-50 dark:bg-blue-900/30 px-1.5 py-0.5 rounded-full">
                                Manual
                              </span>
                            )}
                          </div>
                          {ap.ap_model && (
                            <p className="text-[11px] text-slate-400 dark:text-slate-500 truncate">{ap.ap_model}</p>
                          )}
                          {(ap.management_ip || ap.ap_ip_address) && (
                            <p className="text-[11px] font-mono text-slate-400 dark:text-slate-500">
                              {ap.management_ip || ap.ap_ip_address}
                            </p>
                          )}
                          {ap.site && (
                            <p className="text-[11px] text-slate-400 dark:text-slate-500">{ap.site}</p>
                          )}
                        </div>
                        <div className="flex flex-col items-end gap-1 shrink-0">
                          <span className={`flex items-center gap-1 text-[10px] font-bold uppercase tracking-wide ${cfg.color}`}>
                            <span className={`w-2 h-2 rounded-full ${cfg.dot}`} />
                            {cfg.label}
                          </span>
                          {ap.client_count != null && (
                            <span className="text-[11px] font-semibold text-brandblue bg-blue-50 dark:bg-blue-900/30 px-2 py-0.5 rounded-full">
                              {ap.client_count} client{ap.client_count !== 1 ? "s" : ""}
                            </span>
                          )}
                        </div>
                      </div>
                      {(ap.band_2g_clients != null || ap.band_5g_clients != null) && (
                        <div className="flex gap-3 mt-2 pt-2 border-t border-black/5 dark:border-white/5 text-[10px] text-slate-500 dark:text-slate-400">
                          {ap.band_2g_clients != null && <span>2.4 GHz: <b className="text-slate-600 dark:text-slate-300">{ap.band_2g_clients}</b></span>}
                          {ap.band_5g_clients != null && <span>5 GHz: <b className="text-slate-600 dark:text-slate-300">{ap.band_5g_clients}</b></span>}
                        </div>
                      )}
                      {ap.source === "manual" && (
                        <div className="flex items-center gap-2 mt-3 pt-3 border-t border-black/5 dark:border-white/5">
                          <button
                            onClick={() => checkAp(ap)}
                            disabled={checkingId === ap.id}
                            className="text-[11px] font-semibold text-brandblue hover:text-navy dark:hover:text-white disabled:opacity-50"
                          >
                            {checkingId === ap.id ? "Checking…" : "Check status"}
                          </button>
                          <span className="text-slate-300 dark:text-slate-600">·</span>
                          <button
                            onClick={() => startEdit(ap)}
                            className="text-[11px] font-semibold text-slate-500 dark:text-slate-400 hover:text-navy dark:hover:text-white"
                          >
                            Edit
                          </button>
                          <span className="text-slate-300 dark:text-slate-600">·</span>
                          <button
                            onClick={() => removeAp(ap)}
                            className="text-[11px] font-semibold text-riskcrit hover:text-red-700"
                          >
                            Remove
                          </button>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          {/* SSID table — 1/3 width (polled controller only) */}
          <div>
            <h2 className="text-xs font-semibold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-3">
              SSID Profiles ({ssids.length})
            </h2>
            {ssids.length === 0 ? (
              <div className="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 shadow-sm p-6 text-center text-xs text-slate-400 dark:text-slate-500">
                SSID profiles come from polling a WLC. Select a controller and click <b>Poll WLC</b> above.
              </div>
            ) : (
              <div className="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 shadow-sm overflow-x-auto">
                <table className="w-full text-left border-collapse" aria-label="SSID profiles">
                  <thead>
                    <tr className="text-[10px] font-bold uppercase tracking-wider text-slate-400 dark:text-slate-500 border-b border-slate-200 dark:border-slate-700">
                      <th scope="col" className="pl-4 py-2 pr-2">SSID</th>
                      <th scope="col" className="py-2 pr-2 text-center">Status</th>
                      <th scope="col" className="py-2 pr-4 text-right">Clients</th>
                    </tr>
                  </thead>
                  <tbody>
                    {ssids.map((ssid) => (
                      <tr
                        key={ssid.id}
                        className="border-b border-slate-100 dark:border-slate-700 last:border-0 hover:bg-slate-50 dark:hover:bg-slate-700/40 transition-colors"
                      >
                        <td className="pl-4 py-2.5 pr-2">
                          <span className="text-sm font-medium text-navy dark:text-white font-mono">{ssid.ssid_name}</span>
                        </td>
                        <td className="py-2.5 pr-2 text-center">
                          {ssid.enabled ? (
                            <span className="text-[10px] font-bold text-risklow bg-green-50 dark:bg-green-900/20 px-1.5 py-0.5 rounded">ON</span>
                          ) : (
                            <span className="text-[10px] font-bold text-slate-400 bg-slate-100 dark:bg-slate-700 px-1.5 py-0.5 rounded">OFF</span>
                          )}
                        </td>
                        <td className="py-2.5 pr-4 text-right">
                          <span className="text-sm font-semibold text-brandblue">
                            {ssid.mobile_station_count ?? "—"}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}

      {noData && controllers.length > 0 && (
        <div className="mt-10 bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-12 text-center shadow-sm">
          <div className="text-4xl mb-3">📡</div>
          <h3 className="text-base font-semibold text-navy dark:text-white">No access points</h3>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
            Click <b>+ Add AP</b> above, or poll a Cisco AireOS WLC to auto-discover APs.
          </p>
        </div>
      )}
    </div>
  );
}