import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { ConfigTemplate, TemplateVariable } from "../lib/types";
import { useAuth } from "../lib/auth";

const DEVICE_ROLES = ["", "core", "distribution", "access", "edge"];
const VENDORS = ["", "cisco", "juniper", "arista"];

const emptyForm = {
  name: "",
  description: "",
  device_role: "",
  vendor: "",
  body: "",
  variables: [] as TemplateVariable[],
};

export default function TemplatesPage() {
  const { user } = useAuth();
  const canManage = user?.role === "network_admin";

  const [templates, setTemplates] = useState<ConfigTemplate[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState(emptyForm);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const [selected, setSelected] = useState<ConfigTemplate | null>(null);
  const [previewValues, setPreviewValues] = useState<Record<string, string>>({});
  const [previewResult, setPreviewResult] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewing, setPreviewing] = useState(false);

  const load = () => {
    setLoading(true);
    api
      .get<ConfigTemplate[]>("/config-templates")
      .then((res) => {
        setTemplates(res.data);
        setError(null);
      })
      .catch(() => setError("Failed to load template library."))
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  const openNew = () => {
    setEditingId(null);
    setForm(emptyForm);
    setSaveError(null);
    setShowForm(true);
  };

  const openEdit = (t: ConfigTemplate) => {
    setEditingId(t.id);
    setForm({
      name: t.name,
      description: t.description || "",
      device_role: t.device_role || "",
      vendor: t.vendor || "",
      body: t.body,
      variables: t.variables,
    });
    setSaveError(null);
    setShowForm(true);
  };

  const addVariable = () => {
    setForm((f) => ({ ...f, variables: [...f.variables, { name: "", label: "", default: "", required: true }] }));
  };

  const updateVariable = (idx: number, patch: Partial<TemplateVariable>) => {
    setForm((f) => ({
      ...f,
      variables: f.variables.map((v, i) => (i === idx ? { ...v, ...patch } : v)),
    }));
  };

  const removeVariable = (idx: number) => {
    setForm((f) => ({ ...f, variables: f.variables.filter((_, i) => i !== idx) }));
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setSaveError(null);
    try {
      const payload = {
        name: form.name,
        description: form.description || null,
        device_role: form.device_role || null,
        vendor: form.vendor || null,
        body: form.body,
        variables: form.variables.filter((v) => v.name.trim()),
      };
      if (editingId) {
        await api.patch(`/config-templates/${editingId}`, payload);
      } else {
        await api.post("/config-templates", payload);
      }
      setShowForm(false);
      load();
    } catch (err: any) {
      setSaveError(err?.response?.data?.detail || "Failed to save template.");
    } finally {
      setSaving(false);
    }
  };

  const removeTemplate = async (t: ConfigTemplate) => {
    if (!window.confirm(`Delete template '${t.name}'? This cannot be undone.`)) return;
    try {
      await api.delete(`/config-templates/${t.id}`);
      load();
      if (selected?.id === t.id) setSelected(null);
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to delete template.");
    }
  };

  const openPreview = (t: ConfigTemplate) => {
    setSelected(t);
    const initial: Record<string, string> = {};
    t.variables.forEach((v) => {
      initial[v.name] = v.default || "";
    });
    setPreviewValues(initial);
    setPreviewResult(null);
    setPreviewError(null);
  };

  const runPreview = async () => {
    if (!selected) return;
    setPreviewing(true);
    setPreviewError(null);
    try {
      const res = await api.post<{ rendered_config: string }>(`/config-templates/${selected.id}/render`, {
        variables: previewValues,
      });
      setPreviewResult(res.data.rendered_config);
    } catch (err: any) {
      setPreviewError(err?.response?.data?.detail || "Failed to render template.");
    } finally {
      setPreviewing(false);
    }
  };

  return (
    <div className="pb-16 max-w-6xl mx-auto flex flex-col gap-6 pt-2">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-3xl font-bold text-navy dark:text-white">Config Templates</h1>
          <p className="text-sm text-slate-500 dark:text-slate-400 mt-1 font-medium">
            Reusable Jinja2 provisioning templates ({"{{ vlan_id }}"}, {"{{ uplink_ip }}"}) tied to device role --
            push a standard template and fill in a few variables instead of writing config from scratch.
          </p>
        </div>
        {canManage && (
          <button
            onClick={openNew}
            className="bg-brandblue text-white rounded-full px-5 py-2.5 text-sm font-bold shadow-md hover:bg-navy transition-colors"
          >
            + New Template
          </button>
        )}
      </div>

      {error && <p className="text-riskcrit text-sm">{error}</p>}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl overflow-hidden self-start">
          {loading ? (
            <p className="text-xs text-slate-400 dark:text-slate-500 p-5">Loading templates…</p>
          ) : templates.length === 0 ? (
            <p className="text-xs text-slate-400 dark:text-slate-500 italic p-5">
              No templates yet. {canManage ? "Create one to get started." : "Ask a Network Admin to add one."}
            </p>
          ) : (
            <div className="divide-y divide-slate-100 dark:divide-slate-800">
              {templates.map((t) => (
                <div
                  key={t.id}
                  onClick={() => openPreview(t)}
                  className={`p-4 cursor-pointer hover:bg-slate-50 dark:hover:bg-slate-900 transition-colors ${
                    selected?.id === t.id ? "bg-blue-50 dark:bg-blue-950/30" : ""
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <p className="font-bold text-navy dark:text-white text-sm">{t.name}</p>
                    <div className="flex gap-1.5 shrink-0">
                      {t.device_role && (
                        <span className="text-[10px] font-bold uppercase tracking-wider text-brandblue bg-blue-50 dark:bg-blue-950/40 px-2 py-0.5 rounded-full">
                          {t.device_role}
                        </span>
                      )}
                      {t.vendor && (
                        <span className="text-[10px] font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400 bg-slate-100 dark:bg-slate-700 px-2 py-0.5 rounded-full">
                          {t.vendor}
                        </span>
                      )}
                    </div>
                  </div>
                  {t.description && (
                    <p className="text-xs text-slate-500 dark:text-slate-400 mt-1">{t.description}</p>
                  )}
                  <p className="text-[11px] text-slate-400 dark:text-slate-500 mt-1.5">
                    {t.variables.length} variable{t.variables.length === 1 ? "" : "s"}
                  </p>
                  {canManage && (
                    <div className="flex gap-3 mt-2">
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          openEdit(t);
                        }}
                        className="text-[10px] font-bold uppercase tracking-wider text-brandblue hover:text-navy"
                      >
                        Edit
                      </button>
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          removeTemplate(t);
                        }}
                        className="text-[10px] font-bold uppercase tracking-wider text-riskcrit hover:text-red-800"
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

        <div className="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl p-5 self-start">
          {!selected ? (
            <p className="text-xs text-slate-400 dark:text-slate-500 italic">
              Select a template on the left to preview/render it with sample values.
            </p>
          ) : (
            <div className="flex flex-col gap-4">
              <p className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider">
                Preview -- {selected.name}
              </p>
              {selected.variables.length === 0 ? (
                <p className="text-xs text-slate-400 dark:text-slate-500 italic">
                  This template has no declared variables -- it renders as-is.
                </p>
              ) : (
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  {selected.variables.map((v) => (
                    <div key={v.name}>
                      <label className="block text-[11px] font-semibold text-slate-500 dark:text-slate-400 uppercase mb-1">
                        {v.label || v.name}
                        {v.required && <span className="text-riskcrit ml-1">*</span>}
                      </label>
                      <input
                        className="w-full border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-1.5 text-sm bg-white dark:bg-slate-900"
                        placeholder={v.default || v.name}
                        value={previewValues[v.name] ?? ""}
                        onChange={(e) => setPreviewValues((p) => ({ ...p, [v.name]: e.target.value }))}
                      />
                    </div>
                  ))}
                </div>
              )}
              <button
                onClick={runPreview}
                disabled={previewing}
                className="self-start bg-brandblue text-white rounded-lg px-4 py-2 text-xs font-bold uppercase tracking-wider hover:bg-navy disabled:opacity-50"
              >
                {previewing ? "Rendering…" : "Render Preview"}
              </button>
              {previewError && <p className="text-riskcrit text-xs">{previewError}</p>}
              {previewResult && (
                <pre className="bg-slate-900 text-slate-100 text-xs rounded-lg p-4 overflow-x-auto whitespace-pre-wrap max-h-96">
                  {previewResult}
                </pre>
              )}
              <p className="text-[11px] text-slate-400 dark:text-slate-500">
                To push this, copy the rendered config into a Change Request's proposed config, or use "Use
                Template" from the New Change Request form.
              </p>
            </div>
          )}
        </div>
      </div>

      {showForm && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4">
          <div className="bg-white dark:bg-slate-800 rounded-xl shadow-xl max-w-2xl w-full max-h-[90vh] overflow-y-auto p-6">
            <h2 className="text-lg font-bold text-navy dark:text-white mb-4">
              {editingId ? "Edit Template" : "New Template"}
            </h2>
            <form onSubmit={submit} className="flex flex-col gap-3">
              <input
                className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm"
                placeholder="Template name (e.g. Standard Access Switch)"
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                required
              />
              <input
                className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm"
                placeholder="Description (optional)"
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
              />
              <div className="grid grid-cols-2 gap-3">
                <select
                  className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm"
                  value={form.device_role}
                  onChange={(e) => setForm({ ...form, device_role: e.target.value })}
                >
                  {DEVICE_ROLES.map((r) => (
                    <option key={r} value={r}>
                      {r ? r : "Any device role"}
                    </option>
                  ))}
                </select>
                <select
                  className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm"
                  value={form.vendor}
                  onChange={(e) => setForm({ ...form, vendor: e.target.value })}
                >
                  {VENDORS.map((v) => (
                    <option key={v} value={v}>
                      {v ? v : "Any vendor"}
                    </option>
                  ))}
                </select>
              </div>
              <textarea
                className="border border-slate-300 dark:border-slate-600 rounded-lg px-3 py-2 text-sm font-mono min-h-[180px]"
                placeholder={"interface {{ interface_name }}\n  switchport access vlan {{ vlan_id }}\n  no shutdown"}
                value={form.body}
                onChange={(e) => setForm({ ...form, body: e.target.value })}
                required
              />

              <div>
                <div className="flex items-center justify-between mb-2">
                  <p className="text-xs font-bold text-slate-500 dark:text-slate-400 uppercase tracking-wider">
                    Variables
                  </p>
                  <button
                    type="button"
                    onClick={addVariable}
                    className="text-[11px] font-bold uppercase tracking-wider text-brandblue hover:text-navy"
                  >
                    + Add variable
                  </button>
                </div>
                {form.variables.length === 0 ? (
                  <p className="text-[11px] text-slate-400 dark:text-slate-500 italic">
                    No variables declared yet -- add one for each {"{{ placeholder }}"} in the template body above.
                  </p>
                ) : (
                  <div className="flex flex-col gap-2">
                    {form.variables.map((v, idx) => (
                      <div key={idx} className="grid grid-cols-[1fr_1fr_1fr_auto_auto] gap-2 items-center">
                        <input
                          className="border border-slate-300 dark:border-slate-600 rounded-lg px-2 py-1.5 text-xs"
                          placeholder="name (e.g. vlan_id)"
                          value={v.name}
                          onChange={(e) => updateVariable(idx, { name: e.target.value })}
                        />
                        <input
                          className="border border-slate-300 dark:border-slate-600 rounded-lg px-2 py-1.5 text-xs"
                          placeholder="label"
                          value={v.label || ""}
                          onChange={(e) => updateVariable(idx, { label: e.target.value })}
                        />
                        <input
                          className="border border-slate-300 dark:border-slate-600 rounded-lg px-2 py-1.5 text-xs"
                          placeholder="default (optional)"
                          value={v.default || ""}
                          onChange={(e) => updateVariable(idx, { default: e.target.value })}
                        />
                        <label className="flex items-center gap-1 text-[11px] text-slate-500 dark:text-slate-400">
                          <input
                            type="checkbox"
                            checked={v.required ?? true}
                            onChange={(e) => updateVariable(idx, { required: e.target.checked })}
                          />
                          Required
                        </label>
                        <button
                          type="button"
                          onClick={() => removeVariable(idx)}
                          className="text-riskcrit text-xs font-bold"
                        >
                          ✕
                        </button>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              {saveError && <p className="text-riskcrit text-xs">{saveError}</p>}
              <div className="flex justify-end gap-2 mt-2">
                <button
                  type="button"
                  onClick={() => setShowForm(false)}
                  className="px-4 py-2 text-xs font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={saving}
                  className="bg-brandblue text-white rounded-lg px-5 py-2 text-xs font-bold uppercase tracking-wider hover:bg-navy disabled:opacity-50"
                >
                  {saving ? "Saving…" : editingId ? "Save Changes" : "Create Template"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}