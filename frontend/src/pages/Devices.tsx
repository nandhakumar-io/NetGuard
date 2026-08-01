import { Fragment, useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import {
  Device,
  Snapshot,
  RunningConfig,
  StartupConfig,
  BackupHistoryEntry,
  CompareConfigResponse,
  DeviceHealthSummary,
  DeviceMetric,
  Alert as AlertRow,
} from "../lib/types";
import { useAuth } from "../lib/auth";
import ConfigDiff from "../components/ConfigDiff";

const HEALTH_COLOR_STYLES: Record<string, { dot: string; text: string; bg: string }> = {
  green: { dot: "bg-risklow", text: "text-risklow", bg: "bg-green-50 border-green-200" },
  yellow: { dot: "bg-riskmed", text: "text-riskmed", bg: "bg-amber-50 border-amber-200" },
  red: { dot: "bg-riskcrit", text: "text-riskcrit", bg: "bg-red-50 border-red-200" },
  unknown: { dot: "bg-slate-300", text: "text-slate-400", bg: "bg-slate-50 border-slate-200" },
};

const ALERT_SEVERITY_STYLES: Record<string, { text: string; bg: string; icon: string }> = {
  critical: { text: "text-riskcrit", bg: "bg-red-50 border-red-200", icon: "🚨" },
  warning: { text: "text-riskmed", bg: "bg-amber-50 border-amber-200", icon: "⚠️" },
  info: { text: "text-brandblue", bg: "bg-blue-50 border-blue-200", icon: "ℹ️" },
};

function formatUptime(seconds: number | null): string {
  if (seconds == null) return "—";
  const days = Math.floor(seconds / 86400);
  const hrs = Math.floor((seconds % 86400) / 3600);
  const mins = Math.floor((seconds % 3600) / 60);
  if (days > 0) return `${days}d ${hrs}h`;
  if (hrs > 0) return `${hrs}h ${mins}m`;
  return `${mins}m`;
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

const TAB_NAMES = [
  "Overview",
  "Health",
  "Configuration",
  "Interfaces",
  "Backups",
  "Drift",
  "Alerts",
  "Protocol Operations",
  "Deployment History",
] as const;
type TabName = typeof TAB_NAMES[number];

// --- Subcomponents for Device Details Tabs ---

function DeviceInlineDetails({
  device,
  canManage,
  onQueueRollback,
}: {
  device: Device;
  canManage: boolean;
  onQueueRollback: (snapshot: Snapshot, reason: string) => void;
}) {
  const [activeTab, setActiveTab] = useState<TabName>("Overview");

  // Configuration tab state
  const [running, setRunning] = useState<RunningConfig | null>(null);
  const [startup, setStartup] = useState<StartupConfig | null>(null);
  const [configLoading, setConfigLoading] = useState(false);
  const [configError, setConfigError] = useState<string | null>(null);

  // Backups tab state
  const [history, setHistory] = useState<BackupHistoryEntry[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);

  // Compare sub-state inside backups (simplified for inline view)
  const [baseId, setBaseId] = useState<string>("");
  const [targetId, setTargetId] = useState<string>("");
  const [compareResult, setCompareResult] = useState<CompareConfigResponse | null>(null);
  const [comparing, setComparing] = useState(false);

  // Health tab state (shared with Interfaces tab, which reads the same
  // metric history -- interface_utilization_pct/interface_errors are
  // columns on the same DeviceMetric row, there's no separate per-interface
  // table in this schema).
  const [health, setHealth] = useState<DeviceHealthSummary | null>(null);
  const [metricHistory, setMetricHistory] = useState<DeviceMetric[]>([]);
  const [healthLoading, setHealthLoading] = useState(false);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [polling, setPolling] = useState(false);

  // Alerts tab state
  const [deviceAlerts, setDeviceAlerts] = useState<AlertRow[]>([]);
  const [alertsLoading, setAlertsLoading] = useState(false);
  const [alertsError, setAlertsError] = useState<string | null>(null);
  const [ackingId, setAckingId] = useState<string | null>(null);

  const loadHealth = () => {
    setHealthLoading(true);
    setHealthError(null);
    Promise.all([
      api.get<DeviceHealthSummary>(`/devices/${device.id}/health`),
      api.get<DeviceMetric[]>(`/devices/${device.id}/metrics/history?hours=24`),
    ])
      .then(([healthRes, historyRes]) => {
        setHealth(healthRes.data);
        setMetricHistory(historyRes.data);
      })
      .catch(() => setHealthError("No SNMP telemetry available for this device yet."))
      .finally(() => setHealthLoading(false));
  };

  const pollNow = async () => {
    setPolling(true);
    try {
      await api.post(`/devices/${device.id}/metrics/poll`);
      loadHealth();
    } catch (err: any) {
      setHealthError(err?.response?.data?.detail || "On-demand poll failed.");
    } finally {
      setPolling(false);
    }
  };

  const loadAlerts = () => {
    setAlertsLoading(true);
    setAlertsError(null);
    api
      .get<AlertRow[]>(`/alerts?device_id=${device.id}&limit=25`)
      .then((res) => setDeviceAlerts(res.data))
      .catch(() => setAlertsError("Failed to load alerts for this device."))
      .finally(() => setAlertsLoading(false));
  };

  const acknowledgeAlert = async (alertId: string) => {
    setAckingId(alertId);
    try {
      const res = await api.patch<AlertRow>(`/alerts/${alertId}/acknowledge`);
      setDeviceAlerts((prev) => prev.map((a) => (a.id === alertId ? res.data : a)));
    } catch {
      // leave the row as-is; the button re-enables so the user can retry
    } finally {
      setAckingId(null);
    }
  };

  useEffect(() => {
    if ((activeTab === "Health" || activeTab === "Interfaces") && !health && !healthLoading) {
      loadHealth();
    }
    if (activeTab === "Alerts" && deviceAlerts.length === 0 && !alertsLoading) {
      loadAlerts();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, device.id]);

  useEffect(() => {
    if (activeTab === "Configuration" && !running) {
      setConfigLoading(true);
      Promise.all([
        api.get<RunningConfig>(`/devices/${device.id}/config/running`).catch(() => null),
        api.get<StartupConfig>(`/devices/${device.id}/config/startup`).catch(() => null),
      ]).then(([runRes, startRes]) => {
        if (runRes) setRunning(runRes.data);
        if (startRes) setStartup(startRes.data);
        setConfigLoading(false);
      });
    }

    if (activeTab === "Backups" && history.length === 0) {
      setHistoryLoading(true);
      api
        .get<BackupHistoryEntry[]>(`/devices/${device.id}/config/backups`)
        .then((res) => setHistory(res.data))
        .catch(() => setHistoryError("Failed to load backup history."))
        .finally(() => setHistoryLoading(false));
    }
  }, [activeTab, device.id, running, history.length]);


  const runCompare = async () => {
    setComparing(true);
    setCompareResult(null);
    try {
      const res = await api.post<CompareConfigResponse>(`/devices/${device.id}/config/compare`, {
        base_snapshot_id: baseId || null,
        target_snapshot_id: targetId || null,
      });
      setCompareResult(res.data);
    } catch {
      // Ignored for ui simplicity
    } finally {
      setComparing(false);
    }
  };

  return (
    <div className="flex flex-col bg-slate-50 min-h-[400px]">
      <div className="flex border-b border-slate-200 overflow-x-auto hide-scrollbar bg-white">
        {TAB_NAMES.map((tab) => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={`whitespace-nowrap px-4 py-3 text-xs font-semibold uppercase tracking-wider transition-colors border-b-2 ${
              activeTab === tab
                ? "border-brandblue text-brandblue"
                : "border-transparent text-slate-500 hover:text-navy hover:bg-slate-50"
            }`}
          >
            {tab}
          </button>
        ))}
      </div>

      <div className="p-5 flex-1">
        {activeTab === "Overview" && (
          <div className="grid grid-cols-2 gap-4 max-w-lg">
            <div>
              <p className="text-xs text-slate-500">Hostname</p>
              <p className="font-medium text-navy">{device.hostname}</p>
            </div>
            <div>
              <p className="text-xs text-slate-500">IP Address</p>
              <p className="font-mono text-sm text-navy">{device.ip_address}</p>
            </div>
            <div>
              <p className="text-xs text-slate-500">Vendor</p>
              <p className="font-medium capitalize text-navy">{device.vendor}</p>
            </div>
            <div>
              <p className="text-xs text-slate-500">Site / Location</p>
              <p className="font-medium text-navy">{device.site || "Unknown"}</p>
            </div>
            <div>
              <p className="text-xs text-slate-500">Authentication</p>
              <p className="font-mono text-xs text-slate-600 bg-slate-100 px-2 py-1 rounded w-fit mt-1">
                {device.ssh_username || "none"} : *** ({device.ssh_credential_ref})
              </p>
            </div>
            <div>
              <p className="text-xs text-slate-500">System Uptime</p>
              <p className="text-slate-400 italic text-sm">Waiting for telemetry...</p>
            </div>
          </div>
        )}

        {activeTab === "Health" && (
          <div>
            {healthLoading && !health ? (
              <p className="text-xs text-slate-400">Loading telemetry…</p>
            ) : healthError && !health ? (
              <div className="text-slate-500 flex flex-col items-center justify-center h-48 opacity-70">
                <div className="text-3xl mb-2">🩺</div>
                <p className="text-sm font-medium">{healthError}</p>
                {!device.supports_snmp && (
                  <p className="text-xs text-slate-400 mt-1">
                    Enable SNMP monitoring for this device in Devices → Edit to start collecting health telemetry.
                  </p>
                )}
              </div>
            ) : (
              <div className="flex flex-col gap-5">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-3">
                    <span
                      className={`w-3 h-3 rounded-full ${
                        HEALTH_COLOR_STYLES[health?.health_color || "unknown"].dot
                      }`}
                    />
                    <span className="text-2xl font-bold text-navy">
                      {health?.health_score != null ? `${health.health_score}/100` : "No score yet"}
                    </span>
                    <span className={`text-xs font-bold uppercase tracking-wide ${health?.reachable ? "text-risklow" : "text-riskcrit"}`}>
                      {health?.reachable ? "Reachable" : "Unreachable"}
                    </span>
                  </div>
                  {canManage && (
                    <button
                      onClick={pollNow}
                      disabled={polling}
                      className="text-xs font-bold uppercase tracking-wider text-brandblue border border-blue-200 bg-blue-50 px-3 py-1.5 rounded-lg hover:bg-blue-100 disabled:opacity-50"
                    >
                      {polling ? "Polling…" : "↻ Poll Now"}
                    </button>
                  )}
                </div>

                {healthError && <p className="text-riskcrit text-xs">{healthError}</p>}

                {health?.latest_metric ? (
                  <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                    {[
                      { label: "CPU", value: health.latest_metric.cpu_utilization_pct, suffix: "%" },
                      { label: "Memory", value: health.latest_metric.memory_utilization_pct, suffix: "%" },
                      { label: "Temperature", value: health.latest_metric.temperature_celsius, suffix: "°C" },
                      { label: "Uptime", value: formatUptime(health.latest_metric.uptime_seconds), suffix: "" },
                      { label: "Fan Status", value: health.latest_metric.fan_status || "unknown", suffix: "" },
                      { label: "Power Supply", value: health.latest_metric.power_supply_status || "unknown", suffix: "" },
                      { label: "Interface Util.", value: health.latest_metric.interface_utilization_pct, suffix: "%" },
                      { label: "Interface Errors", value: health.latest_metric.interface_errors, suffix: "" },
                    ].map((m) => (
                      <div key={m.label} className="bg-white border border-slate-200 rounded-lg p-3 shadow-sm">
                        <p className="text-[10px] uppercase font-bold text-slate-400 tracking-wider">{m.label}</p>
                        <p className="text-lg font-bold text-navy capitalize">
                          {m.value === null || m.value === undefined
                            ? "—"
                            : typeof m.value === "number"
                            ? `${Math.round(m.value * 10) / 10}${m.suffix}`
                            : m.value}
                        </p>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="text-xs text-slate-400 italic">No poll recorded yet.</p>
                )}

                {metricHistory.length > 0 && (
                  <div>
                    <h4 className="text-xs uppercase font-bold text-slate-500 mb-2 tracking-wider">
                      Last 24h ({metricHistory.length} poll{metricHistory.length === 1 ? "" : "s"})
                    </h4>
                    <div className="max-h-48 overflow-y-auto border border-slate-200 rounded-lg">
                      <table className="w-full text-xs">
                        <thead className="bg-slate-100 sticky top-0">
                          <tr>
                            <th className="text-left px-3 py-2 font-bold text-slate-500 uppercase">Polled</th>
                            <th className="text-left px-3 py-2 font-bold text-slate-500 uppercase">CPU</th>
                            <th className="text-left px-3 py-2 font-bold text-slate-500 uppercase">Mem</th>
                            <th className="text-left px-3 py-2 font-bold text-slate-500 uppercase">Temp</th>
                            <th className="text-left px-3 py-2 font-bold text-slate-500 uppercase">Score</th>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-slate-100">
                          {[...metricHistory].reverse().slice(0, 50).map((m) => (
                            <tr key={m.id} className="bg-white">
                              <td className="px-3 py-1.5 text-slate-500">{timeAgo(m.polled_at)}</td>
                              <td className="px-3 py-1.5 text-slate-700">{m.cpu_utilization_pct ?? "—"}</td>
                              <td className="px-3 py-1.5 text-slate-700">{m.memory_utilization_pct ?? "—"}</td>
                              <td className="px-3 py-1.5 text-slate-700">{m.temperature_celsius ?? "—"}</td>
                              <td className="px-3 py-1.5 font-bold text-slate-700">{m.health_score ?? "—"}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {activeTab === "Configuration" && (
          <div>
            {configLoading ? (
              <p className="text-xs text-slate-400">Loading configurations...</p>
            ) : (
              <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
                <div>
                  <h4 className="text-xs font-bold uppercase text-slate-500 mb-2">Running Configuration</h4>
                  <pre className="bg-slate-900 border border-slate-700 text-slate-300 text-[11px] rounded-lg p-3 overflow-x-auto max-h-[400px] whitespace-pre-wrap leading-relaxed shadow-inner">
                    {running?.config || "(no configuration available)"}
                  </pre>
                </div>
                <div>
                  <h4 className="text-xs font-bold uppercase text-slate-500 mb-2">Startup Configuration</h4>
                  {startup?.source === "unavailable" ? (
                    <p className="text-xs text-slate-400 italic mt-4">
                      No startup configuration on file yet.
                    </p>
                  ) : (
                    <pre className="bg-slate-900 border border-slate-700 text-slate-300 text-[11px] rounded-lg p-3 overflow-x-auto max-h-[400px] whitespace-pre-wrap leading-relaxed shadow-inner">
                      {startup?.config || "(no startup configuration available)"}
                    </pre>
                  )}
                </div>
              </div>
            )}
          </div>
        )}

        {activeTab === "Interfaces" && (
          <div>
            {healthLoading && metricHistory.length === 0 ? (
              <p className="text-xs text-slate-400">Loading interface telemetry…</p>
            ) : metricHistory.length === 0 ? (
              <div className="text-slate-500 flex flex-col items-center justify-center h-48 opacity-60">
                <div className="text-3xl mb-2">🔌</div>
                <p className="text-sm font-medium">No interface telemetry recorded yet for this device.</p>
                <p className="text-xs text-slate-400 mt-1">
                  {device.supports_snmp
                    ? "Waiting for the next SNMP poll (or use Poll Now on the Health tab)."
                    : "Enable SNMP monitoring on this device to start collecting it."}
                </p>
              </div>
            ) : (
              <div className="flex flex-col gap-4">
                <p className="text-xs text-slate-400">
                  NetGuard currently tracks fleet-aggregate interface throughput and error counts per SNMP poll
                  (not yet broken out per physical interface). Shown below: total link utilization and cumulative
                  error count over the last 24h.
                </p>
                <div className="grid grid-cols-2 gap-3 max-w-md">
                  <div className="bg-white border border-slate-200 rounded-lg p-3 shadow-sm">
                    <p className="text-[10px] uppercase font-bold text-slate-400 tracking-wider">Current Utilization</p>
                    <p className="text-lg font-bold text-navy">
                      {metricHistory[metricHistory.length - 1]?.interface_utilization_pct ?? "—"}%
                    </p>
                  </div>
                  <div className="bg-white border border-slate-200 rounded-lg p-3 shadow-sm">
                    <p className="text-[10px] uppercase font-bold text-slate-400 tracking-wider">Current Errors</p>
                    <p className="text-lg font-bold text-navy">
                      {metricHistory[metricHistory.length - 1]?.interface_errors ?? "—"}
                    </p>
                  </div>
                </div>
                <div className="max-h-64 overflow-y-auto border border-slate-200 rounded-lg">
                  <table className="w-full text-xs">
                    <thead className="bg-slate-100 sticky top-0">
                      <tr>
                        <th className="text-left px-3 py-2 font-bold text-slate-500 uppercase">Polled</th>
                        <th className="text-left px-3 py-2 font-bold text-slate-500 uppercase">Utilization</th>
                        <th className="text-left px-3 py-2 font-bold text-slate-500 uppercase">Errors</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {[...metricHistory].reverse().slice(0, 50).map((m) => (
                        <tr key={m.id} className="bg-white">
                          <td className="px-3 py-1.5 text-slate-500">{timeAgo(m.polled_at)}</td>
                          <td className="px-3 py-1.5 text-slate-700">
                            {m.interface_utilization_pct != null ? `${m.interface_utilization_pct}%` : "—"}
                          </td>
                          <td className={`px-3 py-1.5 font-bold ${m.interface_errors ? "text-riskcrit" : "text-slate-700"}`}>
                            {m.interface_errors ?? "—"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </div>
        )}

        {activeTab === "Backups" && (
          <div>
            {historyLoading ? (
              <p className="text-xs text-slate-400">Loading backup history...</p>
            ) : historyError ? (
              <p className="text-xs text-riskcrit">{historyError}</p>
            ) : history.length === 0 ? (
              <p className="text-xs text-slate-400 italic">No snapshots yet.</p>
            ) : (
              <div className="flex flex-col gap-6">
                <div>
                  <h4 className="text-xs uppercase font-bold text-slate-500 mb-3 tracking-wider">Snapshot History</h4>
                  <ul className="space-y-1.5 max-h-60 overflow-y-auto pr-2">
                    {history.map((s, idx) => (
                      <li
                        key={s.id}
                        className="flex items-center gap-3 text-xs bg-white border border-slate-200 rounded-lg px-3 py-2 shadow-sm"
                      >
                        <span className="font-mono text-slate-500 font-bold shrink-0">v{s.version}</span>
                        <span className="font-mono text-slate-400 shrink-0">{s.checksum.slice(0, 12)}…</span>
                        <span className="text-slate-500 font-medium">{new Date(s.created_at).toLocaleString()}</span>
                        {idx === 0 && (
                          <span className="px-1.5 py-0.5 rounded text-[10px] uppercase tracking-wide font-bold bg-navy text-white">
                            latest
                          </span>
                        )}
                        {canManage && (
                          <button
                            onClick={() => {
                              onQueueRollback(s, "");
                            }}
                            className="ml-auto text-amber-600 font-bold hover:text-amber-700 shrink-0 uppercase tracking-widest text-[10px] bg-amber-50 px-2 py-1 rounded"
                          >
                            ↺ Roll back to this
                          </button>
                        )}
                      </li>
                    ))}
                  </ul>
                </div>

                <div className="bg-white p-4 rounded-lg border border-slate-200 shadow-sm max-w-2xl">
                  <h4 className="text-xs uppercase font-bold text-slate-500 mb-3 tracking-wider flex justify-between items-center">
                    <span>Compare Snapshots</span>
                    <button onClick={runCompare} disabled={comparing} className="bg-brandblue text-white px-3 py-1 rounded text-[11px]">
                      {comparing ? "Comparing..." : "Run Compare"}
                    </button>
                  </h4>
                  <div className="grid grid-cols-2 gap-4">
                    <div>
                      <select className="w-full border border-slate-300 rounded px-2 py-1.5 text-xs text-slate-600" value={baseId} onChange={(e) => setBaseId(e.target.value)}>
                        <option value="">Live Configuration</option>
                        {history.map(h => <option key={h.id} value={h.id}>v{h.version}</option>)}
                      </select>
                    </div>
                    <div>
                      <select className="w-full border border-slate-300 rounded px-2 py-1.5 text-xs text-slate-600" value={targetId} onChange={(e) => setTargetId(e.target.value)}>
                        <option value="">Live Configuration</option>
                        {history.map(h => <option key={h.id} value={h.id}>v{h.version}</option>)}
                      </select>
                    </div>
                  </div>
                  {compareResult && (
                    <div className="mt-4 border-t border-slate-100 pt-4">
                      {compareResult.identical ? (
                        <p className="text-green-600 font-medium text-xs text-center">Configurations are completely identical.</p>
                      ) : (
                        <div className="max-h-64 overflow-y-auto">
                           <ConfigDiff diffText={compareResult.diff} />
                        </div>
                      )}
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>
        )}

        {activeTab === "Alerts" && (
          <div>
            {alertsLoading ? (
              <p className="text-xs text-slate-400">Loading alerts…</p>
            ) : alertsError ? (
              <p className="text-xs text-riskcrit">{alertsError}</p>
            ) : deviceAlerts.length === 0 ? (
              <div className="text-slate-500 flex flex-col items-center justify-center h-48 opacity-60">
                <div className="text-3xl mb-2">✅</div>
                <p className="text-sm font-medium">No alerts recorded for this device.</p>
              </div>
            ) : (
              <ul className="space-y-2 max-h-96 overflow-y-auto pr-1">
                {deviceAlerts.map((a) => {
                  const style = ALERT_SEVERITY_STYLES[a.severity] || ALERT_SEVERITY_STYLES.info;
                  return (
                    <li key={a.id} className={`border rounded-lg px-3 py-2.5 shadow-sm ${style.bg}`}>
                      <div className="flex items-start justify-between gap-3">
                        <div className="flex items-start gap-2">
                          <span className="text-base leading-none mt-0.5">{style.icon}</span>
                          <div>
                            <p className={`text-xs font-bold uppercase tracking-wide ${style.text}`}>
                              {a.category}
                            </p>
                            <p className="text-sm text-navy mt-0.5">{a.message}</p>
                            <p className="text-[11px] text-slate-400 mt-1">
                              {timeAgo(a.created_at)} · {a.source.replace("_", " ")}
                              {a.resolved && " · resolved"}
                              {!a.resolved && a.acknowledged && " · acknowledged"}
                            </p>
                          </div>
                        </div>
                        {!a.acknowledged && !a.resolved && (
                          <button
                            onClick={() => acknowledgeAlert(a.id)}
                            disabled={ackingId === a.id}
                            className="shrink-0 text-[10px] uppercase tracking-wider font-bold text-slate-500 border border-slate-300 bg-white px-2 py-1 rounded hover:bg-slate-50 disabled:opacity-50"
                          >
                            {ackingId === a.id ? "…" : "Acknowledge"}
                          </button>
                        )}
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        )}

        {/* Placeholders for remaining tabs */}
        {["Drift", "Protocol Operations", "Deployment History"].includes(activeTab) && (
          <div className="text-slate-500 flex flex-col items-center justify-center h-48 opacity-60">
            <div className="text-3xl mb-2">🚧</div>
            <p className="text-sm font-medium">{activeTab} data integration coming in next phase.</p>
          </div>
        )}
      </div>
    </div>
  );
}

// --- Main Devices List ---

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
  const [clearingUnstableId, setClearingUnstableId] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [expandedDeviceId, setExpandedDeviceId] = useState<string | null>(null);

  const [rollbackTarget, setRollbackTarget] = useState<{ device: Device; snapshot: Snapshot } | null>(null);
  const [rollbackReason, setRollbackReason] = useState("");
  const [rollbackSubmitting, setRollbackSubmitting] = useState(false);
  const [rollbackError, setRollbackError] = useState<string | null>(null);
  const [rollbackNotice, setRollbackNotice] = useState<string | null>(null);

  const confirmRollback = async () => {
    if (!rollbackTarget) return;
    setRollbackSubmitting(true);
    setRollbackError(null);
    try {
      const res = await api.post(`/devices/${rollbackTarget.device.id}/rollback`, {
        snapshot_id: rollbackTarget.snapshot.id,
        reason: rollbackReason || undefined,
      });
      setRollbackNotice(
        `${res.data.message} (change request ${String(res.data.change_request_id).slice(0, 8)})`
      );
      setRollbackTarget(null);
      setRollbackReason("");
    } catch (err: any) {
      setRollbackError(err?.response?.data?.detail || "Failed to queue rollback.");
    } finally {
      setRollbackSubmitting(false);
    }
  };

  const load = () => {
    api
      .get<Device[]>("/devices")
      .then((res) => {
        setDevices(res.data);
        setError(null);
      })
      .catch(() => setError("Failed to load devices."))
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
      if (expandedDeviceId === id) setExpandedDeviceId(null);
    } catch (err: any) {
      setError(err?.response?.data?.detail || `Failed to remove ${hostname}.`);
    } finally {
      setDeletingId(null);
    }
  };

  const clearUnstableFlag = async (id: string, hostname: string) => {
    if (
      !window.confirm(
        `Clear the unstable flag for ${hostname}? This re-enables automated deploys — only do this after reviewing why it kept failing.`
      )
    )
      return;
    setClearingUnstableId(id);
    setError(null);
    try {
      const res = await api.post<Device>(`/devices/${id}/clear-unstable-flag`, {});
      setDevices((prev) => prev.map((d) => (d.id === id ? res.data : d)));
    } catch (err: any) {
      setError(err?.response?.data?.detail || `Failed to clear unstable flag for ${hostname}.`);
    } finally {
      setClearingUnstableId(null);
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
    <div className="pb-16 flex flex-col gap-6 md:p-2">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-3xl font-bold text-navy">Device Inventory</h1>
          <p className="text-sm text-slate-500 mt-1">Centralized inventory of managed network devices.</p>
        </div>
        <div className="flex items-center gap-3">
            <button
            onClick={load}
            className="text-brandblue font-medium hover:text-navy bg-white border border-brandblue hover:bg-slate-50 px-3 py-1.5 rounded-full transition shadow-sm text-xs"
            >
            ↻ Refresh
            </button>
            {canManage && (
            <button
                onClick={() => setShowForm((s) => !s)}
                className="bg-brandblue text-white rounded-full px-4 py-1.5 text-xs font-semibold shadow-sm hover:bg-navy transition-colors scale-100 active:scale-95"
            >
                {showForm ? "Cancel" : "+ Add Device"}
            </button>
            )}
        </div>
      </div>

      <div className="flex flex-wrap gap-3">
        <div className="bg-white border border-slate-200 shadow-sm rounded-lg px-4 py-2 text-[13px] text-slate-500">
          Total <span className="text-navy font-bold ml-1">{devices.length}</span>
        </div>
        <div className="bg-white border border-slate-200 shadow-sm rounded-lg px-4 py-2 text-[13px] text-slate-500 flex items-center gap-1.5">
          <span className="w-2.5 h-2.5 rounded-full bg-risklow shadow-sm" /> Online
          <span className="text-navy font-bold ml-1">{counts.online}</span>
        </div>
        <div className="bg-white border border-slate-200 shadow-sm rounded-lg px-4 py-2 text-[13px] text-slate-500 flex items-center gap-1.5">
          <span className="w-2.5 h-2.5 rounded-full bg-riskmed shadow-sm" /> Degraded
          <span className="text-navy font-bold ml-1">{counts.degraded}</span>
        </div>
        <div className="bg-white border border-slate-200 shadow-sm rounded-lg px-4 py-2 text-[13px] text-slate-500 flex items-center gap-1.5">
          <span className="w-2.5 h-2.5 rounded-full bg-slate-400 shadow-sm" /> Offline
          <span className="text-navy font-bold ml-1">{counts.offline}</span>
        </div>
      </div>

      {canManage && showForm && (
        <form onSubmit={submit} className="bg-white border-2 border-brandblue/30 rounded-xl p-5 shadow-sm">
          <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
            <input
              className="border border-slate-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brandblue"
              placeholder="Hostname (e.g. RTR-01)"
              value={form.hostname}
              onChange={(e) => setForm({ ...form, hostname: e.target.value })}
              required
            />
            <input
              className="border border-slate-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brandblue"
              placeholder="IP Address"
              value={form.ip_address}
              onChange={(e) => setForm({ ...form, ip_address: e.target.value })}
              required
            />
            <select
              className="border border-slate-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brandblue"
              value={form.vendor}
              onChange={(e) => setForm({ ...form, vendor: e.target.value })}
            >
              <option value="cisco">Cisco</option>
              <option value="juniper">Juniper</option>
              <option value="arista">Arista</option>
              <option value="linux">Linux</option>
            </select>
            <input
              className="border border-slate-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brandblue"
              placeholder="Site (optional)"
              value={form.site}
              onChange={(e) => setForm({ ...form, site: e.target.value })}
            />
          </div>

          <div className="grid grid-cols-1 md:grid-cols-4 gap-3 mt-3">
            <input
              className="border border-slate-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brandblue"
              placeholder="SSH Username"
              value={form.ssh_username}
              onChange={(e) => setForm({ ...form, ssh_username: e.target.value })}
              required
            />
            <div className="md:col-span-2">
              <input
                className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brandblue"
                placeholder="SSH Credential Ref"
                value={form.ssh_credential_ref}
                onChange={(e) => setForm({ ...form, ssh_credential_ref: e.target.value })}
                required
              />
            </div>
            <button
              type="submit"
              disabled={loading}
              className="bg-brandblue text-white rounded-lg px-4 py-2 text-sm font-semibold shadow hover:bg-navy transition-colors disabled:opacity-50 h-fit self-start"
            >
              {loading ? "Adding…" : "Add Device"}
            </button>
          </div>
        </form>
      )}

      {error && <p className="text-riskcrit font-semibold text-sm bg-red-50 border border-red-200 px-3 py-2 rounded-lg">{error}</p>}
      {rollbackNotice && (
        <p className="text-[13px] font-medium text-brandblue bg-blue-50 border border-blue-200 shadow-sm rounded-lg px-4 py-2.5">
          {rollbackNotice}{" "}
          <button onClick={() => setRollbackNotice(null)} className="ml-3 font-bold text-slate-400 hover:text-navy">
            ✕
          </button>
        </p>
      )}

      <div className="flex flex-wrap gap-2 items-center">
        <input
          className="border border-slate-300 shadow-sm rounded-full px-4 py-1.5 text-sm w-full max-w-sm focus:ring-2 focus:ring-brandblue focus:border-transparent outline-none"
          placeholder="Search hostname, IP, or site…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <select
          className="border border-slate-300 shadow-sm rounded-full px-4 py-1.5 text-sm text-slate-600 focus:ring-2 focus:ring-brandblue outline-none"
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
      </div>

      <div className="bg-white border border-slate-200 rounded-xl overflow-hidden shadow-sm">
        <table className="w-full text-sm">
          <thead className="bg-slate-100 border-b border-slate-200">
            <tr>
              <th className="text-left px-5 py-3.5 font-bold text-slate-600 uppercase text-xs tracking-wider">Hostname</th>
              <th className="text-left px-5 py-3.5 font-bold text-slate-600 uppercase text-xs tracking-wider">IP Address</th>
              <th className="text-left px-5 py-3.5 font-bold text-slate-600 uppercase text-xs tracking-wider">Vendor</th>
              <th className="text-left px-5 py-3.5 font-bold text-slate-600 uppercase text-xs tracking-wider">Site</th>
              <th className="text-left px-5 py-3.5 font-bold text-slate-600 uppercase text-xs tracking-wider">Status</th>
              <th className="text-left px-5 py-3.5 font-bold text-slate-600 uppercase text-xs tracking-wider">Details</th>
              {canManage && <th className="text-right px-5 py-3.5 font-bold text-slate-600 uppercase text-xs tracking-wider">Actions</th>}
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {initialLoading && (
              <tr>
                <td colSpan={canManage ? 7 : 6} className="text-center text-slate-500 py-12">
                   <div className="inline-block w-5 h-5 border-2 border-slate-200 border-t-brandblue rounded-full animate-spin mb-2" />
                   <p>Loading devices…</p>
                </td>
              </tr>
            )}
            {!initialLoading && filtered.length === 0 && (
              <tr>
                <td colSpan={canManage ? 7 : 6} className="text-center text-slate-400 py-10 font-medium">
                  {devices.length === 0 ? "No devices yet. Add one above." : "No devices match your search."}
                </td>
              </tr>
            )}
            {filtered.map((d) => (
              <React.Fragment key={d.id}>
                <tr className={`cursor-pointer transition-colors hover:bg-slate-50/70 border-l-4 ${
                    expandedDeviceId === d.id ? "bg-slate-50 border-l-navy" : "border-l-transparent bg-white"
                  }`} 
                  onClick={() => setExpandedDeviceId(expandedDeviceId === d.id ? null : d.id)}
                >
                <td className="px-5 py-4 font-bold text-navy">{d.hostname}</td>
                <td className="px-5 py-4 text-slate-500 font-mono text-xs font-semibold">{d.ip_address}</td>
                <td className="px-5 py-4 text-slate-600 capitalize font-medium">{d.vendor}</td>
                <td className="px-5 py-4 text-slate-600 font-medium">{d.site || "—"}</td>
                <td className="px-5 py-4">
                  <span className="inline-flex items-center gap-1.5 bg-slate-50 border border-slate-200 px-2.5 py-1 rounded-full shadow-sm">
                    <span className={`w-2 h-2 rounded-full ${statusColor[d.status]} animate-pulse`} />
                    <span className="capitalize text-[11px] font-bold text-slate-600 tracking-wide">{d.status}</span>
                  </span>
                  {d.flagged_unstable && (
                    <span
                      className="ml-2 inline-flex items-center gap-1.5 bg-red-50 border border-red-200 text-riskcrit px-2.5 py-1 rounded-full shadow-sm text-[11px] font-bold uppercase tracking-wide"
                      title="Failed deployment repeatedly; automated deploys blocked until a Network Administrator reviews it."
                    >
                      Unstable — Review Required
                    </span>
                  )}
                </td>
                <td className="px-5 py-4">
                  <span className="text-xs text-brandblue font-bold uppercase tracking-wider select-none">
                    {expandedDeviceId === d.id ? "Hide Details" : "View Details"}
                    <span className={`inline-block ml-2 transition-transform ${expandedDeviceId === d.id ? "rotate-180" : "rotate-0"}`}>▼</span>
                  </span>
                </td>
                {canManage && (
                  <td className="px-5 py-4 text-right">
                    <div className="flex justify-end gap-2">
                      {d.flagged_unstable && (
                        <button
                          onClick={(e) => {
                              e.stopPropagation();
                              clearUnstableFlag(d.id, d.hostname);
                          }}
                          disabled={clearingUnstableId === d.id}
                          className="text-[11px] uppercase tracking-wider text-brandblue border border-blue-200 bg-blue-50 px-2 py-1 rounded shadow-sm hover:bg-blue-100 font-bold disabled:opacity-50"
                        >
                          {clearingUnstableId === d.id ? "Wait…" : "Clear Flag"}
                        </button>
                      )}
                      <button
                        onClick={(e) => {
                            e.stopPropagation();
                            removeDevice(d.id, d.hostname);
                        }}
                        disabled={deletingId === d.id}
                        className="text-[11px] uppercase tracking-wider text-riskcrit border border-red-200 bg-red-50 px-2 py-1 rounded shadow-sm hover:bg-red-100 font-bold disabled:opacity-50"
                      >
                        {deletingId === d.id ? "Wait…" : "Remove"}
                      </button>
                    </div>
                  </td>
                )}
                </tr>
                {expandedDeviceId === d.id && (
                  <tr>
                    <td colSpan={canManage ? 7 : 6} className="p-0 border-b-4 border-slate-200">
                        <DeviceInlineDetails 
                            device={d} 
                            canManage={canManage}
                            onQueueRollback={(snapshot, reason) => {
                                setRollbackTarget({ device: d, snapshot });
                                setRollbackReason(reason);
                            }} 
                        />
                    </td>
                  </tr>
                )}
              </React.Fragment>
            ))}
          </tbody>
        </table>
      </div>

      {rollbackTarget && (
        <div className="fixed inset-0 bg-navy/60 backdrop-blur-sm flex items-center justify-center z-50 px-4">
          <div className="bg-white rounded-2xl shadow-2xl max-w-md w-full p-6">
            <h3 className="text-xl font-bold text-navy">Roll back {rollbackTarget.device.hostname}?</h3>
            <p className="text-[13px] text-slate-500 mt-3 leading-relaxed">
              This triggers a full pipeline redeployment restoring snapshot <span className="font-mono font-bold">v{rollbackTarget.snapshot.version}</span> (
              {new Date(rollbackTarget.snapshot.created_at).toLocaleString()}). 
            </p>
            <label className="block text-xs font-bold text-slate-600 mt-5 mb-1 uppercase tracking-wide">Reason (optional)</label>
            <input
              className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brandblue outline-none"
              placeholder="e.g. interface flapping after last change"
              value={rollbackReason}
              onChange={(e) => setRollbackReason(e.target.value)}
            />
            {rollbackError && <p className="text-riskcrit font-semibold text-xs mt-3 bg-red-50 p-2 rounded">{rollbackError}</p>}
            <div className="flex gap-3 justify-end mt-6">
              <button
                onClick={() => setRollbackTarget(null)}
                disabled={rollbackSubmitting}
                className="px-4 py-2 text-sm font-bold text-slate-500 hover:text-slate-700 disabled:opacity-50 transition-colors bg-slate-100 rounded-lg hover:bg-slate-200"
              >
                Cancel
              </button>
              <button
                onClick={confirmRollback}
                disabled={rollbackSubmitting}
                className="bg-riskcrit text-white rounded-lg px-5 py-2 text-sm font-bold hover:opacity-90 disabled:opacity-50 shadow-md transform active:scale-95 transition-all"
              >
                {rollbackSubmitting ? "Queuing Pipeline…" : "Confirm Rollback"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}