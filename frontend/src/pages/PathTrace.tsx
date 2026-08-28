import { useCallback, useEffect, useState } from "react";
import { api } from "../lib/api";
import { Device, HopStatus, PathTrace } from "../lib/types";

const HOP_STATUS_CONFIG: Record<HopStatus, { label: string; color: string; bg: string; dot: string }> = {
  ok: { label: "OK", color: "text-risklow", bg: "bg-green-50", dot: "bg-risklow" },
  degraded: { label: "Degraded", color: "text-riskmed", bg: "bg-amber-50", dot: "bg-riskmed" },
  timeout: { label: "Timeout", color: "text-riskcrit", bg: "bg-red-50", dot: "bg-riskcrit" },
  unknown: { label: "Unknown", color: "text-slate-400", bg: "bg-slate-50", dot: "bg-slate-300" },
};

const STATUS_BADGE: Record<string, { label: string; color: string; bg: string }> = {
  complete: { label: "Reached Target", color: "text-risklow", bg: "bg-green-50" },
  partial: { label: "Partial", color: "text-riskmed", bg: "bg-amber-50" },
  failed: { label: "Failed", color: "text-riskcrit", bg: "bg-red-50" },
};

function formatBps(bytesPerSec: number): string {
  const bits = bytesPerSec * 8;
  if (bits >= 1e9) return `${(bits / 1e9).toFixed(2)} Gbps`;
  if (bits >= 1e6) return `${(bits / 1e6).toFixed(2)} Mbps`;
  if (bits >= 1e3) return `${(bits / 1e3).toFixed(1)} Kbps`;
  return `${bits.toFixed(0)} bps`;
}

