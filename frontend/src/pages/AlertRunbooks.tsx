import { FormEvent, useEffect, useState } from "react";
import { api } from "../lib/api";
import { useToast, errorMessage } from "../lib/toast";
import { useConfirm } from "../lib/confirm";
import { AlertRunbook, AlertSourceType, Device, RemediationActionType, RunbookExecution } from "../lib/types";

/** Admin CRUD for category(+source) -> runbook URL mappings (backs GET/POST/
 *  PUT/DELETE /alert-runbooks). Resolution itself lives server-side in
 *  app.services.alert_runbook and is applied automatically to every Alert
 *  and Incident response that matches -- this page only manages the
 *  mapping table itself, same shape as Templates or PushSettings.
 *
 *  A runbook can optionally also carry a real remediation action (restart
 *  a service / push a config snippet) instead of being reference-only --
 *  see the "Remediation" section of the form and the "Run Now" flow below,
 *  which calls POST /alert-runbooks/{id}/execute
 *  (app.services.runbook_execution_service). That endpoint is gated
 *  server-side to NETWORK_ADMIN (with JIT elevation counting) regardless
 *  of what this page renders, so hiding/showing "Run Now" here is a
 *  convenience, not the actual access control.
 */

const SOURCES: AlertSourceType[] = ["snmp_trap", "health_poll", "drift", "protocol_failure", "syslog"];

interface FormState {
  category: string;
  source: AlertSourceType | "";
  title: string;
  url: string;
  notes: string;
  remediationEnabled: boolean;
  remediationActionType: RemediationActionType | "";
  remediationLabel: string;
  remediationCommand: string;
  remediationRequiredRole: string;
}

const EMPTY_FORM: FormState = {
  category: "",
  source: "",
  title: "",
  url: "",
  notes: "",
  remediationEnabled: false,
  remediationActionType: "",
  remediationLabel: "",
  remediationCommand: "",
  remediationRequiredRole: "",
};

