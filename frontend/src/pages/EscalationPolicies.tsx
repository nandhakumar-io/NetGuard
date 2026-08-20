import { useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import { EscalatedAlertEntry, EscalationChannel, EscalationPolicy, EscalationSeverityScope, OnCallSchedule } from "../lib/types";

const emptyForm = {
  name: "",
  description: "",
  severity_scope: "critical" as EscalationSeverityScope,
  unack_minutes: 15,
  repeat_minutes: "" as number | "",
  secondary_contacts: "",
  on_call_schedule_id: "" as string | "",
  channel: "email" as EscalationChannel,
  webhook_url: "",
};

const CHANNEL_BADGE: Record<string, string> = {
  email: "bg-blue-100 text-blue-700",
  webhook: "bg-purple-100 text-purple-700",
  slack: "bg-emerald-100 text-emerald-700",
  teams: "bg-indigo-100 text-indigo-700",
  push: "bg-amber-100 text-amber-700",
};

const SEVERITY_BADGE: Record<string, string> = {
  critical: "bg-red-100 text-red-700",
  warning: "bg-amber-100 text-amber-700",
  all: "bg-slate-100 text-slate-600",
};

export default function EscalationPolicies() {
  const [policies, setPolicies] = useState<EscalationPolicy[]>([]);
  const [schedules, setSchedules] = useState<OnCallSchedule[]>([]);
  const [log, setLog] = useState<EscalatedAlertEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [form, setForm] = useState(emptyForm);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [runningNow, setRunningNow] = useState(false);

  const load = () => {
    setLoading(true);
    Promise.all([
      api.get<EscalationPolicy[]>("/escalation-policies"),
      api.get<OnCallSchedule[]>("/on-call-schedules"),
      api.get<EscalatedAlertEntry[]>("/escalation-policies/log", { params: { limit: 50 } }),
    ])
      .then(([p, s, l]) => {
        setPolicies(p.data);
        setSchedules(s.data);
        setLog(l.data);
      })
      .catch((err) => setError(err?.response?.data?.detail || "Failed to load escalation policies."))
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  const scheduleName = useMemo(() => {
    const map: Record<string, string> = {};
    schedules.forEach((s) => (map[s.id] = s.name));
    return map;
  }, [schedules]);

  const createPolicy = async () => {
    if (!form.name.trim()) return;
    setCreating(true);
    setError(null);
    try {
      await api.post("/escalation-policies", {
        name: form.name,
        description: form.description || null,
        severity_scope: form.severity_scope,
        unack_minutes: form.unack_minutes,
        repeat_minutes: form.repeat_minutes === "" ? null : Number(form.repeat_minutes),
        secondary_contacts: form.secondary_contacts || null,
        on_call_schedule_id: form.on_call_schedule_id || null,
        channel: form.channel,
        webhook_url: form.webhook_url || null,
      });
      setForm(emptyForm);
      load();
    } catch (err: any) {
      setError(err?.response?.data?.detail || "Failed to create policy.");
    } finally {
      setCreating(false);
    }
  };

  const toggleEnabled = async (p: EscalationPolicy) => {
    await api.patch(`/escalation-policies/${p.id}/toggle`).catch(async () => {
      // Fall back to PUT if a dedicated toggle route isn't wired for some reason.
      await api.put(`/escalation-policies/${p.id}`, { enabled: !p.enabled });
    });
    load();
  };

  const deletePolicy = async (p: EscalationPolicy) => {
    if (!confirm(`Delete escalation policy "${p.name}"?`)) return;
    await api.delete(`/escalation-policies/${p.id}`);
    load();
  };

  const runNow = async () => {
    setRunningNow(true);
    try {
      const res = await api.post("/escalation-policies/run-now");
      load();
      alert(`Sweep complete: ${res.data.escalations_fired} escalation(s) fired.`);
    } finally {
      setRunningNow(false);
    }
  };

  return (
    <div>
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold text-navy dark:text-white">Escalation Policies</h1>
          <p className="text-sm text-slate-500 mt-1 max-w-2xl">
            If an alert sits unacknowledged past a policy's window, it escalates — to a static contact, or to whoever an
            On-Call Schedule currently has on rotation.
          </p>
        </div>
        <button
          onClick={runNow}
          disabled={runningNow}
          className="bg-white dark:bg-noc-panel border border-slate-300 dark:border-noc-border text-navy dark:text-white rounded-lg px-4 py-2 text-sm font-semibold hover:bg-slate-50 dark:hover:bg-white/5 disabled:opacity-50"
        >
          {runningNow ? "Running…" : "Run sweep now"}
        </button>
      </div>

      {error && <p className="text-sm text-red-600 mt-3">{error}</p>}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-5 mt-5">
        <div className="lg:col-span-2 space-y-5">
          <div>
            <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-400 mb-2">Policies</h2>
            <div className="bg-white dark:bg-noc-panel border border-slate-200 dark:border-noc-border rounded-xl overflow-x-auto">
              <table className="w-full text-sm min-w-[680px]">
                <thead className="bg-navy text-white">
                  <tr>
                    <th className="text-left px-4 py-3 font-semibold">Name</th>
                    <th className="text-left px-4 py-3 font-semibold">Severity</th>
                    <th className="text-left px-4 py-3 font-semibold">Unack window</th>
                    <th className="text-left px-4 py-3 font-semibold">Notifies</th>
                    <th className="text-left px-4 py-3 font-semibold">Channel</th>
                    <th className="text-left px-4 py-3 font-semibold">Status</th>
                    <th className="text-left px-4 py-3 font-semibold"></th>
                  </tr>
                </thead>
                <tbody>
                  {loading && (
                    <tr>
                      <td colSpan={7} className="text-center text-slate-400 py-8">Loading policies…</td>
                    </tr>
                  )}
                  {!loading && policies.length === 0 && (
                    <tr>
                      <td colSpan={7} className="text-center text-slate-400 py-8">No escalation policies yet.</td>
                    </tr>
                  )}
                  {policies.map((p, i) => (
                    <tr key={p.id} className={i % 2 ? "bg-slate-50 dark:bg-white/5" : ""}>
                      <td className="px-4 py-3">
                        <p className="font-medium text-navy dark:text-white">{p.name}</p>
                        {p.description && <p className="text-xs text-slate-500">{p.description}</p>}
                      </td>
                      <td className="px-4 py-3">
                        <span className={`px-2 py-1 rounded-full text-xs font-medium ${SEVERITY_BADGE[p.severity_scope]}`}>
                          {p.severity_scope}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-slate-600 dark:text-slate-300">
                        {p.unack_minutes}m{p.repeat_minutes ? ` (repeat ${p.repeat_minutes}m)` : ""}
                      </td>
                      <td className="px-4 py-3 text-slate-600 dark:text-slate-300">
                        {p.on_call_schedule_id ? (
                          <span className="inline-flex items-center gap-1">
                            <span className="px-1.5 py-0.5 rounded-full text-[10px] font-semibold bg-brandblue/10 text-brandblue">on-call</span>
                            {scheduleName[p.on_call_schedule_id] || p.on_call_schedule_id}
                          </span>
                        ) : (
                          p.secondary_contacts || <span className="text-slate-400 italic">—</span>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        <span className={`px-2 py-1 rounded-full text-xs font-medium ${CHANNEL_BADGE[p.channel]}`}>{p.channel}</span>
                      </td>
                      <td className="px-4 py-3">
                        <button
                          onClick={() => toggleEnabled(p)}
                          className={`px-2 py-1 rounded-full text-xs font-medium ${p.enabled ? "bg-green-100 text-green-700" : "bg-slate-100 text-slate-500"}`}
                        >
                          {p.enabled ? "Enabled" : "Disabled"}
                        </button>
                      </td>
                      <td className="px-4 py-3">
                        <button onClick={() => deletePolicy(p)} className="text-xs text-red-500 hover:text-red-700 font-medium">
                          Delete
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <div>
            <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-400 mb-2">Escalation log</h2>
            <div className="bg-white dark:bg-noc-panel border border-slate-200 dark:border-noc-border rounded-xl overflow-x-auto">
              <table className="w-full text-sm min-w-[640px]">
                <thead className="bg-navy text-white">
                  <tr>
                    <th className="text-left px-4 py-3 font-semibold">Alert</th>
                    <th className="text-left px-4 py-3 font-semibold">Policy</th>
                    <th className="text-left px-4 py-3 font-semibold">Escalated</th>
                    <th className="text-left px-4 py-3 font-semibold">Count</th>
                    <th className="text-left px-4 py-3 font-semibold">Acked</th>
                  </tr>
                </thead>
                <tbody>
                  {!loading && log.length === 0 && (
                    <tr>
                      <td colSpan={5} className="text-center text-slate-400 py-8">No alerts have escalated yet.</td>
                    </tr>
                  )}
                  {log.map((l, i) => (
                    <tr key={l.id} className={i % 2 ? "bg-slate-50 dark:bg-white/5" : ""}>
                      <td className="px-4 py-3 text-slate-600 dark:text-slate-300">{l.category} — {l.message}</td>
                      <td className="px-4 py-3 text-navy dark:text-white font-medium">{l.escalation_policy_name || "—"}</td>
                      <td className="px-4 py-3 text-slate-500 whitespace-nowrap">
                        {l.last_escalated_at ? new Date(l.last_escalated_at).toLocaleString() : l.escalated_at ? new Date(l.escalated_at).toLocaleString() : "—"}
                      </td>
                      <td className="px-4 py-3 text-slate-600 dark:text-slate-300">{l.escalation_count}</td>
                      <td className="px-4 py-3">
                        <span className={`px-2 py-1 rounded-full text-xs font-medium ${l.acknowledged ? "bg-green-100 text-green-700" : "bg-red-100 text-red-700"}`}>
                          {l.acknowledged ? "Yes" : "No"}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>

        <div>
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-400 mb-2">New Policy</h2>
          <div className="bg-white dark:bg-noc-panel border border-slate-200 dark:border-noc-border rounded-xl p-4 space-y-3">
            <label className="text-xs text-slate-500 flex flex-col gap-1">
              Name
              <input
                className="border border-slate-300 dark:border-noc-border dark:bg-noc-panel rounded-lg px-3 py-1.5 text-sm"
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="Critical alerts — primary NOC"
              />
            </label>
            <label className="text-xs text-slate-500 flex flex-col gap-1">
              Description
              <input
                className="border border-slate-300 dark:border-noc-border dark:bg-noc-panel rounded-lg px-3 py-1.5 text-sm"
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
              />
            </label>
            <div className="grid grid-cols-2 gap-3">
              <label className="text-xs text-slate-500 flex flex-col gap-1">
                Severity scope
                <select
                  className="border border-slate-300 dark:border-noc-border dark:bg-noc-panel rounded-lg px-3 py-1.5 text-sm"
                  value={form.severity_scope}
                  onChange={(e) => setForm({ ...form, severity_scope: e.target.value as EscalationSeverityScope })}
                >
                  <option value="critical">Critical</option>
                  <option value="warning">Warning</option>
                  <option value="all">All</option>
                </select>
              </label>
              <label className="text-xs text-slate-500 flex flex-col gap-1">
                Unack minutes
                <input
                  type="number"
                  min={1}
                  className="border border-slate-300 dark:border-noc-border dark:bg-noc-panel rounded-lg px-3 py-1.5 text-sm"
                  value={form.unack_minutes}
                  onChange={(e) => setForm({ ...form, unack_minutes: Number(e.target.value) })}
                />
              </label>
            </div>
            <label className="text-xs text-slate-500 flex flex-col gap-1">
              Repeat every (minutes, optional)
              <input
                type="number"
                min={1}
                className="border border-slate-300 dark:border-noc-border dark:bg-noc-panel rounded-lg px-3 py-1.5 text-sm"
                value={form.repeat_minutes}
                onChange={(e) => setForm({ ...form, repeat_minutes: e.target.value === "" ? "" : Number(e.target.value) })}
                placeholder="Leave blank for one-shot"
              />
            </label>

            <label className="text-xs text-slate-500 flex flex-col gap-1">
              On-call schedule (preferred — overrides contacts below for email)
              <select
                className="border border-slate-300 dark:border-noc-border dark:bg-noc-panel rounded-lg px-3 py-1.5 text-sm"
                value={form.on_call_schedule_id}
                onChange={(e) => setForm({ ...form, on_call_schedule_id: e.target.value })}
              >
                <option value="">— None —</option>
                {schedules.map((s) => (
                  <option key={s.id} value={s.id}>{s.name}</option>
                ))}
              </select>
            </label>

            <label className="text-xs text-slate-500 flex flex-col gap-1">
              Secondary contacts (comma-separated emails, fallback)
              <input
                className="border border-slate-300 dark:border-noc-border dark:bg-noc-panel rounded-lg px-3 py-1.5 text-sm"
                value={form.secondary_contacts}
                onChange={(e) => setForm({ ...form, secondary_contacts: e.target.value })}
                placeholder="oncall@company.com"
              />
            </label>

            <label className="text-xs text-slate-500 flex flex-col gap-1">
              Channel
              <select
                className="border border-slate-300 dark:border-noc-border dark:bg-noc-panel rounded-lg px-3 py-1.5 text-sm"
                value={form.channel}
                onChange={(e) => setForm({ ...form, channel: e.target.value as EscalationChannel })}
              >
                <option value="email">Email</option>
                <option value="webhook">Webhook</option>
                <option value="slack">Slack</option>
                <option value="teams">Teams</option>
                <option value="push">Push notification</option>
              </select>
            </label>

            {form.channel === "push" && (
              <p className="text-xs text-slate-400">
                Sends to every device subscribed to push notifications for this alert's severity (configure devices under Push Settings). No URL needed.
              </p>
            )}

            {form.channel !== "email" && form.channel !== "push" && (
              <label className="text-xs text-slate-500 flex flex-col gap-1">
                Webhook URL
                <input
                  className="border border-slate-300 dark:border-noc-border dark:bg-noc-panel rounded-lg px-3 py-1.5 text-sm"
                  value={form.webhook_url}
                  onChange={(e) => setForm({ ...form, webhook_url: e.target.value })}
                  placeholder="https://hooks.slack.com/..."
                />
              </label>
            )}

            <button
              onClick={createPolicy}
              disabled={creating || !form.name.trim()}
              className="w-full bg-brandblue text-white rounded-lg px-4 py-2 text-sm font-semibold hover:opacity-90 disabled:opacity-50"
            >
              {creating ? "Creating…" : "Create Policy"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}