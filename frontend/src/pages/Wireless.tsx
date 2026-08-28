/**
 * Wireless / AP Monitoring page
 *
 * Displays a live snapshot of access points and SSID profiles managed by
 * one or more Cisco AireOS WLCs (or compatible SNMP controllers).
 *
 * Data is populated by app.services.wireless_service.poll_wireless_controller
 * (SNMP walk of AIRESPACE-WIRELESS-MIB bsnAPTable + bsnDot11EssTable).
 *
 * An on-demand "Refresh" button triggers POST /wireless/poll/{id}, then
 * re-fetches APs and SSIDs after a short delay so the user gets visual
 * feedback without page reload.
 */
import { useCallback, useEffect, useState } from "react";
import { api } from "../lib/api";

interface WirelessController {
  device_id: string;
  hostname: string | null;
  ip_address: string | null;
}

interface WirelessAP {
  id: string;
  controller_device_id: string;
  ap_index: string;
  ap_name: string | null;
  ap_model: string | null;
  ap_ip_address: string | null;
  oper_status: number | null;
  oper_status_label: "associated" | "disassociating" | "downloading" | "unknown";
  client_count: number | null;
  band_2g_clients: number | null;
  band_5g_clients: number | null;
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
  disassociating: { label: "Leaving",     color: "text-riskmed",  dot: "bg-riskmed",  bg: "bg-amber-50 dark:bg-amber-900/20" },
  downloading:    { label: "Booting",     color: "text-riskmed",  dot: "bg-amber-400", bg: "bg-amber-50 dark:bg-amber-900/20" },
  unknown:        { label: "Unknown",     color: "text-slate-400", dot: "bg-slate-300", bg: "bg-slate-50 dark:bg-slate-900/20" },
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
  const [aps, setAps] = useState<WirelessAP[]>([]);
  const [ssids, setSsids] = useState<WirelessSSID[]>([]);
  const [loading, setLoading] = useState(false);
  const [polling, setPolling] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Fetch available controllers on mount.
  useEffect(() => {
    api.get<WirelessController[]>("/wireless/controllers")
      .then((res) => {
        setControllers(res.data);
        if (res.data.length > 0) setSelectedId(res.data[0].device_id);
      })
      .catch(() => {});
  }, []);

  const fetchData = useCallback((id: string) => {
    if (!id) return;
    setLoading(true);
    setError(null);
    Promise.all([
      api.get<WirelessSummary>(`/wireless/summary/${id}`),
      api.get<WirelessAP[]>(`/wireless/aps?controller_id=${id}`),
      api.get<WirelessSSID[]>(`/wireless/ssids?controller_id=${id}`),
    ])
      .then(([sumRes, apRes, ssidRes]) => {
        setSummary(sumRes.data);
        setAps(apRes.data);
        setSsids(ssidRes.data);
      })
      .catch(() => setError("Failed to load wireless data."))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (selectedId) fetchData(selectedId);
  }, [selectedId, fetchData]);

  const handlePoll = async () => {
    if (!selectedId || polling) return;
    setPolling(true);
    try {
      await api.post(`/wireless/poll/${selectedId}`);
      // Give the background task a moment then re-fetch.
      setTimeout(() => {
        fetchData(selectedId);
        setPolling(false);
      }, 3500);
    } catch {
      setPolling(false);
    }
  };

  const noData = !loading && aps.length === 0 && ssids.length === 0;

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
            Live snapshot from Cisco AireOS WLC via SNMP · AIRESPACE-WIRELESS-MIB
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
              className="flex items-center gap-1.5 text-sm font-semibold text-white bg-brandblue hover:bg-navy px-3 py-1.5 rounded-lg shadow-sm disabled:opacity-50 transition-colors"
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} className={`w-4 h-4 ${polling ? "animate-spin" : ""}`}>
                <path d="M3 12a9 9 0 109-9 9.75 9.75 0 00-6.74 2.74L3 8" />
                <path d="M3 3v5h5" />
              </svg>
              {polling ? "Polling…" : "Refresh"}
            </button>
          )}
        </div>
      </div>

      {error && (
        <div className="mt-4 p-3 text-sm text-riskcrit bg-red-50 dark:bg-red-900/20 rounded-lg border border-red-200 dark:border-red-800">
          {error}
        </div>
      )}

      {/* No controllers empty state */}
      {controllers.length === 0 && !loading && (
        <div className="mt-10 bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-12 text-center shadow-sm">
          <div className="text-5xl mb-4">📡</div>
          <h3 className="text-lg font-semibold text-navy dark:text-white">No wireless data yet</h3>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-2 max-w-md mx-auto">
            This feature requires a Cisco AireOS WLC or compatible SNMP wireless controller added as a
            device with SNMP enabled. Run a poll from a supported WLC to populate AP and SSID data.
          </p>
        </div>
      )}

      {/* Summary bar */}
      {summary && (
        <div className="mt-6 grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-6 gap-3">
          {[
            { label: "Total APs",    value: summary.total_aps,    color: "text-navy dark:text-white" },
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

      {/* Main content: AP grid + SSID table */}
      {(aps.length > 0 || ssids.length > 0) && (
        <div className="mt-6 grid grid-cols-1 xl:grid-cols-3 gap-6">
          {/* AP grid — 2/3 width */}
          <div className="xl:col-span-2">
            <h2 className="text-xs font-semibold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-3">
              Access Points ({aps.length})
            </h2>
            {loading ? (
              <div className="text-sm text-slate-400 py-8 text-center">Loading…</div>
            ) : (
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                {aps.map((ap) => {
                  const cfg = AP_STATUS[ap.oper_status_label] || AP_STATUS.unknown;
                  return (
                    <div
                      key={ap.id}
                      className={`rounded-xl border border-slate-200 dark:border-slate-700 p-4 shadow-sm transition-colors ${cfg.bg}`}
                    >
                      <div className="flex items-start justify-between gap-2">
                        <div className="min-w-0">
                          <p className="text-sm font-semibold text-navy dark:text-white truncate">
                            {ap.ap_name || `AP #${ap.ap_index}`}
                          </p>
                          {ap.ap_model && (
                            <p className="text-[11px] text-slate-400 dark:text-slate-500 truncate">{ap.ap_model}</p>
                          )}
                          {ap.ap_ip_address && (
                            <p className="text-[11px] font-mono text-slate-400 dark:text-slate-500">{ap.ap_ip_address}</p>
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
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          {/* SSID table — 1/3 width */}
          <div>
            <h2 className="text-xs font-semibold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-3">
              SSID Profiles ({ssids.length})
            </h2>
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
          </div>
        </div>
      )}

      {noData && selectedId && (
        <div className="mt-10 bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-12 text-center shadow-sm">
          <div className="text-4xl mb-3">📡</div>
          <h3 className="text-base font-semibold text-navy dark:text-white">No AP data for this controller</h3>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
            Click <b>Refresh</b> to trigger an SNMP poll, or check that this device is a Cisco AireOS WLC
            with SNMP enabled and the community string configured.
          </p>
        </div>
      )}
    </div>
  );
}
