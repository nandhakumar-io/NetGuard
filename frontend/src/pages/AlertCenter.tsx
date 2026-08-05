import { useEffect, useRef, useState, useCallback } from "react";
import { api } from "../lib/api";
import { Alert, AlertSummary, AlertRule, WebhookEndpoint, WebhookTestResult } from "../lib/types";

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
  syslog: "Syslog",
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

const OP_LABELS: Record<string, string> = { gt: ">", gte: "≥", lt: "<", lte: "≤", eq: "=" };
const METRIC_LABELS: Record<string, string> = { cpu: "CPU %", memory: "Memory %", bandwidth: "Bandwidth %", temperature: "Temp °C", uptime: "Uptime (s)" };

type Tab = "alerts" | "rules" | "webhooks";

export default function AlertCenter() {
  const [tab, setTab] = useState<Tab>("alerts");
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

  // Alert Rules state
  const [alertRules, setAlertRules] = useState<AlertRule[]>([]);
  const [rulesLoading, setRulesLoading] = useState(false);
  const [showRuleForm, setShowRuleForm] = useState(false);
  const [editingRule, setEditingRule] = useState<AlertRule | null>(null);
  const [ruleForm, setRuleForm] = useState({ name: "", description: "", metric: "cpu", operator: "gt", threshold: "90", severity: "warning", scope_vendor: "", scope_site: "", scope_device_role: "", cooldown_seconds: "300" });

  // Webhooks state
  const [webhooks, setWebhooks] = useState<WebhookEndpoint[]>([]);
  const [webhooksLoading, setWebhooksLoading] = useState(false);
  const [showWebhookForm, setShowWebhookForm] = useState(false);
  const [editingWebhook, setEditingWebhook] = useState<WebhookEndpoint | null>(null);
  const [webhookForm, setWebhookForm] = useState({ name: "", url: "", webhook_type: "generic", secret: "", telegram_chat_id: "", events: "" });
  const [testingWebhook, setTestingWebhook] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<WebhookTestResult | null>(null);

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

  const fetchRules = useCallback(() => {
    setRulesLoading(true);
    api.get<AlertRule[]>("/alert-rules").then((res) => setAlertRules(res.data)).catch(() => {}).finally(() => setRulesLoading(false));
  }, []);

  const fetchWebhooks = useCallback(() => {
    setWebhooksLoading(true);
    api.get<WebhookEndpoint[]>("/webhooks").then((res) => setWebhooks(res.data)).catch(() => {}).finally(() => setWebhooksLoading(false));
  }, []);

  useEffect(() => {
    setLoading(true);
    fetchAlerts();
    fetchSummary();
  }, [fetchAlerts, fetchSummary]);

  useEffect(() => {
    if (tab === "rules") fetchRules();
    if (tab === "webhooks") fetchWebhooks();
  }, [tab, fetchRules, fetchWebhooks]);

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
    if (
      !window.confirm(
        `Permanently delete all alerts? This removes every entry -- including resolved history -- and cannot be undone.`
      )
    ) {
      return;
    }
    setClearing(true);
    try {
      await api.delete("/alerts/clear");
      fetchAlerts();
      fetchSummary();
    } catch {
    } finally {
      setClearing(false);
    }
  };

  // NOC counts from alerts
  const nocUnreachable = alerts.filter((a) => !a.resolved && a.category === "Device Unreachable").length;
  const nocPortsDown = alerts.filter((a) => !a.resolved && a.category.startsWith("Interface Down")).length;
  const nocReboots = alerts.filter((a) => !a.resolved && a.category === "Device Restart").length;

  // Alert Rule CRUD
  const resetRuleForm = () => {
    setRuleForm({ name: "", description: "", metric: "cpu", operator: "gt", threshold: "90", severity: "warning", scope_vendor: "", scope_site: "", scope_device_role: "", cooldown_seconds: "300" });
    setEditingRule(null);
    setShowRuleForm(false);
  };

  const handleSaveRule = async () => {
    const payload = {
      name: ruleForm.name,
      description: ruleForm.description || null,
      metric: ruleForm.metric,
      operator: ruleForm.operator,
      threshold: parseFloat(ruleForm.threshold),
      severity: ruleForm.severity,
      scope_vendor: ruleForm.scope_vendor || null,
      scope_site: ruleForm.scope_site || null,
      scope_device_role: ruleForm.scope_device_role || null,
      cooldown_seconds: parseInt(ruleForm.cooldown_seconds) || 300,
    };
    try {
      if (editingRule) {
        await api.put(`/alert-rules/${editingRule.id}`, payload);
      } else {
        await api.post("/alert-rules", payload);
      }
      resetRuleForm();
      fetchRules();
    } catch {}
  };

  const handleDeleteRule = async (id: string) => {
    if (!window.confirm("Delete this alert rule?")) return;
    try {
      await api.delete(`/alert-rules/${id}`);
      fetchRules();
    } catch {}
  };

  const handleToggleRule = async (id: string) => {
    try {
      await api.patch(`/alert-rules/${id}/toggle`);
      fetchRules();
    } catch {}
  };

  // Webhook CRUD
  const resetWebhookForm = () => {
    setWebhookForm({ name: "", url: "", webhook_type: "generic", secret: "", telegram_chat_id: "", events: "" });
    setEditingWebhook(null);
    setShowWebhookForm(false);
  };

  const handleSaveWebhook = async () => {
    const evts = webhookForm.events.trim() ? webhookForm.events.split(",").map((s) => s.trim()).filter(Boolean) : null;
    const payload = {
      name: webhookForm.name,
      url: webhookForm.url,
      webhook_type: webhookForm.webhook_type,
      secret: webhookForm.secret || null,
      telegram_chat_id: webhookForm.telegram_chat_id || null,
      events: evts,
    };
    try {
      if (editingWebhook) {
        await api.put(`/webhooks/${editingWebhook.id}`, payload);
      } else {
        await api.post("/webhooks", payload);
      }
      resetWebhookForm();
      fetchWebhooks();
    } catch {}
  };

  const handleDeleteWebhook = async (id: string) => {
    if (!window.confirm("Delete this webhook endpoint?")) return;
    try {
      await api.delete(`/webhooks/${id}`);
      fetchWebhooks();
    } catch {}
  };

  const handleTestWebhook = async (id: string) => {
    setTestingWebhook(id);
    setTestResult(null);
    try {
      const res = await api.post<WebhookTestResult>(`/webhooks/${id}/test`);
      setTestResult(res.data);
    } catch {
      setTestResult({ success: false, message: "Request failed", status_code: null });
    } finally {
      setTestingWebhook(null);
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

      {/* NOC Summary Bar */}
      <div className="grid grid-cols-3 gap-4 mt-6">
        <div className="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-4 shadow-sm flex items-center gap-3">
          <div className={`w-10 h-10 rounded-lg flex items-center justify-center text-lg font-bold ${nocUnreachable > 0 ? "bg-red-100 dark:bg-red-950/40 text-riskcrit" : "bg-green-50 dark:bg-green-950/30 text-risklow"}`}>
            {nocUnreachable > 0 ? "🔴" : "✅"}
          </div>
          <div>
            <p className="text-2xl font-black text-navy dark:text-white leading-none">{nocUnreachable}</p>
            <p className="text-[10px] font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 mt-0.5">Devices Down</p>
          </div>
        </div>
        <div className="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-4 shadow-sm flex items-center gap-3">
          <div className={`w-10 h-10 rounded-lg flex items-center justify-center text-lg font-bold ${nocPortsDown > 0 ? "bg-amber-100 dark:bg-amber-950/40 text-riskmed" : "bg-green-50 dark:bg-green-950/30 text-risklow"}`}>
            {nocPortsDown > 0 ? "⚠️" : "✅"}
          </div>
          <div>
            <p className="text-2xl font-black text-navy dark:text-white leading-none">{nocPortsDown}</p>
            <p className="text-[10px] font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 mt-0.5">Ports Down</p>
          </div>
        </div>
        <div className="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-4 shadow-sm flex items-center gap-3">
          <div className={`w-10 h-10 rounded-lg flex items-center justify-center text-lg font-bold ${nocReboots > 0 ? "bg-blue-100 dark:bg-blue-950/40 text-brandblue" : "bg-green-50 dark:bg-green-950/30 text-risklow"}`}>
            {nocReboots > 0 ? "🔄" : "✅"}
          </div>
          <div>
            <p className="text-2xl font-black text-navy dark:text-white leading-none">{nocReboots}</p>
            <p className="text-[10px] font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 mt-0.5">Recent Reboots</p>
          </div>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex items-center gap-1 mt-6 bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 px-2 py-1.5 shadow-sm">
        {(["alerts", "rules", "webhooks"] as Tab[]).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-4 py-2 text-xs font-bold uppercase tracking-wider rounded-lg transition-colors ${
              tab === t
                ? "bg-brandblue text-white shadow-sm"
                : "text-slate-500 dark:text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-700"
            }`}
          >
            {t === "alerts" ? "🔔 Alerts" : t === "rules" ? "⚙️ Alert Rules" : "🔗 Webhooks"}
          </button>
        ))}
      </div>

      {/* ===== ALERTS TAB ===== */}
      {tab === "alerts" && (
        <>
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
              <option value="syslog">Syslog</option>
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
                                  <span className="text-[11px] font-medium text-brandblue bg-blue-50 px-2 py-0.5 rounded-full">Acknowledged</span>
                                )}
                                {alert.resolved && (
                                  <span className="text-[11px] font-medium text-risklow bg-green-50 px-2 py-0.5 rounded-full">Resolved</span>
                                )}
                                {alert.suppressed && (
                                  <span title="Likely a downstream consequence of another active alert" className="text-[11px] font-medium text-slate-500 bg-slate-100 dark:bg-slate-700 dark:text-slate-300 px-2 py-0.5 rounded-full">Impacted</span>
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
        </>
      )}

      {/* ===== ALERT RULES TAB ===== */}
      {tab === "rules" && (
        <div className="mt-6 space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-bold text-navy dark:text-white uppercase tracking-wider">Custom Alert Rules</h2>
            <button
              onClick={() => { resetRuleForm(); setShowRuleForm(true); }}
              className="text-xs font-bold uppercase tracking-wider text-white bg-brandblue hover:bg-navy px-3 py-1.5 rounded-lg shadow-sm"
            >
              + New Rule
            </button>
          </div>

          {/* Rule Form */}
          {showRuleForm && (
            <div className="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-5 shadow-sm">
              <h3 className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-4">{editingRule ? "Edit Rule" : "Create Rule"}</h3>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                <input placeholder="Rule Name" value={ruleForm.name} onChange={(e) => setRuleForm({ ...ruleForm, name: e.target.value })} className="col-span-full text-sm border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 bg-slate-50 dark:bg-slate-900 focus:ring-2 focus:ring-brandblue/30 outline-none" />
                <select value={ruleForm.metric} onChange={(e) => setRuleForm({ ...ruleForm, metric: e.target.value })} className="text-sm border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 bg-slate-50 dark:bg-slate-900">
                  <option value="cpu">CPU %</option>
                  <option value="memory">Memory %</option>
                  <option value="bandwidth">Bandwidth %</option>
                  <option value="temperature">Temperature</option>
                  <option value="uptime">Uptime (s)</option>
                </select>
                <select value={ruleForm.operator} onChange={(e) => setRuleForm({ ...ruleForm, operator: e.target.value })} className="text-sm border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 bg-slate-50 dark:bg-slate-900">
                  <option value="gt">&gt; Greater Than</option>
                  <option value="gte">≥ Greater or Equal</option>
                  <option value="lt">&lt; Less Than</option>
                  <option value="lte">≤ Less or Equal</option>
                  <option value="eq">= Equal</option>
                </select>
                <input placeholder="Threshold" type="number" value={ruleForm.threshold} onChange={(e) => setRuleForm({ ...ruleForm, threshold: e.target.value })} className="text-sm border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 bg-slate-50 dark:bg-slate-900" />
                <select value={ruleForm.severity} onChange={(e) => setRuleForm({ ...ruleForm, severity: e.target.value })} className="text-sm border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 bg-slate-50 dark:bg-slate-900">
                  <option value="critical">Critical</option>
                  <option value="warning">Warning</option>
                  <option value="info">Info</option>
                </select>
                <input placeholder="Scope: Vendor (optional)" value={ruleForm.scope_vendor} onChange={(e) => setRuleForm({ ...ruleForm, scope_vendor: e.target.value })} className="text-sm border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 bg-slate-50 dark:bg-slate-900" />
                <input placeholder="Cooldown (seconds)" type="number" value={ruleForm.cooldown_seconds} onChange={(e) => setRuleForm({ ...ruleForm, cooldown_seconds: e.target.value })} className="text-sm border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 bg-slate-50 dark:bg-slate-900" />
                <textarea placeholder="Description (optional)" value={ruleForm.description} onChange={(e) => setRuleForm({ ...ruleForm, description: e.target.value })} className="col-span-full text-sm border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 bg-slate-50 dark:bg-slate-900 resize-none h-16" />
              </div>
              <div className="flex items-center gap-2 mt-4">
                <button onClick={handleSaveRule} disabled={!ruleForm.name || !ruleForm.threshold} className="text-xs font-bold uppercase tracking-wider text-white bg-brandblue hover:bg-navy px-4 py-2 rounded-lg shadow-sm disabled:opacity-40">{editingRule ? "Update" : "Create"}</button>
                <button onClick={resetRuleForm} className="text-xs font-bold uppercase tracking-wider text-slate-500 hover:text-slate-700 dark:text-slate-400 px-4 py-2">Cancel</button>
              </div>
            </div>
          )}

          {/* Rules List */}
          {rulesLoading ? (
            <div className="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-12 text-center">
              <div className="inline-block w-6 h-6 border-2 border-brandblue/30 border-t-brandblue rounded-full animate-spin" />
            </div>
          ) : alertRules.length === 0 ? (
            <div className="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-12 text-center">
              <div className="text-4xl mb-3">⚙️</div>
              <p className="text-sm font-bold text-slate-500 dark:text-slate-400">No custom alert rules configured yet.</p>
              <p className="text-xs text-slate-400 dark:text-slate-500 mt-1">Create rules to automatically fire alerts based on device metrics thresholds.</p>
            </div>
          ) : (
            <div className="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 shadow-sm overflow-hidden">
              <table className="w-full">
                <thead className="bg-slate-50 dark:bg-slate-900">
                  <tr>
                    <th className="text-left px-5 py-3 text-xs font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">Rule</th>
                    <th className="text-left px-5 py-3 text-xs font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">Condition</th>
                    <th className="text-left px-5 py-3 text-xs font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">Severity</th>
                    <th className="text-left px-5 py-3 text-xs font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">Status</th>
                    <th className="text-right px-5 py-3 text-xs font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 dark:divide-slate-700">
                  {alertRules.map((rule) => (
                    <tr key={rule.id} className="hover:bg-slate-50 dark:hover:bg-slate-900/50 transition-colors">
                      <td className="px-5 py-3">
                        <p className="text-sm font-semibold text-navy dark:text-white">{rule.name}</p>
                        {rule.description && <p className="text-xs text-slate-400 mt-0.5 truncate max-w-xs">{rule.description}</p>}
                      </td>
                      <td className="px-5 py-3">
                        <span className="text-sm font-mono text-slate-600 dark:text-slate-300">
                          {METRIC_LABELS[rule.metric] || rule.metric} {OP_LABELS[rule.operator] || rule.operator} {rule.threshold}
                        </span>
                      </td>
                      <td className="px-5 py-3">
                        <span className={`text-xs font-bold uppercase px-2 py-0.5 rounded-full ${
                          rule.severity === "critical" ? "text-riskcrit bg-red-50" : rule.severity === "warning" ? "text-riskmed bg-amber-50" : "text-brandblue bg-blue-50"
                        }`}>{rule.severity}</span>
                      </td>
                      <td className="px-5 py-3">
                        <button onClick={() => handleToggleRule(rule.id)} className={`text-xs font-bold uppercase px-2.5 py-1 rounded-lg transition-colors ${rule.enabled ? "text-risklow bg-green-50 hover:bg-green-100" : "text-slate-400 bg-slate-100 hover:bg-slate-200"}`}>
                          {rule.enabled ? "Active" : "Disabled"}
                        </button>
                      </td>
                      <td className="px-5 py-3 text-right">
                        <button
                          onClick={() => {
                            setEditingRule(rule);
                            setRuleForm({
                              name: rule.name, description: rule.description || "", metric: rule.metric, operator: rule.operator,
                              threshold: String(rule.threshold), severity: rule.severity, scope_vendor: rule.scope_vendor || "",
                              scope_site: rule.scope_site || "", scope_device_role: rule.scope_device_role || "",
                              cooldown_seconds: String(rule.cooldown_seconds),
                            });
                            setShowRuleForm(true);
                          }}
                          className="text-xs text-brandblue hover:text-navy font-medium mr-2"
                        >
                          Edit
                        </button>
                        <button onClick={() => handleDeleteRule(rule.id)} className="text-xs text-riskcrit hover:text-red-800 font-medium">Delete</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* ===== WEBHOOKS TAB ===== */}
      {tab === "webhooks" && (
        <div className="mt-6 space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-bold text-navy dark:text-white uppercase tracking-wider">Webhook Endpoints</h2>
            <button
              onClick={() => { resetWebhookForm(); setShowWebhookForm(true); }}
              className="text-xs font-bold uppercase tracking-wider text-white bg-brandblue hover:bg-navy px-3 py-1.5 rounded-lg shadow-sm"
            >
              + New Webhook
            </button>
          </div>

          {/* Webhook Form */}
          {showWebhookForm && (
            <div className="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-5 shadow-sm">
              <h3 className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-4">{editingWebhook ? "Edit Webhook" : "Create Webhook"}</h3>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <input placeholder="Webhook Name" value={webhookForm.name} onChange={(e) => setWebhookForm({ ...webhookForm, name: e.target.value })} className="text-sm border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 bg-slate-50 dark:bg-slate-900 focus:ring-2 focus:ring-brandblue/30 outline-none" />
                <select value={webhookForm.webhook_type} onChange={(e) => setWebhookForm({ ...webhookForm, webhook_type: e.target.value })} className="text-sm border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 bg-slate-50 dark:bg-slate-900">
                  <option value="generic">Generic HTTP</option>
                  <option value="slack">Slack</option>
                  <option value="teams">Microsoft Teams</option>
                  <option value="telegram">Telegram</option>
                </select>
                <input placeholder={webhookForm.webhook_type === "telegram" ? "https://api.telegram.org/bot<TOKEN>/sendMessage" : "Webhook URL"} value={webhookForm.url} onChange={(e) => setWebhookForm({ ...webhookForm, url: e.target.value })} className="col-span-full text-sm border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 bg-slate-50 dark:bg-slate-900" />
                {webhookForm.webhook_type === "telegram" && (
                  <input placeholder="Telegram Chat ID" value={webhookForm.telegram_chat_id} onChange={(e) => setWebhookForm({ ...webhookForm, telegram_chat_id: e.target.value })} className="text-sm border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 bg-slate-50 dark:bg-slate-900" />
                )}
                <input placeholder="Secret (optional, for HMAC signing)" value={webhookForm.secret} onChange={(e) => setWebhookForm({ ...webhookForm, secret: e.target.value })} className="text-sm border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 bg-slate-50 dark:bg-slate-900" />
                <input placeholder="Event filter (comma-separated, empty = all)" value={webhookForm.events} onChange={(e) => setWebhookForm({ ...webhookForm, events: e.target.value })} className="col-span-full text-sm border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 bg-slate-50 dark:bg-slate-900" />
              </div>
              <div className="flex items-center gap-2 mt-4">
                <button onClick={handleSaveWebhook} disabled={!webhookForm.name || !webhookForm.url} className="text-xs font-bold uppercase tracking-wider text-white bg-brandblue hover:bg-navy px-4 py-2 rounded-lg shadow-sm disabled:opacity-40">{editingWebhook ? "Update" : "Create"}</button>
                <button onClick={resetWebhookForm} className="text-xs font-bold uppercase tracking-wider text-slate-500 hover:text-slate-700 dark:text-slate-400 px-4 py-2">Cancel</button>
              </div>
            </div>
          )}

          {/* Test Result Banner */}
          {testResult && (
            <div className={`rounded-lg px-4 py-3 text-sm font-medium ${testResult.success ? "bg-green-50 text-risklow border border-green-200" : "bg-red-50 text-riskcrit border border-red-200"}`}>
              {testResult.success ? "✅" : "❌"} {testResult.message}
              <button onClick={() => setTestResult(null)} className="ml-3 text-xs opacity-60 hover:opacity-100">Dismiss</button>
            </div>
          )}

          {/* Webhooks List */}
          {webhooksLoading ? (
            <div className="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-12 text-center">
              <div className="inline-block w-6 h-6 border-2 border-brandblue/30 border-t-brandblue rounded-full animate-spin" />
            </div>
          ) : webhooks.length === 0 ? (
            <div className="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-12 text-center">
              <div className="text-4xl mb-3">🔗</div>
              <p className="text-sm font-bold text-slate-500 dark:text-slate-400">No webhook endpoints configured.</p>
              <p className="text-xs text-slate-400 dark:text-slate-500 mt-1">Add webhook endpoints to receive alert notifications via Slack, Teams, Telegram, or custom HTTP endpoints.</p>
            </div>
          ) : (
            <div className="space-y-3">
              {webhooks.map((wh) => (
                <div key={wh.id} className="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-5 shadow-sm hover:shadow-md transition-shadow">
                  <div className="flex items-start justify-between gap-4">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 mb-1">
                        <span className="text-base">{wh.webhook_type === "telegram" ? "📱" : wh.webhook_type === "slack" ? "💬" : wh.webhook_type === "teams" ? "🟣" : "🔗"}</span>
                        <h3 className="text-sm font-bold text-navy dark:text-white">{wh.name}</h3>
                        <span className="text-[10px] font-bold uppercase tracking-wider text-slate-400 bg-slate-100 dark:bg-slate-700 px-2 py-0.5 rounded-full">{wh.webhook_type}</span>
                        <span className={`text-[10px] font-bold uppercase px-2 py-0.5 rounded-full ${wh.enabled ? "text-risklow bg-green-50" : "text-slate-400 bg-slate-100"}`}>{wh.enabled ? "Active" : "Disabled"}</span>
                      </div>
                      <p className="text-xs text-slate-400 dark:text-slate-500 truncate font-mono">{wh.url}</p>
                      {wh.events && wh.events.length > 0 && (
                        <p className="text-[11px] text-slate-400 mt-1">Events: {wh.events.join(", ")}</p>
                      )}
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      <button
                        onClick={() => handleTestWebhook(wh.id)}
                        disabled={testingWebhook === wh.id}
                        className="text-xs font-medium text-brandblue hover:text-navy bg-blue-50 hover:bg-blue-100 px-3 py-1.5 rounded-lg transition-colors disabled:opacity-40"
                      >
                        {testingWebhook === wh.id ? "Testing…" : "Test"}
                      </button>
                      <button
                        onClick={() => {
                          setEditingWebhook(wh);
                          setWebhookForm({
                            name: wh.name, url: wh.url, webhook_type: wh.webhook_type,
                            secret: wh.secret || "", telegram_chat_id: wh.telegram_chat_id || "",
                            events: wh.events?.join(", ") || "",
                          });
                          setShowWebhookForm(true);
                        }}
                        className="text-xs text-brandblue hover:text-navy font-medium"
                      >
                        Edit
                      </button>
                      <button onClick={() => handleDeleteWebhook(wh.id)} className="text-xs text-riskcrit hover:text-red-800 font-medium">Delete</button>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}