function statusBadge(status: RunbookExecution["status"]) {
  const styles: Record<string, string> = {
    success: "bg-emerald-100 text-emerald-700",
    failed: "bg-red-100 text-red-700",
    pending: "bg-amber-100 text-amber-700",
  };
  return <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${styles[status]}`}>{status}</span>;
}

export default function AlertRunbooks() {
  const { success: toastSuccess, error: toastError } = useToast();
  const confirm = useConfirm();

  const [runbooks, setRunbooks] = useState<AlertRunbook[]>([]);
  const [devices, setDevices] = useState<Device[]>([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // Run-now dialog state
  const [runTarget, setRunTarget] = useState<AlertRunbook | null>(null);
  const [runDeviceId, setRunDeviceId] = useState("");
  const [running, setRunning] = useState(false);
  const [runResult, setRunResult] = useState<RunbookExecution | null>(null);

  // Execution history panel
  const [historyFor, setHistoryFor] = useState<AlertRunbook | null>(null);
  const [history, setHistory] = useState<RunbookExecution[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);

  const load = () => {
    setLoading(true);
    api
      .get<AlertRunbook[]>("/alert-runbooks")
      .then((res) => setRunbooks(res.data))
      .finally(() => setLoading(false));
  };

  useEffect(load, []);
  useEffect(() => {
    api.get<Device[]>("/devices").then((res) => setDevices(res.data)).catch(() => {});
  }, []);

  const openCreate = () => {
    setEditingId(null);
    setForm(EMPTY_FORM);
    setError(null);
    setShowForm(true);
  };

  const openEdit = (r: AlertRunbook) => {
    setEditingId(r.id);
    setForm({
      category: r.category,
      source: r.source || "",
      title: r.title,
      url: r.url,
      notes: r.notes || "",
      remediationEnabled: r.remediation_enabled,
      remediationActionType: r.remediation_action_type || "",
      remediationLabel: r.remediation_label || "",
      remediationCommand: r.remediation_command || "",
      remediationRequiredRole: r.remediation_required_role || "",
    });
    setError(null);
    setShowForm(true);
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    if (form.remediationEnabled && (!form.remediationActionType || !form.remediationCommand.trim())) {
      setError("Remediation action type and command are required when remediation is enabled.");
      return;
    }
    setSaving(true);
    const body = {
      category: form.category.trim(),
      source: form.source || null,
      title: form.title.trim(),
      url: form.url.trim(),
      notes: form.notes.trim() || null,
      remediation_enabled: form.remediationEnabled,
      remediation_action_type: form.remediationEnabled ? form.remediationActionType || null : null,
      remediation_label: form.remediationEnabled ? form.remediationLabel.trim() || null : null,
      remediation_command: form.remediationEnabled ? form.remediationCommand.trim() || null : null,
      remediation_required_role: form.remediationEnabled ? form.remediationRequiredRole || null : null,
    };
    try {
      if (editingId) {
        await api.put(`/alert-runbooks/${editingId}`, body);
        toastSuccess("Runbook mapping updated");
      } else {
        await api.post("/alert-runbooks", body);
        toastSuccess("Runbook mapping created");
      }
      setShowForm(false);
      load();
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to save runbook mapping.");
    } finally {
      setSaving(false);
    }
  };

  const remove = async (r: AlertRunbook) => {
    const ok = await confirm(
      `Alerts categorized "${r.category}"${r.source ? ` (source: ${r.source})` : ""} will no longer show a linked runbook.`,
      { title: "Delete runbook mapping?", confirmLabel: "Delete", danger: true }
    );
    if (!ok) return;
    await api.delete(`/alert-runbooks/${r.id}`);
    toastSuccess("Runbook mapping deleted");
    load();
  };

  const openRun = (r: AlertRunbook) => {
    setRunTarget(r);
    setRunDeviceId("");
    setRunResult(null);
  };

  const submitRun = async (e: FormEvent) => {
    e.preventDefault();
    if (!runTarget || !runDeviceId) return;
    setRunning(true);
    setRunResult(null);
    try {
      const res = await api.post<RunbookExecution>(`/alert-runbooks/${runTarget.id}/execute`, {
        device_id: runDeviceId,
      });
      setRunResult(res.data);
      if (res.data.status === "success") {
        toastSuccess("Remediation ran successfully");
      } else {
        toastError(res.data.error || "Remediation failed — see output below.");
      }
    } catch (err) {
      toastError(errorMessage(err, "Failed to run remediation."));
    } finally {
      setRunning(false);
    }
  };

  const openHistory = (r: AlertRunbook) => {
    setHistoryFor(r);
    setHistoryLoading(true);
    api
      .get<RunbookExecution[]>(`/alert-runbooks/${r.id}/executions`)
      .then((res) => setHistory(res.data))
      .catch(() => setHistory([]))
      .finally(() => setHistoryLoading(false));
  };

  return (
    <div>
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold text-navy">Alert Runbooks</h1>
          <p className="text-sm text-slate-500 mt-1">
            Map an alert category to a remediation doc — and optionally a real remediation action — so on-call sees
            exactly what to do, right on the alert.
          </p>
        </div>
        <button
          onClick={openCreate}
          className="bg-brandblue text-white rounded-lg px-4 py-2 text-sm font-semibold hover:opacity-90"
        >
          + Add Runbook Mapping
        </button>
      </div>

      <div className="bg-white border border-slate-200 rounded-xl overflow-x-auto mt-5">
        <table className="w-full text-sm min-w-[900px]">
          <thead className="bg-navy text-white">
            <tr>
              <th className="text-left px-4 py-3 font-semibold">Category</th>
              <th className="text-left px-4 py-3 font-semibold">Source</th>
              <th className="text-left px-4 py-3 font-semibold">Runbook</th>
              <th className="text-left px-4 py-3 font-semibold">Remediation</th>
              <th className="text-right px-4 py-3 font-semibold"></th>
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr>
                <td colSpan={5} className="text-center text-slate-400 py-8">
                  Loading runbook mappings…
                </td>
              </tr>
            )}
            {!loading && runbooks.length === 0 && (
              <tr>
                <td colSpan={5} className="text-center text-slate-400 py-8">
                  No runbooks mapped yet. Alerts won't show a remediation link until you add one.
                </td>
              </tr>
            )}
            {runbooks.map((r, i) => (
              <tr key={r.id} className={i % 2 ? "bg-slate-50" : "bg-white"}>
                <td className="px-4 py-3 font-medium text-navy">{r.category}</td>
                <td className="px-4 py-3 text-slate-600">
                  {r.source ? (
                    <span className="px-2 py-1 rounded-full text-xs font-medium bg-slate-100 text-slate-600">{r.source}</span>
                  ) : (
                    <span className="text-slate-400">any source</span>
                  )}
                </td>
                <td className="px-4 py-3">
                  <a href={r.url} target="_blank" rel="noreferrer" className="text-brandblue hover:text-navy font-medium">
                    {r.title} ↗
                  </a>
                </td>
                <td className="px-4 py-3">
                  {r.remediation_enabled ? (
                    <div className="flex items-center gap-2">
                      <span className="px-2 py-1 rounded-full text-xs font-medium bg-violet-100 text-violet-700">
                        {r.remediation_label || r.remediation_action_type}
                      </span>
                      <button onClick={() => openHistory(r)} className="text-xs text-slate-400 hover:text-slate-600 underline">
                        history
                      </button>
                    </div>
                  ) : (
                    <span className="text-slate-400">doc only</span>
                  )}
                </td>
                <td className="px-4 py-3 text-right whitespace-nowrap">
                  {r.remediation_enabled && (
                    <button onClick={() => openRun(r)} className="text-xs text-white bg-violet-600 hover:bg-violet-700 rounded-md px-2.5 py-1 font-medium mr-3">
                      Run Now
                    </button>
                  )}
                  <button onClick={() => openEdit(r)} className="text-xs text-brandblue hover:text-navy font-medium mr-3">
                    Edit
                  </button>
                  <button onClick={() => remove(r)} className="text-xs text-red-600 hover:text-red-800 font-medium">
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {showForm && (
        <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50 p-4 overflow-y-auto">
          <form onSubmit={submit} className="bg-white rounded-lg p-6 w-full max-w-lg space-y-4 my-8">
            <h2 className="text-lg font-semibold text-slate-900">
              {editingId ? "Edit runbook mapping" : "Add runbook mapping"}
            </h2>

            {error && <p className="text-sm text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2">{error}</p>}

            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">
                Alert category <span className="text-slate-400 font-normal">(matches Alert.category, e.g. "Interface Down")</span>
              </label>
              <input
                required
                value={form.category}
                onChange={(e) => setForm({ ...form, category: e.target.value })}
                className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm"
                placeholder="Interface Down"
              />
            </div>

            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">Source (optional — leave blank to match any)</label>
              <select
                value={form.source}
                onChange={(e) => setForm({ ...form, source: e.target.value as AlertSourceType | "" })}
                className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm"
              >
                <option value="">Any source</option>
                {SOURCES.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">Runbook title</label>
              <input
                required
                value={form.title}
                onChange={(e) => setForm({ ...form, title: e.target.value })}
                className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm"
                placeholder="Interface Down — triage steps"
              />
            </div>

            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">Runbook URL</label>
              <input
                required
                type="url"
                value={form.url}
                onChange={(e) => setForm({ ...form, url: e.target.value })}
                className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm"
                placeholder="https://wiki.internal/runbooks/interface-down"
              />
            </div>

            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">Notes (optional)</label>
              <textarea
                value={form.notes}
                onChange={(e) => setForm({ ...form, notes: e.target.value })}
                className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm"
                rows={2}
              />
            </div>

            <div className="border-t border-slate-200 pt-4">
              <label className="flex items-center gap-2 text-sm font-medium text-slate-700">
                <input
                  type="checkbox"
                  checked={form.remediationEnabled}
                  onChange={(e) => setForm({ ...form, remediationEnabled: e.target.checked })}
                />
                Enable a real remediation action for this runbook
              </label>
              <p className="text-xs text-slate-400 mt-1">
                Pushes a command/config snippet to the device via the same deploy path as Change Requests. Restricted
                to network admins (JIT elevation counts).
              </p>

              {form.remediationEnabled && (
                <div className="mt-3 space-y-3 bg-violet-50/50 border border-violet-100 rounded-lg p-3">
                  <div>
                    <label className="block text-xs font-medium text-slate-600 mb-1">Action type</label>
                    <select
                      required
                      value={form.remediationActionType}
                      onChange={(e) => setForm({ ...form, remediationActionType: e.target.value as RemediationActionType })}
                      className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm"
                    >
                      <option value="">Select…</option>
                      <option value="restart_service">Restart service</option>
                      <option value="push_config">Push config</option>
                    </select>
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-slate-600 mb-1">Label (shown on Run Now button)</label>
                    <input
                      value={form.remediationLabel}
                      onChange={(e) => setForm({ ...form, remediationLabel: e.target.value })}
                      className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm"
                      placeholder="Restart BGP process"
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-slate-600 mb-1">Command / config to push</label>
                    <textarea
                      required
                      value={form.remediationCommand}
                      onChange={(e) => setForm({ ...form, remediationCommand: e.target.value })}
                      className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm font-mono"
                      rows={3}
                      placeholder={"interface GigabitEthernet0/1\n shutdown\n no shutdown"}
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-slate-600 mb-1">
                      Require a specific role (optional — beyond the standard admin/JIT gate)
                    </label>
                    <select
                      value={form.remediationRequiredRole}
                      onChange={(e) => setForm({ ...form, remediationRequiredRole: e.target.value })}
                      className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm"
                    >
                      <option value="">No extra restriction</option>
                      <option value="network_admin">Network Admin</option>
                      <option value="security">Security</option>
                    </select>
                  </div>
                </div>
              )}
            </div>

            <div className="flex justify-end gap-2 pt-2">
              <button
                type="button"
                onClick={() => setShowForm(false)}
                className="px-4 py-2 text-sm font-medium text-slate-600 hover:bg-slate-100 rounded-lg"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={saving}
                className="px-4 py-2 text-sm font-semibold text-white bg-brandblue rounded-lg hover:opacity-90 disabled:opacity-50"
              >
                {saving ? "Saving…" : editingId ? "Save Changes" : "Create Mapping"}
              </button>
            </div>
          </form>
        </div>
      )}

      {runTarget && (
        <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50 p-4">
          <form onSubmit={submitRun} className="bg-white rounded-lg p-6 w-full max-w-md space-y-4">
            <h2 className="text-lg font-semibold text-slate-900">
              Run remediation: {runTarget.remediation_label || runTarget.title}
            </h2>
            <p className="text-xs text-slate-500 bg-slate-50 border border-slate-200 rounded-lg px-3 py-2 font-mono whitespace-pre-wrap">
              {runTarget.remediation_command?.startsWith("__config_intent__:")
                ? `Vendor-specific command — auto-resolved for the target device's vendor at run time (${runTarget.remediation_command.slice("__config_intent__:".length).replace(/_/g, " ")}).`
                : runTarget.remediation_command}
            </p>
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">Target device</label>
              <select
                required
                value={runDeviceId}
                onChange={(e) => setRunDeviceId(e.target.value)}
                className="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm"
              >
                <option value="">Select a device…</option>
                {devices.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.hostname} ({d.ip_address})
                  </option>
                ))}
              </select>
            </div>

            {runResult && (
              <div className={`text-xs rounded-lg px-3 py-2 border ${runResult.status === "success" ? "bg-emerald-50 border-emerald-200 text-emerald-700" : "bg-red-50 border-red-200 text-red-700"}`}>
                {statusBadge(runResult.status)}
                <div className="mt-1 whitespace-pre-wrap font-mono">{runResult.error || runResult.output || "No output."}</div>
              </div>
            )}

            <div className="flex justify-end gap-2 pt-2">
              <button
                type="button"
                onClick={() => setRunTarget(null)}
                className="px-4 py-2 text-sm font-medium text-slate-600 hover:bg-slate-100 rounded-lg"
              >
                Close
              </button>
              <button
                type="submit"
                disabled={running || !runDeviceId}
                className="px-4 py-2 text-sm font-semibold text-white bg-violet-600 rounded-lg hover:bg-violet-700 disabled:opacity-50"
              >
                {running ? "Running…" : "Run remediation"}
              </button>
            </div>
          </form>
        </div>
      )}

      {historyFor && (
        <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50 p-4">
          <div className="bg-white rounded-lg p-6 w-full max-w-lg space-y-3 max-h-[80vh] overflow-y-auto">
            <div className="flex items-center justify-between">
              <h2 className="text-lg font-semibold text-slate-900">Execution history: {historyFor.title}</h2>
              <button onClick={() => setHistoryFor(null)} className="text-slate-400 hover:text-slate-600 text-sm">
                Close
              </button>
            </div>
            {historyLoading && <p className="text-sm text-slate-400">Loading…</p>}
            {!historyLoading && history.length === 0 && <p className="text-sm text-slate-400">No runs yet.</p>}
            {!historyLoading &&
              history.map((h) => (
                <div key={h.id} className="border border-slate-200 rounded-lg p-3 text-xs space-y-1">
                  <div className="flex items-center justify-between">
                    {statusBadge(h.status)}
                    <span className="text-slate-400">{new Date(h.created_at).toLocaleString()}</span>
                  </div>
                  <div className="text-slate-500">by {h.triggered_by}</div>
                  {(h.error || h.output) && (
                    <div className="font-mono whitespace-pre-wrap text-slate-600 bg-slate-50 rounded px-2 py-1">
                      {h.error || h.output}
                    </div>
                  )}
                </div>
              ))}
          </div>
        </div>
      )}
    </div>
  );
}