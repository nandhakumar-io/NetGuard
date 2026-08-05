import { useEffect, useRef, useState, useCallback } from "react";
import { api } from "../lib/api";
import { Alert, AlertSummary } from "../lib/types";

const SEVERITY_CONFIG = {
  critical: { color: "text-riskcrit", bg: "bg-riskcrit", bgLight: "bg-red-50", border: "border-riskcrit/20", icon: "🚨", label: "Critical" },
  warning: { color: "text-riskmed", bg: "bg-riskmed", bgLight: "bg-amber-50", border: "border-riskmed/20", icon: "⚠️", label: "Warning" },
  info: { color: "text-brandblue", bg: "bg-brandblue", bgLight: "bg-blue-50", border: "border-brandblue/20", icon: "ℹ️", label: "Info" },
} as const;

const SOURCE_LABELS: Record<string, string> = {
  snmp_trap: "SNMP Trap",
  health_poll: "Health Poll",
  drift: "Drift Detection",
  protocol_failure: "Protocol Failure",
};

function timeAgo(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime();
  const secs = Math.floor(diff / 1000);
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return `${days}d ago`;
}

export default function AlertCenter() {
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [summary, setSummary] = useState<AlertSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [expandedRoots, setExpandedRoots] = useState<Set<string>>(new Set());
  const toggleExpanded = (id: string) =>
    setExpandedRoots((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  // Filters
  const [severityFilter, setSeverityFilter] = useState<string>("");
  const [sourceFilter, setSourceFilter] = useState<string>("");
  const [statusFilter, setStatusFilter] = useState<string>("active");

  // Realtime
  const [connection, setConnection] = useState<"live" | "polling" | "connecting">("connecting");
  const wsRef = useRef<WebSocket | null>(null);

  const fetchAlerts = useCallback(() => {
    const params = new URLSearchParams();
    if (severityFilter) params.set("severity", severityFilter);
    if (sourceFilter) params.set("source", sourceFilter);
    if (statusFilter) params.set("status", statusFilter);
    params.set("limit", "100");

    api.get<Alert[]>(`/alerts?${params.toString()}`).then((res) => {
      setAlerts(res.data);
      setLoading(false);
    }).catch(() => setLoading(false));
  }, [severityFilter, sourceFilter, statusFilter]);

  const fetchSummary = useCallback(() => {
    api.get<AlertSummary>("/alerts/summary").then((res) => setSummary(res.data)).catch(() => {});
  }, []);

  // Initial fetch + re-fetch on filter change
  useEffect(() => {
    setLoading(true);
    fetchAlerts();
    fetchSummary();
  }, [fetchAlerts, fetchSummary]);

  // WebSocket for realtime
  useEffect(() => {
    let mounted = true;
    const base = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000/api/v1";
    const wsUrl = base.replace(/^http/, "ws") + "/alerts/ws";
    let ws: WebSocket | null = null;
    let pollInterval: ReturnType<typeof setInterval> | null = null;

    try {
      ws = new WebSocket(wsUrl);
      wsRef.current = ws;
      ws.onopen = () => mounted && setConnection("live");
      ws.onmessage = () => {
        if (!mounted) return;
        fetchAlerts();
        fetchSummary();
      };
      ws.onerror = () => {
        if (!mounted) return;
        setConnection("polling");
        if (!pollInterval) pollInterval = setInterval(() => { fetchAlerts(); fetchSummary(); }, 5000);
      };
      ws.onclose = () => {
        if (!mounted) return;
        setConnection((c) => (c === "live" ? "polling" : c));
        if (!pollInterval) pollInterval = setInterval(() => { fetchAlerts(); fetchSummary(); }, 5000);
      };
    } catch {
      setConnection("polling");
      pollInterval = setInterval(() => { fetchAlerts(); fetchSummary(); }, 5000);
    }

    return () => {
      mounted = false;
      ws?.close();
      if (pollInterval) clearInterval(pollInterval);
    };
  }, [fetchAlerts, fetchSummary]);

  const handleAcknowledge = (id: string) => {
    api.patch(`/alerts/${id}/acknowledge`).then(() => { fetchAlerts(); fetchSummary(); }).catch(() => {});
  };

  const handleResolve = (id: string) => {
    api.patch(`/alerts/${id}/resolve`).then(() => { fetchAlerts(); fetchSummary(); }).catch(() => {});
  };

  const [clearing, setClearing] = useState(false);
  const activeCount = summary ? summary.active_total : 0;

  const handleClearAlerts = async () => {
    if (activeCount === 0) return;
    if (!window.confirm(`Clear ${activeCount} active alert${activeCount === 1 ? "" : "s"}? They'll be marked resolved and removed from the active list.`)) {
      return;
    }
    setClearing(true);
    try {
      await api.post("/alerts/clear");
      fetchAlerts();
      fetchSummary();
    } catch {
      // fetchAlerts/fetchSummary already no-op on failure; nothing else to do
    } finally {
      setClearing(false);
    }
  };

  return (
    <div>
      {/* Header */}
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold text-navy dark:text-white">Alert Center</h1>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
            Monitor, acknowledge, and resolve network alerts in real time.
          </p>
        </div>
        <div className="text-right">
          <button
            onClick={handleClearAlerts}
            disabled={clearing || activeCount === 0}
            className="text-xs font-bold uppercase tracking-wider text-white bg-riskcrit/90 hover:bg-riskcrit px-3 py-1.5 rounded-lg shadow-sm disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {clearing ? "Clearing…" : `Clear Alerts${activeCount ? ` (${activeCount})` : ""}`}
          </button>
          <div className="text-xs text-slate-400 dark:text-slate-500 mt-2">
          <span
            className={`inline-flex items-center gap-1.5 font-medium mr-1 ${
              connection === "live" ? "text-risklow" : connection === "polling" ? "text-riskmed" : "text-slate-400 dark:text-slate-500"
            }`}
          >
            <span
              className={`w-1.5 h-1.5 rounded-full ${
                connection === "live" ? "bg-risklow animate-pulse" : connection === "polling" ? "bg-riskmed" : "bg-slate-300 dark:bg-slate-600"
              }`}
            />
            {connection === "live" ? "Live" : connection === "polling" ? "Polling" : "Connecting…"}
          </span>
          </div>
        </div>
      </div>

      {/* Summary Stats */}
      {summary && (
        <div className="grid grid-cols-2 md:grid-cols-5 gap-4 mt-6">
          {([
            { label: "Critical", value: summary.critical, color: "riskcrit", icon: "🚨" },
            { label: "Warnings", value: summary.warning, color: "riskmed", icon: "⚠️" },
            { label: "Info", value: summary.info, color: "brandblue", icon: "ℹ️" },
            { label: "Active Total", value: summary.active_total, color: "navy", icon: "📋" },
            { label: "Resolved", value: summary.resolved, color: "risklow", icon: "✅" },
          ] as const).map((s) => (
            <div key={s.label} className="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-5 shadow-sm group hover:shadow-md transition-shadow">
              <div className="flex items-center gap-2">
                <span className="text-lg">{s.icon}</span>
                <p className="text-xs font-medium text-slate-500 dark:text-slate-400 uppercase tracking-wide">{s.label}</p>
              </div>
              <p className={`text-3xl font-bold mt-2 text-${s.color}`}>{s.value}</p>
            </div>
          ))}
        </div>
      )}

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-3 mt-6 bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 px-5 py-3 shadow-sm">
        <span className="text-xs font-semibold text-slate-500 dark:text-slate-400 uppercase tracking-wider mr-1">Filters</span>

        <select
          id="severity-filter"
          value={severityFilter}
          onChange={(e) => setSeverityFilter(e.target.value)}
          className="text-sm border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-1.5 bg-slate-50 dark:bg-slate-900 focus:ring-2 focus:ring-brandblue/30 focus:border-brandblue outline-none transition"
        >
          <option value="">All Severities</option>
          <option value="critical">Critical</option>
          <option value="warning">Warning</option>
          <option value="info">Info</option>
        </select>

        <select
          id="source-filter"
          value={sourceFilter}
          onChange={(e) => setSourceFilter(e.target.value)}
          className="text-sm border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-1.5 bg-slate-50 dark:bg-slate-900 focus:ring-2 focus:ring-brandblue/30 focus:border-brandblue outline-none transition"
        >
          <option value="">All Sources</option>
          <option value="snmp_trap">SNMP Trap</option>
          <option value="health_poll">Health Poll</option>
          <option value="drift">Drift</option>
          <option value="protocol_failure">Protocol Failure</option>
        </select>

        <select
          id="status-filter"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          className="text-sm border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-1.5 bg-slate-50 dark:bg-slate-900 focus:ring-2 focus:ring-brandblue/30 focus:border-brandblue outline-none transition"
        >
          <option value="">All Statuses</option>
          <option value="active">Active</option>
          <option value="acknowledged">Acknowledged</option>
          <option value="resolved">Resolved</option>
        </select>

        {(severityFilter || sourceFilter || statusFilter !== "active") && (
          <button
            onClick={() => { setSeverityFilter(""); setSourceFilter(""); setStatusFilter("active"); }}
            className="text-xs text-brandblue hover:text-navy dark:text-white font-medium transition-colors"
          >
            Reset
          </button>
        )}
      </div>

      {/* Alert Timeline */}
      <div className="mt-6 space-y-3">
        {loading ? (
          <div className="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-12 text-center">
            <div className="inline-block w-6 h-6 border-2 border-brandblue/30 border-t-brandblue rounded-full animate-spin" />
            <p className="text-sm text-slate-500 dark:text-slate-400 mt-3">Loading alerts…</p>
          </div>
        ) : alerts.length === 0 ? (
          <div className="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-12 text-center">
            <div className="text-5xl mb-4">🛡️</div>
            <h3 className="text-lg font-semibold text-navy dark:text-white">All Clear</h3>
            <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">No alerts match the current filters. Your network is looking healthy.</p>
          </div>
        ) : (
          (() => {
            const byId = new Map(alerts.map((a) => [a.id, a]));
            const impactedByRoot = new Map<string, Alert[]>();
            for (const a of alerts) {
              if (a.suppressed && a.root_cause_alert_id && byId.has(a.root_cause_alert_id)) {
                const list = impactedByRoot.get(a.root_cause_alert_id) || [];
                list.push(a);
                impactedByRoot.set(a.root_cause_alert_id, list);
              }
            }
            // Top-level rows: everything that isn't suppressed under a root
            // cause visible in this list. A suppressed alert whose root
            // cause got filtered out of view still renders standalone (with
            // an "Impacted" badge) so nothing silently disappears.
            const topLevel = alerts.filter((a) => !(a.suppressed && a.root_cause_alert_id && byId.has(a.root_cause_alert_id)));

            const renderAlert = (alert: Alert, nested: boolean) => {
              const sev = SEVERITY_CONFIG[alert.severity] || SEVERITY_CONFIG.info;
              const impacted = impactedByRoot.get(alert.id) || [];
              const isExpanded = expandedRoots.has(alert.id);
              return (
                <div
                  key={alert.id}
                  className={`bg-white dark:bg-slate-800 rounded-xl border ${sev.border} shadow-sm hover:shadow-md transition-all duration-200 overflow-hidden ${
                    alert.resolved ? "opacity-60" : ""
                  } ${nested ? "ml-6 border-dashed" : ""}`}
                >
                  <div className="flex items-stretch">
                    {/* Severity bar */}
                    <div className={`w-1.5 ${sev.bg} shrink-0`} />

                    <div className="flex-1 px-5 py-4">
                      <div className="flex items-start justify-between gap-4">
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-2 flex-wrap">
                            <span className="text-base">{sev.icon}</span>
                            <span className={`text-[11px] font-bold uppercase tracking-wider ${sev.color} ${sev.bgLight} px-2 py-0.5 rounded-full`}>
                              {sev.label}
                            </span>
                            <span className="text-[11px] font-medium text-slate-400 dark:text-slate-500 bg-slate-100 dark:bg-slate-700 px-2 py-0.5 rounded-full">
                              {SOURCE_LABELS[alert.source] || alert.source}
                            </span>
                            {alert.acknowledged && !alert.resolved && (
                              <span className="text-[11px] font-medium text-brandblue bg-blue-50 px-2 py-0.5 rounded-full">
                                Acknowledged
                              </span>
                            )}
                            {alert.resolved && (
                              <span className="text-[11px] font-medium text-risklow bg-green-50 px-2 py-0.5 rounded-full">
                                Resolved
                              </span>
                            )}
                            {alert.suppressed && (
                              <span
                                title="Likely a downstream consequence of another active alert, per the topology graph"
                                className="text-[11px] font-medium text-slate-500 bg-slate-100 dark:bg-slate-700 dark:text-slate-300 px-2 py-0.5 rounded-full"
                              >
                                Impacted
                              </span>
                            )}
                          </div>
                          <h3 className="font-semibold text-navy dark:text-white mt-1.5 text-sm">{alert.category}</h3>
                          <p className="text-sm text-slate-600 dark:text-slate-300 mt-0.5">{alert.message}</p>
                          <div className="flex items-center gap-3 mt-2 text-[11px] text-slate-400 dark:text-slate-500">
                            <span>{timeAgo(alert.created_at)}</span>
                            {alert.device_id && <span>Device: {alert.device_id.slice(0, 8)}…</span>}
                            {alert.acknowledged_by && <span>Ack'd by {alert.acknowledged_by}</span>}
                            {alert.resolved_by && <span>Resolved by {alert.resolved_by}</span>}
                          </div>
                          {!nested && impacted.length > 0 && (
                            <button
                              onClick={() => toggleExpanded(alert.id)}
                              className="mt-2 text-[11px] font-semibold text-brandblue hover:text-navy dark:text-white flex items-center gap-1"
                            >
                              <span>{isExpanded ? "▾" : "▸"}</span>
                              {impacted.length} downstream alert{impacted.length === 1 ? "" : "s"} suppressed as impacted
                            </button>
                          )}
                        </div>

                        {/* Actions */}
                        {!alert.resolved && (
                          <div className="flex items-center gap-2 shrink-0">
                            {!alert.acknowledged && (
                              <button
                                onClick={() => handleAcknowledge(alert.id)}
                                className="text-xs font-medium text-brandblue hover:text-navy dark:text-white bg-blue-50 hover:bg-blue-100 px-3 py-1.5 rounded-lg transition-colors"
                              >
                                Acknowledge
                              </button>
                            )}
                            <button
                              onClick={() => handleResolve(alert.id)}
                              className="text-xs font-medium text-risklow hover:text-green-800 bg-green-50 hover:bg-green-100 px-3 py-1.5 rounded-lg transition-colors"
                            >
                              Resolve
                            </button>
                          </div>
                        )}
                      </div>
                    </div>
                  </div>
                </div>
              );
            };

            return topLevel.map((alert) => (
              <div key={alert.id} className="space-y-2">
                {renderAlert(alert, false)}
                {expandedRoots.has(alert.id) &&
                  (impactedByRoot.get(alert.id) || []).map((child) => renderAlert(child, true))}
              </div>
            ));
          })()
        )}
      </div>
    </div>
  );
}