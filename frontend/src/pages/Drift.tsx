import { useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import { BulkApproveResponse, ComplianceBaselineDetail, ComplianceBaselineSummary, Device, Drift, DriftBaseline, DriftFleetSummary, DriftScanResponse, DriftSeverity, DriftStatus, DriftTrendResponse, FlappingDevicesResponse, LowRiskDriftCandidate, WeeklyGoldenDriftReport } from "../lib/types";
import ConfigDiff from "../components/ConfigDiff";
import StatCard from "../components/StatCard";
import { useAuth } from "../lib/auth";
import { useToast, errorMessage } from "../lib/toast";
import { useConfirm } from "../lib/confirm";

const severityStyle: Record<DriftSeverity, string> = {
  low: "bg-risklow/10 text-risklow",
  medium: "bg-riskmed/10 text-riskmed",
  high: "bg-orange-100 text-orange-700",
  critical: "bg-riskcrit/10 text-riskcrit",
};

const statusStyle: Record<DriftStatus, string> = {
  open: "bg-amber-100 text-amber-700",
  approved: "bg-blue-100 text-blue-700",
  rolled_back: "bg-purple-100 text-purple-700",
  dismissed: "bg-slate-100 text-slate-600",
};

const SEVERITY_FILTERS: { value: DriftSeverity | "all"; label: string }[] = [
  { value: "all", label: "All" },
  { value: "critical", label: "Critical" },
  { value: "high", label: "High" },
  { value: "medium", label: "Medium" },
  { value: "low", label: "Low" },
];

export default function DriftPage() {
  const { user } = useAuth();
  const toast = useToast();
  const confirm = useConfirm();
  const canReview = user?.role === "network_admin";

  const [summary, setSummary] = useState<DriftFleetSummary | null>(null);
  const [drifts, setDrifts] = useState<Drift[]>([]);
  const [devices, setDevices] = useState<Device[]>([]);
  const [selected, setSelected] = useState<Drift | null>(null);
  const [detail, setDetail] = useState<DriftScanResponse["drift"] | null>(null);
  const [findings, setFindings] = useState<string[]>([]);
  const [recommendation, setRecommendation] = useState<{ recommended: boolean; reason: string } | null>(null);

  const [initialLoading, setInitialLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [severityFilter, setSeverityFilter] = useState<DriftSeverity | "all">("all");

  const [scanDeviceId, setScanDeviceId] = useState("");
  const [scanBaseline, setScanBaseline] = useState<DriftBaseline>("previous_backup");
  const [scanning, setScanning] = useState(false);
  const [scanError, setScanError] = useState<string | null>(null);

  const [reviewing, setReviewing] = useState(false);
  const [reviewError, setReviewError] = useState<string | null>(null);
  const [remediating, setRemediating] = useState(false);
  const [remediationError, setRemediationError] = useState<string | null>(null);
  const [remediationNotice, setRemediationNotice] = useState<string | null>(null);

  // "Who's drifted from golden config this week" one-click report -- a
  // deduplicated (one row per device) view scoped to a time window,
  // distinct from the raw per-scan `drifts` feed above.
  const [weeklyReport, setWeeklyReport] = useState<WeeklyGoldenDriftReport | null>(null);
  const [weeklyLoading, setWeeklyLoading] = useState(false);
  const [weeklyError, setWeeklyError] = useState<string | null>(null);
  const [weeklyOpen, setWeeklyOpen] = useState(false);

  // "Bulk-approve low-risk drift" -- one-click approval for OPEN, LOW
  // severity drift where every changed line is a cosmetic
  // description/remark edit (see drift_service.is_low_risk_bulk_approvable).
  const [lowRiskCandidates, setLowRiskCandidates] = useState<LowRiskDriftCandidate[] | null>(null);
  const [lowRiskLoading, setLowRiskLoading] = useState(false);
  const [lowRiskError, setLowRiskError] = useState<string | null>(null);
  const [lowRiskOpen, setLowRiskOpen] = useState(false);
  const [lowRiskSelected, setLowRiskSelected] = useState<Set<string>>(new Set());
  const [bulkApproving, setBulkApproving] = useState(false);
  const [bulkApproveNotice, setBulkApproveNotice] = useState<string | null>(null);

  const loadLowRiskCandidates = () => {
    setLowRiskOpen(true);
    setLowRiskLoading(true);
    setLowRiskError(null);
    setBulkApproveNotice(null);
    api
      .get<LowRiskDriftCandidate[]>("/drift/low-risk-candidates")
      .then((res) => {
        setLowRiskCandidates(res.data);
        setLowRiskSelected(new Set(res.data.map((d) => d.id)));
      })
      .catch((err) => setLowRiskError(errorMessage(err, "Failed to load low-risk drift candidates.")))
      .finally(() => setLowRiskLoading(false));
  };

  const toggleLowRiskSelected = (id: string) => {
    setLowRiskSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const runBulkApprove = async () => {
    if (lowRiskSelected.size === 0) return;
    setBulkApproving(true);
    setLowRiskError(null);
    try {
      const res = await api.post<BulkApproveResponse>("/drift/bulk-approve", {
        drift_ids: Array.from(lowRiskSelected),
      });
      setBulkApproveNotice(`Approved ${res.data.approved_count} low-risk drift record(s).`);
      setLowRiskCandidates((prev) => (prev ? prev.filter((d) => !res.data.approved_ids.includes(d.id)) : prev));
      setLowRiskSelected(new Set());
      load();
    } catch (err) {
      setLowRiskError(errorMessage(err, "Bulk approve failed."));
    } finally {
      setBulkApproving(false);
    }
  };

  // Drift trending / flapping-device detection.
  const [trend, setTrend] = useState<DriftTrendResponse | null>(null);
  const [flapping, setFlapping] = useState<FlappingDevicesResponse | null>(null);

  const runWeeklyReport = () => {
    setWeeklyOpen(true);
    setWeeklyLoading(true);
    setWeeklyError(null);
    api
      .get<WeeklyGoldenDriftReport>("/drift/report/weekly-golden-config", { params: { days: 7 } })
      .then((res) => setWeeklyReport(res.data))
      .catch((err) => setWeeklyError(err?.response?.data?.detail || "Failed to load weekly drift report."))
      .finally(() => setWeeklyLoading(false));
  };


  // Compliance Baselines by role -- shared golden-config-style template
  // per device_role (core/access/edge/...) rather than one-per-device, so
  // scanning with baseline="role_baseline" above has something to compare
  // against. See DriftBaseline.ROLE_BASELINE / ComplianceBaseline.
  const [roleBaselines, setRoleBaselines] = useState<ComplianceBaselineSummary[]>([]);
  const [rolesInUse, setRolesInUse] = useState<string[]>([]);
  const [baselinesLoading, setBaselinesLoading] = useState(false);
  const [editingRole, setEditingRole] = useState<string | null>(null);
  const [roleForm, setRoleForm] = useState({ device_role: "", config: "", description: "" });
  const [roleFormSaving, setRoleFormSaving] = useState(false);
  const [roleFormError, setRoleFormError] = useState<string | null>(null);

  const loadRoleBaselines = () => {
    setBaselinesLoading(true);
    Promise.all([
      api.get<ComplianceBaselineSummary[]>("/compliance-baselines"),
      api.get<string[]>("/compliance-baselines/device-roles"),
    ])
      .then(([baselinesRes, rolesRes]) => {
        setRoleBaselines(baselinesRes.data);
        setRolesInUse(rolesRes.data);
      })
      .finally(() => setBaselinesLoading(false));
  };

  useEffect(loadRoleBaselines, []);

  const startEditRole = async (role: string) => {
    setRoleFormError(null);
    if (role) {
      try {
        const res = await api.get<ComplianceBaselineDetail>(`/compliance-baselines/${encodeURIComponent(role)}`);
        setRoleForm({ device_role: role, config: res.data.config, description: res.data.description || "" });
      } catch {
        setRoleForm({ device_role: role, config: "", description: "" });
      }
    } else {
      setRoleForm({ device_role: "", config: "", description: "" });
    }
    setEditingRole(role || "__new__");
  };

  const saveRoleBaseline = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!roleForm.device_role.trim() || !roleForm.config.trim()) return;
    setRoleFormSaving(true);
    setRoleFormError(null);
    try {
      await api.put(`/compliance-baselines/${encodeURIComponent(roleForm.device_role.trim())}`, {
        config: roleForm.config,
        description: roleForm.description || null,
      });
      setEditingRole(null);
      loadRoleBaselines();
    } catch (err: any) {
      setRoleFormError(err?.response?.data?.detail || "Failed to save compliance baseline.");
    } finally {
      setRoleFormSaving(false);
    }
  };

  const deleteRoleBaseline = async (role: string) => {
    if (!(await confirm(`Delete the compliance baseline for role "${role}"? Devices with this role will fall back to golden config / previous backup for drift scans.`, { confirmLabel: "Delete" }))) return;
    try {
      await api.delete(`/compliance-baselines/${encodeURIComponent(role)}`);
      loadRoleBaselines();
      toast.success(`Baseline for "${role}" deleted.`);
    } catch (err) {
      toast.error(errorMessage(err, "Failed to delete compliance baseline."));
    }
  };

  const load = () => {
    Promise.all([
      api.get<DriftFleetSummary>("/drift/summary"),
      api.get<Drift[]>("/drift"),
      api.get<Device[]>("/devices"),
      api.get<DriftTrendResponse>("/drift/trends", { params: { days: 90, bucket_days: 7 } }),
      api.get<FlappingDevicesResponse>("/drift/flapping", { params: { days: 30, min_events: 3 } }),
    ])
      .then(([summaryRes, driftRes, devRes, trendRes, flappingRes]) => {
        setSummary(summaryRes.data);
        setDrifts(driftRes.data);
        setDevices(devRes.data);
        setTrend(trendRes.data);
        setFlapping(flappingRes.data);
      })
      .finally(() => setInitialLoading(false));
  };

  useEffect(load, []);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      setFindings([]);
      setRecommendation(null);
      return;
    }
    setDetailLoading(true);
    setRemediationError(null);
    setRemediationNotice(null);
    Promise.all([
      api.get(`/drift/${selected.id}`),
      api.get(`/drift/${selected.id}/rollback-recommendation`),
    ])
      .then(([detailRes, recRes]) => {
        setDetail(detailRes.data);
        setRecommendation(recRes.data);
        setFindings([]);
      })
      .finally(() => setDetailLoading(false));
  }, [selected?.id]);

  const hostnameFor = (deviceId: string) => devices.find((d) => d.id === deviceId)?.hostname || deviceId.slice(0, 8);

  const filtered = useMemo(
    () => (severityFilter === "all" ? drifts : drifts.filter((d) => d.severity === severityFilter)),
    [drifts, severityFilter]
  );

  const runScan = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!scanDeviceId) return;
    setScanning(true);
    setScanError(null);
    try {
      const res = await api.post<DriftScanResponse>(`/devices/${scanDeviceId}/drift/scan`, {
        baseline: scanBaseline,
      });
      setFindings(res.data.findings);
      load();
      const newDrift = res.data.drift;
      setSelected(newDrift);
      setDetail(newDrift);
      setRecommendation(res.data.rollback_recommendation);
    } catch (err: any) {
      setScanError(err?.response?.data?.detail || "Drift scan failed.");
    } finally {
      setScanning(false);
    }
  };

  const review = async (status: DriftStatus) => {
    if (!selected) return;
    setReviewing(true);
    setReviewError(null);
    try {
      await api.patch(`/drift/${selected.id}`, { status });
      load();
      setSelected((prev) => (prev ? { ...prev, status } : prev));
      setDetail((prev) => (prev ? { ...prev, status } : prev));
    } catch (err: any) {
      setReviewError(err?.response?.data?.detail || "Failed to update drift status.");
    } finally {
      setReviewing(false);
    }
  };

  return (
    <div>
      <div>
        <h1 className="text-2xl font-bold text-navy">Configuration Drift</h1>
        <p className="text-sm text-slate-500 mt-1">
          Detects when a device's live configuration has diverged from its golden config or last known-good backup.
          Scanned nightly, or on demand below.
        </p>
      </div>

      {summary && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mt-6">
          <StatCard label="Open Drifts" value={summary.total_open_drifts} accent="amber" />
          <StatCard label="Devices Drifted" value={summary.devices_drifted} accent="red" />
          <StatCard label="Avg. Compliance Score" value={`${summary.average_compliance_score}/100`} accent="blue" />
          <StatCard label="Rollback Recommended" value={summary.rollback_recommended_count} accent="red" />
        </div>
      )}

      {/* Drift trend + flapping devices */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 mt-6">
        <div className="lg:col-span-2 bg-white border border-slate-200 rounded-xl p-5">
          <h2 className="text-sm font-bold text-navy">Drift Trend (last {trend?.days ?? 90} days)</h2>
          <p className="text-xs text-slate-500 mt-1 mb-4">
            Fleet-wide drift detections per {trend?.bucket_days ?? 7}-day bucket — a rising trend means devices are drifting more often, not just that more scans ran.
          </p>
          {!trend || trend.points.length === 0 ? (
            <p className="text-xs text-slate-400 py-8 text-center">No drift activity in this window.</p>
          ) : (
            (() => {
              const max = Math.max(1, ...trend.points.map((p) => p.total));
              return (
                <div className="flex items-end gap-1.5 h-40">
                  {trend.points.map((p) => (
                    <div key={p.bucket_start} className="flex-1 flex flex-col items-center justify-end h-full group relative">
                      <div className="w-full flex flex-col justify-end h-full rounded-t overflow-hidden bg-slate-100" style={{ height: "100%" }}>
                        <div
                          className="w-full bg-riskcrit"
                          style={{ height: `${(p.critical / max) * 100}%` }}
                          title={`${p.critical} critical`}
                        />
                        <div
                          className="w-full bg-orange-400"
                          style={{ height: `${(p.high / max) * 100}%` }}
                          title={`${p.high} high`}
                        />
                        <div
                          className="w-full bg-brandblue"
                          style={{ height: `${((p.total - p.critical - p.high) / max) * 100}%` }}
                          title={`${p.total - p.critical - p.high} other`}
                        />
                      </div>
                      <div className="absolute -top-6 opacity-0 group-hover:opacity-100 transition-opacity text-[10px] font-bold bg-navy text-white px-1.5 py-0.5 rounded whitespace-nowrap">
                        {p.total} on {new Date(p.bucket_start).toLocaleDateString()}
                      </div>
                      <span className="text-[9px] text-slate-400 mt-1 rotate-0">{new Date(p.bucket_start).toLocaleDateString(undefined, { month: "short", day: "numeric" })}</span>
                    </div>
                  ))}
                </div>
              );
            })()
          )}
        </div>

        <div className="bg-white border border-slate-200 rounded-xl p-5">
          <h2 className="text-sm font-bold text-navy">Flapping Devices</h2>
          <p className="text-xs text-slate-500 mt-1 mb-3">
            {flapping ? `≥${flapping.min_events} drift events in the last ${flapping.days} days` : "Devices drifting repeatedly"} — a sign of unmanaged hand-edits, not a one-off change.
          </p>
          {!flapping || flapping.devices.length === 0 ? (
            <p className="text-xs text-slate-400 py-6 text-center">No repeatedly-drifting devices right now.</p>
          ) : (
            <ul className="space-y-2">
              {flapping.devices.map((d) => (
                <li key={d.device_id} className="flex items-center justify-between text-xs border-b border-slate-100 pb-2 last:border-0 last:pb-0">
                  <div>
                    <p className="font-semibold text-navy">{d.hostname}</p>
                    <p className="text-slate-400">last drift {new Date(d.last_detected_at).toLocaleDateString()}</p>
                  </div>
                  <div className="text-right">
                    <span className={`inline-block px-2 py-0.5 rounded-full font-bold ${severityStyle[d.max_severity]}`}>{d.event_count}x</span>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>

      <div className="mt-6 bg-white border border-slate-200 rounded-xl p-5">
        <div className="flex items-center justify-between flex-wrap gap-3">
          <div>
            <h2 className="text-sm font-bold text-navy">Drifted From Golden Config This Week</h2>
            <p className="text-xs text-slate-500 mt-1">
              One-click fleet view: every device whose live config has diverged from its golden config in the
              last 7 days, one row per device (not one row per scan).
            </p>
          </div>
          <button
            onClick={runWeeklyReport}
            disabled={weeklyLoading}
            className="text-xs font-bold uppercase tracking-wider text-white bg-brandblue hover:bg-navy px-4 py-2 rounded-lg shadow-sm disabled:opacity-50 shrink-0"
          >
            {weeklyLoading ? "Loading…" : "Show This Week's Drift"}
          </button>
        </div>

        {weeklyOpen && (
          <div className="mt-4">
            {weeklyLoading ? (
              <p className="text-xs text-slate-400">Loading…</p>
            ) : weeklyError ? (
              <p className="text-xs text-riskcrit">{weeklyError}</p>
            ) : weeklyReport && weeklyReport.devices.length === 0 ? (
              <p className="text-xs text-slate-400 italic">
                No device has drifted from its golden config in the last {weeklyReport.days} days.
              </p>
            ) : weeklyReport ? (
              <div className="overflow-x-auto border border-slate-200 rounded-lg">
                <table className="w-full text-sm min-w-[560px]">
                  <thead className="bg-slate-50">
                    <tr>
                      <th className="text-left px-4 py-2 font-semibold text-slate-500 text-xs uppercase">Device</th>
                      <th className="text-left px-4 py-2 font-semibold text-slate-500 text-xs uppercase">Severity</th>
                      <th className="text-left px-4 py-2 font-semibold text-slate-500 text-xs uppercase">Compliance</th>
                      <th className="text-left px-4 py-2 font-semibold text-slate-500 text-xs uppercase">Lines Changed</th>
                      <th className="text-left px-4 py-2 font-semibold text-slate-500 text-xs uppercase">Detected</th>
                      <th className="text-left px-4 py-2 font-semibold text-slate-500 text-xs uppercase">Status</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {weeklyReport.devices.map((d, i) => (
                      <tr
                        key={d.id}
                        onClick={() => setSelected(d)}
                        className={`cursor-pointer hover:bg-blue-50 ${i % 2 ? "bg-slate-50" : "bg-white"}`}
                      >
                        <td className="px-4 py-2.5 font-medium text-navy">{d.hostname}</td>
                        <td className="px-4 py-2.5">
                          <span className={`px-2 py-1 rounded-full text-xs font-semibold capitalize ${severityStyle[d.severity]}`}>
                            {d.severity}
                          </span>
                        </td>
                        <td className="px-4 py-2.5">{d.compliance_score}/100</td>
                        <td className="px-4 py-2.5 font-mono text-xs">
                          +{d.added_lines}/-{d.removed_lines}
                        </td>
                        <td className="px-4 py-2.5 text-slate-500 text-xs">{new Date(d.detected_at).toLocaleString()}</td>
                        <td className="px-4 py-2.5">
                          <span className={`px-2 py-1 rounded-full text-xs font-semibold capitalize ${statusStyle[d.status]}`}>
                            {d.status.replace(/_/g, " ")}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <p className="text-[11px] text-slate-400 px-4 py-2 bg-slate-50 border-t border-slate-200">
                  Click a row to open that drift's full detail below.
                </p>
              </div>
            ) : null}
          </div>
        )}
      </div>

      <form onSubmit={runScan} className="mt-6 bg-white border border-slate-200 rounded-xl p-5 flex flex-wrap items-end gap-3">
        <div>
          <label className="block text-xs font-semibold text-slate-500 uppercase mb-1">Device</label>
          <select
            className="border border-slate-300 rounded-lg px-3 py-2 text-sm min-w-[220px]"
            value={scanDeviceId}
            onChange={(e) => setScanDeviceId(e.target.value)}
            required
          >
            <option value="">Select device…</option>
            {devices.map((d) => (
              <option key={d.id} value={d.id}>
                {d.hostname} ({d.ip_address})
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="block text-xs font-semibold text-slate-500 uppercase mb-1">Baseline</label>
          <select
            className="border border-slate-300 rounded-lg px-3 py-2 text-sm"
            value={scanBaseline}
            onChange={(e) => setScanBaseline(e.target.value as DriftBaseline)}
          >
            <option value="previous_backup">Previous backup</option>
            <option value="golden_config">Golden config</option>
            <option value="role_baseline">Role baseline (by device_role)</option>
          </select>
        </div>
        <button
          type="submit"
          disabled={scanning || !devices.length}
          className="bg-brandblue text-white rounded-lg px-4 py-2 text-sm font-semibold hover:bg-navy transition-colors disabled:opacity-50"
        >
          {scanning ? "Scanning…" : "Run Drift Scan"}
        </button>
        {scanError && <p className="text-riskcrit text-sm w-full">{scanError}</p>}
      </form>

      <div className="mt-6 bg-white border border-slate-200 rounded-xl p-5">
        <div className="flex items-center justify-between flex-wrap gap-2">
          <div>
            <h2 className="text-sm font-bold text-navy">Compliance Baselines by Role</h2>
            <p className="text-xs text-slate-500 mt-1">
              One shared baseline template per device_role (e.g. "core", "access") -- so a core switch and an
              access switch are judged against different expected configs instead of sharing one golden config,
              or having no baseline at all. Set a device's role via Devices → Edit, then scan with
              baseline "Role baseline" above.
            </p>
          </div>
          {canReview && (
            <button
              onClick={() => startEditRole("")}
              className="text-xs font-bold uppercase tracking-wider text-brandblue border border-blue-200 bg-blue-50 px-3 py-1.5 rounded-lg hover:bg-blue-100 shrink-0"
            >
              + Add Baseline
            </button>
          )}
        </div>

        {editingRole && (
          <form onSubmit={saveRoleBaseline} className="mt-4 border border-slate-200 rounded-lg p-4 bg-slate-50 flex flex-col gap-3">
            <div className="flex flex-wrap gap-3">
              <div>
                <label className="block text-xs font-semibold text-slate-500 uppercase mb-1">Device Role</label>
                {editingRole === "__new__" ? (
                  <input
                    list="roles-in-use"
                    className="border border-slate-300 rounded-lg px-3 py-2 text-sm"
                    placeholder="e.g. core, access, edge-firewall"
                    value={roleForm.device_role}
                    onChange={(e) => setRoleForm({ ...roleForm, device_role: e.target.value })}
                    required
                  />
                ) : (
                  <input className="border border-slate-300 rounded-lg px-3 py-2 text-sm bg-slate-100" value={roleForm.device_role} disabled />
                )}
                <datalist id="roles-in-use">
                  {rolesInUse.map((r) => (
                    <option key={r} value={r} />
                  ))}
                </datalist>
              </div>
              <div className="flex-1 min-w-[220px]">
                <label className="block text-xs font-semibold text-slate-500 uppercase mb-1">Description (optional)</label>
                <input
                  className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm"
                  placeholder="e.g. Standard core switch baseline (BGP + OSPF uplinks)"
                  value={roleForm.description}
                  onChange={(e) => setRoleForm({ ...roleForm, description: e.target.value })}
                />
              </div>
            </div>
            <div>
              <label className="block text-xs font-semibold text-slate-500 uppercase mb-1">Baseline Config</label>
              <textarea
                className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm font-mono h-40"
                placeholder="Paste the approved config template for this role..."
                value={roleForm.config}
                onChange={(e) => setRoleForm({ ...roleForm, config: e.target.value })}
                required
              />
            </div>
            {roleFormError && <p className="text-riskcrit text-sm">{roleFormError}</p>}
            <div className="flex gap-2">
              <button
                type="submit"
                disabled={roleFormSaving}
                className="bg-brandblue text-white rounded-lg px-4 py-2 text-sm font-semibold hover:bg-navy transition-colors disabled:opacity-50"
              >
                {roleFormSaving ? "Saving…" : "Save Baseline"}
              </button>
              <button
                type="button"
                onClick={() => setEditingRole(null)}
                className="border border-slate-300 rounded-lg px-4 py-2 text-sm font-semibold hover:bg-slate-100"
              >
                Cancel
              </button>
            </div>
          </form>
        )}

        {baselinesLoading ? (
          <p className="text-xs text-slate-400 mt-4">Loading…</p>
        ) : roleBaselines.length === 0 ? (
          <p className="text-xs text-slate-400 italic mt-4">No role baselines set yet.</p>
        ) : (
          <div className="mt-4 divide-y divide-slate-100">
            {roleBaselines.map((b) => (
              <div key={b.device_role} className="py-3 flex items-center justify-between gap-3 flex-wrap">
                <div>
                  <span className="text-sm font-bold text-navy">{b.device_role}</span>
                  <span className="ml-2 text-xs text-slate-400">
                    {b.device_count} device{b.device_count === 1 ? "" : "s"} · checksum {b.checksum.slice(0, 10)}
                  </span>
                  {b.description && <p className="text-xs text-slate-500 mt-0.5">{b.description}</p>}
                </div>
                {canReview && (
                  <div className="flex gap-2 shrink-0">
                    <button
                      onClick={() => startEditRole(b.device_role)}
                      className="text-xs font-bold uppercase tracking-wider text-slate-600 border border-slate-200 bg-white px-2.5 py-1 rounded-lg hover:bg-slate-100"
                    >
                      Edit
                    </button>
                    <button
                      onClick={() => deleteRoleBaseline(b.device_role)}
                      className="text-xs font-bold uppercase tracking-wider text-riskcrit border border-red-200 bg-white px-2.5 py-1 rounded-lg hover:bg-red-50"
                    >
                      Delete
                    </button>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="flex flex-wrap gap-2 mt-6 mb-3">
        {SEVERITY_FILTERS.map((f) => (
          <button
            key={f.value}
            onClick={() => setSeverityFilter(f.value)}
            className={`px-3 py-1.5 rounded-full text-xs font-medium border transition-colors ${
              severityFilter === f.value
                ? "bg-navy text-white border-navy"
                : "bg-white text-slate-500 border-slate-200 hover:border-slate-300"
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="bg-white border border-slate-200 rounded-xl overflow-x-auto self-start">
          <table className="w-full text-sm min-w-[560px]">
            <thead className="bg-navy text-white">
              <tr>
                <th className="text-left px-4 py-3 font-semibold">Device</th>
                <th className="text-left px-4 py-3 font-semibold">Severity</th>
                <th className="text-left px-4 py-3 font-semibold">Compliance</th>
                <th className="text-left px-4 py-3 font-semibold">Status</th>
              </tr>
            </thead>
            <tbody>
              {initialLoading && (
                <tr>
                  <td colSpan={4} className="text-center text-slate-400 py-8">
                    Loading…
                  </td>
                </tr>
              )}
              {!initialLoading && filtered.length === 0 && (
                <tr>
                  <td colSpan={4} className="text-center text-slate-400 py-8">
                    {drifts.length === 0 ? "No drift detected yet. Run a scan above." : "No drifts match this filter."}
                  </td>
                </tr>
              )}
              {filtered.map((d, i) => (
                <tr
                  key={d.id}
                  onClick={() => setSelected(d)}
                  className={`cursor-pointer hover:bg-blue-50 ${i % 2 ? "bg-slate-50" : "bg-white"} ${
                    selected?.id === d.id ? "ring-2 ring-inset ring-brandblue" : ""
                  }`}
                >
                  <td className="px-4 py-3 font-medium text-navy">{hostnameFor(d.device_id)}</td>
                  <td className="px-4 py-3">
                    <span className={`px-2 py-1 rounded-full text-xs font-semibold capitalize ${severityStyle[d.severity]}`}>
                      {d.severity}
                    </span>
                  </td>
                  <td className="px-4 py-3">{d.compliance_score}/100</td>
                  <td className="px-4 py-3">
                    <span className={`px-2 py-1 rounded-full text-xs font-semibold capitalize ${statusStyle[d.status]}`}>
                      {d.status.replace(/_/g, " ")}
                    </span>
                    {d.maintenance_window_id && (
                      <span
                        title="Device was in an active maintenance window when this was detected"
                        className="ml-1 px-2 py-1 rounded-full text-xs font-semibold bg-slate-100 text-slate-500"
                      >
                        Expected — maintenance
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="bg-white border border-slate-200 rounded-xl p-5">
          {!selected ? (
            <p className="text-sm text-slate-400 italic">Select a drift record to view details.</p>
          ) : detailLoading || !detail ? (
            <p className="text-sm text-slate-400 italic">Loading…</p>
          ) : (
            <div className="space-y-4">
              <div className="flex items-start justify-between gap-2">
                <div>
                  <h3 className="font-semibold text-navy">{hostnameFor(detail.device_id)}</h3>
                  <p className="text-xs text-slate-500 mt-1">
                    Baseline: {detail.baseline.replace(/_/g, " ")} · Detected{" "}
                    {new Date(detail.detected_at).toLocaleString()}
                  </p>
                </div>
                <div className="shrink-0 flex flex-col items-end gap-1">
                  <span className={`px-2 py-1 rounded-full text-xs font-semibold capitalize ${severityStyle[detail.severity]}`}>
                    {detail.severity}
                  </span>
                  {detail.maintenance_window_id && (
                    <span className="px-2 py-1 rounded-full text-xs font-semibold bg-slate-100 text-slate-500 whitespace-nowrap">
                      Expected — maintenance
                    </span>
                  )}
                </div>
              </div>

              <div className="grid grid-cols-3 gap-3 text-center">
                <div className="bg-slate-50 rounded-lg p-2">
                  <p className="text-lg font-bold text-navy">{detail.compliance_score}</p>
                  <p className="text-[10px] text-slate-500 uppercase">Compliance</p>
                </div>
                <div className="bg-slate-50 rounded-lg p-2">
                  <p className="text-lg font-bold text-navy">{detail.risk_score}</p>
                  <p className="text-[10px] text-slate-500 uppercase">Risk Score</p>
                </div>
                <div className="bg-slate-50 rounded-lg p-2">
                  <p className="text-lg font-bold text-navy">
                    +{detail.added_lines}/-{detail.removed_lines}
                  </p>
                  <p className="text-[10px] text-slate-500 uppercase">Lines Changed</p>
                </div>
              </div>

              {detail.ai_summary && (
                <div>
                  <p className="text-xs font-semibold text-slate-500 uppercase mb-1">AI Summary</p>
                  <p className="text-sm text-slate-700">{detail.ai_summary}</p>
                </div>
              )}

              {findings.length > 0 && (
                <ul className="text-xs text-slate-600 list-disc list-inside space-y-0.5">
                  {findings.map((f, i) => (
                    <li key={i}>{f}</li>
                  ))}
                </ul>
              )}

              {recommendation && (
                <div
                  className={`rounded-lg p-3 text-xs ${
                    recommendation.recommended ? "bg-red-50 text-riskcrit" : "bg-green-50 text-risklow"
                  }`}
                >
                  <p className="font-semibold mb-0.5">
                    {recommendation.recommended ? "Rollback recommended" : "No rollback needed"}
                  </p>
                  <p>{recommendation.reason}</p>
                  {recommendation.recommended && (
                    <>
                      {detail.baseline === "golden_config" || detail.baseline === "role_baseline" ? (
                        <>
                          <p className="mt-1 text-slate-500">
                            This drift was detected against an approved {detail.baseline === "golden_config" ? "golden config" : "role baseline"} --
                            it can be auto-remediated by pushing that config straight back to the device, or you
                            can roll back manually via a specific snapshot on the Devices page instead.
                          </p>
                          {canReview && detail.status === "open" && (
                            <button
                              onClick={async () => {
                                if (
                                  !(await confirm(
                                    `Submit a change request to push the ${detail.baseline === "golden_config" ? "golden config" : "role baseline"} to this device? This only submits it for approval -- it still needs a NETWORK_ADMIN to approve (a second, different one if it's Critical Risk) before anything deploys.`,
                                    { danger: false, confirmLabel: "Submit for approval" }
                                  ))
                                )
                                  return;
                                setRemediating(true);
                                setRemediationError(null);
                                try {
                                  const res = await api.post<{ message: string; change_request_id: string; requires_dual_approval: boolean }>(
                                    `/drift/${detail.id}/remediate`
                                  );
                                  setRemediationNotice(res.data.message);
                                  const [detailRes, recRes] = await Promise.all([
                                    api.get(`/drift/${detail.id}`),
                                    api.get(`/drift/${detail.id}/rollback-recommendation`),
                                  ]);
                                  setDetail(detailRes.data);
                                  setRecommendation(recRes.data);
                                  load();
                                } catch (err: any) {
                                  setRemediationError(err?.response?.data?.detail || "Failed to submit auto-remediation.");
                                } finally {
                                  setRemediating(false);
                                }
                              }}
                              disabled={remediating}
                              className="mt-2 bg-riskcrit text-white rounded-lg px-3 py-1.5 text-xs font-bold uppercase tracking-wider hover:bg-red-700 disabled:opacity-50"
                            >
                              {remediating
                                ? "Submitting…"
                                : `⚡ Push ${detail.baseline === "golden_config" ? "Golden Config" : "Role Baseline"} to Fix Drift`}
                            </button>
                          )}
                        </>
                      ) : (
                        <p className="mt-1 text-slate-500">
                          To roll back, go to the <span className="font-medium text-navy">Devices</span> page and select a
                          snapshot to restore.
                        </p>
                      )}
                      {remediationError && <p className="mt-1 text-riskcrit">{remediationError}</p>}
                      {remediationNotice && <p className="mt-1 text-risklow font-medium">{remediationNotice}</p>}
                    </>
                  )}
                </div>
              )}

              {detail.cli_diff && (
                <div>
                  <p className="text-xs font-semibold text-slate-500 uppercase mb-1">CLI Commands (What Changed)</p>
                  <pre className="bg-slate-900 text-xs rounded-lg p-4 overflow-x-auto leading-relaxed">
                    {detail.cli_diff.split("\n").map((line, i) => {
                      let cls = "text-slate-300";
                      if (line.startsWith("interface ") || line.startsWith("router ")) cls = "text-accent font-semibold block";
                      else if (line.trimStart().startsWith("no ")) cls = "text-riskcrit bg-red-950/40 block";
                      else if (line.startsWith("  ")) cls = "text-risklow bg-green-950/40 block";
                      return (
                        <span key={i} className={cls}>
                          {line || " "}
                          {"\n"}
                        </span>
                      );
                    })}
                  </pre>
                </div>
              )}

              <details className="group">
                <summary className="text-xs font-semibold text-slate-500 uppercase mb-1 cursor-pointer hover:text-brandblue select-none">
                  {detail.cli_diff ? "Raw Configuration Diff ▸" : "Configuration Diff"}
                </summary>
                <div className="mt-1">
                  <ConfigDiff diffText={detail.diff_text} />
                </div>
              </details>

              {reviewError && <p className="text-riskcrit text-xs">{reviewError}</p>}
              {detail.status === "open" &&
                (canReview ? (
                  <div className="flex gap-2 pt-2">
                    <button
                      onClick={() => review("approved")}
                      disabled={reviewing}
                      className="bg-brandblue text-white rounded-lg px-4 py-2 text-sm font-semibold hover:bg-navy transition-colors disabled:opacity-50"
                    >
                      Approve as New Baseline
                    </button>
                    <button
                      onClick={() => review("dismissed")}
                      disabled={reviewing}
                      className="bg-slate-200 text-slate-700 rounded-lg px-4 py-2 text-sm font-semibold hover:bg-slate-300 transition-colors disabled:opacity-50"
                    >
                      Dismiss
                    </button>
                  </div>
                ) : (
                  <p className="text-xs text-slate-400 italic pt-2">
                    Only a Network Administrator can approve or dismiss a drift record.
                  </p>
                ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}