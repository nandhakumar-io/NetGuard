import { useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import { Device, Drift, DriftBaseline, DriftFleetSummary, DriftScanResponse, DriftSeverity, DriftStatus } from "../lib/types";
import ConfigDiff from "../components/ConfigDiff";
import StatCard from "../components/StatCard";
import { useAuth } from "../lib/auth";

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

  const load = () => {
    Promise.all([
      api.get<DriftFleetSummary>("/drift/summary"),
      api.get<Drift[]>("/drift"),
      api.get<Device[]>("/devices"),
    ])
      .then(([summaryRes, driftRes, devRes]) => {
        setSummary(summaryRes.data);
        setDrifts(driftRes.data);
        setDevices(devRes.data);
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
        <div className="bg-white border border-slate-200 rounded-xl overflow-hidden self-start">
          <table className="w-full text-sm">
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
                <span className={`shrink-0 px-2 py-1 rounded-full text-xs font-semibold capitalize ${severityStyle[detail.severity]}`}>
                  {detail.severity}
                </span>
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
                    <p className="mt-1 text-slate-500">
                      To roll back, go to the <span className="font-medium text-navy">Devices</span> page and select a
                      snapshot to restore.
                    </p>
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