function timeAgo(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime();
  const secs = Math.floor(diff / 1000);
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

export default function PathTracePage() {
  const [devices, setDevices] = useState<Device[]>([]);
  const [sourceId, setSourceId] = useState("");
  const [targetDeviceId, setTargetDeviceId] = useState("");
  const [targetInput, setTargetInput] = useState("");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [history, setHistory] = useState<PathTrace[]>([]);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [selected, setSelected] = useState<PathTrace | null>(null);

  useEffect(() => {
    api.get<Device[]>("/devices").then((res) => {
      setDevices(res.data);
      if (res.data.length > 0) setSourceId((prev) => prev || res.data[0].id);
    }).catch(() => {});
  }, []);

  const fetchHistory = useCallback(() => {
    setHistoryLoading(true);
    api.get<PathTrace[]>("/path-trace?limit=25").then((res) => {
      setHistory(res.data);
      setHistoryLoading(false);
      if (!selected && res.data.length > 0) setSelected(res.data[0]);
    }).catch(() => setHistoryLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    fetchHistory();
  }, [fetchHistory]);

  const runTrace = async () => {
    if (!sourceId) {
      setError("Pick a source device.");
      return;
    }
    if (!targetDeviceId && !targetInput.trim()) {
      setError("Pick a target device or type a hostname/IP.");
      return;
    }
    setError(null);
    setRunning(true);
    try {
      const body: Record<string, string> = { source_device_id: sourceId };
      if (targetDeviceId) body.target_device_id = targetDeviceId;
      if (targetInput.trim()) body.target_input = targetInput.trim();
      const res = await api.post<PathTrace>("/path-trace", body);
      setSelected(res.data);
      fetchHistory();
    } catch (e: any) {
      setError(e?.response?.data?.detail || "Trace failed to run.");
    } finally {
      setRunning(false);
    }
  };

  return (
    <div>
      <div>
        <h1 className="text-2xl font-bold text-navy dark:text-white">Path Trace</h1>
        <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
          Hop-by-hop route health between two points, similar to NetPath. Falls back to the topology graph when a
          real traceroute isn't available in this environment.
        </p>
      </div>

      {/* Run form */}
      <div className="mt-6 bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-5 shadow-sm">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div>
            <label className="text-xs font-semibold text-slate-500 dark:text-slate-400 uppercase tracking-wider">
              Source device
            </label>
            <select
              value={sourceId}
              onChange={(e) => setSourceId(e.target.value)}
              className="mt-1 w-full text-sm border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 bg-slate-50 dark:bg-slate-900 focus:ring-2 focus:ring-brandblue/30 focus:border-brandblue outline-none transition"
            >
              <option value="">Select a device…</option>
              {devices.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.hostname} ({d.ip_address})
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="text-xs font-semibold text-slate-500 dark:text-slate-400 uppercase tracking-wider">
              Target device (optional)
            </label>
            <select
              value={targetDeviceId}
              onChange={(e) => {
                setTargetDeviceId(e.target.value);
                if (e.target.value) setTargetInput("");
              }}
              className="mt-1 w-full text-sm border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 bg-slate-50 dark:bg-slate-900 focus:ring-2 focus:ring-brandblue/30 focus:border-brandblue outline-none transition"
            >
              <option value="">— none —</option>
              {devices.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.hostname} ({d.ip_address})
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="text-xs font-semibold text-slate-500 dark:text-slate-400 uppercase tracking-wider">
              Or type a hostname/IP
            </label>
            <input
              type="text"
              value={targetInput}
              onChange={(e) => {
                setTargetInput(e.target.value);
                if (e.target.value) setTargetDeviceId("");
              }}
              placeholder="e.g. 8.8.8.8 or example.com"
              disabled={!!targetDeviceId}
              className="mt-1 w-full text-sm border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 bg-slate-50 dark:bg-slate-900 focus:ring-2 focus:ring-brandblue/30 focus:border-brandblue outline-none transition disabled:opacity-50"
            />
          </div>
        </div>

        {error && <p className="text-sm text-riskcrit mt-3">{error}</p>}

        <div className="mt-4">
          <button
            onClick={runTrace}
            disabled={running}
            className="text-sm font-semibold text-white bg-brandblue hover:bg-navy px-4 py-2 rounded-lg shadow-sm disabled:opacity-50 transition-colors"
          >
            {running ? "Tracing…" : "Run Trace"}
          </button>
        </div>
      </div>

      <div className="mt-6 grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* History sidebar */}
        <div className="lg:col-span-1">
          <h2 className="text-xs font-semibold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-2">
            Recent Traces
          </h2>
          <div className="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 shadow-sm divide-y divide-slate-100 dark:divide-slate-700 max-h-[560px] overflow-y-auto">
            {historyLoading ? (
              <div className="p-6 text-center text-sm text-slate-400">Loading…</div>
            ) : history.length === 0 ? (
              <div className="p-6 text-center text-sm text-slate-400">No traces yet.</div>
            ) : (
              history.map((t) => {
                const badge = STATUS_BADGE[t.status] || STATUS_BADGE.partial;
                return (
                  <button
                    key={t.id}
                    onClick={() => setSelected(t)}
                    className={`w-full text-left px-4 py-3 hover:bg-slate-50 dark:hover:bg-slate-700 transition-colors ${
                      selected?.id === t.id ? "bg-blue-50 dark:bg-slate-700" : ""
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <p className="text-sm font-medium text-navy dark:text-white truncate">
                        {t.source_hostname || t.source_ip} → {t.target_hostname || t.target_input}
                      </p>
                    </div>
                    <div className="flex items-center gap-2 mt-1">
                      <span className={`text-[10px] font-bold uppercase tracking-wide px-1.5 py-0.5 rounded ${badge.color} ${badge.bg}`}>
                        {badge.label}
                      </span>
                      <span className="text-[11px] text-slate-400">{t.total_hops} hops</span>
                      <span className="text-[11px] text-slate-400">{timeAgo(t.created_at)}</span>
                    </div>
                  </button>
                );
              })
            )}
          </div>
        </div>

        {/* Selected trace detail */}
        <div className="lg:col-span-2">
          {!selected ? (
            <div className="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-12 text-center">
              <div className="text-5xl mb-4">🛰️</div>
              <h3 className="text-lg font-semibold text-navy dark:text-white">No trace selected</h3>
              <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
                Run a trace above, or pick one from the history.
              </p>
            </div>
          ) : (
            <div className="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 shadow-sm p-5">
              <div className="flex items-start justify-between gap-4 flex-wrap">
                <div>
                  <h3 className="text-sm font-semibold text-navy dark:text-white">
                    {selected.source_hostname || selected.source_ip} → {selected.target_hostname || selected.target_input}
                  </h3>
                  <p className="text-xs text-slate-400 dark:text-slate-500 mt-1">
                    {selected.hop_source === "mtr" ? "Live mtr trace" : selected.hop_source === "traceroute" ? "Live traceroute" : "Derived from topology graph"} ·{" "}
                    {selected.total_hops} hop{selected.total_hops === 1 ? "" : "s"} · {timeAgo(selected.created_at)}
                    {selected.requested_by ? ` · by ${selected.requested_by}` : ""}
                  </p>
                </div>
                <span
                  className={`text-[11px] font-bold uppercase tracking-wider px-2 py-1 rounded-full ${
                    (STATUS_BADGE[selected.status] || STATUS_BADGE.partial).color
                  } ${(STATUS_BADGE[selected.status] || STATUS_BADGE.partial).bg}`}
                >
                  {(STATUS_BADGE[selected.status] || STATUS_BADGE.partial).label}
                </span>
              </div>

              {/* Hop list */}
              <div className="mt-5 space-y-1">
                {selected.hops.length === 0 ? (
                  <p className="text-sm text-slate-400 text-center py-8">No hops recorded for this trace.</p>
                ) : (
                  selected.hops.map((hop, idx) => {
                    const cfg = HOP_STATUS_CONFIG[hop.status] || HOP_STATUS_CONFIG.unknown;
                    const isLast = idx === selected.hops.length - 1;
                    return (
                      <div key={hop.id} className="flex gap-3">
                        {/* Rail */}
                        <div className="flex flex-col items-center w-6 shrink-0">
                          <span className={`w-3 h-3 rounded-full ${cfg.dot} ring-4 ring-white dark:ring-slate-800`} />
                          {!isLast && <span className="flex-1 w-px bg-slate-200 dark:bg-slate-600 my-0.5" />}
                        </div>
                        <div className={`flex-1 rounded-lg px-3 py-2 mb-1 ${cfg.bg} dark:bg-slate-900/40`}>
                          <div className="flex items-center justify-between gap-3 flex-wrap">
                            <div className="min-w-0">
                              <span className="text-[11px] font-mono text-slate-400 mr-2">#{hop.hop_index}</span>
                              <span className="text-sm font-medium text-navy dark:text-white">
                                {hop.hostname || hop.ip_address || "* * *"}
                              </span>
                              {hop.hostname && hop.ip_address && (
                                <span className="text-xs text-slate-400 ml-2">{hop.ip_address}</span>
                              )}
                              {hop.device_id && (
                                <span className="ml-2 text-[10px] font-semibold text-brandblue bg-blue-50 px-1.5 py-0.5 rounded">
                                  managed device
                                </span>
                              )}
                            </div>
                            <div className="flex items-center gap-3 shrink-0 text-xs">
                              <span className={`font-semibold uppercase tracking-wide ${cfg.color}`}>{cfg.label}</span>
                              {hop.rtt_ms != null && <span className="text-slate-500 dark:text-slate-400">{hop.rtt_ms.toFixed(1)} ms</span>}
                              {hop.packet_loss_pct != null && hop.packet_loss_pct > 0 && (
                                <span className="text-riskcrit">{hop.packet_loss_pct.toFixed(0)}% loss</span>
                              )}
                              {hop.flow_bytes_per_sec != null && (
                                <span
                                  className="font-semibold text-brandblue bg-blue-50 dark:bg-blue-900/30 px-1.5 py-0.5 rounded"
                                  title={hop.flow_top_protocol ? `Top protocol: ${hop.flow_top_protocol}` : undefined}
                                >
                                  {formatBps(hop.flow_bytes_per_sec)}
                                </span>
                              )}
                            </div>
                          </div>
                          {/* Extra mtr stats (best/worst/stddev/sent) plus live
                              flow bandwidth -- mtr stats only present for real
                              mtr-sourced hops, flow stats only present when this
                              hop is a managed device actively exporting
                              NetFlow/sFlow (app.services.flow_service). */}
                          {(hop.best_rtt_ms != null || hop.worst_rtt_ms != null || hop.stddev_rtt_ms != null || hop.sent != null || hop.flow_bytes_per_sec != null) && (
                            <div className="flex items-center gap-3 flex-wrap mt-1.5 pt-1.5 border-t border-black/5 dark:border-white/5 text-[10px] text-slate-400 dark:text-slate-500">
                              {hop.last_rtt_ms != null && <span>Last <b className="font-semibold text-slate-500 dark:text-slate-400">{hop.last_rtt_ms.toFixed(1)}</b> ms</span>}
                              {hop.best_rtt_ms != null && <span>Best <b className="font-semibold text-slate-500 dark:text-slate-400">{hop.best_rtt_ms.toFixed(1)}</b> ms</span>}
                              {hop.worst_rtt_ms != null && <span>Worst <b className="font-semibold text-slate-500 dark:text-slate-400">{hop.worst_rtt_ms.toFixed(1)}</b> ms</span>}
                              {hop.stddev_rtt_ms != null && <span>StDev <b className="font-semibold text-slate-500 dark:text-slate-400">{hop.stddev_rtt_ms.toFixed(1)}</b> ms</span>}
                              {hop.sent != null && <span>Sent <b className="font-semibold text-slate-500 dark:text-slate-400">{hop.sent}</b></span>}
                              {hop.flow_top_protocol != null && <span>Top proto <b className="font-semibold text-slate-500 dark:text-slate-400">{hop.flow_top_protocol}</b></span>}
                            </div>
                          )}
                        </div>
                      </div>
                    );
                  })
                )}
              </div>

              {!selected.reached_target && selected.status !== "failed" && (
                <p className="text-xs text-riskmed mt-3">
                  Trace did not confirm reaching the target — the last responsive hop may not be adjacent to it.
                </p>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}