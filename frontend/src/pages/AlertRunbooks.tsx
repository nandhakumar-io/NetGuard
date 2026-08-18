import { FormEvent, useEffect, useState } from "react";
import { api } from "../lib/api";
import { useToast } from "../lib/toast";
import { useConfirm } from "../lib/confirm";
import { AlertRunbook, AlertSourceType } from "../lib/types";

/** Admin CRUD for category(+source) -> runbook URL mappings (backs GET/POST/
 *  PUT/DELETE /alert-runbooks). Resolution itself lives server-side in
 *  app.services.alert_runbook and is applied automatically to every Alert
 *  and Incident response that matches -- this page only manages the
 *  mapping table itself, same shape as Templates or PushSettings.
 */

const SOURCES: AlertSourceType[] = ["snmp_trap", "health_poll", "drift", "protocol_failure", "syslog"];

interface FormState {
  category: string;
  source: AlertSourceType | "";
  title: string;
  url: string;
  notes: string;
}

const EMPTY_FORM: FormState = { category: "", source: "", title: "", url: "", notes: "" };

export default function AlertRunbooks() {
  const { success: toastSuccess } = useToast();
  const confirm = useConfirm();

  const [runbooks, setRunbooks] = useState<AlertRunbook[]>([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const load = () => {
    setLoading(true);
    api
      .get<AlertRunbook[]>("/alert-runbooks")
      .then((res) => setRunbooks(res.data))
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  const openCreate = () => {
    setEditingId(null);
    setForm(EMPTY_FORM);
    setError(null);
    setShowForm(true);
  };

  const openEdit = (r: AlertRunbook) => {
    setEditingId(r.id);
    setForm({ category: r.category, source: r.source || "", title: r.title, url: r.url, notes: r.notes || "" });
    setError(null);
    setShowForm(true);
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setSaving(true);
    const body = {
      category: form.category.trim(),
      source: form.source || null,
      title: form.title.trim(),
      url: form.url.trim(),
      notes: form.notes.trim() || null,
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

  return (
    <div>
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold text-navy">Alert Runbooks</h1>
          <p className="text-sm text-slate-500 mt-1">
            Map an alert category to a remediation doc so on-call sees exactly what to do, right on the alert.
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
        <table className="w-full text-sm min-w-[760px]">
          <thead className="bg-navy text-white">
            <tr>
              <th className="text-left px-4 py-3 font-semibold">Category</th>
              <th className="text-left px-4 py-3 font-semibold">Source</th>
              <th className="text-left px-4 py-3 font-semibold">Runbook</th>
              <th className="text-left px-4 py-3 font-semibold">Notes</th>
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
                <td className="px-4 py-3 text-slate-500 max-w-xs truncate" title={r.notes || undefined}>
                  {r.notes || "—"}
                </td>
                <td className="px-4 py-3 text-right whitespace-nowrap">
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
        <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50 p-4">
          <form onSubmit={submit} className="bg-white rounded-lg p-6 w-full max-w-md space-y-4">
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
    </div>
  );
}