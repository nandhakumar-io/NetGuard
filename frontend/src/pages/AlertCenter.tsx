import { Fragment, useEffect, useRef, useState, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { api, getAccessToken } from "../lib/api";
import { Alert, AlertSummary, AlertRule, AlertRunbook, WebhookEndpoint, WebhookTestResult, WebhookDeliveryAttempt, AlertSnooze, EscalationPolicy, EscalatedAlertEntry, PushSubscription } from "../lib/types";
import { useToast, errorMessage } from "../lib/toast";
import { useConfirm } from "../lib/confirm";
import SavedViews from "../components/SavedViews";

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

// Card view reads well at low volume but each alert eats ~90px of
// vertical space -- on a high-alert-volume day (a fleet-wide flap, a
// noisy new rule) that means constant scrolling to triage. Compact
// mode trades the card chrome for a dense table (one row per alert,
// ~32px) so an operator can scan/select across dozens of alerts at
// once. Persisted per-browser like the palette's recents, since it's
// a per-operator/per-shift viewing preference, not fleet state.
const ALERT_DENSITY_KEY = "netguard_alert_density_v1";
type AlertDensity = "comfortable" | "compact";
function loadAlertDensity(): AlertDensity {
  try {
    const v = localStorage.getItem(ALERT_DENSITY_KEY);
    return v === "compact" ? "compact" : "comfortable";
  } catch {
    return "comfortable";
  }
}

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
const METRIC_LABELS: Record<string, string> = {
  cpu: "CPU %", memory: "Memory %", bandwidth: "Bandwidth %", temperature: "Temp °C", uptime: "Uptime (s)",
  interface_errors: "Interface Errors", interface_down_count: "Interfaces Down", fan_failure: "Fan Failure", power_supply_failure: "PSU Failure",
  trunk_port_down: "Trunk Ports Down", sfp_port_down: "SFP Ports Down", route_unreachable: "Default Route Missing", ping_packet_loss_pct: "Ping Loss %",
};

type Tab = "alerts" | "rules" | "escalations" | "webhooks" | "push";

export default function AlertCenter() {
  const navigate = useNavigate();
  const [density, setDensity] = useState<AlertDensity>(loadAlertDensity);
  useEffect(() => {
    try {
      localStorage.setItem(ALERT_DENSITY_KEY, density);
    } catch {
      // best-effort -- a failed write just means the choice doesn't stick across reloads
    }
  }, [density]);
  const toast = useToast();
  const confirm = useConfirm();
  const [runningRunbookAlertId, setRunningRunbookAlertId] = useState<string | null>(null);

  const runRunbookRemediation = async (alert: Alert) => {
    if (!alert.runbook || !alert.device_id) return;
    const ok = await confirm(
      `This pushes the configured remediation command to the device for this alert. Continue?`,
      { title: "Run remediation now?", confirmLabel: "Run", danger: true }
    );
    if (!ok) return;
    setRunningRunbookAlertId(alert.id);
    try {
      const res = await api.post(`/alert-runbooks/${alert.runbook.id}/execute`, {
        device_id: alert.device_id,
        alert_id: alert.id,
      });
      if (res.data.status === "success") {
        toast.success("Remediation ran successfully");
      } else {
        toast.error(res.data.error || "Remediation failed — check runbook execution history.");
      }
    } catch (err) {
      toast.error(errorMessage(err, "Failed to run remediation."));
    } finally {
      setRunningRunbookAlertId(null);
    }
  };
  const [tab, setTab] = useState<Tab>("alerts");
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [summary, setSummary] = useState<AlertSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [expandedRoots, setExpandedRoots] = useState<Set<string>>(new Set());
  const toggleExpanded = (id: string) =>
    setExpandedRoots((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
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

  // Push subscriptions state (read-only summary here -- full CRUD stays
  // on /push-settings, this tab is just "can alerts actually reach a
  // phone" visibility from the same place alerts/webhooks live).
  const [pushSubs, setPushSubs] = useState<PushSubscription[]>([]);
  const [pushLoading, setPushLoading] = useState(false);
  const [pushTestingId, setPushTestingId] = useState<string | null>(null);
  const [showWebhookForm, setShowWebhookForm] = useState(false);
  const [editingWebhook, setEditingWebhook] = useState<WebhookEndpoint | null>(null);
  const [webhookForm, setWebhookForm] = useState({ name: "", url: "", webhook_type: "generic", secret: "", telegram_chat_id: "", events: "", include_actions: [] as string[], default_runbook_id: "" });
  const [runbooks, setRunbooks] = useState<AlertRunbook[]>([]);
  const [testingWebhook, setTestingWebhook] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<WebhookTestResult | null>(null);
  // Delivery log: expanded per-webhook (deliveriesOpenFor holds the
  // webhook id currently expanded, one at a time), fetched on demand
  // rather than eagerly for every webhook on tab load.
  const [deliveriesOpenFor, setDeliveriesOpenFor] = useState<string | null>(null);
  const [deliveries, setDeliveries] = useState<Record<string, WebhookDeliveryAttempt[]>>({});
  const [deliveriesLoading, setDeliveriesLoading] = useState<string | null>(null);
  const [retryingId, setRetryingId] = useState<string | null>(null);

  // Escalation Policies state
  const [policies, setPolicies] = useState<EscalationPolicy[]>([]);
  const [policiesLoading, setPoliciesLoading] = useState(false);
  const [escalationLog, setEscalationLog] = useState<EscalatedAlertEntry[]>([]);
  const [showPolicyForm, setShowPolicyForm] = useState(false);
  const [editingPolicy, setEditingPolicy] = useState<EscalationPolicy | null>(null);
  const [policyForm, setPolicyForm] = useState({
    name: "",
    description: "",
    severity_scope: "critical",
    unack_minutes: "15",
    repeat_minutes: "",
    secondary_contacts: "",
    channel: "email",
    webhook_url: "",
  });

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
    api.get<AlertRunbook[]>("/alert-runbooks").then((res) => setRunbooks(res.data)).catch(() => {});
  }, []);

  const fetchPushSubs = useCallback(() => {
    setPushLoading(true);
    api.get<PushSubscription[]>("/push-subscriptions").then((res) => setPushSubs(res.data)).catch(() => {}).finally(() => setPushLoading(false));
  }, []);

  const fetchPolicies = useCallback(() => {
    setPoliciesLoading(true);
    Promise.all([
      api.get<EscalationPolicy[]>("/escalation-policies"),
      api.get<EscalatedAlertEntry[]>("/escalation-policies/log"),
    ])
      .then(([policiesRes, logRes]) => {
        setPolicies(policiesRes.data);
        setEscalationLog(logRes.data);
      })
      .catch(() => {})
      .finally(() => setPoliciesLoading(false));
  }, []);

  useEffect(() => {
    setLoading(true);
    fetchAlerts();
    fetchSummary();
  }, [fetchAlerts, fetchSummary]);

  useEffect(() => {
    if (tab === "rules") fetchRules();
    if (tab === "webhooks") fetchWebhooks();
    if (tab === "escalations") fetchPolicies();
    if (tab === "push") fetchPushSubs();
  }, [tab, fetchRules, fetchWebhooks, fetchPolicies, fetchPushSubs]);

  // WebSocket for realtime
  useEffect(() => {
    let mounted = true;
    const base = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000/api/v1";
    const wsUrl = base.replace(/^http/, "ws") + "/alerts/ws?token=" + encodeURIComponent(getAccessToken() || "");
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

  // --- Bulk selection -- lets an admin triaging a pile of alerts after a
  // change window or outage ack/resolve many at once instead of one click
  // per row. Only top-level (non-suppressed) alerts are selectable, since
  // "impacted" alerts are just noise nested under their root cause.
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [bulkWorking, setBulkWorking] = useState(false);
  const toggleSelected = (id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };
  const clearSelection = () => setSelectedIds(new Set());
  const handleBulkAcknowledge = () => {
    const ids = Array.from(selectedIds);
    if (ids.length === 0) return;
    setBulkWorking(true);
    Promise.allSettled(ids.map((id) => api.patch(`/alerts/${id}/acknowledge`)))
      .then(() => { fetchAlerts(); fetchSummary(); clearSelection(); })
      .finally(() => setBulkWorking(false));
  };
  const handleBulkResolve = () => {
    const ids = Array.from(selectedIds);
    if (ids.length === 0) return;
    setBulkWorking(true);
    Promise.allSettled(ids.map((id) => api.patch(`/alerts/${id}/resolve`)))
      .then(() => { fetchAlerts(); fetchSummary(); clearSelection(); })
      .finally(() => setBulkWorking(false));
  };

  // --- Snooze / mute (per-device, per-rule/category, or both) ---
  const [snoozeTarget, setSnoozeTarget] = useState<Alert | null>(null);
  const [snoozeScope, setSnoozeScope] = useState<"device" | "category" | "both">("both");
  const [snoozeDuration, setSnoozeDuration] = useState("4h");
  const [snoozeReason, setSnoozeReason] = useState("");
  const [snoozeSaving, setSnoozeSaving] = useState(false);
  const [snoozeError, setSnoozeError] = useState<string | null>(null);
  const [activeSnoozes, setActiveSnoozes] = useState<AlertSnooze[]>([]);
  const [showSnoozeManager, setShowSnoozeManager] = useState(false);

  const fetchActiveSnoozes = useCallback(() => {
    api
      .get<AlertSnooze[]>("/alert-snoozes?active_only=true")
      .then((res) => setActiveSnoozes(res.data))
      .catch(() => {});
  }, []);

  useEffect(() => {
    fetchActiveSnoozes();
  }, [fetchActiveSnoozes]);

  const DURATION_TO_MS: Record<string, number> = {
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "24h": 24 * 60 * 60 * 1000,
    "7d": 7 * 24 * 60 * 60 * 1000,
  };

  const submitSnooze = () => {
    if (!snoozeTarget) return;
    setSnoozeSaving(true);
    setSnoozeError(null);
    const expires_at = new Date(Date.now() + DURATION_TO_MS[snoozeDuration]).toISOString();
    const payload: Record<string, unknown> = { expires_at, reason: snoozeReason.trim() || null };
    if (snoozeScope === "device" || snoozeScope === "both") payload.device_id = snoozeTarget.device_id;
    if (snoozeScope === "category" || snoozeScope === "both") payload.category = snoozeTarget.category;
    api
      .post("/alert-snoozes", payload)
      .then(() => {
        setSnoozeTarget(null);
        setSnoozeReason("");
        fetchAlerts();
        fetchActiveSnoozes();
      })
      .catch((err) => setSnoozeError(err?.response?.data?.detail || "Failed to create snooze."))
      .finally(() => setSnoozeSaving(false));
  };

  const cancelSnooze = (id: string) => {
    api
      .delete(`/alert-snoozes/${id}`)
      .then(() => {
        fetchActiveSnoozes();
        fetchAlerts();
      })
      .catch(() => {});
  };

  const [clearing, setClearing] = useState(false);
  const activeCount = summary ? summary.active_total : 0;

  const handleClearAlerts = async () => {
    if (activeCount === 0) return;
    if (
      !(await confirm(
        `Permanently delete all alerts? This removes every entry -- including resolved history -- and cannot be undone.`,
        { confirmLabel: "Delete all" }
      ))
    ) {
      return;
    }
    setClearing(true);
    try {
      await api.delete("/alerts/clear");
      fetchAlerts();
      fetchSummary();
    } catch {
      // Best-effort; the surrounding list/table stays on its last good state.
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

  // Common NOC-named conditions pre-filled into the form -- saves
  // hunting through the metric dropdown + guessing a sane operator/
  // threshold/severity for "alert me if a trunk port drops" and similar
  // asks that show up constantly but aren't obvious from a bare metric
  // picker alone.
  const RULE_PRESETS: Record<string, { name: string; description: string; metric: string; operator: string; threshold: string; severity: string }> = {
    trunk_down: { name: "Trunk Port Down", description: "Fires when any trunk-mode switchport goes oper-down.", metric: "trunk_port_down", operator: "gte", threshold: "1", severity: "critical" },
    sfp_down: { name: "SFP/Optic Port Down", description: "Fires when a likely SFP/optic-speed (1G+) port goes down.", metric: "sfp_port_down", operator: "gte", threshold: "1", severity: "warning" },
    route_unreachable: { name: "Default Route Missing", description: "Fires when the device's routing table has no default route.", metric: "route_unreachable", operator: "eq", threshold: "1", severity: "critical" },
    ping_drop: { name: "Sustained Ping Loss", description: "Fires when a 5-probe ping burst loses 20% or more.", metric: "ping_packet_loss_pct", operator: "gte", threshold: "20", severity: "warning" },
  };
  const applyRulePreset = (key: string) => {
    const preset = RULE_PRESETS[key];
    if (!preset) return;
    setRuleForm({ ...preset, scope_vendor: "", scope_site: "", scope_device_role: "", cooldown_seconds: "300" });
    setEditingRule(null);
    setShowRuleForm(true);
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
    } catch {
      // Best-effort; the surrounding list/table stays on its last good state.
    }
  };

  const handleDeleteRule = async (id: string) => {
    if (!(await confirm("Delete this alert rule?"))) return;
    try {
      await api.delete(`/alert-rules/${id}`);
      fetchRules();
    } catch (err) {
      toast.error(errorMessage(err, "Failed to delete alert rule."));
      // Best-effort; the surrounding list/table stays on its last good state.
    }
  };

  const handleToggleRule = async (id: string) => {
    try {
      await api.patch(`/alert-rules/${id}/toggle`);
      fetchRules();
    } catch {
      // Best-effort; the surrounding list/table stays on its last good state.
    }
  };

  // Escalation Policy CRUD
  const resetPolicyForm = () => {
    setPolicyForm({ name: "", description: "", severity_scope: "critical", unack_minutes: "15", repeat_minutes: "", secondary_contacts: "", channel: "email", webhook_url: "" });
    setEditingPolicy(null);
    setShowPolicyForm(false);
  };

  const handleSavePolicy = async () => {
    const payload = {
      name: policyForm.name,
      description: policyForm.description || null,
      severity_scope: policyForm.severity_scope,
      unack_minutes: parseInt(policyForm.unack_minutes) || 15,
      repeat_minutes: policyForm.repeat_minutes ? parseInt(policyForm.repeat_minutes) : null,
      secondary_contacts: policyForm.secondary_contacts || null,
      channel: policyForm.channel,
      webhook_url: policyForm.webhook_url || null,
    };
    try {
      if (editingPolicy) {
        await api.put(`/escalation-policies/${editingPolicy.id}`, payload);
      } else {
        await api.post("/escalation-policies", payload);
      }
      resetPolicyForm();
      fetchPolicies();
    } catch {
      // Best-effort; the surrounding list/table stays on its last good state.
    }
  };

  const handleDeletePolicy = async (id: string) => {
    if (!(await confirm("Delete this escalation policy?"))) return;
    try {
      await api.delete(`/escalation-policies/${id}`);
      fetchPolicies();
    } catch (err) {
      toast.error(errorMessage(err, "Failed to delete escalation policy."));
      // Best-effort; the surrounding list/table stays on its last good state.
    }
  };

  const handleTogglePolicy = async (id: string) => {
    try {
      await api.patch(`/escalation-policies/${id}/toggle`);
      fetchPolicies();
    } catch {
      // Best-effort; the surrounding list/table stays on its last good state.
    }
  };

  const handleRunEscalationNow = async () => {
    try {
      await api.post("/escalation-policies/run-now");
      fetchPolicies();
    } catch {
      // Best-effort.
    }
  };

  // Webhook CRUD
  const resetWebhookForm = () => {
    setWebhookForm({ name: "", url: "", webhook_type: "generic", secret: "", telegram_chat_id: "", events: "", include_actions: [], default_runbook_id: "" });
    setEditingWebhook(null);
    setShowWebhookForm(false);
  };

  const toggleWebhookAction = (action: string) => {
    setWebhookForm((f) => ({
      ...f,
      include_actions: f.include_actions.includes(action)
        ? f.include_actions.filter((a) => a !== action)
        : [...f.include_actions, action],
    }));
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
      include_actions: webhookForm.include_actions.length ? webhookForm.include_actions : null,
      default_runbook_id: webhookForm.include_actions.includes("run_runbook") && webhookForm.default_runbook_id ? webhookForm.default_runbook_id : null,
    };
    try {
      if (editingWebhook) {
        await api.put(`/webhooks/${editingWebhook.id}`, payload);
      } else {
        await api.post("/webhooks", payload);
      }
      resetWebhookForm();
      fetchWebhooks();
    } catch {
      // Best-effort; the surrounding list/table stays on its last good state.
    }
  };

  const handleDeleteWebhook = async (id: string) => {
    if (!(await confirm("Delete this webhook endpoint?"))) return;
    try {
      await api.delete(`/webhooks/${id}`);
      fetchWebhooks();
    } catch (err) {
      toast.error(errorMessage(err, "Failed to delete webhook endpoint."));
      // Best-effort; the surrounding list/table stays on its last good state.
    }
  };

  const handleTestPush = async (id: string) => {
    setPushTestingId(id);
    try {
      const res = await api.post<{ sent: boolean; message: string }>(`/push-subscriptions/${id}/test`);
      if (res.data.sent) toast.success(res.data.message || "Test push sent.");
      else toast.error(res.data.message || "Test push failed to send.");
    } catch (err) {
      toast.error(errorMessage(err, "Failed to send test push."));
    } finally {
      setPushTestingId(null);
    }
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

  const toggleDeliveries = async (id: string) => {
    if (deliveriesOpenFor === id) {
      setDeliveriesOpenFor(null);
      return;
    }
    setDeliveriesOpenFor(id);
    if (deliveries[id]) return; // already loaded once this session
    setDeliveriesLoading(id);
    try {
      const res = await api.get<WebhookDeliveryAttempt[]>(`/webhooks/${id}/deliveries`);
      setDeliveries((prev) => ({ ...prev, [id]: res.data }));
    } catch {
      setDeliveries((prev) => ({ ...prev, [id]: [] }));
    } finally {
      setDeliveriesLoading(null);
    }
  };

  const handleRetryDelivery = async (webhookId: string, attemptId: string) => {
    setRetryingId(attemptId);
    try {
      const res = await api.post<WebhookDeliveryAttempt>(`/webhooks/deliveries/${attemptId}/retry`);
      setDeliveries((prev) => ({
        ...prev,
        [webhookId]: [res.data, ...(prev[webhookId] || [])],
      }));
    } catch {
      // Best-effort -- the delivery list simply won't show a new row.
    } finally {
      setRetryingId(null);
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
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 mt-6">
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
        {/* "push" was defined in the Tab type, had its own fetch (fetchPushSubs)
            and its own render block below, but was never actually added to
            this button list -- so the Push Notifications tab existed and
            worked, it just had no way to be clicked into. Webhooks was the
            only delivery-channel tab visible as a result. */}
        {(["alerts", "rules", "escalations", "webhooks", "push"] as Tab[]).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-4 py-2 text-xs font-bold uppercase tracking-wider rounded-lg transition-colors ${
              tab === t
                ? "bg-brandblue text-white shadow-sm"
                : "text-slate-500 dark:text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-700"
            }`}
          >
            {t === "alerts"
              ? "🔔 Alerts"
              : t === "rules"
              ? "⚙️ Alert Rules"
              : t === "escalations"
              ? "📟 Escalations"
              : t === "webhooks"
              ? "🔗 Webhooks"
              : "📱 Push Notifications"}
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

            <SavedViews
              storageKey="netguard_saved_views_alerts"
              currentFilters={{ severityFilter, sourceFilter, statusFilter }}
              isDefault={(f) => !f.severityFilter && !f.sourceFilter && f.statusFilter === "active"}
              onApply={(f) => {
                setSeverityFilter(f.severityFilter);
                setSourceFilter(f.sourceFilter);
                setStatusFilter(f.statusFilter);
              }}
            />

            <div
              className="ml-auto flex items-center gap-0.5 bg-slate-100 dark:bg-slate-900 rounded-lg p-0.5"
              role="group"
              aria-label="Alert density"
            >
              <button
                onClick={() => setDensity("comfortable")}
                title="Card view -- one card per alert, more detail per row"
                className={`text-xs font-bold px-2.5 py-1 rounded-md transition-colors ${
                  density === "comfortable"
                    ? "bg-white dark:bg-slate-700 text-navy dark:text-white shadow-sm"
                    : "text-slate-400 dark:text-slate-500 hover:text-slate-600 dark:hover:text-slate-300"
                }`}
              >
                ▤ Cards
              </button>
              <button
                onClick={() => setDensity("compact")}
                title="Compact table -- scan and select across many alerts at once"
                className={`text-xs font-bold px-2.5 py-1 rounded-md transition-colors ${
                  density === "compact"
                    ? "bg-white dark:bg-slate-700 text-navy dark:text-white shadow-sm"
                    : "text-slate-400 dark:text-slate-500 hover:text-slate-600 dark:hover:text-slate-300"
                }`}
              >
                ☰ Table
              </button>
            </div>

            <button
              onClick={() => setShowSnoozeManager((v) => !v)}
              className="text-xs font-bold text-purple-600 hover:text-purple-800 bg-purple-50 hover:bg-purple-100 dark:bg-purple-950/40 dark:hover:bg-purple-950/60 px-3 py-1.5 rounded-lg transition-colors flex items-center gap-1.5"
            >
              🔕 Snoozes{activeSnoozes.length > 0 ? ` (${activeSnoozes.length})` : ""}
            </button>
          </div>

          {/* Active Snoozes manager -- view/cancel every currently-active
              device/rule mute, regardless of which alert (if any) is
              currently showing under the current filters. */}
          {showSnoozeManager && (
            <div className="mt-3 bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 px-5 py-4 shadow-sm">
              <h3 className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-3">Active Snoozes</h3>
              {activeSnoozes.length === 0 ? (
                <p className="text-xs text-slate-400 dark:text-slate-500 italic">Nothing is currently snoozed.</p>
              ) : (
                <div className="space-y-2">
                  {activeSnoozes.map((s) => (
                    <div key={s.id} className="flex items-center justify-between gap-3 text-xs bg-slate-50 dark:bg-slate-900 border border-slate-100 dark:border-slate-800 rounded-lg px-3 py-2">
                      <div className="flex items-center gap-2 flex-wrap min-w-0">
                        {s.device_hostname && (
                          <span className="font-bold text-navy dark:text-white">{s.device_hostname}</span>
                        )}
                        {s.category && (
                          <span className="font-mono text-slate-500 dark:text-slate-400 bg-slate-100 dark:bg-slate-700 px-1.5 py-0.5 rounded">{s.category}</span>
                        )}
                        {!s.device_hostname && !s.category && <span className="text-slate-400">(unscoped)</span>}
                        <span className="text-slate-400 dark:text-slate-500">until {new Date(s.expires_at).toLocaleString()}</span>
                        {s.reason && <span className="text-slate-400 dark:text-slate-500 italic truncate">— {s.reason}</span>}
                      </div>
                      <button
                        onClick={() => cancelSnooze(s.id)}
                        className="shrink-0 text-[11px] font-bold text-riskcrit hover:text-red-800"
                      >
                        Cancel
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* Alert Timeline */}
          <div className="mt-6 space-y-3">
            {selectedIds.size > 0 && (
              <div className="sticky top-0 z-10 flex items-center gap-3 bg-navy dark:bg-slate-950 text-white rounded-xl px-4 py-2.5 shadow-md flex-wrap">
                <span className="text-xs font-bold">{selectedIds.size} selected</span>
                <button
                  onClick={handleBulkAcknowledge}
                  disabled={bulkWorking}
                  className="text-xs font-semibold bg-white/15 hover:bg-white/25 px-3 py-1.5 rounded-lg transition-colors disabled:opacity-50"
                >
                  {bulkWorking ? "Working…" : "Acknowledge selected"}
                </button>
                <button
                  onClick={handleBulkResolve}
                  disabled={bulkWorking}
                  className="text-xs font-semibold bg-white/15 hover:bg-white/25 px-3 py-1.5 rounded-lg transition-colors disabled:opacity-50"
                >
                  {bulkWorking ? "Working…" : "Resolve selected"}
                </button>
                <button
                  onClick={clearSelection}
                  className="ml-auto text-xs font-semibold text-white/70 hover:text-white"
                >
                  Clear
                </button>
              </div>
            )}
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
                        {!nested && !alert.resolved && (
                          <label className="flex items-center pl-3 shrink-0" onClick={(e) => e.stopPropagation()}>
                            <input
                              type="checkbox"
                              checked={selectedIds.has(alert.id)}
                              onChange={() => toggleSelected(alert.id)}
                              className="w-3.5 h-3.5 accent-brandblue cursor-pointer"
                              aria-label={`Select alert: ${alert.category}`}
                            />
                          </label>
                        )}
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
                                {alert.muted_until && (
                                  <span
                                    title={`Snoozed until ${new Date(alert.muted_until).toLocaleString()}`}
                                    className="text-[11px] font-medium text-purple-600 bg-purple-50 dark:bg-purple-950/40 dark:text-purple-300 px-2 py-0.5 rounded-full"
                                  >
                                    🔕 Muted until {new Date(alert.muted_until).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
                                  </span>
                                )}
                                {alert.escalated && (
                                  <span
                                    title={
                                      alert.last_escalated_at
                                        ? `Escalated ${alert.escalation_count ?? 1}x, last at ${new Date(alert.last_escalated_at).toLocaleString()}`
                                        : "Escalated to secondary contact"
                                    }
                                    className="text-[11px] font-medium text-orange-600 bg-orange-50 dark:bg-orange-950/40 dark:text-orange-300 px-2 py-0.5 rounded-full"
                                  >
                                    📟 Escalated{(alert.escalation_count ?? 0) > 1 ? ` ×${alert.escalation_count}` : ""}
                                  </span>
                                )}
                              </div>
                              <h3 className="font-semibold text-navy dark:text-white mt-1.5 text-sm">{alert.category}</h3>
                              <p className="text-sm text-slate-600 dark:text-slate-300 mt-0.5">{alert.message}</p>
                              <div className="flex items-center gap-3 mt-2 text-[11px] text-slate-400 dark:text-slate-500">
                                <span>{timeAgo(alert.created_at)}</span>
                                {alert.device_id && (
                                  <button
                                    onClick={() => navigate(`/devices/${alert.device_id}`)}
                                    title="Open unified device view — health, drift, syslog, and config-change timeline"
                                    className="hover:text-brandblue hover:underline"
                                  >
                                    Device: {alert.device_id.slice(0, 8)}…
                                  </button>
                                )}
                                {alert.acknowledged_by && <span>Ack'd by {alert.acknowledged_by}</span>}
                                {alert.resolved_by && <span>Resolved by {alert.resolved_by}</span>}
                                {alert.runbook && (
                                  <a
                                    href={alert.runbook.url}
                                    target="_blank"
                                    rel="noreferrer"
                                    onClick={(e) => e.stopPropagation()}
                                    title={alert.runbook.title}
                                    className="flex items-center gap-1 text-indigo-600 dark:text-indigo-300 hover:underline font-medium"
                                  >
                                    📖 Runbook
                                  </a>
                                )}
                                {alert.runbook?.remediation_enabled && alert.device_id && (
                                  <button
                                    onClick={(e) => { e.stopPropagation(); runRunbookRemediation(alert); }}
                                    disabled={runningRunbookAlertId === alert.id}
                                    title="Run this runbook's remediation action against the affected device"
                                    className="flex items-center gap-1 text-violet-600 dark:text-violet-300 hover:underline font-medium disabled:opacity-50"
                                  >
                                    {runningRunbookAlertId === alert.id ? "Running…" : "⚡ Run Remediation"}
                                  </button>
                                )}
                              </div>
                              {!nested && impacted.length > 0 && (
                                <button
                                  onClick={() => toggleExpanded(alert.id)}
                                  className="mt-2 text-[11px] font-semibold text-brandblue hover:text-navy dark:text-white flex items-center gap-1"
                                >
                                  <span>{isExpanded ? "▾" : "▸"}</span>
                                  {impacted.length + 1} alerts, 1 root cause — {impacted.length} downstream suppressed as impacted
                                </button>
                              )}
                            </div>
                            {!alert.resolved && (
                              <div className="flex items-center gap-2 shrink-0">
                                {alert.device_id && (
                                  <button
                                    onClick={() => {
                                      const params = new URLSearchParams({
                                        alert_id: alert.id,
                                        device_id: alert.device_id!,
                                        category: alert.category,
                                      });
                                      navigate(`/change-requests?${params.toString()}`);
                                    }}
                                    title="Open a pre-linked change request to remediate this alert (auto-linked for postmortem)"
                                    className="text-xs font-medium text-navy dark:text-white hover:text-brandblue bg-slate-100 hover:bg-slate-200 dark:bg-slate-700 dark:hover:bg-slate-600 px-3 py-1.5 rounded-lg transition-colors"
                                  >
                                    Create Change Request
                                  </button>
                                )}
                                {!nested && impacted.length > 0 && (
                                  <button
                                    onClick={() => navigate(`/incidents?from_alert=${alert.id}`)}
                                    title="Open a formal incident from this correlated alert group for postmortem tracking"
                                    className="text-xs font-medium text-navy dark:text-white hover:text-brandblue bg-slate-100 hover:bg-slate-200 dark:bg-slate-700 dark:hover:bg-slate-600 px-3 py-1.5 rounded-lg transition-colors"
                                  >
                                    Open Incident
                                  </button>
                                )}
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
                                {!alert.muted_until && (
                                  <button
                                    onClick={() => {
                                      setSnoozeTarget(alert);
                                      setSnoozeScope(alert.device_id ? "both" : "category");
                                      setSnoozeError(null);
                                    }}
                                    title="Mute this device, this rule, or both for a bounded time"
                                    className="text-xs font-medium text-purple-600 hover:text-purple-800 bg-purple-50 hover:bg-purple-100 dark:bg-purple-950/40 dark:hover:bg-purple-950/60 px-3 py-1.5 rounded-lg transition-colors"
                                  >
                                    Snooze
                                  </button>
                                )}
                              </div>
                            )}
                          </div>
                        </div>
                      </div>
                    </div>
                  );
                };

                const renderAlertRow = (alert: Alert, nested: boolean) => {
                  const sev = SEVERITY_CONFIG[alert.severity] || SEVERITY_CONFIG.info;
                  const impacted = impactedByRoot.get(alert.id) || [];
                  const isExpanded = expandedRoots.has(alert.id);
                  return (
                    <tr
                      key={alert.id}
                      className={`border-b border-slate-100 dark:border-slate-700 last:border-0 hover:bg-slate-50 dark:hover:bg-slate-700/40 ${
                        alert.resolved ? "opacity-60" : ""
                      }`}
                    >
                      <td className="pl-3 py-1.5 w-8">
                        {!alert.resolved && (
                          <input
                            type="checkbox"
                            checked={selectedIds.has(alert.id)}
                            onChange={() => toggleSelected(alert.id)}
                            className="w-3.5 h-3.5 accent-brandblue cursor-pointer"
                            aria-label={`Select alert: ${alert.category}`}
                          />
                        )}
                      </td>
                      <td className="py-1.5 pr-2 w-8 text-center">
                        <span title={sev.label}>{sev.icon}</span>
                      </td>
                      <td className={`py-1.5 pr-3 min-w-0 ${nested ? "pl-6" : ""}`}>
                        <div className="flex items-center gap-1.5 min-w-0">
                          <span className="font-semibold text-navy dark:text-white text-xs truncate">{alert.category}</span>
                          <span className="text-slate-400 dark:text-slate-500 text-xs truncate">— {alert.message}</span>
                          {alert.runbook && (
                            <a
                              href={alert.runbook.url}
                              target="_blank"
                              rel="noreferrer"
                              onClick={(e) => e.stopPropagation()}
                              title={alert.runbook.title}
                              className="text-indigo-600 dark:text-indigo-300 hover:underline shrink-0"
                            >
                              📖
                            </a>
                          )}
                          {alert.runbook?.remediation_enabled && alert.device_id && (
                            <button
                              onClick={(e) => { e.stopPropagation(); runRunbookRemediation(alert); }}
                              disabled={runningRunbookAlertId === alert.id}
                              title="Run this runbook's remediation action against the affected device"
                              className="text-violet-600 dark:text-violet-300 hover:underline shrink-0 disabled:opacity-50"
                            >
                              {runningRunbookAlertId === alert.id ? "…" : "⚡"}
                            </button>
                          )}
                        </div>
                      </td>
                      <td className="py-1.5 pr-3 whitespace-nowrap">
                        <span className="text-[10px] font-medium text-slate-400 dark:text-slate-500 bg-slate-100 dark:bg-slate-700 px-1.5 py-0.5 rounded-full">
                          {SOURCE_LABELS[alert.source] || alert.source}
                        </span>
                      </td>
                      <td className="py-1.5 pr-3 whitespace-nowrap">
                        <div className="flex items-center gap-1 flex-wrap">
                          {alert.acknowledged && !alert.resolved && (
                            <span className="text-[10px] font-medium text-brandblue bg-blue-50 px-1.5 py-0.5 rounded-full">Ack'd</span>
                          )}
                          {alert.resolved && (
                            <span className="text-[10px] font-medium text-risklow bg-green-50 px-1.5 py-0.5 rounded-full">Resolved</span>
                          )}
                          {alert.suppressed && (
                            <span className="text-[10px] font-medium text-slate-500 bg-slate-100 dark:bg-slate-700 dark:text-slate-300 px-1.5 py-0.5 rounded-full">Impacted</span>
                          )}
                          {alert.muted_until && <span className="text-[10px] font-medium text-purple-600 bg-purple-50 dark:bg-purple-950/40 dark:text-purple-300 px-1.5 py-0.5 rounded-full">🔕</span>}
                          {alert.escalated && <span className="text-[10px] font-medium text-orange-600 bg-orange-50 dark:bg-orange-950/40 dark:text-orange-300 px-1.5 py-0.5 rounded-full">📟{(alert.escalation_count ?? 0) > 1 ? `×${alert.escalation_count}` : ""}</span>}
                        </div>
                      </td>
                      <td className="py-1.5 pr-3 whitespace-nowrap text-[11px] text-slate-400 dark:text-slate-500">{timeAgo(alert.created_at)}</td>
                      <td className="py-1.5 pr-3 whitespace-nowrap">
                        {alert.device_id ? (
                          <button
                            onClick={() => navigate(`/devices/${alert.device_id}`)}
                            className="text-[11px] text-slate-400 hover:text-brandblue hover:underline"
                          >
                            {alert.device_id.slice(0, 8)}…
                          </button>
                        ) : (
                          <span className="text-[11px] text-slate-300 dark:text-slate-600">—</span>
                        )}
                      </td>
                      <td className="py-1.5 pr-3">
                        {!nested && impacted.length > 0 && (
                          <button
                            onClick={() => toggleExpanded(alert.id)}
                            className="text-[10px] font-semibold text-brandblue hover:text-navy dark:text-white whitespace-nowrap"
                          >
                            {isExpanded ? "▾" : "▸"} +{impacted.length}
                          </button>
                        )}
                      </td>
                      <td className="py-1.5 pr-3">
                        {!alert.resolved && (
                          <div className="flex items-center gap-1 justify-end">
                            {!alert.acknowledged && (
                              <button
                                onClick={() => handleAcknowledge(alert.id)}
                                title="Acknowledge"
                                className="text-[10px] font-medium text-brandblue hover:text-navy dark:text-white bg-blue-50 hover:bg-blue-100 px-2 py-1 rounded-md transition-colors"
                              >
                                Ack
                              </button>
                            )}
                            <button
                              onClick={() => handleResolve(alert.id)}
                              title="Resolve"
                              className="text-[10px] font-medium text-risklow hover:text-green-800 bg-green-50 hover:bg-green-100 px-2 py-1 rounded-md transition-colors"
                            >
                              Resolve
                            </button>
                            {alert.device_id && (
                              <button
                                onClick={() => {
                                  const params = new URLSearchParams({
                                    alert_id: alert.id,
                                    device_id: alert.device_id!,
                                    category: alert.category,
                                  });
                                  navigate(`/change-requests?${params.toString()}`);
                                }}
                                title="Create Change Request"
                                className="text-[10px] font-medium text-navy dark:text-white hover:text-brandblue bg-slate-100 hover:bg-slate-200 dark:bg-slate-700 dark:hover:bg-slate-600 px-2 py-1 rounded-md transition-colors"
                              >
                                CR
                              </button>
                            )}
                          </div>
                        )}
                      </td>
                    </tr>
                  );
                };

                if (density === "compact") {
                  return (
                    <div className="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 shadow-sm overflow-x-auto">
                      <table className="w-full text-left border-collapse">
                        <thead>
                          <tr className="text-[10px] font-bold uppercase tracking-wider text-slate-400 dark:text-slate-500 border-b border-slate-200 dark:border-slate-700">
                            <th className="pl-3 py-2 w-8" />
                            <th className="py-2 w-8" />
                            <th className="py-2 pr-3">Alert</th>
                            <th className="py-2 pr-3">Source</th>
                            <th className="py-2 pr-3">Status</th>
                            <th className="py-2 pr-3">Age</th>
                            <th className="py-2 pr-3">Device</th>
                            <th className="py-2 pr-3" />
                            <th className="py-2 pr-3 text-right">Actions</th>
                          </tr>
                        </thead>
                        <tbody>
                          {topLevel.map((alert) => (
                            <Fragment key={alert.id}>
                              {renderAlertRow(alert, false)}
                              {expandedRoots.has(alert.id) &&
                                (impactedByRoot.get(alert.id) || []).map((child) => renderAlertRow(child, true))}
                            </Fragment>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  );
                }

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

      {/* Snooze creation modal */}
      {snoozeTarget && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm" onClick={() => setSnoozeTarget(null)}>
          <div
            className="w-full max-w-md bg-white dark:bg-slate-800 rounded-xl shadow-2xl border border-slate-200 dark:border-slate-700 p-5 flex flex-col gap-3"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-sm font-bold text-navy dark:text-white">Snooze this alert</h3>
            <p className="text-xs text-slate-500 dark:text-slate-400">
              {snoozeTarget.category} {snoozeTarget.device_id ? `on device ${snoozeTarget.device_id.slice(0, 8)}…` : "(no device)"}
            </p>
            {snoozeError && <p className="text-xs text-riskcrit font-semibold">{snoozeError}</p>}

            <label className="text-xs font-bold text-slate-500">
              Scope
              <select
                value={snoozeScope}
                onChange={(e) => setSnoozeScope(e.target.value as "device" | "category" | "both")}
                className="mt-1 w-full border border-slate-300 dark:border-slate-600 dark:bg-slate-900 rounded-lg px-3 py-1.5 text-sm"
              >
                {snoozeTarget.device_id && <option value="both">This device + this rule only</option>}
                {snoozeTarget.device_id && <option value="device">This device — mute every alert on it</option>}
                <option value="category">This rule — mute "{snoozeTarget.category}" fleet-wide</option>
              </select>
            </label>

            <label className="text-xs font-bold text-slate-500">
              For how long
              <select
                value={snoozeDuration}
                onChange={(e) => setSnoozeDuration(e.target.value)}
                className="mt-1 w-full border border-slate-300 dark:border-slate-600 dark:bg-slate-900 rounded-lg px-3 py-1.5 text-sm"
              >
                <option value="1h">1 hour</option>
                <option value="4h">4 hours</option>
                <option value="24h">24 hours</option>
                <option value="7d">7 days</option>
              </select>
            </label>

            <label className="text-xs font-bold text-slate-500">
              Reason (optional)
              <input
                value={snoozeReason}
                onChange={(e) => setSnoozeReason(e.target.value)}
                placeholder="e.g. known flapping while vendor RMA is pending"
                className="mt-1 w-full border border-slate-300 dark:border-slate-600 dark:bg-slate-900 rounded-lg px-3 py-1.5 text-sm"
              />
            </label>

            <div className="flex items-center justify-end gap-2 mt-2">
              <button onClick={() => setSnoozeTarget(null)} className="text-xs font-bold text-slate-500 px-3 py-1.5">
                Cancel
              </button>
              <button
                onClick={submitSnooze}
                disabled={snoozeSaving}
                className="text-xs font-bold text-white bg-purple-600 hover:bg-purple-700 disabled:opacity-50 px-4 py-1.5 rounded-full shadow-sm"
              >
                {snoozeSaving ? "Saving…" : "Snooze"}
              </button>
            </div>
          </div>
        </div>
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

          {/* One-click starting points for the conditions a NOC asks for
              by name most often -- pre-fills the form below rather than
              creating the rule outright, so scope/cooldown/threshold can
              still be adjusted before saving. */}
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[10px] font-bold uppercase tracking-wider text-slate-400 dark:text-slate-500">Quick add:</span>
            {[
              { key: "trunk_down", label: "🔌 Trunk Port Down" },
              { key: "sfp_down", label: "📡 SFP Port Down" },
              { key: "route_unreachable", label: "🧭 Route Unreachable" },
              { key: "ping_drop", label: "📶 Ping Drop" },
            ].map((p) => (
              <button
                key={p.key}
                onClick={() => applyRulePreset(p.key)}
                className="text-xs font-medium text-slate-600 dark:text-slate-300 bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 hover:border-brandblue hover:text-brandblue px-2.5 py-1 rounded-full transition-colors"
              >
                {p.label}
              </button>
            ))}
          </div>

          {/* Rule Form */}
          {showRuleForm && (
            <div className="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-5 shadow-sm">
              <h3 className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-4">{editingRule ? "Edit Rule" : "Create Rule"}</h3>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                <input placeholder="Rule Name" value={ruleForm.name} onChange={(e) => setRuleForm({ ...ruleForm, name: e.target.value })} className="col-span-full text-sm border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 bg-slate-50 dark:bg-slate-900 focus:ring-2 focus:ring-brandblue/30 outline-none" />
                <select value={ruleForm.metric} onChange={(e) => setRuleForm({ ...ruleForm, metric: e.target.value })} className="text-sm border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 bg-slate-50 dark:bg-slate-900">
                  <optgroup label="Utilization">
                    <option value="cpu">CPU %</option>
                    <option value="memory">Memory %</option>
                    <option value="bandwidth">Bandwidth %</option>
                    <option value="temperature">Temperature (°C)</option>
                    <option value="uptime">Uptime (s)</option>
                  </optgroup>
                  <optgroup label="Interface Health">
                    <option value="interface_errors">Interface Errors (count)</option>
                    <option value="interface_down_count">Interfaces Down (count)</option>
                  </optgroup>
                  <optgroup label="Hardware Health">
                    <option value="fan_failure">Fan Failure (1 = failed)</option>
                    <option value="power_supply_failure">Power Supply Failure (1 = failed)</option>
                  </optgroup>
                  <optgroup label="Link &amp; Path Health">
                    <option value="trunk_port_down">Trunk Ports Down (count)</option>
                    <option value="sfp_port_down">SFP/Optic Ports Down (count)</option>
                    <option value="route_unreachable">Default Route Missing (1 = missing)</option>
                    <option value="ping_packet_loss_pct">Ping Packet Loss (%)</option>
                  </optgroup>
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
                <input placeholder="Scope: Site (optional)" value={ruleForm.scope_site || ""} onChange={(e) => setRuleForm({ ...ruleForm, scope_site: e.target.value })} className="text-sm border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 bg-slate-50 dark:bg-slate-900" />
                <input placeholder="Scope: Device Role (optional)" value={ruleForm.scope_device_role || ""} onChange={(e) => setRuleForm({ ...ruleForm, scope_device_role: e.target.value })} className="text-sm border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 bg-slate-50 dark:bg-slate-900" />
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
            <div className="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 shadow-sm overflow-x-auto">
              <table className="w-full min-w-[720px]">
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

      {/* ===== ESCALATIONS TAB ===== */}
      {tab === "escalations" && (
        <div className="mt-6 space-y-6">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-bold text-navy dark:text-white uppercase tracking-wider">Escalation Policies</h2>
            <div className="flex items-center gap-2">
              <button
                onClick={handleRunEscalationNow}
                title="Trigger an out-of-cycle sweep instead of waiting for the next scheduled tick"
                className="text-xs font-bold uppercase tracking-wider text-slate-600 dark:text-slate-300 bg-slate-100 hover:bg-slate-200 dark:bg-slate-700 dark:hover:bg-slate-600 px-3 py-1.5 rounded-lg"
              >
                ▶ Run Now
              </button>
              <button
                onClick={() => { resetPolicyForm(); setShowPolicyForm(true); }}
                className="text-xs font-bold uppercase tracking-wider text-white bg-brandblue hover:bg-navy px-3 py-1.5 rounded-lg shadow-sm"
              >
                + New Policy
              </button>
            </div>
          </div>

          <p className="text-xs text-slate-500 dark:text-slate-400 -mt-2">
            If an alert matching a policy's severity sits unacknowledged past its threshold, secondary contacts are notified. Set a repeat interval to keep re-notifying until someone acks.
          </p>

          {/* Policy Form */}
          {showPolicyForm && (
            <div className="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-5 shadow-sm">
              <h3 className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-4">{editingPolicy ? "Edit Policy" : "Create Policy"}</h3>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                <input placeholder="Policy Name" value={policyForm.name} onChange={(e) => setPolicyForm({ ...policyForm, name: e.target.value })} className="col-span-full text-sm border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 bg-slate-50 dark:bg-slate-900 focus:ring-2 focus:ring-brandblue/30 outline-none" />
                <input placeholder="Description (optional)" value={policyForm.description} onChange={(e) => setPolicyForm({ ...policyForm, description: e.target.value })} className="col-span-full text-sm border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 bg-slate-50 dark:bg-slate-900" />
                <select value={policyForm.severity_scope} onChange={(e) => setPolicyForm({ ...policyForm, severity_scope: e.target.value })} className="text-sm border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 bg-slate-50 dark:bg-slate-900">
                  <option value="critical">Critical only</option>
                  <option value="warning">Warning only</option>
                  <option value="all">All severities</option>
                </select>
                <input placeholder="Unacknowledged minutes" type="number" value={policyForm.unack_minutes} onChange={(e) => setPolicyForm({ ...policyForm, unack_minutes: e.target.value })} className="text-sm border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 bg-slate-50 dark:bg-slate-900" />
                <input placeholder="Repeat every N minutes (optional)" type="number" value={policyForm.repeat_minutes} onChange={(e) => setPolicyForm({ ...policyForm, repeat_minutes: e.target.value })} className="text-sm border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 bg-slate-50 dark:bg-slate-900" />
                <select value={policyForm.channel} onChange={(e) => setPolicyForm({ ...policyForm, channel: e.target.value })} className="text-sm border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 bg-slate-50 dark:bg-slate-900">
                  <option value="email">Email</option>
                  <option value="webhook">Webhook</option>
                  <option value="slack">Slack</option>
                  <option value="teams">Teams</option>
                </select>
                <input placeholder="Secondary contacts (comma-separated emails)" value={policyForm.secondary_contacts} onChange={(e) => setPolicyForm({ ...policyForm, secondary_contacts: e.target.value })} className="col-span-2 text-sm border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 bg-slate-50 dark:bg-slate-900" />
                {policyForm.channel !== "email" && (
                  <input placeholder="Webhook URL" value={policyForm.webhook_url} onChange={(e) => setPolicyForm({ ...policyForm, webhook_url: e.target.value })} className="col-span-full text-sm border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 bg-slate-50 dark:bg-slate-900" />
                )}
              </div>
              <div className="flex items-center justify-end gap-2 mt-4">
                <button onClick={resetPolicyForm} className="text-xs font-bold text-slate-500 px-3 py-1.5">Cancel</button>
                <button onClick={handleSavePolicy} disabled={!policyForm.name || !policyForm.unack_minutes} className="text-xs font-bold uppercase tracking-wider text-white bg-brandblue hover:bg-navy px-4 py-2 rounded-lg shadow-sm disabled:opacity-40">{editingPolicy ? "Update" : "Create"}</button>
              </div>
            </div>
          )}

          {/* Policy List */}
          <div className="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 shadow-sm overflow-x-auto">
            {policiesLoading ? (
              <div className="p-8 text-center text-sm text-slate-400">Loading…</div>
            ) : policies.length === 0 ? (
              <div className="p-8 text-center text-sm text-slate-400">No escalation policies yet — unacknowledged alerts won't be escalated to a secondary contact.</div>
            ) : (
              <table className="w-full text-sm min-w-[700px]">
                <thead className="bg-slate-50 dark:bg-slate-900/50 text-[11px] font-bold uppercase tracking-wider text-slate-400">
                  <tr>
                    <th className="text-left px-4 py-2.5">Name</th>
                    <th className="text-left px-4 py-2.5">Scope</th>
                    <th className="text-left px-4 py-2.5">Unack Threshold</th>
                    <th className="text-left px-4 py-2.5">Repeat</th>
                    <th className="text-left px-4 py-2.5">Channel</th>
                    <th className="text-left px-4 py-2.5">Status</th>
                    <th className="text-right px-4 py-2.5">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 dark:divide-slate-700">
                  {policies.map((p) => (
                    <tr key={p.id}>
                      <td className="px-4 py-2.5">
                        <p className="font-semibold text-navy dark:text-white">{p.name}</p>
                        {p.description && <p className="text-xs text-slate-400">{p.description}</p>}
                      </td>
                      <td className="px-4 py-2.5 capitalize">{p.severity_scope}</td>
                      <td className="px-4 py-2.5">{p.unack_minutes}m</td>
                      <td className="px-4 py-2.5">{p.repeat_minutes ? `every ${p.repeat_minutes}m` : "one-shot"}</td>
                      <td className="px-4 py-2.5 capitalize">{p.channel}</td>
                      <td className="px-4 py-2.5">
                        <button onClick={() => handleTogglePolicy(p.id)} className={`text-xs font-bold uppercase px-2.5 py-1 rounded-lg transition-colors ${p.enabled ? "text-risklow bg-green-50 hover:bg-green-100" : "text-slate-400 bg-slate-100 hover:bg-slate-200"}`}>
                          {p.enabled ? "Enabled" : "Disabled"}
                        </button>
                      </td>
                      <td className="px-4 py-2.5 text-right space-x-3">
                        <button onClick={() => { setEditingPolicy(p); setPolicyForm({ name: p.name, description: p.description || "", severity_scope: p.severity_scope, unack_minutes: String(p.unack_minutes), repeat_minutes: p.repeat_minutes ? String(p.repeat_minutes) : "", secondary_contacts: p.secondary_contacts || "", channel: p.channel, webhook_url: p.webhook_url || "" }); setShowPolicyForm(true); }} className="text-xs text-brandblue hover:text-navy font-medium">Edit</button>
                        <button onClick={() => handleDeletePolicy(p.id)} className="text-xs text-riskcrit hover:text-red-800 font-medium">Delete</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          {/* Escalation Log */}
          <div>
            <h2 className="text-sm font-bold text-navy dark:text-white uppercase tracking-wider mb-3">Escalation Log</h2>
            <div className="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 shadow-sm overflow-x-auto">
              {escalationLog.length === 0 ? (
                <div className="p-8 text-center text-sm text-slate-400">Nothing has been escalated yet.</div>
              ) : (
                <table className="w-full text-sm min-w-[500px]">
                  <thead className="bg-slate-50 dark:bg-slate-900/50 text-[11px] font-bold uppercase tracking-wider text-slate-400">
                    <tr>
                      <th className="text-left px-4 py-2.5">Alert</th>
                      <th className="text-left px-4 py-2.5">Policy</th>
                      <th className="text-left px-4 py-2.5">Times</th>
                      <th className="text-left px-4 py-2.5">Last Escalated</th>
                      <th className="text-left px-4 py-2.5">Status</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100 dark:divide-slate-700">
                    {escalationLog.map((e) => (
                      <tr key={e.id}>
                        <td className="px-4 py-2.5">
                          <p className="font-semibold text-navy dark:text-white">{e.category}</p>
                          <p className="text-xs text-slate-400">{e.message}</p>
                        </td>
                        <td className="px-4 py-2.5">{e.escalation_policy_name || "—"}</td>
                        <td className="px-4 py-2.5">×{e.escalation_count}</td>
                        <td className="px-4 py-2.5">{e.last_escalated_at ? timeAgo(e.last_escalated_at) : "—"}</td>
                        <td className="px-4 py-2.5">{e.acknowledged ? <span className="text-risklow">Acknowledged</span> : <span className="text-riskcrit font-semibold">Still unacked</span>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </div>
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

              <div className="mt-4 border-t border-slate-100 dark:border-slate-700 pt-4">
                <div className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider mb-2">Response actions</div>
                <p className="text-xs text-slate-500 dark:text-slate-500 mb-3">
                  Attach one-tap action buttons to each delivery, alongside the plain alert notification. Slack/Teams render real interactive buttons; Telegram gets an inline keyboard; generic endpoints get an <code>actions</code> array of deep links.
                </p>
                <div className="flex flex-wrap gap-3">
                  {[
                    { id: "acknowledge", label: "Acknowledge" },
                    { id: "escalate", label: "Escalate" },
                    { id: "run_runbook", label: "Run Runbook" },
                  ].map((a) => (
                    <label key={a.id} className="flex items-center gap-2 text-sm bg-slate-50 dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded-lg px-3 py-1.5 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={webhookForm.include_actions.includes(a.id)}
                        onChange={() => toggleWebhookAction(a.id)}
                        className="rounded"
                      />
                      {a.label}
                    </label>
                  ))}
                </div>
                {webhookForm.include_actions.includes("run_runbook") && (
                  <select
                    value={webhookForm.default_runbook_id}
                    onChange={(e) => setWebhookForm({ ...webhookForm, default_runbook_id: e.target.value })}
                    className="mt-3 text-sm border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 bg-slate-50 dark:bg-slate-900 w-full md:w-1/2"
                  >
                    <option value="">Default: link to Runbooks page</option>
                    {runbooks.map((rb) => (
                      <option key={rb.id} value={rb.id}>{rb.title}</option>
                    ))}
                  </select>
                )}
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
                      {wh.include_actions && wh.include_actions.length > 0 && (
                        <div className="flex flex-wrap gap-1 mt-1.5">
                          {wh.include_actions.map((a) => (
                            <span key={a} className="text-[10px] font-bold uppercase tracking-wider text-brandblue bg-brandblue/10 px-2 py-0.5 rounded-full">
                              {a === "run_runbook" ? "Run Runbook" : a === "acknowledge" ? "Acknowledge" : "Escalate"}
                            </span>
                          ))}
                        </div>
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
                        onClick={() => toggleDeliveries(wh.id)}
                        className={`text-xs font-medium px-3 py-1.5 rounded-lg transition-colors ${
                          deliveriesOpenFor === wh.id
                            ? "text-white bg-navy"
                            : "text-slate-500 dark:text-slate-400 bg-slate-100 dark:bg-slate-700 hover:bg-slate-200 dark:hover:bg-slate-600"
                        }`}
                      >
                        {deliveriesOpenFor === wh.id ? "Hide Deliveries" : "Deliveries"}
                      </button>
                      <button
                        onClick={() => {
                          setEditingWebhook(wh);
                          setWebhookForm({
                            name: wh.name, url: wh.url, webhook_type: wh.webhook_type,
                            secret: wh.secret || "", telegram_chat_id: wh.telegram_chat_id || "",
                            events: wh.events?.join(", ") || "",
                            include_actions: wh.include_actions || [],
                            default_runbook_id: wh.default_runbook_id || "",
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

                  {/* Delivery Log */}
                  {deliveriesOpenFor === wh.id && (
                    <div className="mt-4 border-t border-slate-100 dark:border-slate-700 pt-3">
                      {deliveriesLoading === wh.id ? (
                        <p className="text-xs text-slate-400">Loading delivery history…</p>
                      ) : (deliveries[wh.id] || []).length === 0 ? (
                        <p className="text-xs text-slate-400">No delivery attempts logged yet.</p>
                      ) : (
                        <div className="space-y-2">
                          {(deliveries[wh.id] || []).map((d) => (
                            <div
                              key={d.id}
                              className="flex items-start justify-between gap-3 text-xs bg-slate-50 dark:bg-slate-900 rounded-lg px-3 py-2"
                            >
                              <div className="min-w-0">
                                <div className="flex items-center gap-2 flex-wrap">
                                  <span className={`font-bold px-1.5 py-0.5 rounded ${d.success ? "text-risklow bg-green-50" : "text-riskcrit bg-red-50"}`}>
                                    {d.success ? "✅ Delivered" : "❌ Failed"}
                                  </span>
                                  <span className="font-medium text-navy dark:text-white">{d.event}</span>
                                  {d.status_code != null && <span className="text-slate-400">HTTP {d.status_code}</span>}
                                  {d.is_retry && <span className="text-slate-400 italic">retry{d.retried_by ? ` by ${d.retried_by}` : ""}</span>}
                                </div>
                                <p className="text-slate-400 dark:text-slate-500 mt-0.5">
                                  {new Date(d.attempted_at).toLocaleString()}
                                </p>
                                {d.error && <p className="text-riskcrit mt-0.5 truncate">{d.error}</p>}
                              </div>
                              {!d.success && (
                                <button
                                  onClick={() => handleRetryDelivery(wh.id, d.id)}
                                  disabled={retryingId === d.id}
                                  className="shrink-0 text-xs font-bold uppercase tracking-wider text-white bg-brandblue hover:bg-navy px-2.5 py-1 rounded-lg disabled:opacity-40"
                                >
                                  {retryingId === d.id ? "Retrying…" : "Retry"}
                                </button>
                              )}
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* ===== PUSH TAB ===== */}
      {tab === "push" && (
        <div className="mt-4 bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 shadow-sm p-5">
          <div className="flex items-start justify-between gap-4 mb-4">
            <div>
              <h3 className="text-sm font-bold text-navy dark:text-white">Push Notifications</h3>
              <p className="text-xs text-slate-500 dark:text-slate-400 mt-1">
                Alerts are pushed to every enabled device below via ntfy, Pushover, or browser Web Push --
                critical alerts always go out; devices opt in individually to warning/info as well.
              </p>
            </div>
            <button
              onClick={() => navigate("/push-settings")}
              className="shrink-0 text-xs font-bold uppercase tracking-wider text-white bg-brandblue hover:bg-navy px-3 py-2 rounded-lg"
            >
              + Add / Manage Devices
            </button>
          </div>

          {pushLoading ? (
            <p className="text-xs text-slate-500 dark:text-slate-400">Loading…</p>
          ) : pushSubs.length === 0 ? (
            <div className="text-center py-8">
              <p className="text-sm text-slate-500 dark:text-slate-400">
                No devices registered yet -- critical alerts won't reach a phone until one is added.
              </p>
              <button
                onClick={() => navigate("/push-settings")}
                className="mt-3 text-xs font-bold uppercase tracking-wider text-white bg-brandblue hover:bg-navy px-3 py-2 rounded-lg"
              >
                Register a Device
              </button>
            </div>
          ) : (
            <div className="space-y-2">
              {pushSubs.map((sub) => (
                <div
                  key={sub.id}
                  className="flex items-center justify-between gap-3 p-3 rounded-lg border border-slate-200 dark:border-slate-700"
                >
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-bold text-navy dark:text-white truncate">{sub.label}</span>
                      <span className="text-[10px] font-bold uppercase tracking-wider text-slate-400 bg-slate-100 dark:bg-slate-700 px-1.5 py-0.5 rounded">
                        {sub.provider}
                      </span>
                      {!sub.enabled && (
                        <span className="text-[10px] font-bold uppercase tracking-wider text-riskcrit bg-red-50 px-1.5 py-0.5 rounded">
                          Disabled
                        </span>
                      )}
                    </div>
                    <p className="text-[11px] text-slate-500 dark:text-slate-400 mt-0.5">
                      {sub.include_non_critical ? "Critical, warning & info" : "Critical alerts only"} · Last push:{" "}
                      {sub.last_pushed_at ? new Date(sub.last_pushed_at).toLocaleString() : "Never"}
                    </p>
                  </div>
                  <button
                    onClick={() => handleTestPush(sub.id)}
                    disabled={pushTestingId === sub.id || !sub.enabled}
                    className="shrink-0 text-xs font-bold uppercase tracking-wider text-brandblue border border-brandblue px-2.5 py-1 rounded-lg disabled:opacity-40"
                  >
                    {pushTestingId === sub.id ? "Sending…" : "Test"}
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}