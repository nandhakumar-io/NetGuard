import React, { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../lib/api";
import SavedViews from "../components/SavedViews";
import {
  Device,
  Snapshot,
  RunningConfig,
  StartupConfig,
  GoldenConfig,
  BackupHistoryEntry,
  CompareConfigResponse,
  DeviceHealthSummary,
  DeviceMetric,
  Alert as AlertRow,
  ProtocolOperationRecord,
  DeploymentRecord,
  InterfacesResponse,
  DeviceDiscoveryResult,
  HealthCheckCatalogEntry,
  FleetHealthSummary,
  RollbackPreviewResponse,
  RollbackSection,
  PartialRollbackPreviewResponse,
  RetentionPolicyResponse,
  DeviceLifecycleState,
  DeviceCsvImportResult,
} from "../lib/types";
import { useAuth } from "../lib/auth";
import { useToast, errorMessage } from "../lib/toast";
import { useConfirm } from "../lib/confirm";
import ConfigDiff from "../components/ConfigDiff";
import ConfigViewer from "../components/ConfigViewer";
import { WebTerminal } from "../components/WebTerminal";
import SnmpCredentialsModal from "../components/SnmpCredentialsModal";
import SshCredentialsModal from "../components/SshCredentialsModal";
import BulkRotateCredentialsModal from "../components/BulkRotateCredentialModal";

const HEALTH_COLOR_STYLES: Record<string, { dot: string; text: string; bg: string }> = {
  green: { dot: "bg-risklow", text: "text-risklow", bg: "bg-green-50 dark:bg-green-950/40 border-green-200 dark:border-green-800" },
  yellow: { dot: "bg-riskmed", text: "text-riskmed", bg: "bg-amber-50 dark:bg-amber-950/40 border-amber-200 dark:border-amber-800" },
  red: { dot: "bg-riskcrit", text: "text-riskcrit", bg: "bg-red-50 dark:bg-red-950/40 border-red-200 dark:border-red-800" },
  // Reachable, but the poll resolved none of the actual health OIDs --
  // see snmp_service.compute_health_score. Distinct from "unknown"
  // (never polled at all / device.status unknown) even though they
  // render the same today, so a future pass can tell them apart.
  gray: { dot: "bg-slate-300 dark:bg-slate-600", text: "text-slate-400 dark:text-slate-500", bg: "bg-slate-50 dark:bg-slate-900 border-slate-200 dark:border-slate-700" },
  unknown: { dot: "bg-slate-300 dark:bg-slate-600", text: "text-slate-400 dark:text-slate-500", bg: "bg-slate-50 dark:bg-slate-900 border-slate-200 dark:border-slate-700" },
};

const ALERT_SEVERITY_STYLES: Record<string, { text: string; bg: string; icon: string }> = {
  critical: { text: "text-riskcrit", bg: "bg-red-50 dark:bg-red-950/40 border-red-200 dark:border-red-800", icon: "🚨" },
  warning: { text: "text-riskmed", bg: "bg-amber-50 dark:bg-amber-950/40 border-amber-200 dark:border-amber-800", icon: "⚠️" },
  info: { text: "text-brandblue", bg: "bg-blue-50 dark:bg-blue-950/40 border-blue-200 dark:border-blue-800", icon: "ℹ️" },
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
  unknown: "bg-slate-300 dark:bg-slate-600",
};

const emptyForm = {
  hostname: "",
  ip_address: "",
  vendor: "cisco",
  site: "",
  device_role: "",
  data_center: "",
  rack: "",
  rack_position: "",
  ssh_username: "",
  ssh_credential_ref: "",
  supports_snmp: false,
  snmp_version: "v2c",
  snmp_port: "161",
  snmp_community_ref: "",
  snmp_username: "",
  snmp_security_level: "authPriv",
  snmp_auth_protocol: "SHA",
  snmp_priv_protocol: "AES128",
  supports_netconf: false,
  netconf_port: "830",
  netconf_use_lock: true,
  supports_restconf: false,
  restconf_url: "",
};

const TAB_NAMES = [
  "Overview",
  "Health",
  "Configuration",
  "Interfaces",
  "Discovery",
  "Backups",
  "Drift",
  "Alerts",
  "Protocol Operations",
  "Deployment History",
] as const;
type TabName = typeof TAB_NAMES[number];

// --- Subcomponents for Device Details Tabs ---

function DiscoveryTable({ columns, rows, empty }: { columns: string[]; rows: string[][]; empty: string }) {
  if (rows.length === 0) {
    return <p className="text-xs text-slate-400 dark:text-slate-500 italic py-4">{empty}</p>;
  }
  return (
    <div className="overflow-x-auto border border-slate-200 dark:border-slate-700 rounded-lg">
      <table className="w-full text-xs">
        <thead className="bg-slate-100 dark:bg-slate-700">
          <tr>
            {columns.map((c) => (
              <th key={c} className="text-left px-3 py-2 font-bold text-slate-500 dark:text-slate-400 uppercase">
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
          {rows.map((row, idx) => (
            <tr key={idx} className="bg-white dark:bg-slate-800">
              {row.map((cell, cidx) => (
                <td
                  key={cidx}
                  className={`px-3 py-1.5 ${
                    cidx === 0
                      ? "font-mono font-semibold text-navy dark:text-white"
                      : "text-slate-600 dark:text-slate-300 font-mono"
                  }`}
                >
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function DeviceInlineDetails({
  device,
  canManage,
  onQueueRollback,
  onDeviceUpdated,
}: {
  device: Device;
  canManage: boolean;
  onQueueRollback: (snapshot: Snapshot, reason: string) => void;
  onDeviceUpdated: (updated: Device) => void;
}) {
  const toast = useToast();
  const confirm = useConfirm();
  const [activeTab, setActiveTab] = useState<TabName>("Overview");
  const [showSnmpCredsModal, setShowSnmpCredsModal] = useState(false);
  const [showSshCredsModal, setShowSshCredsModal] = useState(false);

  // Configuration tab state
  const [running, setRunning] = useState<RunningConfig | null>(null);
  const [startup, setStartup] = useState<StartupConfig | null>(null);
  const [golden, setGolden] = useState<GoldenConfig | null>(null);
  const [goldenLoaded, setGoldenLoaded] = useState(false);
  const [goldenBusy, setGoldenBusy] = useState(false);
  const [goldenNotice, setGoldenNotice] = useState<string | null>(null);
  const [configLoading, setConfigLoading] = useState(false);
  const [configError, setConfigError] = useState<string | null>(null);

  // Backups tab state
  const [history, setHistory] = useState<BackupHistoryEntry[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [backingUp, setBackingUp] = useState(false);
  const [backupNotice, setBackupNotice] = useState<string | null>(null);

  // Snapshot retention: the policy that governs how long backups above
  // stick around, shown alongside the history list so retention is
  // visible, not just something that quietly happens overnight.
  const [retention, setRetention] = useState<RetentionPolicyResponse | null>(null);
  const [retentionLoading, setRetentionLoading] = useState(false);

  // Compare sub-state inside backups (simplified for inline view)
  const [baseId, setBaseId] = useState<string>("");
  const [targetId, setTargetId] = useState<string>("");
  const [compareResult, setCompareResult] = useState<CompareConfigResponse | null>(null);
  const [comparing, setComparing] = useState(false);

  // Health tab state (shared with Interfaces tab for the fleet-aggregate
  // utilization/error chart -- interface_utilization_pct/interface_errors
  // are columns on the same DeviceMetric row, there's no separate
  // per-interface table for that part of the schema).
  const [health, setHealth] = useState<DeviceHealthSummary | null>(null);
  const [metricHistory, setMetricHistory] = useState<DeviceMetric[]>([]);
  const [healthLoading, setHealthLoading] = useState(false);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [polling, setPolling] = useState(false);

  // Post-deployment verification check selection
  const [checkCatalog, setCheckCatalog] = useState<HealthCheckCatalogEntry[]>([]);
  const [checkCatalogLoading, setCheckCatalogLoading] = useState(false);
  // device.enabled_health_checks === null/empty means "run the full suite"
  // (see pipeline_service.py), NOT "nothing selected" -- but at mount time
  // the catalog hasn't loaded yet, so there's no "full suite" to point at.
  // Left empty here and reconciled in the effect below once the catalog
  // is available; this used to just render `[]` in the null case, which
  // made a device configured to run everything look like every checkbox
  // had been silently unchecked after navigating away and back.
  const [selectedChecks, setSelectedChecks] = useState<Set<string>>(
    new Set(device.enabled_health_checks && device.enabled_health_checks.length > 0 ? device.enabled_health_checks : [])
  );
  const [checksDirty, setChecksDirty] = useState(false);
  const [checksSaving, setChecksSaving] = useState(false);
  const [checksNotice, setChecksNotice] = useState<string | null>(null);

  // Device Role (compliance baseline template selector -- see Drift page)
  const [editingRole, setEditingRole] = useState(false);
  const [roleValue, setRoleValue] = useState(device.device_role || "");
  const [roleSaving, setRoleSaving] = useState(false);

  const saveDeviceRole = async () => {
    setRoleSaving(true);
    try {
      const res = await api.patch<Device>(`/devices/${device.id}`, { device_role: roleValue || null });
      onDeviceUpdated(res.data);
      setEditingRole(false);
    } catch {
      // best-effort UI; leave the field open on failure so the user can retry
    } finally {
      setRoleSaving(false);
    }
  };

  useEffect(() => {
    if (activeTab !== "Health" || checkCatalog.length > 0 || checkCatalogLoading) return;
    setCheckCatalogLoading(true);
    api
      .get<HealthCheckCatalogEntry[]>("/devices/health-checks/catalog")
      .then((res) => setCheckCatalog(res.data))
      .catch(() => setCheckCatalog([]))
      .finally(() => setCheckCatalogLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab]);

  // Now that the catalog is known, resolve the "null means everything"
  // case into an actual set of names so the checkboxes render as checked
  // instead of unchecked. Only when the device doesn't have an explicit
  // subset saved and the operator hasn't started editing (checksDirty) --
  // otherwise this would clobber in-progress unsaved changes every time
  // checkCatalog's array identity changes.
  useEffect(() => {
    if (checkCatalog.length === 0 || checksDirty) return;
    if (device.enabled_health_checks && device.enabled_health_checks.length > 0) return;
    setSelectedChecks(new Set(checkCatalog.map((c) => c.name)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [checkCatalog, device.enabled_health_checks]);

  const toggleCheck = (name: string) => {
    setSelectedChecks((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
    setChecksDirty(true);
    setChecksNotice(null);
  };

  const selectAllChecks = () => {
    setSelectedChecks(new Set(checkCatalog.map((c) => c.name)));
    setChecksDirty(true);
    setChecksNotice(null);
  };

  const saveEnabledChecks = async () => {
    setChecksSaving(true);
    setChecksNotice(null);
    try {
      const allSelected = checkCatalog.length > 0 && selectedChecks.size === checkCatalog.length;
      const payload = allSelected ? null : Array.from(selectedChecks);
      const res = await api.patch<Device>(`/devices/${device.id}`, { enabled_health_checks: payload });
      onDeviceUpdated(res.data);
      setChecksDirty(false);
      setChecksNotice("Saved. Future deployments to this device will only run the selected checks.");
    } catch (err: any) {
      setChecksNotice(err?.response?.data?.detail || "Failed to save.");
    } finally {
      setChecksSaving(false);
    }
  };

  // Interfaces tab: real per-interface admin/oper status + IPs, read live
  // from the device via GET /devices/{id}/config/interfaces (NETCONF/
  // RESTCONF/SSH-NAPALM, normalized server-side). Separate from the
  // aggregate SNMP chart above -- this is live-fetched on demand, not
  // polled/stored history.
  const [interfaces, setInterfaces] = useState<InterfacesResponse | null>(null);
  const [interfacesLoading, setInterfacesLoading] = useState(false);
  const [interfacesError, setInterfacesError] = useState<string | null>(null);
  // Toggling "alert on down" is per-interface and fires its own PUT, so it
  // tracks its own in-flight if_descr rather than reusing interfacesLoading.
  const [alertConfigSaving, setAlertConfigSaving] = useState<string | null>(null);

  // Deliberately NOT auto-fetched on tab open (unlike Health/Config) --
  // reading interface status means a live SNMP/NETCONF/SSH round-trip to
  // the device, and firing that on every tab click across a fleet view is
  // needless load on the device and the poller both. The operator opts in
  // with the "Run Discovery" button below; once fetched it's cached in
  // state for the rest of this session same as everything else here.
  const loadInterfaces = () => {
    setInterfacesLoading(true);
    setInterfacesError(null);
    api
      .get<InterfacesResponse>(`/devices/${device.id}/config/interfaces`)
      .then((res) => setInterfaces(res.data))
      .catch((err: any) => setInterfacesError(err?.response?.data?.detail || "Failed to read interface status."))
      .finally(() => setInterfacesLoading(false));
  };

  const setInterfaceAlertsEnabled = (ifName: string, enabled: boolean) => {
    setAlertConfigSaving(ifName);
    api
      .put(`/devices/${device.id}/config/interfaces/${encodeURIComponent(ifName)}/alert-config`, { enabled })
      .then(() => {
        setInterfaces((prev) =>
          prev
            ? { ...prev, interfaces: prev.interfaces.map((i) => (i.name === ifName ? { ...i, alerts_enabled: enabled } : i)) }
            : prev
        );
      })
      .catch((err: any) => setInterfacesError(err?.response?.data?.detail || "Failed to update alert setting."))
      .finally(() => setAlertConfigSaving(null));
  };

  // Discovery tab: on-demand SNMP discovery (hostname, ARP table, routing
  // table, LLDP/CDP neighbors, chassis inventory) from GET
  // /devices/{id}/discovery. Also persists LLDP/CDP results server-side
  // as DiscoveredNeighbor rows, which the Topology page picks up as
  // confirmed (non-inferred) links -- so running Discovery here is what
  // makes real links show up on the Topology graph.
  const [discovery, setDiscovery] = useState<DeviceDiscoveryResult | null>(null);
  const [discoveryLoading, setDiscoveryLoading] = useState(false);
  const [discoveryError, setDiscoveryError] = useState<string | null>(null);
  const [discoverySubTab, setDiscoverySubTab] = useState<"lldp" | "cdp" | "arp" | "routes" | "inventory">("lldp");

  const loadDiscovery = () => {
    setDiscoveryLoading(true);
    setDiscoveryError(null);
    api
      .get<DeviceDiscoveryResult>(`/devices/${device.id}/discovery`)
      .then((res) => setDiscovery(res.data))
      .catch((err: any) =>
        setDiscoveryError(err?.response?.data?.detail || "SNMP discovery failed for this device.")
      )
      .finally(() => setDiscoveryLoading(false));
  };

  // Alerts tab state
  const [deviceAlerts, setDeviceAlerts] = useState<AlertRow[]>([]);
  const [alertsLoading, setAlertsLoading] = useState(false);
  const [alertsError, setAlertsError] = useState<string | null>(null);
  const [ackingId, setAckingId] = useState<string | null>(null);

  // Protocol Operations tab state
  const [protocolOps, setProtocolOps] = useState<ProtocolOperationRecord[]>([]);
  const [protocolOpsLoading, setProtocolOpsLoading] = useState(false);
  const [protocolOpsError, setProtocolOpsError] = useState<string | null>(null);

  // Deployment History tab state
  const [deployments, setDeployments] = useState<DeploymentRecord[]>([]);
  const [deploymentsLoading, setDeploymentsLoading] = useState(false);
  const [deploymentsError, setDeploymentsError] = useState<string | null>(null);

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

  const [clearingAlerts, setClearingAlerts] = useState(false);
  const activeDeviceAlertCount = deviceAlerts.filter((a) => !a.resolved).length;

  const clearDeviceAlerts = async () => {
    if (activeDeviceAlertCount === 0) return;
    if (
      !(await confirm(
        `Permanently delete all ${deviceAlerts.length} alert${deviceAlerts.length === 1 ? "" : "s"} for ${device.hostname}? This removes them entirely -- it cannot be undone.`,
        { confirmLabel: "Delete all" }
      ))
    ) {
      return;
    }
    setClearingAlerts(true);
    try {
      await api.delete(`/alerts/clear?device_id=${device.id}`);
      loadAlerts();
    } catch {
      // leave alerts as-is; button re-enables so the user can retry
    } finally {
      setClearingAlerts(false);
    }
  };

  const loadProtocolOps = () => {
    setProtocolOpsLoading(true);
    setProtocolOpsError(null);
    api
      .get<ProtocolOperationRecord[]>(`/devices/${device.id}/protocol-operations?limit=50`)
      .then((res) => setProtocolOps(res.data))
      .catch(() => setProtocolOpsError("Failed to load protocol operations for this device."))
      .finally(() => setProtocolOpsLoading(false));
  };

  const loadDeployments = () => {
    setDeploymentsLoading(true);
    setDeploymentsError(null);
    api
      .get<DeploymentRecord[]>(`/deployments?device_id=${device.id}`)
      .then((res) => setDeployments(res.data))
      .catch(() => setDeploymentsError("Failed to load deployment history for this device."))
      .finally(() => setDeploymentsLoading(false));
  };

  useEffect(() => {
    if ((activeTab === "Overview" || activeTab === "Health" || activeTab === "Interfaces") && !health && !healthLoading) {
      loadHealth();
    }
    // Interfaces tab auto-fetches live per-interface status on first visit,
    // same as Health -- this is a single protocol read (NETCONF/RESTCONF/SSH),
    // not a full SNMP discovery sweep, so there's no meaningful polling
    // overhead in doing it automatically.
    if (activeTab === "Interfaces" && !interfaces && !interfacesLoading && !interfacesError) {
      loadInterfaces();
    }
    // Discovery tab also auto-fetches on first visit (LLDP/CDP/ARP/routes/
    // inventory) -- a single on-demand SNMP poll of this device, not a
    // recurring background job, so it's safe to fire once when the tab
    // opens rather than making the user click Run Discovery first.
    if (activeTab === "Discovery" && !discovery && !discoveryLoading && !discoveryError) {
      loadDiscovery();
    }
    if (activeTab === "Alerts" && deviceAlerts.length === 0 && !alertsLoading) {
      loadAlerts();
    }
    if (activeTab === "Protocol Operations" && protocolOps.length === 0 && !protocolOpsLoading) {
      loadProtocolOps();
    }
    if (activeTab === "Deployment History" && deployments.length === 0 && !deploymentsLoading) {
      loadDeployments();
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
    if (activeTab === "Configuration" && !goldenLoaded) {
      api
        .get<GoldenConfig>(`/devices/${device.id}/config/golden-config`)
        .then((res) => setGolden(res.data))
        .catch(() => setGolden(null))
        .finally(() => setGoldenLoaded(true));
    }

    if (activeTab === "Backups" && history.length === 0) {
      loadBackupHistory();
    }
    if (activeTab === "Backups" && !retention) {
      loadRetentionPolicy();
    }
  }, [activeTab, device.id, running, history.length]);


  const loadBackupHistory = () => {
    setHistoryLoading(true);
    setHistoryError(null);
    api
      .get<BackupHistoryEntry[]>(`/devices/${device.id}/config/backups`)
      .then((res) => setHistory(res.data))
      .catch(() => setHistoryError("Failed to load backup history."))
      .finally(() => setHistoryLoading(false));
  };

  const loadRetentionPolicy = () => {
    setRetentionLoading(true);
    api
      .get<RetentionPolicyResponse>(`/devices/${device.id}/config/retention`)
      .then((res) => setRetention(res.data))
      .catch(() => {})
      .finally(() => setRetentionLoading(false));
  };


  const runBackupNow = async () => {
    setBackingUp(true);
    setHistoryError(null);
    setBackupNotice(null);
    try {
      const res = await api.post(`/devices/${device.id}/config/backup`, {});
      setBackupNotice(res.data.message || "Backup completed.");
      loadBackupHistory();
    } catch (err: any) {
      setHistoryError(err?.response?.data?.detail || "Failed to back up configuration.");
    } finally {
      setBackingUp(false);
    }
  };

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

  const setRunningAsGolden = async () => {
    if (!running?.config) return;
    setGoldenBusy(true);
    setGoldenNotice(null);
    try {
      const res = await api.put<GoldenConfig>(`/devices/${device.id}/config/golden-config`, {
        config: running.config,
      });
      setGolden(res.data);
      setGoldenNotice("Current running config approved as the golden baseline.");
    } catch (err: any) {
      setGoldenNotice(err?.response?.data?.detail || "Failed to set golden config.");
    } finally {
      setGoldenBusy(false);
    }
  };

  const clearGolden = async () => {
    if (!(await confirm("Clear the golden config baseline for this device?"))) return;
    setGoldenBusy(true);
    setGoldenNotice(null);
    try {
      await api.delete(`/devices/${device.id}/config/golden-config`);
      setGolden(null);
      setGoldenNotice("Golden config cleared.");
    } catch (err: any) {
      setGoldenNotice(err?.response?.data?.detail || "Failed to clear golden config.");
    } finally {
      setGoldenBusy(false);
    }
  };

  return (
    <div className="flex flex-col bg-slate-50 dark:bg-slate-900 min-h-[400px]">
      <div className="flex border-b border-slate-200 dark:border-slate-700 overflow-x-auto hide-scrollbar bg-white dark:bg-slate-800">
        {TAB_NAMES.map((tab) => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={`whitespace-nowrap px-4 py-3 text-xs font-semibold uppercase tracking-wider transition-colors border-b-2 ${
              activeTab === tab
                ? "border-brandblue text-brandblue"
                : "border-transparent text-slate-500 dark:text-slate-400 hover:text-navy dark:hover:text-white hover:bg-slate-50 dark:hover:bg-slate-700"
            }`}
          >
            {tab}
          </button>
        ))}
      </div>

      <div className="p-5 flex-1">
        {activeTab === "Overview" && (
          <div className="grid grid-cols-2 gap-4 max-w-lg">
            {(device.is_eos || device.is_eol) && (
              <div className="col-span-2 flex items-start gap-2 bg-red-50 dark:bg-red-950/30 border border-red-200 dark:border-red-900 rounded-lg px-3 py-2">
                <span className="text-lg leading-none">🕰️</span>
                <div className="text-xs text-red-700 dark:text-red-300">
                  <p className="font-bold uppercase tracking-wide">
                    {device.is_eol ? "End-of-Life" : "End-of-Support"} firmware/hardware
                    {device.eol_platform_label ? ` -- ${device.eol_platform_label}` : ""}
                  </p>
                  <p className="mt-0.5">
                    {device.is_eos && device.eos_date && `EOS since ${device.eos_date}. `}
                    {device.is_eol && device.eol_date && `EOL since ${device.eol_date}. `}
                    {device.eol_note}
                  </p>
                </div>
              </div>
            )}
            <div>
              <p className="text-xs text-slate-500 dark:text-slate-400">Hostname</p>
              <p className="font-medium text-navy dark:text-white">{device.hostname}</p>
            </div>
            <div>
              <p className="text-xs text-slate-500 dark:text-slate-400">IP Address</p>
              <p className="font-mono text-sm text-navy dark:text-white">{device.ip_address}</p>
            </div>
            <div>
              <p className="text-xs text-slate-500 dark:text-slate-400">Vendor</p>
              <p className="font-medium capitalize text-navy dark:text-white">{device.vendor}</p>
            </div>
            <div>
              <p className="text-xs text-slate-500 dark:text-slate-400">Site / Location</p>
              <p className="font-medium text-navy dark:text-white">{device.site || "Unknown"}</p>
            </div>
            <div>
              <p className="text-xs text-slate-500 dark:text-slate-400">
                Device Role <span className="normal-case font-normal">(compliance baseline)</span>
              </p>
              {editingRole ? (
                <div className="flex items-center gap-2 mt-1">
                  <input
                    autoFocus
                    list="device-role-options-detail"
                    className="border border-slate-300 dark:border-slate-600 rounded-lg px-2 py-1 text-sm w-32 bg-white dark:bg-slate-800"
                    value={roleValue}
                    onChange={(e) => setRoleValue(e.target.value)}
                    onKeyDown={(e) => e.key === "Enter" && saveDeviceRole()}
                  />
                  <datalist id="device-role-options-detail">
                    <option value="core" />
                    <option value="distribution" />
                    <option value="access" />
                    <option value="edge-firewall" />
                    <option value="wan-edge" />
                  </datalist>
                  <button
                    onClick={saveDeviceRole}
                    disabled={roleSaving}
                    className="text-xs font-bold uppercase text-brandblue disabled:opacity-50"
                  >
                    {roleSaving ? "…" : "Save"}
                  </button>
                  <button
                    onClick={() => {
                      setRoleValue(device.device_role || "");
                      setEditingRole(false);
                    }}
                    className="text-xs font-bold uppercase text-slate-400"
                  >
                    Cancel
                  </button>
                </div>
              ) : (
                <p className="font-medium text-navy dark:text-white flex items-center gap-2">
                  {device.device_role || "Not set"}
                  {canManage && (
                    <button
                      onClick={() => setEditingRole(true)}
                      className="text-[10px] font-bold uppercase tracking-wider text-slate-400 hover:text-brandblue"
                    >
                      Edit
                    </button>
                  )}
                </p>
              )}
            </div>
            <div>
              <p className="text-xs text-slate-500 dark:text-slate-400">Authentication</p>
              <div className="flex items-center gap-2 mt-1">
                <p className="font-mono text-xs text-slate-600 dark:text-slate-300 bg-slate-100 dark:bg-slate-700 px-2 py-1 rounded w-fit">
                  {device.ssh_username || "none"} : {device.ssh_credentials_configured ? "***" : "not set"}
                </p>
                {canManage && (
                  <button
                    onClick={() => setShowSshCredsModal(true)}
                    className="text-xs font-bold uppercase tracking-wider text-slate-600 dark:text-slate-300 border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-900 px-2 py-1 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-700 flex items-center gap-1"
                  >
                    🔑 SSH Credentials
                    {device.ssh_credentials_configured ? (
                      <span className="w-1.5 h-1.5 rounded-full bg-risklow" />
                    ) : (
                      <span className="w-1.5 h-1.5 rounded-full bg-riskmed" />
                    )}
                  </button>
                )}
              </div>
            </div>
            <div>
              <p className="text-xs text-slate-500 dark:text-slate-400">System Uptime</p>
              {healthLoading && !health ? (
                <p className="text-slate-400 dark:text-slate-500 italic text-sm">Loading…</p>
              ) : health?.latest_metric?.uptime_seconds != null ? (
                <p className="font-medium text-navy dark:text-white">{formatUptime(health.latest_metric.uptime_seconds)}</p>
              ) : (
                <p className="text-slate-400 dark:text-slate-500 italic text-sm">
                  {device.supports_snmp ? "Waiting for next SNMP poll..." : "SNMP not enabled for this device"}
                </p>
              )}
            </div>
            <div>
              <p className="text-xs text-slate-500 dark:text-slate-400">Platform</p>
              <p className="font-medium text-navy dark:text-white">{device.platform || "—"}</p>
            </div>
            <div>
              <p className="text-xs text-slate-500 dark:text-slate-400">Model</p>
              <p className="font-medium text-navy dark:text-white">{device.model || "—"}</p>
            </div>
            <div>
              <p className="text-xs text-slate-500 dark:text-slate-400">Serial Number</p>
              <p className="font-mono text-xs text-navy dark:text-white">{device.serial_number || "—"}</p>
            </div>
            <div>
              <p className="text-xs text-slate-500 dark:text-slate-400">OS Version</p>
              <p className="font-medium text-navy dark:text-white">{device.os_version || "—"}</p>
            </div>
            <div>
              <p className="text-xs text-slate-500 dark:text-slate-400">Connectivity</p>
              <div className="flex flex-wrap items-center gap-1 mt-1">
                <span className={`text-[10px] font-bold uppercase px-2 py-0.5 rounded-full ${device.supports_snmp ? "bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-400" : "bg-slate-100 dark:bg-slate-700 text-slate-400 dark:text-slate-500"}`}>SNMP</span>
                {canManage ? (
                  <button
                    onClick={async () => {
                      try {
                        const res = await api.patch<Device>(`/devices/${device.id}`, { supports_netconf: !device.supports_netconf, netconf_port: device.netconf_port || 830 });
                        onDeviceUpdated(res.data);
                      } catch {
                        // ignore error
                      }
                    }}
                    title={device.supports_netconf ? "Click to disable NETCONF" : "Click to enable NETCONF"}
                    className={`text-[10px] font-bold uppercase px-2 py-0.5 rounded-full cursor-pointer transition-colors hover:ring-2 hover:ring-brandblue/40 ${device.supports_netconf ? "bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-400" : "bg-slate-100 dark:bg-slate-700 text-slate-400 dark:text-slate-500"}`}
                  >NETCONF</button>
                ) : (
                  <span className={`text-[10px] font-bold uppercase px-2 py-0.5 rounded-full ${device.supports_netconf ? "bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-400" : "bg-slate-100 dark:bg-slate-700 text-slate-400 dark:text-slate-500"}`}>NETCONF</span>
                )}
                {canManage ? (
                  <button
                    onClick={async () => {
                      try {
                        const res = await api.patch<Device>(`/devices/${device.id}`, { supports_restconf: !device.supports_restconf });
                        onDeviceUpdated(res.data);
                      } catch {
                        // ignore error
                      }
                    }}
                    title={device.supports_restconf ? "Click to disable RESTCONF" : "Click to enable RESTCONF"}
                    className={`text-[10px] font-bold uppercase px-2 py-0.5 rounded-full cursor-pointer transition-colors hover:ring-2 hover:ring-brandblue/40 ${device.supports_restconf ? "bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-400" : "bg-slate-100 dark:bg-slate-700 text-slate-400 dark:text-slate-500"}`}
                  >RESTCONF</button>
                ) : (
                  <span className={`text-[10px] font-bold uppercase px-2 py-0.5 rounded-full ${device.supports_restconf ? "bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-400" : "bg-slate-100 dark:bg-slate-700 text-slate-400 dark:text-slate-500"}`}>RESTCONF</span>
                )}
                {canManage && (
                  <button
                    onClick={() => setShowSnmpCredsModal(true)}
                    className="ml-1 text-xs font-bold uppercase tracking-wider text-slate-600 dark:text-slate-300 border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-900 px-2 py-1 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-700 flex items-center gap-1"
                  >
                    📡 SNMP Credentials
                    {device.supports_snmp && device.snmp_credentials_configured ? (
                      <span className="w-1.5 h-1.5 rounded-full bg-risklow" />
                    ) : (
                      <span className="w-1.5 h-1.5 rounded-full bg-riskmed" />
                    )}
                  </button>
                )}
              </div>
            </div>
          </div>
        )}

        {activeTab === "Health" && (
          <div className="flex flex-col gap-5">
            {healthLoading && !health ? (
              <p className="text-xs text-slate-400 dark:text-slate-500">Loading telemetry…</p>
            ) : healthError && !health ? (
              <div className="text-slate-500 dark:text-slate-400 flex flex-col items-center justify-center h-32 opacity-70">
                <div className="text-3xl mb-2">🩺</div>
                <p className="text-sm font-medium">{healthError}</p>
                {!device.supports_snmp && (
                  <p className="text-xs text-slate-400 dark:text-slate-500 mt-1">
                    Enable SNMP monitoring for this device in Devices → Edit to start collecting health telemetry.
                  </p>
                )}
              </div>
            ) : (
              <>
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-3">
                    <span
                      className={`w-3 h-3 rounded-full ${
                        HEALTH_COLOR_STYLES[health?.health_color || "unknown"].dot
                      }`}
                    />
                    <span className="text-2xl font-bold text-navy dark:text-white">
                      {health?.health_score != null ? `${health.health_score}/100` : "No score yet"}
                    </span>
                    <span className={`text-xs font-bold uppercase tracking-wide ${health?.reachable ? "text-risklow" : "text-riskcrit"}`}>
                      {health?.reachable ? "Reachable" : "Unreachable"}
                    </span>
                  </div>
                  {canManage && (
                    <div className="flex items-center gap-2">
                      {device.supports_snmp && (
                        <button
                          onClick={() => setShowSnmpCredsModal(true)}
                          className="text-xs font-bold uppercase tracking-wider text-slate-600 dark:text-slate-300 border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-900 px-3 py-1.5 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-700 flex items-center gap-1"
                        >
                          🔑 Credentials
                          {device.snmp_credentials_configured ? (
                            <span className="w-1.5 h-1.5 rounded-full bg-risklow" />
                          ) : (
                            <span className="w-1.5 h-1.5 rounded-full bg-riskmed" />
                          )}
                        </button>
                      )}
                      <button
                        onClick={pollNow}
                        disabled={polling}
                        className="text-xs font-bold uppercase tracking-wider text-brandblue border border-blue-200 dark:border-blue-800 bg-blue-50 dark:bg-blue-950/40 px-3 py-1.5 rounded-lg hover:bg-blue-100 disabled:opacity-50"
                      >
                        {polling ? "Polling…" : "↻ Poll Now"}
                      </button>
                    </div>
                  )}
                </div>

                {healthError && <p className="text-riskcrit text-xs">{healthError}</p>}

                {health?.latest_metric ? (
                  <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                    {[
                      { label: "CPU", value: health.latest_metric.cpu_utilization_pct, suffix: "%", metric: "cpu" as const },
                      { label: "Memory", value: health.latest_metric.memory_utilization_pct, suffix: "%", metric: "memory" as const },
                      { label: "Temperature", value: health.latest_metric.temperature_celsius, suffix: "°C", metric: "temperature" as const },
                      { label: "Uptime", value: formatUptime(health.latest_metric.uptime_seconds), suffix: "", metric: null },
                      { label: "Fan Status", value: health.latest_metric.fan_status || "unknown", suffix: "", metric: "fan" as const },
                      { label: "Power Supply", value: health.latest_metric.power_supply_status || "unknown", suffix: "", metric: "power" as const },
                      { label: "Interface Util.", value: health.latest_metric.interface_utilization_pct, suffix: "%", metric: "interface" as const },
                      { label: "Interface Errors", value: health.latest_metric.interface_errors, suffix: "", metric: "interface" as const },
                    ].map((m) => {
                      const isStale = m.metric != null && health?.stale_metrics?.includes(m.metric);
                      const lastRead = m.metric ? health?.metric_freshness?.[m.metric] ?? null : null;
                      return (
                        <div
                          key={m.label}
                          className={`bg-white dark:bg-slate-800 border rounded-lg p-3 shadow-sm ${
                            isStale ? "border-amber-300 dark:border-amber-700" : "border-slate-200 dark:border-slate-700"
                          }`}
                        >
                          <p className="text-[10px] uppercase font-bold text-slate-400 dark:text-slate-500 tracking-wider flex items-center gap-1">
                            {m.label}
                            {isStale && (
                              <span
                                title={
                                  lastRead
                                    ? `Last successful read: ${new Date(lastRead).toLocaleString()} (${timeAgo(lastRead)}). Other metrics on this device are more current.`
                                    : "This reading hasn't successfully resolved in a while, even though other metrics on this device are current."
                                }
                                className="text-amber-500 normal-case font-bold"
                              >
                                ⚠ stale
                              </span>
                            )}
                          </p>
                          <p className="text-lg font-bold text-navy dark:text-white capitalize">
                            {m.value === null || m.value === undefined
                              ? "—"
                              : typeof m.value === "number"
                              ? `${Math.round(m.value * 10) / 10}${m.suffix}`
                              : m.value}
                          </p>
                          {lastRead && (
                            <p
                              className={`text-[10px] mt-0.5 ${isStale ? "text-amber-600 dark:text-amber-400 font-semibold" : "text-slate-400 dark:text-slate-500"}`}
                              title={new Date(lastRead).toLocaleString()}
                            >
                              last read {timeAgo(lastRead)}
                            </p>
                          )}
                        </div>
                      );
                    })}
                  </div>
                ) : (
                  <p className="text-xs text-slate-400 dark:text-slate-500 italic">No poll recorded yet.</p>
                )}

                {metricHistory.length > 0 && (
                  <div>
                    <h4 className="text-xs uppercase font-bold text-slate-500 dark:text-slate-400 mb-2 tracking-wider">
                      Last 24h ({metricHistory.length} poll{metricHistory.length === 1 ? "" : "s"})
                    </h4>
                    <div className="max-h-48 overflow-auto border border-slate-200 dark:border-slate-700 rounded-lg">
                      <table className="w-full text-xs">
                        <thead className="bg-slate-100 dark:bg-slate-700 sticky top-0">
                          <tr>
                            <th className="text-left px-3 py-2 font-bold text-slate-500 dark:text-slate-400 uppercase">Polled</th>
                            <th className="text-left px-3 py-2 font-bold text-slate-500 dark:text-slate-400 uppercase">CPU</th>
                            <th className="text-left px-3 py-2 font-bold text-slate-500 dark:text-slate-400 uppercase">Mem</th>
                            <th className="text-left px-3 py-2 font-bold text-slate-500 dark:text-slate-400 uppercase">Temp</th>
                            <th className="text-left px-3 py-2 font-bold text-slate-500 dark:text-slate-400 uppercase">Score</th>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                          {[...metricHistory].reverse().slice(0, 50).map((m) => (
                            <tr key={m.id} className="bg-white dark:bg-slate-800">
                              <td className="px-3 py-1.5 text-slate-500 dark:text-slate-400">{timeAgo(m.polled_at)}</td>
                              <td className="px-3 py-1.5 text-slate-700 dark:text-slate-200">{m.cpu_utilization_pct ?? "—"}</td>
                              <td className="px-3 py-1.5 text-slate-700 dark:text-slate-200">{m.memory_utilization_pct ?? "—"}</td>
                              <td className="px-3 py-1.5 text-slate-700 dark:text-slate-200">{m.temperature_celsius ?? "—"}</td>
                              <td className="px-3 py-1.5 font-bold text-slate-700 dark:text-slate-200">{m.health_score ?? "—"}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}
              </>
            )}

            {/* Deployment Health Checks catalog -- deliberately OUTSIDE the
                telemetry loading/error/success branches above. This picker
                selects which *post-deployment verification* checks to run
                (health_monitor.ALL_CHECKS) and has nothing to do with SNMP
                telemetry, but it used to live inside the "telemetry loaded
                successfully" branch only -- so any device that hadn't been
                polled yet, or had SNMP failing/unconfigured (e.g. a
                manually-added device before its SNMP credentials are set
                up), fell into the healthError-and-no-health branch above
                and lost access to this picker entirely, even though it's
                independent of whether SNMP telemetry is working. */}
            <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg p-5">
              <h4 className="text-sm font-bold text-navy dark:text-white mb-1">Deployment Health Checks</h4>
              <p className="text-xs text-slate-500 dark:text-slate-400 mb-4">
                Select which tests to run during post-deployment verification for this device.
              </p>
              {checkCatalogLoading ? (
                <p className="text-xs text-slate-400">Loading catalog...</p>
              ) : checkCatalog.length === 0 ? (
                <p className="text-xs text-slate-400">No health checks available.</p>
              ) : (
                <div className="space-y-4">
                  <div className="flex flex-wrap gap-2">
                    {checkCatalog.map((c) => (
                      <label key={c.name} className="flex items-start gap-2 bg-slate-50 dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded p-3 cursor-pointer hover:border-brandblue/50 w-full md:w-[calc(50%-0.5rem)]">
                        <input type="checkbox" checked={selectedChecks.has(c.name)} onChange={() => toggleCheck(c.name)} className="mt-0.5" />
                        <div>
                          <span className="block text-xs font-bold text-navy dark:text-white capitalize">{c.name.replace(/_/g, ' ')}</span>
                          <span className="block text-[10px] text-slate-500 dark:text-slate-400 mt-1">{c.description}</span>
                        </div>
                      </label>
                    ))}
                  </div>
                  <div className="flex items-center gap-3">
                    <button onClick={saveEnabledChecks} disabled={!checksDirty || checksSaving} className="bg-brandblue text-white px-4 py-2 rounded text-xs font-bold disabled:opacity-50 hover:bg-navy transition-colors shadow-sm">
                      {checksSaving ? 'Saving...' : 'Save Configuration'}
                    </button>
                    <button onClick={selectAllChecks} disabled={selectedChecks.size === checkCatalog.length} className="text-brandblue text-xs font-bold hover:underline disabled:opacity-50">
                      Select All
                    </button>
                    {checksNotice && <span className="text-xs text-risklow font-medium">{checksNotice}</span>}
                  </div>
                </div>
              )}
            </div>
          </div>
        )}

        {activeTab === "Configuration" && (
          <div>
            {goldenNotice && (
              <p className="text-xs text-risklow bg-green-50 dark:bg-green-950/30 border border-green-100 dark:border-green-900 rounded-lg px-3 py-2 mb-3">
                {goldenNotice}
              </p>
            )}
            {configLoading ? (
              <p className="text-xs text-slate-400 dark:text-slate-500">Loading configurations...</p>
            ) : (
              <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
                <ConfigViewer
                  title="Running Configuration"
                  config={running?.config}
                  configPretty={running?.config_pretty}
                  isXml={running?.is_xml}
                  emptyText="(no configuration available)"
                />
                <ConfigViewer
                  title="Startup Configuration"
                  config={startup?.source === "unavailable" ? null : startup?.config}
                  configPretty={startup?.config_pretty}
                  isXml={startup?.is_xml}
                  emptyText={
                    startup?.source === "unavailable"
                      ? "No startup configuration on file yet."
                      : "(no startup configuration available)"
                  }
                />
                <div className="xl:col-span-2">
                  <div className="flex items-center justify-between mb-2">
                    <p className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider">
                      Golden Config
                      {golden && (
                        <span className="ml-2 font-normal normal-case text-slate-400">
                          approved by {golden.set_by} · {new Date(golden.updated_at).toLocaleString()}
                        </span>
                      )}
                    </p>
                    {canManage && (
                      <div className="flex gap-2">
                        <button
                          onClick={setRunningAsGolden}
                          disabled={goldenBusy || !running?.config}
                          className="text-[10px] uppercase tracking-wider font-bold text-white bg-brandblue hover:bg-navy px-3 py-1.5 rounded-lg shadow-sm disabled:opacity-40"
                        >
                          {goldenBusy ? "Saving…" : golden ? "↻ Approve Current Running Config" : "+ Set as Golden Config"}
                        </button>
                        {golden && (
                          <button
                            onClick={clearGolden}
                            disabled={goldenBusy}
                            className="text-[10px] uppercase tracking-wider font-bold text-riskcrit border border-red-200 bg-red-50 hover:bg-red-100 px-3 py-1.5 rounded-lg disabled:opacity-40"
                          >
                            Clear
                          </button>
                        )}
                      </div>
                    )}
                  </div>
                  <ConfigViewer
                    title="Golden Config"
                    config={golden?.config}
                    configPretty={golden?.config_pretty}
                    isXml={golden?.is_xml}
                    emptyText="No golden config set for this device yet. Use the button above to approve the current running config as the baseline."
                  />
                </div>
              </div>
            )}
          </div>
        )}

        {activeTab === "Interfaces" && (
          <div className="flex flex-col gap-6">
            <div>
              <div className="flex items-center justify-between mb-2">
                <h4 className="text-xs uppercase font-bold text-slate-500 dark:text-slate-400 tracking-wider">
                  Interface Status {interfaces && !interfacesError && `(${interfaces.protocol.toUpperCase()})`}
                </h4>
                <button
                  onClick={loadInterfaces}
                  disabled={interfacesLoading}
                  className="text-[10px] font-bold uppercase text-slate-500 dark:text-slate-400 hover:text-brandblue border border-slate-300 dark:border-slate-600 rounded-md px-2 py-1 disabled:opacity-50"
                >
                  {interfacesLoading ? "Refreshing…" : "Refresh"}
                </button>
              </div>
              {interfacesLoading && !interfaces ? (
                <p className="text-xs text-slate-400 dark:text-slate-500">Reading interface status from the device…</p>
              ) : interfacesError || interfaces?.error ? (
                <p className="text-xs text-riskcrit">{interfacesError || interfaces?.error}</p>
              ) : !interfaces || interfaces.interfaces.length === 0 ? (
                <p className="text-xs text-slate-400 dark:text-slate-500 italic">
                  No interfaces reported by the device.
                </p>
              ) : (
                <div className="overflow-x-auto border border-slate-200 dark:border-slate-700 rounded-lg">
                  <table className="w-full text-xs">
                    <thead className="bg-slate-100 dark:bg-slate-700">
                      <tr>
                        <th className="text-left px-3 py-2 font-bold text-slate-500 dark:text-slate-400 uppercase">Interface</th>
                        <th className="text-left px-3 py-2 font-bold text-slate-500 dark:text-slate-400 uppercase">Description</th>
                        <th className="text-left px-3 py-2 font-bold text-slate-500 dark:text-slate-400 uppercase">Admin</th>
                        <th className="text-left px-3 py-2 font-bold text-slate-500 dark:text-slate-400 uppercase">Oper</th>
                        <th className="text-left px-3 py-2 font-bold text-slate-500 dark:text-slate-400 uppercase">IP Address(es)</th>
                        <th className="text-left px-3 py-2 font-bold text-slate-500 dark:text-slate-400 uppercase">MTU</th>
                        <th className="text-left px-3 py-2 font-bold text-slate-500 dark:text-slate-400 uppercase">Port Mode</th>
                        <th className="text-left px-3 py-2 font-bold text-slate-500 dark:text-slate-400 uppercase">VLAN(s)</th>
                        <th className="text-left px-3 py-2 font-bold text-slate-500 dark:text-slate-400 uppercase">Edge Port</th>
                        <th className="text-left px-3 py-2 font-bold text-slate-500 dark:text-slate-400 uppercase">Down Alerts</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                      {interfaces.interfaces.map((iface) => (
                        <tr key={iface.name} className="bg-white dark:bg-slate-800">
                          <td className="px-3 py-1.5 font-mono font-semibold text-navy dark:text-white">{iface.name}</td>
                          <td className="px-3 py-1.5 text-slate-500 dark:text-slate-400">{iface.description || "—"}</td>
                          <td className="px-3 py-1.5">
                            <span
                              className={`px-1.5 py-0.5 rounded-full text-[10px] font-bold uppercase ${
                                iface.admin_status === "up"
                                  ? "bg-green-50 text-green-700 border border-green-200"
                                  : iface.admin_status === "down"
                                  ? "bg-slate-100 text-slate-500 border border-slate-200"
                                  : "bg-slate-50 text-slate-400 border border-slate-200"
                              }`}
                            >
                              {iface.admin_status || "unknown"}
                            </span>
                          </td>
                          <td className="px-3 py-1.5">
                            <span
                              className={`px-1.5 py-0.5 rounded-full text-[10px] font-bold uppercase ${
                                iface.oper_status === "up"
                                  ? "bg-green-50 text-green-700 border border-green-200"
                                  : iface.oper_status === "down"
                                  ? "bg-red-50 text-riskcrit border border-red-200"
                                  : "bg-slate-50 text-slate-400 border border-slate-200"
                              }`}
                            >
                              {iface.oper_status || "unknown"}
                            </span>
                          </td>
                          <td className="px-3 py-1.5 font-mono text-slate-600 dark:text-slate-300">
                            {iface.ip_addresses.length ? iface.ip_addresses.join(", ") : "—"}
                          </td>
                          <td className="px-3 py-1.5 text-slate-500 dark:text-slate-400">{iface.mtu ?? "—"}</td>
                          <td className="px-3 py-1.5">
                            {iface.port_mode ? (
                              <span
                                className={`px-1.5 py-0.5 rounded-full text-[10px] font-bold uppercase ${
                                  iface.port_mode === "trunk"
                                    ? "bg-purple-50 text-purple-700 border border-purple-200"
                                    : iface.port_mode === "access"
                                    ? "bg-sky-50 text-sky-700 border border-sky-200"
                                    : "bg-slate-100 text-slate-500 border border-slate-200"
                                }`}
                              >
                                {iface.port_mode}
                              </span>
                            ) : (
                              <span className="text-slate-400 dark:text-slate-500">—</span>
                            )}
                          </td>
                          <td className="px-3 py-1.5 font-mono text-slate-600 dark:text-slate-300">
                            {iface.port_mode === "trunk" && iface.trunk_vlans?.length
                              ? `${iface.vlan ?? "—"} (native), ${iface.trunk_vlans.join(", ")}`
                              : iface.vlan ?? "—"}
                          </td>
                          <td className="px-3 py-1.5">
                            {iface.edge_port === true ? (
                              <span className="px-1.5 py-0.5 rounded-full text-[10px] font-bold uppercase bg-green-50 text-green-700 border border-green-200">
                                Edge
                              </span>
                            ) : iface.edge_port === false ? (
                              <span className="px-1.5 py-0.5 rounded-full text-[10px] font-bold uppercase bg-slate-100 text-slate-500 border border-slate-200">
                                Non-edge
                              </span>
                            ) : (
                              <span className="text-slate-400 dark:text-slate-500">—</span>
                            )}
                          </td>
                          <td className="px-3 py-1.5">
                            <button
                              onClick={() => setInterfaceAlertsEnabled(iface.name, !iface.alerts_enabled)}
                              disabled={alertConfigSaving === iface.name}
                              title={
                                iface.alerts_enabled
                                  ? "Interface Down alerts are armed for this port -- click to mute"
                                  : "Interface Down alerts are muted for this port -- click to re-arm"
                              }
                              className={`px-1.5 py-0.5 rounded-full text-[10px] font-bold uppercase border transition-colors disabled:opacity-50 ${
                                iface.alerts_enabled
                                  ? "bg-green-50 text-green-700 border-green-200 hover:bg-green-100"
                                  : "bg-slate-100 text-slate-500 border-slate-200 hover:bg-slate-200"
                              }`}
                            >
                              {alertConfigSaving === iface.name ? "Saving…" : iface.alerts_enabled ? "On" : "Muted"}
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <p className="text-[10px] text-slate-400 dark:text-slate-500 mt-1.5">
                Port Mode / VLAN / Edge Port are read via SNMP (BRIDGE-MIB / Q-BRIDGE-MIB, and Cisco's
                CISCO-STP-EXTENSIONS-MIB for edge state) and show "—" on devices or platforms that don't expose them.
                "Down Alerts" toggles whether an Interface Down alert fires for that specific port.
              </p>
            </div>

            <div>
              <h4 className="text-xs uppercase font-bold text-slate-500 dark:text-slate-400 tracking-wider mb-2">
                Fleet-Aggregate Utilization (SNMP, last 24h)
              </h4>
              {healthLoading && metricHistory.length === 0 ? (
                <p className="text-xs text-slate-400 dark:text-slate-500">Loading interface telemetry…</p>
              ) : metricHistory.length === 0 ? (
                <div className="text-slate-500 dark:text-slate-400 flex flex-col items-center justify-center h-32 opacity-60">
                  <div className="text-3xl mb-2">🔌</div>
                  <p className="text-sm font-medium">No interface telemetry recorded yet for this device.</p>
                  <p className="text-xs text-slate-400 dark:text-slate-500 mt-1">
                    {device.supports_snmp
                      ? "Waiting for the next SNMP poll (or use Poll Now on the Health tab)."
                      : "Enable SNMP monitoring on this device to start collecting it."}
                  </p>
                </div>
              ) : (
                <div className="flex flex-col gap-4">
                  <p className="text-xs text-slate-400 dark:text-slate-500">
                    This chart tracks total link utilization and cumulative error count per SNMP poll across the
                    whole device (not broken out per interface) -- use the live table above for per-interface
                    status.
                  </p>
                  <div className="grid grid-cols-2 gap-3 max-w-md">
                    <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg p-3 shadow-sm">
                      <p className="text-[10px] uppercase font-bold text-slate-400 dark:text-slate-500 tracking-wider">Current Utilization</p>
                      <p className="text-lg font-bold text-navy dark:text-white">
                        {metricHistory[metricHistory.length - 1]?.interface_utilization_pct ?? "—"}%
                      </p>
                    </div>
                    <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg p-3 shadow-sm">
                      <p className="text-[10px] uppercase font-bold text-slate-400 dark:text-slate-500 tracking-wider">Current Errors</p>
                      <p className="text-lg font-bold text-navy dark:text-white">
                        {metricHistory[metricHistory.length - 1]?.interface_errors ?? "—"}
                      </p>
                    </div>
                  </div>
                  <div className="max-h-64 overflow-auto border border-slate-200 dark:border-slate-700 rounded-lg">
                    <table className="w-full text-xs">
                      <thead className="bg-slate-100 dark:bg-slate-700 sticky top-0">
                        <tr>
                          <th className="text-left px-3 py-2 font-bold text-slate-500 dark:text-slate-400 uppercase">Polled</th>
                          <th className="text-left px-3 py-2 font-bold text-slate-500 dark:text-slate-400 uppercase">Utilization</th>
                          <th className="text-left px-3 py-2 font-bold text-slate-500 dark:text-slate-400 uppercase">Errors</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                        {[...metricHistory].reverse().slice(0, 50).map((m) => (
                          <tr key={m.id} className="bg-white dark:bg-slate-800">
                            <td className="px-3 py-1.5 text-slate-500 dark:text-slate-400">{timeAgo(m.polled_at)}</td>
                            <td className="px-3 py-1.5 text-slate-700 dark:text-slate-200">
                              {m.interface_utilization_pct != null ? `${m.interface_utilization_pct}%` : "—"}
                            </td>
                            <td className={`px-3 py-1.5 font-bold ${m.interface_errors ? "text-riskcrit" : "text-slate-700 dark:text-slate-200"}`}>
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
          </div>
        )}

        {activeTab === "Discovery" && (
          <div className="flex flex-col gap-4">
            <div className="flex items-center justify-between">
              <div>
                <h4 className="text-xs uppercase font-bold text-slate-500 dark:text-slate-400 tracking-wider">
                  SNMP Discovery
                </h4>
                <p className="text-[11px] text-slate-400 dark:text-slate-500 mt-0.5">
                  Reads ARP, routing, LLDP/CDP neighbors, and chassis inventory directly from the device. LLDP/CDP
                  results are saved and used to draw confirmed links on the Topology page.
                </p>
              </div>
              <button
                onClick={loadDiscovery}
                disabled={discoveryLoading}
                className="text-xs font-bold uppercase tracking-wider text-white bg-brandblue hover:bg-navy px-3 py-1.5 rounded-lg shadow-sm disabled:opacity-50 shrink-0"
              >
                {discoveryLoading ? "Discovering…" : discovery ? "Run Discovery Again" : "Run Discovery"}
              </button>
            </div>

            {discoveryError && (
              <div className="text-xs text-riskcrit bg-red-50 dark:bg-red-950/30 border border-red-200 dark:border-red-900 rounded-lg px-3 py-2">
                {discoveryError}
              </div>
            )}

            {!discovery && !discoveryLoading && !discoveryError && (
              <div className="bg-white dark:bg-slate-800 rounded-xl border border-slate-200 dark:border-slate-700 p-10 text-center">
                <div className="text-4xl mb-3">🔎</div>
                <p className="text-sm text-slate-500 dark:text-slate-400">
                  No discovery data yet for this device. Click "Run Discovery" to poll it via SNMP.
                </p>
              </div>
            )}

            {discovery && (
              <>
                <div className="flex flex-wrap items-center gap-4 text-xs bg-slate-50 dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded-lg px-3 py-2">
                  <span className="text-slate-500 dark:text-slate-400">
                    Reported hostname:{" "}
                    <span className="font-mono font-semibold text-navy dark:text-white">
                      {discovery.reported_hostname || "—"}
                    </span>
                  </span>
                  <span className="text-slate-400 dark:text-slate-500">
                    Retrieved {timeAgo(discovery.retrieved_at)}
                  </span>
                </div>

                <div className="flex gap-1 border-b border-slate-200 dark:border-slate-700">
                  {([
                    { key: "lldp", label: `LLDP (${discovery.lldp_neighbors.length})` },
                    { key: "cdp", label: `CDP (${discovery.cdp_neighbors.length})` },
                    { key: "arp", label: `ARP (${discovery.arp_table.length})` },
                    { key: "routes", label: `Routes (${discovery.routing_table.length})` },
                    { key: "inventory", label: `Inventory (${discovery.inventory.length})` },
                  ] as const).map((t) => (
                    <button
                      key={t.key}
                      onClick={() => setDiscoverySubTab(t.key)}
                      className={`text-xs font-semibold px-3 py-2 border-b-2 -mb-px transition-colors ${
                        discoverySubTab === t.key
                          ? "border-brandblue text-brandblue"
                          : "border-transparent text-slate-400 dark:text-slate-500 hover:text-slate-600"
                      }`}
                    >
                      {t.label}
                    </button>
                  ))}
                </div>

                {discoverySubTab === "lldp" && (
                  <DiscoveryTable
                    empty="No LLDP neighbors reported (LLDP may be disabled on this device)."
                    columns={["Local Port", "Neighbor", "Neighbor Port"]}
                    rows={discovery.lldp_neighbors.map((n) => [
                      n.local_port_index,
                      n.neighbor_name || "—",
                      n.neighbor_port || "—",
                    ])}
                  />
                )}
                {discoverySubTab === "cdp" && (
                  <DiscoveryTable
                    empty="No CDP neighbors reported (CDP may be disabled, or this isn't a Cisco device)."
                    columns={["Local Interface", "Neighbor", "Neighbor Port", "Platform"]}
                    rows={discovery.cdp_neighbors.map((n) => [
                      n.local_if_index,
                      n.neighbor_id || "—",
                      n.neighbor_port || "—",
                      n.neighbor_platform || "—",
                    ])}
                  />
                )}
                {discoverySubTab === "arp" && (
                  <DiscoveryTable
                    empty="No ARP entries reported."
                    columns={["Interface Index", "IP Address", "MAC Address"]}
                    rows={discovery.arp_table.map((a) => [a.if_index, a.ip_address, a.mac_address])}
                  />
                )}
                {discoverySubTab === "routes" && (
                  <DiscoveryTable
                    empty="No routing table entries reported."
                    columns={["Destination", "Mask", "Next Hop", "Interface Index"]}
                    rows={discovery.routing_table.map((r) => [
                      r.destination,
                      r.mask || "—",
                      r.next_hop,
                      r.if_index || "—",
                    ])}
                  />
                )}
                {discoverySubTab === "inventory" && (
                  <DiscoveryTable
                    empty="No chassis/module inventory reported."
                    columns={["Index", "Name", "Description", "Model", "Serial Number"]}
                    rows={discovery.inventory.map((i) => [
                      i.index,
                      i.name || "—",
                      i.description || "—",
                      i.model || "—",
                      i.serial_number || "—",
                    ])}
                  />
                )}
              </>
            )}
          </div>
        )}

        {activeTab === "Backups" && (
          <div>
            <div className="flex items-center justify-between mb-3">
              <p className="text-xs text-slate-400 dark:text-slate-500">
                On-demand backups are stored the same way as automatic pre-deployment snapshots.
              </p>
              {canManage && (
                <button
                  onClick={runBackupNow}
                  disabled={backingUp}
                  className="text-[10px] uppercase tracking-wider font-bold text-white bg-brandblue hover:bg-navy px-3 py-1.5 rounded-lg shadow-sm disabled:opacity-40 disabled:cursor-not-allowed shrink-0"
                >
                  {backingUp ? "Backing up…" : "⭳ Backup Now"}
                </button>
              )}
            </div>
            {retention && (
              <div className="text-[11px] text-slate-500 dark:text-slate-400 bg-slate-50 dark:bg-slate-900/40 border border-slate-200 dark:border-slate-700 rounded-lg px-3 py-2 mb-3">
                <span className="font-semibold text-slate-600 dark:text-slate-300">Retention policy:</span>{" "}
                {retention.policy.description}
                {retention.device && (
                  <span className="block mt-1">
                    This device: {retention.device.total_snapshots} snapshot{retention.device.total_snapshots === 1 ? "" : "s"} on file
                    {" "}({retention.device.protected_snapshots} protected
                    {retention.device.eligible_for_purge > 0
                      ? `, ${retention.device.eligible_for_purge} eligible for the next nightly purge`
                      : ""}
                    ).
                  </span>
                )}
              </div>
            )}
            {backupNotice && (
              <p className="text-xs text-risklow bg-green-50 dark:bg-green-950/30 border border-green-100 dark:border-green-900 rounded-lg px-3 py-2 mb-3">
                {backupNotice}
              </p>
            )}
            {historyLoading ? (
              <p className="text-xs text-slate-400 dark:text-slate-500">Loading backup history...</p>
            ) : historyError ? (
              <p className="text-xs text-riskcrit">{historyError}</p>
            ) : history.length === 0 ? (
              <p className="text-xs text-slate-400 dark:text-slate-500 italic">No snapshots yet.</p>
            ) : (
              <div className="flex flex-col gap-6">
                <div>
                  <h4 className="text-xs uppercase font-bold text-slate-500 dark:text-slate-400 mb-3 tracking-wider">Snapshot History</h4>
                  <ul className="space-y-1.5 max-h-60 overflow-y-auto pr-2">
                    {history.map((s, idx) => (
                      <li
                        key={s.id}
                        className="flex items-center gap-3 text-xs bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg px-3 py-2 shadow-sm"
                      >
                        <span className="font-mono text-slate-500 dark:text-slate-400 font-bold shrink-0">v{s.version}</span>
                        <span className="font-mono text-slate-400 dark:text-slate-500 shrink-0">{s.checksum.slice(0, 12)}…</span>
                        <span className="text-slate-500 dark:text-slate-400 font-medium">{new Date(s.created_at).toLocaleString()}</span>
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
                            className="ml-auto text-amber-600 dark:text-amber-400 font-bold hover:text-amber-700 shrink-0 uppercase tracking-widest text-[10px] bg-amber-50 dark:bg-amber-950/40 px-2 py-1 rounded"
                          >
                            ↺ Roll back to this
                          </button>
                        )}
                      </li>
                    ))}
                  </ul>
                </div>

                <div className="bg-white dark:bg-slate-800 p-4 rounded-lg border border-slate-200 dark:border-slate-700 shadow-sm max-w-2xl">
                  <h4 className="text-xs uppercase font-bold text-slate-500 dark:text-slate-400 mb-3 tracking-wider flex justify-between items-center">
                    <span>Compare Snapshots</span>
                    <button onClick={runCompare} disabled={comparing} className="bg-brandblue text-white px-3 py-1 rounded text-[11px]">
                      {comparing ? "Comparing..." : "Run Compare"}
                    </button>
                  </h4>
                  <div className="grid grid-cols-2 gap-4">
                    <div>
                      <select className="w-full border border-slate-300 dark:border-slate-600 rounded px-2 py-1.5 text-xs text-slate-600 dark:text-slate-300" value={baseId} onChange={(e) => setBaseId(e.target.value)}>
                        <option value="">Live Configuration</option>
                        {history.map(h => <option key={h.id} value={h.id}>v{h.version}</option>)}
                      </select>
                    </div>
                    <div>
                      <select className="w-full border border-slate-300 dark:border-slate-600 rounded px-2 py-1.5 text-xs text-slate-600 dark:text-slate-300" value={targetId} onChange={(e) => setTargetId(e.target.value)}>
                        <option value="">Live Configuration</option>
                        {history.map(h => <option key={h.id} value={h.id}>v{h.version}</option>)}
                      </select>
                    </div>
                  </div>
                  {compareResult && (
                    <div className="mt-4 border-t border-slate-100 dark:border-slate-800 pt-4">
                      {compareResult.identical ? (
                        <p className="text-green-600 dark:text-green-400 font-medium text-xs text-center">Configurations are completely identical.</p>
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
            {deviceAlerts.length > 0 && (
              <div className="flex justify-end mb-2">
                <button
                  onClick={clearDeviceAlerts}
                  disabled={clearingAlerts || activeDeviceAlertCount === 0}
                  className="text-[10px] uppercase tracking-wider font-bold text-white bg-riskcrit/90 hover:bg-riskcrit px-2.5 py-1.5 rounded-lg shadow-sm disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  {clearingAlerts ? "Clearing…" : `Clear Alerts${activeDeviceAlertCount ? ` (${activeDeviceAlertCount})` : ""}`}
                </button>
              </div>
            )}
            {alertsLoading ? (
              <p className="text-xs text-slate-400 dark:text-slate-500">Loading alerts…</p>
            ) : alertsError ? (
              <p className="text-xs text-riskcrit">{alertsError}</p>
            ) : deviceAlerts.length === 0 ? (
              <div className="text-slate-500 dark:text-slate-400 flex flex-col items-center justify-center h-48 opacity-60">
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
                            <p className="text-sm text-navy dark:text-white mt-0.5">{a.message}</p>
                            <p className="text-[11px] text-slate-400 dark:text-slate-500 mt-1">
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
                            className="shrink-0 text-[10px] uppercase tracking-wider font-bold text-slate-500 dark:text-slate-400 border border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-800 px-2 py-1 rounded hover:bg-slate-50 dark:hover:bg-slate-700 disabled:opacity-50"
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

        {activeTab === "Protocol Operations" && (
          <div>
            {protocolOpsLoading ? (
              <p className="text-xs text-slate-400 dark:text-slate-500">Loading protocol operations…</p>
            ) : protocolOpsError ? (
              <p className="text-xs text-riskcrit">{protocolOpsError}</p>
            ) : protocolOps.length === 0 ? (
              <div className="text-slate-500 dark:text-slate-400 flex flex-col items-center justify-center h-48 opacity-60">
                <div className="text-3xl mb-2">🔗</div>
                <p className="text-sm font-medium">No NETCONF/RESTCONF/SNMP operations recorded yet.</p>
              </div>
            ) : (
              <div className="max-h-96 overflow-auto border border-slate-200 dark:border-slate-700 rounded-lg">
                <table className="w-full text-xs">
                  <thead className="bg-slate-100 dark:bg-slate-700 sticky top-0">
                    <tr>
                      <th className="text-left px-3 py-2 font-bold text-slate-500 dark:text-slate-400 uppercase">When</th>
                      <th className="text-left px-3 py-2 font-bold text-slate-500 dark:text-slate-400 uppercase">Protocol</th>
                      <th className="text-left px-3 py-2 font-bold text-slate-500 dark:text-slate-400 uppercase">Operation</th>
                      <th className="text-left px-3 py-2 font-bold text-slate-500 dark:text-slate-400 uppercase">Operator</th>
                      <th className="text-left px-3 py-2 font-bold text-slate-500 dark:text-slate-400 uppercase">Result</th>
                      <th className="text-left px-3 py-2 font-bold text-slate-500 dark:text-slate-400 uppercase">Duration</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                    {protocolOps.map((op) => (
                      <tr key={op.id} className="bg-white dark:bg-slate-800">
                        <td className="px-3 py-1.5 text-slate-500 dark:text-slate-400">{timeAgo(op.created_at)}</td>
                        <td className="px-3 py-1.5 uppercase font-bold text-slate-600 dark:text-slate-300">{op.protocol}</td>
                        <td className="px-3 py-1.5 text-slate-700 dark:text-slate-200">{op.operation}</td>
                        <td className="px-3 py-1.5 text-slate-500 dark:text-slate-400">{op.operator}</td>
                        <td className="px-3 py-1.5">
                          <span className={`font-bold ${op.success ? "text-risklow" : "text-riskcrit"}`}>
                            {op.success ? "Success" : "Failed"}
                          </span>
                          {op.http_status != null && <span className="text-slate-400 dark:text-slate-500 ml-1">({op.http_status})</span>}
                          {!op.success && op.error_message && (
                            <p className="text-slate-400 dark:text-slate-500 mt-0.5 max-w-xs truncate" title={op.error_message}>
                              {op.error_message}
                            </p>
                          )}
                        </td>
                        <td className="px-3 py-1.5 text-slate-500 dark:text-slate-400">
                          {op.execution_time_ms != null ? `${Math.round(op.execution_time_ms)}ms` : "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}

        {activeTab === "Deployment History" && (
          <div>
            {deploymentsLoading ? (
              <p className="text-xs text-slate-400 dark:text-slate-500">Loading deployment history…</p>
            ) : deploymentsError ? (
              <p className="text-xs text-riskcrit">{deploymentsError}</p>
            ) : deployments.length === 0 ? (
              <div className="text-slate-500 dark:text-slate-400 flex flex-col items-center justify-center h-48 opacity-60">
                <div className="text-3xl mb-2">🚀</div>
                <p className="text-sm font-medium">No deployments recorded yet for this device.</p>
              </div>
            ) : (
              <ul className="space-y-2 max-h-96 overflow-y-auto pr-1">
                {deployments.map((d) => {
                  const statusStyle: Record<string, string> = {
                    succeeded: "text-risklow bg-green-50 dark:bg-green-950/40 border-green-200 dark:border-green-800",
                    failed: "text-riskcrit bg-red-50 dark:bg-red-950/40 border-red-200 dark:border-red-800",
                    rolled_back: "text-riskmed bg-amber-50 dark:bg-amber-950/40 border-amber-200 dark:border-amber-800",
                    in_progress: "text-brandblue bg-blue-50 dark:bg-blue-950/40 border-blue-200 dark:border-blue-800",
                    queued: "text-slate-500 dark:text-slate-400 bg-slate-50 dark:bg-slate-900 border-slate-200 dark:border-slate-700",
                  };
                  const style = statusStyle[d.status] || statusStyle.queued;
                  return (
                    <li key={d.id} className={`border rounded-lg px-3 py-2.5 shadow-sm ${style}`}>
                      <div className="flex items-center justify-between gap-3">
                        <div>
                          <p className="text-xs font-bold uppercase tracking-wide">{d.status.replace("_", " ")}</p>
                          <p className="text-[11px] text-slate-500 dark:text-slate-400 mt-0.5">
                            {d.protocol.toUpperCase()} · {new Date(d.created_at).toLocaleString()}
                          </p>
                          {d.error_message && <p className="text-[11px] text-riskcrit mt-1">{d.error_message}</p>}
                        </div>
                        {d.health_checks.length > 0 && (
                          <span className="text-[10px] font-bold uppercase text-slate-400 dark:text-slate-500 shrink-0">
                            {d.health_checks.filter((h) => h.passed).length}/{d.health_checks.length} checks passed
                          </span>
                        )}
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        )}

        {/* Placeholder for remaining tabs not yet wired to a dedicated data source */}
        {["Drift"].includes(activeTab) && (
          <div className="text-slate-500 dark:text-slate-400 flex flex-col items-center justify-center h-48 opacity-60">
            <div className="text-3xl mb-2">🚧</div>
            <p className="text-sm font-medium">{activeTab} data integration coming in next phase.</p>
          </div>
        )}
      </div>

      {showSnmpCredsModal && (
        <SnmpCredentialsModal
          device={device}
          onClose={() => setShowSnmpCredsModal(false)}
          onDeviceUpdated={onDeviceUpdated}
          onSaved={(updated) => {
            onDeviceUpdated(updated);
            setShowSnmpCredsModal(false);
          }}
        />
      )}

      {showSshCredsModal && (
        <SshCredentialsModal
          device={device}
          onClose={() => setShowSshCredsModal(false)}
          onSaved={(updated) => {
            onDeviceUpdated(updated);
            setShowSshCredsModal(false);
          }}
        />
      )}
    </div>
  );
}

// --- Main Devices List ---

export default function Devices() {
  const { user } = useAuth();
  const toast = useToast();
  const confirm = useConfirm();
  const canManage = user?.role === "network_admin";
  const [devices, setDevices] = useState<Device[]>([]);
  const [form, setForm] = useState(emptyForm);
  const [loading, setLoading] = useState(false);
  const [initialLoading, setInitialLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [searchParams] = useSearchParams();
  const [query, setQuery] = useState(searchParams.get("q") || "");
  const [vendorFilter, setVendorFilter] = useState<string>("all");
  const [dcFilter, setDcFilter] = useState<string>("all");
  const [rackFilter, setRackFilter] = useState<string>("all");
  // "core" | "distribution" | "access" | ... (free-text, org-defined) --
  // same values Device.device_role/the Topology layered view use. Added
  // alongside the other filters mainly so saved views like "My Core
  // Switches" are actually expressible, not just vendor/DC/rack ones.
  const [roleFilter, setRoleFilter] = useState<string>("all");
  // Fleet Health strip -- scoped to whatever vendorFilter is currently
  // selected, so picking "juniper" in the existing vendor dropdown also
  // narrows this to a "Juniper fleet" health view instead of needing a
  // separate page. Refetches whenever the filter changes.
  const [fleetHealth, setFleetHealth] = useState<FleetHealthSummary | null>(null);
  const [fleetHealthLoading, setFleetHealthLoading] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [clearingUnstableId, setClearingUnstableId] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [editingDeviceId, setEditingDeviceId] = useState<string | null>(null);
  const [expandedDeviceId, setExpandedDeviceId] = useState<string | null>(null);
  const [activeTerminalDevice, setActiveTerminalDevice] = useState<string | null>(null);

  const [rollbackTarget, setRollbackTarget] = useState<{ device: Device; snapshot: Snapshot } | null>(null);
  const [rollbackReason, setRollbackReason] = useState("");
  const [rollbackSubmitting, setRollbackSubmitting] = useState(false);
  const [rollbackError, setRollbackError] = useState<string | null>(null);
  const [rollbackNotice, setRollbackNotice] = useState<string | null>(null);

  // Rollback preview: the diff this rollback would apply, fetched as soon
  // as the confirmation modal opens so it's shown *before* the user
  // confirms, not only afterward in the change request / audit log.
  const [rollbackPreview, setRollbackPreview] = useState<RollbackPreviewResponse | null>(null);
  const [rollbackPreviewLoading, setRollbackPreviewLoading] = useState(false);
  const [rollbackPreviewError, setRollbackPreviewError] = useState<string | null>(null);

  // Section-level (partial) rollback: an alternate mode of the same modal.
  // "full" restores the entire snapshot (unchanged, existing behavior);
  // "partial" restores only one section (an ACL, VLAN, interface, ...) of
  // it, leaving the rest of the device's current config untouched.
  const [rollbackMode, setRollbackMode] = useState<"full" | "partial">("full");
  const [rollbackSections, setRollbackSections] = useState<RollbackSection[] | null>(null);
  const [rollbackSectionsLoading, setRollbackSectionsLoading] = useState(false);
  const [rollbackSectionsError, setRollbackSectionsError] = useState<string | null>(null);
  const [rollbackSectionKey, setRollbackSectionKey] = useState<string>("");
  const [partialRollbackPreview, setPartialRollbackPreview] = useState<PartialRollbackPreviewResponse | null>(null);
  const [partialRollbackPreviewLoading, setPartialRollbackPreviewLoading] = useState(false);
  const [partialRollbackPreviewError, setPartialRollbackPreviewError] = useState<string | null>(null);

  // Reset to "full" mode and clear section state each time a new rollback
  // target is picked, and load that device's revertible sections so the
  // "Section-level" tab has something to offer as soon as it's clicked.
  useEffect(() => {
    setRollbackMode("full");
    setRollbackSectionKey("");
    setRollbackSections(null);
    setPartialRollbackPreview(null);
    setPartialRollbackPreviewError(null);
    if (!rollbackTarget) return;
    setRollbackSectionsLoading(true);
    setRollbackSectionsError(null);
    api
      .get<RollbackSection[]>(`/devices/${rollbackTarget.device.id}/rollback/sections`)
      .then((res) => setRollbackSections(res.data))
      .catch((err) => setRollbackSectionsError(err?.response?.data?.detail || "Failed to load revertible sections."))
      .finally(() => setRollbackSectionsLoading(false));
  }, [rollbackTarget?.device.id]);

  // Partial rollback preview: fetched as soon as both a snapshot and a
  // section are chosen in "Section-level" mode.
  useEffect(() => {
    if (rollbackMode !== "partial" || !rollbackTarget || !rollbackSectionKey) {
      setPartialRollbackPreview(null);
      setPartialRollbackPreviewError(null);
      return;
    }
    setPartialRollbackPreviewLoading(true);
    setPartialRollbackPreviewError(null);
    setPartialRollbackPreview(null);
    api
      .get<PartialRollbackPreviewResponse>(`/devices/${rollbackTarget.device.id}/rollback/partial/preview`, {
        params: { snapshot_id: rollbackTarget.snapshot.id, section_key: rollbackSectionKey },
      })
      .then((res) => setPartialRollbackPreview(res.data))
      .catch((err) =>
        setPartialRollbackPreviewError(err?.response?.data?.detail || "Failed to load partial rollback preview.")
      )
      .finally(() => setPartialRollbackPreviewLoading(false));
  }, [rollbackMode, rollbackTarget?.device.id, rollbackTarget?.snapshot.id, rollbackSectionKey]);

  useEffect(() => {
    if (!rollbackTarget) {
      setRollbackPreview(null);
      setRollbackPreviewError(null);
      return;
    }
    setRollbackPreviewLoading(true);
    setRollbackPreviewError(null);
    setRollbackPreview(null);
    api
      .get<RollbackPreviewResponse>(`/devices/${rollbackTarget.device.id}/rollback/preview`, {
        params: { snapshot_id: rollbackTarget.snapshot.id },
      })
      .then((res) => setRollbackPreview(res.data))
      .catch((err) => setRollbackPreviewError(err?.response?.data?.detail || "Failed to load rollback preview."))
      .finally(() => setRollbackPreviewLoading(false));
  }, [rollbackTarget?.device.id, rollbackTarget?.snapshot.id]);


  // --- Bulk edit (multi-select) ---
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkError, setBulkError] = useState<string | null>(null);
  const [bulkNotice, setBulkNotice] = useState<string | null>(null);
  const [bulkSiteValue, setBulkSiteValue] = useState("");
  const [bulkVendorValue, setBulkVendorValue] = useState("cisco");
  const [bulkDcValue, setBulkDcValue] = useState("");
  const [bulkRackValue, setBulkRackValue] = useState("");
  const [bulkRoleValue, setBulkRoleValue] = useState("");
  const [showBulkRotateModal, setShowBulkRotateModal] = useState(false);

  const toggleSelected = (id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const clearSelection = () => setSelectedIds(new Set());

  // Human-readable "blocked" message for a 409 from DELETE /devices/{id} --
  // shared by the single-row and bulk delete paths so they read the same.
  const describeBlockingCounts = (counts: Record<string, number> | undefined): string => {
    if (!counts) return "";
    return Object.entries(counts)
      .map(([k, v]) => `${v} ${k.replace(/_/g, " ")}`)
      .join(", ");
  };

  const extractErrorMessage = (err: any, fallback: string): string => {
    const detail = err?.response?.data?.detail;
    if (typeof detail === "string") return detail;
    if (detail?.message) return detail.message;
    return fallback;
  };

  /** Bulk-applies a partial PATCH to every selected device. Used for
   * "assign site", "enable/disable SNMP monitoring", and "tag vendor" --
   * all three are just different fields on the same endpoint the single
   * device edit path already uses.
   */
  const bulkPatch = async (fields: Record<string, unknown>, label: string) => {
    const ids = Array.from(selectedIds);
    if (ids.length === 0) return;
    setBulkBusy(true);
    setBulkError(null);
    setBulkNotice(null);
    const updated: Device[] = [];
    const failed: string[] = [];
    for (const id of ids) {
      const hostname = devices.find((d) => d.id === id)?.hostname || id;
      try {
        const res = await api.patch<Device>(`/devices/${id}`, fields);
        updated.push(res.data);
      } catch (err: any) {
        failed.push(`${hostname} (${extractErrorMessage(err, "failed")})`);
      }
    }
    if (updated.length) {
      setDevices((prev) => prev.map((d) => updated.find((u) => u.id === d.id) || d));
      setBulkNotice(`${label} applied to ${updated.length} device${updated.length === 1 ? "" : "s"}.`);
    }
    if (failed.length) setBulkError(`Failed for: ${failed.join("; ")}`);
    setBulkBusy(false);
  };

  const bulkAssignSite = () => {
    if (!bulkSiteValue.trim()) return;
    bulkPatch({ site: bulkSiteValue.trim() }, `Site "${bulkSiteValue.trim()}"`);
  };

  const bulkSetSnmp = (enabled: boolean) =>
    bulkPatch({ supports_snmp: enabled }, `SNMP monitoring ${enabled ? "enabled" : "disabled"}`);

  const bulkTagVendor = () => bulkPatch({ vendor: bulkVendorValue }, `Vendor "${bulkVendorValue}"`);

  /** Bulk move to a Data Center / Rack — same pair the Groups page's
   * drag-and-drop writes, so a rack full of devices can be relocated in
   * one shot instead of dragging them one at a time. Rack alone (no DC
   * change) is a valid use too -- e.g. re-numbering racks within a site. */
  const bulkMoveRack = () => {
    if (!bulkDcValue.trim() && !bulkRackValue.trim()) return;
    const fields: Record<string, unknown> = {};
    if (bulkDcValue.trim()) fields.data_center = bulkDcValue.trim();
    if (bulkRackValue.trim()) fields.rack = bulkRackValue.trim();
    const label = [bulkDcValue.trim() && `DC "${bulkDcValue.trim()}"`, bulkRackValue.trim() && `Rack "${bulkRackValue.trim()}"`]
      .filter(Boolean)
      .join(" / ");
    bulkPatch(fields, label);
  };

  /** Bulk-assign device_role (core/distribution/access/etc.) -- the same
   * field the hierarchical topology layout groups devices by, so tagging
   * a batch of newly-added switches here immediately places them in the
   * right tier on that view. */
  const bulkAssignRole = () => {
    if (!bulkRoleValue.trim()) return;
    bulkPatch({ device_role: bulkRoleValue.trim() }, `Role "${bulkRoleValue.trim()}"`);
  };

  /** Bulk-add tags (union with each device's existing tags, not a
   * replace) -- mirrors the backend's default "add" mode for
   * POST /devices/bulk assign_tags, done here via the same per-device
   * PATCH loop as the other bulk actions so it shares one error/notice
   * UX with them instead of a second code path. */
  const [bulkTagsValue, setBulkTagsValue] = useState("");
  const bulkAssignTags = () => {
    const newTags = bulkTagsValue
      .split(",")
      .map((t) => t.trim())
      .filter(Boolean);
    if (newTags.length === 0) return;
    const ids = Array.from(selectedIds);
    if (ids.length === 0) return;
    setBulkBusy(true);
    setBulkError(null);
    setBulkNotice(null);
    (async () => {
      const updated: Device[] = [];
      const failed: string[] = [];
      for (const id of ids) {
        const device = devices.find((d) => d.id === id);
        const hostname = device?.hostname || id;
        const merged = Array.from(new Set([...(device?.tags || []), ...newTags]));
        try {
          const res = await api.patch<Device>(`/devices/${id}`, { tags: merged });
          updated.push(res.data);
        } catch (err: any) {
          failed.push(`${hostname} (${extractErrorMessage(err, "failed")})`);
        }
      }
      if (updated.length) {
        setDevices((prev) => prev.map((d) => updated.find((u) => u.id === d.id) || d));
        setBulkNotice(`Tags [${newTags.join(", ")}] applied to ${updated.length} device${updated.length === 1 ? "" : "s"}.`);
        setBulkTagsValue("");
      }
      if (failed.length) setBulkError(`Failed for: ${failed.join("; ")}`);
      setBulkBusy(false);
    })();
  };

  /** Bulk lifecycle-state transition (staging -> production ->
   * decommissioned), e.g. cutting a batch of newly-added devices over
   * to production together, or marking a retired batch decommissioned
   * without deleting their history. */
  const [bulkLifecycleValue, setBulkLifecycleValue] = useState<DeviceLifecycleState>("production");
  const bulkSetLifecycle = () => bulkPatch({ lifecycle_state: bulkLifecycleValue }, `Lifecycle "${bulkLifecycleValue}"`);

  /** Deletes one device, transparently retrying with force=true (after an
   * explicit confirm listing what would be destroyed) if the backend
   * blocks the plain delete because the device has change/deployment
   * history. Shared by the single-row Remove button and bulk delete so
   * both paths behave identically.
   */
  const deleteDeviceWithForceConfirm = async (
    id: string,
    hostname: string
  ): Promise<{ result: "deleted" | "blocked-declined" | "failed"; detail?: string }> => {
    try {
      await api.delete(`/devices/${id}`);
      return { result: "deleted" };
    } catch (err: any) {
      if (err?.response?.status === 409) {
        const counts = err.response.data?.detail?.counts;
        const summary = describeBlockingCounts(counts);
        const proceed = await confirm(
          `'${hostname}' has change/deployment history (${summary}) and can't be removed without confirmation.\n\nPermanently delete it along with this history? This cannot be undone.`,
          { confirmLabel: "Delete anyway" }
        );
        if (!proceed) return { result: "blocked-declined" };
        try {
          await api.delete(`/devices/${id}?force=true`);
          return { result: "deleted" };
        } catch (err2: any) {
          return { result: "failed", detail: extractErrorMessage(err2, err2?.message) };
        }
      }
      return { result: "failed", detail: extractErrorMessage(err, err?.message) };
    }
  };

  const bulkDelete = async () => {
    const ids = Array.from(selectedIds);
    if (ids.length === 0) return;
    const hostnames = ids.map((id) => devices.find((d) => d.id === id)?.hostname || id);
    if (
      !(await confirm(
        `Remove ${ids.length} device${ids.length === 1 ? "" : "s"} from inventory? This cannot be undone.\n\n${hostnames.join(", ")}`,
        { confirmLabel: "Remove" }
      ))
    )
      return;

    setBulkBusy(true);
    setBulkError(null);
    setBulkNotice(null);
    const deleted: string[] = [];
    const declined: string[] = [];
    const failed: string[] = [];
    let lastFailDetail: string | undefined;

    for (const id of ids) {
      const hostname = devices.find((d) => d.id === id)?.hostname || id;
      const { result, detail } = await deleteDeviceWithForceConfirm(id, hostname);
      if (result === "deleted") deleted.push(id);
      else if (result === "blocked-declined") declined.push(hostname);
      else {
        failed.push(hostname);
        if (detail) lastFailDetail = detail;
      }
    }

    if (deleted.length) {
      setDevices((prev) => prev.filter((d) => !deleted.includes(d.id)));
      setSelectedIds((prev) => {
        const next = new Set(prev);
        deleted.forEach((id) => next.delete(id));
        return next;
      });
      if (deleted.some((id) => id === expandedDeviceId)) setExpandedDeviceId(null);
    }
    const notices: string[] = [];
    if (deleted.length) notices.push(`Removed ${deleted.length} device${deleted.length === 1 ? "" : "s"}.`);
    if (notices.length) setBulkNotice(notices.join(" "));
    const problems: string[] = [];
    if (declined.length) problems.push(`Kept (declined): ${declined.join(", ")}`);
    if (failed.length) problems.push(`Failed: ${failed.join(", ")}${lastFailDetail ? ` — ${lastFailDetail}` : ""}`);
    if (problems.length) setBulkError(problems.join(" · "));
    setBulkBusy(false);
  };

  const confirmRollback = async () => {
    if (!rollbackTarget) return;
    if (rollbackMode === "partial" && !rollbackSectionKey) return;
    setRollbackSubmitting(true);
    setRollbackError(null);
    try {
      const res =
        rollbackMode === "partial"
          ? await api.post(`/devices/${rollbackTarget.device.id}/rollback/partial`, {
              snapshot_id: rollbackTarget.snapshot.id,
              section_key: rollbackSectionKey,
              reason: rollbackReason || undefined,
            })
          : await api.post(`/devices/${rollbackTarget.device.id}/rollback`, {
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

  // --- CSV bulk import/export (for orgs without NetBox) ---
  const [csvBusy, setCsvBusy] = useState(false);
  const [csvImportResult, setCsvImportResult] = useState<DeviceCsvImportResult | null>(null);
  const csvFileInputRef = React.useRef<HTMLInputElement>(null);

  const exportDevicesCsv = async () => {
    setCsvBusy(true);
    try {
      const res = await api.get<string>("/devices/export", { responseType: "blob" as any });
      const blob = new Blob([res.data], { type: "text/csv" });
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "devices_export.csv";
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.URL.revokeObjectURL(url);
    } catch {
      setError("Failed to export devices.");
    } finally {
      setCsvBusy(false);
    }
  };

  const importDevicesCsv = async (file: File) => {
    setCsvBusy(true);
    setCsvImportResult(null);
    setError(null);
    try {
      const formData = new FormData();
      formData.append("file", file);
      const res = await api.post<DeviceCsvImportResult>("/devices/import", formData, {
        headers: { "Content-Type": "multipart/form-data" },
      });
      setCsvImportResult(res.data);
      load();
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to import CSV.");
    } finally {
      setCsvBusy(false);
      if (csvFileInputRef.current) csvFileInputRef.current.value = "";
    }
  };

  useEffect(() => {
    setFleetHealthLoading(true);
    api
      .get<FleetHealthSummary>("/metrics/health-summary", {
        params: vendorFilter !== "all" ? { vendor: vendorFilter } : undefined,
      })
      .then((res) => setFleetHealth(res.data))
      .catch(() => setFleetHealth(null))
      .finally(() => setFleetHealthLoading(false));
  }, [vendorFilter]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    const payload = {
      ...form,
      snmp_port: form.snmp_port ? Number(form.snmp_port) : null,
      netconf_port: form.netconf_port ? Number(form.netconf_port) : null,
      rack_position: form.rack_position ? Number(form.rack_position) : null,
    };
    try {
      if (editingDeviceId) {
        const res = await api.patch<Device>(`/devices/${editingDeviceId}`, payload);
        setDevices((prev) => prev.map((d) => (d.id === editingDeviceId ? res.data : d)));
      } else {
        await api.post("/devices", payload);
      }
      setForm(emptyForm);
      setShowForm(false);
      setEditingDeviceId(null);
      load();
    } catch (err: any) {
      setError(err?.response?.data?.detail || `Failed to ${editingDeviceId ? "update" : "create"} device.`);
    } finally {
      setLoading(false);
    }
  };

  const startEdit = (d: Device) => {
    setEditingDeviceId(d.id);
    setForm({
      hostname: d.hostname || "",
      ip_address: d.ip_address || "",
      vendor: d.vendor || "cisco",
      site: d.site || "",
      device_role: d.device_role || "",
      data_center: d.data_center || "",
      rack: d.rack || "",
      rack_position: d.rack_position != null ? String(d.rack_position) : "",
      ssh_username: d.ssh_username || "",
      ssh_credential_ref: d.ssh_credential_ref || "",
      supports_snmp: !!d.supports_snmp,
      snmp_version: d.snmp_version || "v2c",
      snmp_port: d.snmp_port != null ? String(d.snmp_port) : "161",
      snmp_community_ref: d.snmp_community_ref || "",
      snmp_username: d.snmp_username || "",
      snmp_security_level: d.snmp_security_level || "authPriv",
      snmp_auth_protocol: d.snmp_auth_protocol || "SHA",
      snmp_priv_protocol: d.snmp_priv_protocol || "AES128",
      supports_netconf: !!d.supports_netconf,
      netconf_port: d.netconf_port != null ? String(d.netconf_port) : "830",
      netconf_use_lock: true,
      supports_restconf: !!d.supports_restconf,
      restconf_url: d.restconf_url || "",
    });
    setShowForm(true);
    setExpandedDeviceId(null);
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const cancelForm = () => {
    setShowForm(false);
    setEditingDeviceId(null);
    setForm(emptyForm);
  };

  const removeDevice = async (id: string, hostname: string) => {
    if (!(await confirm(`Remove ${hostname} from inventory? This cannot be undone.`, { confirmLabel: "Remove" }))) return;
    setDeletingId(id);
    setError(null);
    const { result, detail } = await deleteDeviceWithForceConfirm(id, hostname);
    if (result === "deleted") {
      setDevices((prev) => prev.filter((d) => d.id !== id));
      if (expandedDeviceId === id) setExpandedDeviceId(null);
      setSelectedIds((prev) => {
        if (!prev.has(id)) return prev;
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    } else if (result === "failed") {
      setError(`Failed to remove ${hostname}.${detail ? ` ${detail}` : ""}`);
    }
    setDeletingId(null);
  };

  const clearUnstableFlag = async (id: string, hostname: string) => {
    if (
      !(await confirm(
        `Clear the unstable flag for ${hostname}? This re-enables automated deploys — only do this after reviewing why it kept failing.`,
        { danger: false, confirmLabel: "Clear flag" }
      ))
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
  const dataCenters = useMemo(() => Array.from(new Set(devices.map((d) => d.data_center).filter(Boolean))), [devices]);
  const racks = useMemo(() => Array.from(new Set(devices.map((d) => d.rack).filter(Boolean))), [devices]);
  const roles = useMemo(() => Array.from(new Set(devices.map((d) => d.device_role).filter(Boolean))), [devices]);

  // --- Row virtualization ---------------------------------------------
  // NOC fleets can run into the thousands of devices; rendering every
  // <tr> (each with badges, sparkline-adjacent status pills, and an
  // expandable detail panel) gets sluggish well before that. Past
  // VIRTUALIZE_THRESHOLD rows we switch the table body to a scrollable
  // pane and only mount the rows currently in (or near) the viewport,
  // padded out with spacer rows so scrollbar height/position stays
  // correct. Below the threshold -- the common case -- this is a no-op
  // and the table renders exactly as it always has.
  const ROW_HEIGHT = 64; // approx rendered height of a collapsed row (py-4 + text)
  const VIRTUALIZE_THRESHOLD = 150;
  const OVERSCAN_ROWS = 12;
  const ESTIMATED_VIEWPORT_PX = 900; // ~ generous max-h-[70vh] on common screens
  const tableScrollRef = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return devices.filter((d) => {
      if (vendorFilter !== "all" && d.vendor !== vendorFilter) return false;
      if (dcFilter !== "all" && d.data_center !== dcFilter) return false;
      if (rackFilter !== "all" && d.rack !== rackFilter) return false;
      if (roleFilter !== "all" && d.device_role !== roleFilter) return false;
      if (!q) return true;
      return (
        d.hostname.toLowerCase().includes(q) ||
        d.ip_address.toLowerCase().includes(q) ||
        (d.site || "").toLowerCase().includes(q) ||
        (d.data_center || "").toLowerCase().includes(q) ||
        (d.rack || "").toLowerCase().includes(q)
      );
    });
  }, [devices, query, vendorFilter, dcFilter, rackFilter, roleFilter]);

  // A row is expanded inline (detail panel pushes rows below it down),
  // which breaks the fixed-row-height spacer math. Previously this fell
  // back to rendering the full unvirtualized list whenever any row was
  // expanded -- on a multi-thousand-device fleet that meant clicking a
  // single row to inspect it froze the page. Instead: keep virtualizing,
  // but force the expanded device's index into the rendered window (even
  // if it's currently scrolled out of view -- expanding a row shouldn't
  // make it vanish), and pad the spacer math with one row's worth of
  // estimated extra height for the panel. This is an approximation, same
  // as ROW_HEIGHT itself is -- the panel's real height varies by tab/data
  // -- so the scrollbar can be off by a bit near the expanded row, but
  // that's a minor cosmetic gap versus thousands of mounted rows.
  const ESTIMATED_EXPANDED_PANEL_PX = 640;
  const shouldVirtualize = filtered.length > VIRTUALIZE_THRESHOLD;
  const expandedIndex = expandedDeviceId
    ? filtered.findIndex((d) => d.id === expandedDeviceId)
    : -1;
  let virtualStartIndex = shouldVirtualize
    ? Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - OVERSCAN_ROWS)
    : 0;
  let virtualEndIndex = shouldVirtualize
    ? Math.min(filtered.length, Math.ceil((scrollTop + ESTIMATED_VIEWPORT_PX) / ROW_HEIGHT) + OVERSCAN_ROWS)
    : filtered.length;
  if (shouldVirtualize && expandedIndex >= 0) {
    virtualStartIndex = Math.min(virtualStartIndex, expandedIndex);
    virtualEndIndex = Math.max(virtualEndIndex, expandedIndex + 1);
  }
  const visibleDevices = shouldVirtualize ? filtered.slice(virtualStartIndex, virtualEndIndex) : filtered;
  const topSpacerPx = shouldVirtualize ? virtualStartIndex * ROW_HEIGHT : 0;
  const expandedInWindow = expandedIndex >= virtualStartIndex && expandedIndex < virtualEndIndex;
  const bottomSpacerPx = shouldVirtualize
    ? (filtered.length - virtualEndIndex) * ROW_HEIGHT + (expandedInWindow ? ESTIMATED_EXPANDED_PANEL_PX : 0)
    : 0;

  const counts = useMemo(() => {
    const c = { online: 0, offline: 0, degraded: 0, unknown: 0 };
    devices.forEach((d) => (c[d.status] = (c[d.status] ?? 0) + 1));
    return c;
  }, [devices]);

  return (
    <div className="pb-16 flex flex-col gap-6 md:p-2">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-3xl font-bold text-navy dark:text-white">Device Inventory</h1>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">Centralized inventory of managed network devices.</p>
        </div>
        <div className="flex items-center gap-3">
            <button
            onClick={load}
            className="text-brandblue font-medium hover:text-navy dark:hover:text-white bg-white dark:bg-slate-800 border border-brandblue hover:bg-slate-50 dark:hover:bg-slate-700 px-3 py-1.5 rounded-full transition shadow-sm text-xs"
            >
            ↻ Refresh
            </button>
            <button
              onClick={exportDevicesCsv}
              disabled={csvBusy}
              className="text-brandblue font-medium hover:text-navy dark:hover:text-white bg-white dark:bg-slate-800 border border-brandblue hover:bg-slate-50 dark:hover:bg-slate-700 px-3 py-1.5 rounded-full transition shadow-sm text-xs disabled:opacity-50"
            >
              ⭳ Export CSV
            </button>
            {canManage && (
              <>
                <input
                  ref={csvFileInputRef}
                  type="file"
                  accept=".csv,text/csv"
                  className="hidden"
                  onChange={(e) => e.target.files?.[0] && importDevicesCsv(e.target.files[0])}
                />
                <button
                  onClick={() => csvFileInputRef.current?.click()}
                  disabled={csvBusy}
                  className="text-brandblue font-medium hover:text-navy dark:hover:text-white bg-white dark:bg-slate-800 border border-brandblue hover:bg-slate-50 dark:hover:bg-slate-700 px-3 py-1.5 rounded-full transition shadow-sm text-xs disabled:opacity-50"
                >
                  {csvBusy ? "Working…" : "⭱ Import CSV"}
                </button>
              </>
            )}
            {canManage && (
            <button
                onClick={() => (showForm ? cancelForm() : setShowForm(true))}
                className="bg-brandblue text-white rounded-full px-4 py-1.5 text-xs font-semibold shadow-sm hover:bg-navy transition-colors scale-100 active:scale-95"
            >
                {showForm ? "Cancel" : "+ Add Device"}
            </button>
            )}
        </div>
      </div>

      {csvImportResult && (
        <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl px-4 py-3 text-xs flex flex-col gap-1 shadow-sm">
          <div className="flex items-center justify-between">
            <span className="font-semibold text-navy dark:text-white">
              CSV import: {csvImportResult.created.length} created, {csvImportResult.updated.length} updated
              {csvImportResult.errors.length > 0 && `, ${csvImportResult.errors.length} error(s)`} (of{" "}
              {csvImportResult.total_rows} rows)
            </span>
            <button onClick={() => setCsvImportResult(null)} className="text-slate-400 hover:text-slate-600">
              ✕
            </button>
          </div>
          {csvImportResult.errors.length > 0 && (
            <ul className="text-riskcrit list-disc list-inside">
              {csvImportResult.errors.map((e, i) => (
                <li key={i}>
                  Row {e.row}{e.hostname ? ` (${e.hostname})` : ""}: {e.error}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      <div className="flex flex-wrap gap-3">
        <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 shadow-sm rounded-lg px-4 py-2 text-[13px] text-slate-500 dark:text-slate-400">
          Total <span className="text-navy dark:text-white font-bold ml-1">{devices.length}</span>
        </div>
        <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 shadow-sm rounded-lg px-4 py-2 text-[13px] text-slate-500 dark:text-slate-400 flex items-center gap-1.5">
          <span className="w-2.5 h-2.5 rounded-full bg-risklow shadow-sm" /> Online
          <span className="text-navy dark:text-white font-bold ml-1">{counts.online}</span>
        </div>
        <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 shadow-sm rounded-lg px-4 py-2 text-[13px] text-slate-500 dark:text-slate-400 flex items-center gap-1.5">
          <span className="w-2.5 h-2.5 rounded-full bg-riskmed shadow-sm" /> Degraded
          <span className="text-navy dark:text-white font-bold ml-1">{counts.degraded}</span>
        </div>
        <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 shadow-sm rounded-lg px-4 py-2 text-[13px] text-slate-500 dark:text-slate-400 flex items-center gap-1.5">
          <span className="w-2.5 h-2.5 rounded-full bg-slate-400 shadow-sm" /> Offline
          <span className="text-navy dark:text-white font-bold ml-1">{counts.offline}</span>
        </div>
      </div>

      {canManage && showForm && (
        <form onSubmit={submit} className="bg-white dark:bg-slate-800 border-2 border-brandblue/30 rounded-xl p-5 shadow-sm">
          <div className="flex items-center justify-between mb-1">
            <h3 className="text-sm font-bold text-navy dark:text-white">
              {editingDeviceId ? `Edit Device — ${devices.find((d) => d.id === editingDeviceId)?.hostname || ""}` : "Add Device"}
            </h3>
            {editingDeviceId && (
              <button
                type="button"
                onClick={cancelForm}
                className="text-[11px] uppercase tracking-wider font-bold text-slate-400 hover:text-navy dark:hover:text-white"
              >
                ✕ Cancel Edit
              </button>
            )}
          </div>
          <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
            <input
              className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brandblue"
              placeholder="Hostname (e.g. RTR-01)"
              value={form.hostname}
              onChange={(e) => setForm({ ...form, hostname: e.target.value })}
              required
            />
            <input
              className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brandblue"
              placeholder="IP Address"
              value={form.ip_address}
              onChange={(e) => setForm({ ...form, ip_address: e.target.value })}
              required
            />
            <select
              className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brandblue"
              value={form.vendor}
              onChange={(e) => setForm({ ...form, vendor: e.target.value })}
            >
              <option value="cisco">Cisco</option>
              <option value="juniper">Juniper</option>
              <option value="arista">Arista</option>
              <option value="linux">Linux</option>
            </select>
            <input
              className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brandblue"
              placeholder="Site (optional)"
              value={form.site}
              onChange={(e) => setForm({ ...form, site: e.target.value })}
            />
            <input
              list="device-role-options"
              className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brandblue"
              placeholder="Device Role (e.g. core, access — optional)"
              title="Selects which compliance baseline template (Drift page) this device is checked against, so a core switch and an access switch aren't judged against the same expected config."
              value={form.device_role}
              onChange={(e) => setForm({ ...form, device_role: e.target.value })}
            />
            <datalist id="device-role-options">
              <option value="core" />
              <option value="distribution" />
              <option value="access" />
              <option value="edge-firewall" />
              <option value="wan-edge" />
            </datalist>
            <input
              className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brandblue"
              placeholder="Data Center (optional)"
              title="Top-level grouping for the Topology page's Data Center / Rack view."
              value={form.data_center}
              onChange={(e) => setForm({ ...form, data_center: e.target.value })}
            />
            <input
              className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brandblue"
              placeholder="Rack (optional)"
              title="Rack this device is mounted in, nested under Data Center in the Topology grouping view."
              value={form.rack}
              onChange={(e) => setForm({ ...form, rack: e.target.value })}
            />
            <input
              type="number"
              min={1}
              className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brandblue"
              placeholder="Rack position (U, optional)"
              title="Sort order top-to-bottom within the rack view -- cosmetic only."
              value={form.rack_position}
              onChange={(e) => setForm({ ...form, rack_position: e.target.value })}
            />
          </div>

          <div className="grid grid-cols-1 md:grid-cols-4 gap-3 mt-3">
            <input
              className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brandblue"
              placeholder="SSH Username"
              value={form.ssh_username}
              onChange={(e) => setForm({ ...form, ssh_username: e.target.value })}
              required
            />
            <div className="md:col-span-2">
              <input
                className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brandblue"
                placeholder="SSH Credential Ref (legacy/optional)"
                value={form.ssh_credential_ref}
                onChange={(e) => setForm({ ...form, ssh_credential_ref: e.target.value })}
              />
            </div>
          </div>
          <p className="text-xs text-slate-400 dark:text-slate-500 mt-1">
            The SSH password itself is set after saving, via the 🔑 SSH Credentials button on the device's
            Overview tab — they're stored encrypted, not as a plain form field.
          </p>

          <div className="grid grid-cols-1 md:grid-cols-4 gap-3 mt-3 pt-3 border-t border-slate-100 dark:border-slate-800">
            <label className="flex items-center gap-2 text-xs font-medium text-slate-600 dark:text-slate-300">
              <input
                type="checkbox"
                checked={form.supports_snmp}
                onChange={(e) => setForm({ ...form, supports_snmp: e.target.checked })}
              />
              Enable SNMP monitoring
            </label>
            {form.supports_snmp && (
              <>
                <select
                  className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brandblue"
                  value={form.snmp_version}
                  onChange={(e) => setForm({ ...form, snmp_version: e.target.value })}
                >
                  <option value="v1">SNMP v1</option>
                  <option value="v2c">SNMP v2c</option>
                  <option value="v3">SNMP v3</option>
                </select>
                <input
                  className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brandblue"
                  placeholder="Port"
                  type="number"
                  value={form.snmp_port}
                  onChange={(e) => setForm({ ...form, snmp_port: e.target.value })}
                />
                {form.snmp_version !== "v3" ? (
                  <div className="md:col-span-2">
                    <input
                      className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brandblue"
                      placeholder="SNMP Community Credential Ref (legacy env-var fallback — or set the community itself after creating the device via 🔑 Credentials)"
                      value={form.snmp_community_ref}
                      onChange={(e) => setForm({ ...form, snmp_community_ref: e.target.value })}
                    />
                  </div>
                ) : (
                  <div className="md:col-span-2">
                    <input
                      className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brandblue"
                      placeholder="SNMPv3 Username"
                      value={form.snmp_username}
                      onChange={(e) => setForm({ ...form, snmp_username: e.target.value })}
                    />
                  </div>
                )}
                {form.snmp_version === "v3" && (
                  <>
                    <select
                      className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brandblue"
                      value={form.snmp_security_level}
                      onChange={(e) => setForm({ ...form, snmp_security_level: e.target.value })}
                    >
                      <option value="noAuthNoPriv">noAuthNoPriv</option>
                      <option value="authNoPriv">authNoPriv</option>
                      <option value="authPriv">authPriv</option>
                    </select>
                    {form.snmp_security_level !== "noAuthNoPriv" && (
                      <select
                        className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brandblue"
                        value={form.snmp_auth_protocol}
                        onChange={(e) => setForm({ ...form, snmp_auth_protocol: e.target.value })}
                      >
                        {["MD5", "SHA", "SHA224", "SHA256", "SHA384", "SHA512"].map((p) => (
                          <option key={p} value={p}>{p}</option>
                        ))}
                      </select>
                    )}
                    {form.snmp_security_level === "authPriv" && (
                      <select
                        className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brandblue"
                        value={form.snmp_priv_protocol}
                        onChange={(e) => setForm({ ...form, snmp_priv_protocol: e.target.value })}
                      >
                        {["DES", "3DES", "AES128", "AES192", "AES256"].map((p) => (
                          <option key={p} value={p}>{p}</option>
                        ))}
                      </select>
                    )}
                    <p className="md:col-span-4 text-[11px] text-slate-400 dark:text-slate-500 italic">
                      Set the actual auth/privacy passphrases after creating the device, via the 🔑 Credentials
                      button on its Health tab — they're stored encrypted, not as plain form fields.
                    </p>
                  </>
                )}
              </>
            )}
          </div>

          <div className="grid grid-cols-1 md:grid-cols-4 gap-3 mt-3">
            <label className="flex items-center gap-2 text-xs font-medium text-slate-600 dark:text-slate-300">
              <input
                type="checkbox"
                checked={form.supports_netconf}
                onChange={(e) => setForm({ ...form, supports_netconf: e.target.checked })}
              />
              Enable NETCONF
            </label>
            {form.supports_netconf && (
              <input
                className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brandblue"
                placeholder="NETCONF Port (default 830)"
                type="number"
                value={form.netconf_port}
                onChange={(e) => setForm({ ...form, netconf_port: e.target.value })}
              />
            )}
            {form.supports_netconf && (
              <label className="flex items-center gap-2 text-xs font-medium text-slate-600 dark:text-slate-300" title="Turn off if this device's NETCONF agent doesn't support <lock> (or rejects it) -- otherwise every push/restore fails at the lock step.">
                <input
                  type="checkbox"
                  checked={form.netconf_use_lock}
                  onChange={(e) => setForm({ ...form, netconf_use_lock: e.target.checked })}
                />
                Lock datastore on push
              </label>
            )}
            <label className="flex items-center gap-2 text-xs font-medium text-slate-600 dark:text-slate-300">
              <input
                type="checkbox"
                checked={form.supports_restconf}
                onChange={(e) => setForm({ ...form, supports_restconf: e.target.checked })}
              />
              Enable RESTCONF
            </label>
            {form.supports_restconf && (
              <div className="md:col-span-2">
                <input
                  className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brandblue"
                  placeholder="RESTCONF URL (e.g. https://10.0.0.1/restconf)"
                  value={form.restconf_url}
                  onChange={(e) => setForm({ ...form, restconf_url: e.target.value })}
                />
              </div>
            )}
            <button
              type="submit"
              disabled={loading}
              className="bg-brandblue text-white rounded-lg px-4 py-2 text-sm font-semibold shadow hover:bg-navy transition-colors disabled:opacity-50 h-fit self-start md:col-start-4"
            >
              {loading ? (editingDeviceId ? "Saving…" : "Adding…") : editingDeviceId ? "Save Changes" : "Add Device"}
            </button>
          </div>
        </form>
      )}

      {error && <p className="text-riskcrit font-semibold text-sm bg-red-50 dark:bg-red-950/40 border border-red-200 dark:border-red-800 px-3 py-2 rounded-lg">{error}</p>}
      {rollbackNotice && (
        <p className="text-[13px] font-medium text-brandblue bg-blue-50 dark:bg-blue-950/40 border border-blue-200 dark:border-blue-800 shadow-sm rounded-lg px-4 py-2.5">
          {rollbackNotice}{" "}
          <button onClick={() => setRollbackNotice(null)} className="ml-3 font-bold text-slate-400 dark:text-slate-500 hover:text-navy dark:hover:text-white">
            ✕
          </button>
        </p>
      )}

      <div className="flex flex-wrap gap-2 items-center">
        <input
          className="border border-slate-300 dark:border-slate-600 shadow-sm rounded-full px-4 py-1.5 text-sm w-full max-w-sm focus:ring-2 focus:ring-brandblue focus:border-transparent outline-none"
          placeholder="Search hostname, IP, or site…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <select
          className="border border-slate-300 dark:border-slate-600 shadow-sm rounded-full px-4 py-1.5 text-sm text-slate-600 dark:text-slate-300 focus:ring-2 focus:ring-brandblue outline-none"
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
        {(dataCenters as string[]).length > 0 && (
          <select
            className="border border-slate-300 dark:border-slate-600 shadow-sm rounded-full px-4 py-1.5 text-sm text-slate-600 dark:text-slate-300 focus:ring-2 focus:ring-brandblue outline-none"
            value={dcFilter}
            onChange={(e) => setDcFilter(e.target.value)}
          >
            <option value="all">All DCs</option>
            {(dataCenters as string[]).map((dc) => (
              <option key={dc} value={dc}>{dc}</option>
            ))}
          </select>
        )}
        {(racks as string[]).length > 0 && (
          <select
            className="border border-slate-300 dark:border-slate-600 shadow-sm rounded-full px-4 py-1.5 text-sm text-slate-600 dark:text-slate-300 focus:ring-2 focus:ring-brandblue outline-none"
            value={rackFilter}
            onChange={(e) => setRackFilter(e.target.value)}
          >
            <option value="all">All Racks</option>
            {(racks as string[]).map((r) => (
              <option key={r} value={r}>{r}</option>
            ))}
          </select>
        )}
        {(roles as string[]).length > 0 && (
          <select
            className="border border-slate-300 dark:border-slate-600 shadow-sm rounded-full px-4 py-1.5 text-sm text-slate-600 dark:text-slate-300 focus:ring-2 focus:ring-brandblue outline-none"
            value={roleFilter}
            onChange={(e) => setRoleFilter(e.target.value)}
          >
            <option value="all">All Roles</option>
            {(roles as string[]).map((r) => (
              <option key={r} value={r} className="capitalize">{r}</option>
            ))}
          </select>
        )}
        <SavedViews
          storageKey="netguard_saved_views_devices"
          currentFilters={{ query, vendorFilter, dcFilter, rackFilter, roleFilter }}
          isDefault={(f) => !f.query && f.vendorFilter === "all" && f.dcFilter === "all" && f.rackFilter === "all" && f.roleFilter === "all"}
          onApply={(f) => {
            setQuery(f.query);
            setVendorFilter(f.vendorFilter);
            setDcFilter(f.dcFilter);
            setRackFilter(f.rackFilter);
            setRoleFilter(f.roleFilter);
          }}
        />
      </div>

      {/* Fleet Health strip -- scoped to vendorFilter above, so switching
          the dropdown to e.g. "juniper" turns this into a Juniper-only
          fleet health rollup (GET /metrics/health-summary?vendor=juniper)
          without a separate page. */}
      {fleetHealth && fleetHealth.devices_monitored > 0 && (
        <div className="flex flex-wrap items-center gap-4 text-xs font-medium bg-slate-50 dark:bg-slate-800/60 border border-slate-200 dark:border-slate-700 rounded-xl px-4 py-2.5">
          <span className="font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wide">
            {vendorFilter === "all" ? "Fleet health" : `${vendorFilter} fleet health`}
          </span>
          <span className="flex items-center gap-1.5">
            <span className="w-2 h-2 rounded-full bg-risklow" /> {fleetHealth.green} green
          </span>
          <span className="flex items-center gap-1.5">
            <span className="w-2 h-2 rounded-full bg-riskmed" /> {fleetHealth.yellow} yellow
          </span>
          <span className="flex items-center gap-1.5">
            <span className="w-2 h-2 rounded-full bg-riskcrit" /> {fleetHealth.red} red
          </span>
          <span className="flex items-center gap-1.5 text-slate-400 dark:text-slate-500">
            <span className="w-2 h-2 rounded-full bg-slate-400" /> {fleetHealth.unknown} unknown
          </span>
          {fleetHealth.average_health_score != null && (
            <span className="text-slate-500 dark:text-slate-400">
              avg score {fleetHealth.average_health_score}/100
            </span>
          )}
          {fleetHealth.devices_with_stale_metrics > 0 && (
            <span className="ml-auto flex items-center gap-1.5 text-amber-600 dark:text-amber-400" title="These devices have at least one metric (CPU/memory/interface/temperature/fan/power) that hasn't successfully resolved in a while, even if their overall color is green.">
              ⚠ {fleetHealth.devices_with_stale_metrics} device{fleetHealth.devices_with_stale_metrics === 1 ? "" : "s"} with stale metrics
            </span>
          )}
        </div>
      )}
      {fleetHealthLoading && !fleetHealth && (
        <p className="text-xs text-slate-400">Loading fleet health…</p>
      )}

      {bulkNotice && (
        <p className="text-[13px] font-medium text-risklow bg-green-50 dark:bg-green-950/40 border border-green-200 dark:border-green-800 shadow-sm rounded-lg px-4 py-2.5">
          {bulkNotice}{" "}
          <button onClick={() => setBulkNotice(null)} className="ml-3 font-bold text-slate-400 dark:text-slate-500 hover:text-navy dark:hover:text-white">
            ✕
          </button>
        </p>
      )}
      {bulkError && (
        <p className="text-[13px] font-medium text-riskcrit bg-red-50 dark:bg-red-950/40 border border-red-200 dark:border-red-800 shadow-sm rounded-lg px-4 py-2.5">
          {bulkError}{" "}
          <button onClick={() => setBulkError(null)} className="ml-3 font-bold text-slate-400 dark:text-slate-500 hover:text-navy dark:hover:text-white">
            ✕
          </button>
        </p>
      )}

      {canManage && selectedIds.size > 0 && (
        <div className="flex flex-wrap items-center gap-3 bg-navy dark:bg-slate-950 text-white rounded-xl px-4 py-3 shadow-sm">
          <span className="text-sm font-bold whitespace-nowrap">
            {selectedIds.size} selected
          </span>
          <button
            onClick={clearSelection}
            disabled={bulkBusy}
            className="text-xs text-slate-300 hover:text-white underline disabled:opacity-50"
          >
            Clear
          </button>

          <div className="w-px self-stretch bg-white/15" />

          <div className="flex items-center gap-1.5">
            <input
              className="border border-white/20 bg-white/10 placeholder-slate-400 rounded-full px-3 py-1.5 text-xs text-white w-36 focus:ring-2 focus:ring-accent outline-none"
              placeholder="Assign site…"
              value={bulkSiteValue}
              onChange={(e) => setBulkSiteValue(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && bulkAssignSite()}
              disabled={bulkBusy}
            />
            <button
              onClick={bulkAssignSite}
              disabled={bulkBusy || !bulkSiteValue.trim()}
              className="text-xs font-semibold bg-white/10 hover:bg-white/20 px-3 py-1.5 rounded-full disabled:opacity-40"
            >
              Apply
            </button>
          </div>

          <div className="flex items-center gap-1.5">
            <select
              className="border border-white/20 bg-white/10 rounded-full px-3 py-1.5 text-xs text-white focus:ring-2 focus:ring-accent outline-none"
              value={bulkVendorValue}
              onChange={(e) => setBulkVendorValue(e.target.value)}
              disabled={bulkBusy}
            >
              <option value="cisco" className="text-navy">Cisco</option>
              <option value="juniper" className="text-navy">Juniper</option>
              <option value="arista" className="text-navy">Arista</option>
              <option value="linux" className="text-navy">Linux</option>
            </select>
            <button
              onClick={bulkTagVendor}
              disabled={bulkBusy}
              className="text-xs font-semibold bg-white/10 hover:bg-white/20 px-3 py-1.5 rounded-full disabled:opacity-40"
            >
              Tag vendor
            </button>
          </div>

          <button
            onClick={() => bulkSetSnmp(true)}
            disabled={bulkBusy}
            className="text-xs font-semibold bg-white/10 hover:bg-white/20 px-3 py-1.5 rounded-full disabled:opacity-40 whitespace-nowrap"
          >
            Enable SNMP
          </button>
          <button
            onClick={() => bulkSetSnmp(false)}
            disabled={bulkBusy}
            className="text-xs font-semibold bg-white/10 hover:bg-white/20 px-3 py-1.5 rounded-full disabled:opacity-40 whitespace-nowrap"
          >
            Disable SNMP
          </button>

          <div className="w-px self-stretch bg-white/15" />

          <div className="flex items-center gap-1.5">
            <input
              className="border border-white/20 bg-white/10 placeholder-slate-400 rounded-full px-3 py-1.5 text-xs text-white w-28 focus:ring-2 focus:ring-accent outline-none"
              placeholder="Data center…"
              value={bulkDcValue}
              onChange={(e) => setBulkDcValue(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && bulkMoveRack()}
              disabled={bulkBusy}
            />
            <input
              className="border border-white/20 bg-white/10 placeholder-slate-400 rounded-full px-3 py-1.5 text-xs text-white w-24 focus:ring-2 focus:ring-accent outline-none"
              placeholder="Rack…"
              value={bulkRackValue}
              onChange={(e) => setBulkRackValue(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && bulkMoveRack()}
              disabled={bulkBusy}
            />
            <button
              onClick={bulkMoveRack}
              disabled={bulkBusy || (!bulkDcValue.trim() && !bulkRackValue.trim())}
              className="text-xs font-semibold bg-white/10 hover:bg-white/20 px-3 py-1.5 rounded-full disabled:opacity-40 whitespace-nowrap"
            >
              Move
            </button>
          </div>

          <div className="flex items-center gap-1.5">
            <select
              className="border border-white/20 bg-white/10 rounded-full px-3 py-1.5 text-xs text-white focus:ring-2 focus:ring-accent outline-none"
              value={bulkRoleValue}
              onChange={(e) => setBulkRoleValue(e.target.value)}
              disabled={bulkBusy}
            >
              <option value="" className="text-navy">Role…</option>
              <option value="core" className="text-navy">Core</option>
              <option value="distribution" className="text-navy">Distribution</option>
              <option value="access" className="text-navy">Access</option>
              <option value="edge" className="text-navy">Edge</option>
              <option value="firewall" className="text-navy">Firewall</option>
            </select>
            <button
              onClick={bulkAssignRole}
              disabled={bulkBusy || !bulkRoleValue.trim()}
              className="text-xs font-semibold bg-white/10 hover:bg-white/20 px-3 py-1.5 rounded-full disabled:opacity-40 whitespace-nowrap"
            >
              Tag role
            </button>
          </div>

          <div className="flex items-center gap-1.5">
            <input
              className="border border-white/20 bg-white/10 placeholder-slate-400 rounded-full px-3 py-1.5 text-xs text-white w-32 focus:ring-2 focus:ring-accent outline-none"
              placeholder="Add tags (a,b)…"
              value={bulkTagsValue}
              onChange={(e) => setBulkTagsValue(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && bulkAssignTags()}
              disabled={bulkBusy}
            />
            <button
              onClick={bulkAssignTags}
              disabled={bulkBusy || !bulkTagsValue.trim()}
              className="text-xs font-semibold bg-white/10 hover:bg-white/20 px-3 py-1.5 rounded-full disabled:opacity-40 whitespace-nowrap"
            >
              Tag
            </button>
          </div>

          <div className="flex items-center gap-1.5">
            <select
              className="border border-white/20 bg-white/10 rounded-full px-3 py-1.5 text-xs text-white focus:ring-2 focus:ring-accent outline-none"
              value={bulkLifecycleValue}
              onChange={(e) => setBulkLifecycleValue(e.target.value as DeviceLifecycleState)}
              disabled={bulkBusy}
            >
              <option value="staging" className="text-navy">Staging</option>
              <option value="production" className="text-navy">Production</option>
              <option value="decommissioned" className="text-navy">Decommissioned</option>
            </select>
            <button
              onClick={bulkSetLifecycle}
              disabled={bulkBusy}
              className="text-xs font-semibold bg-white/10 hover:bg-white/20 px-3 py-1.5 rounded-full disabled:opacity-40 whitespace-nowrap"
            >
              Set lifecycle
            </button>
          </div>

          <button
            onClick={() => setShowBulkRotateModal(true)}
            disabled={bulkBusy}
            className="text-xs font-semibold bg-white/10 hover:bg-white/20 px-3 py-1.5 rounded-full disabled:opacity-40 whitespace-nowrap"
          >
            Rotate credentials
          </button>

          <button
            onClick={bulkDelete}
            disabled={bulkBusy}
            className="ml-auto text-xs font-bold bg-riskcrit hover:bg-red-700 text-white px-3 py-1.5 rounded-full disabled:opacity-50 whitespace-nowrap"
          >
            {bulkBusy ? "Working…" : `Delete ${selectedIds.size}`}
          </button>
        </div>
      )}

      <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl overflow-hidden shadow-sm">
        {shouldVirtualize && (
          <p className="px-5 py-1.5 text-[11px] text-slate-400 bg-slate-50 dark:bg-slate-900 border-b border-slate-100 dark:border-slate-800">
            Showing {filtered.length.toLocaleString()} devices in a scrollable, virtualized view for performance.
          </p>
        )}
        <div
          ref={tableScrollRef}
          className={shouldVirtualize ? "overflow-x-auto overflow-y-auto max-h-[70vh]" : "overflow-x-auto"}
          onScroll={shouldVirtualize ? (e) => setScrollTop(e.currentTarget.scrollTop) : undefined}
        >
        <table className="w-full text-sm">
          <thead className="bg-slate-100 dark:bg-slate-700 border-b border-slate-200 dark:border-slate-700 sticky top-0 z-10">
            <tr>
              {canManage && (
                <th className="w-10 px-4 py-3.5">
                  <input
                    type="checkbox"
                    aria-label="Select all visible devices"
                    checked={filtered.length > 0 && filtered.every((d) => selectedIds.has(d.id))}
                    ref={(el) => {
                      if (el) {
                        const someSelected = filtered.some((d) => selectedIds.has(d.id));
                        const allSelected = filtered.length > 0 && filtered.every((d) => selectedIds.has(d.id));
                        el.indeterminate = someSelected && !allSelected;
                      }
                    }}
                    onChange={(e) => {
                      e.stopPropagation();
                      setSelectedIds((prev) => {
                        const next = new Set(prev);
                        if (e.target.checked) filtered.forEach((d) => next.add(d.id));
                        else filtered.forEach((d) => next.delete(d.id));
                        return next;
                      });
                    }}
                    className="w-4 h-4 rounded border-slate-300 dark:border-slate-600 text-brandblue focus:ring-brandblue"
                  />
                </th>
              )}
              <th className="text-left px-5 py-3.5 font-bold text-slate-600 dark:text-slate-300 uppercase text-xs tracking-wider">Hostname</th>
              <th className="text-left px-5 py-3.5 font-bold text-slate-600 dark:text-slate-300 uppercase text-xs tracking-wider">IP Address</th>
              <th className="text-left px-5 py-3.5 font-bold text-slate-600 dark:text-slate-300 uppercase text-xs tracking-wider">Vendor</th>
              <th className="text-left px-5 py-3.5 font-bold text-slate-600 dark:text-slate-300 uppercase text-xs tracking-wider">Site</th>
              <th className="text-left px-5 py-3.5 font-bold text-slate-600 dark:text-slate-300 uppercase text-xs tracking-wider">Data Center</th>
              <th className="text-left px-5 py-3.5 font-bold text-slate-600 dark:text-slate-300 uppercase text-xs tracking-wider">Rack / Pos</th>
              <th className="text-left px-5 py-3.5 font-bold text-slate-600 dark:text-slate-300 uppercase text-xs tracking-wider">Status</th>
              <th className="text-left px-5 py-3.5 font-bold text-slate-600 dark:text-slate-300 uppercase text-xs tracking-wider">Details</th>
              {canManage && <th className="text-right px-5 py-3.5 font-bold text-slate-600 dark:text-slate-300 uppercase text-xs tracking-wider">Actions</th>}
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
            {initialLoading && (
              <tr>
                <td colSpan={canManage ? 10 : 8} className="text-center text-slate-500 dark:text-slate-400 py-12">
                   <div className="inline-block w-5 h-5 border-2 border-slate-200 dark:border-slate-700 border-t-brandblue rounded-full animate-spin mb-2" />
                   <p>Loading devices…</p>
                </td>
              </tr>
            )}
            {!initialLoading && filtered.length === 0 && (
              <tr>
                <td colSpan={canManage ? 10 : 8} className="text-center text-slate-400 dark:text-slate-500 py-10 font-medium">
                  {devices.length === 0 ? "No devices yet. Add one above." : "No devices match your search."}
                </td>
              </tr>
            )}
            {shouldVirtualize && topSpacerPx > 0 && (
              <tr aria-hidden="true" style={{ height: topSpacerPx }}>
                <td colSpan={canManage ? 10 : 8} className="p-0 border-0" />
              </tr>
            )}
            {visibleDevices.map((d) => (
              <Fragment key={d.id}>
                <tr className={`cursor-pointer transition-colors hover:bg-slate-50/70 border-l-4 ${
                    expandedDeviceId === d.id ? "bg-slate-50 dark:bg-slate-900 border-l-navy" : "border-l-transparent bg-white dark:bg-slate-800"
                  }`} 
                  onClick={() => setExpandedDeviceId(expandedDeviceId === d.id ? null : d.id)}
                >
                {canManage && (
                  <td className="px-4 py-4" onClick={(e) => e.stopPropagation()}>
                    <input
                      type="checkbox"
                      aria-label={`Select ${d.hostname}`}
                      checked={selectedIds.has(d.id)}
                      onChange={() => toggleSelected(d.id)}
                      className="w-4 h-4 rounded border-slate-300 dark:border-slate-600 text-brandblue focus:ring-brandblue"
                    />
                  </td>
                )}
                <td className="px-5 py-4 font-bold text-navy dark:text-white">{d.hostname}</td>
                <td className="px-5 py-4 text-slate-500 dark:text-slate-400 font-mono text-xs font-semibold">{d.ip_address}</td>
                <td className="px-5 py-4 text-slate-600 dark:text-slate-300 capitalize font-medium">{d.vendor}</td>
                <td className="px-5 py-4 text-slate-600 dark:text-slate-300 font-medium">
                  {d.site || "—"}
                </td>
                <td className="px-5 py-4 text-slate-600 dark:text-slate-300 font-medium">
                  {d.data_center || "—"}
                </td>
                <td className="px-5 py-4 text-slate-600 dark:text-slate-300 font-medium">
                  {d.rack || "—"}
                  {d.rack_position && <span className="text-[10px] text-slate-400 ml-1">U{d.rack_position}</span>}
                </td>
                <td className="px-5 py-4">
                  <span className="inline-flex items-center gap-1.5 bg-slate-50 dark:bg-slate-900 border border-slate-200 dark:border-slate-700 px-2.5 py-1 rounded-full shadow-sm">
                    <span className={`w-2 h-2 rounded-full ${statusColor[d.status]} animate-pulse`} />
                    <span className="capitalize text-[11px] font-bold text-slate-600 dark:text-slate-300 tracking-wide">{d.status}</span>
                  </span>
                  {d.flagged_unstable && (
                    <span
                      className="ml-2 inline-flex items-center gap-1.5 bg-red-50 dark:bg-red-950/40 border border-red-200 dark:border-red-800 text-riskcrit px-2.5 py-1 rounded-full shadow-sm text-[11px] font-bold uppercase tracking-wide"
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
                          className="text-[11px] uppercase tracking-wider text-brandblue border border-blue-200 dark:border-blue-800 bg-blue-50 dark:bg-blue-950/40 px-2 py-1 rounded shadow-sm hover:bg-blue-100 font-bold disabled:opacity-50"
                        >
                          {clearingUnstableId === d.id ? "Wait…" : "Clear Flag"}
                        </button>
                      )}
                      <button
                        onClick={(e) => {
                            e.stopPropagation();
                            setActiveTerminalDevice(d.id);
                        }}
                        className="text-[11px] uppercase tracking-wider text-slate-100 border border-slate-700 bg-slate-800 px-2 py-1 rounded shadow-sm hover:bg-slate-700 font-bold"
                      >
                        Terminal
                      </button>
                      <button
                        onClick={(e) => {
                            e.stopPropagation();
                            startEdit(d);
                        }}
                        className="text-[11px] uppercase tracking-wider text-brandblue border border-blue-200 dark:border-blue-800 bg-blue-50 dark:bg-blue-950/40 px-2 py-1 rounded shadow-sm hover:bg-blue-100 font-bold"
                      >
                        Edit
                      </button>
                      <button
                        onClick={(e) => {
                            e.stopPropagation();
                            removeDevice(d.id, d.hostname);
                        }}
                        disabled={deletingId === d.id}
                        className="text-[11px] uppercase tracking-wider text-riskcrit border border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-950/40 px-2 py-1 rounded shadow-sm hover:bg-red-100 font-bold disabled:opacity-50"
                      >
                        {deletingId === d.id ? "Wait…" : "Remove"}
                      </button>
                    </div>
                  </td>
                )}
                </tr>
                {expandedDeviceId === d.id && (
                  <tr>
                    <td colSpan={canManage ? 10 : 8} className="p-0 border-b-4 border-slate-200 dark:border-slate-700">
                        <DeviceInlineDetails 
                            device={d} 
                            canManage={canManage}
                            onQueueRollback={(snapshot, reason) => {
                                setRollbackTarget({ device: d, snapshot });
                                setRollbackReason(reason);
                            }} 
                            onDeviceUpdated={(updated) =>
                                setDevices((prev) => prev.map((x) => (x.id === updated.id ? updated : x)))
                            }
                        />
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
            {shouldVirtualize && bottomSpacerPx > 0 && (
              <tr aria-hidden="true" style={{ height: bottomSpacerPx }}>
                <td colSpan={canManage ? 10 : 8} className="p-0 border-0" />
              </tr>
            )}
          </tbody>
        </table>
        </div>
      </div>

      {rollbackTarget && (
        <div className="fixed inset-0 bg-navy/60 backdrop-blur-sm flex items-center justify-center z-50 px-4">
          <div className="bg-white dark:bg-slate-800 rounded-2xl shadow-2xl max-w-2xl w-full p-6 max-h-[85vh] overflow-y-auto">
            <h3 className="text-xl font-bold text-navy dark:text-white">Roll back {rollbackTarget.device.hostname}?</h3>

            <div className="flex gap-2 mt-4 border-b border-slate-200 dark:border-slate-700">
              <button
                onClick={() => setRollbackMode("full")}
                className={`px-3 py-2 text-xs font-bold uppercase tracking-wide border-b-2 -mb-px transition-colors ${
                  rollbackMode === "full"
                    ? "border-riskcrit text-riskcrit"
                    : "border-transparent text-slate-400 hover:text-slate-600 dark:hover:text-slate-300"
                }`}
              >
                Full Rollback
              </button>
              <button
                onClick={() => setRollbackMode("partial")}
                className={`px-3 py-2 text-xs font-bold uppercase tracking-wide border-b-2 -mb-px transition-colors ${
                  rollbackMode === "partial"
                    ? "border-riskcrit text-riskcrit"
                    : "border-transparent text-slate-400 hover:text-slate-600 dark:hover:text-slate-300"
                }`}
              >
                Section-Level (Partial)
              </button>
            </div>

            {rollbackMode === "full" ? (
              <>
                <p className="text-[13px] text-slate-500 dark:text-slate-400 mt-3 leading-relaxed">
                  This triggers a full pipeline redeployment restoring snapshot <span className="font-mono font-bold">v{rollbackTarget.snapshot.version}</span> (
                  {new Date(rollbackTarget.snapshot.created_at).toLocaleString()}). Every line of the device's config is replaced.
                </p>

                <div className="mt-4">
                  <h4 className="text-xs font-bold text-slate-600 dark:text-slate-300 uppercase tracking-wide mb-2">
                    Preview: What This Rollback Will Change
                  </h4>
                  {rollbackPreviewLoading ? (
                    <p className="text-xs text-slate-400">Loading preview…</p>
                  ) : rollbackPreviewError ? (
                    <p className="text-xs text-riskcrit">{rollbackPreviewError}</p>
                  ) : rollbackPreview ? (
                    <div>
                      {rollbackPreview.warning && (
                        <p className="text-xs text-amber-700 bg-amber-50 dark:bg-amber-950/30 border border-amber-200 dark:border-amber-900 rounded-lg px-3 py-2 mb-2">
                          {rollbackPreview.warning}
                        </p>
                      )}
                      {rollbackPreview.blocked && (
                        <p className="text-xs text-riskcrit bg-red-50 dark:bg-red-950/30 border border-red-200 dark:border-red-900 rounded-lg px-3 py-2 mb-2 font-semibold">
                          {rollbackPreview.blocked_reason}
                        </p>
                      )}
                      {rollbackPreview.identical ? (
                        <p className="text-xs text-risklow bg-green-50 dark:bg-green-950/20 border border-green-100 dark:border-green-900 rounded-lg px-3 py-2">
                          No difference -- the live configuration already matches this snapshot.
                        </p>
                      ) : (
                        <>
                          <p className="text-[11px] text-slate-500 dark:text-slate-400 mb-1.5 font-mono">
                            +{rollbackPreview.added_lines}/-{rollbackPreview.removed_lines} lines · comparing{" "}
                            {rollbackPreview.current_source === "live"
                              ? "live configuration"
                              : rollbackPreview.current_source === "last_snapshot"
                              ? "most recent snapshot"
                              : "nothing (no baseline available)"}{" "}
                            against v{rollbackPreview.target_version}
                          </p>
                          <div className="max-h-64 overflow-y-auto border border-slate-200 dark:border-slate-700 rounded-lg">
                            <ConfigDiff diffText={rollbackPreview.diff} />
                          </div>
                        </>
                      )}
                    </div>
                  ) : null}
                </div>
              </>
            ) : (
              <>
                <p className="text-[13px] text-slate-500 dark:text-slate-400 mt-3 leading-relaxed">
                  Reverts only <span className="font-bold">one section</span> (an ACL, VLAN, interface, route-map, ...) to its version in
                  snapshot <span className="font-mono font-bold">v{rollbackTarget.snapshot.version}</span>. Everything else in the device's
                  current configuration is left completely untouched — smaller blast radius than a full rollback.
                </p>

                <label className="block text-xs font-bold text-slate-600 dark:text-slate-300 mt-4 mb-1 uppercase tracking-wide">
                  Section to revert
                </label>
                {rollbackSectionsLoading ? (
                  <p className="text-xs text-slate-400">Loading revertible sections…</p>
                ) : rollbackSectionsError ? (
                  <p className="text-xs text-riskcrit">{rollbackSectionsError}</p>
                ) : rollbackSections && rollbackSections.length === 0 ? (
                  <p className="text-xs text-slate-400">
                    No independently revertible sections were detected on this device's current configuration. Use Full Rollback instead.
                  </p>
                ) : (
                  <select
                    className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brandblue outline-none bg-white dark:bg-slate-700"
                    value={rollbackSectionKey}
                    onChange={(e) => setRollbackSectionKey(e.target.value)}
                  >
                    <option value="">Select a section…</option>
                    {rollbackSections?.map((s) => (
                      <option key={s.key} value={s.key}>
                        {s.kind}: {s.name} ({s.line_count} lines)
                      </option>
                    ))}
                  </select>
                )}

                {rollbackSectionKey && (
                  <div className="mt-4">
                    <h4 className="text-xs font-bold text-slate-600 dark:text-slate-300 uppercase tracking-wide mb-2">
                      Preview: What This Section Rollback Will Change
                    </h4>
                    {partialRollbackPreviewLoading ? (
                      <p className="text-xs text-slate-400">Loading preview…</p>
                    ) : partialRollbackPreviewError ? (
                      <p className="text-xs text-riskcrit">{partialRollbackPreviewError}</p>
                    ) : partialRollbackPreview ? (
                      <div>
                        {partialRollbackPreview.blocked && (
                          <p className="text-xs text-riskcrit bg-red-50 dark:bg-red-950/30 border border-red-200 dark:border-red-900 rounded-lg px-3 py-2 mb-2 font-semibold">
                            {partialRollbackPreview.blocked_reason}
                          </p>
                        )}
                        {!partialRollbackPreview.section.existed_in_target && (
                          <p className="text-xs text-amber-700 bg-amber-50 dark:bg-amber-950/30 border border-amber-200 dark:border-amber-900 rounded-lg px-3 py-2 mb-2">
                            This section didn't exist yet in snapshot v{rollbackTarget.snapshot.version} — reverting will remove it entirely.
                          </p>
                        )}
                        {partialRollbackPreview.identical ? (
                          <p className="text-xs text-risklow bg-green-50 dark:bg-green-950/20 border border-green-100 dark:border-green-900 rounded-lg px-3 py-2">
                            No difference -- this section already matches the snapshot.
                          </p>
                        ) : (
                          <div className="max-h-64 overflow-y-auto border border-slate-200 dark:border-slate-700 rounded-lg">
                            <ConfigDiff diffText={partialRollbackPreview.diff} />
                          </div>
                        )}
                      </div>
                    ) : null}
                  </div>
                )}
              </>
            )}

            <label className="block text-xs font-bold text-slate-600 dark:text-slate-300 mt-5 mb-1 uppercase tracking-wide">Reason (optional)</label>
            <input
              className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-brandblue outline-none"
              placeholder="e.g. interface flapping after last change"
              value={rollbackReason}
              onChange={(e) => setRollbackReason(e.target.value)}
            />
            {rollbackError && <p className="text-riskcrit font-semibold text-xs mt-3 bg-red-50 dark:bg-red-950/40 p-2 rounded">{rollbackError}</p>}
            <div className="flex gap-3 justify-end mt-6">
              <button
                onClick={() => setRollbackTarget(null)}
                disabled={rollbackSubmitting}
                className="px-4 py-2 text-sm font-bold text-slate-500 dark:text-slate-400 hover:text-slate-700 disabled:opacity-50 transition-colors bg-slate-100 dark:bg-slate-700 rounded-lg hover:bg-slate-200"
              >
                Cancel
              </button>
              <button
                onClick={confirmRollback}
                disabled={
                  rollbackSubmitting ||
                  (rollbackMode === "full" ? rollbackPreviewLoading || !!rollbackPreview?.blocked : !rollbackSectionKey || partialRollbackPreviewLoading || !!partialRollbackPreview?.blocked)
                }
                className="bg-riskcrit text-white rounded-lg px-5 py-2 text-sm font-bold hover:opacity-90 disabled:opacity-50 shadow-md transform active:scale-95 transition-all"
              >
                {rollbackSubmitting ? "Queuing Pipeline…" : rollbackMode === "partial" ? "Confirm Section Rollback" : "Confirm Rollback"}
              </button>
            </div>
          </div>
        </div>
      )}

        {/* Web Terminal Modal Overlay */}
        {activeTerminalDevice && (
          <div className="fixed inset-0 z-50 flex items-center justify-center p-6 bg-slate-900/80 backdrop-blur-sm">
            <div className="bg-slate-900 w-full max-w-6xl h-[80vh] rounded-xl shadow-2xl flex flex-col overflow-hidden border border-slate-700">
              <div className="flex items-center justify-between px-4 py-3 bg-slate-800 border-b border-slate-700">
                <div className="flex items-center gap-2">
                  <h3 className="text-slate-200 font-bold tracking-wide text-sm font-mono uppercase">
                    Terminal Session: {devices.find(d => d.id === activeTerminalDevice)?.hostname}
                  </h3>
                </div>
                <button
                  onClick={() => setActiveTerminalDevice(null)}
                  className="text-slate-400 dark:text-slate-500 hover:text-white font-bold text-sm bg-transparent border-0"
                >
                  Close [ X ]
                </button>
              </div>
              <div className="flex-grow p-1 overflow-hidden">
                <WebTerminal deviceId={activeTerminalDevice} />
              </div>
            </div>
          </div>
        )}

        {showBulkRotateModal && (
          <BulkRotateCredentialsModal
            deviceIds={Array.from(selectedIds)}
            deviceCount={selectedIds.size}
            onClose={() => setShowBulkRotateModal(false)}
            onDone={(result) => {
              setShowBulkRotateModal(false);
              const affectedCount = result.affected_device_ids.length;
              const failedCount = Object.keys(result.failed).length;
              setBulkNotice(result.detail || `Rotated credentials on ${affectedCount} device(s).`);
              if (failedCount) {
                setBulkError(
                  `Failed for: ${Object.entries(result.failed)
                    .map(([id, msg]) => `${devices.find((d) => d.id === id)?.hostname || id} (${msg})`)
                    .join("; ")}`
                );
              } else {
                setBulkError(null);
              }
            }}
          />
        )}
    </div>
  );
}