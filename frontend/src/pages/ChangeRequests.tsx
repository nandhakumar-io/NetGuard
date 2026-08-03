import { useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import { ChangePriority, ChangeRequest, ChangeStatus, Device } from "../lib/types";
import RiskBadge from "../components/RiskBadge";
import ConfigDiff from "../components/ConfigDiff";
import { useAuth } from "../lib/auth";

const statusStyle: Record<string, string> = {
  draft: "bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300",
  pending_approval: "bg-amber-100 text-amber-700",
  approved: "bg-blue-100 text-blue-700",
  rejected: "bg-red-100 text-red-700",
  validating: "bg-blue-100 text-blue-700",
  deploying: "bg-blue-100 text-blue-700",
  monitoring: "bg-blue-100 text-blue-700",
  success: "bg-green-100 text-green-700",
  failed: "bg-red-100 text-red-700",
  rolled_back: "bg-red-100 text-red-700",
};

const priorityStyle: Record<ChangePriority, string> = {
  low: "bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300",
  medium: "bg-blue-100 text-blue-700",
  high: "bg-amber-100 text-amber-700",
  emergency: "bg-red-100 text-red-700",
};

const emptyForm = {
  device_id: "",
  description: "",
  business_justification: "",
  priority: "medium" as ChangePriority,
  proposed_config: "",
};

const STATUS_FILTERS: { value: ChangeStatus | "all"; label: string }[] = [
  { value: "all", label: "All" },
  { value: "pending_approval", label: "Pending" },
  { value: "approved", label: "Approved" },
  { value: "success", label: "Success" },
  { value: "failed", label: "Failed" },
  { value: "rejected", label: "Rejected" },
];

export default function ChangeRequests() {
  const { user } = useAuth();
  const canApprove = user?.role === "network_admin";
  const [requests, setRequests] = useState<ChangeRequest[]>([]);
  const [devices, setDevices] = useState<Device[]>([]);
  const [selected, setSelected] = useState<ChangeRequest | null>(null);
  const [form, setForm] = useState(emptyForm);
  const [loading, setLoading] = useState(false);
  const [initialLoading, setInitialLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [acting, setActing] = useState(false);
  const [statusFilter, setStatusFilter] = useState<ChangeStatus | "all">("all");
  const [showForm, setShowForm] = useState(false);

  const load = () => {
    Promise.all([api.get<ChangeRequest[]>("/change-requests"), api.get<Device[]>("/devices")])
      .then(([reqRes, devRes]) => {
        setRequests(reqRes.data);
        setDevices(devRes.data);
        setSelected((prev) => (prev ? reqRes.data.find((r) => r.id === prev.id) || null : null));
      })
      .finally(() => setInitialLoading(false));
  };

  useEffect(load, []);

  // Keep the detail panel fresh while a pipeline is running.
  useEffect(() => {
    if (!selected || !["approved", "validating", "deploying", "monitoring"].includes(selected.status)) return;
    const interval = setInterval(load, 4000);
    return () => clearInterval(interval);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected?.id, selected?.status]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      await api.post("/change-requests", form);
      setForm(emptyForm);
      setShowForm(false);
      load();
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to submit change request.");
    } finally {
      setLoading(false);
    }
  };

  const act = async (id: string, action: "approve" | "reject" | "rescore") => {
    setActing(true);
    setActionError(null);
    try {
      await api.post(`/change-requests/${id}/${action}`);
      load();
    } catch (err: any) {
      setActionError(err?.response?.data?.detail || `Failed to ${action} the request.`);
    } finally {
      setActing(false);
    }
  };

  const hostnameFor = (deviceId: string) => devices.find((d) => d.id === deviceId)?.hostname || deviceId.slice(0, 8);

  const filtered = useMemo(
    () => (statusFilter === "all" ? requests : requests.filter((r) => r.status === statusFilter)),
    [requests, statusFilter]
  );

  const pendingCount = requests.filter((r) => r.status === "pending_approval").length;

  return (
    <div>
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold text-navy dark:text-white">Change Requests</h1>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">
            Submit configuration changes for AI risk analysis, validation, and approval.
          </p>
        </div>
        <button
          onClick={() => setShowForm((s) => !s)}
          disabled={!devices.length}
          className="bg-brandblue text-white rounded-lg px-4 py-2 text-sm font-semibold hover:bg-navy dark:bg-slate-950 transition-colors disabled:opacity-50"
        >
          {showForm ? "Cancel" : "+ New Change Request"}
        </button>
      </div>

      {!devices.length && !initialLoading && (
        <p className="text-xs text-slate-400 dark:text-slate-500 mt-3">Add a device first under the Devices page.</p>
      )}

      {showForm && (
        <form onSubmit={submit} className="mt-5 bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-5 space-y-3">
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            <select
              className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm"
              value={form.device_id}
              onChange={(e) => setForm({ ...form, device_id: e.target.value })}
              required
            >
              <option value="">Select device…</option>
              {devices.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.hostname} ({d.ip_address})
                </option>
              ))}
            </select>
            <input
              className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm"
              placeholder="Change description"
              value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })}
              required
            />
            <select
              className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm"
              value={form.priority}
              onChange={(e) => setForm({ ...form, priority: e.target.value as ChangePriority })}
            >
              <option value="low">Low priority</option>
              <option value="medium">Medium priority</option>
              <option value="high">High priority</option>
              <option value="emergency">Emergency</option>
            </select>
          </div>
          <input
            className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm"
            placeholder="Business justification (optional)"
            value={form.business_justification}
            onChange={(e) => setForm({ ...form, business_justification: e.target.value })}
          />
          <textarea
            className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm font-mono"
            rows={5}
            placeholder={"Proposed configuration, e.g.\ninterface Gi0/1\n ip address 10.2.2.1 255.255.255.0"}
            value={form.proposed_config}
            onChange={(e) => setForm({ ...form, proposed_config: e.target.value })}
            required
          />
          <button
            type="submit"
            disabled={loading || !devices.length}
            className="bg-brandblue text-white rounded-lg px-4 py-2 text-sm font-semibold hover:bg-navy dark:bg-slate-950 transition-colors disabled:opacity-50"
          >
            {loading ? "Analyzing…" : "Submit Change Request"}
          </button>
          {error && <p className="text-riskcrit text-sm">{error}</p>}
        </form>
      )}

      <div className="flex flex-wrap gap-2 mt-6 mb-3">
        {STATUS_FILTERS.map((f) => (
          <button
            key={f.value}
            onClick={() => setStatusFilter(f.value)}
            className={`px-3 py-1.5 rounded-full text-xs font-medium border transition-colors ${
              statusFilter === f.value
                ? "bg-navy dark:bg-slate-950 text-white border-navy dark:border-slate-600"
                : "bg-white dark:bg-slate-800 text-slate-500 dark:text-slate-400 border-slate-200 dark:border-slate-700 hover:border-slate-300 dark:border-slate-600"
            }`}
          >
            {f.label}
            {f.value === "pending_approval" && pendingCount > 0 && (
              <span className="ml-1.5 bg-amber-500 text-white rounded-full px-1.5 text-[10px]">{pendingCount}</span>
            )}
          </button>
        ))}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl overflow-hidden self-start">
          <table className="w-full text-sm">
            <thead className="bg-navy dark:bg-slate-950 text-white">
              <tr>
                <th className="text-left px-4 py-3 font-semibold">Device</th>
                <th className="text-left px-4 py-3 font-semibold">Priority</th>
                <th className="text-left px-4 py-3 font-semibold">Risk</th>
                <th className="text-left px-4 py-3 font-semibold">Status</th>
              </tr>
            </thead>
            <tbody>
              {initialLoading && (
                <tr>
                  <td colSpan={4} className="text-center text-slate-400 dark:text-slate-500 py-8">
                    Loading…
                  </td>
                </tr>
              )}
              {!initialLoading && filtered.length === 0 && (
                <tr>
                  <td colSpan={4} className="text-center text-slate-400 dark:text-slate-500 py-8">
                    {requests.length === 0 ? "No change requests yet." : "No requests match this filter."}
                  </td>
                </tr>
              )}
              {filtered.map((r, i) => (
                <tr
                  key={r.id}
                  onClick={() => setSelected(r)}
                  className={`cursor-pointer hover:bg-blue-50 ${i % 2 ? "bg-slate-50 dark:bg-slate-900" : "bg-white dark:bg-slate-800"} ${
                    selected?.id === r.id ? "ring-2 ring-inset ring-brandblue" : ""
                  }`}
                >
                  <td className="px-4 py-3 font-medium text-navy dark:text-white">
                    {hostnameFor(r.device_id)}
                    {r.is_rollback === "true" && (
                      <span className="ml-1.5 px-1.5 py-0.5 rounded text-[10px] font-semibold bg-purple-100 text-purple-700 align-middle">
                        ↺ rollback
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    <span className={`px-2 py-1 rounded-full text-xs font-semibold capitalize ${priorityStyle[r.priority]}`}>
                      {r.priority}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    {r.risk_score != null && (
                      <span className="inline-flex items-center gap-1.5">
                        <RiskBadge score={r.risk_score} />
                        {r.risk_engine_backend === "llm" && r.risk_llm_applied && (
                          <span title="AI-reviewed" className="text-xs">✨</span>
                        )}
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    <span className={`px-2 py-1 rounded-full text-xs font-semibold capitalize ${statusStyle[r.status] || ""}`}>
                      {r.status.replace(/_/g, " ")}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-5">
          {!selected ? (
            <p className="text-sm text-slate-400 dark:text-slate-500 italic">Select a change request to view details.</p>
          ) : (
            <div className="space-y-4">
              <div className="flex items-start justify-between gap-2">
                <div>
                  <h3 className="font-semibold text-navy dark:text-white flex items-center gap-2">
                    {selected.description}
                    {selected.is_rollback === "true" && (
                      <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-purple-100 text-purple-700">
                        ↺ rollback
                      </span>
                    )}
                  </h3>
                  <p className="text-xs text-slate-500 dark:text-slate-400 mt-1">
                    Device: {hostnameFor(selected.device_id)} · Submitted{" "}
                    {new Date(selected.created_at).toLocaleString()}
                  </p>
                  {selected.business_justification && (
                    <p className="text-xs text-slate-500 dark:text-slate-400 mt-1 italic">"{selected.business_justification}"</p>
                  )}
                </div>
                <span className={`shrink-0 px-2 py-1 rounded-full text-xs font-semibold capitalize ${priorityStyle[selected.priority]}`}>
                  {selected.priority}
                </span>
              </div>
              {selected.risk_score != null && (
                <div>
                  <div className="flex items-center justify-between mb-1">
                    <p className="text-xs font-semibold text-slate-500 dark:text-slate-400 uppercase">AI Risk Analysis</p>
                    {selected.risk_engine_backend === "llm" && (
                      selected.risk_llm_applied ? (
                        <span
                          className="inline-flex items-center gap-1 bg-purple-50 border border-purple-200 text-purple-700 text-[10px] font-bold uppercase tracking-wide px-2 py-0.5 rounded-full"
                          title="An LLM pass ran and its findings were merged into this score, in addition to the rule engine."
                        >
                          ✨ AI-Reviewed
                        </span>
                      ) : (
                        <span
                          className="inline-flex items-center gap-1 bg-slate-100 dark:bg-slate-700 border border-slate-200 dark:border-slate-700 text-slate-500 dark:text-slate-400 text-[10px] font-bold uppercase tracking-wide px-2 py-0.5 rounded-full"
                          title={selected.risk_llm_error || "LLM backend selected but the model pass did not run; this score is rule-engine only."}
                        >
                          Rule-Engine Only
                        </span>
                      )
                    )}
                  </div>
                  <RiskBadge score={selected.risk_score} />
                  {selected.risk_findings && (
                    <ul className="text-xs text-slate-600 dark:text-slate-300 mt-2 list-disc list-inside space-y-0.5">
                      {selected.risk_findings.split("; ").map((f, i) => (
                        <li key={i}>{f}</li>
                      ))}
                    </ul>
                  )}
                  {selected.config_source && selected.config_source !== "live" && (
                    <p className="text-[11px] text-riskmed bg-amber-50 border border-amber-200 rounded-lg px-2.5 py-1.5 mt-2">
                      This score was computed against a {selected.config_source === "snapshot" ? "stale snapshot" : "missing"}{" "}
                      config, not a fresh live read from the device.
                    </p>
                  )}
                  {selected.risk_llm_error && (
                    <p className="text-[11px] text-slate-500 dark:text-slate-400 bg-slate-50 dark:bg-slate-900 border border-slate-200 dark:border-slate-700 rounded-lg px-2.5 py-1.5 mt-2">
                      LLM pass didn't run: {selected.risk_llm_error}
                    </p>
                  )}
                  {["draft", "pending_approval"].includes(selected.status) &&
                    (selected.config_source !== "live" || (selected.risk_engine_backend === "llm" && !selected.risk_llm_applied)) && (
                      <button
                        onClick={() => act(selected.id, "rescore")}
                        disabled={acting}
                        className="mt-2 text-[11px] font-bold uppercase tracking-wide text-brandblue border border-blue-200 bg-blue-50 hover:bg-blue-100 px-2.5 py-1 rounded-lg shadow-sm disabled:opacity-50"
                      >
                        {acting ? "Retrying…" : "↻ Retry / Re-score"}
                      </button>
                    )}
                </div>
              )}
              <div>
                <p className="text-xs font-semibold text-slate-500 dark:text-slate-400 uppercase mb-1">Configuration Diff</p>
                <ConfigDiff diffText={selected.config_diff} />
              </div>
              {selected.requires_dual_approval && selected.status === "pending_approval" && (
                <div className="rounded-lg bg-amber-50 border border-amber-200 text-amber-800 text-xs px-3 py-2">
                  {selected.first_approved_by
                    ? `${selected.dual_approval_reason ?? "Critical Risk"}: first approval recorded. A second, different Network Administrator must approve to deploy.`
                    : `${selected.dual_approval_reason ?? "Critical Risk"}: this change requires approval from two different Network Administrators before deployment.`}
                </div>
              )}
              {actionError && <p className="text-riskcrit text-xs">{actionError}</p>}
              {selected.status === "pending_approval" &&
                (canApprove ? (
                  <div className="flex gap-2 pt-2">
                    <button
                      onClick={() => act(selected.id, "approve")}
                      disabled={acting}
                      className="bg-risklow text-white rounded-lg px-4 py-2 text-sm font-semibold hover:opacity-90 disabled:opacity-50"
                    >
                      {acting
                        ? "Working…"
                        : selected.requires_dual_approval
                        ? selected.first_approved_by
                          ? "Give 2nd Approval & Deploy"
                          : "Give 1st Approval"
                        : "Approve & Deploy"}
                    </button>
                    <button
                      onClick={() => act(selected.id, "reject")}
                      disabled={acting}
                      className="bg-riskcrit text-white rounded-lg px-4 py-2 text-sm font-semibold hover:opacity-90 disabled:opacity-50"
                    >
                      Reject
                    </button>
                  </div>
                ) : (
                  <p className="text-xs text-slate-400 dark:text-slate-500 italic pt-2">
                    Only a Network Administrator can approve or reject this request.
                  </p>
                ))}
              {["approved", "validating", "deploying", "monitoring", "success", "failed", "rolled_back"].includes(
                selected.status
              ) && (
                <p className="text-xs text-slate-500 dark:text-slate-400 pt-1">
                  See the <span className="font-medium text-navy dark:text-white">Deployments</span> page for pipeline details
                  (snapshot, health checks, rollback status).
                  {["approved", "validating", "deploying", "monitoring"].includes(selected.status) &&
                    " This panel auto-refreshes while the pipeline runs."}
                </p>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}